"""Match website communities to CRM accounts and build review proposals.

Read-only: this only produces proposals. Nothing is written to the CRM here.
"""
import json
import re

from rapidfuzz import fuzz

from crm import BELLHAVEN_PARENT_ID
from rules import CARE_MAP, NAME_MATCH_MIN, NEAR_NAME_MIN, has_billing_history, plan_orphan, plan_reparent

ACCOUNTS_FILE = "data/accounts.json"
COMMUNITIES_FILE = "data/communities.json"
PROPOSALS_FILE = "data/proposals.json"

# Compare street words in short form, so "Street" and "St" are the same.
ABBREV = {
    "street": "st", "avenue": "ave", "road": "rd", "drive": "dr", "lane": "ln",
    "boulevard": "blvd", "pike": "pk", "highway": "hwy", "parkway": "pkwy",
    "court": "ct", "circle": "cir", "place": "pl", "trail": "trl",
    "north": "n", "south": "s", "east": "e", "west": "w",
    "northeast": "ne", "northwest": "nw", "southeast": "se", "southwest": "sw",
}
COMPLETE_FIELDS = ("name", "parent_id", "billing_street", "billing_city", "billing_state",
                   "billing_zip", "care_type", "phone")


def norm_text(s):
    """Lowercase, drop punctuation, collapse spaces."""
    return " ".join(re.sub(r"[^\w\s]", " ", (s or "").lower()).split())


def norm_name(s):
    """Like norm_text, but a leading "The" does not count (The Arbors = Arbors)."""
    t = norm_text(s)
    return t[4:] if t.startswith("the ") else t


def norm_street(s):
    return " ".join(ABBREV.get(w, w) for w in norm_text(s).split())


def zip5(z):
    return (z or "").strip()[:5]


def phone_digits(p):
    return re.sub(r"\D", "", p or "")[-10:]


def site_view(site):
    """Put a website record in the same shape as a CRM account, so one set of helpers works for both."""
    return {
        "slug": site["slug"], "name": site["name"], "phone": site["phone"],
        "billing_street": site["street"], "billing_city": site["city"],
        "billing_state": site["state"], "billing_zip": site["zip"],
        "care_offerings": site["care_offerings"],
    }


def care_type_for(offerings):
    for o in offerings:
        if o in CARE_MAP:
            return CARE_MAP[o]
    return ""


def name_score(web, acct):
    return round(fuzz.token_sort_ratio(norm_name(web["name"]), norm_name(acct["name"])))


def same_city_state(a, b):
    return (norm_text(a["billing_city"]) == norm_text(b["billing_city"])
            and (a["billing_state"] or "").upper() == (b["billing_state"] or "").upper())


def match_method(web, acct):
    """Return how the account matches the website location, or None. Strongest evidence first."""
    same_street = norm_street(web["billing_street"]) == norm_street(acct["billing_street"])
    if same_street and zip5(web["billing_zip"]) == zip5(acct["billing_zip"]):
        return "address + zip"
    if same_street and same_city_state(web, acct):
        return "address + city/state (zip differs)"
    # A name alone is never enough across cities or states.
    if same_city_state(web, acct) and name_score(web, acct) >= NAME_MATCH_MIN:
        return "name + city/state (address differs)"
    return None


def same_address(a, b):
    return norm_street(a["billing_street"]) == norm_street(b["billing_street"]) and same_city_state(a, b)


def completeness(acct):
    return sum(1 for f in COMPLETE_FIELDS if acct.get(f))


def pick_survivor(web, members):
    """members: list of (account, method, score). Order: under Bellhaven, billing history,
    phone matches website, most complete, then account_id as a stable tie-break."""
    def rank(item):
        a = item[0]
        return (
            a["status"] == "Inactive",
            a["parent_id"] != BELLHAVEN_PARENT_ID,
            not has_billing_history(a),
            phone_digits(a["phone"]) != phone_digits(web["phone"]),
            -completeness(a),
            a["account_id"],
        )
    return min(members, key=rank)


def address_changes(web, acct):
    """Only the fields that really differ after normalizing. Values are the website's."""
    changes = {}
    # A PO box is a valid billing (mailing) address; the website shows the building. Keep it.
    po_box = re.match(r"p\s*o\s*box\b", norm_text(acct["billing_street"]))
    if not po_box and norm_street(web["billing_street"]) != norm_street(acct["billing_street"]):
        changes["billing_street"] = web["billing_street"]
    if not same_city_state(web, acct):
        changes["billing_city"] = web["billing_city"]
        changes["billing_state"] = web["billing_state"]
    if zip5(web["billing_zip"]) != zip5(acct["billing_zip"]):
        changes["billing_zip"] = web["billing_zip"]
    return changes


def crm_summary(a):
    return {k: a.get(k) for k in ("account_id", "name", "parent_name", "billing_street", "billing_city",
                                  "billing_state", "billing_zip", "care_type", "status", "phone",
                                  "lifetime_revenue", "outstanding_ar")}


def proposal(kind, key, targets, changes, reason, web=None, crm=None, method=None, score=None, flag=None):
    p = {
        "type": kind,
        "key_source": key,
        "target_ids": targets,
        "changes": changes,
        "evidence": {"website": web and {k: web[k] for k in ("name", "billing_street", "billing_city", "billing_state", "billing_zip", "care_offerings", "phone")},
                     "crm": crm and crm_summary(crm),
                     "match_method": method, "name_score": score},
        "reason": reason,
    }
    if flag:
        p["flag"] = flag
    return p


def build_for_site(web, members):
    """All proposals for one website location that already has CRM accounts."""
    survivor, method, score = pick_survivor(web, members)
    out = []
    for acct, m, s in members:
        if acct is survivor or acct["chow_current_account"] or acct["duplicate_of_account"]:  # a linked CHOW account stays exactly as it is (SOP)
            continue
        # A CHOW that was approved halfway: the new Bellhaven copy exists, but the old
        # account (with money on it) is not linked yet. Link it, do not mark it a duplicate.
        if (survivor["parent_id"] == BELLHAVEN_PARENT_ID and acct["parent_id"] != BELLHAVEN_PARENT_ID
                and has_billing_history(acct) and not acct["chow_current_account"]):
            out.append(proposal(
                "chow_existing", acct["account_id"], [acct["account_id"]],
                {"chow_current_account": survivor["account_id"]},
                f"Old account with billing history, and {survivor['name']} under Bellhaven is already at the same place. "
                f"Link the old account to the new one (a CHOW, change of ownership, that stopped halfway).",
                web, acct, m, s))
            continue
        out.append(proposal(
            "mark_duplicate", web["slug"], [acct["account_id"]],
            {"duplicate_of_account": survivor["account_id"], "status": "Inactive"},
            f"Same location as {survivor['name']} ({survivor['account_id']}). This copy is the duplicate, so point it at the survivor and set it Inactive.",
            web, acct, m, s))

    name_fix = {"name": web["name"]} if norm_name(web["name"]) != norm_name(survivor["name"]) else {}
    addr_fix = address_changes(web, survivor)
    if survivor["parent_id"] != BELLHAVEN_PARENT_ID:
        plan = plan_reparent(survivor, BELLHAVEN_PARENT_ID)
        was = survivor["parent_name"] or "no parent"
        if plan["action"] == "chow":
            new_account = {
                "name": web["name"], "parent_id": BELLHAVEN_PARENT_ID,
                "billing_street": web["billing_street"], "billing_city": web["billing_city"],
                "billing_state": web["billing_state"], "billing_zip": web["billing_zip"],
                "care_type": care_type_for(web["care_offerings"]), "phone": web["phone"], "status": "Active",
            }
            out.append(proposal(
                "chow", web["slug"], [survivor["account_id"]],
                {"create_new_account": new_account, "chow_current_account": "(new account id)"},
                f"Under {was} with revenue and unpaid AR, so the CHOW rule applies: billing needs the old account kept as is. Create a new account for the facility under Bellhaven and link the old account to it.",
                web, survivor, method, score))
        else:
            out.append(proposal(
                "reparent", web["slug"], [survivor["account_id"]],
                {"parent_id": BELLHAVEN_PARENT_ID, **name_fix, **addr_fix},
                f"Under {was} with no unpaid balance on a billed account (revenue or unpaid AR is zero), so move it directly under Bellhaven.",
                web, survivor, method, score))
    else:
        if name_fix:
            out.append(proposal("update_name", web["slug"], [survivor["account_id"]], name_fix,
                                "Website name differs from the CRM name. Use the website name.",
                                web, survivor, method, score))
        if addr_fix:
            out.append(proposal("update_address", web["slug"], [survivor["account_id"]], addr_fix,
                                "Website address differs from the CRM address. Use the website address.",
                                web, survivor, method, score))
    return out


def build_create(web, unmatched):
    """No CRM account matches this location. Flag unmatched accounts nearby so a human checks first."""
    flags = []
    for a in unmatched:
        s = name_score(web, a)
        if same_city_state(web, a) or s >= NEAR_NAME_MIN:
            flags.append(f"Nearby but not matched: {a['name']} ({a['billing_street']}, {a['billing_city']}, "
                         f"{a['billing_state']}; {a['parent_name'] or 'no parent'}; name score {round(s)}). "
                         f"Different address, so treated as a different facility; check before approving.")
    if len(web["care_offerings"]) > 1:
        flags.append("Website lists more than one offering. care_type uses the first one that maps.")
    new_account = {
        "name": web["name"], "parent_id": BELLHAVEN_PARENT_ID,
        "billing_street": web["billing_street"], "billing_city": web["billing_city"],
        "billing_state": web["billing_state"], "billing_zip": web["billing_zip"],
        "care_type": care_type_for(web["care_offerings"]), "phone": web["phone"], "status": "Active",
    }
    return proposal("create", web["slug"], [], {"new_account": new_account},
                    "No CRM account is at this location, so create it under Bellhaven.",
                    web, None, None, None, "; ".join(flags) or None)


def build_orphans(accounts, matched_ids):
    """Bellhaven accounts the website no longer lists."""
    out = []
    for a in accounts:
        if (a["parent_id"] != BELLHAVEN_PARENT_ID or a["account_id"] in matched_ids
                or a["chow_current_account"] or a["status"] == "Needs Review"):  # already handled
            continue
        successor = next((b for b in accounts
                          if b["parent_id"] != BELLHAVEN_PARENT_ID and b["account_id"] not in matched_ids
                          and same_address(a, b)), None)
        plan = plan_orphan(a, successor)
        if plan["action"] == "chow_existing":
            out.append(proposal(
                "chow_existing", a["account_id"], [a["account_id"]],
                {"chow_current_account": successor["account_id"]},
                f"Not on the website and has revenue and unpaid AR. {successor['name']} under "
                f"{successor['parent_name']} is at the same address, so it is probably the new owner. "
                f"Leave this account as is and point it at the new owner, instead of making a third copy.",
                None, a, None, None, None))
            out[-1]["evidence"]["successor"] = crm_summary(successor)
        else:
            out.append(proposal(
                "orphan_review", a["account_id"], [a["account_id"]],
                {"status": "Needs Review", "note": "Not on the website. No new owner found at this address. Needs a human check."},
                "Not on the website, and no new owner found at the same address. Mark it Needs Review.",
                None, a))
    return out


def build_all(accounts, sites):
    """Every proposal for this run. Pure: reads the lists it is given, writes nothing."""
    webs = [site_view(s) for s in sites]
    clusters = {}
    for w in webs:
        clusters[w["slug"]] = [(a, m, name_score(w, a)) for a in accounts
                               if (m := match_method(w, a))]

    # One CRM account matching two website locations would be a bug. Make it loud.
    seen = {}
    for slug, members in clusters.items():
        for a, _, _ in members:
            seen.setdefault(a["account_id"], []).append(slug)
    for aid, slugs in seen.items():
        if len(slugs) > 1:
            print(f"WARNING: {aid} matches several website locations: {slugs}")
    matched_ids = set(seen)

    proposals = []
    confident = []
    for w in webs:
        members = clusters[w["slug"]]
        if not members:
            proposals.append(build_create(w, [a for a in accounts if a["account_id"] not in matched_ids]))
        else:
            props = build_for_site(w, members)
            if props:
                proposals.extend(props)
            else:
                confident.append(w["slug"])
    proposals.extend(build_orphans(accounts, matched_ids))
    print(f"Website locations: {len(webs)}  |  CRM accounts matched: {len(matched_ids)}")
    print(f"Confident matches with no proposal: {len(confident)}")
    return proposals


def main():
    with open(ACCOUNTS_FILE, encoding="utf-8") as f:
        accounts = json.load(f)
    with open(COMMUNITIES_FILE, encoding="utf-8") as f:
        sites = json.load(f)
    proposals = build_all(accounts, sites)
    with open(PROPOSALS_FILE, "w", encoding="utf-8") as f:
        json.dump(proposals, f, indent=2)
    print(f"Saved {len(proposals)} proposals to {PROPOSALS_FILE}")
    counts = {}
    for p in proposals:
        counts[p["type"]] = counts.get(p["type"], 0) + 1
    print("Proposals by type:", dict(sorted(counts.items())))
    for p in proposals:
        ids = ",".join(p["target_ids"]) or "-"
        flag = f"  [FLAG: {p['flag']}]" if p.get("flag") else ""
        print(f"  {p['type']:<15} {p['key_source']:<38} {ids:<20} {json.dumps(p['changes'])}{flag}")


if __name__ == "__main__":
    main()
