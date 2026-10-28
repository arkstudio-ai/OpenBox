"""The production provider duplicated followup text under the submit field name."""
import pytest
from pydantic import ValidationError

from tool.assistant_tools import FollowupArgs


def payload(**extra):
    return {"task_id": "fruit", "text": "按小丫水果店方案开始制作。", "expected_revision": 6,
            "source_message_ids": ["human-start"], **extra}


def test_identical_submit_field_is_not_a_failed_followup():
    original = payload()
    assert FollowupArgs.model_validate({**original, "instructions": original["text"]}).model_dump() == (
        FollowupArgs.model_validate(original).model_dump())


@pytest.mark.parametrize("extra", [{"instructions": "改成别的任务"}, {"unexpected": "anything"}])
def test_conflicting_or_unknown_fields_still_fail(extra):
    with pytest.raises(ValidationError):
        FollowupArgs.model_validate(payload(**extra))
