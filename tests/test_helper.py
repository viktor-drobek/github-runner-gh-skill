"""Signal, diagnostics, and runner-ID matching cases for the helper."""
from __future__ import annotations

import os
import stat
import sys
import unittest
from pathlib import Path

# Lets any runner (discover, python -m unittest tests.test_helper, direct execution) import the harness.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from gh_harness import (  # noqa: E402
    SIGNALS,
    TOKEN,
    TOKEN_COMMANDS,
    GhScriptTestCase,
    job,
    jobs_key,
    jobs_page,
    paged,
    token_fixture,
)


class RunnerHelperTest(GhScriptTestCase):
    def test_both_token_commands_suppress_inherited_diagnostics(self) -> None:
        for command, endpoint in TOKEN_COMMANDS:
            for debug in ({"GH_DEBUG": "api"}, {"DEBUG": "1"}, {"GH_DEBUG": "api", "DEBUG": "1"}):
                with self.subTest(command=command, debug=debug):
                    self.set_fixture(token_fixture(endpoint))
                    result = self.run_in_repo(command, env=debug)
                    self.assert_succeeded(result)
                    # The fake leaks bodies of other requests, proving diagnostics were active.
                    self.assertIn("viewerPermission", result.stderr)
                    self.assertNotIn(TOKEN, result.stdout + result.stderr)
                    path = Path(result.stdout.strip())
                    self.assertEqual(path.parent, self.temp_dir)
                    self.assertEqual(path.read_text().strip(), TOKEN)
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                    self.assertEqual(self.token_files(), [path])
                    path.unlink()

    def test_failure_after_request_removes_token_file(self) -> None:
        self.install_stub("chmod", "#!/usr/bin/env bash\nexit 1\n")
        self.set_fixture(token_fixture("registration-token"))
        self.assert_no_token_handoff(self.run_in_repo("registration-token-file"))

    def test_signals_remove_token_files_and_stop_active_requests(self) -> None:
        for command, endpoint in TOKEN_COMMANDS:
            for name, signum in SIGNALS.items():
                with self.subTest(command=command, signal=name):
                    self.set_fixture(token_fixture(endpoint, signal_parent=int(signum)))
                    # The fake request sleeps 30 s, so a cleanup regression fails fast here instead of hanging.
                    result = self.run_in_repo(command, timeout=5)
                    self.assert_no_token_handoff(result)
                    self.assertEqual(result.returncode, 128 + signum)
                    request_pid = int(self.request_pid.read_text())
                    with self.assertRaises(ProcessLookupError):
                        os.kill(request_pid, 0)

    def assignment_fixture(self, *jobs_responses: dict) -> None:
        self.set_fixture({jobs_key(123): jobs_responses[0] if len(jobs_responses) == 1 else list(jobs_responses)})

    @staticmethod
    def job(runner_id: int | None, name: str = "same-name", status: str = "in_progress") -> dict:
        return jobs_page(job(456, "build", status, runner_id, name, run_id=123))

    def test_assignment_requires_matching_id_and_name(self) -> None:
        for candidate in (self.job(999), self.job(None), self.job(22, name="other-name"), self.job(22, status="queued")):
            with self.subTest(job=candidate):
                self.assignment_fixture(candidate)
                result = self.run_in_repo("wait-assignment", "22", "same-name", "123", "1")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertIn("no in_progress or completed job of run 123", result.stderr)

    def test_matching_assignment_on_later_page_reports_runner_id(self) -> None:
        self.assignment_fixture(paged(self.job(999), self.job(22)))
        result = self.run_in_repo("wait-assignment", "22", "same-name", "123", "1")
        self.assert_succeeded(result)
        self.assertEqual(
            result.stdout,
            "run_id\tjob_id\tjob_name\tstatus\trunner_id\trunner_name\tconclusion\n"
            "123\t456\tbuild\tin_progress\t22\tsame-name\t\n",
        )

    def test_assignment_normalizes_numeric_ids_with_leading_zeroes(self) -> None:
        self.assignment_fixture(self.job(22))
        result = self.run_in_repo("wait-assignment", "022", "same-name", "0123")
        self.assert_succeeded(result)
        self.assertIn("\t22\tsame-name\t\n", result.stdout)
        self.assertEqual(self.api_endpoints(), [jobs_key(123)])


if __name__ == "__main__":
    unittest.main()
