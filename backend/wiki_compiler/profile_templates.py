"""A general source-to-reviewed-knowledge workflow, editable as a declaration."""
KNOWLEDGE_REVIEW = {
    "schemaVersion": 1, "profileId": "knowledge-review", "profileVersion": "1.0", "title": "Knowledge review",
    "entities": {"topic": {"title": "Topic", "fields": {
        "summary": {"type": "string", "required": True, "min": 1, "max": 800},
        "state": {"type": "enum", "enum": ["draft", "reviewed", "archived"], "required": True}},
        "lifecycle": {"field": "state", "initial": "draft", "terminal": ["archived"],
            "transitions": {"draft": ["reviewed"], "reviewed": ["archived"], "archived": []},
            "transitionRequirements": {"reviewed": ["summary"]}}}},
    "relations": {"supports": {"from": ["topic"], "to": ["topic"], "direction": "directed",
        "attributes": {"note": {"type": "string", "max": 800}}}},
    "artifacts": {"review-note": {"title": "Review note", "mediaTypes": ["text/markdown", "text/plain", "application/json"]}},
    "workflows": {"organize-review": {"title": "Organize and review", "inputs": {
        "goal": {"type": "string", "required": True, "min": 1, "max": 800}}, "stages": [
            {"id": "organize", "title": "Organize sources", "reads": [], "writes": [], "action": "organize",
             "gates": ["agent:task-completed"]},
            {"id": "document", "title": "Review and classify a published page", "reads": ["topic"], "writes": ["topic"],
             "outputsRequired": 1, "gates": ["human:review", "trust:sources-current"]},
            {"id": "finalize", "title": "Confirm lifecycle", "reads": ["topic"], "writes": ["topic"],
             "relationWrites": ["supports"], "artifactWrites": ["review-note"], "outputsRequired": 1,
             "gates": ["human:finalize", "trust:sources-current"]}]}}
}
