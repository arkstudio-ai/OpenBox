"""Credential removal applies before hashes, blobs, event logs and previews."""
import re
import json
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_PRIVATE = {
    "authorization", "proxyauthorization", "cookie", "setcookie", "password", "passwd",
    "apikey", "accesskey", "accesskeyid", "accesskeysecret", "secret", "clientsecret",
    "accesstoken", "refreshtoken", "idtoken", "securitytoken", "sessiontoken", "token",
    "signature", "signedurl", "presignedurl", "connectionticket", "ticket",
    "encryptedcontent", "encryptedreasoning", "reasoningsignature", "providerprivate",
    "providerbinding", "capabilitykey", "responsechainid", "privatekey",
    "canonicaltoolid", "wiretoolname", "providerbindingdigest", "providerdialect", "streamseq",
    "providermetadata", "providerspecificfields", "providerreplay", "provideraccountid",
    "credential", "credentials",
}
_BEARER = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9_./+=-]+")
_TOKEN = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{12,}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})\b")
_ASSIGNMENT = re.compile(r'''(?ix)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|passwd|secret|authorization|cookie|signature|token)(?:\\?["'])?\s*[=:]\s*)(?:\\?["'])?''')
_URL = re.compile(r"https?://[^\s<>\"']+")


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _url(match) -> str:
    value = match.group(0)
    try:
        parsed = urlsplit(value)
        query = [(key, "[REDACTED]" if _key(key) in _PRIVATE or _key(key).startswith(("xamz", "xoss", "xgoog")) or _key(key) in {"sig", "se", "sp", "sv"} else val) for key, val in parse_qsl(parsed.query, keep_blank_values=True)]
        host = parsed.netloc.split("@")[-1]
        return urlunsplit((parsed.scheme, host, parsed.path, urlencode(query), parsed.fragment))
    except ValueError:
        return "[REDACTED URL]"


def _assignments(value: str) -> str:
    """Handle incomplete/escaped argument JSON without needing a valid parse."""
    matches = list(_ASSIGNMENT.finditer(value))
    for match in reversed(matches):
        end = match.end()
        # A quoted fragment may not have received its closing quote yet.
        quoted = value[end - 1:end] in {'"', "'"}
        quote = value[end - 1:end]
        opening_slashes = 0
        position = end - 2
        while position >= 0 and value[position] == "\\":
            opening_slashes += 1
            position -= 1
        while end < len(value):
            if quoted and value[end] == quote:
                closing_slashes = 0
                position = end - 1
                while position >= 0 and value[position] == "\\":
                    closing_slashes += 1
                    position -= 1
                if closing_slashes == opening_slashes:
                    break
            if not quoted and value[end] in ',;}]\n\r& ':
                break
            end += 1
        value = value[:match.end()] + "[REDACTED]" + value[end:]
    return value


def _schema_node(value) -> bool:
    return isinstance(value, dict) and ((isinstance(value.get("type"), str) and value["type"] in {"object", "array", "string", "number", "integer", "boolean", "null"})
        or isinstance(value.get("type"), list)
        or any(key in value for key in ("properties", "$ref", "oneOf", "anyOf", "allOf")))


def sanitize(value, *, _removed: set | None = None, _schema=False, _sensitive_property=False):
    removed = _removed if _removed is not None else set()
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            normalized = _key(str(key))
            if _schema and normalized in _PRIVATE and _schema_node(child):
                result[str(key)] = sanitize(child, _removed=removed, _schema=True, _sensitive_property=True)
                continue
            if _schema and _sensitive_property and normalized in {"default", "example", "examples", "const", "enum"}:
                result[str(key)] = "[REDACTED]"
                continue
            if normalized in _PRIVATE or str(key).startswith("__private"):
                removed.add(str(key))
                result[str(key)] = "[REDACTED]"
            else:
                schema = _schema or normalized in {"schema", "parameters", "inputschema", "outputschema", "jsonschema"} and _schema_node(child)
                result[str(key)] = sanitize(child, _removed=removed, _schema=schema, _sensitive_property=_sensitive_property)
        return result
    if isinstance(value, list):
        return [sanitize(child, _removed=removed, _schema=_schema, _sensitive_property=_sensitive_property) for child in value]
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("{", "[", '"')):
            try:
                decoded = json.loads(value)
            except (ValueError, RecursionError):
                pass
            else:
                return json.dumps(sanitize(decoded, _removed=removed, _schema=_schema), ensure_ascii=False)
        value = _URL.sub(_url, value)
        value = _BEARER.sub(lambda m: f"{m[1]} [REDACTED]", value)
        value = _TOKEN.sub("[REDACTED]", value)
        return _assignments(value)
    return value
