"""Match assistant output to exact display literals from a frozen card snapshot.

This is a playback dependency, never an intent router or consent check. User
speech is not inspected. Shared labels cannot select one of several cards.
"""
import re

from voice.cards import version

# Words that frame any card ("the task is waiting for your decision…") or name
# what every video card is about (shots and their positions, script, duration);
# a phrase needs two words outside these to say anything about which card is meant.
GENERIC = frozenset("""a an the is are was were be been to of for on in at by with and or but not no your you yours
i me my we our it its this that these those there here what which who whom how where when why would could can will
shall should may might must please just now currently still also either both each all any some one as so if then
than about from into onto over under after before up down out do does did done have has had get got let lets
task tasks video videos decision decisions decide choose chose choice choices option options prefer like want wants
need needs waiting wait confirm confirms confirmation approve approval review card cards current ready next step
okay ok sure yes go ahead know tell think sound sounds good great right fine thanks thank help anything else something
pending asking asks ask first second third last version versions change changes changed keep keeping make made
script scripts shot shots scene scenes clip clips footage plan plans storyboard copy text subtitle subtitles music
seconds second minute minutes total duration length credits credit cost costs tier standard quick quality""".split())
GENERIC_CJK = frozenset("的了是在和或您你我他她它们这那个一要请吗吧呢就也都还把被为与及并而且需确认下想选择任务视频方案镜头"
                        "分镜文案时长秒画面版本修改调整卡片积分档生成制作字幕配乐音乐好可以什么怎样哪些种张条项件等待决定问题"
                        "答复处理通知现时候先再又很更最已经正没不无对给让从到着过后前里上中来去说看做用能会第二三四五六七八九十两几")
# Phrases of four words, two of them specific; a reply repeats one card's
# announcement when four such phrases came from it and no other card's
# (measured: unrelated replies share at most one, explanations five or more).
RENDERING_WORDS, RENDERING_SPECIFIC, RENDERING_MATCHES = 4, 2, 4


def normalized(value):
    return "".join(char for char in str(value or "").casefold() if char.isalnum())


def names(decision):
    """The display names a card or task question goes by (a task's or project's title)."""
    return [name for name in (decision.get("task_title"), decision.get("session_title"), decision.get("project_name"))
            if name]


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


def _tokens(text):
    return re.findall(r"[a-z0-9]+|[㐀-鿿]", str(text or "").casefold())


def _phrases(text):
    tokens = _tokens(text)
    return {tuple(tokens[index:index + RENDERING_WORDS]) for index in range(len(tokens) - RENDERING_WORDS + 1)}


def rendering(text, display_names=()):
    """Distinctive phrases of what the model itself said about one card, in whatever language it spoke.

    Its display names are left to ``mentioned``, which refuses a name several
    cards share: another card of the same task goes by the same title.
    """
    named = set().union(*map(_phrases, display_names))
    return frozenset(phrase for phrase in _phrases(text) if phrase not in named and sum(
        token not in GENERIC and token not in GENERIC_CJK for token in phrase) >= RENDERING_SPECIFIC)


def rendered(text, renderings):
    """Cards whose own announcement a reply repeats, by several phrases only that card's announcement used.

    A translated name defeats exact literals (measured 2026-10-27: an English
    reply explained a Chinese card as "the video task"), but the model reuses
    its own wording for the card it is explaining. Boilerplate, a task's name
    or another card's subject never adds up to a match.
    """
    owners = {}
    for identity, (_, phrases) in renderings.items():
        for phrase in phrases:
            owners.setdefault(phrase, set()).add(identity)
    hits = {}
    for phrase in rendering(text):
        if len(ids := owners.get(phrase, ())) == 1:
            identity = next(iter(ids))
            hits[identity] = hits.get(identity, 0) + 1
    return {identity: renderings[identity][0] for identity, count in hits.items() if count >= RENDERING_MATCHES}
