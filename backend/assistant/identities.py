"""Domain-separated canonical identities accepted by the 64-character Inbox key."""
from hashlib import sha256
import json


def inbox_key(domain: str, *identities: str | int) -> str:
    return sha256(json.dumps([domain, *identities], ensure_ascii=True,
                             separators=(",", ":")).encode()).hexdigest()
