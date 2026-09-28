"""In-Memory-Nachbildung der Trilium-ETAPI für Tests (httpx.MockTransport)."""

import json
import re

import httpx

TOKEN = "test-token"


class FakeTrilium:
    def __init__(self):
        self.notes = {
            "root": {"noteId": "root", "title": "root", "type": "text", "parentNoteIds": [], "content": ""},
            "inbox123": {"noteId": "inbox123", "title": "Inbox", "type": "text", "parentNoteIds": ["root"],
                         "content": ""},
            "nasSetup01": {"noteId": "nasSetup01", "title": "NAS Setup", "type": "text", "parentNoteIds": ["root"],
                           "content": "<p>Das NAS ist unter <strong>/mnt/nas</strong> gemountet.</p>"
                                      "<ul><li>SMB-Freigabe</li><li>Backup nachts</li></ul>",
                           "utcDateModified": "2026-09-20 10:00:00.000Z"},
            "shop0000001": {"noteId": "shop0000001", "title": "Einkaufsliste", "type": "text",
                            "parentNoteIds": ["root"], "content": "<ul><li>Brot</li></ul>",
                            "utcDateModified": "2026-09-26 08:00:00.000Z"},
        }
        self.requests: list[httpx.Request] = []
        self.counter = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def _public(self, n):
        return {k: v for k, v in n.items() if k != "content"}

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("Authorization") != TOKEN:
            return httpx.Response(401, json={"message": "unauthorized"})
        path = request.url.path.removeprefix("/etapi")
        m = re.fullmatch(r"/notes/(\w+)/content", path)
        if m:
            note = self.notes.get(m.group(1))
            if not note:
                return httpx.Response(404, json={"message": "not found"})
            if request.method == "PUT":
                note["content"] = request.content.decode()
                return httpx.Response(204)
            return httpx.Response(200, text=note["content"])
        m = re.fullmatch(r"/notes/(\w+)", path)
        if m:
            note = self.notes.get(m.group(1))
            if not note:
                return httpx.Response(404, json={"message": "not found"})
            if request.method == "PATCH":
                note.update(json.loads(request.content))
            return httpx.Response(200, json=self._public(note))
        if path == "/notes":
            q = request.url.params["search"]
            exact = re.fullmatch(r'note\.title = "(.*)"', q)
            contains = re.fullmatch(r'note\.title \*=\* "(.*)"', q)
            res = []
            for n in self.notes.values():
                if n["noteId"] == "root":
                    continue
                if exact:
                    ok = n["title"] == exact.group(1)
                elif contains:
                    ok = contains.group(1).lower() in n["title"].lower()
                else:
                    words = q.lower().split()
                    hay = (n["title"] + " " + n["content"]).lower()
                    ok = all(w in hay for w in words)
                if ok:
                    res.append(self._public(n))
            return httpx.Response(200, json={"results": res[: int(request.url.params.get("limit", 100))]})
        if path.startswith("/inbox/"):
            return httpx.Response(200, json=self._public(self.notes["inbox123"]))
        if path == "/create-note":
            body = json.loads(request.content)
            if body["parentNoteId"] not in self.notes:
                return httpx.Response(400, json={"message": "parent not found"})
            self.counter += 1
            nid = f"new{self.counter:09d}"
            self.notes[nid] = {"noteId": nid, "title": body["title"], "type": body["type"],
                               "parentNoteIds": [body["parentNoteId"]], "content": body["content"]}
            return httpx.Response(201, json={"note": self._public(self.notes[nid]), "branch": {}})
        if path == "/app-info":
            return httpx.Response(200, json={"appVersion": "0.99.0"})
        return httpx.Response(404, json={"message": "unknown"})
