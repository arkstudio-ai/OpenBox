"""Apply Patch tool: structured multi-file patches."""
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from tool.tool import ToolResult, ToolContext, define_tool


class ApplyPatchArgs(BaseModel):
    patch: str = Field(description="The patch content in structured format")


@dataclass(frozen=True)
class PatchOperation:
    """One parsed filesystem mutation shared by policy and execution."""

    type: Literal["add", "update", "delete"]
    path: str
    content: str = ""


class PatchParseError(ValueError):
    """The patch does not contain a safe, recognizable file operation."""


def parse_patch(patch: str) -> list[PatchOperation]:
    """Parse the patch language once so authorization and execution agree."""
    lines = str(patch or "").strip().split("\n")
    operations: list[PatchOperation] = []
    current_type: Literal["add", "update", "delete"] | None = None
    current_path = ""
    current_content: list[str] = []

    def flush() -> None:
        nonlocal current_type, current_path, current_content
        if current_type is None:
            return
        if not current_path or "\x00" in current_path:
            raise PatchParseError("Patch file path is empty or invalid")
        operations.append(
            PatchOperation(
                type=current_type,
                path=current_path,
                content="\n".join(current_content),
            )
        )
        current_type = None
        current_path = ""
        current_content = []

    for line in lines:
        if line.startswith("*** Begin Patch"):
            continue
        elif line.startswith("*** End Patch"):
            flush()
            break
        elif line.startswith("*** Update File: "):
            flush()
            current_type = "update"
            current_path = line[17:].strip()
            current_content = []
        elif line.startswith("*** Add File: "):
            flush()
            current_type = "add"
            current_path = line[14:].strip()
            current_content = []
        elif line.startswith("*** Delete File: "):
            flush()
            current_type = "delete"
            current_path = line[17:].strip()
            current_content = []
        elif current_type is not None:
            current_content.append(line)

    flush()
    if not operations:
        raise PatchParseError("Patch contains no file operations")
    return operations


async def execute(args: ApplyPatchArgs, ctx: ToolContext) -> ToolResult:
    """Apply a structured patch to multiple files."""
    operations = parse_patch(args.patch)

    results = []
    for op in operations:
        try:
            execution_path = ctx.resolve_file_path(op.path)
        except ValueError as exc:
            results.append(f"Error on {op.path}: {exc}")
            continue
        try:
            if op.type == "add":
                content = "\n".join(
                    l[1:] if l.startswith("+") else l
                    for l in op.content.split("\n")
                    if not l.startswith("-")
                )
                await ctx.sandbox.write_file(execution_path, content)
                results.append(f"Added {op.path}")
            elif op.type == "delete":
                await ctx.sandbox.delete_file(execution_path)
                results.append(f"Deleted {op.path}")
            elif op.type == "update":
                # Read current file
                raw = await ctx.sandbox.read_file(
                    execution_path, offset=0, limit=100000
                )
                # Strip line numbers
                file_lines = []
                for l in raw.split("\n"):
                    tab_idx = l.find("\t")
                    file_lines.append(l[tab_idx + 1:] if tab_idx >= 0 else l)
                current = "\n".join(file_lines)

                # Apply patch hunks
                new_content = _apply_patch_hunks(current, op.content)
                await ctx.sandbox.write_file(execution_path, new_content)
                results.append(f"Updated {op.path}")
        except Exception as e:
            results.append(f"Error on {op.path}: {e}")

    return ToolResult(
        title=f"Applied patch ({len(operations)} files)",
        output="\n".join(results),
    )


def _apply_patch_hunks(content: str, patch_text: str) -> str:
    """Apply patch hunks to file content."""
    lines = content.split("\n")
    patch_lines = patch_text.split("\n")
    result = list(lines)

    # Simple approach: find context, apply changes
    i = 0
    offset = 0
    while i < len(patch_lines):
        line = patch_lines[i]
        if line.startswith("@@@"):
            i += 1
            continue
        elif line.startswith("-"):
            # Remove line - find it in result
            to_remove = line[1:]
            for j in range(max(0, offset), len(result)):
                if result[j].rstrip() == to_remove.rstrip():
                    result.pop(j)
                    offset = j
                    break
        elif line.startswith("+"):
            # Add line
            to_add = line[1:]
            result.insert(offset, to_add)
            offset += 1
        else:
            # Context line - advance offset
            for j in range(max(0, offset), len(result)):
                if result[j].rstrip() == line.rstrip():
                    offset = j + 1
                    break
        i += 1

    return "\n".join(result)


APPLY_PATCH_DESCRIPTION = """\
Apply a structured patch to create, update, or delete multiple files in one call.
Operations run in order; failures are reported per file and do not roll back an
earlier successful operation.

Your patch language is a stripped-down, file-oriented diff format:

*** Begin Patch
[ one or more file sections ]
*** End Patch

Each operation starts with one of three headers:
- *** Add File: <path> — create a new file. Every line is prefixed with +
- *** Delete File: <path> — remove an existing file
- *** Update File: <path> — patch an existing file in place
- *** Move to: <new_path> — rename an existing file during update

Example patch:

*** Begin Patch
*** Add File: hello.txt
+Hello world
*** Update File: src/app.py
@@ def greet():
-print("Hi")
+print("Hello, world!")
*** Delete File: obsolete.txt
*** End Patch

Important:
- You must include a header with your intended action (Add/Delete/Update)
- You must prefix new lines with `+` even when creating a new file
- For updates, include enough context lines (without +/- prefix) to locate the change uniquely"""

apply_patch_tool = define_tool(
    "apply_patch",
    description=APPLY_PATCH_DESCRIPTION,
    parameters=ApplyPatchArgs,
    execute=execute,
)
