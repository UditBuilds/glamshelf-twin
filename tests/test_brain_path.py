"""Brain file path as a setting (multi-brand task 2).

  - BRAIN_FILE_PATH picks the brain; unset = brain/brain.md (pinned by
    tests/test_brand_golden.py); relative paths resolve against the
    project folder; /etc/secrets/<file> (Render Secret Files) is kept as
    an absolute path;
  - the chosen file is what the twin actually loads;
  - a missing brain doesn't stop boot (the webhook's existing missing-
    brain handling takes over) but the boot log says NOT FOUND;
  - settings for one brand with the other's brain is warned about loudly;
  - .gitignore keeps client brain / settings files out of the repo.

Each scenario boots app in a fresh process, since the path is read once at
import. No network beyond what importing app already does.

Run:  python -m unittest tests.test_brain_path
"""
import json, os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MARK = "@@RESULT@@"
PROBE = (
    "import io, json, contextlib\n"
    "with contextlib.redirect_stdout(io.StringIO()) as boot:\n"
    "    import app\n"
    "    text = app.load_brain() if app.BRAIN_FILE.exists() else None\n"
    f"print({MARK!r} + json.dumps({{'brain_file': str(app.BRAIN_FILE), "
    "'text': text, 'boot': boot.getvalue()}))\n"
)


def boot(**env) -> dict:
    with tempfile.TemporaryDirectory(prefix="brain-path-test-") as tmp:
        full_env = {
            **os.environ, "DB_PATH": os.path.join(tmp, "test.db"),
            "GITHUB_TOKEN": "", "GITHUB_REPO": "",
            "SECRET_KEY": "t", "APP_PASSWORD": "t", "DASHBOARD_KEY": "t",
        }
        for name in ("BRAND_CONFIG_PATH", "BRAIN_FILE_PATH"):
            full_env.pop(name, None)
        full_env.update(env)
        result = subprocess.run(
            [sys.executable, "-c", PROBE], cwd=ROOT, env=full_env,
            capture_output=True, text=True, encoding="utf-8", timeout=180,
        )
    for line in result.stdout.splitlines():
        if line.startswith(MARK):
            return json.loads(line[len(MARK):])
    raise AssertionError(f"app didn't boot:\n{result.stdout[-2000:]}\n{result.stderr[-3000:]}")


class BrainPathTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="brain-path-files-"))
        cls.client_brain = cls.tmp / "client.brain.md"
        cls.client_brain.write_text("# FAKE CLIENT BRAIN\nfor tests only\n", encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_default_is_the_glamshelf_brain(self):
        out = boot()
        self.assertEqual(Path(out["brain_file"]), ROOT / "brain" / "brain.md")
        self.assertEqual(out["text"], (ROOT / "brain" / "brain.md").read_text(encoding="utf-8"))
        self.assertNotIn("WARNING", out["boot"])
        self.assertNotIn("NOT FOUND", out["boot"])

    def test_absolute_path_is_loaded(self):
        out = boot(BRAIN_FILE_PATH=str(self.client_brain),
                   BRAND_CONFIG_PATH="brands/example.json")
        self.assertEqual(Path(out["brain_file"]), self.client_brain)
        self.assertEqual(out["text"], "# FAKE CLIENT BRAIN\nfor tests only\n")
        self.assertNotIn("WARNING", out["boot"])   # settings + brain both set

    def test_relative_path_resolves_against_the_project(self):
        out = boot(BRAIN_FILE_PATH="brain/brain.md")
        self.assertEqual(Path(out["brain_file"]), ROOT / "brain" / "brain.md")

    def test_render_secret_file_path_missing_still_boots(self):
        out = boot(BRAIN_FILE_PATH="/etc/secrets/client.brain.md",
                   BRAND_CONFIG_PATH="brands/example.json")
        self.assertEqual(Path(out["brain_file"]), Path("/etc/secrets/client.brain.md"))
        self.assertIsNone(out["text"])
        self.assertIn("NOT FOUND", out["boot"])

    def test_other_brand_with_the_glamshelf_brain_warns(self):
        out = boot(BRAND_CONFIG_PATH="brands/example.json")
        self.assertIn("WARNING: settings are for Example Candle Co", out["boot"])

    def test_client_brain_with_glamshelf_settings_warns(self):
        out = boot(BRAIN_FILE_PATH=str(self.client_brain))
        self.assertIn("WARNING: BRAIN_FILE_PATH is set but BRAND_CONFIG_PATH isn't", out["boot"])


@unittest.skipUnless(shutil.which("git") and (ROOT / ".git").exists(), "needs git")
class GitignoreTest(unittest.TestCase):
    def ignored(self, path: str) -> bool:
        return subprocess.run(
            ["git", "check-ignore", "-q", path], cwd=ROOT
        ).returncode == 0

    def test_client_files_are_ignored(self):
        for path in ("brands/acme.json", "brain/acme.md", "acme.brain.md",
                     "docs/acme.brand.json", "brain.md", "brand.json",
                     "secrets/acme.md", "instagram_token.json"):
            with self.subTest(path=path):
                self.assertTrue(self.ignored(path))

    def test_glamshelf_and_example_stay_tracked(self):
        for path in ("brands/glamshelf.json", "brands/example.json", "brain/brain.md",
                     "brain/history/brain-v1.6.md", "app.py", "README.md"):
            with self.subTest(path=path):
                self.assertFalse(self.ignored(path))


if __name__ == "__main__":
    unittest.main()
