"""Deployment-scoped Redis channels; Redis DB numbers do not scope Pub/Sub."""
import os
import re


def event_channel(name: str) -> str:
    """Keep legacy deployments compatible unless an explicit scope is set.

    All workers of a deployment must use the same namespace. Changing it
    requires a coordinated restart; listening on both scopes would defeat
    isolation. Redis ACLs / dedicated instances remain the security boundary.
    """
    namespace = os.environ.get("EVENT_BUS_NAMESPACE", "")
    if not namespace:
        return name
    if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", namespace) is None:
        raise ValueError("EVENT_BUS_NAMESPACE must contain 1-64 letters, digits, '.', '_' or '-'")
    return f"{namespace}:{name}"
