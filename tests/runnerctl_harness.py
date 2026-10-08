"""Test base for the reference manager: the gh harness plus fake docker, timeout, and systemd-notify."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from gh_harness import ROOT, GhScriptTestCase

RUNNERCTL = ROOT / "scripts" / "runnerctl"
FAKE_DOCKER = ROOT / "tests" / "fake_docker.py"
FAKE_TIMEOUT = ROOT / "tests" / "fake_timeout.py"
FAKE_NOTIFY = ROOT / "tests" / "fake_systemd_notify.py"


class RunnerctlTestCase(GhScriptTestCase):
    def setUp(self) -> None:
        super().setUp()
        for name, target in (("docker", FAKE_DOCKER), ("timeout", FAKE_TIMEOUT), ("systemd-notify", FAKE_NOTIFY)):
            (self.bin_dir / name).symlink_to(target)
        self.state_dir = self.temp_dir / "state"
        self.docker_log = self.temp_dir / "docker-calls.jsonl"
        self.docker_fixture = self.temp_dir / "docker-fixture.json"
        self.timeout_log = self.temp_dir / "timeout-calls.jsonl"
        self.notify_log = self.temp_dir / "notify-calls.jsonl"
        self.stop_file = self.temp_dir / "container-stopped"
        self.hang_marker = self.temp_dir / "hanging"
        self.env.update(
            RUNNERCTL_STATE_DIR=str(self.state_dir),
            RUNNERCTL_UPTIME_FILE=str(self.clock),
            GITHUB_RUNNER_GH=str(ROOT / "scripts" / "github-runner-gh"),
            FAKE_DOCKER_LOG=str(self.docker_log),
            FAKE_DOCKER_FIXTURE=str(self.docker_fixture),
            FAKE_DOCKER_STOP_FILE=str(self.stop_file),
            FAKE_TIMEOUT_LOG=str(self.timeout_log),
            FAKE_NOTIFY_LOG=str(self.notify_log),
            FAKE_HANG_MARKER=str(self.hang_marker),
        )
        self.env.pop("FAKE_DOCKER_WAIT_IGNORES_TERM", None)

    # The registration every test uses; the fake docker reports its label on the volume unless a test says otherwise.
    slug = "owner-repository-0badf00d"

    def tearDown(self) -> None:
        # A fake that a test left blocking (a follower, a hanging daemon call) lives in its own process group;
        # it is found by the test's own bin directory in its command line and ended here.
        import signal

        marker = str(self.bin_dir) + "/"
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit() or int(entry.name) == os.getpid():
                continue
            try:
                cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
            except OSError:
                continue
            if marker in cmdline:
                try:
                    os.kill(int(entry.name), signal.SIGKILL)
                except OSError:
                    pass
        super().tearDown()

    def set_docker(self, fixture: dict) -> None:
        fixture = {"volume ls": {"stdout": f"gha-{self.slug}-runner\n"}, "volume inspect": {"stdout": f"{self.slug}\n"}, **fixture}
        self.docker_fixture.write_text(json.dumps(fixture), encoding="utf-8")
        for stale in (self.docker_log, Path(str(self.docker_log) + ".calls"), self.timeout_log, self.notify_log, self.stop_file, self.hang_marker):
            stale.unlink(missing_ok=True)

    def run_runnerctl(self, *args: str, env: dict | None = None, timeout: float = 30) -> subprocess.CompletedProcess:
        command = [str(RUNNERCTL), *args]
        with subprocess.Popen(
            command, env={**self.env, **(env or {})}, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True
        ) as process:
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, 9)
                process.communicate()
                raise
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)

    def docker_calls(self) -> list[dict]:
        if not self.docker_log.exists():
            return []
        return [json.loads(line) for line in self.docker_log.read_text(encoding="utf-8").splitlines()]

    def docker_argv(self) -> list[list[str]]:
        return [call["argv"] for call in self.docker_calls()]

    def timeout_calls(self) -> list[dict]:
        if not self.timeout_log.exists():
            return []
        return [json.loads(line) for line in self.timeout_log.read_text(encoding="utf-8").splitlines()]

    def notify_calls(self) -> list[list[str]]:
        if not self.notify_log.exists():
            return []
        return [json.loads(line) for line in self.notify_log.read_text(encoding="utf-8").splitlines()]

    def registration(self, slug: str) -> dict:
        return json.loads((self.state_dir / f"{slug}.json").read_text(encoding="utf-8"))

    def seed_state(self, slug: str, **fields: object) -> None:
        self.state_dir.mkdir(exist_ok=True)
        path = self.state_dir / f"{slug}.json"
        path.write_text(json.dumps(fields), encoding="utf-8")
        path.chmod(0o600)
