"""Run the documented multi-tool recovery across separate processes."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class LedgerToolNodeExampleTest(unittest.TestCase):
    def test_lost_reply_is_confirmed_without_repeating_either_effect(self):
        example = Path(__file__).resolve().parents[1] / 'examples' / 'ledger_tool_node.py'
        self.assertTrue(example.exists(), 'Runnable LedgerToolNode example is missing')
        with tempfile.TemporaryDirectory() as directory:
            def run(*args):
                process = subprocess.run(
                    [sys.executable, str(example), '--state-dir', directory, *args],
                    capture_output=True, text=True, timeout=30,
                )
                self.assertEqual(process.returncode, 0, process.stderr)
                return json.loads(process.stdout)

            stopped = run('start', '--lose-response')
            self.assertEqual(stopped['status'], 'paused')
            self.assertEqual(stopped['deliveries'], {'mail': 1, 'slack': 1})
            again = run('resume')
            self.assertEqual(again['status'], 'paused', repr(again))
            self.assertEqual(again['deliveries'], stopped['deliveries'])
            confirmed = run('confirm', '--workers-stopped')
            self.assertEqual(confirmed['confirmed'], 1)
            done = run('resume')
            self.assertEqual(done['status'], 'completed')
            self.assertEqual(done['deliveries'], stopped['deliveries'])
            self.assertEqual(len(done['results']), 2)


if __name__ == '__main__':
    unittest.main()
