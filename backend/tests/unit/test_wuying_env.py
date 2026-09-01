from pathlib import Path

from scripts import wuying_env


def test_dev_launcher_uses_frontend_proxy_port_and_explicit_profile():
    launcher = (wuying_env.BACKEND_DIR / "scripts" / "wuying_dev.sh").read_text(
        encoding="utf-8",
    )

    assert 'BACKEND_PORT="${OPENBOX_DEV_BACKEND_PORT:-8080}"' in launcher
    assert 'export OPENBOX_ENV_FILE="$DEV_ENV"' in launcher
    assert 'export JWT_SECRET=""' in launcher


def test_explicit_process_value_wins_over_profile(tmp_path: Path, monkeypatch):
    profile = tmp_path / "prod.env"
    profile.write_text("WUYING_DESKTOP_ID=from-file\n", encoding="utf-8")
    monkeypatch.setenv("OPENBOX_ENV_FILE", str(profile))
    monkeypatch.setenv("WUYING_DESKTOP_ID", "from-process")

    assert wuying_env.environment_file() == profile
    assert wuying_env.environment_value("WUYING_DESKTOP_ID") == "from-process"


def test_explicit_profile_is_used_without_falling_back(tmp_path: Path, monkeypatch):
    profile = tmp_path / "prod.env"
    profile.write_text("WUYING_DESKTOP_ID=from-profile\n", encoding="utf-8")
    monkeypatch.setenv("OPENBOX_ENV_FILE", str(profile))
    monkeypatch.delenv("WUYING_DESKTOP_ID", raising=False)

    assert wuying_env.environment_value("WUYING_DESKTOP_ID") == "from-profile"


def test_missing_explicit_profile_fails_closed(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENBOX_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.delenv("WUYING_DESKTOP_ID", raising=False)

    assert wuying_env.environment_file() is None
    assert wuying_env.environment_value("WUYING_DESKTOP_ID") == ""


def test_default_profile_prefers_user_env_over_dev_profile(tmp_path: Path, monkeypatch):
    (tmp_path / ".env").write_text("OPENBOX_PROFILE=local\n", encoding="utf-8")
    (tmp_path / ".env.wuying-dev").write_text(
        "OPENBOX_PROFILE=dev\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(wuying_env, "BACKEND_DIR", tmp_path)
    monkeypatch.delenv("OPENBOX_ENV_FILE", raising=False)
    monkeypatch.delenv("OPENBOX_PROFILE", raising=False)

    assert wuying_env.environment_file() == tmp_path / ".env"
    assert wuying_env.environment_value("OPENBOX_PROFILE") == "local"


def test_explicit_profile_does_not_mix_values_from_default_env(tmp_path: Path, monkeypatch):
    explicit = tmp_path / "explicit.env"
    explicit.write_text("EXPLICIT_ONLY=yes\n", encoding="utf-8")
    (tmp_path / ".env").write_text("DEFAULT_ONLY=no\n", encoding="utf-8")
    monkeypatch.setattr(wuying_env, "BACKEND_DIR", tmp_path)
    monkeypatch.setenv("OPENBOX_ENV_FILE", str(explicit))
    monkeypatch.delenv("EXPLICIT_ONLY", raising=False)
    monkeypatch.delenv("DEFAULT_ONLY", raising=False)

    assert wuying_env.load_environment() == explicit
    assert wuying_env.environment_value("EXPLICIT_ONLY") == "yes"
    assert wuying_env.environment_value("DEFAULT_ONLY") == ""


def test_explicit_base_reuses_credentials_but_selected_dev_overrides_execution_plane(
    tmp_path: Path,
    monkeypatch,
):
    base = tmp_path / "base.env"
    base.write_text(
        "OPENBOX_API_KEY=base-model-key\n"
        "WUYING_DESKTOP_ID=production-desktop\n"
        "JWT_SECRET=production-jwt\n",
        encoding="utf-8",
    )
    dev = tmp_path / "dev.env"
    dev.write_text(
        "WUYING_DESKTOP_ID=development-desktop\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENBOX_BASE_ENV_FILE", str(base))
    monkeypatch.setenv("OPENBOX_ENV_FILE", str(dev))
    monkeypatch.setenv("JWT_SECRET", "")
    for key in ("OPENBOX_API_KEY", "WUYING_DESKTOP_ID"):
        monkeypatch.delenv(key, raising=False)

    assert wuying_env.load_environment() == dev
    assert wuying_env.environment_value("OPENBOX_API_KEY") == "base-model-key"
    assert wuying_env.environment_value("WUYING_DESKTOP_ID") == "development-desktop"
    assert wuying_env.environment_value("JWT_SECRET") == ""
