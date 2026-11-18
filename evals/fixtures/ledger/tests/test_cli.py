import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from ledger.cli import main


class LedgerCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LEDGER_FILE"] = str(Path(self.tmp.name) / "ledger.json")

    def tearDown(self):
        os.environ.pop("LEDGER_FILE", None)
        self.tmp.cleanup()

    def run_cli(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(list(argv))
        return code, out.getvalue()

    def test_list_empty(self):
        code, out = self.run_cli("list")
        self.assertEqual(code, 0)
        self.assertIn("暂无记录", out)

    def test_add_then_list(self):
        self.run_cli("add", "12.5", "午饭")
        self.run_cli("add", "3", "咖啡")
        code, out = self.run_cli("list")
        self.assertEqual(code, 0)
        self.assertIn("午饭", out)
        self.assertIn("合计：15.50", out)


if __name__ == "__main__":
    unittest.main()
