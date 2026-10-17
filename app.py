"""Review app. Approve or reject each proposal. Only an Approve click writes to the CRM.

Run from the project folder:  streamlit run app.py
"""
import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

import crm
import match
import rules
import scrape
from crm import BELLHAVEN_PARENT_ID
from pipeline import SCHEMA, STATE_DB

st.set_page_config(page_title="Bellhaven review", layout="wide")


def db():
    conn = sqlite3.connect(STATE_DB)
    conn.row_factory = sqlite3.Row
    conn.execute(SCHEMA)
    return conn


def note_text(row, new_id=None):
    """The dated line added to the account's note: what was done, in past tense."""
    ch, ev = json.loads(row["changes"]), json.loads(row["evidence"])
    c, t = ev["crm"] or {}, row["type"]
    text = {
        "create": "Created from the Bellhaven website listing. No existing CRM account was found at this address.",
        "chow": f"Change of ownership (CHOW): now owned by Bellhaven Senior Living, see account {new_id}. "
                "Kept unchanged per the billing SOP (revenue and unpaid AR on file).",
        "chow_existing": f"Change of ownership (CHOW): no longer listed by Bellhaven. The new owner's account is "
                         f"{ch.get('chow_current_account')}. Kept unchanged per the billing SOP (revenue and unpaid AR on file).",
        "reparent": f"Moved under Bellhaven Senior Living (was {(c.get("parent_name") or "no parent").replace(" (Parent Account)", "")}) to match the Bellhaven website.",
        "update_name": f"Renamed from \"{c.get('name')}\" to match the Bellhaven website.",
        "update_address": f"Address updated to match the Bellhaven website (was {address_line(c)}).",
        "mark_duplicate": f"Duplicate of account {ch.get('duplicate_of_account')}. Marked Inactive.",
        "orphan_review": "No longer listed on the Bellhaven website and no new owner found. Needs review.",
    }[t]
    if t == "reparent" and "name" in ch:
        text += f" Renamed from \"{c.get('name')}\"."
    return f"{date.today().isoformat()} - Bellhaven website review: {text}"


def stamp(account_id, fields, note_line):
    """PATCH fields onto an account and append a dated note. Keeps the existing note."""
    acct = crm.get_account(account_id)
    old = (acct.get("note") or "").strip()
    fields = {k: v for k, v in fields.items() if k != "note"}  # the proposal's note is replaced by the reason
    return crm.patch_account(account_id, {**fields, "note": f"{old} | {note_line}" if old else note_line})


def existing_under_bellhaven(web):
    """A Bellhaven account already at this location. Means an earlier, half-finished run created it."""
    for a in crm.get_all_accounts():
        if a["parent_id"] == BELLHAVEN_PARENT_ID and match.match_method(web, a):
            return a["account_id"]
    return None


def create_or_reuse(row, progress, note_line, web):
    """Create the facility under Bellhaven, unless it is already there from an earlier attempt."""
    if "created_id" not in progress:
        found = existing_under_bellhaven(web)
        if found:
            progress["created_id"] = found
            progress["created_reused"] = True
        else:
            changes = json.loads(row["changes"])
            # create proposals use "new_account"; chow proposals use "create_new_account"
            body = {**(changes.get("new_account") or changes["create_new_account"]), "note": note_line}
            resp = crm.create_account(body)
            if not resp.get("account_id"):
                raise RuntimeError(f"Create returned no account_id: {resp}")
            if "parent_id" not in resp:  # the create reply may be partial; read the account back to check it
                resp = {**resp, **crm.get_account(resp["account_id"])}
            if resp.get("parent_id") != BELLHAVEN_PARENT_ID:
                raise RuntimeError(f"Created account is not under Bellhaven: {resp}")
            progress["created_id"] = resp["account_id"]
            progress["create_response"] = resp
        save_progress(row["key"], progress)  # saved before the next call, so a retry skips this step
    return progress["created_id"]


def save_progress(key, progress):
    conn = db()
    conn.execute("UPDATE proposals SET api_response=? WHERE key=?", (json.dumps(progress), key))
    conn.commit()
    conn.close()


def apply_proposal(row):
    """Run the CRM calls for one approved proposal. Raises on failure; the row stays pending."""
    changes = json.loads(row["changes"])
    targets = json.loads(row["target_ids"])
    web = json.loads(row["evidence"])["website"]
    progress = json.loads(row["api_response"] or "{}")
    note_line = note_text(row)

    if row["type"] in ("create", "chow"):
        new_note = f"{date.today().isoformat()} - Bellhaven website review: Created under Bellhaven Senior Living" + (
            f" after a change of ownership (CHOW). Previous owner's account: {targets[0]}." if row["type"] == "chow"
            else ". No existing CRM account was found at this address.")
        new_id = create_or_reuse(row, progress, new_note, web)
        if row["type"] == "chow":
            progress["old_response"] = stamp(targets[0], {"chow_current_account": new_id}, note_text(row, new_id))
    elif row["type"] == "chow_existing":
        progress["response"] = stamp(targets[0], {"chow_current_account": changes["chow_current_account"]}, note_line)
    else:  # update_name, update_address, reparent, mark_duplicate, orphan_review
        if "parent_id" in changes and rules.plan_reparent(crm.get_account(targets[0]), BELLHAVEN_PARENT_ID)["action"] == "chow":
            raise RuntimeError("Billing changed since this was proposed: it now has revenue and unpaid AR, so the SOP "
                               "forbids moving it. Reject this card; the next run proposes a CHOW instead.")
        progress["response"] = stamp(targets[0], changes, note_line)
    return progress


def decide(row_key, status, reason=None, progress=None):
    conn = db()
    conn.execute("UPDATE proposals SET status=?, reject_reason=?, decided_at=?, api_response=COALESCE(?, api_response) WHERE key=? AND status='pending'",
                 (status, reason, datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  json.dumps(progress) if progress is not None else None, row_key))
    conn.commit()
    conn.close()


LABEL = {  # plain names for the reviewer
    "create": "Add new facility", "chow": "Change of ownership (CHOW): keep old account, add new one",
    "chow_existing": "Link old account to new owner (CHOW)", "reparent": "Move under Bellhaven",
    "update_address": "Fix address", "update_name": "Fix name",
    "mark_duplicate": "Retire duplicate record", "orphan_review": "Not on website: flag for review",
}
CHECK = {  # one-line "what to check before approving" per type
    "create": "Confirm this facility is on the website and not already in the CRM under another name.",
    "chow": "Confirm the website shows a new owner at the same address, then check the billing line.",
    "chow_existing": "Confirm the new-owner account is the same location, then check the billing line.",
    "reparent": "Confirm the account is the same street address as the website listing, then check the billing line.",
    "update_address": "Confirm the website address is the current one for this same facility.",
    "update_name": "Confirm it is the same facility at the same address, just renamed.",
    "mark_duplicate": "Confirm both records are the same street address; the survivor keeps the history.",
    "orphan_review": "Search the website once more by name and address before sending it to a person.",
}
ORDER = list(LABEL)  # the order groups appear in the queue
ADDRESS = ("billing_street", "billing_city", "billing_state", "billing_zip")


def site_name(row):
    ev = json.loads(row["evidence"])
    return (ev["website"] or {}).get("name") or (ev["crm"] or {}).get("name") or row["site_slug"]


def address_line(d):
    return f"{d.get('billing_street') or ''}, {d.get('billing_city') or ''}, {d.get('billing_state') or ''} {d.get('billing_zip') or ''}"


def describe(row):
    """What will change, in plain words. The raw JSON stays in the database."""
    ch, ev = json.loads(row["changes"]), json.loads(row["evidence"])
    crm_rec = ev["crm"] or {}
    t = row["type"]
    if t == "create":
        new = ch["new_account"]
        return [f"Create **{new['name']}** under Bellhaven at {address_line(new)}"]
    if t == "chow":
        new = ch["create_new_account"]
        return [f"Create **{new['name']}** under Bellhaven at {address_line(new)}",
                f"Point **{crm_rec.get('name')}** at the new account. The old account is otherwise left as it is."]
    if t == "chow_existing":
        target = (ev.get("successor") or {}).get("name") or ch["chow_current_account"]
        return [f"Point **{crm_rec.get('name')}** at **{target}**"]
    if t == "mark_duplicate":
        return [f"Mark **{crm_rec.get('name')}** Inactive and point it at account {ch['duplicate_of_account']}"]
    if t == "orphan_review":
        return ["Set status to **Needs Review**. A person decides what happens next."]
    lines = []
    if "parent_id" in ch:
        lines.append(f"Move under Bellhaven (now under {crm_rec.get('parent_name') or 'no parent'})")
    if "name" in ch:
        lines.append(f"Rename **{crm_rec.get('name')}** to **{ch['name']}**")
    if any(k in ch for k in ADDRESS):
        old = address_line(crm_rec)
        lines.append(f"Address: {old} → {address_line({**crm_rec, **ch})}")
    return lines


MONEY = ("lifetime_revenue", "outstanding_ar")
PRETTY = {"name": "Name", "billing_street": "Street", "billing_city": "City", "billing_state": "State",
          "billing_zip": "Zip", "care_offerings": "Care", "care_type": "Care type", "phone": "Phone",
          "parent_name": "Parent", "status": "Status", "lifetime_revenue": "Lifetime revenue",
          "outstanding_ar": "Unpaid AR", "account_id": "CRM id"}
review_card = components.declare_component("review_card", path=str(Path(__file__).parent / "review_card"))


def billing_check(row):
    """The SOP billing rule as a plain sentence, for types that depend on it."""
    if row["type"] not in ("chow", "reparent", "chow_existing"):
        return ""
    c = json.loads(row["evidence"])["crm"] or {}
    out = f"Billing check: revenue ${rules._num(c.get('lifetime_revenue')):,.0f}, unpaid AR ${rules._num(c.get('outstanding_ar')):,.0f} → "
    chow = rules.plan_reparent({**c, "account_id": c.get("account_id")}, BELLHAVEN_PARENT_ID)["action"] == "chow"  # same rule Approve uses
    return out + ("both above 0, so billing keeps the old account and a new one is created (CHOW)" if chow
                  else "not both above 0, so move directly")


def hero(row):
    """Structured version of describe(): [field, before, after] for field edits, else a plain line."""
    ch, ev = json.loads(row["changes"]), json.loads(row["evidence"])
    crm_rec = ev["crm"] or {}
    if row["type"] not in ("reparent", "update_address", "update_name"):
        return describe(row)
    out = []
    if "parent_id" in ch:
        out.append(["Parent", crm_rec.get("parent_name") or "no parent", "Bellhaven"])
    if "name" in ch:
        out.append(["Name", crm_rec.get("name") or "", ch["name"]])
    if any(k in ch for k in ADDRESS):
        out.append(["Address", address_line(crm_rec), address_line({**crm_rec, **ch})])
    return out


def same_fields(ev):
    """Labels whose website and CRM values match once normalized (Dr = Drive), so the card doesn't flag them."""
    w, c = ev["website"] or {}, ev["crm"] or {}
    return [PRETTY.get(k, k) for k in w if k in c
            and match.norm_street(str(w[k])) == match.norm_street(str(c[k]))]


def pairs(d):
    """Label/value pairs for the card, skipping empty fields."""
    out = []
    for k, v in d.items():
        if v in (None, "", []):
            continue
        if k in MONEY:
            v = f"${v:,.0f}"
        elif isinstance(v, list):
            v = ", ".join(v)
        out.append([PRETTY.get(k, k), str(v)])
    return out


def queue(types, skipped):
    conn = db()
    rows = conn.execute("SELECT * FROM proposals WHERE status='pending'").fetchall()
    conn.close()
    rows = [r for r in rows if r["type"] in types and r["key"] not in skipped]
    # Flagged items first, then by type, then by site name.
    return sorted(rows, key=lambda r: (not r["flag"], ORDER.index(r["type"]), site_name(r)))


def handle(row, result, attempts, skipped):
    act = result["action"]
    if act == "skip":
        skipped.append(row["key"])
        st.rerun()
    if act == "unskip":  # bring back the last skipped card and show it next
        if skipped:
            st.session_state["front"] = skipped.pop()
        attempts[row["key"]] = attempts.get(row["key"], 0) + 1  # new card key, so the "unskip" value isn't replayed
        st.rerun()
    conn = db()  # another tab may have decided this card already
    still_pending = conn.execute("SELECT status FROM proposals WHERE key=?", (row["key"],)).fetchone()[0] == "pending"
    conn.close()
    if not still_pending:
        st.rerun()
    if act == "reject":
        decide(row["key"], "rejected", result.get("reason") or None)
        st.rerun()
    # Approve writes to the CRM. Rerun outside the try, or the rerun signal would be read as a failure.
    try:
        with st.spinner("Writing to the CRM…"):
            progress = apply_proposal(row)
            decide(row["key"], "approved", progress=progress)
    except Exception as e:  # stays pending; saved progress lets a retry skip finished steps
        attempts[row["key"]] = attempts.get(row["key"], 0) + 1  # new card key, so the card shows again
        st.session_state["last_error"] = f"Not written, still pending. Retry is safe. Error: {e}"
    st.rerun()


def review_one(types):
    skipped = st.session_state.setdefault("skipped", [])  # list keeps skip order for unskip
    attempts = st.session_state.setdefault("attempts", {})
    rows = queue(types, skipped)
    if not rows:
        st.success("All caught up.")
        if skipped and st.button(f"Review the {len(skipped)} skipped"):
            skipped.clear()
            st.rerun()
        return
    row = next((r for r in rows if r["key"] == st.session_state.get("front")), rows[0])
    if skipped:
        st.caption(f"{len(skipped)} skipped for now")
    if err := st.session_state.pop("last_error", None):
        st.error(err)
    ev = json.loads(row["evidence"])
    result = review_card(
        card_id=row["key"], ctype=row["type"], hero=hero(row), label=LABEL[row["type"]], title=site_name(row), flag=row["flag"], check=CHECK[row["type"]], sop=billing_check(row), reason=row["reason"],
        changes=[line.replace("**", "") for line in describe(row)],
        website=pairs(ev["website"] or {}), crm=pairs(ev["crm"] or {}), same=same_fields(ev), can_unskip=bool(skipped),
        site_url=f"{scrape.SITE}/communities/{row['site_slug']}" if ev["website"] else "",
        key=f"{row['key']}-{attempts.get(row['key'], 0)}", default=None)
    if result and result.get("action"):
        handle(row, result, attempts, skipped)


def history_tab():
    conn = db()
    rows = conn.execute("SELECT * FROM proposals WHERE status!='pending' ORDER BY decided_at DESC").fetchall()
    conn.close()
    if not rows:
        st.info("No decisions yet.")
        return
    st.dataframe([{
        "Site": site_name(r), "Change": LABEL[r["type"]],
        "Decision": "Approved" if r["status"] == "approved" else "Rejected",
        "When (UTC)": (r["decided_at"] or "").replace("T", " ").replace("+00:00", ""),
        "Reason": r["reject_reason"] or "",
        "CRM id": (json.loads(r["target_ids"]) or [json.loads(r["api_response"] or "{}").get("created_id", "")])[0],
    } for r in rows], width="stretch", hide_index=True)


def lookup_tab():
    """Read one account live from the CRM, to confirm an approved change landed."""
    with st.form("lookup"):  # fetch only on submit, so reruns from other tabs don't repeat the GET
        account_id = st.text_input("CRM account id", placeholder="e.g. 001LGFPBJY4N9MB6KL (copy it from History)").strip()
        submitted = st.form_submit_button("Look up")
    if not (submitted and account_id):
        return
    if not account_id.isalnum():  # a "/" or ".." would reach a different API path
        st.error("A CRM id is letters and digits only, no spaces or slashes.")
        return
    try:
        acct = crm.get_account(account_id)
    except Exception as e:
        st.error(f"Could not read {account_id}: {e}")
        return
    if acct.get("parent_id") == BELLHAVEN_PARENT_ID:
        st.success(f"{acct.get('name')} is under Bellhaven Senior Living.")
    st.dataframe([{"Field": k, "Value": str(v)} for k, v in acct.items()], width="stretch", height="content", hide_index=True)


def counts():
    conn = db()
    out = dict(conn.execute("SELECT status, count(*) FROM proposals GROUP BY status").fetchall())
    conn.close()
    return out.get("pending", 0), out.get("approved", 0), out.get("rejected", 0)


# Notion-style page header and view tabs.
st.markdown("""<style>
.block-container { padding-top: 1.25rem; max-width: 100%; padding-left: 4rem; padding-right: 4rem; }
[data-testid="stHeader"], [data-testid="stToolbar"], [data-testid="stAppDeployButton"] { display: none; }
html, body, .stApp { font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI Variable Display", "Segoe UI", Helvetica, "Apple Color Emoji", Arial, sans-serif; }
.bh-crumb { font-size: 14px; color: rgba(55,53,47,.45); }
.bh-crumb b { color: rgba(55,53,47,.65); font-weight: 500; }
.stTabs [data-baseweb="tab-list"] { gap: 4px; border-bottom: 1px solid rgba(55,53,47,.09); }
.stTabs [data-baseweb="tab"] { color: rgba(55,53,47,.65); font-size: 14px; padding: 6px 8px; height: auto; }
.stTabs [aria-selected="true"] { color: #37352F; }
.stTabs [data-baseweb="tab-highlight"] { background-color: #37352F; height: 2px; }
.stTabs [data-baseweb="tab-border"] { display: none; }
</style>""", unsafe_allow_html=True)
pending_n, approved_n, rejected_n = counts()
st.markdown(f'<div class="bh-crumb"><b>Bellhaven review</b> &nbsp;/&nbsp; {pending_n} to review · {approved_n} approved · {rejected_n} rejected</div>', unsafe_allow_html=True)

to_review, history, lookup = st.tabs(["To review", "History", "Look up in CRM"])
with to_review:
    review_one(ORDER)
with history:
    history_tab()
with lookup:
    lookup_tab()
