"""Small read/write client for the Bellhaven CRM API.

Step 1 only uses the read functions. Write functions come later and are only
called from the review app after an approval.
"""
import os

import requests

BASE_URL = "https://analyst-assessment-production.up.railway.app/api/v1"
PAGE_SIZE = 100  # shortcut: assumes the API caps page_size at 100, check the docs if pulls come back short
BELLHAVEN_PARENT_ID = "0015QAPLGS3FVYEEEM"
ACCOUNTS_FILE = "data/accounts.json"


def _headers():
    token = os.environ.get("BH_TOKEN")
    if not token:
        raise RuntimeError("BH_TOKEN is not set. Set it in your shell; never put it in a file.")
    if not (token.isascii() and token.startswith("bh_")):
        raise RuntimeError("BH_TOKEN looks like a placeholder, not your bh_ token. Set the real token and restart the app.")
    return {"Authorization": f"Bearer {token}"}


def get_me():
    r = requests.get(f"{BASE_URL}/me", headers=_headers(), timeout=30)
    r.raise_for_status()
    return r.json()


def get_account(account_id):
    r = requests.get(f"{BASE_URL}/accounts/{account_id}", headers=_headers(), timeout=30)
    r.raise_for_status()
    return r.json()


# Write functions below. Only app.py calls these, and only after an Approve click.
def patch_account(account_id, fields):
    r = requests.patch(f"{BASE_URL}/accounts/{account_id}", headers=_headers(), json=fields, timeout=30)
    r.raise_for_status()
    return r.json()


def create_account(fields):
    # Request body is a guess until the first real create is approved (see OpenAPI: no body schema).
    r = requests.post(f"{BASE_URL}/accounts", headers=_headers(), json=fields, timeout=30)
    r.raise_for_status()
    return r.json()


def get_all_accounts():
    """Walk every page of GET /accounts and return one flat list."""
    accounts = []
    page = 1
    while True:
        r = requests.get(
            f"{BASE_URL}/accounts",
            headers=_headers(),
            params={"page": page, "page_size": PAGE_SIZE},
            timeout=30,
        )
        r.raise_for_status()
        body = r.json()
        accounts.extend(body["data"])
        if not body["data"] or len(accounts) >= body["total"]:
            break
        page += 1
    return accounts


if __name__ == "__main__":
    import json

    accounts = get_all_accounts()
    os.makedirs(os.path.dirname(ACCOUNTS_FILE), exist_ok=True)
    with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
        json.dump(accounts, f, indent=2)

    under_bellhaven = [a for a in accounts if a.get("parent_id") == BELLHAVEN_PARENT_ID]
    print(f"Total accounts pulled: {len(accounts)}")
    print(f"Under Bellhaven parent: {len(under_bellhaven)}")
    for a in under_bellhaven:
        print(f"  {a['account_id']}  {a['name']}  |  {a.get('billing_city')}, {a.get('billing_state')}  |  {a.get('status')}")
