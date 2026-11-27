"""Web control panel: aiohttp, in the bot's own event loop. LAN-bound, HTTP Basic auth (user "admin").

Login password, in order: the one set from the UI (hashed in data/secrets.json), then WEB_PASSWORD from .env,
else one is generated on first start and written to data/initial_password.txt (mode 600).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import os
import secrets as pysecrets
from pathlib import Path
from urllib.parse import quote

import segno
from aiohttp import web

from . import schema
from .config import DATA, atomic_write, prompt_slug

log = logging.getLogger("meshbot.web")

STATIC = Path(__file__).parent / "web"
USER = "admin"
BOT = web.AppKey("bot", object)
INITIAL_PW = DATA / "initial_password.txt"


# ---------- passwords ----------
def hash_pw(pw: str, salt: bytes | None = None, it: int = 200_000) -> str:
    salt = salt or os.urandom(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, it)
    return f"pbkdf2${it}${salt.hex()}${h.hex()}"


def check_hash(pw: str, stored: str) -> bool:
    try:
        _, it, salt, h = stored.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(it))
        return hmac.compare_digest(calc.hex(), h)
    except Exception:
        return False


def ensure_password(cfg) -> None:
    if cfg.secrets.get("web_password_hash") or os.environ.get("WEB_PASSWORD"):
        return
    pw = pysecrets.token_urlsafe(12)
    cfg.secrets.set("web_password_hash", hash_pw(pw))
    atomic_write(INITIAL_PW, pw + "\n")
    os.chmod(INITIAL_PW, 0o600)
    log.warning("no web password was set: generated one in %s (log in as admin, then change it in Settings)",
                INITIAL_PW)


def password_ok(cfg, pw: str) -> bool:
    stored = cfg.secrets.get("web_password_hash")
    if stored:
        return check_hash(pw, stored)
    env = os.environ.get("WEB_PASSWORD", "")
    return bool(env) and hmac.compare_digest(pw.encode(), env.encode())


@web.middleware
async def auth_mw(request: web.Request, handler):
    cfg = request.app[BOT].cfg
    hdr = request.headers.get("Authorization", "")
    if hdr.startswith("Basic "):
        try:
            user, _, pw = base64.b64decode(hdr[6:]).decode().partition(":")
        except Exception:
            user, pw = "", ""
        if user == USER and await asyncio.to_thread(password_ok, cfg, pw):
            return await handler(request)
        await asyncio.sleep(1)  # slow down guessing
    return web.Response(status=401, headers={"WWW-Authenticate": 'Basic realm="meshbot"'}, text="login required")


@web.middleware
async def error_mw(request: web.Request, handler):
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except (ValueError, KeyError) as e:
        return web.json_response({"error": str(e).strip("'\"")}, status=400)
    except Exception as e:
        log.exception("web error on %s", request.path)
        return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=500)


def J(data, status=200):
    return web.json_response(data, status=status, dumps=lambda o: __import__("json").dumps(o, default=str))


# ---------- handlers ----------
routes = web.RouteTableDef()


@routes.get("/")
async def index(request):
    return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


@routes.get("/api/status")
async def status(request):
    return J(request.app[BOT].status())


@routes.get("/api/schema")
async def get_schema(request):
    return J(schema.as_json())


def settings_payload(cfg) -> dict:
    values = {path: cfg.value(path) for path in schema.field_index()}
    chans = []
    for raw in cfg.get("channels") or []:
        c = dict(raw)
        c["private"] = not str(c["name"]).startswith("#")
        c["secret_set"] = bool(c.get("secret_env") and os.environ.get(c["secret_env"])) or bool(
            cfg.secrets.channel(c["name"]))
        chans.append(c)
    return {"values": values, "channels": chans,
            "secrets": {"llm_api_key": bool(cfg.secrets.get("llm_api_key")),
                        "llm_api_key_env": bool(os.environ.get("LITELLM_API_KEY")),
                        "openhop_api_key": bool(cfg.secrets.get("openhop_api_key")),
                        "openhop_api_key_env": bool(os.environ.get("OPENHOP_API_KEY")),
                        "guest_passwords": [l.partition("=")[0].strip() for l in
                                            (cfg.secrets.get("guest_passwords") or "").splitlines() if l.strip()],
                        "web_password": bool(cfg.secrets.get("web_password_hash"))},
            "config_error": cfg.last_error}


@routes.get("/api/settings")
async def get_settings(request):
    return J(settings_payload(request.app[BOT].cfg))


@routes.post("/api/settings")
async def post_settings(request):
    cfg = request.app[BOT].cfg
    body = await request.json()
    cfg.set_values(body.get("values") or {})
    log.info("settings changed from web: %s", ", ".join(body.get("values") or {}))
    return J(settings_payload(cfg))


@routes.put("/api/channels")
async def put_channels(request):
    cfg = request.app[BOT].cfg
    body = await request.json()
    old = {c["name"] for c in cfg.get("channels") or []}
    cfg.set_channels(body.get("channels") or [])
    new = {c["name"] for c in cfg.get("channels") or []}
    for gone in old - new:  # drop secrets of removed channels
        cfg.secrets.set_channel(gone, None)
    log.info("channels changed from web: %s", ", ".join(sorted(new)) or "(none)")
    return J(settings_payload(cfg))


def channel_link(cfg, name: str) -> tuple[str, str]:
    """meshcore://channel/add link (the format the MeshCore app and CoreScope use) and its QR code as inline SVG.
    Contains the channel key: only served to the logged-in admin, on request."""
    ch = cfg.channel_by_name(name)
    if not ch:
        raise KeyError("unknown channel")
    if name.startswith("#"):
        key = hashlib.sha256(name.encode()).hexdigest()[:32]  # hashtag channels: key derived from the name
    else:
        key = cfg.channel_secret(ch).hex()  # raises if no secret is set yet
    url = f"meshcore://channel/add?name={quote(name, safe='')}&secret={key}"
    svg = segno.make(url, error="m").svg_inline(scale=6, border=3, dark="#282727", light="#ffffff")
    return url, svg


@routes.post("/api/channels/qr")
async def channel_qr(request):
    cfg = request.app[BOT].cfg
    name = str((await request.json()).get("name", ""))
    url, svg = channel_link(cfg, name)
    log.info("channel join QR shown for %s", name)
    return J({"name": name, "url": url, "svg": svg})


@routes.post("/api/channels/secret")
async def channel_secret(request):
    cfg = request.app[BOT].cfg
    body = await request.json()
    name = str(body.get("name", ""))
    if not cfg.channel_by_name(name):
        raise KeyError("unknown channel")
    if name.startswith("#"):
        raise ValueError("hashtag channels don't use a secret")
    secret = body.get("secret")
    if secret == "generate":
        secret = pysecrets.token_hex(16)
        cfg.secrets.set_channel(name, secret)
        url, svg = channel_link(cfg, name)
        return J({**settings_payload(cfg), "generated": secret, "url": url, "svg": svg})  # to share
    cfg.secrets.set_channel(name, (secret or "").strip() or None)
    return J(settings_payload(cfg))


@routes.post("/api/secrets")
async def post_secrets(request):
    cfg = request.app[BOT].cfg
    body = await request.json()
    if "llm_api_key" in body:
        cfg.secrets.set("llm_api_key", (body["llm_api_key"] or "").strip() or None)
        log.info("LiteLLM API key %s from web", "updated" if body["llm_api_key"] else "cleared")
    if "openhop_api_key" in body:
        cfg.secrets.set("openhop_api_key", (body["openhop_api_key"] or "").strip() or None)
        log.info("openHop API key %s from web", "updated" if body["openhop_api_key"] else "cleared")
    if "guest_passwords" in body:
        text = "\n".join(l.strip() for l in str(body["guest_passwords"] or "").splitlines() if l.strip())
        bad = [l for l in text.splitlines() if "=" not in l]
        if bad:
            raise ValueError(f"each line must be Name=password (bad: {bad[0][:40]})")
        cfg.secrets.set("guest_passwords", text or None)
        log.info("repeater guest passwords %s from web (%d)", "updated" if text else "cleared",
                 len(text.splitlines()) if text else 0)
    return J(settings_payload(cfg))


@routes.post("/api/password")
async def post_password(request):
    cfg = request.app[BOT].cfg
    body = await request.json()
    new = str(body.get("new") or "")
    if len(new) < 8:
        raise ValueError("password must be at least 8 characters")
    if not await asyncio.to_thread(password_ok, cfg, str(body.get("current") or "")):
        raise ValueError("current password is wrong")
    cfg.secrets.set("web_password_hash", await asyncio.to_thread(hash_pw, new))
    INITIAL_PW.unlink(missing_ok=True)
    log.info("web password changed")
    return J({"ok": True})


@routes.get("/api/prompts")
async def get_prompts(request):
    cfg = request.app[BOT].cfg
    sys_p = cfg.prompt_path("system")
    chans = {}
    for c in cfg.channels():
        p = cfg.prompt_path(c["name"])
        chans[c["name"]] = p.read_text() if p.exists() else ""
    return J({"system": sys_p.read_text() if sys_p.exists() else "", "channels": chans})


@routes.put("/api/prompts")
async def put_prompt(request):
    cfg = request.app[BOT].cfg
    body = await request.json()
    which, text = str(body.get("which", "")), str(body.get("text", ""))
    if which != "system" and not cfg.channel_by_name(which):
        raise KeyError("unknown channel")
    path = cfg.prompt_path(which)
    if which != "system" and not text.strip():
        path.unlink(missing_ok=True)
    else:
        if not text.strip():
            raise ValueError("system prompt can't be empty")
        atomic_write(path, text.rstrip() + "\n")
    log.info("prompt '%s' saved from web", which)
    return J({"ok": True})


@routes.get("/api/raw_config")
async def get_raw(request):
    return J({"text": request.app[BOT].cfg.raw_text()})


@routes.put("/api/raw_config")
async def put_raw(request):
    cfg = request.app[BOT].cfg
    cfg.write_raw((await request.json()).get("text", ""))
    log.info("config.yaml replaced from web (raw editor)")
    return J({"ok": True})


def _int(q, k):
    v = q.get(k)
    return int(v) if v not in (None, "") else None


@routes.get("/api/messages")
async def messages(request):
    q = request.query
    return J(request.app[BOT].history.messages(q.get("channel") or None, min(_int(q, "limit") or 100, 500),
                                                 _int(q, "before"), _int(q, "after")))


@routes.get("/api/decisions")
async def decisions(request):
    q = request.query
    return J(request.app[BOT].history.decisions(q.get("channel") or None, q.get("outcome") or None,
                                                  min(_int(q, "limit") or 50, 500), _int(q, "before"),
                                                  q.get("q") or None))


@routes.get("/api/decisions/{id}")
async def decision(request):
    d = request.app[BOT].history.decision(int(request.match_info["id"]))
    if not d:
        raise KeyError("no such decision")
    return J(d)


def compose_prompt(cfg, channel: str, system_draft: str | None, channel_draft: str | None) -> str | None:
    """Prompt built from unsaved drafts (falling back to the saved files), or None to use the files."""
    if system_draft is None and channel_draft is None:
        return None
    sp, cp = cfg.prompt_path("system"), cfg.prompt_path(channel)
    s = system_draft if system_draft is not None else (sp.read_text() if sp.exists() else "")
    c = channel_draft if channel_draft is not None else (cp.read_text() if cp.exists() else "")
    return s.strip() + ("\n\n## Notes for this channel\n" + c.strip() if c.strip() else "")


@routes.post("/api/playground")
async def playground(request):
    bot = request.app[BOT]
    b = await request.json()
    thread = [t for t in b.get("thread") or [] if str(t.get("text", "")).strip()]
    if not thread:
        raise ValueError("add at least one message")
    prompt = compose_prompt(bot.cfg, b["channel"], b.get("system_draft"), b.get("channel_draft"))
    overrides = {}
    for k, v in (b.get("overrides") or {}).items():
        f = schema.channel_field(k)
        if f and v not in (None, ""):
            overrides[k] = schema.coerce(f, v)
    return J(await bot.playground(b["channel"], thread, prompt, overrides))


@routes.post("/api/replay")
async def replay(request):
    bot = request.app[BOT]
    b = await request.json()
    ids = [int(i) for i in b.get("ids") or []][:20]
    out = []
    for i in ids:
        rec = bot.history.decision(i)
        if not rec:
            continue
        prompt = compose_prompt(bot.cfg, rec["channel"], b.get("system_draft"), b.get("channel_draft"))
        out.append(await bot.replay(i, prompt))
    return J(out)


@routes.post("/api/send")
async def send(request):
    b = await request.json()
    return J(await request.app[BOT].manual_send(str(b.get("channel", "")), str(b.get("text", ""))))


@routes.post("/api/test_llm")
async def test_llm(request):
    """Ping every model in use (primary and fallback). A success also clears that model's failover hold."""
    bot = request.app[BOT]
    out = []
    for m in [x["model"] for x in bot.model_health()]:
        try:
            out.append({"ok": True, **await bot.brain.ping(m)})
        except Exception as e:
            out.append({"ok": False, "model": m, "error": f"{type(e).__name__}: {e}"[:300]})
    return J({"results": out})


@routes.post("/api/test_mesh")
async def test_mesh(request):
    """Check CoreScope and openHop are reachable. Never transmits."""
    return J({"results": await request.app[BOT].meshtools.test()})


@routes.get("/api/hass")
async def hass_tools(request):
    """The Home Assistant MCP server LiteLLM exposes and how each tool is classified. Calls no tools."""
    return J(await request.app[BOT].brain.hass.overview())


@routes.get("/api/models")
async def models(request):
    try:
        return J({"models": await request.app[BOT].brain.models()})
    except Exception as e:
        return J({"models": [], "error": f"{type(e).__name__}: {e}"[:300]})


@routes.post("/api/rename_companion")
async def rename(request):
    bot = request.app[BOT]
    return J({"name": await bot.mesh.rename(bot.cfg.bot_name)})


@routes.get("/api/logs")
async def logs(request):
    buf = request.app[BOT].logbuf
    after = _int(request.query, "after") or 0
    lines = [] if not buf else [{"seq": s, "level": lv, "text": t} for s, lv, t in buf.lines if s > after]
    return J(lines)


def make_app(bot) -> web.Application:
    app = web.Application(middlewares=[auth_mw, error_mw], client_max_size=1024 * 1024)
    app[BOT] = bot
    app.add_routes(routes)
    app.router.add_static("/static/", STATIC)
    return app


async def start_web(bot) -> None:
    host = os.environ.get("WEB_BIND", "127.0.0.1")
    port = int(os.environ.get("WEB_PORT", "8093"))
    ensure_password(bot.cfg)
    runner = web.AppRunner(make_app(bot), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    log.info("web UI on http://%s:%s (user %s)", host, port, USER)


__all__ = ["start_web", "make_app", "hash_pw", "check_hash", "prompt_slug"]
