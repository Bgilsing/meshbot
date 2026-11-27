#!/bin/sh
# Create a companion identity in openHop for meshbot (default name MeshBot, served on 127.0.0.1:5070).
# Usage: ./scripts/create_companion.sh [NAME] [PORT]   (OPENHOP_CONTAINER overrides the container name)
# Runs inside the openhop-repeater container, logs in with the admin password from its own
# config.yaml (the same way openHop's CLI does), and calls /api/create_identity. openHop
# saves it to config.yaml and activates it immediately, with no restart. Prints no secrets.
set -e
NAME="${1:-MeshBot}"
PORT="${2:-5070}"
docker exec -i -e NAME="$NAME" -e PORT="$PORT" "${OPENHOP_CONTAINER:-openhop-repeater}" python3 - <<'PY'
import json, os, urllib.request, urllib.error, yaml
cfg = yaml.safe_load(open("/etc/openhop_repeater/config.yaml"))
pw = cfg["repeater"]["security"]["admin_password"]
def call(path, body, tok=None):
    h = {"Content-Type": "application/json"}
    if tok: h["Authorization"] = "Bearer " + tok
    r = urllib.request.Request("http://127.0.0.1:8000" + path, json.dumps(body).encode(), h, method="POST")
    try:
        return json.loads(urllib.request.urlopen(r, timeout=30).read())
    except urllib.error.HTTPError as e:
        return {"http_error": e.code, "body": e.read().decode()[:300]}
t = call("/auth/login", {"username": "admin", "password": pw, "client_id": "meshbot-setup"})
tok = t.get("token") or (t.get("data") or {}).get("token")
if not tok:
    raise SystemExit("login failed: " + str({k: v for k, v in t.items() if "token" not in k}))
out = call("/api/create_identity", {"name": os.environ["NAME"], "type": "companion",
           "settings": {"node_name": os.environ["NAME"], "tcp_port": int(os.environ["PORT"]),
                        "bind_address": "127.0.0.1"}}, tok)
def scrub(o):
    if isinstance(o, dict): return {k: ("<redacted>" if "key" in k.lower() else scrub(v)) for k, v in o.items()}
    if isinstance(o, list): return [scrub(x) for x in o]
    return o
print(json.dumps(scrub(out), indent=1)[:1500])
PY
sleep 2
ss -ltn | grep -q "127.0.0.1:$PORT" && echo "OK: companion listening on 127.0.0.1:$PORT" || echo "WARN: nothing listening on $PORT yet"
