import asyncio

import httpx
import pytest

from app import tools as T


def test_html_to_text_drops_chrome():
    html = ("<html><head><title>Komets Win</title><style>x{}</style></head><body><nav>Menu Home</nav>"
            "<article><h1>Komets 6, Walleye 3</h1><p>Springfield won &amp; leads.</p><script>evil()</script>"
            "<table><tr><td>Final</td><td>6-3</td></tr></table></article><footer>(c) 2026</footer></body></html>")
    title, text = T.html_to_text(html)
    assert title == "Komets Win"
    assert "Komets 6, Walleye 3" in text and "Springfield won & leads." in text and "6-3" in text
    assert "Menu" not in text and "evil" not in text and "2026" not in text


def test_focus_keeps_relevant_lines():
    lines = [f"filler line {i} about nothing much at all" for i in range(400)]
    lines[250] = "Tonight: low of 28F with snow showers"
    out = T.focus("\n".join(lines), "tonight low snow", 1500)
    assert len(out) <= 1500 and "low of 28F" in out and "filler line 0 " in out


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://10.0.0.1/x", "http://172.16.0.1:8093/",
                                 "http://169.254.169.254/latest/meta-data", "http://[::1]/", "http://localhost/",
                                 "http://router.lan/", "ftp://example.com/", "http://example.com:8080/",
                                 "http://[::ffff:192.168.1.1]/"])
def test_read_page_refuses_private_targets(url):
    with pytest.raises(T.ToolError):
        asyncio.run(T.fetch_page(url))


def test_resolver_refuses_names_pointing_inside():
    class Fake:
        def __init__(self, ip):
            self.ip = ip

        async def resolve(self, host, port, family):
            return [{"hostname": host, "host": self.ip, "port": port, "family": family, "proto": 0, "flags": 0}]

    r = T.PublicOnlyResolver.__new__(T.PublicOnlyResolver)
    r._r = Fake("192.168.1.10")
    with pytest.raises(OSError):
        asyncio.run(r.resolve("evil.example", 80))
    r._r = Fake("93.184.215.14")
    assert asyncio.run(r.resolve("example.com", 80))[0]["host"] == "93.184.215.14"


def test_weather_formats_and_geocodes_zip():
    seen = []

    def handler(req):
        seen.append(req.url)
        if "geocoding" in req.url.host:
            assert req.url.params["countryCode"] == "US"
            return httpx.Response(200, json={"results": [{"name": "Springfield", "admin1": "Illinois", "country_code": "US",
                                                          "latitude": 41.2, "longitude": -85.1}]})
        if "weather.gov" in req.url.host:
            return httpx.Response(200, json={"features": [{"properties": {"event": "Winter Storm Warning",
                                                                          "ends": "2026-11-28T06:00:00-05:00"}}]})
        return httpx.Response(200, json={
            "current": {"time": "2026-11-26T19:00", "temperature_2m": 31.4, "apparent_temperature": 24, "weather_code": 71,
                        "relative_humidity_2m": 80, "wind_speed_10m": 12, "wind_direction_10m": 270, "wind_gusts_10m": 20},
            "daily": {"time": ["2026-11-26"], "weather_code": [73], "temperature_2m_max": [34], "temperature_2m_min": [25],
                      "precipitation_probability_max": [70], "precipitation_sum": [0.3], "wind_speed_10m_max": [15]}})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await T.weather(c, "62701")
    text, meta = asyncio.run(go())
    assert "Now: 31F (feels 24F), light snow" in text and "wind W 12 mph" in text
    assert "high 34F low 25F, precip chance 70%" in text and "Winter Storm Warning" in text
    assert meta["place"] == "Springfield, Illinois, US"


def test_tools_follow_channel_switches():
    assert T.enabled_tools({"web_search": True, "weather": True}) == ["web_search", "read_page", "weather"]
    assert T.enabled_tools({"web_search": False, "weather": True}) == ["weather"]
    assert T.enabled_tools({}) == []


def test_focus_without_matches_fills_budget():
    text = "\n".join(f"line {i} " + "x" * 40 for i in range(500))
    out = T.focus(text, "nonexistentword", 3000)
    assert len(out) >= 2900
