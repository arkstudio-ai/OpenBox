from pydantic import BaseModel, Field
from agent.llm import _parameter_schemas, _tool_parameters_schema
from tool.tool import ToolInfo


async def unused(*args):
    pass


def test_builtin_schema_reuses_generation_and_isolates_mutation(monkeypatch):
    class Params(BaseModel):
        title: str = Field(description='完整名称')
    calls = []
    original = Params.model_json_schema
    def schema(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(Params, 'model_json_schema', schema)
    tool = ToolInfo('one', 'one', Params, unused)
    first = _tool_parameters_schema(tool)
    first['properties']['title']['description'] = 'changed by a consumer'
    assert _tool_parameters_schema(tool)['properties']['title']['description'] == '完整名称'
    assert len(calls) == 1
    Params.model_rebuild(force=True)
    assert _tool_parameters_schema(tool)['required'] == ['title']
    assert len(calls) == 2


def test_dynamic_raw_and_plugin_schemas_are_never_frozen():
    class Params(BaseModel):
        item: str
    raw = {'type':'object', 'properties':{'x':{'type':'string'}}}
    tool = ToolInfo('dynamic', '', Params, unused, raw_schema=raw)
    assert _tool_parameters_schema(tool)['properties']['x']['type'] == 'string'
    raw['properties']['x']['type'] = 'integer'
    assert _tool_parameters_schema(tool)['properties']['x']['type'] == 'integer'
    tool.raw_schema = None
    tool.source = 'custom'
    before = len(_parameter_schemas)
    _tool_parameters_schema(tool)
    assert len(_parameter_schemas) == before


def test_config_changes_and_distinct_models_do_not_share_contracts():
    class Params(BaseModel):
        item: str
    tool = ToolInfo('one', '', Params, unused)
    original = _tool_parameters_schema(tool)
    Params.model_config['json_schema_extra'] = {'additionalProperties':False}
    assert _tool_parameters_schema(tool)['additionalProperties'] is False
    assert 'additionalProperties' not in original
    class Other(BaseModel):
        count: int
    tool.parameters = Other
    assert set(_tool_parameters_schema(tool)['properties']) == {'count'}


def test_callable_annotations_stay_dynamic():
    class Params(BaseModel):
        item: str
    counter = [0]
    def annotate(schema):
        counter[0] += 1
        schema['description'] = str(counter[0])
    Params.model_config['json_schema_extra'] = annotate
    tool = ToolInfo('dynamic-annotation', '', Params, unused)
    assert _tool_parameters_schema(tool)['description'] == '1'
    assert _tool_parameters_schema(tool)['description'] == '2'
