"""Every committed prefix must be safe; future chunks cannot retract history."""
from copy import deepcopy
import json

import pytest

from trajectory.stream_redaction import CaptureStreamRedactor, StreamTextRedactor


def fragments(text, widths):
    start = 0
    for width in widths:
        yield text[start:start + width]
        start += width
    if start < len(text):
        yield text[start:]


def capture(parts, *, block_id='tool:0', kind='tool_arguments'):
    redactor = CaptureStreamRedactor()
    events = []
    for index, part in enumerate(parts, 1):
        raw = {'choices':[{'index':0,'delta':{'tool_calls':[{'index':0,'function':{
            'name':'example', 'arguments':part}}], 'content':part, 'reasoning_content':part}}],
            'type':'fixture.delta','delta':part}
        event = {'chunk_index':index,'mode':'delta','blocks':[
            {'type':kind,'block_id':block_id,'delta':part}], 'raw':raw}
        original = deepcopy(event)
        events.append(redactor.redact(event))
        assert event == original
    final = redactor.finalize()
    if final:
        events.append(final)
    return events


def output(events):
    result = {}
    for event in events:
        for block in event['blocks']:
            identity = block['block_id']
            if block.get('mode',event.get('mode')) == 'replace':
                result[identity] = block['delta']
            else:
                result[identity] = result.get(identity,'') + block['delta']
    return result


def test_split_sensitive_key_hides_value_in_every_raw_and_block_copy():
    events = capture(['{"api_', 'key":"', 'fixture-sensitive-fragment', '"}'])
    assert 'fixture-sensitive-fragment' not in json.dumps(events)
    assert json.loads(output(events)['tool:0']) == {'api_key':'[REDACTED]'}
    assert [event['chunk_index'] for event in events] == [1,2,3,4]
    assert events[2]['raw']['choices'][0]['delta']['tool_calls'][0]['function']['arguments'] == {
        '$stream_blocks':['tool:0'],'availability':'sanitized_reference'}


@pytest.mark.parametrize('source,secret',[
    ('{"password":"FIXTURE_SENSITIVE_A"}', 'FIXTURE_SENSITIVE_A'),
    ('{"api key":"FIXTURE_SPACED_KEY"}', 'FIXTURE_SPACED_KEY'),
    ('{"a.p.i/k e y":"FIXTURE_PUNCTUATED_KEY"}', 'FIXTURE_PUNCTUATED_KEY'),
    ('{"access-key-secret":"FIXTURE_SENSITIVE_B"}', 'FIXTURE_SENSITIVE_B'),
    ('{"credentials":{"nested":["FIXTURE_SENSITIVE_C",{"safe":"nested value"}]},"n":1}', 'FIXTURE_SENSITIVE_C'),
    ('{\\"api_key\\":\\"FIXTURE_SENSITIVE_D\\"}', 'FIXTURE_SENSITIVE_D'),
    ('{"api\\u005fkey":"FIXTURE_SENSITIVE_E"}', 'FIXTURE_SENSITIVE_E'),
    ('{"\\u0070assword":"FIXTURE_SENSITIVE_F"}', 'FIXTURE_SENSITIVE_F'),
    ('Bearer FIXTURE_SENSITIVE_G rest', 'FIXTURE_SENSITIVE_G'),
    ('Basic FIXTURE_SENSITIVE_H rest', 'FIXTURE_SENSITIVE_H'),
    ('Authorization: Bearer FIXTURE_SENSITIVE_I\nOK', 'FIXTURE_SENSITIVE_I'),
    ('Cookie: name=FIXTURE_SENSITIVE_J;next=value\nOK', 'FIXTURE_SENSITIVE_J'),
    ('https://fixture.invalid/asset?X-Amz-Signature=FIXTURE_SENSITIVE_K&name=public ', 'FIXTURE_SENSITIVE_K'),
    ('https://fixture.invalid/asset?%73ig=FIXTURE_SENSITIVE_L&name=public ', 'FIXTURE_SENSITIVE_L'),
    ('https://name:FIXTURE_SENSITIVE_M@fixture.invalid/path ', 'FIXTURE_SENSITIVE_M'),
    ('sk-FIXTURE_SENSITIVE_N ok', 'FIXTURE_SENSITIVE_N'),
    ('eyJFIXTURE_SENSITIVE_O.part.signature ok', 'FIXTURE_SENSITIVE_O'),
])
def test_all_chunk_split_positions_are_safe(source,secret):
    # Inspect the append-only events, not only the eventual replacement view.
    for split in range(1,len(source)):
        events = capture([source[:split],source[split:]], kind='text')
        serialized = json.dumps(events)
        assert secret not in serialized, (source,split,serialized)
        # Even a suffix delivered alone after sensitive context is never sent.
        location = source.index(secret)
        if location < split < location + len(secret):
            tail = source[split:location + len(secret)]
            if len(tail) >= 4:
                assert tail not in serialized, (source,split,serialized)
    events = capture(list(source),kind='text')
    assert secret not in json.dumps(events)
    assert secret not in output(events)['tool:0']


@pytest.mark.parametrize('source',[
    'A', 'Hello world 😀\n', '{"filename":"report.md","count":3}',
    '{"path":"C:\\workspace\\report.md","text":"A B"}',
    'ordinary sk and ey finish safely', 'http is a word',
    'the password is not shown', 's', 'e', 'h', 'sk', 'ey',
])
def test_non_sensitive_semantics_survive_every_single_character_chunk(source):
    events = capture(list(source),kind='text')
    assert output(events)['tool:0'] == source
    actual = [event for event in events if 'redaction_control' not in event]
    assert [event['chunk_index'] for event in actual] == list(range(1,len(source)+1))
    if events[-1].get('redaction_control'):
        assert events[-1]['observed_chunk_index'] == len(source)
        assert events[-1]['chunk_index'] == len(source)+1


def test_plain_fragments_do_not_change_without_an_ambiguous_prefix():
    parts=['A','B','{"filename":"','report.md','","n":3}']
    events=capture(parts)
    assert [event['blocks'][0]['delta'] for event in events] == parts


def test_parallel_blocks_and_requests_do_not_share_secret_state():
    left,right=CaptureStreamRedactor(),CaptureStreamRedactor()
    first=left.redact({'chunk_index':1,'blocks':[
        {'block_id':'tool:0','type':'tool_arguments','delta':'{"password":"'},
        {'block_id':'tool:1','type':'tool_arguments','delta':'{"label":"'}]})
    second=left.redact({'chunk_index':2,'blocks':[
        {'block_id':'tool:0','type':'tool_arguments','delta':'HIDDEN_ONLY"}'},
        {'block_id':'tool:1','type':'tool_arguments','delta':'public"}'}]})
    other=right.redact({'chunk_index':1,'blocks':[{'block_id':'tool:0','delta':'public'}]})
    assert output([first,second]) == {'tool:0':'{"password":"[REDACTED]"}','tool:1':'{"label":"public"}'}
    assert other['blocks'][0]['delta']=='public'
    assert 'HIDDEN_ONLY' not in json.dumps([first,second,other])


def test_replace_to_delta_transition_keeps_lexical_context():
    redactor=StreamTextRedactor()
    before=redactor.redact('X'*100_000+' password="',mode='replace')
    during=redactor.redact('VALUE_NEVER_PERSISTED')
    after=redactor.redact('"\nvisible',final=True)
    assert before['output'].endswith('password="[REDACTED]')
    assert during['output']==''
    assert after['output']=='"\nvisible'
    assert 'VALUE_NEVER_PERSISTED' not in json.dumps([before,during,after])


def test_cumulative_replace_never_emits_short_token_prefix_or_secret_tail():
    redactor=StreamTextRedactor()
    values=['safe s','safe sk','safe sk-', 'safe sk-FIXTURE_VALUE','safe sk-FIXTURE_VALUE end']
    events=[redactor.redact(value,mode='replace') for value in values]
    assert events[0]['output']==events[1]['output']=='safe '
    assert all('sk-' not in event['output'] and 'FIXTURE_VALUE' not in event['output'] for event in events)
    final=redactor.redact(values[-1],mode='replace',final=True)
    assert final['output']=='safe [REDACTED] end'


def test_quoted_secret_does_not_end_at_escaped_quote():
    source='{"password":"FIRST_PART\\"HIDDEN_TAIL","visible":"ok"}'
    events=capture(list(source))
    assert output(events)['tool:0']=='{"password":"[REDACTED]","visible":"ok"}'
    assert 'FIRST_PART' not in json.dumps(events) and 'HIDDEN_TAIL' not in json.dumps(events)


def test_incomplete_and_oversized_urls_never_emit_authority_or_signature():
    redactor=StreamTextRedactor()
    assert redactor.redact('https://user:PENDING_SECRET@host/?sig=')['output']==''
    values=[redactor.redact('x'*1000)['output'] for _ in range(40)]
    values.append(redactor.redact(' final',final=True)['output'])
    assert ''.join(values)=='[REDACTED URL] final'
    assert len(redactor._scanner.url)==0


def test_final_provider_replace_has_safe_raw_output_references():
    redactor=CaptureStreamRedactor()
    data={'chunk_index':1,'mode':'delta','blocks':[{'block_id':'out','type':'text','mode':'replace',
        'delta':'Bearer PRIVATE_BEARER_VALUE'}],
        'raw':{'type':'response.completed','response':{'status':'completed','output':[
            {'type':'message','id':'out','content':[{'type':'output_text','text':'Bearer PRIVATE_BEARER_VALUE'}]}]}}}
    event=redactor.redact(data)
    assert 'PRIVATE_BEARER_VALUE' not in json.dumps(event)
    assert event['blocks'][0]['mode']=='replace'
    assert event['raw']['response']['output'][0]['content'][0]['text']['availability']=='sanitized_reference'


def test_complete_http_response_without_blocks_retains_business_fields():
    raw={'type':'http.response','response':{'status':'completed','output':{
        'task_id':'local_task_123','url':'https://fixture.invalid/public.png',
        'revised_prompt':'A picture of a tree', 'transcript':'Hello world',
        'errors':[{'message':'A useful diagnostic'}], 'secret':'DO_NOT_RETAIN',
        'short_token':'sk-short'}}}
    result=CaptureStreamRedactor().redact({'chunk_index':1,'blocks':[],'raw':raw})
    actual=result['raw']['response']['output']
    assert actual['task_id']=='local_task_123'
    assert actual['url']=='https://fixture.invalid/public.png'
    assert actual['revised_prompt']=='A picture of a tree'
    assert actual['transcript']=='Hello world'
    assert actual['errors']==[{'message':'A useful diagnostic'}]
    assert actual['secret']==actual['short_token']=='[REDACTED]'
    assert '$stream_blocks' not in json.dumps(result)


def test_native_complete_tool_schema_preserves_definitions():
    parameters={'type':'object','properties':{
        'password':{'type':'string','description':'A credential parameter','default':'NO_DEFAULT'},
        'count':{'type':'integer','default':3}},'required':['password']}
    raw={'type':'response.completed','response':{'output':[{
        'type':'tool_search_output','tools':[{'name':'example','description':'Public tool description',
            'parameters':parameters}]}]}}
    result=CaptureStreamRedactor().redact({'chunk_index':1,'blocks':[],'raw':raw})
    tool=result['raw']['response']['output'][0]['tools'][0]
    assert tool['description']=='Public tool description'
    assert tool['parameters']['properties']['password']=={
        'type':'string','description':'A credential parameter','default':'[REDACTED]'}
    assert tool['parameters']['properties']['count']=={'type':'integer','default':3}
    assert tool['parameters']['required']==['password']


def test_unmapped_raw_delta_has_its_own_state_and_explicit_safe_tail():
    redactor=CaptureStreamRedactor()
    events=[redactor.redact({'chunk_index':index,'blocks':[],
        'raw':{'type':'response.custom.delta','item_id':'raw_only','delta':part}})
        for index,part in enumerate(['password="','PRIVATE_UNMAPPED_VALUE','" safe s'],1)]
    final=redactor.finalize()
    assert 'PRIVATE_UNMAPPED_VALUE' not in json.dumps(events)
    assert all(event['raw']['delta']['availability']=='sanitized_inline_delta' for event in events)
    assert final['raw']['sanitized_stream_tails'][0]['output']=='s'
    assert '$stream_blocks' not in json.dumps(events)


def test_unknown_nested_provider_delta_is_not_mistaken_for_complete_text():
    redactor=CaptureStreamRedactor()
    events=[redactor.redact({'chunk_index':index,'blocks':[],
        'raw':{'choices':[{'index':0,'delta':{'provider_thinking':[{'thinking':part}]}}]}})
        for index,part in enumerate(['Bearer ','HIDDEN_NESTED_VALUE',' done'],1)]
    assert 'HIDDEN_NESTED_VALUE' not in json.dumps(events)
    pieces=[event['raw']['choices'][0]['delta']['provider_thinking'][0]['thinking']['output'] for event in events]
    assert ''.join(pieces)=='Bearer [REDACTED] done'
