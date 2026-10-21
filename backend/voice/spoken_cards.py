"""Match assistant output to exact display literals from a frozen card snapshot.

This is a playback dependency, never an intent router or consent check. User
speech is not inspected. Shared labels cannot select one of several cards.
"""
from voice.cards import version


def normalized(value):
    return "".join(char for char in str(value or "").casefold() if char.isalnum())


def snapshot(main_cards, questions):
    result = {}
    for card in main_cards:
        literals = [card.get("task_title"), *(card.get("options") or [])]
        result[card["card_id"]] = (version(card), _literals(literals))
    for question in questions:
        literals = [question.get("session_title"), question.get("project_name")]
        for item in question.get("questions") or []:
            literals.extend(item.get("options") or [])
        result[question["id"]] = (question.get("version", ""), _literals(literals))
    return result


def _literals(values):
    # Short generic options (yes/cancel/确认) do not identify a subject.
    return {text for value in values if len(text := normalized(value)) >= 6}


def mentioned(text, sources):
    owners = {}
    for identity, (_, literals) in sources.items():
        for literal in literals:
            owners.setdefault(literal, set()).add(identity)
    spoken = normalized(text)
    identities = {next(iter(ids)) for literal, ids in owners.items()
                  if len(ids) == 1 and literal in spoken}
    return {identity: sources[identity][0] for identity in identities}
