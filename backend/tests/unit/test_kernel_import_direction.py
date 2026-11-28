"""Kernel guard: kernel files must not import upper layers (ratcheting baseline).

The kernel is the packages agent, session, question, permission, command and
snapshot, plus the 12 framework files of tool/ (TOOL_FRAMEWORK). It may import
the foundation (core, db, ...) and itself. Every import of an upper layer
(execution and extensions, memory, platform services, products) or of the
interface layer (api, main) is a violation. The existing ones are listed one
line per (kernel file, imported module) in kernel_import_baseline.txt, which
may only go down. Definitions: docs/KERNEL_DSH_COMPARISON_20261128.md, "怎么读
这份文档", section 10 step 0 and section 25.2 item 0-2. Part of `make check-kernel`.

What counts: every import statement in a kernel file, at module level or lazy
inside a function, including those under `if TYPE_CHECKING:` (marked as such),
relative imports (resolved), and importlib.import_module / __import__ calls with
a literal module name. `from pkg import a, b` of a top-level package counts once
per name, as the plan's statistics did. Imports between modules of tool/ count
too: tool/registry.py importing a general or product tool is a kernel file
importing an upper layer. Only source is parsed; nothing is imported or started.

Regenerate the baseline (after removing upward imports) with
    cd backend && uv run python scripts/kernel_import_baseline.py --write
"""
from __future__ import annotations  # the script also runs on a bare python3, without the project environment

import ast
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
BASELINE = Path(__file__).with_name("kernel_import_baseline.txt")
SCRIPT = "scripts/kernel_import_baseline.py"
REGENERATE = f"cd backend && uv run python {SCRIPT} --write"

# The complete layer table. Every top-level package or module of backend/ must
# be in exactly one layer (or in NOT_LAYERED); an unclassified one fails
# test_every_backend_package_is_classified.
LAYERS = {
    "foundation": "core db cache bus blob storage models audit",
    "kernel": "agent session question permission command snapshot",
    "exec+ext": "sandbox lsp mcp skill",
    "memory": "memory wiki_compiler",
    "platform-services": "auth billing trajectory cron notifications project assets",
    "products": "assistant voice light video platforms publish trends autopilot store",
    "interface": "api main",
}
# Lowest first. The kernel may import only the layers in KERNEL_MAY_IMPORT.
LAYER_ORDER = ("foundation", "kernel", "exec+ext", "memory", "platform-services", "products", "interface")
KERNEL_MAY_IMPORT = frozenset({"foundation", "kernel"})
# Not runtime code. They are not layers, and the kernel must not import them either.
NOT_LAYERED = frozenset({"tests", "scripts"})

# tool/ is split by file. The 12 framework files are kernel, these general tools
# are execution and extensions, and every other tool/ module is products.
TOOL_FRAMEWORK = frozenset("""
    tool registry truncation argument_repair invalid batch capability_search platform_plugins
    plugin_lifecycle sensitive_paths filetime __init__""".split())
TOOL_GENERAL = frozenset("""
    bash read write edit multiedit apply_patch glob_tool grep web_fetch web_search view_image computer
    browser_action browser_mode todo_tool plan task question_tool skill_tool skill_manage mcp_tool
    cron_tool share_file""".split())

PACKAGE_LAYER = {name: layer for layer, names in LAYERS.items() for name in names.split()}
SKIP_DIRS = {"__pycache__", ".venv", "node_modules"}


@dataclass(frozen=True)
class Import:
    file: str      # kernel file, relative to backend/
    line: int
    module: str    # imported backend module, or package.name for `from package import name`
    layer: str
    kind: str      # "top", "lazy", "type-checking" or "dynamic"


def backend_packages() -> set[str]:
    """Top-level packages (directories holding Python files) and modules of backend/."""
    found = set()
    for entry in BACKEND.iterdir():
        if entry.name.startswith(".") or entry.name in SKIP_DIRS:
            continue
        if entry.is_dir() and any(_python_files(entry)):
            found.add(entry.name)
        elif entry.suffix == ".py":
            found.add(entry.stem)
    return found


def _python_files(directory: Path):
    for path in sorted(directory.rglob("*.py")):
        if not SKIP_DIRS.intersection(path.relative_to(directory).parts):
            yield path


def kernel_files() -> list[Path]:
    files = []
    for package in LAYERS["kernel"].split():
        files.extend(_python_files(BACKEND / package))
    files.extend(BACKEND / "tool" / f"{name}.py" for name in sorted(TOOL_FRAMEWORK))
    return files


def layer_of(module: str) -> str | None:
    """Layer of a backend module; None for anything outside backend/ (stdlib, third party)."""
    parts = module.split(".")
    top = parts[0]
    if top in NOT_LAYERED:
        return "not-layered"
    if top == "tool":
        stem = parts[1] if len(parts) > 1 else "__init__"
        if stem in TOOL_FRAMEWORK:
            return "kernel"
        return "exec+ext" if stem in TOOL_GENERAL else "products"
    if top in PACKAGE_LAYER:
        return PACKAGE_LAYER[top]
    return "unclassified" if top in _PACKAGES else None


_PACKAGES = backend_packages()


class _Visitor(ast.NodeVisitor):
    def __init__(self, path: Path):
        self.relative = path.relative_to(BACKEND).as_posix()
        # The package a relative import starts from: the file's directory.
        self.package = list(path.relative_to(BACKEND).parent.parts)
        self.depth = 0
        self.type_checking = 0
        self.found: list[tuple[str, int, str]] = []  # (module, line, kind)

    def _kind(self) -> str:
        if self.type_checking:
            return "type-checking"
        return "lazy" if self.depth else "top"

    def visit_FunctionDef(self, node):
        self.depth += 1
        self.generic_visit(node)
        self.depth -= 1

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_Lambda = visit_FunctionDef

    def visit_If(self, node):
        guarded = "TYPE_CHECKING" in ast.unparse(node.test)
        self.visit(node.test)
        self.type_checking += guarded
        for child in node.body:
            self.visit(child)
        self.type_checking -= guarded
        for child in node.orelse:
            self.visit(child)

    def visit_Import(self, node):
        for alias in node.names:
            self.found.append((alias.name, node.lineno, self._kind()))

    def visit_ImportFrom(self, node):
        if node.level:
            base = self.package[:len(self.package) - (node.level - 1)] if node.level > 1 else self.package
            module = ".".join([*base, *([node.module] if node.module else [])])
        else:
            module = node.module or ""
        names = [alias.name for alias in node.names]
        if not module:  # `from .. import x` at the top of backend/: each name is a top-level module
            for name in names:
                self.found.append((name, node.lineno, self._kind()))
        elif "." not in module and module in _PACKAGES and names != ["*"]:
            # `from light import production, prompt`: one import per name.
            for name in names:
                self.found.append((f"{module}.{name}", node.lineno, self._kind()))
        else:
            self.found.append((module, node.lineno, self._kind()))

    def visit_Call(self, node):
        func = node.func
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
        if (name in {"import_module", "__import__"} and node.args and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str) and not node.args[0].value.startswith(".")):
            self.found.append((node.args[0].value, node.lineno, "dynamic"))
        self.generic_visit(node)


def _resolve(module: str) -> str:
    """`tool.ToolResult` names an attribute of tool/__init__.py, not a module of tool/."""
    parts = module.split(".")
    if parts[0] == "tool" and len(parts) > 1:
        candidate = BACKEND / "tool" / parts[1]
        if not (candidate.with_suffix(".py").exists() or candidate.is_dir()):
            return "tool"
    return module


def scan() -> list[Import]:
    """Every import in a kernel file that targets a layer the kernel may not import."""
    violations = []
    for path in kernel_files():
        visitor = _Visitor(path)
        visitor.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        for module, line, kind in visitor.found:
            layer = layer_of(_resolve(module))
            if layer is not None and layer not in KERNEL_MAY_IMPORT:
                violations.append(Import(visitor.relative, line, module, layer, kind))
    return violations


def tally(imports: list[Import]) -> Counter:
    return Counter((item.file, item.module) for item in imports)


def render_baseline(imports: list[Import]) -> str:
    """The baseline file for these imports: a header with totals, then one line per entry."""
    groups = defaultdict(list)
    for item in imports:
        groups[(item.file, item.module)].append(item)
    by_layer = Counter(item.layer for item in imports)
    inside_tool = [item for item in imports if item.file.startswith("tool/") and item.module.startswith("tool.")]
    between = by_layer - Counter(item.layer for item in inside_tool)
    upper = [layer for layer in LAYER_ORDER if layer not in KERNEL_MAY_IMPORT]
    extra = sorted({item.layer for item in imports} - set(upper))
    lines = [
        "# Kernel imports of upper layers: the ratchet baseline of tests/unit/test_kernel_import_direction.py.",
        "# One line per (kernel file, imported module): file, module, count, layer, how the imports are written.",
        f"# Generated by `{REGENERATE}`. Do not edit by hand. Counts may only go down.",
        f"# Total {len(imports)}: " + ", ".join(f"{layer} {by_layer[layer]}" for layer in [*upper, *extra]),
        f"#   between packages {sum(between.values())}: "
        + ", ".join(f"{layer} {between[layer]}" for layer in [*upper, *extra]),
        f"#   inside tool/ {len(inside_tool)}: "
        + ", ".join(f"{file} {count}" for file, count in sorted(Counter(i.file for i in inside_tool).items())),
    ]
    for (file, module), items in sorted(groups.items()):
        kinds = Counter(item.kind for item in items)
        how = " ".join(f"{kind}={kinds[kind]}" for kind in ("top", "lazy", "type-checking", "dynamic") if kinds[kind])
        lines.append(f"{file}  {module}  {len(items)}  {items[0].layer}  {how}")
    return "\n".join(lines) + "\n"


def read_baseline(path: Path = BASELINE) -> Counter:
    entries = Counter()
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip() or raw.startswith("#"):
            continue
        fields = raw.split()
        if len(fields) < 3 or not fields[2].isdigit():
            raise ValueError(f"{path.name}:{number}: expected 'file  module  count  ...', got {raw!r}")
        entries[(fields[0], fields[1])] += int(fields[2])
    return entries


def compare(current: Counter, baseline: Counter) -> tuple[dict, dict]:
    """(grown, shrunk): entries above the baseline, and baseline entries now lower or gone."""
    grown = {key: (baseline.get(key, 0), count) for key, count in current.items() if count > baseline.get(key, 0)}
    shrunk = {key: (count, current.get(key, 0)) for key, count in baseline.items() if current.get(key, 0) < count}
    return grown, shrunk


def describe_growth(grown: dict, imports: list[Import]) -> str:
    lines = []
    for (file, module), (was, now) in sorted(grown.items()):
        sites = [item for item in imports if (item.file, item.module) == (file, module)]
        where = ", ".join(f"{file}:{item.line} ({item.kind})" for item in sites)
        lines.append(f"  {file} -> {module} [{sites[0].layer}]: baseline {was}, now {now}; at {where}")
    return "\n".join(lines)


def describe_shrink(shrunk: dict) -> str:
    return "\n".join(f"  {file} -> {module}: baseline {was}, now {now}"
                     for (file, module), (was, now) in sorted(shrunk.items()))


# --- tests -------------------------------------------------------------------

def test_every_backend_package_is_classified():
    known = set(PACKAGE_LAYER) | NOT_LAYERED | {"tool"}  # tool/ is classified file by file
    unknown = sorted(_PACKAGES - known)
    assert not unknown, (
        f"backend/ has top-level packages or modules that are not in the layer table: {', '.join(unknown)}. "
        "Add each one to LAYERS in tests/unit/test_kernel_import_direction.py (foundation, kernel, exec+ext, "
        "memory, platform-services or products; interface for entry points) so the kernel import check "
        "knows whether the kernel may import it.")
    duplicated = sorted(name for name in PACKAGE_LAYER if sum(name in names.split() for names in LAYERS.values()) > 1)
    assert not duplicated, f"packages listed in more than one layer: {', '.join(duplicated)}"
    missing = sorted(name for name in TOOL_FRAMEWORK | TOOL_GENERAL if not (BACKEND / "tool" / f"{name}.py").exists())
    assert not missing, (
        f"tool/ files named in TOOL_FRAMEWORK or TOOL_GENERAL no longer exist: {', '.join(missing)}. "
        "Update the two lists in tests/unit/test_kernel_import_direction.py to the renamed files.")


def test_kernel_adds_no_upper_layer_imports():
    imports = scan()
    assert BASELINE.exists(), f"{BASELINE.name} is missing; create it with `{REGENERATE}`"
    grown, _ = compare(tally(imports), read_baseline())
    assert not grown, (
        "Kernel files import upper layers beyond the baseline (kernel_import_baseline.txt):\n"
        + describe_growth(grown, imports) + "\n"
        "The kernel (agent, session, question, permission, command, snapshot and the 12 framework files of "
        "tool/) may import only the foundation (core, db, cache, bus, blob, storage, models, audit) and itself. "
        "Keep the new logic in its own package and let that package call into the kernel, or pass the value "
        "in from the caller. Do not move the import into a function to hide it: lazy and TYPE_CHECKING "
        "imports count too. If the import is truly unavoidable, run "
        f"`cd backend && uv run python {SCRIPT} --write --allow-increase` and give the reason in the change "
        "description.")


def test_kernel_import_baseline_is_current():
    assert BASELINE.exists(), f"{BASELINE.name} is missing; create it with `{REGENERATE}`"
    _, shrunk = compare(tally(scan()), read_baseline())
    assert not shrunk, (
        "Upward imports were removed from kernel files (good), but kernel_import_baseline.txt still lists "
        "them:\n" + describe_shrink(shrunk) + "\n"
        f"Run `{REGENERATE}` and commit the updated baseline, so the baseline only goes down and the removed "
        "imports cannot come back unnoticed.")
