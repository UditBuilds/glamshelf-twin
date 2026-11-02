"""
Glam Shelf Twin — FastAPI shell (PR A of the Flask -> FastAPI move).

A FastAPI app that mounts the existing Flask app (app.py) at "/" through
a2wsgi, so Render can serve the Twin with uvicorn. No route lives in
FastAPI yet: every request goes to Flask, unchanged.

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
"""
import asyncio
import logging
import re
import time

from a2wsgi import WSGIMiddleware
from fastapi import FastAPI

import app as twin   # app.py's startup (DB, threads, RAG, graph) runs here, once

logging.getLogger("uvicorn.access").disabled = True

FLASK_THREADS = 1
SLOW_REQUEST_SECONDS = 60

_UNSAFE_LOG_CHARS = re.compile(r"[^A-Za-z0-9._~-]")


def _first_segment(path: str) -> str:
    """"/webhook/<token>?x=1" -> "/webhook". The rest of a path can be a
    secret, so it never reaches the logs; odd characters become "?" so a
    path can't forge a log line."""
    segment = (path or "").lstrip("/").split("/", 1)[0]
    return "/" + _UNSAFE_LOG_CHARS.sub("?", segment)[:40]


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


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(GunicornParity)
app.mount("/", WSGIMiddleware(twin.app, workers=FLASK_THREADS))
