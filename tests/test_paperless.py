import httpx
import pytest
from conftest import run
from fake_paperless import TOKEN, FakePaperless

from orbwise.tools import paperless as pl
from orbwise.tools.registry import CONFIRM, SAFE, ToolContext, get_tool, load_all_tools, tool_schemas

TOOLS = {"paperless_search", "paperless_ask", "paperless_read", "paperless_open"}


@pytest.fixture
def fake(monkeypatch, cfg):
    f = FakePaperless()
    monkeypatch.setattr(pl, "TRANSPORT", f.transport())
    monkeypatch.setattr(pl, "_KNOWN", {})
    cfg.paperless.url = "http://paperless.local:8000"
    cfg.paperless.token = TOKEN
    return f


def ctx(cfg, memory=None):
    return ToolContext(cfg=cfg, memory=memory)


def names(schemas):
    return {s["function"]["name"] for s in schemas}


def test_tools_only_when_configured_and_read_only(cfg, monkeypatch):
    monkeypatch.delenv("ORBWISE_PAPERLESS_TOKEN", raising=False)
    load_all_tools()
    assert not TOOLS & names(tool_schemas(cfg))
    cfg.paperless.url, cfg.paperless.token = "http://x", "t"
    assert TOOLS <= names(tool_schemas(cfg))
    assert all(get_tool(t).risk == SAFE for t in TOOLS)
    assert {"paperless_suggest_metadata", "paperless_apply_metadata"} <= names(tool_schemas(cfg))
    assert get_tool("paperless_suggest_metadata").risk == SAFE
    cfg.tools.disabled = ["paperless_apply_metadata"]  # wieder nur lesend
    assert "paperless_apply_metadata" not in names(tool_schemas(cfg)) and TOOLS <= names(tool_schemas(cfg))


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
    target = tmp_path / "orbwise" / "paperless" / "7-Handyvertrag Telekom.pdf"
    assert target.read_bytes() == b"%PDF-1.7 archiv" and "Geöffnet" in out
    assert launched == [["xdg-open", str(target)]]
    n = len(fake.requests)
    run(pl.paperless_open(ctx(cfg), 7))  # zweites Mal aus dem Cache
    assert not any("download" in str(r.url) for r in fake.requests[n:])
    run(pl.paperless_open(ctx(cfg), 8, original=True))
    assert (tmp_path / "orbwise" / "paperless" / "8-Stromrechnung 2026.jpg").read_bytes() == b"ORIGINAL"


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
    assert "Paperless unter http://paperless.local:8000" in out and "orbwise doctor" in out


def test_split_passages_overlap_and_coverage():
    text = ("Satz eins ist hier. " * 300).strip()
    parts = pl.split_passages(text, 500)
    assert all(len(p) <= 500 for p in parts) and len(parts) > 10
    assert parts[-1].endswith("hier.")


def test_url_normalization():
    from orbwise.tools.netutil import normalize_url
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
    from orbwise.tools.netutil import verify_arg
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


# ---------------------------------------------------------------- Metadaten vorschlagen/übernehmen

def test_apply_schema_has_object_items():
    load_all_tools()
    props = get_tool("paperless_apply_metadata").parameters["properties"]
    assert props["changes"] == {"type": "array", "items": {"type": "object"}, "description": props["changes"]["description"]}
    assert get_tool("paperless_suggest_metadata").parameters["properties"]["document_ids"]["items"] == {"type": "integer"}


def test_suggest_shows_state_paperless_hints_and_known_names(cfg, fake):
    out = run(pl.paperless_suggest_metadata(ctx(cfg), [8, 7, 99]))
    assert "[8] Stromrechnung 2026" in out
    assert "Paperless schlägt vor – Korrespondent: Stadtwerke · Typ: Rechnung · Tags: Steuer · Daten im Text: 2026-09-01" in out
    assert "84,20 EUR" in out and "[99] ✘" in out
    assert "Vorhandene Korrespondenten (2): Stadtwerke, Telekom" in out and "Vorhandene Tags (3): Posteingang, Steuer, Vertrag" in out
    assert "paperless_apply_metadata" in out
    cfg.paperless.max_chars = 1000  # Budget wird aufgeteilt
    long = run(pl.paperless_suggest_metadata(ctx(cfg), "7"))
    assert "…“" in long and len(long) < 2500
    fake.suggestions = {}  # alte Version ohne Vorschlags-Endpunkt
    assert "schlägt vor" not in run(pl.paperless_suggest_metadata(ctx(cfg), [7]))
    assert "IDs" in run(pl.paperless_suggest_metadata(ctx(cfg), []))
    many = run(pl.paperless_suggest_metadata(ctx(cfg), list(range(1, 14))))
    assert "Nur die ersten 10" in many


def test_apply_existing_names_tags_title_and_date(cfg, fake):
    changes = [{"document_id": 7, "correspondent": "telekom", "document_type": "Rechnung", "add_tags": ["steuer"],
                "remove_tags": ["Vertrag"], "title": "Handyvertrag Telekom 2025", "created": "2025-03-02"}]
    out = run(pl.paperless_apply_metadata(ctx(cfg), changes))
    assert out.startswith("✔ Dok 7 „Handyvertrag Telekom 2025“") and "Neu angelegt" not in out
    doc_id, body = fake.patches[-1]
    assert doc_id == 7 and body == {"title": "Handyvertrag Telekom 2025", "created": "2025-03-02",
                                    "document_type": 21, "tags": [11]}  # Korrespondent war schon Telekom
    assert "schon so eingetragen" in run(pl.paperless_apply_metadata(ctx(cfg), changes))


def test_apply_creates_missing_entries_and_handles_batch_errors(cfg, fake):
    changes = [{"document_id": 8, "correspondent": "Vodafone", "add_tags": ["Handy", "Steuer"]},
               {"document_id": 99, "title": "gibt es nicht"},
               {"document_id": 7, "created": "1.3.2025"}]
    out = run(pl.paperless_apply_metadata(ctx(cfg), changes))
    lines = out.splitlines()
    assert lines[0].startswith("✔ Dok 8") and "Kein Dokument mit der ID 99" in lines[1]
    assert "YYYY-MM-DD" in lines[2] and "nichts geändert" in lines[2]
    assert lines[3] == "Neu angelegt: Korrespondent „Vodafone“, Tag „Handy“"
    new_corr = next(c for c in fake.correspondents if c["name"] == "Vodafone")
    new_tag = next(t for t in fake.tags if t["name"] == "Handy")
    assert fake.patches == [(8, {"correspondent": new_corr["id"], "tags": [11, new_tag["id"]]})]


def test_apply_accepts_json_text_and_legacy_date_field(cfg, fake):
    fake.legacy_dates = True
    out = run(pl.paperless_apply_metadata(ctx(cfg), '[{"document_id": 8, "created": "2026-09-02"}]'))
    assert out.startswith("✔ Dok 8") and fake.patches[-1] == (8, {"created_date": "2026-09-02"})
    assert "Keine Änderungen" in run(pl.paperless_apply_metadata(ctx(cfg), "[]"))


def test_apply_confirmation_lists_changes_and_new_entries(cfg, fake):
    load_all_tools()
    spec = get_tool("paperless_apply_metadata")
    args = {"changes": [{"document_id": 7, "correspondent": "Telekom Deutschland", "add_tags": ["Vertrag", "Handy"],
                         "remove_tags": ["Posteingang"]},
                        {"document_id": 8, "document_type": "Rechnung", "title": "Strom 09/2026"}]}
    risk, reason = spec.assess(ctx(cfg), args)
    assert risk == CONFIRM and "noch nicht geprüft" in reason and "NEU" not in reason  # Namen noch nicht geladen
    run(pl.paperless_suggest_metadata(ctx(cfg), [7]))  # lädt die vorhandenen Namen
    risk, reason = spec.assess(ctx(cfg), args)
    assert "Paperless-Metadaten ändern (2 Dokumente)" in reason
    assert "Dok 7: Korrespondent → Telekom Deutschland · +Tag Vertrag · +Tag Handy · −Tag Posteingang" in reason
    assert "Dok 8: Titel → „Strom 09/2026“ · Typ → Rechnung" in reason
    assert "NEU anlegen: Korrespondent „Telekom Deutschland“ (ähnlich vorhanden: „Telekom“); Tag „Handy“" in reason
    assert "Rechnung“" not in reason.split("NEU")[1] and "„Vertrag“" not in reason.split("NEU")[1]
