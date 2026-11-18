import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/df-verify/scripts"
sys.path.insert(0, str(SCRIPTS))
import check_evidence  # noqa: E402
from fingerprint import fingerprint  # noqa: E402


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                   capture_output=True)


PROGRESS = """状态：{status}
证据版本：{version}

## 验证证据
| 命令 | 目录 | 退出码 | 结果 |
|---|---|---|---|
{rows}
## 下一步
"""


class EvidenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        git(self.repo, "init", "-q")
        (self.repo / "a.py").write_text("x = 1\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "init")
        (self.repo / ".flow").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def write_progress(self, status="done", version=None, rows="| pytest | . | 0 | ok |"):
        version = fingerprint(self.repo) if version is None else version
        (self.repo / ".flow/progress.md").write_text(
            PROGRESS.format(status=status, version=version, rows=rows), encoding="utf-8")

    def test_fingerprint_tracks_changes_but_ignores_flow(self):
        first = fingerprint(self.repo)
        (self.repo / ".flow/notes.md").write_text("ignored", encoding="utf-8")
        self.assertEqual(fingerprint(self.repo), first)
        (self.repo / "a.py").write_text("x = 2\n", encoding="utf-8")
        modified = fingerprint(self.repo)
        self.assertNotEqual(modified, first)
        (self.repo / "b.py").write_text("y = 1\n", encoding="utf-8")
        self.assertNotEqual(fingerprint(self.repo), modified)

    def test_no_progress_or_not_done_allows_stop(self):
        self.assertIsNone(check_evidence.check(self.repo))
        self.write_progress(status="in-progress", version="stale")
        self.assertIsNone(check_evidence.check(self.repo))

    def test_done_with_current_evidence_allows_stop(self):
        self.write_progress()
        self.assertIsNone(check_evidence.check(self.repo))

    def test_done_with_stale_evidence_blocks(self):
        self.write_progress()
        (self.repo / "a.py").write_text("x = 3\n", encoding="utf-8")
        self.assertIn("不一致", check_evidence.check(self.repo))

    def test_done_without_evidence_rows_blocks(self):
        self.write_progress(rows="")
        self.assertIn("为空", check_evidence.check(self.repo))

    def test_hook_entrypoint(self):
        self.write_progress()
        (self.repo / "a.py").write_text("x = 4\n", encoding="utf-8")
        payload = json.dumps({"cwd": str(self.repo), "stop_hook_active": False})
        proc = subprocess.run([sys.executable, str(SCRIPTS / "check_evidence.py")], input=payload,
                              capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        payload = json.dumps({"cwd": str(self.repo), "stop_hook_active": True})
        proc = subprocess.run([sys.executable, str(SCRIPTS / "check_evidence.py")], input=payload,
                              capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
