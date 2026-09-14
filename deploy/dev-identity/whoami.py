"""A whoami endpoint for the local Compose stack, and nothing else.

The token IS the identity: whatever the caller sends comes back as the user
id. Groups follow a `+`, so `alice+sre+platform` is alice in groups sre and
platform, and `MEMORY_MASTER_USERS=alice` makes that same alice an operator.

NEVER run this anywhere real. It authenticates everybody, by construction.
"""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HEADER = os.environ.get("RESOLVER_HEADER", "authorization")
PORT = int(os.environ.get("PORT", "8080"))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        raw = (self.headers.get(HEADER) or "").strip()
        # A Bearer prefix may or may not be present; strip it either way.
        token = raw.split(" ", 1)[-1].strip() if raw else ""
        if not token:
            self.send_error(401, "no credential")
            return
        user, *groups = token.split("+")
        self._json({"user_id": user, "groups": groups})

    def _json(self, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        # The token rides a header this would otherwise print on error.
        return


if __name__ == "__main__":
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
