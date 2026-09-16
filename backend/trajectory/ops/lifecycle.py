"""Merge OpenBox's OSS lifecycle rules into a bucket's existing rules (SPEC §12).

PutBucketLifecycle replaces every rule of a bucket, and the production bucket is
shared with user assets, so deploy/gw2/scripts/apply-oss-lifecycle.sh first reads
the current configuration and this module merges the desired rules in by ID,
keeping every other rule as it is. It refuses what OSS would reject or what
would silently change someone else's rule set: rules without an ID, a part
policy (AbortMultipartUpload) next to a Not filter, a Not prefix outside its
rule prefix, two part policies on overlapping prefixes, and, unless explicitly
allowed (the x-oss-allow-same-action-overlap header), two rules with the same
action on overlapping prefixes. Only pairs involving a managed rule are checked;
the bucket already accepted its own rules.

Standard library only, so an operator machine needs nothing but python3:

    python3 backend/trajectory/ops/lifecycle.py merge --existing current.xml \\
        --desired deploy/gw2/oss-lifecycle.xml --output merged.xml
    python3 backend/trajectory/ops/lifecycle.py verify --expected merged.xml --actual after.xml

``merge`` exits 0 when the merged rules differ from the bucket's, 3 when there
is nothing to apply and 2 on conflicts or unreadable input; ``verify`` exits 1
when the bucket does not hold exactly the expected rules.
"""
import argparse
import copy
import sys
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

ROOT = "LifecycleConfiguration"
ACTIONS = (
    "Expiration", "Transition", "NoncurrentVersionExpiration", "NoncurrentVersionTransition", "AbortMultipartUpload",
)
# Returned by GetBucketLifecycle, not accepted as part of a rule on PUT.
RESPONSE_ONLY = ("AtimeBase",)
# Flags OSS may echo with their default value; absent and default compare equal.
DEFAULT_FALSE = ("IsAccessTime", "ReturnToStdWhenVisit", "AllowSmallFile", "ExpiredObjectDeleteMarker")
MAX_RULES = 1000
UNCHANGED = 3


class LifecycleError(Exception):
    def __init__(self, problems: list[str] | str):
        self.problems = [problems] if isinstance(problems, str) else list(problems)
        super().__init__("; ".join(self.problems))


def parse(text: str) -> list[ElementTree.Element]:
    """Rules of a LifecycleConfiguration document; empty input means no rules.

    CLI output may surround the document with other text, so only the span from
    the root start tag to its end tag is parsed.
    """
    text = (text or "").strip()
    if not text:
        return []
    start = text.find(f"<{ROOT}")
    if start < 0:
        raise LifecycleError("input holds no LifecycleConfiguration document")
    close = text.rfind(f"</{ROOT}>")
    document = text[start:close + len(ROOT) + 3] if close > start else text[start:text.find(">", start) + 1]
    try:
        root = ElementTree.fromstring(document)
    except ElementTree.ParseError as exc:
        raise LifecycleError(f"invalid lifecycle XML: {exc}") from None
    for element in root.iter():
        element.tag = element.tag.rsplit("}", 1)[-1]
    return [rule for rule in root if rule.tag == "Rule"]


def _text(element: ElementTree.Element | None, path: str) -> str:
    child = None if element is None else element.find(path)
    return (child.text or "").strip() if child is not None else ""


def rule_id(rule: ElementTree.Element) -> str:
    return _text(rule, "ID")


def rule_prefix(rule: ElementTree.Element) -> str:
    return _text(rule, "Prefix")


def actions(rule: ElementTree.Element) -> set[str]:
    return {child.tag for child in rule if child.tag in ACTIONS}


def _not_filters(rule: ElementTree.Element) -> list[ElementTree.Element]:
    return rule.findall("Filter/Not")


def _excluded(rule: ElementTree.Element, prefix: str) -> bool:
    """Whether a prefix-only Not filter of ``rule`` excludes every object under ``prefix``."""
    for node in _not_filters(rule):
        excluded = _text(node, "Prefix")
        if excluded and node.find("Tag") is None and prefix.startswith(excluded):
            return True
    return False


def overlaps(first: ElementTree.Element, second: ElementTree.Element) -> bool:
    a, b = rule_prefix(first), rule_prefix(second)
    if not (a.startswith(b) or b.startswith(a)):
        return False
    common = a if len(a) >= len(b) else b
    return not (_excluded(first, common) or _excluded(second, common))


def problems(rules: list[ElementTree.Element], managed: set[str], *, allow_same_action_overlap: bool) -> list[str]:
    found = []
    if len(rules) > MAX_RULES:
        found.append(f"{len(rules)} rules exceed the OSS limit of {MAX_RULES}")
    seen: set[str] = set()
    for rule in rules:
        identifier = rule_id(rule)
        if not identifier:
            found.append(f"a rule on prefix {rule_prefix(rule)!r} has no ID")
        elif identifier in seen:
            found.append(f"rule ID {identifier} appears twice")
        seen.add(identifier)
        if identifier not in managed:
            continue
        prefix = rule_prefix(rule)
        for node in _not_filters(rule):
            excluded = _text(node, "Prefix")
            if prefix and not excluded.startswith(prefix):
                found.append(f"{identifier}: Not prefix {excluded!r} must start with the rule prefix {prefix!r}")
            if node.find("Tag") is None and excluded == prefix:
                found.append(f"{identifier}: a Not filter without a tag must differ from the rule prefix")
        if _not_filters(rule) and "AbortMultipartUpload" in actions(rule):
            found.append(f"{identifier}: AbortMultipartUpload cannot be combined with a Not filter")
    for index, first in enumerate(rules):
        for second in rules[index + 1:]:
            if not {rule_id(first), rule_id(second)} & managed or not overlaps(first, second):
                continue
            label = (
                f"{rule_id(first)} ({rule_prefix(first) or '<whole bucket>'}) and "
                f"{rule_id(second)} ({rule_prefix(second) or '<whole bucket>'})"
            )
            shared = actions(first) & actions(second)
            if "AbortMultipartUpload" in shared:
                found.append(f"{label} both abort multipart uploads on overlapping prefixes")
            same = sorted(shared - {"AbortMultipartUpload"})
            if same and not allow_same_action_overlap:
                found.append(
                    f"{label} apply {', '.join(same)} to overlapping prefixes; "
                    "pass --allow-same-action-overlap if that is intended"
                )
    return found


def _fields(element: ElementTree.Element | None) -> tuple:
    if element is None:
        return ()
    values = {child.tag: (child.text or "").strip() for child in element if len(child) == 0}
    for flag in DEFAULT_FALSE:
        if values.get(flag, "false").lower() == "false":
            values.pop(flag, None)
    return tuple(sorted(values.items()))


def semantic(rule: ElementTree.Element) -> dict:
    """What a rule does, independent of element order, whitespace and echoed defaults."""
    return {
        "Prefix": rule_prefix(rule),
        "Status": _text(rule, "Status"),
        "Not": sorted((_text(node, "Prefix"), _fields(node.find("Tag"))) for node in _not_filters(rule)),
        "Tag": sorted(_fields(tag) for tag in rule.findall("Tag")),
        **{action: sorted(_fields(node) for node in rule.findall(action)) for action in ACTIONS},
    }


def describe(rule: ElementTree.Element) -> str:
    parts = [f"{rule_id(rule) or '<no id>'}: prefix={rule_prefix(rule) or '<whole bucket>'}", _text(rule, "Status")]
    parts += [f"not={_text(node, 'Prefix')}" for node in _not_filters(rule)]
    for action in ACTIONS:
        for node in rule.findall(action):
            parts.append(f"{action}({','.join(f'{key}={value}' for key, value in _fields(node))})")
    return " ".join(parts)


def _writable(rule: ElementTree.Element) -> ElementTree.Element:
    rule = copy.deepcopy(rule)
    for parent in list(rule.iter()):
        for child in list(parent):
            if child.tag in RESPONSE_ONLY:
                parent.remove(child)
    return rule


def serialize(rules: list[ElementTree.Element]) -> str:
    root = ElementTree.Element(ROOT)
    root.extend(_writable(rule) for rule in rules)
    ElementTree.indent(root)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ElementTree.tostring(root, encoding="unicode") + "\n"


@dataclass
class MergeResult:
    rules: list[ElementTree.Element]
    kept: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated)

    def xml(self) -> str:
        return serialize(self.rules)


def merge(existing_text: str, desired_text: str, *, allow_same_action_overlap: bool = False) -> MergeResult:
    """Existing rules with the desired ones replacing same-ID rules in place and the rest appended."""
    desired: dict[str, ElementTree.Element] = {}
    for rule in parse(desired_text):
        identifier = rule_id(rule)
        if not identifier or identifier in desired:
            raise LifecycleError("every desired rule needs a unique ID")
        desired[identifier] = rule
    if not desired:
        raise LifecycleError("the desired configuration has no rules")
    result = MergeResult(rules=[])
    for rule in parse(existing_text):
        identifier = rule_id(rule)
        if identifier in desired:
            wanted = desired[identifier]
            (result.unchanged if semantic(rule) == semantic(wanted) else result.updated).append(identifier)
            result.rules.append(copy.deepcopy(wanted))
        else:
            result.kept.append(identifier)
            result.rules.append(_writable(rule))
    for identifier, rule in desired.items():
        if identifier not in result.updated and identifier not in result.unchanged:
            result.added.append(identifier)
            result.rules.append(copy.deepcopy(rule))
    found = problems(result.rules, set(desired), allow_same_action_overlap=allow_same_action_overlap)
    if found:
        raise LifecycleError(found)
    return result


def verify(expected_text: str, actual_text: str) -> list[str]:
    """Differences between the rules that were applied and the rules the bucket reports."""
    expected = {rule_id(rule): semantic(rule) for rule in parse(expected_text)}
    actual = {rule_id(rule): semantic(rule) for rule in parse(actual_text)}
    found = [f"rule {identifier} is missing" for identifier in expected if identifier not in actual]
    found += [
        f"rule {identifier} differs: expected {expected[identifier]}, found {actual[identifier]}"
        for identifier in expected
        if identifier in actual and actual[identifier] != expected[identifier]
    ]
    found += [f"unexpected rule {identifier}" for identifier in actual if identifier not in expected]
    return found


def _read(path: str) -> str:
    return sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lifecycle.py", description="Merge and verify OSS lifecycle rules.")
    commands = parser.add_subparsers(dest="command", required=True)
    merge_command = commands.add_parser("merge", help="merge desired rules into the existing ones")
    merge_command.add_argument("--existing", required=True,
                               help="get-bucket-lifecycle output; an empty file means the bucket has no rules")
    merge_command.add_argument("--desired", required=True)
    merge_command.add_argument("--output", required=True)
    merge_command.add_argument("--allow-same-action-overlap", action="store_true")
    verify_command = commands.add_parser("verify", help="compare the applied rules with the bucket's rules")
    verify_command.add_argument("--expected", required=True)
    verify_command.add_argument("--actual", required=True)
    show_command = commands.add_parser("show", help="print one line per rule")
    show_command.add_argument("file")
    args = parser.parse_args(argv)
    try:
        if args.command == "merge":
            result = merge(
                _read(args.existing), _read(args.desired), allow_same_action_overlap=args.allow_same_action_overlap
            )
            Path(args.output).write_text(result.xml(), encoding="utf-8")
            for label in ("kept", "added", "updated", "unchanged"):
                print(f"{label}: {', '.join(getattr(result, label)) or '-'}")
            for rule in result.rules:
                print(f"  {describe(rule)}")
            return 0 if result.changed else UNCHANGED
        if args.command == "verify":
            found = verify(_read(args.expected), _read(args.actual))
            for problem in found:
                print(problem, file=sys.stderr)
            return 1 if found else 0
        for rule in parse(_read(args.file)):
            print(describe(rule))
        return 0
    except LifecycleError as exc:
        for problem in exc.problems:
            print(f"error: {problem}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
