import asyncio
import time
from datetime import datetime, timezone

import httpx
import pytest
import yaml

from app import tools as T
from app.brain import format_thread, tool_rules
from app.config import Config
from app.history import Msg
from app.meshdata import MeshTools, ago, dist_bearing

from .test_pipeline import CFG

NOW = time.time()


def iso(secs_ago):
    return datetime.fromtimestamp(NOW - secs_ago, timezone.utc).isoformat().replace("+00:00", "Z")


HOME_PK = "aa" * 32
NODES = [
    {"name": "Theusela 1w V2", "public_key": "6751" + "0" * 60, "role": "repeater", "lat": 41.0947, "lon": -85.155,
     "last_heard": iso(120), "first_seen": iso(9 * 86400), "relay_count_1h": 3, "relay_count_24h": 132,
     "usefulness_grade": "B", "advert_count": 10},
    {"name": "Theusela Mobile", "public_key": "6799" + "0" * 60, "role": "companion", "lat": 41.1, "lon": -85.1,
     "last_heard": iso(7200), "first_seen": iso(3600)},
    {"name": "K9SLN-R EN71", "public_key": "ad5b" + "1" * 60, "role": "repeater", "lat": 41.2, "lon": -85.2,
     "last_heard": iso(30), "first_seen": iso(20 * 86400), "relay_count_1h": 9, "relay_count_24h": 300},
    {"name": "Home Rptr", "public_key": HOME_PK, "role": "repeater", "lat": 41.08, "lon": -85.14,
     "last_heard": iso(10), "first_seen": iso(30 * 86400)},
]


def make(tmp_path, channel=None, mesh=None, openhop_key=None, extra_mesh=None, home=(41.08, -85.14)):
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump({**CFG, "mesh": {"radio_timeout_s": 5, **(extra_mesh or {})}}))
    cfg = Config(p, data_dir=tmp_path / "data")
    if openhop_key:
        cfg.secrets.set("openhop_api_key", openhop_key)
    seen = []

    def handler(req):
        seen.append(req)
        path = req.url.path
        if req.url.port == 8000:
            assert req.headers.get("X-API-Key") == openhop_key
            name = path.rsplit("/", 1)[-1]
            data = {"stats": {"node_name": "Home Rptr", "mode": "forward", "uptime_seconds": 86400 * 3,
                              "rx_count": 500, "noise_floor_dbm": -112.5, "public_key": HOME_PK,
                              "config": {"secret": "nope"}},
                    "noise_floor_stats": {"stats": {"avg": -111.2, "min": -115.0}, "hours": 24},
                    "neighbor_links": {"links": [{"pubkey": "ad5b", "snr": 6.25, "last_seen": iso(60)}]}}[name]
            return httpx.Response(200, json={"success": True, "data": data})
        assert "X-API-Key" not in req.headers  # the openHop key never goes to CoreScope
        if path == "/api/nodes":
            return httpx.Response(200, json={"nodes": NODES, "total": len(NODES)})
        if path == "/api/stats":
            return httpx.Response(200, json={"totalNodes": 4, "packetsLastHour": 223, "packetsLast24h": 9630,
                                             "counts": {"repeaters": 3, "companions": 1, "rooms": 0}})
        if path == "/api/observers":
            return httpx.Response(200, json={"observers": [{"name": "O1", "last_seen": iso(60)},
                                                           {"name": "O2", "last_seen": iso(86400)}]})
        if path.endswith("/neighbors"):
            return httpx.Response(200, json={"neighbors": [
                {"name": "IN.Observer", "role": "observer", "count": 92},
                {"name": "K9SLN-R EN71", "role": "repeater", "count": 59, "avg_snr": 4.17, "distance_km": 12.0}]})
        if path.startswith("/api/nodes/"):
            pk = path.split("/")[3]
            n = next((x for x in NODES if x["public_key"] == pk), None)
            return httpx.Response(200 if n else 404, json={"node": n, "recentAdverts": []})
        if path == "/api/channels":  # must never be fetched: it carries message text
            raise AssertionError("channel messages fetched")
        return httpx.Response(404, json={})

    mt = MeshTools(cfg, mesh, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    async def fake_home():
        return home
    mt.home = fake_home
    return mt, cfg, seen


def run(coro):
    return asyncio.run(coro)


def test_formatting_helpers():
    assert ago(NOW - 30, NOW) == "30s ago" and ago(NOW - 600, NOW) == "10m ago" and ago(None) == "never"
    assert ago(iso(3 * 86400), NOW).endswith("d ago")
    db = dist_bearing((41.0, -85.0), 41.1, -85.0)
    assert db.endswith(" N") and db.startswith("6.9mi")
    assert dist_bearing(None, 1, 2) == "" and dist_bearing((1, 2), 0, 0) == ""


def test_node_lookup_with_candidates(tmp_path):
    mt, _, _ = make(tmp_path)
    text, meta = run(mt.node("theusela"))
    first = text.splitlines()[0]
    assert first.startswith("Theusela 1w V2 [repeater, key 67510000]") and "heard 2m ago" in first
    assert "of home" in first
    assert "relayed 3 pkts last 1h, 132 last 24h" in text and "grade B" in text
    assert "Neighbors (as the network sees them): K9SLN-R EN71 SNR 4.2 7.5mi" in text
    assert "IN.Observer" not in text  # observers dropped when real neighbors exist
    assert "Other matches: Theusela Mobile [companion]" in text and meta["found"] == 2


def test_node_by_key_prefix_and_missing(tmp_path):
    mt, _, _ = make(tmp_path)
    assert run(mt.node("ad5b"))[0].startswith("K9SLN-R EN71")
    assert "No node matching" in run(mt.node("nonexistent-xyz"))[0]


def test_nodes_list_filters_and_sorts(tmp_path):
    mt, _, _ = make(tmp_path)
    text, meta = run(mt.nodes_list("repeaters", 1, "busiest", 5))
    assert meta["count"] == 3 and text.startswith("3 repeaters heard in the last 1h (of 4 known)")
    assert text.splitlines()[1].startswith("- K9SLN-R EN71") and "300 relays/24h" in text
    text, meta = run(mt.run("mesh_nodes", {"first_seen_within_h": 24, "sort": "new"}, {}))
    assert meta["count"] == 1 and "Theusela Mobile" in text and "first seen in the last 24h" in text
    with pytest.raises(T.ToolError):
        run(mt.nodes_list("toaster"))


def test_overview(tmp_path):
    mt, _, _ = make(tmp_path)
    text, _ = run(mt.overview())
    assert "4 nodes (3 repeaters" in text and "223 last hour" in text
    assert "Observers feeding the map: 1 of 2 online" in text and "Busiest relays last hour: K9SLN-R EN71 (9)" in text


def test_home_repeater_needs_key_and_uses_allowlist(tmp_path):
    mt, _, _ = make(tmp_path)
    with pytest.raises(T.ToolError, match="no openHop API key"):
        run(mt.run("repeater_status", {}, {}))
    mt, _, seen = make(tmp_path, openhop_key="tok123")
    text, _ = run(mt.run("repeater_status", {}, {}))
    assert "Home Rptr (our repeater), mode forward, up 3.0d" in text and "noise floor dBm -112.5" in text
    assert "Noise floor 24h (dBm): avg -111.2" in text and "nope" not in text
    text, _ = run(mt.run("repeater_neighbors", {}, {}))
    assert "K9SLN-R EN71 SNR 6.2" in text
    with pytest.raises(T.ToolError, match="not allowed"):
        run(mt.oh("restart_service"))


class FakeMesh:
    connected = True

    def __init__(self, login="ok", contact=None, advert=None):
        self.calls = []
        self._login, self._contact, self._advert = login, contact, advert

    async def get_contact(self, pk):
        return self._contact

    async def advert_path(self, pk):
        return self._advert

    async def ensure_contact(self, node):
        self.calls.append(("contact", node["name"]))

    async def login(self, pk, pw, timeout):
        self.calls.append(("login", pw))
        if self._login == "refused":
            raise PermissionError("login refused (wrong guest password?)")
        if self._login == "timeout":
            raise TimeoutError("no answer to login (out of range or busy)")
        return {"is_admin": False}

    async def logout(self, pk):
        self.calls.append(("logout",))

    async def neighbours(self, pk, timeout):
        return {"neighbours_count": 2, "neighbours": [{"pubkey": "ad5b1111", "secs_ago": 50, "snr": 7.5},
                                                      {"pubkey": "ffff0000", "secs_ago": 4000, "snr": -3.0}]}

    async def status_req(self, pk, timeout):
        return {"uptime": 172800, "bat": 4100, "noise_floor": -110, "last_snr": 5.5, "nb_recv": 10, "nb_sent": 4}

    async def path_discovery(self, pk, timeout):
        self.calls.append(("path",))
        return {"out_path_len": 2, "out_path": "ad5b6751", "out_path_hash_len": 2}


def test_radio_queries_off_falls_back_to_map(tmp_path):
    fm = FakeMesh()
    mt, _, _ = make(tmp_path, mesh=fm)
    text, meta = run(mt.run("repeater_neighbors", {"name": "Theusela 1w"}, {"radio_queries": False}))
    assert "Radio queries are off" in text and "Neighbors (as the network sees them)" in text
    assert fm.calls == [] and "radio" not in meta


def test_guest_login_neighbours_cache_and_cap(tmp_path, monkeypatch):
    fm = FakeMesh()
    mt, cfg, _ = make(tmp_path, mesh=fm, extra_mesh={"radio_max_per_hour": 1, "default_guest_password": "hello"})
    cfg.secrets.set("guest_passwords", "theusela 1w v2=s3cret")
    ch = {"radio_queries": True}
    text, meta = run(mt.run("repeater_neighbors", {"name": "Theusela 1w"}, ch))
    assert meta.get("radio") and "Theusela 1w V2 reports 2 neighbours" in text and "logged in as guest" in text
    assert "K9SLN-R EN71 SNR 7.5" in text and "FFFF0000" in text
    assert fm.calls == [("contact", "Theusela 1w V2"), ("login", "s3cret"), ("logout",)]
    # same question again: cached, no new transmission
    text2, _ = run(mt.run("repeater_neighbors", {"name": "Theusela 1w"}, ch))
    assert "(from " in text2 and len(fm.calls) == 3
    # a different repeater: hourly cap (1) reached, falls back to the map
    text3, meta3 = run(mt.run("repeater_status", {"name": "K9SLN"}, ch))
    assert "limit reached" in meta3["radio_error"] and "K9SLN-R EN71 [repeater" in text3
    assert mt.radio_used_last_hour() == 1


def test_guest_refused_reports_and_falls_back(tmp_path):
    fm = FakeMesh(login="refused")
    mt, _, _ = make(tmp_path, mesh=fm)
    text, meta = run(mt.run("repeater_status", {"name": "K9SLN"}, {"radio_queries": True}))
    assert "Radio query to K9SLN-R EN71 failed: login refused" in text and "Network map view" in text
    assert mt.radio_log[-1][3].startswith("login refused")


def test_remote_status(tmp_path):
    mt, _, _ = make(tmp_path, mesh=FakeMesh())
    text, _ = run(mt.run("repeater_status", {"name": "K9SLN"}, {"radio_queries": True}))
    assert "status (radio): up 2.0d, battery 4.10V, noise floor -110dBm, last SNR 5.5, rx 10 tx 4" in text


def test_named_home_repeater_uses_openhop_not_radio(tmp_path):
    fm = FakeMesh()
    mt, _, _ = make(tmp_path, mesh=fm, openhop_key="tok123")
    text, _ = run(mt.run("repeater_neighbors", {"name": "Home Rptr"}, {"radio_queries": True}))
    assert "Our repeater's direct neighbors" in text and fm.calls == []


def test_companions_are_not_queried(tmp_path):
    fm = FakeMesh()
    mt, _, _ = make(tmp_path, mesh=fm)
    text, _ = run(mt.run("repeater_status", {"name": "Theusela Mobile"}, {"radio_queries": True}))
    assert "is a companion, not a repeater" in text and fm.calls == []


def test_hops_passive_and_live(tmp_path):
    fm = FakeMesh(advert={"path_len": 2, "path": "ad5b6751", "path_hash_mode": 1, "timestamp": NOW - 300})
    mt, _, _ = make(tmp_path, mesh=fm)
    text, _ = run(mt.run("mesh_hops", {"name": "Home Rptr"}, {"radio_queries": False}))
    assert "last advert reached us in 2 hops via K9SLN-R EN71 > Theusela 1w V2, 5m ago" in text
    text, _ = run(mt.run("mesh_hops", {"name": "Home Rptr", "live": True}, {"radio_queries": False}))
    assert "Live path discovery is turned off" in text and ("path",) not in fm.calls
    text, _ = run(mt.run("mesh_hops", {"name": "Home Rptr", "live": True}, {"radio_queries": True}))
    assert "Live path discovery: Home Rptr is 2 hops from TestBot via K9SLN-R EN71 > Theusela 1w V2" in text


def test_hops_unknown_route(tmp_path):
    mt, _, _ = make(tmp_path, mesh=FakeMesh())
    text, _ = run(mt.run("mesh_hops", {"name": "K9SLN"}, {"radio_queries": True}))
    assert "No route to K9SLN-R EN71 is known yet" in text and "live path discovery could find one" in text


def test_tools_gated_by_channel_flag():
    assert not set(T.MESH_TOOLS) & set(T.enabled_tools({"weather": True}))
    assert set(T.MESH_TOOLS) <= set(T.enabled_tools({"mesh_data": True}))
    rules_off = tool_rules({"mesh_data": True, "name": "#t"}, 4)
    assert "you MUST use the mesh tools" in rules_off and "Radio queries are OFF" in rules_off
    assert "Radio queries are ON" in tool_rules({"mesh_data": True, "radio_queries": True}, 4)
    assert T.summarize_args("mesh_hops", {"name": "X", "live": True}) == "X live"


def test_thread_shows_hops():
    msgs = [Msg(1, "#t", NOW, "Al", "hi", False, hops=2), Msg(2, "#t", NOW, "Bo", "yo", False, hops=0),
            Msg(3, "#t", NOW, "Cy", "eh", False), Msg(4, "#t", NOW, "TestBot", "hey", True, hops=1)]
    t = format_thread(msgs, "TestBot")
    assert "Al (2 hops): hi" in t and "Bo (direct): yo" in t and "Cy: eh" in t and "TestBot (you): hey" in t
