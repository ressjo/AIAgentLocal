import pytest
from conftest import run

from orbwise.tools import packages as pk
from orbwise.tools.registry import ToolContext


@pytest.fixture
def calls(monkeypatch):
    seen = []
    replies = {}

    async def fake_run(ctx, cmd, timeout, stream=True, cwd=None):
        seen.append(cmd)
        key = " ".join(cmd) if isinstance(cmd, list) else cmd
        for prefix, reply in replies.items():
            if key.startswith(prefix):
                return reply
        return 0, "ok"

    monkeypatch.setattr(pk.proc, "run", fake_run)
    return seen, replies


def ctx(cfg, pm):
    cfg.tools.package_manager = pm
    cfg.tools.aur_helper = "none"
    return ToolContext(cfg=cfg, memory=None)


def test_detects_package_manager(cfg, monkeypatch):
    c = ToolContext(cfg=cfg, memory=None)
    monkeypatch.setattr(pk.shutil, "which", lambda n: "/usr/bin/apt-get" if n == "apt-get" else None)
    assert pk.package_manager(c) == "apt"
    monkeypatch.setattr(pk.shutil, "which", lambda n: "/usr/bin/pacman" if n == "pacman" else None)
    assert pk.package_manager(c) == "pacman"


def test_apt_update_install_remove(cfg, calls):
    seen, replies = calls
    c = ctx(cfg, "apt")
    replies["apt-cache show nichtda"] = (100, "")
    run(pk.system_update(c))
    assert seen[-1] == ["sudo", "-A", "env", "DEBIAN_FRONTEND=noninteractive", "sh", "-c",
                        "apt-get update && apt-get -y upgrade"]
    out = run(pk.install_package(c, "htop nichtda"))
    assert ["sudo", "-A", "env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "-y", "install", "htop"] in seen
    assert "Nicht gefunden: nichtda" in out
    run(pk.remove_package(c, "htop"))
    assert seen[-1][-4:] == ["-y", "remove", "--autoremove", "htop"]
    replies["apt list"] = (0, "Listing... Done\nfirefox/noble-updates 131.0 amd64 [upgradable from: 130.0]\n")
    assert run(pk.list_updates(c)).startswith("firefox/noble-updates")
    run(pk.search_package(c, "editor"))
    assert seen[-1] == ["apt-cache", "search", "--", "editor"]


def test_pacman_unchanged(cfg, calls):
    seen, _ = calls
    c = ctx(cfg, "pacman")
    run(pk.system_update(c))
    assert seen[0] == ["sudo", "-A", "pacman", "-Syu", "--noconfirm"]
    run(pk.remove_package(c, "htop"))
    assert seen[-1] == ["sudo", "-A", "pacman", "-Rns", "--noconfirm", "htop"]
