"""Use strict output on verified Bailian routes; retain other providers' contract.

JSON mode alone guarantees syntax, not our envelope or field names. Unsupported
routes are never probed/retried or silently changed to another model. Local
schema, source/quote and independent grounding checks remain authoritative.
"""
from urllib.parse import urlsplit


def response_format(model, base_url, *, name, schema):
    host = (urlsplit(base_url).hostname or "").lower()
    official = (host == "dashscope.aliyuncs.com" or host.endswith(".dashscope.aliyuncs.com")
                or host.endswith(".maas.aliyuncs.com"))
    bare = model.rsplit("/", 1)[-1].lower()
    supported = any(bare == prefix or bare.startswith(prefix + "-") for prefix in (
        "qwen3.8-flash", "qwen3.8-max", "qwen3.7-flash", "qwen3.7-plus", "qwen3.7-max"))
    if official and supported:
        return {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}}
    return {"type": "json_object"}


def extraction_schema():
    # Same envelope and optional fields accepted by validate_proposals. This
    # constrains representation only, never eligibility for persistent memory.
    from memory.extraction import ALLOWED_TYPES, MAX_CANDIDATES, MAX_SUMMARY_CHARS
    quote = {"type": "object", "additionalProperties": False,
        "required": ["source_index", "quote"], "properties": {
            "source_index": {"type": "integer", "minimum": 0},
            "quote": {"type": "string", "minLength": 3, "maxLength": 2400}}}
    experience_fields = {key: {"type": "string", "minLength": 1, "maxLength": 1000} for key in (
        "title", "conditions", "expected_result", "failure_boundaries", "test_plan")}
    experience_fields["title"]["maxLength"] = 120
    experience_fields["steps"] = {"type": "array", "minItems": 1, "maxItems": 12,
                                  "items": {"type": "string", "minLength": 1, "maxLength": 1000}}
    candidate = {"type": "object", "additionalProperties": False,
        "required": ["type", "summary", "fact_key", "confidence", "source_indexes", "quotes"],
        "properties": {
            "type": {"type": "string", "enum": sorted(ALLOWED_TYPES)},
            "summary": {"type": "string", "minLength": 1, "maxLength": MAX_SUMMARY_CHARS},
            "fact_key": {"type": ["string", "null"], "maxLength": 120},
            "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
            "source_indexes": {"type": "array", "minItems": 1, "maxItems": 8,
                               "items": {"type": "integer", "minimum": 0}},
            "quotes": {"type": "array", "minItems": 1, "maxItems": 16, "items": quote},
            "valid_until": {"type": ["string", "null"]},
            "setting": {"type": ["string", "null"]},
            "experience": {"type": ["object", "null"], "additionalProperties": False,
                           "required": list(experience_fields), "properties": experience_fields}}}
    return {"type": "object", "additionalProperties": False, "required": ["candidates"],
        "properties": {"candidates": {"type": "array", "maxItems": MAX_CANDIDATES, "items": candidate}}}
