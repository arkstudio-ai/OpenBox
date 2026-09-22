"""Contract checks must follow moves without hiding removed definitions."""
from scripts import check_main_contract as check


def test_tool_contract_follows_unique_domain_move(tmp_path, monkeypatch):
    monkeypatch.setattr(check, "ROOT", tmp_path)
    path = tmp_path / "backend/tool/workspace/read.py"
    path.parent.mkdir(parents=True)
    path.write_text('tool = define_tool("read")')
    resolved = check.current_source_path("backend/tool/read.py")
    assert resolved == path
    assert check.inventory(resolved.read_text())["builtin_tools"] == {"read"}


def test_ambiguous_move_does_not_hide_missing_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(check, "ROOT", tmp_path)
    for domain in ("workspace", "integrations"):
        path = tmp_path / "backend/tool" / domain / "read.py"
        path.parent.mkdir(parents=True)
        path.write_text("")
    assert check.current_source_path("backend/tool/read.py") is None


def test_other_source_paths_do_not_match_unrelated_files(tmp_path, monkeypatch):
    monkeypatch.setattr(check, "ROOT", tmp_path)
    path = tmp_path / "backend/tool/workspace/read.py"
    path.parent.mkdir(parents=True)
    path.write_text("")
    assert check.current_source_path("backend/api/read.py") is None
