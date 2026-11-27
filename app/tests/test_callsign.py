import asyncio
import json

import httpx
import pytest
import yaml

from app import tools as T
from app.brain import tool_rules
from app.callsign import CallsignLookup, normalize, person, place
from app.config import Config

from .test_pipeline import CFG

QRZ_LOGIN = """<?xml version="1.0"?><QRZDatabase xmlns="http://xmldata.qrz.com"><Session><Key>K1</Key>
</Session></QRZDatabase>"""
QRZ_REC = """<?xml version="1.0"?><QRZDatabase xmlns="http://xmldata.qrz.com"><Callsign><call>DL1ABC</call>
<fname>Hans Peter</fname><name>Muster</name><addr1>Hauptstr. 1</addr1><addr2>Bremen</addr2><state></state>
<country>Germany</country><email>hans@example.com</email><lat>53.1</lat><lon>8.8</lon></Callsign>
<Session><Key>K1</Key></Session></QRZDatabase>"""
QRZ_TIMEOUT = """<?xml version="1.0"?><QRZDatabase xmlns="http://xmldata.qrz.com"><Session>
<Error>Session Timeout</Error></Session></QRZDatabase>"""
QRZ_NOTFOUND = """<?xml version="1.0"?><QRZDatabase xmlns="http://xmldata.qrz.com"><Session>
<Error>Not found: N0NONE</Error><Key>K1</Key></Session></QRZDatabase>"""
CALLOOK_PERSON = {"status": "VALID", "type": "PERSON", "name": "JANE Q PUBLIC",
                  "address": {"line1": "12 ELM ST", "line2": "SPRINGFIELD, IL 62701"},
                  "location": {"latitude": "39.8", "longitude": "-89.6", "gridsquare": "EM59"},
                  "otherInfo": {"frn": "0001234567"}}
CALLOOK_CLUB = {"status": "VALID", "type": "CLUB", "name": "ARRL HQ OPERATORS CLUB",
                "address": {"line1": "225 MAIN ST", "line2": "NEWINGTON, CT 06111"}}


def make(tmp_path, qrz=False, routes=None):
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(CFG))
    cfg = Config(p, data_dir=tmp_path / "data")
    if qrz:
        cfg.secrets.set("qrz_username", "me")
        cfg.secrets.set("qrz_password", "pw")
    calls = []

    def handler(req):
        calls.append(req.url)
        return routes(req)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return CallsignLookup(cfg, client), calls


def run(c):
    return asyncio.run(c)


def test_helpers():
    assert normalize(" w1aw ") == "W1AW" and normalize("VE3/N0CALL/P") == "N0CALL"
    with pytest.raises(T.ToolError):
        normalize("hello there")
    assert person("JANE Q", "PUBLIC") == "Jane P." and person("", "") == "name not listed"
    assert place("SPRINGFIELD", "IL") == "Springfield, IL" and place("Bremen", "", "Germany") == "Bremen, Germany"
    assert place("Austin", "TX", "United States") == "Austin, TX"


def test_callook_person_strips_everything_else(tmp_path):
    lk, calls = make(tmp_path, routes=lambda r: httpx.Response(200, json=CALLOOK_PERSON))
    text, meta = run(lk.lookup("n0call"))
    assert text == "N0CALL: Jane P., Springfield, IL."
    for leak in ("ELM", "62701", "39.8", "EM59", "0001234567", "PUBLIC", "Q "):
        assert leak not in text
    assert meta == {"found": True, "source": "FCC (callook.info)"}
    run(lk.lookup("N0CALL"))
    assert len(calls) == 1  # cached


def test_callook_club_and_missing(tmp_path):
    lk, _ = make(tmp_path, routes=lambda r: httpx.Response(200, json=CALLOOK_CLUB if "W1AW" in str(r.url)
                                                           else {"status": "INVALID"}))
    assert run(lk.lookup("W1AW"))[0] == "W1AW: ARRL HQ OPERATORS CLUB (club station), Newington, CT."
    text, meta = run(lk.lookup("N0NONE"))
    assert "no license record found" in text and meta == {"found": False}


def test_qrz_login_lookup_and_privacy(tmp_path):
    def routes(r):
        p = dict(r.url.params)
        if "username" in p:
            assert p["password"] == "pw"
            return httpx.Response(200, text=QRZ_LOGIN)
        assert p["s"] == "K1" and p["callsign"] == "DL1ABC"
        return httpx.Response(200, text=QRZ_REC)
    lk, _ = make(tmp_path, qrz=True, routes=routes)
    text, meta = run(lk.lookup("dl1abc"))
    assert text == "DL1ABC: Hans M., Bremen, Germany." and meta["source"] == "QRZ"
    for leak in ("Hauptstr", "hans@", "53.1", "Muster", "Peter"):
        assert leak not in text


def test_qrz_session_timeout_relogs_and_notfound_falls_back(tmp_path):
    state = {"logins": 0, "lookups": 0}

    def routes(r):
        p = dict(r.url.params)
        if "callook" in str(r.url):
            return httpx.Response(200, json={"status": "INVALID"})
        if "username" in p:
            state["logins"] += 1
            return httpx.Response(200, text=QRZ_LOGIN)
        state["lookups"] += 1
        if p["callsign"] == "N0NONE":
            return httpx.Response(200, text=QRZ_NOTFOUND)
        return httpx.Response(200, text=QRZ_TIMEOUT if state["lookups"] == 1 else QRZ_REC)
    lk, _ = make(tmp_path, qrz=True, routes=routes)
    assert run(lk.lookup("DL1ABC"))[0].startswith("DL1ABC: Hans M.") and state["logins"] == 2
    text, _ = run(lk.lookup("N0NONE"))
    assert "no license record found" in text


def test_qrz_down_falls_back_to_fcc(tmp_path):
    def routes(r):
        if "qrz" in str(r.url):
            return httpx.Response(503)
        return httpx.Response(200, json=CALLOOK_PERSON)
    lk, _ = make(tmp_path, qrz=True, routes=routes)
    text, meta = run(lk.lookup("N0CALL"))
    assert text == "N0CALL: Jane P., Springfield, IL." and meta["source"] == "FCC (callook.info)"


def test_tool_switch_and_rules():
    assert "callsign_lookup" in T.enabled_tools({"callsign_lookup": True})
    assert "callsign_lookup" not in T.enabled_tools({"callsign_lookup": False})
    r = tool_rules({"callsign_lookup": True}, 4)
    assert "first name, last initial and city/state" in r and "lead with their nodes" in r
    assert T.summarize_args("callsign_lookup", {"callsign": "W1AW"}) == "W1AW"
