"""Nachbildung der Telegram-Bot-API für Tests (httpx.MockTransport)."""

import asyncio
import json
import re
import time

import httpx

TOKEN = "123:TEST"


class FakeTelegram:
    def __init__(self):
        self.updates: list[dict] = []
        self.sent: list[dict] = []
        self.edited: list[dict] = []
        self.actions: list[str] = []
        self.next_id = 1
        self.files = {"voice-1": b"OggS-fake"}
        self.documents: list[dict] = []  # per sendDocument verschickte Dateien

    def user_message(self, chat_id: int, text: str = "", voice: str | None = None) -> None:
        msg = {"message_id": self.next_id, "chat": {"id": chat_id}}
        if text:
            msg["text"] = text
        if voice:
            msg["voice"] = {"file_id": voice}
        self.updates.append({"update_id": self.next_id, "message": msg})
        self.next_id += 1

    def user_file(self, chat_id: int, file_id: str, data: bytes, name: str = "", caption: str = "",
                  photo: bool = False, size: int | None = None) -> None:
        self.files[file_id] = data
        msg = {"message_id": self.next_id, "chat": {"id": chat_id}}
        info = {"file_id": file_id, "file_size": len(data) if size is None else size}
        if photo:
            msg["photo"] = [{**info, "file_size": 10, "width": 90}, {**info, "width": 1280}]
        else:
            msg["document"] = {**info, "file_name": name}
        if caption:
            msg["caption"] = caption
        self.updates.append({"update_id": self.next_id, "message": msg})
        self.next_id += 1

    def press(self, chat_id: int, data: str) -> None:
        self.updates.append({"update_id": self.next_id, "callback_query": {
            "id": f"cq{self.next_id}", "data": data, "message": {"chat": {"id": chat_id}}}})
        self.next_id += 1

    def buttons(self) -> list[str]:
        """callback_data der Knöpfe unter der letzten Nachricht mit Tastatur."""
        for m in reversed(self.sent):
            if "reply_markup" in m:
                return [b["callback_data"] for row in m["reply_markup"]["inline_keyboard"] for b in row]
        return []

    def texts(self) -> list[str]:
        return [m["text"] for m in self.sent]

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "/file/bot" in path:
            return httpx.Response(200, content=self.files.get(path.rsplit("/", 1)[-1], b""))
        method = path.rsplit("/", 1)[-1]
        if method == "sendDocument":
            raw = request.read()
            name = re.search(rb'name="document"; filename="([^"]+)"', raw)
            content = raw.split(b"\r\n\r\n", 3)[-1] if b"\r\n\r\n" in raw else b""
            doc = {"filename": name.group(1).decode() if name else "", "raw": raw, "content": content}
            self.documents.append(doc)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 5000 + len(self.documents)}})
        body = json.loads(request.content or b"{}")
        if f"/bot{TOKEN}/" not in path:
            return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"id": 1, "is_bot": True, "username": "orbwise_test_bot"}})
        if method == "deleteWebhook":
            self.webhook_deleted = True
            return httpx.Response(200, json={"ok": True, "result": True})
        if method == "getUpdates":
            offset = body.get("offset", 0)
            ups = [u for u in self.updates if u["update_id"] >= offset]
            self.updates = ups
            if not ups:
                time.sleep(0.02)  # echtes Long Polling wartet – hier kurz, damit die Schleife nicht rast
            return httpx.Response(200, json={"ok": True, "result": ups})
        if method == "sendMessage":
            msg = {**body, "message_id": 1000 + len(self.sent)}
            self.sent.append(msg)
            return httpx.Response(200, json={"ok": True, "result": msg})
        if method == "editMessageText":
            self.edited.append(body)
            return httpx.Response(200, json={"ok": True, "result": body})
        if method == "sendChatAction":
            self.actions.append(body.get("action"))
            return httpx.Response(200, json={"ok": True, "result": True})
        if method == "answerCallbackQuery":
            return httpx.Response(200, json={"ok": True, "result": True})
        if method == "getFile":
            return httpx.Response(200, json={"ok": True, "result": {"file_path": body["file_id"]}})
        return httpx.Response(404, json={"ok": False, "description": f"unknown {method}"})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


async def wait_for(cond, timeout: float = 5.0) -> None:
    for _ in range(int(timeout / 0.02)):
        if cond():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("Bedingung nicht erfüllt")
