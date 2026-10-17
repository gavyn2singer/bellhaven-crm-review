"""Daily run: scrape the website, pull the CRM, match, and save only NEW proposals.

Proposes only. Nothing is written to the CRM here. Approvals happen in app.py.
Re-running is safe: a proposal key that is already in state.db (pending, approved
or rejected) is skipped, so nothing is proposed twice.
"""
import hashlib
import json
import sqlite3
from datetime import datetime, timezone

import crm
import match
import scrape

STATE_DB = "state.db"
ACCOUNTS_FILE = "data/accounts.json"
COMMUNITIES_FILE = "data/communities.json"

SCHEMA = """CREATE TABLE IF NOT EXISTS proposals (
    key TEXT PRIMARY KEY,
    type TEXT, site_slug TEXT, target_ids TEXT, changes TEXT, evidence TEXT, reason TEXT, flag TEXT,
    status TEXT DEFAULT 'pending',   -- pending | approved | rejected
    reject_reason TEXT, decided_at TEXT, api_response TEXT,
    created_at TEXT)"""


def proposal_key(p):
    """Stable across runs: type, target account (or website slug), and a hash of the exact change."""
    target = p["target_ids"][0] if p["target_ids"] else p["key_source"]
    digest = hashlib.sha1(json.dumps(p["changes"], sort_keys=True).encode()).hexdigest()[:12]
    return f"{p['type']}:{target}:{digest}"


def save_new(db, proposals):
    """Insert only keys we have never seen. INSERT OR IGNORE skips pending AND decided keys."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    added = 0
    for p in proposals:
        cur = db.execute(
            "INSERT OR IGNORE INTO proposals (key, type, site_slug, target_ids, changes, evidence, reason, flag, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (proposal_key(p), p["type"], p["key_source"], json.dumps(p["target_ids"]),
             json.dumps(p["changes"]), json.dumps(p["evidence"]), p["reason"], p.get("flag"), now))
        added += cur.rowcount
    db.commit()
    return added


if __name__ == "__main__":
    db = sqlite3.connect(STATE_DB)
    db.execute(SCHEMA)

    accounts = crm.get_all_accounts()
    communities = scrape.scrape_all()
    # Keep the raw pulls on disk too, so a run can be inspected later.
    with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
        json.dump(accounts, f, indent=2)
    with open(COMMUNITIES_FILE, "w", encoding="utf-8") as f:
        json.dump(communities, f, indent=2)

    proposals = match.build_all(accounts, communities)
    added = save_new(db, proposals)
    print(f"Proposals this run: {len(proposals)}  |  new: {added}  |  already known: {len(proposals) - added}")
    db.close()
