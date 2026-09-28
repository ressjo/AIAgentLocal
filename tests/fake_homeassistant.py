"""Kleine Home-Assistant-REST-Nachbildung für Tests (httpx.MockTransport)."""

import json

import httpx

TOKEN = "ha-test-token"


class FakeHA:
    def __init__(self):
        self.states = {
            "light.wohnzimmer_decke": {"state": "off", "attributes": {"friendly_name": "Wohnzimmer Decke"}},
            "light.kueche": {"state": "on", "attributes": {"friendly_name": "Küche", "brightness": 204}},
            "climate.bad": {"state": "heat", "attributes": {"friendly_name": "Heizung Bad", "current_temperature": 20.5,
                                                            "temperature": 21}},
            "sensor.buero_temperatur": {"state": "22.4", "attributes": {"friendly_name": "Büro Temperatur",
                                                                        "unit_of_measurement": "°C"}},
            "cover.garagentor": {"state": "closed", "attributes": {"friendly_name": "Garagentor"}},
            "scene.filmabend": {"state": "scening", "attributes": {"friendly_name": "Filmabend"}},
            "lock.haustuer": {"state": "locked", "attributes": {"friendly_name": "Haustür"}},
        }
        self.areas = {"light.wohnzimmer_decke": "Wohnzimmer", "light.kueche": "Küche", "climate.bad": "Bad"}
        self.calls: list[tuple[str, dict]] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def _state(self, eid):
        return {"entity_id": eid, "last_changed": "2026-09-28T10:00:00+00:00", **self.states[eid]}

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, text="401: Unauthorized")
        path = request.url.path.removeprefix("/api")
        if path == "/config":
            return httpx.Response(200, json={"version": "2026.9.1"})
        if path == "/states":
            return httpx.Response(200, json=[self._state(e) for e in self.states])
        if path.startswith("/states/"):
            eid = path.split("/", 2)[2]
            return httpx.Response(200, json=self._state(eid)) if eid in self.states else httpx.Response(404, json={})
        if path == "/template":
            return httpx.Response(200, text=json.dumps(self.areas), headers={"content-type": "text/plain"})
        if path.startswith("/services/"):
            _, _, domain, service = path.split("/")
            data = json.loads(request.content)
            self.calls.append((f"{domain}.{service}", data))
            eid = data["entity_id"]
            st = self.states[eid]
            if service in ("turn_on", "open_cover"):
                st["state"] = "on" if domain != "cover" else "open"
                if "brightness_pct" in data:
                    st["attributes"]["brightness"] = round(data["brightness_pct"] * 2.55)
            elif service in ("turn_off", "close_cover"):
                st["state"] = "off" if domain != "cover" else "closed"
            elif service == "set_temperature":
                st["attributes"]["temperature"] = data["temperature"]
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={})
