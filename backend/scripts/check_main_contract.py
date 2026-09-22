"""Read-only inventory check for removed main routes, fields and tool names.

This is a structural guard, not a behavioral-equivalence test. It deliberately
avoids importing application code, loading credentials, or starting services.
Run from any directory with --base pointing at the main revision to preserve.
"""
import argparse
import ast
import json
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]
SCOPES = ("backend/api", "backend/tool", "backend/core/config.py", "backend/models")


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True)


def inventory(source):
    result = {"routes": set(), "model_fields": set(), "builtin_tools": set()}
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                if (isinstance(decorator, ast.Call)
                        and isinstance(decorator.func, ast.Attribute)
                        and decorator.func.attr in {"get", "post", "put", "patch", "delete", "websocket"}
                        and decorator.args
                        and isinstance(decorator.args[0], ast.Constant)):
                    result["routes"].add((ast.unparse(decorator.func.value),
                                          decorator.func.attr, decorator.args[0].value))
        if isinstance(node, ast.ClassDef) and any(
            "BaseModel" in ast.unparse(base) or "Args" in ast.unparse(base) for base in node.bases
        ):
            result["model_fields"].update(
                (node.name, field.target.id) for field in node.body
                if isinstance(field, ast.AnnAssign) and isinstance(field.target, ast.Name)
            )
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "define_tool" and node.args
                and isinstance(node.args[0], ast.Constant)):
            result["builtin_tools"].add(node.args[0].value)
    return result


def current_source_path(filename):
    """Follow an unambiguous flat-tool move into a functional domain.

    Match the implementation filename, not a union of similarly named models
    across the repository: another module must not hide a removed contract.
    Routes and other source files continue to require their original path.
    """
    path = ROOT / filename
    if path.is_file():
        return path
    if path.parent == ROOT / "backend" / "tool":
        matches = [candidate for candidate in path.parent.glob(f"*/{path.name}") if candidate.is_file()]
        if len(matches) == 1:
            return matches[0]
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="origin/main")
    args = parser.parse_args()
    base = git("rev-parse", "--verify", f"{args.base}^{{commit}}").strip()
    files = git("ls-tree", "-r", "--name-only", base, *SCOPES).splitlines()
    counts = {"routes": 0, "model_fields": 0, "builtin_tools": 0}
    missing = []
    for filename in files:
        if not filename.endswith(".py"):
            continue
        previous = inventory(git("show", f"{base}:{filename}"))
        path = current_source_path(filename)
        current = inventory(path.read_text()) if path is not None else {key: set() for key in counts}
        for kind, items in previous.items():
            counts[kind] += len(items)
            missing.extend({"file": filename, "kind": kind, "missing": item}
                           for item in sorted(items - current[kind]))
    print(json.dumps({"baseline": base, "counts": counts, "missing": missing},
                     indent=2, ensure_ascii=False))
    return bool(missing)


if __name__ == "__main__":
    raise SystemExit(main())
