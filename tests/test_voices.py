import json
import os

import httpx
import pytest
from conftest import run
from fastapi.testclient import TestClient

from orbwise.voice import catalog
from orbwise.voice.tts import PiperTTS

REAL_FETCH = catalog.fetch_catalog


SAMPLE = {  # Auszug im Format von piper-voices/voices.json
    "de_DE-thorsten-high": {"language": {"code": "de_DE"}, "quality": "high", "num_speakers": 1,
                            "files": {"x.onnx": {"size_bytes": 114_000_000}, "x.json": {"size_bytes": 5000}}},
    "de_DE-eva_k-x_low": {"language": {"code": "de_DE"}, "quality": "x_low", "num_speakers": 1,
                          "files": {"x.onnx": {"size_bytes": 20_600_000}}},
    "de_DE-mls-medium": {"language": {"code": "de_DE"}, "quality": "medium", "num_speakers": 236,
                         "files": {"x.onnx": {"size_bytes": 77_000_000}}},
    "en_GB-cori-high": {"language": {"code": "en_GB"}, "quality": "high", "num_speakers": 1,
                        "files": {"x.onnx": {"size_bytes": 114_000_000}}},
}


@pytest.fixture(autouse=True)
def offline_catalog(monkeypatch):
    """Kein Netz in Tests: der Katalog kommt aus SAMPLE (einzelne Tests prüfen fetch_catalog selbst)."""
    async def fake(cache_dir, transport=None):
        return SAMPLE
    monkeypatch.setattr(catalog, "fetch_catalog", fake)
    monkeypatch.setattr(catalog, "_last_failure", 0.0)


def test_catalog_urls():
    v = catalog.BY_NAME["de_DE-thorsten-high"]
    assert v.urls()[0] == ("https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/"
                           "thorsten/high/de_DE-thorsten-high.onnx")
    assert catalog.BY_NAME["de_DE-thorsten_emotional-medium"].urls()[1].endswith(
        "/thorsten_emotional/medium/de_DE-thorsten_emotional-medium.onnx.json")


def test_voice_list_marks_installed(tmp_path):
    (tmp_path / "de_DE-pavoque-low.onnx").write_bytes(b"x")
    (tmp_path / "eigene-stimme.onnx").write_bytes(b"x")
    items = {v["name"]: v for v in catalog.voice_list(tmp_path, "de_DE-pavoque-low")}
    assert items["de_DE-pavoque-low"]["installed"] and items["de_DE-pavoque-low"]["current"]
    assert not items["de_DE-thorsten-high"]["installed"]
    assert items["eigene-stimme"]["description"] == "Eigene Stimme"


def test_install_downloads_both_files(tmp_path):
    seen = []

    def handler(req):
        seen.append(str(req.url))
        return httpx.Response(200, content=b"MODEL" if req.url.path.endswith(".onnx") else b"{}")

    run(catalog.install_voice("de_DE-karlsson-low", tmp_path, httpx.MockTransport(handler)))
    assert (tmp_path / "de_DE-karlsson-low.onnx").read_bytes() == b"MODEL"
    assert (tmp_path / "de_DE-karlsson-low.onnx.json").exists()
    assert len(seen) == 2 and not list(tmp_path.glob("*.part"))
    with pytest.raises(ValueError):
        run(catalog.install_voice("../evil", tmp_path))


def test_piper_select_rate_and_speaker(cfg, tmp_path):
    cfg.voice.tts_voice = tmp_path / "de_DE-thorsten-high.onnx"
    tts = PiperTTS(cfg.voice)
    assert not tts.available()
    (tmp_path / "de_DE-thorsten_emotional-medium.onnx").write_bytes(b"x")
    (tmp_path / "de_DE-thorsten_emotional-medium.onnx.json").write_text(
        json.dumps({"speaker_id_map": {"amused": 0, "neutral": 4}}))
    assert tts.available() and tts.current == "de_DE-thorsten_emotional-medium"  # Fallback auf installierte
    assert not tts.select("gibt-es-nicht")
    assert tts._speaker_id("de_DE-thorsten_emotional-medium") == 4
    tts.set_rate(0.5)
    assert tts.rate == 0.8


@pytest.fixture
def client(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    cfg.voice.enabled = True
    cfg.voice.tts_voice = tmp_path / "voices" / "de_DE-thorsten-high.onnx"
    from orbwise.server import create_app
    return TestClient(create_app(cfg), base_url="http://localhost:8765")


def test_voice_endpoints(client, monkeypatch):
    data = client.get("/api/voices").json()
    assert data["available"] and any(v["name"] == "de_DE-pavoque-low" for v in data["voices"])
    assert client.get("/api/voices/de_DE-pavoque-low/preview").status_code == 404

    installed = []

    async def fake_install(name, voices_dir, transport=None, extra=None):
        installed.append(name)
        (voices_dir).mkdir(parents=True, exist_ok=True)
        (voices_dir / f"{name}.onnx").write_bytes(b"x")

    monkeypatch.setattr(catalog, "install_voice", fake_install)
    assert client.post("/api/voices/de_DE-pavoque-low/install").status_code == 200
    assert installed == ["de_DE-pavoque-low"]
    assert client.post("/api/voices/unbekannt/install").status_code == 404
    names = {v["name"]: v for v in client.get("/api/voices").json()["voices"]}
    assert names["de_DE-pavoque-low"]["installed"]


def test_foreign_origin_cannot_post(client):
    r = client.post("/api/voices/de_DE-pavoque-low/install", headers={"Origin": "http://evil.example"})
    assert r.status_code == 403


def test_delete_voice(client, tmp_path):
    voices = tmp_path / "voices"
    voices.mkdir(parents=True, exist_ok=True)
    for name in ("de_DE-thorsten-high", "de_DE-pavoque-low"):
        (voices / f"{name}.onnx").write_bytes(b"x" * 2_000_000)
        (voices / f"{name}.onnx.json").write_text("{}")
    data = client.get("/api/voices").json()
    listed = {v["name"]: v for v in data["voices"]}
    assert data["current"] == "de_DE-thorsten-high" and listed["de_DE-pavoque-low"]["size_mb"] == 2
    assert client.delete("/api/voices/de_DE-thorsten-high").status_code == 409  # aktive Stimme
    assert client.delete("/api/voices/gibtsnicht").status_code == 404
    assert client.delete("/api/voices/..%2F..%2Fetc%2Fpasswd").status_code == 404
    assert client.delete("/api/voices/de_DE-pavoque-low").json() == {"ok": True}
    assert not (voices / "de_DE-pavoque-low.onnx").exists() and not (voices / "de_DE-pavoque-low.onnx.json").exists()
    assert (voices / "de_DE-thorsten-high.onnx").exists()
    assert client.delete("/api/voices/de_DE-pavoque-low", headers={"Origin": "http://evil.example"}).status_code == 403


def test_fetch_catalog_caches_and_works_offline(tmp_path, monkeypatch):
    calls = []

    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(200, json=SAMPLE)

    ok = httpx.MockTransport(handler)
    assert run(REAL_FETCH(tmp_path, ok)) == SAMPLE and (tmp_path / catalog.CATALOG_FILE).exists()
    assert run(REAL_FETCH(tmp_path, ok)) == SAMPLE and len(calls) == 1  # 24 h aus dem Cache
    assert calls == ["/rhasspy/piper-voices/resolve/main/voices.json"]

    def down(req):
        raise httpx.ConnectError("offline")

    old = (tmp_path / catalog.CATALOG_FILE)
    stale = catalog.time.time() - 2 * catalog.CATALOG_MAX_AGE
    os.utime(old, (stale, stale))  # Cache veraltet → neu laden scheitert → alte Kopie
    assert run(REAL_FETCH(tmp_path, httpx.MockTransport(down))) == SAMPLE
    assert run(REAL_FETCH(tmp_path / "leer", httpx.MockTransport(down))) == {}  # kurz danach: kein neuer Versuch


def test_voice_list_with_full_catalog(tmp_path):
    (tmp_path / "de_DE-eva_k-x_low.onnx").write_bytes(b"x")
    items = catalog.voice_list(tmp_path, "de_DE-eva_k-x_low", "de", SAMPLE)
    names = [v["name"] for v in items]
    assert names[:7] == [v.name for v in catalog.CATALOG if v.language == "de"]  # Auswahl zuerst
    assert items[0]["recommended"] and items[0]["download_mb"] == 114
    eva = next(v for v in items if v["name"] == "de_DE-eva_k-x_low")
    assert eva["label"] == "Eva K" and eva["description"] == "de_DE · Qualität x_low" and not eva["recommended"]
    assert eva["installed"] and eva["current"] and eva["download_mb"] == 21
    mls = next(v for v in items if v["name"] == "de_DE-mls-medium")
    assert "236 Sprecher" in mls["description"]
    assert "en_GB-cori-high" not in names  # andere Sprache
    assert names.count("de_DE-eva_k-x_low") == 1  # nicht zusätzlich als „Eigene Stimme“


def test_install_voice_from_full_catalog(tmp_path):
    seen = []

    def handler(req):
        seen.append(req.url.path)
        return httpx.Response(200, content=b"M")

    run(catalog.install_voice("de_DE-eva_k-x_low", tmp_path, httpx.MockTransport(handler), extra=SAMPLE))
    assert seen[0] == "/rhasspy/piper-voices/resolve/main/de/de_DE/eva_k/x_low/de_DE-eva_k-x_low.onnx"
    assert (tmp_path / "de_DE-eva_k-x_low.onnx.json").exists()
    with pytest.raises(ValueError):  # nicht im Katalog
        run(catalog.install_voice("de_DE-fremd-low", tmp_path, httpx.MockTransport(handler), extra=SAMPLE))


def test_install_endpoint_checks_catalog(client, monkeypatch):
    installed = []

    async def fake_install(name, voices_dir, transport=None, extra=None):
        installed.append(name)

    monkeypatch.setattr(catalog, "install_voice", fake_install)
    assert client.post("/api/voices/de_DE-eva_k-x_low/install").status_code == 200
    assert client.post("/api/voices/de_DE-fremd-low/install").status_code == 404
    assert installed == ["de_DE-eva_k-x_low"]
    names = [v["name"] for v in client.get("/api/voices").json()["voices"]]
    assert "de_DE-eva_k-x_low" in names and "en_GB-cori-high" not in names
