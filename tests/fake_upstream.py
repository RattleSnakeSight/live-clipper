"""A fake upstream for the update check: serves a version file over loopback, with whatever the test sets.

`FakeUpstream().version = "0.4.0"` changes the answer; `.mode` makes it misbehave ("429", "500", "slow",
"huge", "garbage", "drop"). `.hits` counts requests; ETag and If-None-Match work like the real host's."""

import hashlib
import http.server
import threading
import time


class FakeUpstream:
    def __init__(self, version: str):
        self.version, self.mode, self.hits, self.not_modified = version, "", 0, 0
        up = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                up.hits += 1
                if up.mode == "drop":
                    self.connection.close()
                    return
                if up.mode in ("429", "500"):
                    self.send_response(int(up.mode))
                    self.end_headers()
                    return
                if up.mode == "slow":
                    time.sleep(12)
                body = {"huge": b"#" * 100_000, "garbage": b"<html>not a version</html>"}.get(
                    up.mode, f'__version__ = "{up.version}"\n'.encode())
                etag = '"' + hashlib.sha1(body).hexdigest() + '"'
                if self.headers.get("If-None-Match") == etag:
                    up.not_modified += 1
                    self.send_response(304)
                    self.send_header("ETag", etag)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("ETag", etag)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/live_clipper/__init__.py"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
