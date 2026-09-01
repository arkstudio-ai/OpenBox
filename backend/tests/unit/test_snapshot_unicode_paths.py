"""Snapshot commands keep project-relative Unicode paths readable."""

from snapshot.snapshot import Store, _parse_unified_diff


def test_snapshot_git_commands_disable_octal_path_quoting():
    command = Store(
        gitdir="/workspace/.snapshots/project",
        workdir="/workspace/project",
    ).git("diff-tree --numstat -r before after")

    assert command.startswith("git -c core.quotePath=false ")


def test_unified_diff_parser_preserves_unicode_path_and_content():
    entries = _parse_unified_diff(
        "\n".join(
            [
                "diff --git a/中文目录/你好.txt b/中文目录/你好.txt",
                "--- /dev/null",
                "+++ b/中文目录/你好.txt",
                "@@ -0,0 +1,2 @@",
                "+你好，OpenBox！",
                "+路径显示必须正确且不能乱码",
            ]
        )
    )

    assert entries[0]["path"] == "中文目录/你好.txt"
    assert [line["content"] for line in entries[0]["hunks"][0]["lines"]] == [
        "你好，OpenBox！",
        "路径显示必须正确且不能乱码",
    ]
