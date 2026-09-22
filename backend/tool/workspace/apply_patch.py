"""Apply Patch tool: structured multi-file patches."""
import shlex
from pydantic import BaseModel, Field

from tool.tool import ToolResult, ToolContext, define_tool


class ApplyPatchArgs(BaseModel):
    patch: str = Field(description="The patch content in structured format")


def parse_patch(patch: str) -> list[dict]:
    """Share the exact operation targets between permission checks and execution."""
    lines = patch.strip().splitlines()
    if not lines or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
        raise ValueError("Patch must start with *** Begin Patch and end with *** End Patch")
    operations = []
    current_op = None
    current_content = []

    for line in lines[1:-1]:
        if line.startswith("*** Update File: "):
            if current_op:
                current_op["content"] = "\n".join(current_content)
                operations.append(current_op)
            current_op = {"type": "update", "path": line[17:].strip()}
            current_content = []
        elif line.startswith("*** Add File: "):
            if current_op:
                current_op["content"] = "\n".join(current_content)
                operations.append(current_op)
            current_op = {"type": "add", "path": line[14:].strip()}
            current_content = []
        elif line.startswith("*** Delete File: "):
            if current_op:
                current_op["content"] = "\n".join(current_content)
                operations.append(current_op)
            operations.append({"type": "delete", "path": line[17:].strip(), "content": ""})
            current_op = None
            current_content = []
        elif line.startswith("*** "):
            raise ValueError(f"Unsupported patch header: {line}")
        elif current_op is None and line.strip():
            raise ValueError("Patch content must follow an Add File or Update File header")
        else:
            current_content.append(line)

    if current_op:
        current_op["content"] = "\n".join(current_content)
        operations.append(current_op)

    if not operations or any(not op["path"] or "\x00" in op["path"] for op in operations):
        raise ValueError("Patch must include at least one operation with a valid file path")
    return operations


async def execute(args: ApplyPatchArgs, ctx: ToolContext) -> ToolResult:
    """Apply a structured patch to multiple files."""
    operations = [
        {**op, "path": ctx.resolve_file_path(op["path"])}
        for op in parse_patch(args.patch)
    ]

    from trajectory.files import captures_files, record_file_change
    from tool.workspace.edit import _strip_line_numbers
    results = []
    errors = 0
    for op in operations:
        before = after = None
        try:
            if op["type"] == "add":
                content = "\n".join(
                    l[1:] if l.startswith("+") else l
                    for l in op["content"].split("\n")
                    if not l.startswith("-")
                )
                await ctx.sandbox.write_file(op["path"], content)
                after = content
                results.append(f"Added {op['path']}")
            elif op["type"] == "delete":
                if captures_files(ctx):
                    try:
                        before = _strip_line_numbers(await ctx.sandbox.read_file(op["path"], offset=0, limit=100000))
                    except Exception:
                        pass
                outcome = await ctx.sandbox.execute(f"rm -f -- {shlex.quote(op['path'])}")
                if outcome.exit_code != 0:
                    raise RuntimeError(outcome.stderr or "File deletion failed")
                results.append(f"Deleted {op['path']}")
            elif op["type"] == "update":
                # Read current file
                raw = await ctx.sandbox.read_file(op["path"], offset=0, limit=100000)
                # Strip line numbers
                file_lines = []
                for l in raw.split("\n"):
                    tab_idx = l.find("\t")
                    file_lines.append(l[tab_idx + 1:] if tab_idx >= 0 else l)
                current = "\n".join(file_lines)
                before = current

                # Apply patch hunks
                new_content = _apply_patch_hunks(current, op["content"])
                await ctx.sandbox.write_file(op["path"], new_content)
                after = new_content
                results.append(f"Updated {op['path']}")
            await record_file_change(ctx, op["path"], operation=op["type"], before=before, after=after)
        except Exception as e:
            errors += 1
            results.append(f"Error on {op['path']}: {e}")

    return ToolResult(
        title=f"Applied patch ({len(operations)} files)",
        output="\n".join(results),
        metadata={"error": bool(errors), "failed_files": errors},
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
Apply a structured patch to create, update, or delete multiple files.
Each file's outcome is reported separately; a failure does not roll back earlier files.

Your patch language is a stripped-down, file-oriented diff format:

*** Begin Patch
[ one or more file sections ]
*** End Patch

Each operation starts with one of three headers:
- *** Add File: <path> — create a new file. Every line is prefixed with +
- *** Delete File: <path> — remove an existing file
- *** Update File: <path> — patch an existing file in place

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
