"""Local fake video provider ("sandbox") used as the default test destination.

Threaded HTTP server on 127.0.0.1 with persistent JSON state. It behaves like a small real provider:
OAuth (authorization code + PKCE S256, refresh tokens), bearer auth, upload, processing delay,
publish states, list-by-tag, and failure injection for exercising the publisher.

  GET  /oauth/authorize?redirect_uri&state&code_challenge   -> 302 back with ?code (auto-consent)
  POST /oauth/token  grant_type=authorization_code|refresh_token
  GET  /v1/me                         -> {account, scopes}
  POST /v1/videos?title&tag&privacy   body = video bytes -> 201 {id, status}
  GET  /v1/videos/<id>                -> {id, status: processing|published|private|draft, url, tag}
  GET  /v1/videos?tag=<tag>           -> {items: [...]}
  POST /_admin/inject {op: upload|status|list|me|token, fail: timeout_after_accept|timeout|429|401|500,
                       times: 1, retry_after: 2, hang: 1.0}
  POST /_admin/expire_tokens          POST /_admin/issue_token -> {access_token, refresh_token}
  POST /_admin/config {processing_delay}   GET /_admin/state
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets as _rnd
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse


class SandboxServer:
    def __init__(self, state_path: Path | str, host: str = "127.0.0.1", port: int = 0,
                 processing_delay: float = 0.5):
        self.state_path = Path(state_path)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.state = {"tokens": {}, "refresh": {}, "codes": {}, "videos": {}, "inject": [], "seq": 0,
                      "processing_delay": processing_delay}
        if self.state_path.exists():
            self.state.update(json.loads(self.state_path.read_text()))
        self.httpd = ThreadingHTTPServer((host, port), _Handler)
        self.httpd.daemon_threads = True
        self.httpd.sb = self
        self.thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        h, p = self.httpd.server_address[:2]
        return f"http://{h}:{p}"

    def start(self) -> SandboxServer:
        self.thread = threading.Thread(target=self.httpd.serve_forever, args=(0.05,), name="live-clipper-sandbox", daemon=True)
        self.thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    # ---- state -----------------------------------------------------------
    def save(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state))
        os.replace(tmp, self.state_path)

    def issue(self) -> dict:
        with self.lock:
            at, rt = "sbx_at_" + _rnd.token_urlsafe(16), "sbx_rt_" + _rnd.token_urlsafe(16)
            self.state["tokens"][at] = True
            self.state["refresh"][rt] = at
            self.save()
            return {"access_token": at, "refresh_token": rt, "expires_in": 3600, "token_type": "Bearer",
                    "scope": "video.upload video.publish"}

    def take_fault(self, op: str) -> dict | None:
        with self.lock:
            for f in self.state["inject"]:
                if f.get("op") == op and f.get("times", 1) > 0:
                    f["times"] = f.get("times", 1) - 1
                    self.state["inject"] = [x for x in self.state["inject"] if x.get("times", 1) > 0]
                    self.save()
                    return f
        return None

    def video_view(self, v: dict) -> dict:
        ready = time.time() >= v["ready_at"]
        status = v["privacy"] if ready else "processing"
        status = {"public": "published"}.get(status, status)
        return {"id": v["id"], "title": v["title"], "tag": v["tag"], "status": status, "size": v["size"],
                "url": f"{self.url}/watch/{v['id']}" if ready else "", "created_at": v["created_at"]}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # quiet
        pass

    @property
    def sb(self) -> SandboxServer:
        return self.server.sb

    def _send(self, code: int, body=None, headers: dict | None = None) -> None:
        data = json.dumps(body if body is not None else {}).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for k, v in (headers or {}).items():
                self.send_header(k, str(v))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _authed(self) -> bool:
        tok = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        if not self.sb.state["tokens"].get(tok):
            self._send(401, {"error": "token_expired", "message": "access token invalid or expired"})
            return False
        return True

    def _fault(self, op: str, accepted: bool = False) -> bool:
        """Apply an injected failure. Returns True if the request was answered (or abandoned)."""
        f = self.sb.take_fault(op)
        if not f:
            return False
        kind = str(f.get("fail"))
        if kind in ("timeout", "timeout_after_accept"):
            time.sleep(float(f.get("hang", 1.0)))
            self.close_connection = True
            return True
        if kind == "429":
            self._send(429, {"error": "rate_limited"}, {"Retry-After": f.get("retry_after", 2)})
        elif kind == "401":
            self._send(401, {"error": "token_expired"})
        else:
            self._send(500, {"error": "internal"})
        return True

    def _peek_fault(self, op: str, kind: str) -> bool:
        return any(f.get("op") == op and f.get("fail") == kind for f in self.sb.state["inject"])

    def do_GET(self):
        u = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(u.query).items()}
        sb = self.sb
        if u.path == "/oauth/authorize":
            code = "sbx_code_" + _rnd.token_urlsafe(12)
            with sb.lock:
                sb.state["codes"][code] = {"challenge": qs.get("code_challenge", ""), "t": time.time()}
                sb.save()
            loc = qs["redirect_uri"] + "?" + urlencode({"code": code, "state": qs.get("state", "")})
            self.send_response(302)
            self.send_header("Location", loc)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if u.path == "/_admin/state":
            return self._send(200, sb.state)
        if not self._authed():
            return
        if u.path == "/v1/me":
            if self._fault("me"):
                return
            return self._send(200, {"account": "sandbox-account", "scopes": ["video.upload", "video.publish"]})
        if u.path == "/v1/videos":
            if self._fault("list"):
                return
            items = [sb.video_view(v) for v in sb.state["videos"].values()
                     if not qs.get("tag") or v["tag"] == qs["tag"]]
            return self._send(200, {"items": items})
        if u.path.startswith("/v1/videos/"):
            if self._fault("status"):
                return
            v = sb.state["videos"].get(u.path.rsplit("/", 1)[1])
            return self._send(200, sb.video_view(v)) if v else self._send(404, {"error": "not_found"})
        self._send(404, {"error": "not_found"})

    def do_POST(self):
        u = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(u.query).items()}
        sb = self.sb
        body = self._body()
        if u.path == "/oauth/token":
            if self._fault("token"):
                return
            f = {k: v[0] for k, v in parse_qs(body.decode()).items()}
            with sb.lock:
                if f.get("grant_type") == "authorization_code":
                    c = sb.state["codes"].pop(f.get("code", ""), None)
                    ok = c is not None and (not c["challenge"] or c["challenge"] == base64.urlsafe_b64encode(
                        hashlib.sha256(f.get("code_verifier", "").encode()).digest()).rstrip(b"=").decode())
                elif f.get("grant_type") == "refresh_token":
                    old = sb.state["refresh"].pop(f.get("refresh_token", ""), None)
                    ok = old is not None
                    if ok:
                        sb.state["tokens"].pop(old, None)
                else:
                    ok = False
                if not ok:
                    sb.save()
                    return self._send(400, {"error": "invalid_grant"})
            return self._send(200, sb.issue())
        if u.path.startswith("/_admin/"):
            data = json.loads(body or b"{}")
            with sb.lock:
                if u.path == "/_admin/inject":
                    sb.state["inject"].append({"times": 1, **data})
                elif u.path == "/_admin/expire_tokens":
                    sb.state["tokens"] = {k: False for k in sb.state["tokens"]}
                elif u.path == "/_admin/issue_token":
                    return self._send(200, sb.issue())
                elif u.path == "/_admin/config":
                    sb.state.update({k: v for k, v in data.items() if k in ("processing_delay",)})
                sb.save()
            return self._send(200, {"ok": True})
        if not self._authed():
            return
        if u.path == "/v1/videos":
            if not self._peek_fault("upload", "timeout_after_accept") and self._fault("upload"):
                return
            if not body:
                return self._send(400, {"error": "empty upload"})
            with sb.lock:
                sb.state["seq"] += 1
                vid = f"v{sb.state['seq']:05d}"
                now = time.time()
                sb.state["videos"][vid] = {"id": vid, "title": qs.get("title", ""), "tag": qs.get("tag", ""),
                                           "privacy": qs.get("privacy", "public"), "size": len(body),
                                           "sha256": hashlib.sha256(body).hexdigest(), "created_at": now,
                                           "ready_at": now + float(sb.state["processing_delay"])}
                sb.save()
            if self._fault("upload", accepted=True):     # timeout_after_accept: stored, client never hears back
                return
            return self._send(201, sb.video_view(sb.state["videos"][vid]))
        self._send(404, {"error": "not_found"})


_SINGLETON: SandboxServer | None = None
_SLOCK = threading.Lock()


def ensure_sandbox(state_dir) -> SandboxServer:
    """Process-wide local test provider (state under <state_dir>/state.json). Nothing leaves this machine."""
    global _SINGLETON
    with _SLOCK:
        want = Path(state_dir) / "state.json"
        if _SINGLETON is not None and _SINGLETON.state_path != want:
            _SINGLETON.stop()
            _SINGLETON = None
        if _SINGLETON is None:
            _SINGLETON = SandboxServer(want).start()
        return _SINGLETON


def shutdown_sandbox() -> None:
    global _SINGLETON
    with _SLOCK:
        if _SINGLETON is not None:
            _SINGLETON.stop()
            _SINGLETON = None
