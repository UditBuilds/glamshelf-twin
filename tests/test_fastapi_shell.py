"""The FastAPI shell (main.py, PR A of the Flask -> FastAPI move).

main.py mounts the Flask app at "/" through a2wsgi so Render can run
uvicorn. No route moved, so every request must get what Flask gives when
it serves the request directly (Flask's test client here):

  - /healthz and the Flask pages answer the same through FastAPI;
  - the raw body reaches Flask byte for byte, so Meta's HMAC check passes;
  - the Flask session cookie works through the mount;
  - FastAPI's own /docs, /redoc and /openapi.json stay 404;
  - header names with "_" are dropped, as gunicorn did, so
    CF_Connecting_IP can't poison CF-Connecting-IP and dodge the login limit;
  - Flask runs on one thread, as under gunicorn's sync worker;
  - uvicorn's access log is off (it would log the WATI token in
    /webhook/<token>), even when the start command forgets --no-access-log;
  - a request still running after 60s is logged, first path segment only.

The last class boots the real uvicorn server on a local port.
No live API call.

Run:  python -m unittest tests.test_fastapi_shell
"""
import asyncio, hashlib, hmac, io, json, logging, os, re, signal, subprocess, sys, tempfile, time, unittest
import urllib.error, urllib.request
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
if "app" not in sys.modules:
    os.environ["DB_PATH"] = os.path.join(
        tempfile.mkdtemp(prefix="glamshelf-fastapi-shell-test-"), "test.db"
    )
    os.environ["GITHUB_TOKEN"] = ""; os.environ["GITHUB_REPO"] = ""
    os.environ.setdefault("SECRET_KEY", "t"); os.environ.setdefault("APP_PASSWORD", "t")
    os.environ.setdefault("DASHBOARD_KEY", "t")
import app as glam
import main
from a2wsgi import WSGIMiddleware
from fastapi.testclient import TestClient
from starlette.routing import Mount

IG_SECRET = "test-instagram-app-secret"


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(IG_SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _clear_login_failures():
    with glam._login_failures_lock:
        glam._login_failures.clear()


class ThroughFastAPI(unittest.TestCase):
    def setUp(self):
        glam._init_db()
        _clear_login_failures()
        self.addCleanup(_clear_login_failures)
        self.api = TestClient(main.app, follow_redirects=False)
        self.flask = glam.app.test_client()

    def test_healthz_matches_flask(self):
        via_api, direct = self.api.get("/healthz"), self.flask.get("/healthz")
        self.assertEqual(via_api.status_code, 200)
        self.assertEqual(via_api.json(), {"status": "ok"})
        self.assertEqual((via_api.status_code, via_api.json()), (direct.status_code, direct.get_json()))

    def test_keyed_healthz_matches_flask(self):
        key = {"X-Dashboard-Key": glam.DASHBOARD_KEY}
        via_api, direct = self.api.get("/healthz", headers=key), self.flask.get("/healthz", headers=key)
        self.assertEqual(via_api.status_code, direct.status_code)
        self.assertEqual(sorted(via_api.json()), sorted(direct.get_json()))
        self.assertGreater(len(via_api.json()), 1)     # the full payload, not just status

    def test_flask_redirects_are_unchanged(self):
        with redirect_stdout(io.StringIO()):
            for path in ("/", "/logout"):
                via_api, direct = self.api.get(path), self.flask.get(path)
                self.assertEqual(via_api.status_code, 302, path)
                self.assertEqual(via_api.headers["location"], "/login", path)
                self.assertEqual(direct.headers["Location"], "/login", path)

    def test_login_page_is_byte_identical(self):
        via_api, direct = self.api.get("/login"), self.flask.get("/login")
        self.assertEqual(via_api.status_code, 200)
        self.assertEqual(via_api.content, direct.data)
        self.assertEqual(via_api.headers["content-type"], direct.headers["Content-Type"])

    def test_session_cookie_works_through_the_mount(self):
        with redirect_stdout(io.StringIO()):
            login = self.api.post("/login", data={"password": glam.APP_PASSWORD})
            self.assertEqual(login.status_code, 302)
            self.assertEqual(login.headers["location"], "/")
            self.assertIn("session=", login.headers["set-cookie"])
            self.assertEqual(self.api.get("/").status_code, 200)          # cookie sent back
            fresh = TestClient(main.app, follow_redirects=False)
            self.assertEqual(fresh.get("/").status_code, 302)             # no cookie

    def test_signed_instagram_webhook_reaches_flask_byte_for_byte(self):
        event = {"sender": {"id": "17841400000000001"}, "note": "✨ lash glue — é"}
        body = json.dumps({"object": "instagram", "entry": [{"messaging": [event]}]},
                          ensure_ascii=False).encode("utf-8")
        with patch.object(glam, "INSTAGRAM_APP_SECRET", IG_SECRET), \
                patch.object(glam, "_process_instagram_event") as process, \
                patch.dict(os.environ), redirect_stdout(io.StringIO()):
            os.environ.pop("INSTAGRAM_WEBHOOK_VERIFY_DISABLED", None)
            ok = self.api.post("/instagram-webhook", content=body, headers={
                "Content-Type": "application/json", "X-Hub-Signature-256": _sign(body)})
            forged = self.api.post("/instagram-webhook", content=body, headers={
                "Content-Type": "application/json", "X-Hub-Signature-256": _sign(body + b" ")})
        self.assertEqual((ok.status_code, ok.json()), (200, {"status": "ok"}))
        self.assertEqual(forged.status_code, 401)
        process.assert_called_once_with(event)

    def test_fastapi_docs_stay_off(self):
        for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
            via_api, direct = self.api.get(path), self.flask.get(path)
            self.assertEqual(via_api.status_code, 404, path)
            self.assertEqual(via_api.content, direct.data, path)

    def test_underscore_header_cannot_poison_cf_connecting_ip(self):
        # a2wsgi would join both into one CF-Connecting-IP value; _client_ip
        # would reject it and fall back to a header the client controls.
        with redirect_stdout(io.StringIO()):
            r = self.api.post("/login", data={"password": "wrong"}, headers={
                "CF-Connecting-IP": "203.0.113.7", "CF_Connecting_IP": "198.51.100.9"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(list(glam._login_failures), ["203.0.113.7"])


class Shape(unittest.TestCase):
    def test_flask_is_the_only_route_mounted_at_root_on_one_thread(self):
        self.assertEqual(len(main.app.routes), 1)       # no route lives in FastAPI yet
        mount = main.app.routes[0]
        self.assertIsInstance(mount, Mount)
        self.assertEqual(mount.path, "")                # Mount("/")
        self.assertIsInstance(mount.app, WSGIMiddleware)
        self.assertIs(mount.app.app, glam.app)
        self.assertEqual(main.FLASK_THREADS, 1)
        self.assertEqual(mount.app.executor._max_workers, 1)

    def test_main_uses_the_one_app_module(self):
        self.assertIs(main.twin, sys.modules["app"])

    def test_uvicorn_access_logger_is_off(self):
        self.assertTrue(logging.getLogger("uvicorn.access").disabled)

    def test_parity_middleware_uses_gunicorns_60s(self):
        self.assertEqual(main.SLOW_REQUEST_SECONDS, 60)
        added = [m for m in main.app.user_middleware if m.cls is main.GunicornParity]
        self.assertEqual(len(added), 1)
        self.assertEqual((added[0].args, added[0].kwargs), ((), {}))


async def _receive():
    return {"type": "http.request", "body": b"", "more_body": False}


async def _discard(message):
    pass


def _scope(path="/", query=b"", headers=()):
    return {"type": "http", "method": "POST", "path": path, "query_string": query,
            "headers": list(headers)}


class GunicornParityMiddleware(unittest.TestCase):
    def test_only_underscore_header_names_are_dropped(self):
        seen = {}

        async def capture(scope, receive, send):
            seen["headers"] = scope["headers"]

        asyncio.run(main.GunicornParity(capture)(_scope(headers=[
            (b"x-hub-signature-256", b"sha256=ab"),
            (b"cf_connecting_ip", b"198.51.100.9"),
            (b"cookie", b"session=x"),
            (b"x_forwarded_for", b"198.51.100.10"),
        ]), _receive, _discard))
        self.assertEqual(seen["headers"], [(b"x-hub-signature-256", b"sha256=ab"), (b"cookie", b"session=x")])

    def _run(self, path, query, delay, slow_after):
        async def slow_app(scope, receive, send):
            await asyncio.sleep(delay)
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        out = io.StringIO()
        with redirect_stdout(out):
            asyncio.run(main.GunicornParity(slow_app, slow_after=slow_after)(
                _scope(path, query), _receive, _discard))
        return out.getvalue()

    def test_slow_request_is_logged_by_first_segment_only(self):
        out = self._run("/webhook/s3cr3t-wati-token", b"key=also-s3cr3t", delay=0.3, slow_after=0.05)
        self.assertIn("[SLOW] POST /webhook still running", out)
        self.assertIn("[SLOW] POST /webhook finished after", out)
        self.assertNotIn("s3cr3t", out)

    def test_fast_request_logs_nothing(self):
        self.assertEqual(self._run("/healthz", b"", delay=0, slow_after=5), "")

    def test_first_segment_never_forges_a_log_line(self):
        self.assertEqual(main._first_segment("/insta\ngram-webhook/x"), "/insta?gram-webhook")
        self.assertEqual(main._first_segment("/"), "/")
        self.assertEqual(main._first_segment(""), "/")
        self.assertEqual(main._first_segment("/" + "a" * 100), "/" + "a" * 40)


class RealUvicornBoot(unittest.TestCase):
    """`uvicorn main:app` in a subprocess, deliberately WITHOUT
    --no-access-log: main.py must keep the access log off on its own."""

    def test_boots_answers_healthz_and_logs_no_paths(self):
        env = dict(os.environ)
        env["DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="glamshelf-uvicorn-boot-test-"), "test.db")
        env["PYTHONUNBUFFERED"] = "1"
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", "0",
             "--workers", "1"],
            cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace",
        )
        lines = []
        try:
            port = None
            deadline = time.monotonic() + 120
            while port is None and time.monotonic() < deadline:
                line = proc.stdout.readline()
                if not line:
                    break
                lines.append(line)
                m = re.search(r"Uvicorn running on http://127\.0\.0\.1:(\d+)", line)
                if m:
                    port = int(m.group(1))
            self.assertIsNotNone(port, "".join(lines[-40:]))

            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(f"http://127.0.0.1:{port}/healthz", timeout=30) as r:
                self.assertEqual((r.status, json.loads(r.read())), (200, {"status": "ok"}))
            with self.assertRaises(urllib.error.HTTPError) as missing:
                opener.open(f"http://127.0.0.1:{port}/no-such-page/s3cr3t-in-path?key=s3cr3t-in-query", timeout=30)
            self.assertEqual(missing.exception.code, 404)
        finally:
            proc.terminate()
            try:
                rest, _ = proc.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                rest, _ = proc.communicate()
            lines.append(rest or "")
        output = "".join(lines)
        self.assertNotIn("s3cr3t", output)
        self.assertNotIn('"GET /healthz', output)        # uvicorn's access-log line format
        # Graceful shutdown on SIGTERM (what Render sends); uvicorn then
        # re-raises the signal, so the exit status is -SIGTERM.
        self.assertIn("Finished server process", output)
        self.assertEqual(proc.returncode, -signal.SIGTERM, output[-2000:])


if __name__ == "__main__":
    unittest.main()
