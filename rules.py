"""Business rules: thresholds, care mapping, and the SOP branches.

Change a number or mapping here and the whole pipeline follows.
"""
NAME_MATCH_MIN = 85  # rapidfuzz score (0-100) a name needs to count as the same facility
NEAR_NAME_MIN = 85  # an unmatched account with a name this close (any city) gets flagged before a create
CARE_MAP = {  # website care offering -> CRM care_type
    "Short-Term Rehabilitation & Nursing": "Skilled Nursing",
    "Memory Support": "Memory Care",
    "Assisted Living": "Assisted Living",
}


def _num(value):
    """Money fields can be blank or missing. Treat those as 0."""
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def has_billing_history(account):
    return _num(account.get("lifetime_revenue")) > 0 or _num(account.get("outstanding_ar")) > 0


def plan_reparent(account, correct_parent_id):
    """SOP: an account needs to move to a different parent.

    CHOW case (revenue > 0 AND unpaid AR > 0): do NOT move the old account.
    The new owner gets a new account, and the old one points to it.
    Otherwise: move the existing account directly.
    """
    if _num(account.get("lifetime_revenue")) > 0 and _num(account.get("outstanding_ar")) > 0:
        return {"action": "chow", "old_account_id": account["account_id"]}
    return {"action": "reparent", "account_id": account["account_id"], "parent_id": correct_parent_id}


def plan_orphan(account, successor):
    """A Bellhaven account the website no longer lists.

    If the facility was sold, the new owner's account often already exists at the
    same address. With money on the books, point the old account at it (no third
    copy). Otherwise flag it for a human.
    """
    if successor and _num(account.get("lifetime_revenue")) > 0 and _num(account.get("outstanding_ar")) > 0:
        return {"action": "chow_existing", "old_account_id": account["account_id"], "successor_id": successor["account_id"]}
    return {"action": "orphan_review", "account_id": account["account_id"]}
