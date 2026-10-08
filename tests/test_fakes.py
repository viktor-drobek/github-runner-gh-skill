"""Tests of the test doubles: the fake timeout must behave like coreutils timeout on the cases the manager
relies on, with the fake clock instead of real time."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

FAKE_TIMEOUT = Path(__file__).resolve().parent / "fake_timeout.py"


class FakeTimeoutTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.clock = self.dir / "clock"
        self.clock.write_text("1000\n")
        self.env = {
            **os.environ,
            "FAKE_CLOCK": str(self.clock),
            "FAKE_TIMEOUT_LOG": str(self.dir / "timeout.log"),
            "FAKE_HANG_MARKER": str(self.dir / "hang"),
            "FAKE_TIMEOUT_FALLBACK_SECONDS": "1",
        }

    def run_timeout(self, *command: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(FAKE_TIMEOUT), "--kill-after=3", "10", *command], env=self.env, capture_output=True, text=True, timeout=30)

    def clock_now(self) -> int:
        return int(self.clock.read_text().split()[0])

    def test_a_command_that_returns_keeps_its_status_and_the_clock(self) -> None:
        result = self.run_timeout("bash", "-c", "exit 7")
        self.assertEqual(result.returncode, 7)
        self.assertEqual(self.clock_now(), 1000)

    def test_a_declared_hang_that_ignores_term_is_charged_the_budget_and_killed(self) -> None:
        marker = self.dir / "hang"
        result = self.run_timeout("bash", "-c", f"trap '' TERM; touch {marker}; while :; do sleep 1; done")
        self.assertEqual(result.returncode, 137, "coreutils timeout reports 137 when KILL was needed")
        self.assertEqual(self.clock_now(), 1000 + 10 + 3, "the duration and the kill grace")

    def test_a_declared_hang_that_ends_on_term_is_charged_the_duration_only(self) -> None:
        marker = self.dir / "hang"
        result = self.run_timeout("bash", "-c", f"touch {marker}; while :; do sleep 1; done")
        self.assertEqual(result.returncode, 124)
        self.assertEqual(self.clock_now(), 1000 + 10)

    def test_a_blocking_command_without_the_marker_times_out_through_the_real_time_fallback(self) -> None:
        result = self.run_timeout("bash", "-c", "while :; do sleep 1; done")
        self.assertEqual(result.returncode, 124)
        self.assertEqual(self.clock_now(), 1000 + 10)

    def test_a_signal_to_timeout_is_forwarded_to_the_command_group(self) -> None:
        import signal
        import time

        process = subprocess.Popen([sys.executable, str(FAKE_TIMEOUT), "--kill-after=3", "10", "bash", "-c", "sleep 30 & wait"], env=self.env)
        time.sleep(0.5)
        process.send_signal(signal.SIGTERM)
        self.assertEqual(process.wait(timeout=10), 143)
        self.assertEqual(self.clock_now(), 1000)


if __name__ == "__main__":
    unittest.main()
