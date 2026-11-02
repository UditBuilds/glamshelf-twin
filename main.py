"""
Glam Shelf Twin — FastAPI shell (PR A of the Flask -> FastAPI move).

A FastAPI app that mounts the existing Flask app (app.py) at "/" through
a2wsgi, so Render can serve the Twin with uvicorn. Every request goes to
Flask, unchanged, except a plain GET/HEAD /healthz (below).

Render start command:
    uvicorn main:app --host 0.0.0.0 --port $PORT --workers 1 --no-access-log
Rollback (the command this replaces):
    gunicorn app:app --workers 1 --timeout 60

What gunicorn did that uvicorn + a2wsgi don't do on their own, and what
this file does about it:

  - ONE request at a time. gunicorn's sync worker ran Flask strictly one
    request after another, and app.py's in-memory state (the dedup set,
    rate limits, the vision-context register) relies on that. a2wsgi
    would run Flask on 10 threads; FLASK_THREADS keeps it at 1. Exactly
    one uvicorn worker too (--workers 1, explicit: uvicorn otherwise reads
    WEB_CONCURRENCY), because importing app.py starts the background
    threads, the RAG model load and the hourly loop.
  - Header names with an underscore were dropped (gunicorn's
    header_map="drop"). a2wsgi would map "CF_Connecting_IP" onto the same
    WSGI key as "CF-Connecting-IP" and join the two values, and a client
    could then get around the per-IP login limit (app._client_ip).
    GunicornParity drops them.
  - No access log. uvicorn's access log prints each path with its query
    string, and the WATI token is part of the URL path
    (/webhook/<token>). --no-access-log, and the logger is switched off
    below as well, so a start command without the flag still logs nothing.
  - --timeout 60 killed the worker when one request ran past 60s. Nothing
    kills a request here. GunicornParity only logs one that is still
    running SLOW_REQUEST_SECONDS after it arrived (observe-only), naming
    its first path segment and never the rest of the path or the query.

/healthz and Render's health check. Render gives each check 5s, stops
routing traffic after 15s of failed checks and restarts the service after
60s. Through the single Flask thread, /healthz would wait behind every DM
in progress, so one slow DM would make Render pull traffic: slow is not
broken. So PlainHealthz answers a plain GET/HEAD /healthz (no
X-Dashboard-Key header, no query string) itself, without waiting for
Flask, with the public answer app.healthz gives: 200 {"status": "ok"}, or
503 {"status": "error"} when the DB can't be read, and also 503 when one
Flask request has held the Flask thread for more than HUNG_FLASK_SECONDS.
That one is timed from when the Flask thread picked it up (FlaskClock),
not from when it arrived: a DM waiting behind another slow DM isn't hung.
A real hang then still gets the service restarted by Render; slow DMs
never do. The keyed /healthz (full diagnostics) still goes to Flask.
"""
import asyncio
import itertools
import json
import logging
import re
import sqlite3
import threading
import time

from a2wsgi import WSGIMiddleware
from fastapi import FastAPI
from starlette.concurrency import run_in_threadpool
from werkzeug.wsgi import ClosingIterator

import app as twin   # app.py's startup (DB, threads, RAG, graph) runs here, once

logging.getLogger("uvicorn.access").disabled = True

FLASK_THREADS = 1
SLOW_REQUEST_SECONDS = 60
HUNG_FLASK_SECONDS = 55

_UNSAFE_LOG_CHARS = re.compile(r"[^A-Za-z0-9._~-]")


def _first_segment(path: str) -> str:
    """"/webhook/<token>?x=1" -> "/webhook". The rest of a path can be a
    secret, so it never reaches the logs; odd characters become "?" so a
    path can't forge a log line."""
    segment = (path or "").lstrip("/").split("/", 1)[0]
    return "/" + _UNSAFE_LOG_CHARS.sub("?", segment)[:40]


class FlaskClock:
    """WSGI wrapper around the Flask app that knows how long the Flask
    request in progress has been running: timed from when the Flask thread
    picked it up, until its response is closed."""

    def __init__(self, app, clock=time.monotonic):
        self.app = app
        self.clock = clock
        self._ids = itertools.count()
        self._running: dict[int, tuple[float, str]] = {}
        self._lock = threading.Lock()

    def __call__(self, environ, start_response):
        key = next(self._ids)
        label = f"{environ.get('REQUEST_METHOD', '?')} {_first_segment(environ.get('PATH_INFO', ''))}"
        with self._lock:
            self._running[key] = (self.clock(), label)
        try:
            body = self.app(environ, start_response)
        except BaseException:
            self._finish(key)
            raise
        return ClosingIterator(body, lambda: self._finish(key))

    def _finish(self, key: int) -> None:
        with self._lock:
            self._running.pop(key, None)

    def longest_running(self) -> tuple[float, str]:
        """(seconds, "METHOD /first-segment") of the Flask request running
        longest, or (0.0, "") when Flask is idle."""
        with self._lock:
            if not self._running:
                return 0.0, ""
            started, label = min(self._running.values())
        return self.clock() - started, label


def _is_plain_healthz(scope) -> bool:
    return (
        scope.get("method") in ("GET", "HEAD")
        and scope.get("path") == "/healthz"
        and not scope.get("query_string")
        and not any(name == b"x-dashboard-key" for name, _ in scope.get("headers") or [])
    )


def _db_readable() -> bool:
    """The DB check behind app.healthz's public answer: the same three
    counts, on a fresh connection."""
    try:
        conn = sqlite3.connect(twin.DB_PATH)
        try:
            for table in ("message_logs", "orders", "instagram_logs"):
                conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        finally:
            conn.close()
        return True
    except Exception:
        return False


class PlainHealthz:
    """ASGI middleware: answers a plain GET/HEAD /healthz without the Flask
    thread (see the module docstring); everything else passes through."""

    def __init__(self, app, flask: FlaskClock):
        self.app = app
        self.flask = flask

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not _is_plain_healthz(scope):
            await self.app(scope, receive, send)
            return
        running, label = self.flask.longest_running()
        if running > HUNG_FLASK_SECONDS:
            print(f"[HEALTHZ] 503: {label} has held the Flask thread for {running:.0f}s "
                  f"(over {HUNG_FLASK_SECONDS:g}s)")
            ok = False
        else:
            ok = await run_in_threadpool(_db_readable)
        # Byte for byte what Flask's jsonify sends for the public answer.
        body = (json.dumps({"status": "ok" if ok else "error"}, separators=(",", ":")) + "\n").encode()
        await send({
            "type": "http.response.start",
            "status": 200 if ok else 503,
            "headers": [(b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode())],
        })
        await send({"type": "http.response.body", "body": b"" if scope["method"] == "HEAD" else body})


class GunicornParity:
    """ASGI middleware: drops header names containing "_" (as gunicorn
    did) and logs a request still running `slow_after` seconds after it
    arrived. The time includes any wait behind the request ahead of it on
    the single Flask thread."""

    def __init__(self, app, slow_after: float = SLOW_REQUEST_SECONDS):
        self.app = app
        self.slow_after = slow_after

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = scope.get("headers") or []
        kept = [(name, value) for name, value in headers if b"_" not in name]
        if len(kept) != len(headers):
            scope = dict(scope, headers=kept)

        label = f"{scope.get('method', '?')} {_first_segment(scope.get('path', ''))}"
        started = time.monotonic()
        timer = asyncio.get_running_loop().call_later(
            self.slow_after,
            print,
            f"[SLOW] {label} still running {self.slow_after:g}s after it arrived "
            f"(gunicorn's --timeout 60 used to kill the worker; nothing is killed now)",
        )
        try:
            await self.app(scope, receive, send)
        finally:
            timer.cancel()
            took = time.monotonic() - started
            if took >= self.slow_after:
                print(f"[SLOW] {label} finished after {took:.0f}s")


flask_clock = FlaskClock(twin.app)

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(PlainHealthz, flask=flask_clock)
app.add_middleware(GunicornParity)        # added last = outermost: headers are cleaned first
app.mount("/", WSGIMiddleware(flask_clock, workers=FLASK_THREADS))
