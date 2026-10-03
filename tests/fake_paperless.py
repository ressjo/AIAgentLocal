"""In-Memory-Nachbildung der Paperless-ngx-REST-API für Tests (httpx.MockTransport)."""

import json
import re

import httpx

TOKEN = "pl-test-token"

VERTRAG = ("Mobilfunkvertrag Telekom\n\nVertragsbeginn: 01.03.2025\n\n" + "Allgemeine Bedingungen. " * 150 +
           "\n\nDie Mindestvertragslaufzeit beträgt 24 Monate und endet am 28.02.2027. Eine Kündigung ist mit einer "
           "Frist von einem Monat zum Laufzeitende möglich.\n\n" + "Datenschutzhinweise und Rechtliches. " * 150)


class FakePaperless:
    def __init__(self):
        self.correspondents = [{"id": 1, "name": "Telekom"}, {"id": 2, "name": "Stadtwerke"}]
        self.tags = [{"id": 10, "name": "Vertrag"}, {"id": 11, "name": "Steuer"},
                     {"id": 12, "name": "Posteingang", "is_inbox_tag": True}]
        self.types = [{"id": 20, "name": "Vertrag"}, {"id": 21, "name": "Rechnung"}]
        self.docs = [
            {"id": 7, "title": "Handyvertrag Telekom", "created": "2025-03-01", "correspondent": 1,
             "document_type": 20, "tags": [10], "content": VERTRAG, "original_file_name": "scan_007.pdf"},
            {"id": 8, "title": "Stromrechnung 2026", "created": "2026-09-01", "correspondent": 2,
             "document_type": 21, "tags": [11], "content": "Rechnung Strom. Betrag: 84,20 EUR. Fällig am 15.09.2026.",
             "original_file_name": "strom.jpg"},
        ]
        self.requests: list[httpx.Request] = []
        self.patches: list[tuple[int, dict]] = []
        self.uploads: list[dict] = []
        # Vorschläge des Paperless-Klassifikators (leer = Endpunkt fehlt → 404, wie bei alten Versionen)
        self.suggestions: dict[int, dict] = {8: {"correspondents": [2], "document_types": [21], "tags": [11],
                                                 "dates": ["2026-09-01"]}}
        self.legacy_dates = False  # alte Versionen: Datum nur über created_date

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("Authorization") != f"Token {TOKEN}":
            return httpx.Response(401, json={"detail": "Ungültiges Token."})
        path, q = request.url.path.removeprefix("/api"), request.url.params
        lists = {"/correspondents/": self.correspondents, "/tags/": self.tags, "/document_types/": self.types}
        if path in lists and request.method == "POST":
            name = json.loads(request.content)["name"]
            if any(x["name"].lower() == name.lower() for x in lists[path]):
                return httpx.Response(400, json={"name": ["existiert bereits"]})
            item = {"id": 100 + sum(len(v) for v in lists.values()), "name": name}
            lists[path].append(item)
            return httpx.Response(201, json=item)
        if path in lists:
            items = lists[path]
            if q.get("is_inbox_tag") == "true":
                items = [x for x in items if x.get("is_inbox_tag")]
            return httpx.Response(200, json={"count": len(items), "results": items})
        if path == "/documents/post_document/" and request.method == "POST":
            body = request.read()
            name = re.search(rb'name="document"; filename="([^"]+)"', body)
            title = re.search(rb'name="title"\r\n\r\n([^\r]*)', body)
            self.uploads.append({"filename": name.group(1).decode() if name else "", "body": body,
                                 "title": title.group(1).decode() if title else ""})
            return httpx.Response(200, json=f"task-{len(self.uploads)}")
        m = re.fullmatch(r"/documents/(\d+)/suggestions/", path)
        if m:
            if not self.suggestions:
                return httpx.Response(404, json={"detail": "Nicht gefunden."})
            return httpx.Response(200, json=self.suggestions.get(int(m.group(1)), {}))
        m = re.fullmatch(r"/documents/(\d+)/", path)
        if m and request.method == "PATCH":
            doc = next((d for d in self.docs if d["id"] == int(m.group(1))), None)
            if not doc:
                return httpx.Response(404, json={"detail": "Nicht gefunden."})
            body = json.loads(request.content)
            if "created" in body and self.legacy_dates:
                return httpx.Response(400, json={"created": ["Datetime has wrong format."]})
            self.patches.append((doc["id"], dict(body)))
            body["created"] = body.pop("created_date", body.get("created", doc["created"]))
            doc.update(body)
            return httpx.Response(200, json=doc)
        if path == "/documents/":
            docs = list(self.docs)
            if q.get("query"):
                words = q["query"].lower().split()
                docs = [d for d in docs if any(w in (d["title"] + d["content"]).lower() for w in words)]
            if q.get("correspondent__name__icontains"):
                names = {c["id"] for c in self.correspondents
                         if q["correspondent__name__icontains"].lower() in c["name"].lower()}
                docs = [d for d in docs if d["correspondent"] in names]
            if q.get("tags__id__in"):
                wanted = {int(x) for x in q["tags__id__in"].split(",")}
                docs = [d for d in docs if wanted & set(d["tags"])]
            if q.get("tags__name__iexact"):
                ids = {t["id"] for t in self.tags if t["name"].lower() == q["tags__name__iexact"].lower()}
                docs = [d for d in docs if ids & set(d["tags"])]
            for field in ("correspondent", "document_type"):
                if q.get(f"{field}__isnull") == "true":
                    docs = [d for d in docs if not d.get(field)]
            if q.get("created__gte"):
                docs = [d for d in docs if d["created"] >= q["created__gte"]]
            if q.get("ordering") == "-created":
                docs.sort(key=lambda d: d["created"], reverse=True)
            out = []
            for d in docs[: int(q.get("page_size", 25))]:
                d = dict(d)
                if q.get("truncate_content"):
                    d["content"] = d["content"][:300]
                if q.get("query"):
                    d["__search_hit__"] = {"score": 1, "highlights": f"<span class=\"match\">{q['query']}</span> …"}
                out.append(d)
            return httpx.Response(200, json={"count": len(docs), "results": out}, headers={"X-Version": "2.17.1"})
        m = re.fullmatch(r"/documents/(\d+)/(download/)?", path)
        if m:
            doc = next((d for d in self.docs if d["id"] == int(m.group(1))), None)
            if not doc:
                return httpx.Response(404, json={"detail": "Nicht gefunden."})
            if m.group(2):
                body = b"ORIGINAL" if q.get("original") else b"%PDF-1.7 archiv"
                return httpx.Response(200, content=body, headers={"Content-Type": "application/pdf"})
            return httpx.Response(200, json=doc)
        return httpx.Response(404, json={})
