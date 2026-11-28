"""Show or regenerate the kernel import baseline (tests/unit/kernel_import_baseline.txt).

    cd backend && uv run python scripts/kernel_import_baseline.py            # compare, print totals
    cd backend && uv run python scripts/kernel_import_baseline.py --write    # rewrite after removing imports

--write refuses to record new or larger entries: the baseline only goes down.
--allow-increase records them anyway; give the reason in the change description.
The layer table and the counting rules live in tests/unit/test_kernel_import_direction.py.
Only source is parsed and only the standard library is used, so a bare python3
works as well as `uv run python` (for example in a worktree without backend/.venv).
"""
import argparse
from collections import Counter
from pathlib import Path
import sys

BACKEND = Path(__file__).resolve().parents[1]
# This checkout's tests package, not one an installed environment points at.
sys.path.insert(0, str(BACKEND))

from tests.unit import test_kernel_import_direction as guard  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write", action="store_true", help="rewrite the baseline file")
    parser.add_argument("--allow-increase", action="store_true",
                        help="with --write, also record new or larger entries")
    args = parser.parse_args(argv)

    imports = guard.scan()
    rendered = guard.render_baseline(imports)
    baseline = guard.read_baseline() if guard.BASELINE.exists() else Counter()
    grown, shrunk = guard.compare(guard.tally(imports), baseline)
    print("\n".join(line[2:] for line in rendered.splitlines() if line.startswith("#   ") or
                    line.startswith("# Total")))
    if grown:
        print(f"\nAbove the baseline ({len(grown)} entries):\n" + guard.describe_growth(grown, imports))
    if shrunk:
        print(f"\nBelow the baseline ({len(shrunk)} entries):\n" + guard.describe_shrink(shrunk))

    if not args.write:
        if not grown and not shrunk:
            print("\nThe baseline matches the source.")
        return 1 if grown or shrunk else 0
    if grown and not args.allow_increase and guard.BASELINE.exists():
        print("\nNot written: the baseline only goes down. Remove the new imports, or rerun with "
              "--allow-increase and give the reason in the change description.")
        return 1
    current = guard.BASELINE.read_text(encoding="utf-8") if guard.BASELINE.exists() else ""
    if rendered == current:
        print(f"\n{guard.BASELINE.relative_to(BACKEND)} is already up to date.")
        return 0
    guard.BASELINE.write_text(rendered, encoding="utf-8")
    print(f"\nWrote {guard.BASELINE.relative_to(BACKEND)} ({len(guard.tally(imports))} entries).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
