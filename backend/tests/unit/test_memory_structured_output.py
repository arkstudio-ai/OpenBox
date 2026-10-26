import json

import httpx
import pytest

from memory.extraction import ConfiguredMemoryExtractor, ExtractionProviderError, ExtractionSchemaError, validate_proposals
from memory.jobs import ExtractionInput
from memory.providers.structured_output import extraction_schema, response_format


def frozen():
    return ExtractionInput("test", "user", "workspace", None, "session", "turn", "hash",
        ({"source_kind": "user_statement", "body": "我平时喜欢画水彩。"},), (), (), "acl")


def proposal(**extra):
    return {"type": "PREFERENCE", "summary": "用户平时喜欢画水彩。", "fact_key": "personal.hobby.watercolor",
            "confidence": 95, "source_indexes": [0],
            "quotes": [{"source_index": 0, "quote": "我平时喜欢画水彩。"}], **extra}


def test_explicit_absence_of_optional_experience_is_not_an_invalid_procedure():
    result = validate_proposals({"candidates": [proposal(experience=None)]}, frozen())
    assert len(result) == 1 and "experience" not in result[0]


@pytest.mark.parametrize("irrelevant", [None, {}, {"steps": []}, {"title": "Not a procedure"}])
def test_setting_proof_never_validates_or_persists_extraneous_experience(irrelevant):
    source = "我们以后用英语聊吧，你说的短一些就好。"
    value = ExtractionInput("test", "user", "workspace", None, "session", "turn", "hash",
        ({"source_kind": "native_voice_utterance", "body": source},), (), (), "acl")
    diagnostics = {}
    result = validate_proposals({"candidates": [proposal(type="FEEDBACK", summary="用户希望用英语交流。",
        fact_key="personal.style.language", setting="英语", experience=irrelevant,
        quotes=[{"source_index": 0, "quote": source}])]}, value, diagnostics=diagnostics)
    assert len(result) == 1 and "experience" not in result[0] and result[0]["setting"] == "英语"
    assert diagnostics.get("discarded_setting_experiences", 0) == int(irrelevant is not None)


def test_invalid_non_setting_experience_still_fails_closed():
    with pytest.raises(ExtractionSchemaError, match="invalid_experience"):
        validate_proposals({"candidates": [proposal(experience={"steps": []})]}, frozen())


def test_experience_wire_shape_matches_storage_bounds_and_allows_absence():
    import jsonschema
    schema = extraction_schema()
    jsonschema.validate({"candidates": [proposal(experience=None)]}, schema)
    procedure = {"title": "说明", "conditions": "收到资料", "steps": ["检查资料"],
                 "expected_result": "资料完整", "failure_boundaries": "缺少资料时停止", "test_plan": "核对资料"}
    jsonschema.validate({"candidates": [proposal(experience=procedure)]}, schema)
    for field in ("title", "conditions", "expected_result", "failure_boundaries", "test_plan"):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({"candidates": [proposal(experience={**procedure, field: ""})]}, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"candidates": [proposal(experience={**procedure, "steps": [""]})]}, schema)


@pytest.mark.parametrize("root,model,expected", [
    ("https://dashscope.aliyuncs.com/compatible-mode/v1", "openai/qwen3.8-flash", "json_schema"),
    ("https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "openai/qwen3.8-flash", "json_object"),
    ("https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1", "qwen3.8-flash-2026-10-20", "json_schema"),
    ("https://proxy.invalid/v1", "openai/qwen3.8-flash", "json_object"),
    ("https://dashscope.aliyuncs.com.evil.invalid/v1", "openai/qwen3.8-flash", "json_object"),
    ("https://dashscope.aliyuncs.com/compatible-mode/v1", "openai/qwen-old", "json_object"),
])
def test_route_capability_is_explicit_and_does_not_change_unknown_gateways(root, model, expected):
    assert response_format(model, root, name="memory", schema=extraction_schema())["type"] == expected


async def test_strict_provider_request_keeps_local_validation_and_source_only_contract():
    calls = []
    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        contract = payload["response_format"]
        assert contract["type"] == "json_schema" and contract["json_schema"]["strict"]
        assert contract["json_schema"]["schema"]["required"] == ["candidates"]
        assert json.loads(payload["messages"][-1]["content"])["sources"][0]["text"] == frozen().sources[0]["body"]
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
            "message": {"content": '{"candidates":[]}'}}], "usage": {"prompt_tokens": 12, "completion_tokens": 5}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await ConfiguredMemoryExtractor(model="openai/qwen3.8-flash",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1", api_key="fixture", client=client)(frozen())
    assert result.proposals == [] and len(calls) == 1
    assert result.usage["output_contract"] == "json_schema"
    assert result.usage["output_tokens"] == 5


@pytest.mark.parametrize("body,error", [
    ({}, "invalid_envelope"),
    ({"candidates": [{"type": "PREFERENCE", "summary": "用户喜欢游泳。", "fact_key": "personal.hobby",
                      "confidence": 99, "source_indexes": [0],
                      "quotes": [{"source_index": 0, "quote": "我喜欢游泳"}]}]}, "unsupported_quote"),
])
async def test_strict_mode_does_not_trust_invalid_or_fabricated_provider_output(body, error):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(body)}}]}))) as client:
        with pytest.raises(ExtractionSchemaError, match=error):
            await ConfiguredMemoryExtractor(model="openai/qwen3.8-flash",
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1", api_key="fixture", client=client)(frozen())


async def test_rejected_schema_does_not_trigger_retry_or_silent_fallback():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(400, json={"error": "unsupported schema"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ExtractionProviderError, match="provider_http_400"):
            await ConfiguredMemoryExtractor(model="openai/qwen3.8-flash",
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1", api_key="fixture", client=client)(frozen())
    assert len(calls) == 1
