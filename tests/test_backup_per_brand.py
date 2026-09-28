"""Backup and file names per brand (multi-brand task 6).

  - storage_prefix in the brand file names this copy's files: the temp-
    folder DB (<prefix>_logs.db), the dedup cache (<prefix>_seen_ids.txt),
    the default GITHUB_BACKUP_PATH and the backup commit message. Glam
    Shelf's are unchanged (also pinned by tests/test_brand_golden.py);
    GITHUB_BACKUP_PATH set in the env still wins.
  - Backup is fully off — restore, hourly backup and the loop — when
    GITHUB_REPO (or GITHUB_TOKEN) is empty: no GitHub call at all.

GitHub is stubbed — no network.

Run:  python -m unittest tests.test_backup_per_brand
"""
import io, json, os, subprocess, sys, tempfile, unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if "app" not in sys.modules:
    os.environ["DB_PATH"] = os.path.join(
        tempfile.mkdtemp(prefix="glamshelf-backup-brand-test-"), "test.db"
    )
    os.environ["GITHUB_TOKEN"] = ""; os.environ["GITHUB_REPO"] = ""
    os.environ.pop("GITHUB_BACKUP_PATH", None)
    os.environ.setdefault("SECRET_KEY", "t"); os.environ.setdefault("APP_PASSWORD", "t")
    os.environ.setdefault("DASHBOARD_KEY", "t")
import app as glam
import brand_config

MARK = "@@RESULT@@"
CHILD = (
    "import io, json, contextlib\n"
    "from unittest.mock import Mock, patch\n"
    "with contextlib.redirect_stdout(io.StringIO()):\n"
    "    import app\n"
    "    puts = []\n"
    "    get404 = Mock(ok=False, status_code=404, text='')\n"
    "    with patch.object(app, 'GITHUB_TOKEN', 't'), patch.object(app, 'GITHUB_REPO', 'o/r'), \\\n"
    "         patch.object(app.requests, 'get', lambda *a, **k: get404), \\\n"
    "         patch.object(app.requests, 'put', lambda url, **k: puts.append((url, k['json']['message']))\n"
    "                      or Mock(ok=True, status_code=200)):\n"
    "        app._backup_db_to_github()\n"
    "import os\n"
    f"print({MARK!r} + json.dumps({{\n"
    "  'legacy_db': os.path.basename(app._LEGACY_DB_PATH),\n"
    "  'dedup': os.path.basename(app.DEDUP_CACHE_FILE),\n"
    "  'backup_path': app.GITHUB_BACKUP_PATH,\n"
    "  'backup_url': puts[0][0], 'backup_message': puts[0][1].split(' @ ')[0],\n"
    "}))\n"
)


def child(**env) -> dict:
    with tempfile.TemporaryDirectory(prefix="backup-brand-child-") as tmp:
        full_env = {**os.environ, "DB_PATH": os.path.join(tmp, "test.db"),
                    "GITHUB_TOKEN": "", "GITHUB_REPO": "",
                    "SECRET_KEY": "t", "APP_PASSWORD": "t", "DASHBOARD_KEY": "t"}
        for name in ("BRAND_CONFIG_PATH", "GITHUB_BACKUP_PATH"):
            full_env.pop(name, None)
        full_env.update(env)
        result = subprocess.run([sys.executable, "-c", CHILD], cwd=ROOT, env=full_env,
                                capture_output=True, text=True, encoding="utf-8", timeout=180)
    for line in result.stdout.splitlines():
        if line.startswith(MARK):
            return json.loads(line[len(MARK):])
    raise AssertionError(f"child failed:\n{result.stdout[-2000:]}\n{result.stderr[-3000:]}")


class FileNamesTest(unittest.TestCase):
    def test_glamshelf_names_unchanged(self):
        self.assertEqual(glam.STORAGE_PREFIX, "glamshelf")
        self.assertEqual(os.path.basename(glam._LEGACY_DB_PATH), "glamshelf_logs.db")
        self.assertEqual(os.path.basename(glam.DEDUP_CACHE_FILE), "glamshelf_seen_ids.txt")
        self.assertEqual(glam.GITHUB_BACKUP_PATH, "glamshelf_logs.db")

    def test_example_brand_gets_its_own_files(self):
        out = child(BRAND_CONFIG_PATH="brands/example.json")
        self.assertEqual(out, {
            "legacy_db": "examplecandle_logs.db",
            "dedup": "examplecandle_seen_ids.txt",
            "backup_path": "examplecandle_logs.db",
            "backup_url": "https://api.github.com/repos/o/r/contents/examplecandle_logs.db",
            "backup_message": "auto-backup examplecandle_logs.db",
        })

    def test_env_backup_path_still_wins(self):
        out = child(BRAND_CONFIG_PATH="brands/example.json", GITHUB_BACKUP_PATH="backups/brand-x.db")
        self.assertEqual(out["backup_path"], "backups/brand-x.db")
        self.assertTrue(out["backup_url"].endswith("/contents/backups/brand-x.db"))

    def test_bad_prefix_is_rejected(self):
        data = json.loads((ROOT / "brands" / "example.json").read_text(encoding="utf-8"))
        for bad in ("Example Candle", "../evil", "a" * 41, "_lead"):
            with self.subTest(prefix=bad), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "brand.json"
                path.write_text(json.dumps({**data, "storage_prefix": bad}), encoding="utf-8")
                with self.assertRaises(brand_config.BrandConfigError) as ctx:
                    brand_config.load_brand_config(path)
                self.assertIn("storage_prefix", str(ctx.exception))


class BackupOffTest(unittest.TestCase):
    """Empty GITHUB_REPO (or GITHUB_TOKEN) -> no GitHub call of any kind."""

    def assert_fully_off(self, token, repo):
        get, put, thread = Mock(), Mock(), Mock()
        missing_db = os.path.join(tempfile.mkdtemp(prefix="backup-off-"), "none.db")
        with patch.object(glam, "GITHUB_TOKEN", token), patch.object(glam, "GITHUB_REPO", repo), \
             patch.object(glam.requests, "get", get), patch.object(glam.requests, "put", put), \
             patch.object(glam.threading, "Thread", thread), \
             redirect_stdout(io.StringIO()) as buf:
            self.assertFalse(glam._github_backup_configured())
            with patch.object(glam, "DB_PATH", missing_db):
                glam._restore_db_from_github()        # would restore: no local DB
            glam._backup_db_to_github()
            glam._start_backup_loop()
        get.assert_not_called()
        put.assert_not_called()
        thread.assert_not_called()
        self.assertFalse(os.path.exists(missing_db))
        out = buf.getvalue()
        self.assertIn("[RESTORE] Skipped", out)
        self.assertIn("[BACKUP] Skipped", out)

    def test_empty_repo(self):
        self.assert_fully_off(token="ghp_set", repo="")

    def test_empty_token(self):
        self.assert_fully_off(token="", repo="owner/backup")


if __name__ == "__main__":
    unittest.main()
