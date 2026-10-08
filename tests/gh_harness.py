"""Shared harness that runs the helper against the offline gh fake with a fake clock."""
from __future__ import annotations

import errno
import json
import os
import pty
import select
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "github-runner-gh"
FAKE_GH = Path(__file__).resolve().with_name("fake_gh.py")

if shutil.which("jq") is None:
    raise RuntimeError("jq is required: the gh fake emulates --jq with it and the helper calls it")

REPO = "owner/repository"
RUNNERS = f"GET repos/{REPO}/actions/runners?per_page=100"
RUNS = f"GET repos/{REPO}/actions/runs?status=in_progress&exclude_pull_requests=true&per_page=100"
RATE_LIMIT = "GET rate_limit"
DEFAULT_LABELS = (("self-hosted", "read-only"), ("Linux", "read-only"), ("repository-build", "custom"))
START_TIME = 1_000_000

TOKEN = "test-only-runner-secret"
TOKEN_COMMANDS = (("registration-token-file", "registration-token"), ("remove-token-file", "remove-token"))
WITH_TOKEN_COMMANDS = (("with-registration-token", "registration-token"), ("with-remove-token", "remove-token"))
SIGNALS = {"HUP": signal.SIGHUP, "INT": signal.SIGINT, "TERM": signal.SIGTERM}

# Response fixtures shared by the suites; see the docstring of fake_gh.py for the shapes.
NOT_FOUND = {"status": 404}
NOT_FOUND_HTML = {"status": 404, "raw": "<html>not found</html>"}
BAD_GATEWAY = {"status": 502}
FORBIDDEN = {"status": 403, "message": "Resource not accessible by personal access token"}
UNAUTHORIZED = {"status": 401, "message": "Bad credentials"}
RATE_LIMITED = {"status": 403, "message": "API rate limit exceeded for user ID 1."}
SECONDARY_RATE_LIMITED = {"status": 429, "message": "You have exceeded a secondary rate limit. Please wait a few minutes before you try again."}
NETWORK_ERROR = {"exit": 1, "stderr": "error connecting to api.github.com\ncheck your internet connection\n"}

# Time advances only through the fake sleep, which also records every requested duration.
FAKE_DATE = '#!/usr/bin/env bash\ncat "$FAKE_CLOCK"\n'
FAKE_SLEEP = (
    "#!/usr/bin/env bash\n"
    'printf \'%s\\n\' "$1" >>"$FAKE_SLEEP_LOG"\n'
    'printf \'%s\\n\' "$(( $(cat "$FAKE_CLOCK") + $1 ))" >"$FAKE_CLOCK"\n'
)


def runner_url(runner_id: int | str) -> str:
    return f"repos/{REPO}/actions/runners/{runner_id}"


def runner_key(method: str, runner_id: int | str) -> str:
    return f"{method} {runner_url(runner_id)}"


def jobs_key(run_id: int | str) -> str:
    return f"GET repos/{REPO}/actions/runs/{run_id}/jobs?per_page=100"


def workflow_runs_key(workflow: str) -> str:
    return f"GET repos/{REPO}/actions/workflows/{workflow}/runs?event=workflow_dispatch&exclude_pull_requests=true&per_page=100"


def commit_runs_key(workflow: str, head_sha: str, event: str | None = None) -> str:
    suffix = f"&event={event}" if event else ""
    return f"GET repos/{REPO}/actions/workflows/{workflow}/runs?head_sha={head_sha}{suffix}&per_page=100"


def dispatch_key(workflow: str) -> str:
    return f"POST repos/{REPO}/actions/workflows/{workflow}/dispatches"


RUNNER_22 = runner_key("GET", 22)
DELETE_22 = runner_key("DELETE", 22)


def runner(runner_id: int, name: str | None, *, busy: bool = False, ephemeral: object = True, labels=DEFAULT_LABELS) -> dict:
    record = {
        "id": runner_id,
        "name": name,
        "os": "linux",
        "status": "online",
        "busy": busy,
        "labels": [{"name": label, "type": kind} for label, kind in labels],
    }
    if ephemeral is not None:
        record["ephemeral"] = ephemeral
    return record


def runners_page(*records: dict) -> dict:
    return {"body": {"total_count": len(records), "runners": list(records)}}


ACTOR = "operator"


# Shell lines for a manager stub that needs the helper's PID: the helper runs the manager under a supervisor
# subshell, so the helper is the manager's grandparent.
HELPER_PID = 'helper=$(ps -o ppid= -p "$PPID"); helper="${helper// /}"\n'


SHA = "0123456789abcdef0123456789abcdef01234567"


def run(run_id: int, *, actor: str = ACTOR, title: str = "CI", head_sha: str = SHA, event: str = "push", status: str = "in_progress") -> dict:
    return {
        "id": run_id,
        "name": "CI",
        "display_title": title,
        "status": status,
        "event": event,
        "head_sha": head_sha,
        "html_url": f"https://github.com/{REPO}/actions/runs/{run_id}",
        "actor": {"login": actor},
    }


def runs_page(*runs: int | dict) -> dict:
    records = [run(item) if isinstance(item, int) else item for item in runs]
    return {"body": {"total_count": len(records), "workflow_runs": records}}


USER = "GET user"
USER_RESPONSE = {"body": {"login": ACTOR}}


def job(job_id: int, name: str, status: str, runner_id: int | None, runner_name: str | None, *, run_id: int = 100, conclusion: str | None = None) -> dict:
    return {
        "id": job_id,
        "run_id": run_id,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "runner_id": runner_id,
        "runner_name": runner_name,
        "runner_group_name": None,
    }


def jobs_page(*jobs: dict) -> dict:
    return {"body": {"total_count": len(jobs), "jobs": list(jobs)}}


def paged(*responses: dict) -> dict:
    return {"pages": [response["body"] for response in responses]}


def token_fixture(endpoint: str, **response: object) -> dict:
    return {f"POST {runner_url(endpoint)}": {"body": {"token": TOKEN}, **response}}


class GhScriptTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.temp_dir = Path(temp_dir.name)
        self.bin_dir = self.temp_dir / "bin"
        self.bin_dir.mkdir()
        (self.bin_dir / "gh").symlink_to(FAKE_GH)
        self.install_stub("date", FAKE_DATE)
        self.install_stub("sleep", FAKE_SLEEP)
        self.log = self.temp_dir / "gh-calls.jsonl"
        self.fixture = self.temp_dir / "fixture.json"
        self.state = self.temp_dir / "gh-state.json"
        self.clock = self.temp_dir / "clock"
        self.sleep_log = self.temp_dir / "sleeps"
        self.request_pid = self.temp_dir / "request.pid"
        self.env = os.environ.copy()
        for inherited in ("GH_REPO", "GH_DEBUG", "DEBUG", "GH_TELEMETRY", "CLICOLOR_FORCE", "GH_FORCE_TTY", "GITHUB_RUNNER_TOKEN_FILE"):
            self.env.pop(inherited, None)
        self.env.update(
            PATH=f"{self.bin_dir}{os.pathsep}{self.env['PATH']}",
            TMPDIR=str(self.temp_dir),
            FAKE_GH_LOG=str(self.log),
            FAKE_GH_FIXTURE=str(self.fixture),
            FAKE_GH_STATE=str(self.state),
            FAKE_GH_REQUEST_PID=str(self.request_pid),
            FAKE_CLOCK=str(self.clock),
            FAKE_SLEEP_LOG=str(self.sleep_log),
        )
        self.set_fixture({})

    def install_stub(self, name: str, content: str) -> None:
        path = self.bin_dir / name
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)

    def set_fixture(self, api: dict, **settings: object) -> None:
        """Starts a scenario: installs the responses and resets the clock and every log."""
        self.fixture.write_text(json.dumps({"api": api, **settings}), encoding="utf-8")
        self.clock.write_text(f"{START_TIME}\n", encoding="utf-8")
        for stale in (self.state, self.log, self.sleep_log, self.request_pid, *self.token_files()):
            stale.unlink(missing_ok=True)

    def run_script(self, *args: str, env: dict | None = None, timeout: float = 30) -> subprocess.CompletedProcess:
        command = [str(SCRIPT), *args]
        with subprocess.Popen(
            command,
            env={**self.env, **(env or {})},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        ) as process:
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
                raise
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)

    def run_in_repo(self, *args: str, env: dict | None = None, timeout: float = 30) -> subprocess.CompletedProcess:
        return self.run_script("--repo", REPO, *args, env=env, timeout=timeout)

    def run_in_repo_on_terminal(self, *args: str, env: dict | None = None, typed: str = "", timeout: float = 30) -> tuple[int, str]:
        """Runs the helper with a pseudo-terminal as its standard streams, types `typed` into it, and returns the exit status and output."""
        pid, master = pty.fork()
        if pid == 0:  # pragma: no cover - the child image is replaced
            try:
                os.execve(str(SCRIPT), [str(SCRIPT), "--repo", REPO, *args], {**self.env, **(env or {})})
            finally:
                os._exit(127)
        output = b""
        timed_out = False
        try:
            os.write(master, typed.encode("utf-8"))
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                ready, _, _ = select.select([master], [], [], remaining)
                if not ready:
                    continue
                try:
                    chunk = os.read(master, 4096)
                except OSError as error:
                    if error.errno != errno.EIO:  # EIO is how a closed pseudo-terminal reports its end
                        raise
                    chunk = b""
                if not chunk:
                    break
                output += chunk
            if timed_out:
                # TERM lets the helper's trap stop the manager's process group; KILL then covers the helper itself.
                os.kill(pid, signal.SIGTERM)
                time.sleep(1)
                os.kill(pid, signal.SIGKILL)
        finally:
            os.close(master)
        _, status = os.waitpid(pid, 0)
        if timed_out:
            self.fail(f"the helper did not finish on the terminal within {timeout}s: {output.decode('utf-8', 'replace')!r}")
        return os.waitstatus_to_exitcode(status), output.decode("utf-8", "replace")

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def api_endpoints(self) -> list[str]:
        """The `METHOD path` key of every gh api call, parsed the way the fake parses its arguments."""
        endpoints = []
        for call in self.calls():
            if call[0] != "api":
                continue
            method, path = "GET", None
            arguments = iter(call[1:])
            for argument in arguments:
                if argument == "--method":
                    method = next(arguments)
                elif argument in ("--jq", "--json", "-f", "-F", "--field", "--raw-field"):
                    next(arguments)
                elif not argument.startswith("-") and path is None:
                    path = argument
            endpoints.append(f"{method} {path}")
        return endpoints

    def sleeps(self) -> list[int]:
        if not self.sleep_log.exists():
            return []
        return [int(line) for line in self.sleep_log.read_text(encoding="utf-8").splitlines()]

    def token_files(self) -> list[Path]:
        return sorted(self.temp_dir.glob("github-runner-*-token.*"))

    def assert_succeeded(self, result: subprocess.CompletedProcess) -> None:
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def assert_no_deletes(self) -> None:
        self.assertFalse([endpoint for endpoint in self.api_endpoints() if endpoint.startswith("DELETE ")])

    def assert_no_token_handoff(self, result: subprocess.CompletedProcess) -> None:
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertNotIn(TOKEN, result.stderr)
        self.assertEqual(self.token_files(), [])
