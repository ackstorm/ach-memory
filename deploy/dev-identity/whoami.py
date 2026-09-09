"""A whoami endpoint for the local Compose stack, and nothing else.

ach-memory issues no credentials, so a stack with no LiteLLM and no ACH in
front of it has no identity source at all and cannot be driven. This is a
deployment artifact that answers the shape `auth/providers/platform.py`
already resolves against -- it is NOT a third auth provider, there is no
`if dev:` branch anywhere in the service, and nothing in the codebase knows
this file exists. Deleting it removes a container, not a code path.

The token IS the identity: whatever the caller sends comes back as the user
id, so two shells with two different values are two different people with two
different banks -- which is what the smoke and e2e scripts need now that they
cannot mint one key per user. Groups follow a `+`, so `alice+sre+platform`
is alice in the groups sre and platform, and

    MEMORY_MASTER_USERS=alice

makes that same alice an operator.

NEVER run this anywhere real. It authenticates everybody, by construction.
"""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Whichever header ach-memory was configured to forward the token on
# (MEMORY_AUTH_PLATFORM_RESOLVER_HEADER). Matched case-insensitively by
# http.client's header store, so the spelling here does not matter.
HEADER = os.environ.get("RESOLVER_HEADER", "authorization")
PORT = int(os.environ.get("PORT", "8080"))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        raw = (self.headers.get(HEADER) or "").strip()
        # `platform._platform_token` already strips a Bearer prefix before
        # forwarding, but a curl run by hand may not have.
        token = raw.split(" ", 1)[-1].strip() if raw else ""
        if not token:
            # 401 is one of the three codes the platform provider treats as a
            # refusal rather than a backend outage.
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
        # The request line is path-only, but the token rides a header this
        # class would happily print on an error. Silence is cheaper than
        # filtering.
        return


if __name__ == "__main__":
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
