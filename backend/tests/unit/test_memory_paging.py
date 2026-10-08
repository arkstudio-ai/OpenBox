"""Every memory stays reachable: newest first, a page at a time, and findable by its words."""
import pytest

from memory import service
from tests.unit.test_memory_authority_v2 import authority_scope, identity  # noqa: F401


@pytest.mark.parametrize("authority_scope", ["sqlite", "postgres"], indirect=True)
async def test_old_memories_page_in_and_are_found_by_their_words(authority_scope):
    scope = authority_scope
    for index in range(105):
        await service.create_note(**identity(scope), summary=f"备忘{index:03d}：第{index}次团购的取件码")
    await service.create_note(**identity(scope), summary="折扣 50%_off 的说明")
    page = dict(**identity(scope), status="ACTIVE", limit=100, newest_first=True)
    first, after = await service.page_memories(**page)
    second, end = await service.page_memories(**page, offset=after)
    assert len(first) == 100 and after == 100 and len(second) == 6 and end is None
    assert first[0]["summary"] == "折扣 50%_off 的说明" and second[-1]["summary"].startswith("备忘000")
    found, _ = await service.page_memories(**page, query="备忘000")
    assert [item["summary"] for item in found] == ["备忘000：第0次团购的取件码"]
    # Wildcards in a search are just characters.
    literal, _ = await service.page_memories(**page, query="50%_")
    assert [item["summary"] for item in literal] == ["折扣 50%_off 的说明"]
    assert (await service.page_memories(**page, query="%"))[0] == literal
