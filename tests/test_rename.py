"""Umbenennung Jarvis → Orbwise: alte Installationen, Umgebungsvariablen und Config-Werte funktionieren weiter."""

import os
import subprocess
from pathlib import Path

from orbwise import migrate
from orbwise.config import Config, env

ROOT = Path(__file__).resolve().parents[1]
OLD_LAUNCHER = '#!/usr/bin/env bash\n# JARVIS launcher\nJARVIS_HOME="${JARVIS_HOME:-/x}"\nexec "$JARVIS_HOME/.venv/bin/jarvis" "$@"\n'


def old_install(home: Path) -> None:
    (home / ".config/jarvis").mkdir(parents=True)
    (home / ".config/jarvis/config.yaml").write_text("memory:\n  dir: ~/.local/share/jarvis/memory\nlanguage: de\n")
    (home / ".local/share/jarvis/memory").mkdir(parents=True)
    (home / ".local/share/jarvis/memory/facts.md").write_text("# Fakten\n- NAS unter 10.0.0.5\n")
    (home / ".local/bin").mkdir(parents=True)
    (home / ".local/bin/jarvis").write_text(OLD_LAUNCHER)
    (home / ".local/bin/jarvis-open").write_text('#!/bin/bash\n# Starts JARVIS\n"$HOME/.local/bin/jarvis" serve\n')
    (home / ".local/share/applications").mkdir(parents=True)
    (home / ".local/share/applications/jarvis.desktop").write_text(f"Exec={home}/.local/bin/jarvis-open\n")
    (home / ".config/autostart").mkdir(parents=True)
    (home / ".config/autostart/jarvis.desktop").write_text(f"Exec=sh -c '{home}/.local/bin/jarvis serve'\n")


def test_migrates_old_installation(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    old_install(tmp_path)
    done = migrate.migrate(home=tmp_path, root=ROOT)
    assert len(done) >= 5
    new_conf, new_data = tmp_path / ".config/orbwise", tmp_path / ".local/share/orbwise"
    assert (new_data / "memory/facts.md").read_text().endswith("10.0.0.5\n")
    assert "~/.local/share/orbwise/memory" in (new_conf / "config.yaml").read_text()
    assert (tmp_path / ".config/jarvis").is_symlink() and (tmp_path / ".local/share/jarvis/memory/facts.md").exists()
    launcher = (tmp_path / ".local/bin/orbwise").read_text()
    assert f'ORBWISE_HOME="${{ORBWISE_HOME:-{ROOT}}}"' in launcher and "@" not in launcher.split("Reinstall")[0]
    assert "exec \"$HOME/.local/bin/orbwise\"" in (tmp_path / ".local/bin/jarvis").read_text()
    assert "orbwise-open" in (tmp_path / ".local/bin/jarvis-open").read_text()
    assert (tmp_path / ".local/bin/orbwise-open").exists()
    assert not (tmp_path / ".local/share/applications/jarvis.desktop").exists()
    assert "orbwise-open" in (tmp_path / ".local/share/applications/orbwise.desktop").read_text()
    assert str(tmp_path) in (tmp_path / ".config/autostart/orbwise.desktop").read_text()
    assert migrate.migrate(home=tmp_path, root=ROOT) == []  # zweiter Lauf ändert nichts


def test_shim_forwards_to_new_command(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    old_install(tmp_path)
    migrate.migrate(home=tmp_path, root=ROOT)
    (tmp_path / ".local/bin/orbwise").write_text('#!/bin/bash\necho "orbwise $* alias=$ORBWISE_VIA_ALIAS"\n')
    out = subprocess.run([str(tmp_path / ".local/bin/jarvis"), "doctor"], capture_output=True, text=True,
                         env={**os.environ, "HOME": str(tmp_path)}).stdout
    assert out.strip() == "orbwise doctor alias=1"


def test_existing_new_folder_is_not_overwritten(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    old_install(tmp_path)
    (tmp_path / ".config/orbwise").mkdir()
    (tmp_path / ".config/orbwise/config.yaml").write_text("language: en\n")
    done = migrate.migrate(home=tmp_path, root=ROOT)
    assert any("existiert schon" in d for d in done)
    assert (tmp_path / ".config/orbwise/config.yaml").read_text() == "language: en\n"
    assert (tmp_path / ".config/jarvis/config.yaml").exists() and not (tmp_path / ".config/jarvis").is_symlink()


def test_foreign_files_are_left_alone(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    (tmp_path / ".local/bin").mkdir(parents=True)
    (tmp_path / ".local/bin/jarvis").write_text("#!/bin/sh\necho some other tool\n")
    assert migrate.migrate(home=tmp_path, root=ROOT) == []
    assert "other tool" in (tmp_path / ".local/bin/jarvis").read_text()


def test_old_env_names_and_privilege_value(monkeypatch):
    monkeypatch.delenv("ORBWISE_PAPERLESS_TOKEN", raising=False)
    monkeypatch.setenv("JARVIS_PAPERLESS_TOKEN", "alt")
    assert env("PAPERLESS_TOKEN") == "alt" and Config().paperless.api_token == "alt"
    monkeypatch.setenv("ORBWISE_PAPERLESS_TOKEN", "neu")
    assert Config().paperless.api_token == "neu"
    for old in ("jarvis", "orbwise", "dashboard"):
        assert Config.model_validate({"tools": {"privilege_cmd": old}}).tools.privilege_cmd == "dashboard"
    assert Config.model_validate({"tools": {"privilege_cmd": "pkexec"}}).tools.privilege_cmd == "pkexec"
    assert Config().assistant_name == "Jarvis"  # die Persona bleibt


def test_both_commands_are_installed():
    import tomllib
    scripts = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["scripts"]
    assert scripts["orbwise"] == scripts["jarvis"] == "orbwise.cli:main"
