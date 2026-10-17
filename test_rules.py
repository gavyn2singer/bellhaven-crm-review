"""Checks for the SOP branches in rules.py and a dry run of app.apply_proposal over every pending row.

Run from the project folder:  .venv\\Scripts\\python.exe test_rules.py
No network: the CRM is mocked, and requests is blocked. state.db is only read; the dry run uses a temp copy.
"""
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import rules  # noqa: E402

BH = "0015QAPLGS3FVYEEEM"


def test_plan_reparent():
    old, parent = "001OLD", BH
    cases = [
        ({"lifetime_revenue": 500, "outstanding_ar": 10}, "chow"),
        ({"lifetime_revenue": 500, "outstanding_ar": 0}, "reparent"),
        ({"lifetime_revenue": 0, "outstanding_ar": 10}, "reparent"),
        ({"lifetime_revenue": 0, "outstanding_ar": 0}, "reparent"),
        ({"lifetime_revenue": None, "outstanding_ar": None}, "reparent"),
        ({"lifetime_revenue": "", "outstanding_ar": ""}, "reparent"),
        ({}, "reparent"),
        ({"lifetime_revenue": "abc", "outstanding_ar": 10}, "reparent"),
    ]
    for fields, want in cases:
        acct = {"account_id": old, **fields}
        got = rules.plan_reparent(acct, parent)["action"]
        assert got == want, f"plan_reparent({fields}) gave {got}, want {want}"
    chow = rules.plan_reparent({"account_id": old, "lifetime_revenue": 1, "outstanding_ar": 1}, parent)
    assert chow == {"action": "chow", "old_account_id": old}, chow
    move = rules.plan_reparent({"account_id": old}, parent)
    assert move == {"action": "reparent", "account_id": old, "parent_id": parent}, move
    print("rules.plan_reparent: 8 cases OK")


def test_plan_orphan():
    acct = {"account_id": "001OLD", "lifetime_revenue": 900, "outstanding_ar": 40}
    succ = {"account_id": "001NEW"}
    got = rules.plan_orphan(acct, succ)
    assert got == {"action": "chow_existing", "old_account_id": "001OLD", "successor_id": "001NEW"}, got
    assert rules.plan_orphan(acct, None) == {"action": "orphan_review", "account_id": "001OLD"}
    no_money = {"account_id": "001OLD", "lifetime_revenue": 0, "outstanding_ar": None}
    assert rules.plan_orphan(no_money, succ)["action"] == "orphan_review"
    assert rules.plan_orphan(no_money, None)["action"] == "orphan_review"
    print("rules.plan_orphan: 4 cases OK")


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_apply_proposal_dry_run():
    real_db = HERE / "state.db"
    real_hash_before = _sha(real_db)

    tmpdir = tempfile.mkdtemp(prefix="rules_test_")
    try:
        temp_db = os.path.join(tmpdir, "state_copy.db")
        shutil.copy(real_db, temp_db)
        os.environ["BH_TOKEN"] = "dummy-token-for-tests"

        # Block any real HTTP. Every CRM call below is mocked anyway.
        import requests

        def no_network(*a, **k):
            raise AssertionError("network call attempted during dry run")

        for name in ("get", "post", "patch", "put", "delete", "request"):
            setattr(requests, name, no_network)

        # Streamlit stubs. Module-level UI in app.py must not crash or act.
        st = mock.MagicMock()
        st.session_state = {}
        st.tabs.side_effect = lambda names: tuple(mock.MagicMock() for _ in names)
        st.sidebar.multiselect.return_value = list(rules_labels())
        comps = mock.MagicMock()
        comps.declare_component.return_value = mock.MagicMock(return_value=None)
        sys.modules["streamlit"] = st
        sys.modules["streamlit.components"] = mock.MagicMock(v1=comps)
        sys.modules["streamlit.components.v1"] = comps

        import pipeline
        pipeline.STATE_DB = temp_db
        import app  # noqa: E402  (after stubs and STATE_DB override)
        app.STATE_DB = temp_db

        import crm
        calls = {"create": [], "patch": []}

        def fake_get_account(account_id):
            return {"account_id": account_id, "note": ""}

        def fake_patch(account_id, fields):
            calls["patch"].append((account_id, dict(fields)))
            return {"account_id": account_id, **fields}

        def fake_create(body):
            calls["create"].append(dict(body))
            return {"account_id": "NEW1", **body}

        conn = sqlite3.connect(temp_db)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM proposals WHERE status='pending' ORDER BY type, key").fetchall()
        conn.close()
        if not rows:  # every proposal decided: nothing left to dry-run
            print("apply_proposal dry run: no pending rows, skipped")
            return

        # Billing changed after proposing: a re-parent must refuse to write (SOP re-checked at approve time).
        stale = next(r for r in rows if r["type"] == "reparent")
        billed = lambda i: {"account_id": i, "note": "", "lifetime_revenue": 5, "outstanding_ar": 5}
        with mock.patch.object(crm, "get_account", billed), mock.patch.object(crm, "patch_account", fake_patch):
            try:
                app.apply_proposal(stale)
                raise AssertionError("stale billing should block the re-parent")
            except RuntimeError:
                pass
        assert not calls["patch"], "re-parent wrote despite revenue and AR"
        print("stale-billing re-check OK")

        seen = Counter()
        with mock.patch.object(crm, "get_account", fake_get_account), \
             mock.patch.object(crm, "patch_account", fake_patch), \
             mock.patch.object(crm, "create_account", fake_create), \
             mock.patch.object(crm, "get_all_accounts", lambda: []):
            for row in rows:
                calls["create"].clear()
                calls["patch"].clear()
                t = row["type"]
                targets = json.loads(row["target_ids"])
                changes = json.loads(row["changes"])
                app.apply_proposal(row)
                seen[t] += 1
                creates, patches = list(calls["create"]), list(calls["patch"])

                if t in ("create", "chow"):
                    new_acct = changes.get("new_account") or changes["create_new_account"]
                    assert len(creates) == 1, f"{row['key']}: expected 1 create, got {len(creates)}"
                    assert creates[0]["parent_id"] == BH, f"{row['key']}: create not under Bellhaven"
                    assert creates[0]["name"] == new_acct["name"], f"{row['key']}: create name mismatch"

                if t == "create":
                    assert not patches, f"{row['key']}: create row should not patch anything, got {patches}"

                if t == "chow":
                    assert len(patches) == 1, f"{row['key']}: chow expects 1 patch, got {len(patches)}"
                    acct_id, fields = patches[0]
                    assert acct_id == targets[0], f"{row['key']}: chow patched wrong account"
                    assert set(fields) == {"chow_current_account", "note"}, f"{row['key']}: chow patch fields {set(fields)}"
                    assert fields["chow_current_account"] == "NEW1", f"{row['key']}: chow pointer wrong"
                    assert "parent_id" not in fields, f"{row['key']}: chow must never move the old account"
                    assert fields["note"], f"{row['key']}: chow note empty"

                if t == "chow_existing":
                    assert len(creates) == 0 and len(patches) == 1, f"{row['key']}: chow_existing shape wrong"
                    assert patches[0][1]["chow_current_account"] == changes["chow_current_account"]
                    assert "parent_id" not in patches[0][1]

                if t == "reparent":
                    assert not creates, f"{row['key']}: reparent should not create"
                    assert len(patches) == 1 and patches[0][0] == targets[0], f"{row['key']}: reparent patch target wrong"
                    assert patches[0][1]["parent_id"] == BH, f"{row['key']}: reparent did not set parent_id"

                if t == "mark_duplicate":
                    assert not creates and len(patches) == 1, f"{row['key']}: mark_duplicate shape wrong"
                    f = patches[0][1]
                    assert f["duplicate_of_account"] == changes["duplicate_of_account"], f"{row['key']}: dup pointer wrong"
                    assert f["status"] == "Inactive", f"{row['key']}: dup not Inactive"

                if t in ("update_name", "update_address", "orphan_review"):
                    assert not creates and len(patches) == 1 and patches[0][0] == targets[0], f"{row['key']}: {t} shape wrong"

        assert sum(seen.values()) == len(rows)
        for t in sorted(seen):
            print(f"apply_proposal dry run: {t}: {seen[t]} rows OK")
        print(f"apply_proposal dry run: {len(rows)} pending rows total, no network, CRM mocked")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    assert _sha(real_db) == real_hash_before, "real state.db was modified"
    print("state.db unchanged")


def rules_labels():
    return ["create", "chow", "chow_existing", "reparent", "update_address", "update_name", "mark_duplicate", "orphan_review"]


if __name__ == "__main__":
    test_plan_reparent()
    test_plan_orphan()
    test_apply_proposal_dry_run()
    print("ALL PASSED")


# PO box billing street is kept; zip fixes still apply.
import match
_w = {"billing_street": "3156 W Prospect Rd", "billing_city": "Ashtabula", "billing_state": "OH", "billing_zip": "44004"}
assert match.address_changes(_w, {**_w, "billing_street": "PO Box 517"}) == {}
assert match.address_changes(_w, {**_w, "billing_street": "P.O. Box 517", "billing_zip": "44005"}) == {"billing_zip": "44004"}
assert match.address_changes(_w, {**_w, "billing_street": "10 Main St"}) == {"billing_street": "3156 W Prospect Rd"}
print("PO box rule OK")


# After a CHOW is approved, the next run must not mark the linked old account as a duplicate.
import json as _json, contextlib as _cl, io as _io
from crm import BELLHAVEN_PARENT_ID as _B
_acc = _json.load(open("data/accounts.json")); _sites = _json.load(open("data/communities.json"))
_old = next(a for a in _acc if a["account_id"] == "001A34WFSUYHCRBLFT"); _old["chow_current_account"] = "NEWX"
_acc.append({**_old, "account_id": "NEWX", "parent_id": _B, "lifetime_revenue": 0, "outstanding_ar": 0, "chow_current_account": ""})
with _cl.redirect_stdout(_io.StringIO()):
    _ps = match.build_all(_acc, _sites)
assert not [p for p in _ps if "001A34WFSUYHCRBLFT" in p["target_ids"]], "linked CHOW account was re-proposed"
print("CHOW re-run rule OK")
