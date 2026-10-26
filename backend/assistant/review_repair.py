"""Bound a proposal correction to its content, not unrelated desktop work.

The caller passes only tool receipts produced in the current user-initiated
run. This narrows existing capabilities; it never grants a tool or approval.
"""
OUT_OF_SCOPE = "REVISION_REVIEW_OUT_OF_SCOPE"
UNAVAILABLE = "REVISION_REVIEW_UNAVAILABLE"
REPAIR_ONLY = "REVISION_REPAIR_ONLY"


def repair_mode(parts):
    failures = 0
    for part in parts:
        data = part if isinstance(part, dict) else vars(part)
        metadata = data.get("metadata") or {}
        code = metadata.get("failure_code")
        if code == REPAIR_ONLY:
            return "reply"
        if data.get("tool") != "question":
            continue
        if code == UNAVAILABLE:
            return "reply"
        if code == OUT_OF_SCOPE:
            failures += 1
        elif data.get("status") == "completed":
            failures = 0
    return "reply" if failures >= 3 else "correct" if failures else None


def permits_tool(mode, canonical_id):
    return mode is None or mode == "correct" and canonical_id == "question"


def instruction(mode):
    if mode == "correct":
        return ("The revised proposal failed its content check. Correct the supplied mismatches "
            "using the exact baseline and current evidence. If an essential attachment or fact is "
            "missing, call question with purpose=clarification to collect it; for files set "
            "allow_attachments=true. Do not invent a proposal before the missing evidence arrives. "
            "Otherwise call question with the complete corrected proposal. "
            "Only question remains available for this bounded correction. "
            "Do not inspect platform files or run diagnostic commands. If the correction is not "
            "possible, explain that the draft could not be checked and stop.")
    return ("The revised proposal check is unavailable or its bounded corrections are exhausted. "
        "Stop tool use and briefly explain the blocker. Do not claim a new review card, approval "
        "or completed production. The prior proposal and user's edits remain saved; do not ask "
        "the user to upload the same attachment again.")
