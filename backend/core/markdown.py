"""YAML frontmatter parsing for Skill and Command files."""
from typing import Any

try:
    import frontmatter
except ImportError:
    frontmatter = None


def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Parse a markdown file with YAML frontmatter.

    Returns (metadata_dict, body_content).
    """
    if frontmatter:
        post = frontmatter.loads(content)
        return dict(post.metadata), post.content

    # Fallback: manual parsing
    if not content.startswith("---"):
        return {}, content

    end = content.find("---", 3)
    if end == -1:
        return {}, content

    import yaml
    try:
        metadata = yaml.safe_load(content[3:end]) or {}
    except yaml.YAMLError:
        metadata = {}

    body = content[end + 3:].lstrip("\n")
    return metadata, body


#: Skill descriptions are advertised on every request, so a frontmatter that has
#: grown into prose costs tokens on all of them. The full text stays in the body,
#: which the skill tool returns when the model actually loads the skill.
MAX_DESCRIPTION_CHARS = 500


def clip_description(text: str, limit: int = MAX_DESCRIPTION_CHARS) -> str:
    """Collapse whitespace and cap a skill description at ``limit`` characters."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"
