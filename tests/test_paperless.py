import httpx
import pytest
from conftest import run
from fake_paperless import TOKEN, FakePaperless

from jarvis.tools import paperless as pl
from jarvis.tools.registry import SAFE, ToolContext, get_tool, load_all_tools, tool_schemas

TOOLS = {"paperless_search", "paperless_ask", "paperless_read", "paperless_open"}


@pytest.fixture
def fake(monkeypatch, cfg):
    f = FakePaperless()
    monkeypatch.setattr(pl, "TRANSPORT", f.transport())
    cfg.paperless.url = "http://paperless.local:8000"
    cfg.paperless.token = TOKEN
    return f


def ctx(cfg, memory=None):
    return ToolContext(cfg=cfg, memory=memory)


def names(schemas):
    return {s["function"]["name"] for s in schemas}


def test_tools_only_when_configured_and_read_only(cfg, monkeypatch):
    monkeypatch.delenv("JARVIS_PAPERLESS_TOKEN", raising=False)
    load_all_tools()
    assert not TOOLS & names(tool_schemas(cfg))
    cfg.paperless.url, cfg.paperless.token = "http://x", "t"
    assert TOOLS <= names(tool_schemas(cfg))
    assert all(get_tool(t).risk == SAFE for t in TOOLS)


def test_search_with_names_highlights_and_filters(cfg, fake):
    out = run(pl.paperless_search(ctx(cfg), "Handyvertrag"))
    assert "[7] Handyvertrag Telekom · 2025-03-01 · von Telekom · Vertrag · Tags: Vertrag" in out
    assert "Handyvertrag …" in out and "<span" not in out
    req = fake.requests[0]
    assert req.url.params["query"] == "Handyvertrag" and req.headers["Authorization"] == f"Token {TOKEN}"
    out = run(pl.paperless_search(ctx(cfg), correspondent="stadtwerke"))
    assert "[8] Stromrechnung 2026" in out and "[7]" not in out
    doc_req = [r for r in fake.requests if r.url.path == "/api/documents/"][-1]
    assert doc_req.url.params["ordering"] == "-created" and "query" not in doc_req.url.params
    assert "YYYY-MM-DD" in run(pl.paperless_search(ctx(cfg), date_from="1.1.2026"))
    assert "Keine passenden" in run(pl.paperless_search(ctx(cfg), "xyzunbekannt"))


def test_ask_short_document_returns_full_text(cfg, fake):
    out = run(pl.paperless_ask(ctx(cfg), 8, "Wie hoch ist der Betrag?"))
    assert "vollständiger Text" in out and "84,20 EUR" in out


def test_ask_long_document_returns_relevant_passage(cfg, fake, memory):
    cfg.paperless.max_chars = 2500
    out = run(pl.paperless_ask(ctx(cfg, memory), 7, "Wann endet die Mindestvertragslaufzeit und wie kündige ich?"))
    assert "28.02.2027" in out and "relevantesten" in out
    assert len(out) < 4000


def test_ask_works_without_embeddings(cfg, fake):
    cfg.paperless.max_chars = 2500
    out = run(pl.paperless_ask(ctx(cfg), 7, "Kündigung Frist Laufzeitende"))
    assert "Laufzeitende" in out


def test_read_paginates(cfg, fake):
    cfg.paperless.max_chars = 1000
    out = run(pl.paperless_read(ctx(cfg), 7))
    assert "weiter mit offset=1000" in out
    assert "weiter mit offset" in run(pl.paperless_read(ctx(cfg), 7, 1000))


def test_open_downloads_to_cache_and_opens(cfg, fake, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    launched = []

    async def fake_launch(argv, wait=2.0):
        launched.append(argv)
        return True, ""

    monkeypatch.setattr(pl.proc, "launch", fake_launch)
    monkeypatch.setattr(pl.shutil, "which", lambda c: "/usr/bin/" + c)
    out = run(pl.paperless_open(ctx(cfg), 7))
    target = tmp_path / "jarvis" / "paperless" / "7-Handyvertrag Telekom.pdf"
    assert target.read_bytes() == b"%PDF-1.7 archiv" and "Geöffnet" in out
    assert launched == [["xdg-open", str(target)]]
    n = len(fake.requests)
    run(pl.paperless_open(ctx(cfg), 7))  # zweites Mal aus dem Cache
    assert not any("download" in str(r.url) for r in fake.requests[n:])
    run(pl.paperless_open(ctx(cfg), 8, original=True))
    assert (tmp_path / "jarvis" / "paperless" / "8-Stromrechnung 2026.jpg").read_bytes() == b"ORIGINAL"


def test_errors(cfg, fake):
    assert "Kein Dokument mit der ID 99" in run(pl.paperless_ask(ctx(cfg), 99, "?"))
    cfg.paperless.token = "falsch"
    assert "Token ist ungültig" in run(pl.paperless_search(ctx(cfg), "x"))
    st = run(pl.paperless_status(cfg))
    assert not st["online"] and "Token" in st["error"]


def test_status_and_unreachable(cfg, fake, monkeypatch):
    st = run(pl.paperless_status(cfg))
    assert st == {"enabled": True, "online": True, "count": 2, "version": "2.17.1",
                  "url": "http://paperless.local:8000"}

    def boom(request):
        raise httpx.ConnectError("nope")

    import httpx
    monkeypatch.setattr(pl, "TRANSPORT", httpx.MockTransport(boom))
    out = run(pl.paperless_search(ctx(cfg), "x"))
    assert "Paperless unter http://paperless.local:8000" in out and "jarvis doctor" in out


def test_split_passages_overlap_and_coverage():
    text = ("Satz eins ist hier. " * 300).strip()
    parts = pl.split_passages(text, 500)
    assert all(len(p) <= 500 for p in parts) and len(parts) > 10
    assert parts[-1].endswith("hier.")


def test_url_normalization():
    from jarvis.tools.netutil import normalize_url
    assert normalize_url("https:///192.168.1.20:8444") == "https://192.168.1.20:8444"
    assert normalize_url("https://nas:8444/api/", "/api") == "https://nas:8444"
    assert normalize_url("nas.local:8000") == "http://nas.local:8000"
    assert normalize_url("HTTP:/nas:1/") == "HTTP://nas:1"
    assert normalize_url("") == ""


def test_client_uses_normalized_url_no_proxy_and_verify_option(cfg, monkeypatch):
    import ssl
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    cfg.paperless.url, cfg.paperless.token = "https:///192.168.1.20:8444/", "t"
    pc = pl.PaperlessClient(cfg)
    assert str(pc.client.base_url) == "https://192.168.1.20:8444/api/"
    assert pc.client._trust_env is False
    cfg.paperless.verify_ssl = False
    from jarvis.tools.netutil import verify_arg
    assert verify_arg(False) is False and verify_arg(True) is True
    with pytest.raises((FileNotFoundError, ssl.SSLError, OSError)):
        verify_arg("/gibt/es/nicht.pem")
    run(pc.client.aclose())


@pytest.mark.parametrize("exc,expected", [
    (httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed certificate"),
     "verify_ssl: false"),
    (httpx.ConnectError("[SSL: WRONG_VERSION_NUMBER] wrong version number"), "http:// statt https://"),
    (httpx.ConnectError("[Errno -2] Name or service not known"), "Hostname nicht auflösbar"),
    (httpx.ConnectError("[Errno 111] Connection refused"), "stimmt der Port"),
    (httpx.ReadTimeout("timed out"), "Zeitüberschreitung"),
    (httpx.ConnectError("All connection attempts failed"), "stimmt der Port"),
    (httpx.ReadError(""), "mit https:// eintragen"),
])
def test_error_explanations(cfg, fake, monkeypatch, exc, expected):
    calls = []

    def boom(request):
        calls.append(request)
        raise exc

    monkeypatch.setattr(pl, "TRANSPORT", httpx.MockTransport(boom))
    out = run(pl.paperless_search(ctx(cfg), "x"))
    assert expected in out and "http://paperless.local:8000" in out


def test_retry_once_on_connect_error(cfg, fake, monkeypatch):
    handler = fake.transport().handler
    state = {"n": 0}

    def flaky(request):
        state["n"] += 1
        if state["n"] == 1:
            raise httpx.ConnectError("connection reset")
        return handler(request)

    monkeypatch.setattr(pl, "TRANSPORT", httpx.MockTransport(flaky))
    assert "[7] Handyvertrag" in run(pl.paperless_search(ctx(cfg), "Handyvertrag"))


def test_html_login_page_instead_of_api(cfg, fake, monkeypatch):
    monkeypatch.setattr(pl, "TRANSPORT", httpx.MockTransport(
        lambda r: httpx.Response(200, text="<html>Login</html>", headers={"content-type": "text/html"})))
    assert "Webseite statt der Paperless-API" in run(pl.paperless_search(ctx(cfg), "x"))
