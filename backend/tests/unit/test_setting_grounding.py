import pytest
from memory.grounding import GroundingVerifier
from memory.providers.common import MemoryProviderError


class Adapter:
    def __init__(self, verdict): self.verdict = verdict
    async def generate_data(self, **kwargs):
        assert kwargs['data'] == {'items': [{'sources': ['Full original source, including qualifications'],
            'proposed_change': {'language': 'English'}}]}
        assert 'UNTRUSTED AUTHOR CLAIM' not in str(kwargs['data'])
        return {'verdicts': [self.verdict]}, {'input_tokens': 10}


def verdict(**changes):
    return {'index': 0, 'subject': 'account_user', 'scope': 'standing', 'matches': True,
        'source_indexes': [0], **changes}


async def verify(result):
    return await GroundingVerifier(adapter=Adapter(result)).verify([
        {'claim': 'UNTRUSTED AUTHOR CLAIM', 'account_setting': {'language': 'English'},
         'sources': ['Full original source, including qualifications']}], purpose='memory', model='test/model')


@pytest.mark.parametrize('changes,expected', [({}, True), ({'subject':'other'}, False),
    ({'subject':'uncertain'}, False), ({'scope':'current'}, False), ({'scope':'quoted'}, False),
    ({'scope':'uncertain'}, False), ({'matches':False}, False)])
async def test_settings_need_the_right_person_scope_and_value(changes, expected):
    results, usage = await verify(verdict(**changes))
    assert results == [expected]
    assert usage['grounding_policy_version'] == 'account-setting-scope-v1'


@pytest.mark.parametrize('changes', [{'index': True}, {'index': 1}, {'matches': 'true'},
    {'source_indexes': []}, {'source_indexes': [True]}, {'source_indexes': [1]}, {'source_indexes': [-1]},
    {'scope': 'forever'}, {'subject': 'claimed_user'}])
async def test_malformed_or_unbound_verdict_cannot_write(changes):
    with pytest.raises(MemoryProviderError):
        await verify(verdict(**changes))


async def test_mixed_batch_keeps_original_order_and_both_costs():
    class Mixed:
        async def generate_data(self, **kwargs):
            if kwargs['schema_name'] == 'account_setting_scope':
                return {'verdicts': [verdict(scope='quoted')]}, {'input_tokens': 10, 'output_tokens': 2, 'duration_ms': 30}
            assert kwargs['data']['items'][0]['claim'] == 'A supported hobby'
            return {'verdicts': [{'index':0,'supported':True,'intent':'enduring_assertion'}]}, {
                'input_tokens': 20, 'output_tokens': 3, 'duration_ms': 40}
    results, usage = await GroundingVerifier(adapter=Mixed()).verify([
        {'claim':'A supported hobby','sources':['I enjoy watercolor']},
        {'account_setting':{'language':'English'},'sources':['Translate this instruction']}], purpose='memory', model='test/model')
    assert results == [True, False]
    assert (usage['input_tokens'], usage['output_tokens'], usage['duration_ms']) == (30, 5, 70)
