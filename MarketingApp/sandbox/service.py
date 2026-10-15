"""Sandbox service: runs tools from installed packs for an agent that cannot start containers itself.

An agent deployed in a container usually has no Docker of its own, so ``ETHGENT_SANDBOX=auto``
would fall back to a plain subprocess next to the agent's files and environment. This service is
the alternative: run it as its own container beside the agent (no secrets, read-only filesystem,
no capabilities) and point the agent at it with ``ETHGENT_SANDBOX_URL`` and
``ETHGENT_SANDBOX_TOKEN``. Each call is then run here, as a subprocess with the same memory, CPU
and time limits as the ``subprocess`` mode.

    ETHGENT_SANDBOX_TOKEN=... python -m MarketingApp.sandbox.service

Settings (environment): ``ETHGENT_SANDBOX_TOKEN`` (required; the service will not start without
it), ``ETHGENT_SANDBOX_HOST`` (default ``0.0.0.0``), ``ETHGENT_SANDBOX_PORT`` (default ``8092``),
``ETHGENT_SANDBOX_MAX_CONCURRENT`` (default 4) and the limits ``ETHGENT_SANDBOX_TIMEOUT``,
``ETHGENT_SANDBOX_MEMORY_MB``. The limits are this service's, not the caller's: an agent cannot
raise them.

One request is ``POST /run`` with ``Authorization: Bearer <token>`` and a JSON body
``{"source", "filename", "function", "args", "positional", "approved", "env"}``. The answer has
the same shape as a local run: ``{"ok": true, "result": ...}`` or ``{"ok": false, "error": ...}``.
``GET /health`` answers without a token.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import executor

MAX_BODY_BYTES = 2 * 1024 * 1024
_FILENAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,80}\.py$")
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,120}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,120}$")


class BadRequest(ValueError):
    pass


def _validate(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise BadRequest("body must be a JSON object")
    source, filename, function = body.get("source"), body.get("filename"), body.get("function")
    if not isinstance(source, str) or not source.strip():
        raise BadRequest("source is required")
    if not isinstance(filename, str) or not _FILENAME.match(filename):
        raise BadRequest("filename must be a plain name ending in .py")
    if not isinstance(function, str) or not _IDENTIFIER.match(function):
        raise BadRequest("function must be a Python identifier")
    args = body.get("args") or {}
    positional = body.get("positional") or []
    env = body.get("env") or {}
    if not isinstance(args, dict) or not isinstance(positional, list):
        raise BadRequest("args must be an object and positional a list")
    if not isinstance(env, dict) or not all(
        isinstance(k, str) and _ENV_NAME.match(k) and isinstance(v, str) for k, v in env.items()
    ):
        raise BadRequest("env must map variable names to strings")
    return {
        "source": source,
        "filename": filename,
        "function": function,
        "args": args,
        "positional": positional,
        "approved": body.get("approved") is True,
        "env": env,
    }


def run_request(request: dict[str, Any], conf: dict[str, Any]) -> dict[str, Any]:
    """Write the tool to a temporary folder and run it with the local subprocess sandbox."""
    with tempfile.TemporaryDirectory(prefix="ethgent-svc-") as folder:
        tool_file = Path(folder) / request["filename"]
        tool_file.write_text(request["source"], encoding="utf-8")
        env = request["env"]
        outcome = executor.run_tool(
            tool_file,
            request["function"],
            request["args"],
            list(env),
            env,  # only what the agent sent: names missing here are not looked up in this process
            positional=request["positional"],
            approved=request["approved"],
            conf={**conf, "mode": "subprocess"},
        )
    return outcome


def build_server(
    host: str,
    port: int,
    token: str,
    *,
    max_concurrent: int = 4,
    conf: dict[str, Any] | None = None,
    busy_wait_seconds: float = 30.0,
) -> ThreadingHTTPServer:
    if not token:
        raise ValueError("a token is required")
    conf = conf or executor.settings()
    slots = threading.BoundedSemaphore(max(1, max_concurrent))
    expected = f"Bearer {token}".encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        server_version = "EthgentSandbox/1"

        def log_message(self, fmt: str, *args: Any) -> None:  # one short line per call, no bodies
            sys.stderr.write(f"[sandbox-service] {self.address_string()} {fmt % args}\n")

        def _send(self, code: int, payload: dict[str, Any]) -> None:
            data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                self._send(200, {"ok": True})
            else:
                self._send(404, {"ok": False, "error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/run":
                self._send(404, {"ok": False, "error": "not found"})
                return
            given = (self.headers.get("Authorization") or "").encode("utf-8")
            if not hmac.compare_digest(given, expected):
                self._send(401, {"ok": False, "error": "unauthorized"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length <= 0 or length > MAX_BODY_BYTES:
                self._send(413, {"ok": False, "error": f"body must be 1..{MAX_BODY_BYTES} bytes"})
                return
            try:
                request = _validate(json.loads(self.rfile.read(length).decode("utf-8")))
            except (BadRequest, ValueError) as exc:
                self._send(400, {"ok": False, "error": str(exc)})
                return
            if not slots.acquire(timeout=busy_wait_seconds):
                self._send(503, {"ok": False, "error": "sandbox service is busy"})
                return
            try:
                outcome = run_request(request, conf)
            except executor.SandboxError as exc:
                self._send(500, {"ok": False, "error": str(exc)})
                return
            finally:
                slots.release()
            self._send(200, outcome)

    return ThreadingHTTPServer((host, port), Handler)


def main() -> int:
    token = (os.getenv("ETHGENT_SANDBOX_TOKEN") or "").strip()
    if not token:
        print("ETHGENT_SANDBOX_TOKEN is not set; the sandbox service does not start without it.", file=sys.stderr)
        return 2
    host = os.getenv("ETHGENT_SANDBOX_HOST") or "0.0.0.0"
    port = int(os.getenv("ETHGENT_SANDBOX_PORT") or 8092)
    max_concurrent = int(os.getenv("ETHGENT_SANDBOX_MAX_CONCURRENT") or 4)
    server = build_server(host, port, token, max_concurrent=max_concurrent)
    print(f"[sandbox-service] listening on {host}:{port} (max {max_concurrent} at a time)", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
