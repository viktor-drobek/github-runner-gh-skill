"""Component tests for the reference manager. Nothing here starts a container or a unit."""
from __future__ import annotations

import os
import re
import stat
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gh_harness import NOT_FOUND, REPO, RUNNER_22, TOKEN, runner, runner_key, token_fixture  # noqa: E402
from runnerctl_harness import RUNNERCTL, RunnerctlTestCase  # noqa: E402

SLUG = "owner-repository-0badf00d"
NAME = SLUG
CONTAINER = f"gha-{SLUG}"
VOLUME = f"gha-{SLUG}-runner"
LABEL = f"runnerctl.slug={SLUG}"
IMAGE = "registry.example.invalid/runner@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
RUNNER_26 = runner_key("GET", 26)
DELETE_26 = runner_key("DELETE", 26)


def runner_file(agent_id: int = 26, ephemeral: bool | None = None) -> str:
    record = {"agentId": agent_id, "agentName": NAME, "poolId": 1, "poolName": "Default", "serverUrl": "https://pipelines.actions.githubusercontent.com/x", "gitHubUrl": f"https://github.com/{REPO}", "workFolder": "_work"}
    if ephemeral:
        record["ephemeral"] = True
    import json
    return json.dumps(record)


class RunnerctlTest(RunnerctlTestCase):
    def test_script_has_valid_syntax_and_bounded_budgets_sum_to_the_stop_budget(self) -> None:
        text = RUNNERCTL.read_text(encoding="utf-8")
        self.assertTrue(os.access(RUNNERCTL, os.X_OK))
        constants = {name: int(value) for name, value in re.findall(r"^readonly ([A-Z_]+)=(\d+)$", text, re.M)}
        phases = ("STOP_CONTAINER_BUDGET_SECONDS", "REMOVE_TOKEN_BUDGET_SECONDS", "GET_BUDGET_SECONDS", "DELETE_BUDGET_SECONDS", "RM_BUDGET_SECONDS", "VOLUME_RM_BUDGET_SECONDS")
        self.assertEqual(sum(constants[name] for name in phases), constants["STOP_BUDGET_SECONDS"])
        self.assertLess(constants["STOP_BUDGET_SECONDS"], 90, "the unit's TimeoutStopSec")
        self.assertLess(constants["STOP_CONTAINER_GRACE_SECONDS"] + constants["KILL_GRACE_SECONDS"], constants["STOP_CONTAINER_BUDGET_SECONDS"])
        self.assertLess(2 * constants["HELPER_GRACE_SECONDS"], constants["KILL_GRACE_SECONDS"], "the helper's grace and its wait for the group run after timeout's TERM and must end before its KILL")
        self.assertNotIn("gh api", text, "the manager reaches GitHub only through the helper")
        self.assertNotIn("curl ", text)

    def test_invocation_errors_exit_2_without_touching_docker(self) -> None:
        cases = {
            "no command": [],
            "unknown command": ["frobnicate"],
            "status without slug": ["status"],
            "status with a slug containing a slash": ["status", "a/b"],
            "status with an empty slug": ["status", ""],
        }
        for name, args in cases.items():
            with self.subTest(case=name):
                self.set_docker({})
                result = self.run_runnerctl(*args)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(self.docker_calls(), [])

    def test_status_reports_state_container_and_github_through_the_helper(self) -> None:
        self.seed_state(SLUG, repository=REPO, runner_id=22, name="runner-a", mode="persistent", state="registered",
                        phases=[{"phase": "stop-container", "started": 100, "ended": 105, "result": "0"}])
        self.set_docker({"ps {{.State}}": {"stdout": "running\n"}})
        self.set_fixture({RUNNER_22: {"body": runner(22, "runner-a", ephemeral=None)}})
        result = self.run_runnerctl("status", SLUG)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines(),
            [f"slug\t{SLUG}", f"repository\t{REPO}", "runner_id\t22", "name\trunner-a", "mode\tpersistent", "state\tregistered",
             "container\trunning", "github\tregistered", "phase\tstop-container\t100\t105\t0"],
        )
        self.assertEqual(self.docker_argv(), [["ps", "-a", "--filter", f"name=^gha-{SLUG}$", "--format", "{{.State}}"]])
        self.assertEqual(self.api_endpoints()[-1], RUNNER_22)

    def test_status_reports_a_helper_failure_as_such_and_a_missing_container_as_absent(self) -> None:
        self.seed_state(SLUG, repository=REPO, runner_id=22, name="runner-a", mode="ephemeral", state="running")
        self.set_docker({"ps {{.State}}": {"stdout": ""}})
        self.set_fixture({RUNNER_22: {"exit": 1, "stderr": "error connecting to api.github.com\n"}})
        result = self.run_runnerctl("status", SLUG)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("container\tabsent", result.stdout)
        self.assertRegex(result.stdout, r"github\tcheck failed \(1\): .*error connecting")
        self.assertNotIn("github\tgone", result.stdout)

    def register_fixture(self, *, ephemeral_in_file: bool | None = None, config_exit: int = 0) -> Path:
        token_file = self.temp_dir / "registration-token"
        token_file.write_text(TOKEN + "\n", encoding="utf-8")
        token_file.chmod(0o600)
        self.set_docker({"run": [{"exit": config_exit}, {"stdout": runner_file(ephemeral=ephemeral_in_file)}]})
        return token_file

    def assert_token_only_on_the_container_stdin(self) -> None:
        for call in self.docker_calls():
            self.assertNotIn(TOKEN, " ".join(call["argv"]), call["argv"])
            self.assertNotIn(TOKEN, " ".join(call["env"].values()))
            self.assertEqual(call["mounts"], {}, "no file with the token is mounted")
        fed = [call for call in self.docker_calls() if call["stdin"]]
        self.assertEqual(len(fed), 1, "only the configuration container reads standard input")
        self.assertEqual(fed[0]["stdin"], TOKEN + "\n")
        self.assertIn("-i", fed[0]["argv"])
        if (self.state_dir / f"{SLUG}.json").exists():
            self.assertNotIn(TOKEN, (self.state_dir / f"{SLUG}.json").read_text(encoding="utf-8"))

    def test_register_configures_through_the_mounted_token_and_records_the_registration(self) -> None:
        token_file = self.register_fixture(ephemeral_in_file=None)
        self.set_fixture({RUNNER_26: {"body": runner(26, NAME, ephemeral=None)}})
        result = self.run_runnerctl("register", "--repo", REPO, "--labels", "repository-build", "--image", IMAGE, "--token-file", str(token_file), "--slug", SLUG)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"{SLUG}\n")
        argv = self.docker_argv()
        self.assertEqual(argv[0], ["volume", "create", "--label", LABEL, VOLUME])
        config = argv[1]
        self.assertEqual(config[:1], ["run"])
        self.assertIn("--rm", config)
        self.assertIn(f"{CONTAINER}-config", config)
        self.assertIn(LABEL, config)
        self.assertNotIn(str(token_file), " ".join(config), "the token file is not mounted")
        self.assertIn(f"type=volume,src={VOLUME},dst=/home/runner", config)
        self.assertEqual(config[config.index("--cap-drop") + 1], "ALL")
        self.assertEqual(config[config.index("--user") + 1], "runner")
        self.assertIn(IMAGE, config)
        tail = config[config.index("./config.sh"):]
        self.assertEqual(tail, ["./config.sh", "--url", f"https://github.com/{REPO}", "--name", NAME, "--labels", "repository-build", "--work", "_work", "--runnergroup", "Default", "--replace", "--disableupdate"])
        self.assertNotIn("--token", config)
        self.assertNotIn("--unattended", config, "the token is answered to the prompt, so unattended mode must stay off")
        wrapper = config[config.index("-c") + 1]
        self.assertIn("script -q -e --echo never -c", wrapper, "the prompt is answered through a pseudo-terminal with echo off")
        self.assertIn("IFS= read -r token", wrapper, "the token is read from standard input, not from a file")
        self.assertNotIn("/run/runner-token", wrapper)
        self.assertEqual(argv[2][-1:], [".runner"])
        self.assert_token_only_on_the_container_stdin()
        state = self.registration(SLUG)
        self.assertEqual({key: state[key] for key in ("repository", "name", "mode", "runner_id", "state", "image", "container", "volume", "labels")},
                         {"repository": REPO, "name": NAME, "mode": "persistent", "runner_id": "26", "state": "registered", "image": IMAGE, "container": CONTAINER, "volume": VOLUME, "labels": "repository-build"})
        self.assertEqual(stat.S_IMODE((self.state_dir / f"{SLUG}.json").stat().st_mode), 0o600)
        self.assertEqual(self.api_endpoints()[-1], RUNNER_26)

    def test_register_ephemeral_passes_the_flag_and_requires_it_in_the_runner_file(self) -> None:
        token_file = self.register_fixture(ephemeral_in_file=True)
        self.set_fixture({RUNNER_26: {"body": runner(26, NAME)}})
        result = self.run_runnerctl("register", "--repo", REPO, "--labels", "x", "--image", IMAGE, "--ephemeral", "--token-file", str(token_file), "--slug", SLUG)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.docker_argv()[1][-1], "--ephemeral")
        self.assertEqual(self.registration(SLUG)["mode"], "ephemeral")

    def test_register_deletes_a_registration_whose_mode_differs_from_the_declared_one(self) -> None:
        token_file = self.register_fixture(ephemeral_in_file=None)
        self.set_fixture({RUNNER_26: [{"body": runner(26, NAME, ephemeral=None)}, NOT_FOUND], DELETE_26: {"body": None}})
        result = self.run_runnerctl("register", "--repo", REPO, "--labels", "x", "--image", IMAGE, "--ephemeral", "--token-file", str(token_file), "--slug", SLUG)
        self.assertEqual(result.returncode, 1)
        self.assertIn("configured with ephemeral=false, not as the declared ephemeral; the registration was deleted by ID and name", result.stderr)
        self.assertIn(DELETE_26, self.api_endpoints())
        self.assertEqual(self.registration(SLUG)["state"], "failed")

    def test_register_removes_the_volume_when_config_fails_and_keeps_it_when_the_helper_cannot_confirm(self) -> None:
        with self.subTest(case="config.sh fails before registering"):
            token_file = self.register_fixture(config_exit=1)
            self.set_docker({"run": [{"exit": 1}, {"exit": 1, "stderr": "cat: .runner: No such file or directory\n"}]})
            self.set_fixture({})
            result = self.run_runnerctl("register", "--repo", REPO, "--labels", "x", "--image", IMAGE, "--token-file", str(token_file), "--slug", SLUG)
            self.assertEqual(result.returncode, 1)
            self.assertIn("nothing was registered and the volume was removed", result.stderr)
            self.assertIn(["volume", "rm", VOLUME], self.docker_argv())
            self.assertEqual(self.registration(SLUG)["state"], "failed")
            self.assertNotIn("runner_id", self.registration(SLUG))
            self.assertEqual([e for e in self.api_endpoints() if "runners" in e], [])
        with self.subTest(case="config.sh fails after registering, deletion confirmed"):
            (self.state_dir / f"{SLUG}.json").unlink()
            token_file = self.register_fixture()
            self.set_docker({"run": [{"exit": 1}, {"stdout": runner_file()}]})
            self.set_fixture({RUNNER_26: [{"body": runner(26, NAME, ephemeral=None)}, NOT_FOUND], DELETE_26: {"body": None}})
            result = self.run_runnerctl("register", "--repo", REPO, "--labels", "x", "--image", IMAGE, "--token-file", str(token_file), "--slug", SLUG)
            self.assertEqual(result.returncode, 1)
            self.assertIn("after registering runner 26; the registration was deleted by ID and name", result.stderr)
            self.assertIn(DELETE_26, self.api_endpoints())
            self.assertIn(["volume", "rm", VOLUME], self.docker_argv())
            self.assertEqual(self.registration(SLUG)["state"], "failed")
        with self.subTest(case="config.sh fails after registering, deletion not confirmed"):
            (self.state_dir / f"{SLUG}.json").unlink()
            token_file = self.register_fixture()
            self.set_docker({"run": [{"exit": 1}, {"stdout": runner_file()}]})
            self.set_fixture({RUNNER_26: {"exit": 1, "stderr": "error connecting to api.github.com\n"}})
            result = self.run_runnerctl("register", "--repo", REPO, "--labels", "x", "--image", IMAGE, "--token-file", str(token_file), "--slug", SLUG)
            self.assertEqual(result.returncode, 1)
            self.assertIn("could not be deleted; the state is incomplete for reconcile", result.stderr)
            self.assertNotIn(["volume", "rm", VOLUME], self.docker_argv())
            self.assertEqual(self.registration(SLUG)["state"], "incomplete")
            self.assertEqual(self.registration(SLUG)["runner_id"], "26")
        with self.subTest(case="helper cannot confirm"):
            (self.state_dir / f"{SLUG}.json").unlink()
            token_file = self.register_fixture()
            self.set_fixture({RUNNER_26: {"exit": 1, "stderr": "error connecting to api.github.com\n"}})
            result = self.run_runnerctl("register", "--repo", REPO, "--labels", "x", "--image", IMAGE, "--token-file", str(token_file), "--slug", SLUG)
            self.assertEqual(result.returncode, 1)
            self.assertIn("the state is incomplete for reconcile", result.stderr)
            self.assertNotIn(["volume", "rm", VOLUME], self.docker_argv())
            self.assertEqual(self.registration(SLUG)["state"], "incomplete")
            self.assertEqual(self.registration(SLUG)["runner_id"], "26")

    def test_register_rejects_bad_arguments_before_touching_docker(self) -> None:
        token_file = self.temp_dir / "registration-token"
        token_file.write_text(TOKEN, encoding="utf-8")
        cases = {
            "no repository": ["register", "--labels", "x", "--token-file", str(token_file)],
            "no labels": ["register", "--repo", REPO, "--token-file", str(token_file)],
            "no image": ["register", "--repo", REPO, "--labels", "x", "--token-file", str(token_file)],
            "self-hosted as a custom label": ["register", "--repo", REPO, "--labels", "self-hosted,x", "--token-file", str(token_file)],
            "self-hosted in another letter case": ["register", "--repo", REPO, "--labels", "Self-Hosted", "--token-file", str(token_file)],
            "image by tag only": ["register", "--repo", REPO, "--labels", "x", "--token-file", str(token_file), "--image", "registry.example.invalid/runner:stable"],
            "image with a short digest": ["register", "--repo", REPO, "--labels", "x", "--token-file", str(token_file), "--image", "registry.example.invalid/runner@sha256:abc123"],
            "no token file at all": ["register", "--repo", REPO, "--labels", "x"],
            "unknown option": ["register", "--repo", REPO, "--labels", "x", "--token-file", str(token_file), "--verbose"],
        }
        for name, args in cases.items():
            with self.subTest(case=name):
                self.set_docker({})
                result = self.run_runnerctl(*args)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(self.docker_calls(), [])

    def seed_persistent(self, mode: str = "persistent", **fields: object) -> None:
        record = {"repository": REPO, "runner_id": "26", "name": NAME, "mode": mode, "image": IMAGE, "container": CONTAINER, "volume": VOLUME, "state": "registered"}
        record.update(fields)
        self.seed_state(SLUG, **record)

    def phases(self) -> list[tuple[str, int, int, str]]:
        path = self.state_dir / f"{SLUG}.json"
        if not path.exists():
            return []
        return [(p["phase"], p["started"], p["ended"], p["result"]) for p in self.registration(SLUG).get("phases", [])]

    def test_remove_runs_the_stop_path_with_the_token_the_helper_hands_over(self) -> None:
        self.seed_persistent()
        self.set_docker({"run": {"exit": 0}})
        self.set_fixture({**token_fixture("remove-token"), RUNNER_26: NOT_FOUND})
        result = self.run_runnerctl("remove", SLUG)
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = self.docker_argv()
        self.assertEqual(argv[0], ["stop", "--time", "15", CONTAINER])
        self.assertEqual(argv[1][:2], ["ps", "--filter"])
        self.assertEqual(argv[2][:2], ["volume", "ls"])
        self.assertEqual(argv[3][:2], ["volume", "inspect"])
        unconfigure = argv[4]
        self.assertEqual(unconfigure[0], "run")
        self.assertIn(f"{CONTAINER}-remove", unconfigure)
        self.assertEqual(unconfigure[-2:], ["./config.sh", "remove"])
        self.assertNotIn("--token", unconfigure)
        self.assertEqual([call["env"].get("GITHUB_RUNNER_GH_STOP_GRACE_SECONDS") for call in self.docker_calls()][4], "1")
        self.assert_token_only_on_the_container_stdin()
        self.assertEqual(argv[5], ["rm", CONTAINER])
        self.assertEqual(argv[6][:2], ["ps", "--filter"])
        self.assertEqual(argv[7][:2], ["volume", "ls"])
        self.assertEqual(argv[8][:2], ["volume", "inspect"])
        self.assertEqual(argv[9], ["volume", "rm", VOLUME])
        # stop, check-containers, check-volume (ls, inspect), remove-token, get, rm, check-containers, check-volume (ls, inspect), volume-rm
        self.assertEqual([(c["duration"], c["kill_after"]) for c in self.timeout_calls()], [(18, 3), (2, 3), (2, 3), (2, 3), (21, 3), (5, 3), (2, 3), (2, 3), (2, 3), (2, 3), (2, 3)])
        self.assertFalse((self.state_dir / f"{SLUG}.json").exists(), "a removed registration has no state file")
        self.assertEqual(self.token_files(), [], "the helper removed its token file")

    def test_remove_with_a_given_token_file_skips_the_helper_handoff(self) -> None:
        self.seed_persistent()
        token_file = self.temp_dir / "remove-token"
        token_file.write_text(TOKEN + "\n", encoding="utf-8")
        self.set_docker({"run": {"exit": 0}})
        self.set_fixture({RUNNER_26: NOT_FOUND})
        result = self.run_runnerctl("remove", SLUG, "--token-file", str(token_file))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(str(token_file), " ".join(self.docker_argv()[4]))
        self.assertNotIn("remove-token", " ".join(self.api_endpoints()))
        self.assert_token_only_on_the_container_stdin()

    def test_remove_deletes_a_registration_github_still_lists_under_the_exact_name(self) -> None:
        self.seed_persistent()
        self.set_docker({"run": {"exit": 0}})
        self.set_fixture({**token_fixture("remove-token"), RUNNER_26: [{"body": runner(26, NAME, ephemeral=None)}, {"body": runner(26, NAME, ephemeral=None)}, NOT_FOUND], DELETE_26: {"body": None}})
        result = self.run_runnerctl("remove", SLUG)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(DELETE_26, self.api_endpoints())
        self.assertEqual([p[0] for p in self.phases()], [])
        self.assertFalse((self.state_dir / f"{SLUG}.json").exists())

    def test_remove_leaves_a_registration_with_another_name_alone(self) -> None:
        self.seed_persistent()
        self.set_docker({"run": {"exit": 0}})
        self.set_fixture({**token_fixture("remove-token"), RUNNER_26: {"body": runner(26, "someone-else", ephemeral=None)}})
        result = self.run_runnerctl("remove", SLUG)
        self.assertEqual(result.returncode, 1)
        self.assertIn("under another name", result.stderr)
        self.assertNotIn(DELETE_26, self.api_endpoints())
        self.assertNotIn(["volume", "rm", VOLUME], self.docker_argv())
        self.assertEqual(self.registration(SLUG)["state"], "incomplete")

    def test_remove_of_an_ephemeral_registration_skips_the_removal_token(self) -> None:
        self.seed_persistent(mode="ephemeral")
        self.set_docker({})
        self.set_fixture({RUNNER_26: NOT_FOUND})
        result = self.run_runnerctl("remove", SLUG)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([a[0] for a in self.docker_argv()], ["stop", "ps", "volume", "volume", "rm", "ps", "volume", "volume", "volume"])
        self.assertEqual([e for e in self.api_endpoints() if "token" in e], [])

    def test_remove_bounds_every_phase_and_the_worst_case_stays_inside_the_stop_budget(self) -> None:
        self.seed_persistent()
        self.set_docker({"stop": {"hang": True}, "run": {"hang": True}, "rm": {"hang": True}, "volume rm": {"hang": True}})
        self.set_fixture({**token_fixture("remove-token"), RUNNER_26: {"body": runner(26, NAME, ephemeral=None)}, DELETE_26: {"hang": True}})
        result = self.run_runnerctl("remove", SLUG, timeout=90)
        self.assertEqual(result.returncode, 1, result.stderr)
        phases = self.phases()
        ended_by_timeout = {"124", "137"}
        self.assertEqual([p[0] for p in phases if not p[0].startswith("check-")], ["stop-container", "remove-token", "get", "delete", "rm"])
        for name, _, _, result in phases:
            with self.subTest(phase=name):
                self.assertIn(result, ended_by_timeout if name in ("stop-container", "remove-token", "delete", "rm") else {"0"})
        budgets = {"stop-container": 21, "remove-token": 24, "get": 8, "delete": 12, "rm": 5, "volume-rm": 5, "check-containers": 5, "check-volume": 5, "check-volume-label": 5}
        for name, started, ended, _ in phases:
            with self.subTest(phase=name):
                # stop: 18 + 4; remove-token: 21, then the helper's own grace ends it before timeout's KILL; delete: 9 + 4; rm: 2 + 4.
                self.assertLessEqual(ended - started, budgets[name])
        elapsed = phases[-1][2] - phases[0][1]
        # Every phase at least reached its TERM point; whether a tree ended before the kill grace is not modelled.
        self.assertGreaterEqual(elapsed, 18 + 21 + 9 + 2)
        self.assertLessEqual(elapsed, 75)
        self.assertEqual(self.registration(SLUG)["state"], "incomplete")

    def test_remove_stays_inside_the_stop_budget_when_the_container_is_started_again_once(self) -> None:
        # Both stops hang and every later phase hangs too: the second stop and what follows only get what is left.
        self.seed_persistent()
        self.set_docker({"stop": {"hang": True}, "ps": [{"stdout": f"{CONTAINER}\n"}, {"stdout": ""}], "run": {"hang": True}, "rm": {"hang": True}, "volume rm": {"hang": True}})
        self.set_fixture({**token_fixture("remove-token"), RUNNER_26: {"body": runner(26, NAME, ephemeral=None)}, DELETE_26: {"hang": True}})
        result = self.run_runnerctl("remove", SLUG, timeout=120)
        self.assertEqual(result.returncode, 1, result.stderr)
        phases = self.phases()
        self.assertEqual([p[0] for p in phases][:3], ["stop-container", "check-containers", "stop-container-again"])
        self.assertLessEqual(phases[-1][2] - phases[0][1], 75)
        skipped = [p[0] for p in phases if p[3] == "skipped"]
        self.assertTrue(skipped, "phases without budget left are recorded as skipped")
        self.assertEqual(self.registration(SLUG)["state"], "incomplete")

    def test_remove_bounds_the_volume_removal_and_leaves_a_foreign_volume_alone(self) -> None:
        with self.subTest(case="volume rm hangs"):
            self.seed_persistent(mode="ephemeral")
            self.set_docker({"volume rm": {"hang": True}})
            self.set_fixture({RUNNER_26: NOT_FOUND})
            result = self.run_runnerctl("remove", SLUG, timeout=60)
            self.assertEqual(result.returncode, 1)
            self.assertIn("could not be removed in time", result.stderr)
            self.assertIn([p for p in self.phases() if p[0] == "volume-rm"][0][3], ("124", "137"))
            self.assertEqual(self.registration(SLUG)["state"], "incomplete")
        with self.subTest(case="volume without the label"):
            (self.state_dir / f"{SLUG}.json").unlink()
            self.seed_persistent(mode="ephemeral")
            self.set_docker({"volume inspect": {"stdout": "someone-else\n"}})
            self.set_fixture({RUNNER_26: NOT_FOUND})
            result = self.run_runnerctl("remove", SLUG)
            self.assertEqual(result.returncode, 1)
            self.assertIn("does not carry the label", result.stderr)
            self.assertNotIn(["volume", "rm", VOLUME], self.docker_argv())
            self.assertEqual(self.registration(SLUG)["state"], "incomplete")
        for name, answer in (("volume already gone", ""),):
            with self.subTest(case=name):
                (self.state_dir / f"{SLUG}.json").unlink(missing_ok=True)
                self.seed_persistent(mode="ephemeral")
                self.set_docker({"volume ls": {"stdout": answer}})
                self.set_fixture({RUNNER_26: NOT_FOUND})
                result = self.run_runnerctl("remove", SLUG)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn(["volume", "rm", VOLUME], self.docker_argv())
                self.assertFalse((self.state_dir / f"{SLUG}.json").exists())

    def test_remove_bounds_the_ownership_checks_and_stops_when_they_do_not_answer(self) -> None:
        for name, docker in (("docker ps hangs", {"ps": {"hang": True}}), ("volume ls hangs", {"volume ls": {"hang": True}})):
            with self.subTest(case=name):
                (self.state_dir / f"{SLUG}.json").unlink(missing_ok=True)
                self.seed_persistent()
                self.set_docker(docker)
                self.set_fixture({**token_fixture("remove-token"), RUNNER_26: NOT_FOUND})
                result = self.run_runnerctl("remove", SLUG, timeout=60)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("in time; the registration is left for reconcile", result.stderr)
                phases = self.phases()
                self.assertLessEqual(phases[-1][2] - phases[0][1], 75)
                self.assertNotIn("remove-token", [p[0] for p in phases], "no removal token while ownership is unknown")
                self.assertEqual(self.registration(SLUG)["state"], "incomplete")

    def test_remove_treats_a_failed_ownership_check_as_unknown_not_as_absence(self) -> None:
        cases = {
            "volume ls killed": ({"volume ls": {"exit": 137}}, "could not be inspected in time"),
            "volume ls daemon error": ({"volume ls": {"exit": 1, "stderr": "Cannot connect to the Docker daemon\n"}}, "could not be inspected in time"),
            "volume inspect daemon error": ({"volume inspect": {"exit": 1, "stderr": "Cannot connect to the Docker daemon\n"}}, "could not be inspected in time"),
            "docker ps daemon error": ({"ps": {"exit": 1, "stderr": "Cannot connect to the Docker daemon\n"}}, "could not be listed in time"),
        }
        for name, (docker, message) in cases.items():
            with self.subTest(case=name):
                (self.state_dir / f"{SLUG}.json").unlink(missing_ok=True)
                self.seed_persistent(mode="ephemeral")
                self.set_docker(docker)
                self.set_fixture({RUNNER_26: NOT_FOUND})
                result = self.run_runnerctl("remove", SLUG)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn(message, result.stderr)
                self.assertNotIn(["volume", "rm", VOLUME], self.docker_argv())
                self.assertTrue((self.state_dir / f"{SLUG}.json").exists(), "the state is kept")
                self.assertEqual(self.registration(SLUG)["state"], "incomplete")

    def test_run_reports_readiness_while_the_log_follower_keeps_running(self) -> None:
        import signal as _signal

        self.seed_persistent()
        # The line is printed, then the follower stays open as docker logs --follow does on an idle listener.
        self.set_docker({"ps {{.State}}": {"stdout": ""}, "logs": {"stdout": self.LISTENING, "block": True}, "run": {"exit": 0}})
        self.set_fixture({**token_fixture("remove-token"), RUNNER_26: NOT_FOUND})
        process = self.start_run()
        self.wait_for(lambda: ["--ready"] in self.notify_calls(), "READY=1 despite the open follower")
        self.wait_for(lambda: any(a[0] == "wait" for a in self.docker_argv()), "docker wait")
        process.send_signal(_signal.SIGTERM)
        status, _, stderr = self.finish(process)
        self.assertEqual(status, 0, stderr)

    def test_status_reads_one_locked_snapshot_and_a_removed_registration_is_reported_as_missing(self) -> None:
        import fcntl
        import subprocess as _subprocess
        import time as _time

        self.seed_persistent()
        self.set_docker({"ps {{.State}}": {"stdout": "running\n"}})
        self.set_fixture({RUNNER_26: {"body": runner(26, NAME, ephemeral=None)}})
        lock = open(self.state_dir / f"{SLUG}.state.lock", "w")
        fcntl.flock(lock, fcntl.LOCK_EX)
        process = _subprocess.Popen([str(RUNNERCTL), "status", SLUG], env=self.env, stdout=_subprocess.PIPE, stderr=_subprocess.PIPE, text=True)
        _time.sleep(0.7)
        self.assertIsNone(process.poll(), "status waits for the state lock before it reads")
        (self.state_dir / f"{SLUG}.json").unlink()
        lock.close()
        stdout, stderr = process.communicate(timeout=20)
        self.assertEqual(process.returncode, 1)
        self.assertIn(f"no registration {SLUG}", stderr)
        self.assertEqual(stdout, "", "nothing half read")

    def test_remove_ends_the_path_when_the_container_is_started_again_twice(self) -> None:
        self.seed_persistent()
        self.set_docker({"ps": [{"stdout": f"{CONTAINER}\n"}, {"stdout": f"{CONTAINER}\n"}]})
        self.set_fixture({})
        result = self.run_runnerctl("remove", SLUG)
        self.assertEqual(result.returncode, 1)
        self.assertIn("started again during the stop path", result.stderr)
        self.assertEqual([p[0] for p in self.phases()], ["stop-container", "check-containers", "stop-container-again", "check-containers"])
        self.assertEqual(self.registration(SLUG)["state"], "incomplete")
        self.assertEqual([e for e in self.api_endpoints() if "runners" in e], [])

    def test_remove_refuses_while_run_holds_the_registration(self) -> None:
        import fcntl

        self.seed_persistent()
        self.set_docker({})
        self.set_fixture({})
        lock = open(self.state_dir / f"{SLUG}.run.lock", "w")
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            result = self.run_runnerctl("remove", SLUG)
        finally:
            lock.close()
        self.assertEqual(result.returncode, 1)
        self.assertIn("stop the service instead", result.stderr)
        self.assertEqual(self.docker_calls(), [])

    LISTENING = "Runner listener\n√ Connected to GitHub\n\nCurrent runner version: '2.337.0'\n2026-09-27 10:00:00Z: Listening for Jobs\n"

    def start_run(self, env: dict | None = None):
        import subprocess

        return subprocess.Popen(
            [str(RUNNERCTL), "run", SLUG], env={**self.env, "NOTIFY_SOCKET": str(self.temp_dir / "notify.sock"), **(env or {})},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
        )

    def wait_for(self, condition, what: str, timeout: float = 10) -> None:
        import time

        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > deadline:
                self.fail(f"timed out waiting for {what}")
            time.sleep(0.05)

    def finish(self, process, timeout: float = 30) -> tuple[int, str, str]:
        import os as _os
        import signal as _signal

        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except Exception:
            _os.killpg(process.pid, _signal.SIGKILL)
            stdout, stderr = process.communicate()
            self.fail(f"runnerctl run did not finish: {stderr}")
        return process.returncode, stdout, stderr

    def test_run_supervises_the_listener_and_runs_the_stop_path_on_term(self) -> None:
        import signal as _signal

        self.seed_persistent()
        self.set_docker({"ps {{.State}}": {"stdout": ""}, "logs": {"stdout": self.LISTENING}, "run": {"exit": 0}})
        self.set_fixture({**token_fixture("remove-token"), RUNNER_26: NOT_FOUND})
        process = self.start_run()
        self.wait_for(lambda: ["--ready"] in self.notify_calls(), "READY=1")
        self.wait_for(lambda: any(a[0] == "wait" for a in self.docker_argv()), "docker wait")
        self.assertEqual(self.registration(SLUG)["state"], "running")
        argv = self.docker_argv()
        create = next(a for a in argv if a[0] == "create")
        self.assertIn(LABEL, create)
        self.assertIn(f"type=volume,src={VOLUME},dst=/home/runner", create)
        self.assertEqual(create[-2:], [IMAGE, "./run.sh"])
        self.assertIn(["start", CONTAINER], argv)
        self.assertEqual([a for a in argv if a[0] == "wait"], [["wait", CONTAINER]])
        process.send_signal(_signal.SIGTERM)
        status, stdout, stderr = self.finish(process)
        self.assertEqual(status, 0, stderr)
        self.assertIn(["--stopping"], self.notify_calls())
        phases = [a[0] for a in self.docker_argv() if a[0] in ("stop", "rm") or a[:2] == ["volume", "rm"]]
        self.assertEqual(phases, ["stop", "rm", "volume"])
        self.assertFalse((self.state_dir / f"{SLUG}.json").exists())
        self.assertEqual(self.token_files(), [])

    def test_run_answers_a_stop_that_arrives_while_the_container_starts(self) -> None:
        import signal as _signal

        self.seed_persistent()
        self.set_docker({"ps {{.State}}": {"stdout": ""}, "start": {"block": True}, "run": {"exit": 0}})
        self.set_fixture({**token_fixture("remove-token"), RUNNER_26: NOT_FOUND})
        process = self.start_run()
        self.wait_for(lambda: any(a[0] == "start" for a in self.docker_argv()), "docker start")
        process.send_signal(_signal.SIGTERM)
        # docker start is a foreground command, so the trap runs once it returns; the fake blocks until TERM.
        import subprocess as _subprocess
        _subprocess.run(["pkill", "-TERM", "-f", f"docker start {CONTAINER}"], check=False)
        status, _, stderr = self.finish(process)
        self.assertEqual(status, 0, stderr)
        self.assertNotIn(["--ready"], self.notify_calls())
        self.assertIn(["--stopping"], self.notify_calls())
        self.assertIn(["stop", "--time", "15", CONTAINER], self.docker_argv())
        self.assertFalse((self.state_dir / f"{SLUG}.json").exists())

    def test_run_bounds_every_setup_call_and_leaves_the_registration_for_the_unit_to_retry(self) -> None:
        absent = {"stdout": ""}
        cases = {
            "docker info hangs": ({"info": {"hang": True}}, {"require_userns": "true"}, "docker info"),
            "docker ps fails": ({"ps {{.State}}": {"exit": 1, "stderr": "Cannot connect to the Docker daemon\n"}}, {}, "docker ps"),
            "docker create fails": ({"ps {{.State}}": absent, "create": {"exit": 125, "stderr": "docker: Error response from daemon\n"}}, {}, "docker create"),
            "docker start hangs": ({"ps {{.State}}": absent, "start": {"hang": True}}, {}, "docker start"),
        }
        for name, (docker, fields, failed_call) in cases.items():
            with self.subTest(case=name):
                (self.state_dir / f"{SLUG}.json").unlink(missing_ok=True)
                self.seed_persistent(**fields)
                self.set_docker(docker)
                self.set_fixture({**token_fixture("remove-token"), RUNNER_26: {"body": runner(26, NAME, ephemeral=None)}})
                result = self.run_runnerctl("run", SLUG, env={"NOTIFY_SOCKET": "x"}, timeout=60)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn(f"{failed_call} did not complete within 30s or failed; the registration {SLUG} is left as it is for the unit to retry", result.stderr)
                bounded = next(c for c in self.timeout_calls() if c["argv"][:2] == failed_call.split())
                # the container listing runs under the 5 s check budget, the other setup calls under the 30 s setup budget
                self.assertEqual(bounded["duration"], (5 if failed_call == "docker ps" else 30) - 3, "every setup call is bounded")
                self.assertNotIn(["--ready"], self.notify_calls())
                self.assertNotIn(["--stopping"], self.notify_calls())
                self.assertEqual([a for a in self.docker_argv() if a[0] == "stop"], [], "no stop path: nothing is deregistered for a setup failure")
                self.assertEqual([e for e in self.api_endpoints() if "token" in e or "DELETE" in e], [], "no removal token, no deletion")
                self.assertEqual(self.registration(SLUG)["state"], "registered")

    def test_run_stops_even_when_docker_wait_ignores_term(self) -> None:
        import signal as _signal

        self.seed_persistent()
        self.set_docker({"ps {{.State}}": {"stdout": ""}, "logs": {"stdout": self.LISTENING}, "run": {"exit": 0}})
        self.set_fixture({**token_fixture("remove-token"), RUNNER_26: NOT_FOUND})
        process = self.start_run(env={"FAKE_DOCKER_WAIT_IGNORES_TERM": "1"})
        self.wait_for(lambda: any(a[0] == "wait" for a in self.docker_argv()), "docker wait")
        process.send_signal(_signal.SIGTERM)
        status, _, stderr = self.finish(process)
        self.assertEqual(status, 0, stderr)
        self.assertIn(2, self.sleeps(), "the grace before KILL of docker wait")

    def test_run_answers_a_stop_during_the_readiness_wait(self) -> None:
        import signal as _signal

        self.seed_persistent()
        self.set_docker({"ps {{.State}}": {"stdout": ""}, "logs": {"stdout": "starting\n", "block": True}, "run": {"exit": 0}})
        self.set_fixture({**token_fixture("remove-token"), RUNNER_26: NOT_FOUND})
        process = self.start_run()
        self.wait_for(lambda: any(a[0] == "logs" for a in self.docker_argv()), "the readiness wait")
        process.send_signal(_signal.SIGTERM)
        status, _, stderr = self.finish(process)
        self.assertEqual(status, 0, stderr)
        self.assertNotIn(["--ready"], self.notify_calls())
        self.assertIn(["--stopping"], self.notify_calls())
        self.assertFalse((self.state_dir / f"{SLUG}.json").exists())

    def test_run_stops_a_listener_that_never_reports_readiness(self) -> None:
        self.seed_persistent()
        self.set_docker({"ps {{.State}}": {"stdout": ""}, "logs": {"stdout": "starting\n", "hang": True}, "run": {"exit": 0}})
        self.set_fixture({**token_fixture("remove-token"), RUNNER_26: NOT_FOUND})
        # Nothing else hangs, so the fake timeout charges the readiness budget and the run stops on its own.
        result = self.run_runnerctl("run", SLUG, env={"NOTIFY_SOCKET": "x"}, timeout=60)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("did not report \"Listening for Jobs\" within 120s", result.stderr)
        readiness_call = next(c for c in self.timeout_calls() if "readiness" in c["argv"])
        self.assertEqual(readiness_call["duration"], 120 - 3)
        self.assertIn(["--stopping"], self.notify_calls())

    def test_run_of_a_persistent_listener_that_exits_reports_the_code_for_the_unit_to_restart(self) -> None:
        self.seed_persistent()
        self.set_docker({"ps {{.State}}": {"stdout": ""}, "logs": {"stdout": self.LISTENING}, "wait": {"stdout": "2\n"}})
        self.set_fixture({})
        result = self.run_runnerctl("run", SLUG, env={"NOTIFY_SOCKET": "x"})
        self.assertEqual(result.returncode, 1)
        self.assertIn("exited with code 2", result.stderr)
        self.assertEqual(self.registration(SLUG)["state"], "exited")
        self.assertEqual([a[0] for a in self.docker_argv() if a[0] in ("stop", "rm")], [], "no stop path: the unit restarts it")

    def test_run_of_an_ephemeral_listener_ends_with_evidence_or_exit_3(self) -> None:
        for evidence, expected in ((True, 0), (False, 3)):
            with self.subTest(evidence=evidence):
                (self.state_dir / f"{SLUG}.json").unlink(missing_ok=True)
                fields = {"assignment": "100\t7\tbuild\tcompleted\t26\t" + NAME + "\tsuccess", "assignment_run_id": "100"} if evidence else {}
                self.seed_persistent(mode="ephemeral", **fields)
                self.set_docker({"ps {{.State}}": {"stdout": ""}, "logs": {"stdout": self.LISTENING}, "wait": {"stdout": "0\n"}})
                self.set_fixture({RUNNER_26: NOT_FOUND})
                result = self.run_runnerctl("run", SLUG, env={"NOTIFY_SOCKET": "x"})
                self.assertEqual(result.returncode, expected, result.stderr)
                self.assertEqual([e for e in self.api_endpoints() if "token" in e], [], "no removal token for an ephemeral runner")
                self.assertFalse((self.state_dir / f"{SLUG}.json").exists(), "the registration is cleaned up either way")
                if not evidence:
                    self.assertIn("without recorded assignment evidence", result.stderr)

    def test_run_refuses_a_running_container_an_incomplete_state_and_a_missing_userns(self) -> None:
        cases = {
            "running container": ({"ps {{.State}}": {"stdout": "running\n"}}, {}, "already running"),
            "incomplete state": ({}, {"state": "incomplete"}, "run reconcile first"),
            "userns required": ({"info": {"stdout": "[name=apparmor name=seccomp,profile=builtin]\n"}, "ps {{.State}}": {"stdout": ""}}, {"require_userns": "true"}, "requires user-namespace remapping"),
        }
        for name, (docker, fields, message) in cases.items():
            with self.subTest(case=name):
                (self.state_dir / f"{SLUG}.json").unlink(missing_ok=True)
                self.seed_persistent(**fields)
                self.set_docker(docker)
                self.set_fixture({})
                result = self.run_runnerctl("run", SLUG)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn(message, result.stderr)
                self.assertNotIn("start", [a[0] for a in self.docker_argv()])

    def test_verify_records_the_helper_evidence_beside_the_state(self) -> None:
        from gh_harness import job, jobs_key, jobs_page

        self.seed_persistent()
        self.set_docker({})
        self.set_fixture({jobs_key(100): jobs_page(job(7, "build", "completed", 26, NAME, conclusion="success"))})
        result = self.run_runnerctl("verify", SLUG, "100")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[1], f"100\t7\tbuild\tcompleted\t26\t{NAME}\tsuccess")
        self.assertEqual(self.registration(SLUG)["assignment_run_id"], "100")
        self.assertEqual(self.registration(SLUG)["assignment"], f"100\t7\tbuild\tcompleted\t26\t{NAME}\tsuccess")
        self.assertEqual(stat.S_IMODE((self.state_dir / f"{SLUG}.json").stat().st_mode), 0o600)

    def test_reconcile_finishes_an_interrupted_stop_by_exact_id_and_name(self) -> None:
        cases = {
            "already gone": ({RUNNER_26: NOT_FOUND}, 0, False),
            "still listed under the exact name": ({RUNNER_26: [{"body": runner(26, NAME, ephemeral=None)}, {"body": runner(26, NAME, ephemeral=None)}, NOT_FOUND], DELETE_26: {"body": None}}, 0, True),
            "listed under another name": ({RUNNER_26: {"body": runner(26, "other", ephemeral=None)}}, 1, False),
        }
        for name, (fixture, expected, deleted) in cases.items():
            with self.subTest(case=name):
                (self.state_dir / f"{SLUG}.json").unlink(missing_ok=True)
                self.seed_persistent(state="incomplete")
                self.set_docker({})
                self.set_fixture(fixture)
                result = self.run_runnerctl("reconcile", SLUG)
                self.assertEqual(result.returncode, expected, result.stderr)
                self.assertEqual(DELETE_26 in self.api_endpoints(), deleted)
                self.assertEqual([e for e in self.api_endpoints() if "token" in e], [])
                self.assertEqual((self.state_dir / f"{SLUG}.json").exists(), expected == 1)

    def test_reconcile_refuses_while_the_container_runs(self) -> None:
        self.seed_persistent(state="incomplete")
        self.set_docker({"ps": {"stdout": f"{CONTAINER}\n"}})
        self.set_fixture({})
        result = self.run_runnerctl("reconcile", SLUG)
        self.assertEqual(result.returncode, 1)
        self.assertIn("stop the service instead", result.stderr)

    def test_unit_template_matches_the_manager_contract(self) -> None:
        unit = (RUNNERCTL.parent.parent / "systemd" / "user" / "runnerctl@.service").read_text(encoding="utf-8")
        for line in ("Type=notify", "NotifyAccess=main", "KillMode=mixed", "TimeoutStopSec=90", "Restart=on-failure", "StateDirectory=runnerctl", "ExecStart=%h/github-runner-gh-skill/scripts/runnerctl run %i", "Environment=RUNNERCTL_STATE_DIR=%S/runnerctl", "WantedBy=default.target"):
            self.assertIn(line, unit)
        self.assertNotIn("ExecStop=", unit)
        self.assertNotIn("User=", unit)

    def test_status_bounds_the_container_listing_and_reports_a_daemon_failure_as_unknown(self) -> None:
        cases = {
            "listing hangs": ({"ps {{.State}}": {"hang": True}}, "container\tunknown (the container listing did not complete)"),
            "daemon unreachable": ({"ps {{.State}}": {"exit": 1, "stderr": "Cannot connect to the Docker daemon\n"}}, "container\tunknown (the container listing did not complete)"),
            "nothing listed": ({"ps {{.State}}": {"stdout": ""}}, "container\tabsent"),
            "created": ({"ps {{.State}}": {"stdout": "created\n"}}, "container\tcreated"),
        }
        for name, (docker, line) in cases.items():
            with self.subTest(case=name):
                (self.state_dir / f"{SLUG}.json").unlink(missing_ok=True)
                self.seed_persistent()
                self.set_docker(docker)
                self.set_fixture({RUNNER_26: {"body": runner(26, NAME, ephemeral=None)}})
                result = self.run_runnerctl("status", SLUG, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(line, result.stdout.splitlines())
                self.assertEqual(self.timeout_calls()[0]["duration"], 5 - 3, "the listing is bounded")

    def test_status_bounds_the_github_lookup(self) -> None:
        self.seed_persistent()
        self.set_docker({"ps {{.State}}": {"stdout": "running\n"}})
        self.set_fixture({RUNNER_26: {"hang": True}})
        result = self.run_runnerctl("status", SLUG, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("github\tunknown (the lookup did not complete within 8s)", result.stdout.splitlines())
        lookup = next(c for c in self.timeout_calls() if "get" in c["argv"])
        self.assertEqual(lookup["duration"], 8 - 3)

    def test_run_exits_4_without_a_registration_so_the_unit_does_not_loop(self) -> None:
        self.set_docker({})
        self.set_fixture({})
        result = self.run_runnerctl("run", SLUG)
        self.assertEqual(result.returncode, 4)
        self.assertIn("register again before starting the unit", result.stderr)
        unit = (RUNNERCTL.parent.parent / "systemd" / "user" / "runnerctl@.service").read_text(encoding="utf-8")
        self.assertIn("RestartPreventExitStatus=3 4", unit)

    def test_status_without_a_registration_fails(self) -> None:
        self.set_docker({})
        result = self.run_runnerctl("status", SLUG)
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"no registration {SLUG}", result.stderr)


if __name__ == "__main__":
    unittest.main()
