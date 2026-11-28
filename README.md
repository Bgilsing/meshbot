<p align="center"><img src="app/web/logo.svg" alt="meshbot" width="360"></p>

# meshbot: an AI on MeshCore channels

meshbot is an AI assistant for [MeshCore](https://github.com/meshcore-dev/MeshCore) LoRa mesh networks. It joins one or more
channels through an [openHop](https://openhop.dev/) companion identity, reads every message, and lets an LLM decide
from the conversation whether to reply. Replies are compressed to fit mesh packets, split into numbered parts when
an answer needs more room. A web control panel lets you tune everything (prompts, models, limits, channels, tools)
while it runs.

```
LoRa radio <-> openHop repeater --(companion protocol, TCP 127.0.0.1:5070)--> meshbot --(OpenAI API)--> LiteLLM / LLM
```

## Features
- **AI judgement on when to talk.** The model sees the recent thread and decides whether to respond. Eagerness,
  prompts and per-channel notes are editable, and **shadow mode** lets a channel decide and log without
  transmitting.
- **Mesh-sized answers.** Byte budgets in UTF-8 include the `Name: ` prefix and ` 1/3` markers. Answers that are
  too long get one shorten re-ask, then a clean cut.
- **Hashtag and private channels**, with join QR codes (`meshcore://channel/add`) for the MeshCore app.
- **Direct messages:** people can DM the bot. Every DM gets an answer, each sender has their own thread, and DMs
  have their own settings (mode, models, tools, caps).
- **Adverts** every 48 h (configurable), from a Dashboard button, or when a mesh user asks for one, so nodes can
  find the bot and DM it.
- **Model failover.** Each channel has a primary model (e.g. a fast cloud model) and a fallback (e.g. a local
  model). If the primary is unreachable, the fallback answers and is told the internet may be down.
- **Tools**, switched per channel:
  - web search (via a LiteLLM MCP server such as Brave) plus reading pages
  - weather (Open-Meteo, plus US NWS alerts)
  - amateur callsign lookup: first name, last initial and city/state only (QRZ, or free US FCC data)
  - mesh data: nodes, last heard, hops, neighbors and network activity, from
    [CoreScope](https://github.com/kpa-clawbot/corescope) and openHop
  - radio queries: guest login to a repeater for its neighbors or status, and live path discovery
  - Home Assistant (read or control, private channels only)
  - send an advert on request (rate-limited)
- **Web UI** with a live message log, a decision log with replay against new prompts, a playground, a prompt editor,
  channel management, and settings, including secrets.
- **Guards** against bot loops and spam: cooldowns, hourly caps per channel and per sender, an ignore list, and a
  global transmit kill switch.

## Requirements
- Docker with Compose v2.
- An [openHop](https://openhop.dev/) repeater on the same host, with a companion identity for the bot. openHop owns
  the radio, and meshbot talks to it over the companion protocol.
- An OpenAI-compatible LLM endpoint. [LiteLLM](https://github.com/BerriAI/litellm) is recommended, because web
  search and Home Assistant are reached through its MCP servers.
- Optional:
  - CoreScope (mesh data tools)
  - an openHop API token (your repeater's own stats)
  - Home Assistant's MCP server registered in LiteLLM

## Setup
1. **Create the companion** in openHop. One port serves one client, so give the bot its own:
   `./scripts/create_companion.sh MeshBot 5070`. Set `OPENHOP_CONTAINER` if your container isn't called
   `openhop-repeater`. You can also create it in openHop's web UI as a companion bound to 127.0.0.1:5070.
2. **Configure the host:** `cp .env.example .env`, then set `TZ` and `WEB_BIND`. `WEB_BIND` is the address the UI
   listens on; use the host's LAN IP to reach it from other machines.
3. **Start it:** `docker compose up -d --build`. On first start it creates `conf/config.yaml` and
   `conf/prompts/system.md` from `app/defaults/`.
4. **Log in:** `cat data/initial_password.txt` shows the generated web password. Open `http://<WEB_BIND>:8093` and
   log in as `admin`.
5. **Settings tab:**
   - Set the LLM endpoint, model and API key.
   - Change the web password (this deletes `initial_password.txt`).
   - Set the path hash size (1, 2 or 3 bytes) to match your network.
6. **Channels tab:** add channels. New channels start in shadow mode; watch the Live and Decisions tabs, then
   switch a channel to `live`.

## Web UI
| Tab | What it's for |
|---|---|
| Dashboard | Transmit kill switch; companion, model and mesh-data health; per-channel and DM modes; send an advert; send a message by hand |
| Live | Every message heard and sent. The badge shows what the bot decided; click it to see why |
| Decisions | Every model call, filterable. Replay up to 20 against the current or a draft prompt |
| Playground | Try a made-up conversation: the decision, the packets with byte counts, the tools used, the exact prompt |
| Prompts | System prompt and per-channel notes; drafts can be tested before saving |
| Channels | Channels, slots, private secrets (set or generate), join QR codes, per-channel overrides |
| Settings | Secrets (API keys, web password, guest passwords), models, tools, mesh data, direct messages, adverts, Home Assistant, defaults |
| Advanced | Raw config.yaml editor (validated before saving) and the live log |

Changes apply on the next message, without a restart. Changing channels or the companion settings reconnects
the companion on its own.

## Files
- `conf/config.yaml` and `conf/prompts/`: your configuration. Not tracked by git. The UI edits them and keeps the
  comments in config.yaml.
- `data/secrets.json` (mode 600): API keys, repeater guest passwords, the hashed web password and private channel
  keys set from the UI.
- `data/history.db`: messages and decisions. `data/decisions.jsonl` is a copy of the decisions for `jq`.
- `data/state.json`: when the last advert went out.
- `.env`: host settings, plus optional `LITELLM_API_KEY`, `OPENHOP_API_KEY`, `WEB_PASSWORD` and `secret_env`
  values. Anything set in the UI wins.
- `app/schema.py`: every setting, declared once. The UI forms and validation are generated from it, so adding a
  setting is one `F(...)` line.

CLI replay: `docker compose exec meshbot python -m app.replay --last 20 --changed`

## How it behaves
- **Debounce:** a burst of messages becomes one LLM call (`debounce_s`). If a new message arrives while the model is
  thinking, that answer is dropped and the thread is evaluated again.
- **Backlog:** messages the companion had queued are stored as context on (re)connect. They never trigger replies.
- **No loops:** the bot never replies to itself or to `ignore_senders` (put other bots there).
- **Hop counts:** the thread shows each sender's hop count, so "how many hops am I?" needs no tool.
- **Renaming:** change Bot name in Settings, then press "Rename companion" on the Dashboard so the companion matches.
- **One client per companion port:** never point a second meshbot (a test copy) at the same companion port. It
  knocks the running bot off and writes its own channels into the companion's slots.

### Direct messages
- DMs arrive through the same companion. Each sender is a conversation named `@<their node name>`, shown in Live,
  Decisions and the Playground. Settings > Direct messages has its own mode (starts in `shadow`), models, tools and
  caps. Home Assistant is never offered in DMs.
- The bot always answers a DM. If the model chooses silence, it is asked once more. The kill switch, shadow mode,
  `ignore_senders`, cooldown and hourly caps still apply.
- Replies wait for the recipient's ACK, retrying and then flooding. A reply that is never acknowledged is logged as
  `send_failed`.
- MeshCore can only decrypt a DM from a node whose advert the companion has heard, and the sender needs the bot's
  advert to DM it. Adverts take care of the second part.
- Prompt notes for every DM conversation live in Prompts > "Notes for direct messages".

### Adverts
- **Scheduled:** every `advert.interval_h` hours (default 48, flood). The time of the last advert is kept in
  `data/state.json`, so restarts don't send extra ones. The kill switch pauses the schedule.
- **On request:** the `send_advert` tool, switched per channel and for DMs with "Advert on request". It is limited to
  one per `request_min_interval_min`, and in shadow mode or the Playground it only reports what it would do.
- **Dashboard:** "Send advert" sends one right away and skips the request limit.

### Failover
Blank model fields use Settings > Model, and `none` turns a channel's fallback off. The primary gets
`primary_timeout_s`. If it fails with a service error, it's skipped for `failover_hold_s`, so an outage doesn't add
a timeout to every message. "Test models" on the Dashboard ends a hold early.

### Tools
- **Web search + read pages:** `web_search` calls a LiteLLM MCP search tool (`/mcp-rest/tools/call`; the server_id is
  looked up automatically). `read_page` extracts a page's readable text. It only fetches public internet addresses:
  LAN, localhost and other private IPs are refused, including through DNS or redirects, so mesh users can't use it
  to probe your network.
- **Weather:** Open-Meteo forecast plus active NWS alerts (US). Accepts a ZIP or a place, and defaults to
  `weather_location`.
- **Callsign lookup** (`app/callsign.py`, on by default):
  - Sources: QRZ.com's XML service when a QRZ login is set in Settings > Secrets (worldwide), otherwise free US FCC
    data from callook.info.
  - Returns only the licensee's first name, last initial and city/state, plus country outside the US; club stations
    get the club name. Street address, email, coordinates and license details are dropped in code, so the model
    never sees them.
  - The prompt makes mesh data the default answer to "who is" questions. Personal details are given only when
    someone asks for them.
- **Mesh data** (`app/meshdata.py`): `mesh_node`, `mesh_nodes`, `mesh_overview`, `mesh_hops`, `repeater_status`,
  `repeater_neighbors`.
  - Sources: CoreScope's REST API (channel message text is never read), the companion's route and advert-path
    tables, and openHop's API for your own repeater.
  - openHop tokens are full-admin, so only a fixed list of read-only GET endpoints is ever called.
- **Radio queries** (needs Mesh data):
  - Naming a repeater makes the bot log in to it as guest over the air, adding it as a contact if needed.
    `mesh_hops live=true` runs a path discovery.
  - Every request (login, neighbours, status, path discovery) is retried up to `radio_attempts` times, since the
    mesh drops packets. Logins always flood with a freshly reset path; other requests flood on their last try.
  - Capped by `radio_max_per_hour`. Results are cached per repeater, and a failed query falls back to the network
    map.
- **Home Assistant** (`app/hass.py`), per channel: `off` (default), `read` or `control`.
  - **Which tools:** tools come from the Home Assistant MCP server registered in LiteLLM. Only allowlisted tools are
    offered (`read_tools`, plus `control_tools` in control mode), so admin tools such as restart, backups and
    deletes stay hidden unless you add them. Optional `blocked_services` fence off things like `lock.unlock`.
  - **Who can use it:** private channels only. Hashtag channels never get access, because anyone can join them by
    name.
  - **Access control:** the channel key is the real access control, because MeshCore sender names can be faked. In
    control mode the model treats everyone on the channel as authorized.
  - **Visibility:** device changes show as ACTION in "Tools used". Settings > Home Assistant lists the tools without
    calling any.

## Tests
```
docker compose build
docker run --rm -v "$PWD/app:/srv/app:ro" -e PYTHONPATH=/srv -w /srv meshbot:local \
  python -m pytest -q -p no:cacheprovider app/tests
```

## License
MIT, see [LICENSE](LICENSE).
