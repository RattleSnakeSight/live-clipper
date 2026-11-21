# Architecture

Live Clipper is one local Python app. Subsystems (monitor, select, edit, publish, web UI) plug into a small
core. This page is the contract between them.

## Process model

```
live-clipper start ──spawns──> supervisor ──spawns──> worker (uvicorn + all services)
                               (holds lock)            (writes heartbeat every 5 s)
```

- `live_clipper.supervisor` holds the single-instance lock `state/supervisor.lock` (`{"pid", "port"}`).
  A second `live-clipper start` sees a live lock and just opens the browser on the running instance.
- It restarts the worker when it exits (backoff 1 s doubling to 60 s, reset after 5 minutes up) or when
  `state/heartbeat` is older than 60 s (hung event loop). Each restart is a row in `events`
  (`source='supervisor'`).
- Stop: `live-clipper stop` creates `state/stop`; the supervisor creates `state/worker.stop`, the worker
  shuts down gracefully (services get `stop()`), and the supervisor exits. SIGINT/SIGTERM/CTRL_BREAK work too.
- `start --foreground` runs the supervisor in the terminal instead of in the background.

## Plug-in interface

Add one line to `live_clipper/registry.py`:

```python
SERVICES = ["live_clipper.monitor:MonitorService"]  # "module:factory", factory() returns a Service
```

A service implements `live_clipper.services.Service`:

```python
class MonitorService:
    name = "monitor"
    router: fastapi.APIRouter | None    # mounted as-is; use prefix "/api/monitor"
    async def start(self, ctx: AppContext) -> None: ...  # start background tasks; return quickly
    async def stop(self) -> None: ...                     # cancel tasks, close processes
    def status(self) -> dict: ...                          # cheap; shown in GET /api/status
```

A service that fails to import or start is logged and skipped; the rest of the app keeps running.

## AppContext (`ctx`)

| Field | What |
|---|---|
| `ctx.paths` | `Paths`: `root`, `db`, `db_file`, `media`, `logs`, `secrets`, `state` |
| `ctx.db` | `Database`: `conn()` (per-thread sqlite3 connection), `execute(sql, params)`, `tx()` (`with ctx.db.tx():` = `BEGIN IMMEDIATE`), `event(level, source, message, data=None)` |
| `ctx.settings` | `KV` over `settings_kv`: `get(key, default=None)`, `set(key, value)` (JSON values) |
| `ctx.app_state` | `KV` over `app_state` (wizard/tour flags, also at `GET/PUT /api/app-state/{key}`) |
| `ctx.secrets` | `Vault`: `get(name) -> dict|None`, `set(name, dict)`, `delete(name)`, `names()` |
| `ctx.bus` | `EventBus`: `publish(topic, payload)`, `subscribe(topic, handler) -> unsubscribe` |
| `ctx.notify(title, body)` | user notification; published as bus topic `app.notify` |
| `ctx.port`, `ctx.token`, `ctx.version`, `ctx.started_at`, `ctx.services` | runtime info |

## Event bus

In-process only. `handler(topic, payload)`; sync or `async def` (scheduled on the server loop, also when
published from another thread). Errors in handlers are logged, never raised to the publisher.

Topics are `<subsystem>.<event>`, owned by the publishing subsystem: `monitor.*`, `select.*`, `edit.*`,
`publish.*`, plus core `app.*` (`app.notify`). Subscribe to an exact topic, `prefix.*`, or `*`.
Payloads are JSON-serialisable dicts.

## Database and migrations

One SQLite file `db/live-clipper.sqlite3` (WAL, foreign keys on, 30 s busy timeout).

```python
from live_clipper.db import register_migrations
register_migrations("monitor", [
    "CREATE TABLE monitor_channels(id INTEGER PRIMARY KEY, url TEXT NOT NULL)",  # version 1
    my_python_migration,                                                         # version 2: fn(conn)
])
```

- Call at import time of your module. Namespace = your subsystem name; prefix your tables with it.
- Versions are list positions; only append, never edit or reorder a shipped migration.
- Each migration runs in its own transaction and is recorded in `schema_migrations(namespace, version)`.
- Core namespace `core` owns `settings_kv`, `app_state`, `events`.

## Logging and secrets

- `logging.getLogger(__name__)`; output goes to `logs/live-clipper.log` (worker) and
  `logs/supervisor.log`, rotated at 5 MB x 5, plus the console.
- A filter masks bearer tokens, `Authorization` headers, `access_token`, `refresh_token`, `client_secret`,
  `api_key`, `code`, `password` in query strings and JSON/dict fields, and any value passed to
  `live_clipper.logging_setup.register_secret(value)`. Every vault value is registered automatically.
- The vault is `secrets/vault.json.enc` (Fernet). Its key is in the OS keyring when the optional `keyring`
  package works, else `secrets/vault.key` (0600 on POSIX).

## Auth model

- The server binds `127.0.0.1` only; a non-loopback host is refused unless `LIVE_CLIPPER_ALLOW_REMOTE=1`.
- The supervisor generates a random token on each start (`state/token`). The browser opens
  `http://127.0.0.1:<port>/?t=<token>`, which sets an HttpOnly, SameSite=Strict cookie and redirects to `/`.
- Every `/api/*` route except `GET /api/health` needs that cookie or `Authorization: Bearer <token>`.
  Routers from services, HTTP and WebSocket, are covered automatically.
- `Host` must be `127.0.0.1:<port>` or `localhost:<port>` (DNS-rebinding protection).
- Non-GET/HEAD/OPTIONS requests with an `Origin` other than the server's own are refused.
- Static UI files from `live_clipper/web/` are served at `/` without auth; API docs at `/api/docs`.

Core endpoints: `GET /api/health` (status, version, uptime, pid), `GET /api/status` (plus per-service
`status()`), `GET/PUT /api/app-state/{key}` (body `{"value": <json>}`).

## Data dir

Outside the install, so updates keep it. Default is the per-user data dir from `platformdirs`
(e.g. `%LOCALAPPDATA%\live-clipper`, `~/Library/Application Support/live-clipper`,
`~/.local/share/live-clipper`); override with `LIVE_CLIPPER_HOME`.

```
db/        live-clipper.sqlite3
media/     recordings and clips (subsystems create their own subfolders)
logs/      live-clipper.log, supervisor.log
secrets/   vault.json.enc, vault.key (only without a keyring)
state/     token, supervisor.lock, heartbeat, stop / worker.stop (transient)
```
