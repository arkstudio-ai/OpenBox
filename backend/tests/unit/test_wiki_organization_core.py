from dataclasses import replace
from itertools import permutations

import pytest

from wiki_compiler.contracts import SourceSnapshot
from wiki_compiler.hashing import text_hash
from wiki_compiler.organization import (
    OrganizationError, OrganizationRequest, concept_slug, normalize_name,
    reconcile_concepts, validate_concepts,
)


SOURCE = "Orion uses review gates. Project Orion is also called North Star. Review gates require human approval."
REQUEST = OrganizationRequest("m1", (SourceSnapshot("s1", 3, SOURCE, text_hash(SOURCE), "domain", 1),), (), "model")


def proposal(title="Orion", **overrides):
    return {"title": title, "aliases": [], "category": "Projects", "description": "A documented project.",
            "evidence": [{"source_id": "s1", "quote": "Orion uses review gates."}], "relations": [], **overrides}


def extracted(title, aliases=(), **overrides):
    return validate_concepts({"concepts": [proposal(title, aliases=list(aliases), **overrides)]}, REQUEST)[0]


def test_grounded_extraction_preserves_actual_source_version_and_explicit_relations():
    records = validate_concepts({"concepts": [proposal(relations=[{
        "type": "related", "target": "Review gates", "evidence": [{"source_id": "s1", "quote": "Orion uses review gates."}],
    }])]}, REQUEST)
    assert records[0]["evidence"][0]["revision"] == 3
    assert records[0]["relations"][0]["target"] == "Review gates"
    assert validate_concepts({"concepts": []}, REQUEST) == []


@pytest.mark.parametrize("change", [
    {"evidence": []}, {"evidence": [{"source_id": "s1", "quote": "invented fact"}]},
    {"evidence": [{"source_id": ["s1"], "quote": "Orion"}]},
    {"evidence": [{"source_id": "foreign", "quote": "Orion"}]},
    {"existing_id": "foreign-concept"}, {"existing_id": {}}, {"aliases": [42]},
    {"relations": [{"type": "causes", "target": "anything", "evidence": []}]},
    {"publish": True}, {"title": " "}, {"category": None},
])
def test_untrusted_extraction_cannot_invent_sources_identities_or_control_fields(change):
    with pytest.raises(OrganizationError):
        validate_concepts({"concepts": [proposal(**change)]}, REQUEST)


def test_transitive_alias_grouping_is_order_independent_and_preserves_all_members():
    items = [extracted("Orion", ["North Star"]), extracted("North Star", ["Polaris"]), extracted("Polaris")]
    for ordering in permutations(items):
        result = reconcile_concepts(list(ordering), [])
        assert len(result) == 1
        assert len(result[0]["members"]) == 3
        assert result[0]["title"] == "North Star"
        assert set(result[0]["aliases"]) == {"Orion", "Polaris"}


def test_existing_identity_and_user_title_win_over_model_order():
    existing = [{"id": "c1", "canonical_key": "orion", "title": "Project Orion", "aliases": ["North Star"]}]
    request = replace(REQUEST, existing=tuple(existing))
    items = validate_concepts({"concepts": [proposal("Polaris", existing_id="c1"), proposal("North Star")]}, request)
    result = reconcile_concepts(items, existing)
    assert len(result) == 1 and result[0]["id"] == "c1"
    assert result[0]["title"] == "Project Orion" and len(result[0]["members"]) == 2


def test_ambiguous_alias_needs_an_explicit_host_merge_not_model_overwrite():
    existing = [{"id": "a", "canonical_key": "a", "title": "Orion", "aliases": []},
                {"id": "b", "canonical_key": "b", "title": "North Star", "aliases": []}]
    request = replace(REQUEST, existing=tuple(existing))
    items = validate_concepts({"concepts": [proposal("Orion", aliases=["North Star"], existing_id="a")]}, request)
    with pytest.raises(OrganizationError, match="ambiguous_concept_identity"):
        reconcile_concepts(items, existing)


def test_unicode_identity_and_slugs_are_stable_without_ascii_title_collisions():
    assert normalize_name("  ＡＢＣ  Guide ") == "abc guide"
    assert concept_slug("发布规范") != concept_slug("发布语言")
    assert concept_slug("发布规范") == concept_slug("发布规范")
