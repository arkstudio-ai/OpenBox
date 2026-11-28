"""Select test files under backend/tests (not manual/) that import a kernel package.

Static scan with ast: every Import / ImportFrom node anywhere in the file
(module level, inside functions, inside try blocks). Also counts
importlib.import_module("<kernel>...") string calls.
Usage: python select_kernel_tests.py <backend_dir> <out_json>
"""
import ast
import json
import sys
from pathlib import Path

KERNEL_TOP = {"agent", "session", "question", "permission", "command", "snapshot"}
KERNEL_TOOL = {
    "tool.tool", "tool.registry", "tool.truncation", "tool.argument_repair", "tool.invalid",
    "tool.batch", "tool.capability_search", "tool.platform_plugins", "tool.plugin_lifecycle",
    "tool.sensitive_paths", "tool.filetime",
}


def kernel_hits(module: str, names: list[str]) -> set[str]:
    hits = set()
    if not module:
        return hits
    top = module.split(".")[0]
    if top in KERNEL_TOP:
        hits.add(top)
    for km in KERNEL_TOOL:
        if module == km or module.startswith(km + "."):
            hits.add(km)
    if module == "tool":
        for n in names:
            if f"tool.{n}" in KERNEL_TOOL:
                hits.add(f"tool.{n}")
    return hits


def scan(path: Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return {"<syntax-error>"}
    hits: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                hits |= kernel_hits(alias.name, [])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                hits |= kernel_hits(node.module, [a.name for a in node.names])
        elif isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")
            if name == "import_module" and node.args and isinstance(node.args[0], ast.Constant) \
                    and isinstance(node.args[0].value, str):
                hits |= kernel_hits(node.args[0].value, [])
    return hits


def main() -> None:
    backend = Path(sys.argv[1]).resolve()
    out = Path(sys.argv[2])
    tests = backend / "tests"
    selected = []
    total = 0
    for p in sorted(tests.rglob("*.py")):
        rel = p.relative_to(backend)
        parts = rel.parts
        if "manual" in parts:
            continue
        if not (p.name.startswith("test_") or p.name.endswith("_test.py")):
            continue
        total += 1
        hits = scan(p)
        if hits:
            selected.append({"file": str(rel), "kernel_imports": sorted(hits)})
    out.write_text(json.dumps({"total_test_files": total, "selected": selected}, indent=1))
    print(f"test files scanned: {total}; selected: {len(selected)}")
    by_dir = {}
    for s in selected:
        d = s["file"].split("/")[1]
        by_dir[d] = by_dir.get(d, 0) + 1
    print(by_dir)


if __name__ == "__main__":
    main()
