import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from orbwise import models as mdl
from orbwise.config import LLMConfig
from orbwise.llm_router import LLMRouter


def test_recommend_and_fit():
    assert mdl.recommend(16) == "qwen3:14b" and mdl.recommend(8) == "qwen3:8b"
    assert mdl.recommend(24) == "qwen3:30b" and mdl.recommend(0) == "qwen3:4b"
    p14 = mdl.BY_TAG["qwen3:14b"]
    assert mdl.fit(p14, 16) == "ok" and mdl.fit(p14, 9) == "tight" and mdl.fit(p14, 6) == "big"
    assert mdl.slug("qwen3:14b") == "qwen3-14b" and mdl.slug("hf.co/x/y:Q4_K_M") == "hf.co-x-y-q4-k-m"
    items = mdl.preset_list(16, {"qwen3:8b"})
    assert next(i for i in items if i["tag"] == "qwen3:8b")["installed"]
    assert [i["tag"] for i in items if i["recommended"]] == ["qwen3:14b"]


def test_interactive_choice():
    answers = iter(["", "3", "unsinn ! name", "my-model:7b"])

    def ask(prompt):
        return next(answers)

    out = []
    assert mdl.choose_interactive(8, ask=ask, out=out.append) == "qwen3:8b"
    assert mdl.choose_interactive(8, ask=ask, out=out.append) == mdl.PRESETS[2].tag
    assert mdl.choose_interactive(8, ask=ask, out=out.append) == "my-model:7b"


def test_added_models_become_profiles(tmp_path):
    state = tmp_path / "state.json"
    assert mdl.register_model(state, "qwen3:14b", activate=True) == "qwen3-14b"
    mdl.register_model(state, "llama3.1:8b")
    router = LLMRouter(LLMConfig(model="qwen3:8b"), state_path=state)
    assert {"standard", "qwen3-14b", "llama3.1-8b"} <= set(router.profiles)
    assert router.active == "qwen3-14b" and router.profile.model == "qwen3:14b" and router.profile.label == "Qwen 3 14B"
    assert mdl.unregister_model(state, "qwen3-14b") == "qwen3:14b"
    assert "active_profile" not in json.loads(state.read_text())
    assert mdl.unregister_model(state, "gibtsnicht") is None


class FakeOllama(BaseHTTPRequestHandler):
    pulled: list = []

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/tags":
            return self._json({"models": [{"name": "qwen3:8b"}]})
        self._json({}, 404)

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if self.path != "/api/pull":
            return self._json({})
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        if req["model"] == "gibts:nicht":
            self.wfile.write(b'{"error":"pull model manifest: file does not exist"}\n')
            return
        FakeOllama.pulled.append(req["model"])
        for line in ({"status": "pulling manifest"}, {"status": "downloading", "completed": 50, "total": 100},
                     {"status": "success"}):
            self.wfile.write((json.dumps(line) + "\n").encode())


@pytest.fixture
def ollama():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_pull_via_web_api(cfg, ollama, monkeypatch):
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    monkeypatch.delenv("ORBWISE_FAKE_LLM", raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    cfg.llm.base_url = ollama
    from orbwise.server import create_app
    with TestClient(create_app(cfg), base_url="http://localhost:8765") as client:
        presets = client.get("/api/models/presets").json()
        assert any(p["tag"] == "qwen3:8b" and p["installed"] for p in presets["presets"])
        with client.websocket_connect("ws://localhost:8765/ws", headers={"Origin": "http://localhost:8765"}) as ws:
            ws.receive_json()
            assert client.post("/api/models/pull", json={"tag": "qwen3:14b"}).status_code == 200
            events = []
            while True:
                ev = ws.receive_json()
                if ev["type"] == "model_pull":
                    events.append(ev)
                    if ev.get("done") or ev.get("error"):
                        break
            assert events[-1] == {"type": "model_pull", "tag": "qwen3:14b", "done": True, "profile": "qwen3-14b"}
            assert any(e.get("status") for e in events[:-1])  # Fortschritt (gedrosselt auf ~2 pro Sekunde)
            names = [p["name"] for p in client.get("/api/models").json()["profiles"]]
            assert "qwen3-14b" in names
            client.post("/api/models/pull", json={"tag": "gibts:nicht"})
            while True:
                ev = ws.receive_json()
                if ev["type"] == "model_pull" and (ev.get("done") or ev.get("error")):
                    break
            assert "does not exist" in ev["error"]
        assert client.post("/api/models/pull", json={"tag": "; rm -rf /"}).status_code == 400
    assert FakeOllama.pulled == ["qwen3:14b"]
