"""Integration tests for the reference manager: a real Docker daemon and a real user systemd.

Run by hand with `make integration`; never by `make test`. Requirements: Docker with the pinned runner image
pulled, `systemd-run --user` available, and a scratch repository the operator names in
RUNNERCTL_INTEGRATION_REPO with `gh` authenticated as an admin of it. `systemd-run --user` needs the user
bus: in a desktop session whose bus address points elsewhere, export
DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus first. A registration is made in that
repository and removed again; the run's output is what a pull request attaches as evidence.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[2]
RUNNERCTL = ROOT / "scripts" / "runnerctl"
HELPER = ROOT / "scripts" / "github-runner-gh"
REPO = os.environ.get("RUNNERCTL_INTEGRATION_REPO", "")


def run(*command: str, env: dict | None = None, timeout: float = 300, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(command, env={**os.environ, **(env or {})}, capture_output=True, text=True, timeout=timeout, check=check)


@unittest.skipUnless(REPO, "set RUNNERCTL_INTEGRATION_REPO to OWNER/REPOSITORY of a scratch repository")
class RunnerctlIntegrationTest(unittest.TestCase):
    """Registers a persistent runner in the scratch repository, runs it under a transient user unit, stops the
    unit, and checks that the stop path completed before TimeoutStopSec, that GitHub answers 404 afterwards,
    and that no token reached the configuration container's arguments, environment, or inspect output."""

    def setUp(self) -> None:
        self.state_dir = Path(os.environ.get("RUNNERCTL_STATE_DIR", Path.home() / ".local/state/runnerctl-integration"))
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.env = {"RUNNERCTL_STATE_DIR": str(self.state_dir)}
        self.slug = f"integration-{int(time.time())}"
        self.unit = f"runnerctl-test-{self.slug}"

    def tearDown(self) -> None:
        subprocess.run(["systemctl", "--user", "stop", self.unit], capture_output=True)
        subprocess.run([str(RUNNERCTL), "reconcile", self.slug], env={**os.environ, **self.env}, capture_output=True)

    def test_register_run_stop_under_a_transient_unit(self) -> None:
        # Registration through the helper's token handoff; the configuration container is watched for the token.
        watcher = subprocess.Popen(
            ["bash", "-c", 'while :; do docker top "gha-$1-config" -o pid,args 2>/dev/null; sleep 0.2; done', "watch", self.slug],
            stdout=subprocess.PIPE, text=True,
        )
        try:
            registration = run(str(HELPER), "--repo", REPO, "with-registration-token", str(RUNNERCTL), "register", "--repo", REPO,
                               "--labels", "integration", "--slug", self.slug, "--token-file", "{}", env=self.env)
        finally:
            watcher.terminate()
            watched, _ = watcher.communicate()
        self.assertEqual(registration.stdout.strip(), self.slug)
        state = json.loads((self.state_dir / f"{self.slug}.json").read_text())
        self.assertEqual(state["state"], "registered")
        self.assertNotIn("--token", watched)
        for line in watched.splitlines():
            self.assertLess(len(line), 400, "no token-length argument was seen in the configuration container")

        run("systemd-run", "--user", "--unit", self.unit, "-p", "Type=notify", "-p", "NotifyAccess=main", "-p", "KillMode=mixed",
            "-p", "TimeoutStopSec=90", "-p", f"Environment=RUNNERCTL_STATE_DIR={self.state_dir}", str(RUNNERCTL), "run", self.slug)
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            active = subprocess.run(["systemctl", "--user", "show", "-p", "StatusText,ActiveState", self.unit], capture_output=True, text=True).stdout
            if "ActiveState=active" in active and json.loads((self.state_dir / f"{self.slug}.json").read_text()).get("state") == "running":
                break
            time.sleep(2)
        else:
            self.fail("the unit did not become active with the listener running")

        started = time.monotonic()
        stop = run("systemctl", "--user", "stop", self.unit, timeout=120, check=False)
        elapsed = time.monotonic() - started
        self.assertEqual(stop.returncode, 0, stop.stderr)
        self.assertLess(elapsed, 90, "the stop path ended before TimeoutStopSec")
        result = subprocess.run(["systemctl", "--user", "show", "-p", "Result", self.unit], capture_output=True, text=True).stdout.strip()
        self.assertIn(result, ("Result=success", ""), "the unit was not killed by the timeout")
        self.assertFalse((self.state_dir / f"{self.slug}.json").exists(), "the stop path confirmed the removal and removed the state")
        lookup = run(str(HELPER), "--repo", REPO, "get", state["runner_id"], check=False)
        self.assertEqual(lookup.returncode, 1)
        self.assertIn("GitHub answers 404", lookup.stderr)
        listing = run(str(HELPER), "--repo", REPO, "list")
        self.assertNotIn(self.slug, listing.stdout, "no ghost registration")


if __name__ == "__main__":
    unittest.main()
