# Architecture

Live Clipper is one local Python app. Subsystems (monitor, select, edit, publish, web UI) plug into a small
core. This page is the contract between them. For a map of which module does what, see "Where to change
what" in [CONTRIBUTING.md](../CONTRIBUTING.md); the subsystem contracts are in [CONTRACTS.md](CONTRACTS.md).

The pipeline in one line: the monitor records a live channel into a rolling buffer and reads its chat; the
selector turns chat bursts, speech and the picture into scored moments; the editor cuts, captions and renders
the clip; the publisher delivers it to each destination, or holds it for review. The browser UI talks to all of
them through the local API.

## Process model

```
live-clipper start ──spawns──> supervisor ──spawns──> worker (uvicorn + all services)
                               (holds lock)            (writes heartbeat every 5 s)
```

- `live_clipper.supervisor` holds an OS lifetime lock on `state/supervisor.guard`. This file is never
  deleted; the OS releases ownership when the supervisor exits, including after a crash.
  `state/supervisor.lock` holds discovery metadata (`pid`, `port`, `lock_version`, `instance`), not permission
  to signal a process. A leftover metadata file cannot claim a reused PID. A second start opens the existing
  healthy instance, or reports that its owner is not responding.
- The worker separately holds `state/worker.guard` and writes `state/worker.lock`. If its supervisor crashes,
  this guard prevents a second worker from using the data folder. Start reports the surviving worker; stop
  requests its shutdown through `worker.stop`, after which a normal start restores supervision.
- Older PID-only records are recognized through authenticated local status. A process born after the old
  record was written, or a record from before this boot, is stale. Ambiguous legacy ownership stops startup with an error;
  it never triggers a second instance or a guessed process kill.
- It restarts the worker when it exits (backoff 1 s doubling to 60 s, reset after 5 minutes up) or when
  `state/heartbeat` is older than 60 s (hung event loop). Each restart is a row in `events`
  (`source='supervisor'`).
- Stop: `live-clipper stop` creates `state/stop`; the supervisor creates `state/worker.stop`, the worker
  shuts down gracefully (services get `stop()`), and the supervisor exits. SIGINT/SIGTERM/CTRL_BREAK work too.
  The supervisor can kill its own unresponsive worker through its retained process handle. If the supervisor
  itself fails to stop, the CLI returns failure and keeps its lock; it never kills by PID or image name.
  Update, uninstall and version-change restart abort when that stop fails.
- `start --foreground` runs the supervisor in the terminal instead of in the background.
- Update gate (`live_clipper/update_gate.py`): the worker checks the version line on the project's main
  branch before anything else. Only services marked `essential = True` (the heartbeat) start at once; the
  rest start when the check says this copy is current. While it is `checking`, `pending` or `unknown`, the
  Guard answers every `/api/*` request with 423 except health, status, `/api/update*`, app-state reads and
  the API reference, and closes websockets. A release found while the services run makes the worker exit
  with code 75; the supervisor restarts it at once (not counted as a crash) and the new worker starts gated.
  A second `live-clipper start` asks the running app to check (`POST /api/update/check`), and restarts it
  if the running version differs from the installed one (an upgrade while it ran).

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
Services start only once the update gate is open (see Process model); until then `GET /api/status`
reports them as `{"state": "paused", "reason": "update check"}`.

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
- Static UI files from `live_clipper/web/` are served at `/` without auth.
- The API reference is at `/api/docs` and its source, the OpenAPI document, at `/api/openapi.json`; both need
  the token. The reference is a local page (`web/api-docs.html`, `web/js/api-docs.js`) served with a
  `Content-Security-Policy` that allows this app's own files only. FastAPI's built-in docs page is off because
  it loads its viewer from a public CDN.

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
state/     token, supervisor.guard, supervisor.lock, worker.guard, worker.lock, heartbeat, stop / worker.stop (transient)
```
