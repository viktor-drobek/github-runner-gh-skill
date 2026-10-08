#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import signal
import stat
import subprocess
import sys
import unittest
from pathlib import Path

# Lets any runner (discover, python -m unittest tests.test_skill, direct execution) import the harness.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from gh_harness import (  # noqa: E402
    BAD_GATEWAY,
    DEFAULT_LABELS,
    DELETE_22,
    FORBIDDEN,
    HELPER_PID,
    NETWORK_ERROR,
    NOT_FOUND,
    NOT_FOUND_HTML,
    RATE_LIMIT,
    RATE_LIMITED,
    REPO,
    ROOT,
    RUNNER_22,
    RUNNERS,
    RUNS,
    SCRIPT,
    SHA,
    SECONDARY_RATE_LIMITED,
    SIGNALS,
    START_TIME,
    TOKEN,
    TOKEN_COMMANDS,
    UNAUTHORIZED,
    USER,
    USER_RESPONSE,
    WITH_TOKEN_COMMANDS,
    GhScriptTestCase,
    commit_runs_key,
    dispatch_key,
    job,
    jobs_key,
    jobs_page,
    paged,
    run,
    runner,
    runner_key,
    runners_page,
    runs_page,
    token_fixture,
    workflow_runs_key,
)

SKILL = ROOT / "SKILL.md"
README = ROOT / "README.md"
JOBS_100 = jobs_key(100)
RUNNER_ROW = "22\trunner-a\tlinux\tonline\tfalse\ttrue\tself-hosted,Linux,repository-build"
ASSIGNMENT_HEADER = "run_id\tjob_id\tjob_name\tstatus\trunner_id\trunner_name\tconclusion"
ACCEPTED_DELETION = f"GitHub accepted the deletion of runner 22 (retired-runner) from {REPO}"
CONFIRM_HINT = "run 'get 22': 'GitHub answers 404' means the runner is gone"
RUNNER_404 = f"GitHub answers 404 for runner 22 in {REPO}: no repository-level runner has that ID"
GH_403 = "gh: Resource not accessible by personal access token (HTTP 403)"


def bash_blocks(document: Path) -> list[str]:
    return re.findall(r"```bash\n(.*?)```", document.read_text(encoding="utf-8"), re.DOTALL)


def script_constant(name: str) -> str:
    match = re.search(rf"^readonly {name}=(\d+)$", SCRIPT.read_text(encoding="utf-8"), re.MULTILINE)
    assert match, name
    return match.group(1)


class DocumentationContractTest(unittest.TestCase):
    def test_skill_metadata_and_scope(self) -> None:
        text = SKILL.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\nname: github-runner-gh-skill\n"))
        self.assertIn("gh auth status", text)
        self.assertIn("registration-token", text)
        self.assertIn("--method DELETE", text)

    def test_security_invariants_are_documented(self) -> None:
        text = SKILL.read_text(encoding="utf-8")
        for required in (
            "EPHEMERAL=true",
            "Listening for Jobs",
            "in_progress",
            "reserved default label",
            "0600",
            "check RUNNER_ID",
            "with-registration-token",
        ):
            self.assertIn(required, text)

    def test_runner_modes_are_documented(self) -> None:
        skill = SKILL.read_text(encoding="utf-8")
        readme = README.read_text(encoding="utf-8")
        for required in ("ephemeral or persistent", "check RUNNER_ID MODE", "--ephemeral", "Do not infer"):
            self.assertIn(required, skill)
        self.assertIn('check RUNNER_ID "$mode"', skill)
        self.assertIn('check RUNNER_ID "$mode" "$run_id" EXACT_RUNNER_NAME', skill)
        self.assertIn('check RUNNER_ID "$mode" "$run_id" EXACT_RUNNER_NAME', readme)
        self.assertNotIn("check RUNNER_ID ephemeral", readme, "the README example takes the mode from a variable")
        for required in ("ephemeral or persistent", "check RUNNER_ID MODE", "persistent (constant)"):
            self.assertIn(required, readme)

    def test_verification_workflow_template_selects_only_the_named_runner(self) -> None:
        template = (ROOT / "templates" / "verify-runner.yml").read_text(encoding="utf-8")
        self.assertIn("run-name: verify ${{ inputs.marker }}", template)
        self.assertRegex(template, r"(?ms)^\s+marker:\n(?:\s+\S.*\n)*?\s+required: true")
        self.assertRegex(template, r"(?ms)^\s+runner_label:\n(?:\s+\S.*\n)*?\s+required: true")
        self.assertNotRegex(template, r"(?m)^\s+default:")
        self.assertIn("permissions: {}", template)
        self.assertRegex(template, r"(?m)^    timeout-minutes: 5$", "the job-level timeout is five minutes")
        self.assertIn('runs-on: [self-hosted, "${{ inputs.runner_label }}"]', template)
        # The steps block runs from "steps:" to the end of the job; every list item in it, named or not, is a step.
        steps_block = re.search(r"(?ms)^    steps:\n(.*?)(?=^\S|\Z)", template).group(1)
        items = re.findall(r"(?m)^      - ", steps_block)
        self.assertEqual(len(items), 1, "one verification step")
        self.assertEqual([line.strip() for line in steps_block.strip().splitlines()], ["- name: Report the runner", 'run: echo "verification job ${{ inputs.marker }} on $RUNNER_NAME"'])
        self.assertEqual(template.count("jobs:"), 1)
        for document in (SKILL, README):
            self.assertIn("templates/verify-runner.yml", document.read_text(encoding="utf-8"))

    def test_manual_token_handoff_block_is_a_self_contained_script(self) -> None:
        blocks = [block for block in bash_blocks(SKILL) if re.search(r"trap '[^']*' EXIT", block)]
        self.assertEqual(len(blocks), 1, "SKILL.md owns the single manual token-handoff block")
        block = blocks[0]
        self.assertIn("trap - EXIT", block)
        self.assertIn("-perm 600", block)
        self.assertNotIn("stat -c", block)
        self.assertNotRegex(block, r"(?m)^\s*repo=")
        for line in block.splitlines():
            if line.strip().startswith("test "):
                self.assertRegex(line, r"\|\| exit 1$")
        self.assertFalse(any("trap '" in block for block in bash_blocks(README)), "README defers to SKILL.md")
        self.assertIn("with-registration-token", README.read_text(encoding="utf-8"))

    def test_verification_blocks_wait_on_the_dispatched_run(self) -> None:
        for document in (SKILL, README):
            blocks = [block for block in bash_blocks(document) if "wait-assignment" in block]
            with self.subTest(document=document.name):
                self.assertTrue(blocks)
                blocks_text = "\n".join(bash_blocks(document))
                self.assertIn('marker="verify-$(date +%s)-$$-$RANDOM"', blocks_text)
                self.assertRegex(blocks_text, r'run_id="\$\(\S*scripts/github-runner-gh --repo \S+ dispatch-run \S+ \S+ "marker=\$marker"')
                self.assertNotIn("gh run list", document.read_text(encoding="utf-8"))
                for block in blocks:
                    self.assertRegex(block, r'wait-assignment RUNNER_ID EXACT_RUNNER_NAME "\$run_id" \d+ \|\| exit 1')
                    self.assertNotRegex(block, r"&\s*$")
                    self.assertNotIn("wait_pid", block)

    def test_documented_limits_match_script_constants(self) -> None:
        default = script_constant("DEFAULT_TIMEOUT_SECONDS")
        maximum = script_constant("MAX_TIMEOUT_SECONDS")
        interval = script_constant("POLL_INTERVAL_SECONDS")
        grace = script_constant("DEFAULT_STOP_GRACE_SECONDS")
        max_grace = script_constant("MAX_STOP_GRACE_SECONDS")
        for document in (SKILL, README):
            text = document.read_text(encoding="utf-8")
            with self.subTest(document=document.name):
                self.assertIn(f"defaults to {default}", text)
                self.assertIn(f"must not exceed {maximum}", text)
                self.assertIn(f"every {interval} seconds", text)
        skill = SKILL.read_text(encoding="utf-8")
        self.assertIn(f"`GITHUB_RUNNER_GH_STOP_GRACE_SECONDS` (a positive integer up to {max_grace}, default {grace})", skill)

    def test_script_uses_gh_for_github_access(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("gh api", text)
        self.assertNotIn("curl ", text)
        self.assertNotIn("wget ", text)
        self.assertNotIn("gh auth token", text)
        self.assertNotIn("api.github.com", text)
        self.assertNotIn("nameWithOwner", text)

    def test_script_has_valid_bash_syntax_and_is_executable(self) -> None:
        subprocess.run(["bash", "-n", str(SCRIPT)], check=True)
        self.assertTrue(os.access(SCRIPT, os.X_OK))

    def test_usage_reads_the_timeout_constants(self) -> None:
        result = subprocess.run([str(SCRIPT)], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn(f"defaults to {script_constant('DEFAULT_TIMEOUT_SECONDS')}", result.stderr)
        self.assertIn(f"must not exceed {script_constant('MAX_TIMEOUT_SECONDS')}", result.stderr)

    def test_readme_uses_gh_for_cloning_and_notes_output_changes(self) -> None:
        text = README.read_text(encoding="utf-8")
        self.assertIn("gh repo clone", text)
        self.assertIn("`ephemeral` column", text)
        self.assertIn("`conclusion` column", text)
        self.assertIn("RUN_ID", text)

    def test_tests_run_by_module_path_from_repository_root(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "unittest", "tests.test_skill.DocumentationContractTest.test_script_uses_gh_for_github_access"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class ScriptBehaviorTest(GhScriptTestCase):
    def test_token_files_are_private_and_never_printed(self) -> None:
        for command, endpoint in TOKEN_COMMANDS:
            with self.subTest(command=command):
                self.set_fixture(token_fixture(endpoint))
                result = self.run_in_repo(command)
                self.assert_succeeded(result)
                self.assertNotIn(TOKEN, result.stdout + result.stderr)
                token_file = Path(result.stdout.strip())
                self.assertEqual(self.token_files(), [token_file])
                self.assertEqual(token_file.read_text(encoding="utf-8").strip(), TOKEN)
                self.assertEqual(stat.S_IMODE(token_file.stat().st_mode), 0o600)
                self.assertNotIn(TOKEN, json.dumps(self.calls()))

    def test_token_response_without_token_fails_and_leaves_no_file(self) -> None:
        cases = {
            "missing": {"body": {"expires_at": "2026-01-01T00:00:00Z"}},
            "null": {"body": {"token": None}},
            "empty": {"body": {"token": ""}},
            "request failed": NOT_FOUND,
            "network error": NETWORK_ERROR,
        }
        for name, response in cases.items():
            for command, endpoint in TOKEN_COMMANDS:
                with self.subTest(command=command, response=name):
                    self.set_fixture(token_fixture(endpoint, **response))
                    result = self.run_in_repo(command)
                    self.assert_no_token_handoff(result)
                    kind = endpoint.removesuffix("-token")
                    if "status" in response or "exit" in response:
                        self.assertIn(f"failed to request the {kind} token", result.stderr)
                    else:
                        self.assertIn(f"GitHub returned no {kind} token", result.stderr)

    def test_with_token_commands_hand_the_file_to_the_command_and_remove_it(self) -> None:
        recorder = self.temp_dir / "manager.log"
        self.install_stub(
            "fake-manager",
            "#!/usr/bin/env bash\n"
            "# GNU stat takes -c; BSD stat (macOS) takes -f.\n"
            'printf \'%s\\n\' "argv:$*" "env:$GITHUB_RUNNER_TOKEN_FILE" "mode:$(stat -c %a "$1" 2>/dev/null || stat -f %Lp "$1")" "token:$(cat "$1")" >>"$MANAGER_LOG"\n'
            'exit "${MANAGER_EXIT:-0}"\n',
        )
        env = {"MANAGER_LOG": str(recorder)}
        for command, endpoint in WITH_TOKEN_COMMANDS:
            for exit_code in ("0", "7"):
                with self.subTest(command=command, exit_code=exit_code):
                    recorder.unlink(missing_ok=True)
                    self.set_fixture(token_fixture(endpoint))
                    result = self.run_in_repo(command, "fake-manager", "{}", "--flag", env={**env, "MANAGER_EXIT": exit_code})
                    self.assertEqual(result.returncode, int(exit_code), result.stderr)
                    self.assertEqual(result.stdout, "")
                    self.assertNotIn(TOKEN, result.stderr)
                    argv, env_line, mode, token = recorder.read_text(encoding="utf-8").splitlines()
                    path = argv.removeprefix("argv:").split(" ")[0]
                    self.assertTrue(path.startswith(str(self.temp_dir / f"github-runner-{endpoint}.")), path)
                    self.assertEqual(argv, f"argv:{path} --flag")
                    self.assertEqual(env_line, f"env:{path}")
                    self.assertEqual(mode, "mode:600")
                    self.assertEqual(token, f"token:{TOKEN}")
                    self.assertEqual(self.token_files(), [])
                    self.assertNotIn(TOKEN, json.dumps(self.calls()))

    def test_with_token_command_substitutes_the_path_even_when_it_contains_an_ampersand(self) -> None:
        odd_tmpdir = self.temp_dir / "runner&temp"
        odd_tmpdir.mkdir()
        recorder = self.temp_dir / "manager.log"
        self.install_stub(
            "fake-manager",
            '#!/usr/bin/env bash\nprintf \'%s\\n\' "argv:$1" "env:$GITHUB_RUNNER_TOKEN_FILE" "token:$(cat "$1")" >>"$MANAGER_LOG"\n',
        )
        self.set_fixture(token_fixture("registration-token"))
        result = self.run_in_repo(
            "with-registration-token", "fake-manager", "{}", env={"MANAGER_LOG": str(recorder), "TMPDIR": str(odd_tmpdir)}
        )
        self.assert_succeeded(result)
        argv, env_line, token = recorder.read_text(encoding="utf-8").splitlines()
        self.assertEqual(argv.removeprefix("argv:"), env_line.removeprefix("env:"))
        self.assertTrue(argv.startswith(f"argv:{odd_tmpdir}/github-runner-registration-token."), argv)
        self.assertEqual(token, f"token:{TOKEN}")
        self.assertEqual(list(odd_tmpdir.glob("github-runner-*")), [])

    def test_with_token_command_removes_the_file_when_the_command_is_interrupted(self) -> None:
        self.install_stub("fake-manager", "#!/usr/bin/env bash\n" + HELPER_PID + 'echo manager-stderr >&2\nkill -TERM "$helper"\nsleep 30\n')
        self.set_fixture(token_fixture("registration-token"))
        result = self.run_in_repo("with-registration-token", "fake-manager", timeout=5)
        self.assertEqual(result.returncode, 143)
        self.assertEqual(self.token_files(), [])
        self.assertEqual(result.stderr, "manager-stderr\n", "the manager's own output passes through without job-control notices")

    def test_with_token_command_stops_a_manager_that_ignores_the_interrupt(self) -> None:
        pid_file = self.temp_dir / "manager.pid"
        self.install_stub(
            "fake-manager",
            "#!/usr/bin/env bash\n" + HELPER_PID + "HELPER_PID=$helper exec python3 -c 'import os, signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            'open(os.environ["MANAGER_PID_FILE"], "w").write(str(os.getpid()))\n'
            'os.kill(int(os.environ["HELPER_PID"]), signal.SIGTERM)\n'
            "time.sleep(30)'\n",
        )
        self.set_fixture(token_fixture("registration-token"))
        result = self.run_in_repo("with-registration-token", "fake-manager", env={"MANAGER_PID_FILE": str(pid_file)}, timeout=10)
        self.assertEqual(result.returncode, 143)
        self.assertEqual(self.token_files(), [])
        manager_pid = int(pid_file.read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(manager_pid, 0)
        self.assertIn(5, self.sleeps(), "the grace period before KILL")

    def test_with_token_command_stops_the_manager_even_when_its_supervisor_is_stopped(self) -> None:
        # The manager stops the supervisor that started it, which may not have reported the group yet, then
        # interrupts the helper and ignores TERM. Cleanup must still end the manager's group and the supervisor.
        manager_pid_file = self.temp_dir / "manager.pid"
        supervisor_pid_file = self.temp_dir / "supervisor.pid"
        self.install_stub(
            "fake-manager",
            "#!/usr/bin/env bash\n" + HELPER_PID + 'printf \'%s\' "$PPID" >"$SUPERVISOR_PID_FILE"\n'
            'kill -STOP "$PPID"\n'
            "HELPER_PID=$helper exec python3 -c 'import os, signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            'open(os.environ["MANAGER_PID_FILE"], "w").write(str(os.getpid()))\n'
            'os.kill(int(os.environ["HELPER_PID"]), signal.SIGTERM)\n'
            "time.sleep(30)'\n",
        )
        self.set_fixture(token_fixture("registration-token"))
        result = self.run_in_repo(
            "with-registration-token",
            "fake-manager",
            env={"MANAGER_PID_FILE": str(manager_pid_file), "SUPERVISOR_PID_FILE": str(supervisor_pid_file)},
            timeout=15,
        )
        self.assertEqual(result.returncode, 143)
        self.assertEqual(self.token_files(), [])
        for name, pid_file in (("manager", manager_pid_file), ("supervisor", supervisor_pid_file)):
            with self.subTest(process=name):
                pid = int(pid_file.read_text())
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    continue
                os.kill(pid, signal.SIGKILL)
                self.fail(f"the {name} outlived the helper")

    def test_with_token_command_waits_the_configured_grace_period_before_kill(self) -> None:
        self.install_stub(
            "fake-manager",
            "#!/usr/bin/env bash\n" + HELPER_PID + "HELPER_PID=$helper exec python3 -c 'import os, signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            'os.kill(int(os.environ["HELPER_PID"]), signal.SIGTERM)\n'
            "time.sleep(30)'\n",
        )
        self.set_fixture(token_fixture("registration-token"))
        result = self.run_in_repo("with-registration-token", "fake-manager", env={"GITHUB_RUNNER_GH_STOP_GRACE_SECONDS": "25"}, timeout=10)
        self.assertEqual(result.returncode, 143)
        self.assertEqual(self.token_files(), [])
        self.assertIn(25, self.sleeps(), "the configured grace period before KILL")
        self.assertNotIn(5, self.sleeps())

    def test_with_token_command_stops_the_children_of_a_wrapper_manager(self) -> None:
        pid_file = self.temp_dir / "child.pid"
        self.install_stub(
            "fake-manager",
            "#!/usr/bin/env bash\n"
            "python3 -c 'import os, signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            'open(os.environ["CHILD_PID_FILE"], "w").write(str(os.getpid()))\n'
            "time.sleep(30)' &\n"
            'child=$!\n'
            'while [[ ! -s "$CHILD_PID_FILE" ]]; do :; done\n' + HELPER_PID + 'kill -TERM "$helper"\n'
            'wait "$child"\n',
        )
        self.set_fixture(token_fixture("registration-token"))
        result = self.run_in_repo("with-registration-token", "fake-manager", env={"CHILD_PID_FILE": str(pid_file)}, timeout=10)
        self.assertEqual(result.returncode, 143)
        self.assertEqual(self.token_files(), [])
        child_pid = int(pid_file.read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(child_pid, 0)

    def test_with_token_command_lets_the_manager_read_the_terminal(self) -> None:
        recorder = self.temp_dir / "manager.log"
        self.install_stub(
            "fake-manager",
            "#!/usr/bin/env bash\n"
            'read -r answer || exit 9\n'
            'printf \'%s\\n\' "answer:$answer" "token:$(cat "$1")" >>"$MANAGER_LOG"\n',
        )
        self.set_fixture(token_fixture("registration-token"))
        status, output = self.run_in_repo_on_terminal(
            "with-registration-token", "fake-manager", "{}", env={"MANAGER_LOG": str(recorder)}, typed="yes\n"
        )
        self.assertEqual(status, 0, output)
        self.assertEqual(recorder.read_text(encoding="utf-8").splitlines(), ["answer:yes", f"token:{TOKEN}"])
        self.assertNotIn(TOKEN, output)
        self.assertEqual(self.token_files(), [])

    def test_with_token_command_leaves_nothing_behind_when_the_handoff_pipe_cannot_be_made(self) -> None:
        self.install_stub("mkfifo", "#!/usr/bin/env bash\necho 'mkfifo: refused' >&2\nexit 1\n")
        self.install_stub("fake-manager", "#!/usr/bin/env bash\nexit 0\n")
        self.set_fixture(token_fixture("registration-token"))
        result = self.run_in_repo("with-registration-token", "fake-manager")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("mkfifo: refused", result.stderr)
        self.assertEqual(self.token_files(), [])
        self.assertEqual(list(self.temp_dir.glob("github-runner-handoff.*")), [])

    def test_with_token_command_requires_a_command(self) -> None:
        for command, _ in WITH_TOKEN_COMMANDS:
            with self.subTest(command=command):
                self.set_fixture({})
                result = self.run_in_repo(command)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(self.calls(), [])

    def test_invalid_input_is_rejected_before_calling_github(self) -> None:
        cases = {
            "unknown command": (["--repo", REPO, "lst"], {}),
            "missing command": (["--repo", REPO], {}),
            "extra argument": (["--repo", REPO, "list", "extra"], {}),
            "missing argument": (["--repo", REPO, "get"], {}),
            "missing repository": (["list"], {}),
            "missing repository for delete": (["delete", "22", "runner"], {}),
            "empty --repo": (["--repo", "", "list"], {}),
            "empty GH_REPO": (["list"], {"GH_REPO": ""}),
            "malformed repository": (["--repo", "not-a-repository", "list"], {}),
            "nested repository": (["--repo", "a/b/c", "list"], {}),
            "non-ASCII repository": (["--repo", "é/x", "list"], {"LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8"}),
            "zero ID": (["--repo", REPO, "get", "0"], {}),
            "all-zero ID": (["--repo", REPO, "check", "000", "ephemeral"], {}),
            "check without mode": (["--repo", REPO, "check", "22"], {}),
            "check with empty mode": (["--repo", REPO, "check", "22", ""], {}),
            "check with unknown mode": (["--repo", REPO, "check", "22", "permanent"], {}),
            "check with three arguments": (["--repo", REPO, "check", "22", "ephemeral", "100"], {}),
            "check with a non-numeric run ID": (["--repo", REPO, "check", "22", "ephemeral", "1x", "runner-a"], {}),
            "check with an empty name": (["--repo", REPO, "check", "22", "ephemeral", "100", ""], {}),
            "negative ID": (["--repo", REPO, "get", "-1"], {}),
            "non-numeric run ID": (["--repo", REPO, "jobs", "12a"], {}),
            "non-ASCII digits": (["--repo", REPO, "get", "٣"], {"LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8"}),
            "fullwidth digits": (["--repo", REPO, "wait-assignment", "１２", "runner", "100"], {"LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8"}),
            "ID above int64": (["--repo", REPO, "get", "9223372036854775808"], {}),
            "ID wrapping past 64 bits": (["--repo", REPO, "delete", "99999999999999999999999999", "runner"], {}),
            "empty delete name": (["--repo", REPO, "delete", "22", ""], {}),
            "dispatch without ref": (["--repo", REPO, "dispatch-run", "verify.yml"], {}),
            "dispatch with a workflow path": (["--repo", REPO, "dispatch-run", ".github/workflows/verify.yml", "main"], {}),
            "dispatch with a ref containing spaces": (["--repo", REPO, "dispatch-run", "verify.yml", "release branch"], {}),
            "dispatch with a ref that looks like an option": (["--repo", REPO, "dispatch-run", "verify.yml", "--ref"], {}),
            "dispatch with an input without a value": (["--repo", REPO, "dispatch-run", "verify.yml", "main", "marker"], {}),
            "dispatch with an invalid input key": (["--repo", REPO, "dispatch-run", "verify.yml", "main", "bad key=1"], {}),
            "dispatch with a marker containing spaces": (["--repo", REPO, "dispatch-run", "verify.yml", "main", "marker=a b"], {}),
            "dispatch with an empty marker": (["--repo", REPO, "dispatch-run", "verify.yml", "main", "marker="], {}),
            "dispatch without a marker": (["--repo", REPO, "dispatch-run", "verify.yml", "main", "runner=runner-1"], {}),
            "dispatch with two markers": (["--repo", REPO, "dispatch-run", "verify.yml", "main", "marker=a", "marker=b"], {}),
            "dispatch with a marker containing a backslash": (["--repo", REPO, "dispatch-run", "verify.yml", "main", "marker=abc\\def"], {}),
            "dispatch with a marker containing a quote": (["--repo", REPO, "dispatch-run", "verify.yml", "main", "marker=abc'1"], {}),
            "dispatch with a marker containing a non-breaking space": (
                ["--repo", REPO, "dispatch-run", "verify.yml", "main", "marker=abc\u00a0def"],
                {"LANG": "C", "LC_ALL": "C"},
            ),
            "run-for-commit without SHA": (["--repo", REPO, "run-for-commit", "verify.yml"], {}),
            "run-for-commit with a short SHA": (["--repo", REPO, "run-for-commit", "verify.yml", SHA[:39]], {}),
            "run-for-commit with a non-hex SHA": (["--repo", REPO, "run-for-commit", "verify.yml", SHA[:39] + "g"], {}),
            "run-for-commit with a Unicode digit in the SHA": (["--repo", REPO, "run-for-commit", "verify.yml", SHA[:39] + "٣"], {"LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8"}),
            "run-for-commit with a workflow path": (["--repo", REPO, "run-for-commit", ".github/workflows/ci.yml", SHA], {}),
            "run-for-commit with an unknown event": (["--repo", REPO, "run-for-commit", "ci.yml", SHA, "release"], {}),
            "run-for-commit with too many arguments": (["--repo", REPO, "run-for-commit", "ci.yml", SHA, "push", "x"], {}),
            "zero grace period": (["--repo", REPO, "list"], {"GITHUB_RUNNER_GH_STOP_GRACE_SECONDS": "0"}),
            "grace period above maximum": (["--repo", REPO, "list"], {"GITHUB_RUNNER_GH_STOP_GRACE_SECONDS": "601"}),
            "non-numeric grace period": (["--repo", REPO, "with-registration-token", "true"], {"GITHUB_RUNNER_GH_STOP_GRACE_SECONDS": "5s"}),
            "wait without run ID": (["--repo", REPO, "wait-assignment", "22", "runner"], {}),
            "wait with too many arguments": (["--repo", REPO, "wait-assignment", "22", "runner", "100", "5", "x"], {}),
            "empty runner name": (["--repo", REPO, "wait-assignment", "22", "", "100"], {}),
            "empty run ID": (["--repo", REPO, "wait-assignment", "22", "runner", ""], {}),
            "empty timeout": (["--repo", REPO, "wait-assignment", "22", "runner", "100", ""], {}),
            "zero timeout": (["--repo", REPO, "wait-assignment", "22", "runner", "100", "0"], {}),
            "timeout above maximum": (["--repo", REPO, "wait-assignment", "22", "runner", "100", "86401"], {}),
            "timeout wrapping past 64 bits": (["--repo", REPO, "wait-assignment", "22", "runner", "100", "18446744073709551617"], {}),
        }
        for name, (args, env) in cases.items():
            with self.subTest(case=name):
                self.set_fixture({})
                result = self.run_script(*args, env=env)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(self.calls(), [])

    def test_missing_repository_is_never_guessed(self) -> None:
        result = self.run_script("list")
        self.assertEqual(result.returncode, 2)
        self.assertIn("does not guess the target repository", result.stderr)

        self.set_fixture({RUNNERS: runners_page()})
        self.assert_succeeded(self.run_script("list", env={"GH_REPO": REPO}))
        self.assertIn(["repo", "view", REPO, "--json", "viewerPermission", "--jq", ".viewerPermission"], self.calls())

    def test_largest_valid_id_is_accepted(self) -> None:
        largest = "9223372036854775807"
        self.set_fixture({runner_key("GET", largest): {"body": runner(int(largest), "runner")}})
        result = self.run_in_repo("get", largest)
        self.assert_succeeded(result)
        self.assertEqual(self.api_endpoints(), [runner_key("GET", largest)])
        self.assertTrue(result.stdout.startswith(f"{largest}\t"))

    def test_leading_zeros_are_removed_from_ids(self) -> None:
        self.set_fixture({runner_key("GET", 7): {"body": runner(7, "runner-a")}, jobs_key(12): jobs_page()})
        self.assert_succeeded(self.run_in_repo("get", "007"))
        self.assert_succeeded(self.run_in_repo("check", "0007", "ephemeral"))
        self.assert_succeeded(self.run_in_repo("jobs", "012"))
        self.assertEqual(self.api_endpoints(), [runner_key("GET", 7), runner_key("GET", 7), jobs_key(12)])

    def test_non_admin_is_blocked_before_runner_access(self) -> None:
        self.set_fixture({RUNNERS: runners_page()}, viewer_permission="WRITE")
        result = self.run_in_repo("list")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("admin permission is required", result.stderr)
        self.assertEqual(self.api_endpoints(), [])

    def test_unauthenticated_cli_is_blocked(self) -> None:
        self.set_fixture({RUNNERS: runners_page()}, authenticated=False)
        result = self.run_in_repo("list")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.api_endpoints(), [])

    def test_permission_reports_admin(self) -> None:
        result = self.run_in_repo("permission")
        self.assert_succeeded(result)
        self.assertEqual(result.stdout, "ADMIN\n")

    def test_list_and_get_show_ephemeral_state_across_pages(self) -> None:
        self.set_fixture(
            {
                RUNNERS: paged(
                    runners_page(runner(22, "runner-a"), runner(23, "runner-b", busy=True, ephemeral=False)),
                    runners_page(runner(24, "runner-c", ephemeral=None)),
                ),
                runner_key("GET", 23): {"body": runner(23, "runner-b", busy=True, ephemeral=False)},
            }
        )
        listed = self.run_in_repo("list")
        self.assert_succeeded(listed)
        self.assertEqual(
            listed.stdout.splitlines(),
            [
                "id\tname\tos\tstatus\tbusy\tephemeral\tlabels",
                RUNNER_ROW,
                "23\trunner-b\tlinux\tonline\ttrue\tfalse\tself-hosted,Linux,repository-build",
                "24\trunner-c\tlinux\tonline\tfalse\tunknown\tself-hosted,Linux,repository-build",
            ],
        )
        self.assertIn("--paginate", self.calls()[-1])

        fetched = self.run_in_repo("get", "23")
        self.assert_succeeded(fetched)
        self.assertEqual(fetched.stdout, "23\trunner-b\tlinux\tonline\ttrue\tfalse\tself-hosted,Linux,repository-build\n")

    def test_check_prints_the_runner_and_verifies_the_declared_mode_and_labels(self) -> None:
        with_custom_self_hosted = DEFAULT_LABELS + (("Self-Hosted", "custom"),)
        cases = {
            "ephemeral as declared": ("ephemeral", runner(22, "runner-a"), None),
            "persistent as declared": ("persistent", runner(22, "runner-a", ephemeral=False), None),
            "empty name": ("ephemeral", runner(22, ""), None),
            "declared ephemeral, registered persistent": (
                "ephemeral",
                runner(22, "runner-a", ephemeral=False),
                "Error: runner 22 (runner-a) is declared ephemeral, but GitHub reports ephemeral=false\n",
            ),
            "declared persistent, registered ephemeral": (
                "persistent",
                runner(22, "runner-a"),
                "Error: runner 22 (runner-a) is declared persistent, but GitHub reports ephemeral=true\n",
            ),
            "ephemeral state unknown": (
                "ephemeral",
                runner(22, "runner-a", ephemeral=None),
                "Error: runner 22 (runner-a) is declared ephemeral, but GitHub does not report its ephemeral state; rerun with the run ID and exact name once the verification job completed to record the observation\n",
            ),
            "persistent with unknown state": (
                "persistent",
                runner(22, "runner-a", ephemeral=None),
                "Error: runner 22 (runner-a) is declared persistent, but GitHub does not report its ephemeral state; rerun with the run ID and exact name once the verification job completed to record the observation\n",
            ),
            "custom self-hosted on an ephemeral runner": (
                "ephemeral",
                runner(22, "runner-a", labels=with_custom_self_hosted),
                "Error: runner 22 (runner-a) has the reserved label 'self-hosted' as a custom label\n",
            ),
            "custom self-hosted on a persistent runner": (
                "persistent",
                runner(22, "runner-a", ephemeral=False, labels=with_custom_self_hosted),
                "Error: runner 22 (runner-a) has the reserved label 'self-hosted' as a custom label\n",
            ),
        }
        for name, (mode, record, error) in cases.items():
            with self.subTest(case=name):
                self.set_fixture({RUNNER_22: {"body": record}})
                result = self.run_in_repo("check", "22", mode)
                self.assertTrue(result.stdout.startswith(f"22\t{record['name']}\tlinux\t"), result.stdout)
                if error is None:
                    self.assert_succeeded(result)
                    self.assertIn(f"Runner 22 ({record['name']}) is {mode} and has no custom self-hosted label", result.stdout)
                else:
                    self.assertEqual(result.returncode, 1)
                    self.assertEqual(result.stderr, error)
                self.assertEqual(self.api_endpoints(), [RUNNER_22])

    def test_check_after_the_job_records_an_observation_and_fails_on_a_contradiction(self) -> None:
        done = job(7, "build", "completed", 22, "runner-a", conclusion="success")
        on_other_id = job(7, "build", "completed", 23, "runner-a", conclusion="success")
        still_running = job(7, "build", "in_progress", 22, "runner-a")
        row = "22\trunner-a\tlinux\tonline\tfalse\t"
        persistent_observation = "observation: runner 22 (runner-a) still registered after completed job 7 of run 100; consistent with persistent, not a proof of mode\n"
        cases = {
            "persistent, field absent, still registered": (
                "persistent", runner(22, "runner-a", ephemeral=None), done,
                f"{row}unknown\tself-hosted,Linux,repository-build\n{persistent_observation}Runner 22 (runner-a) has no custom self-hosted label; GitHub does not report its ephemeral state\n", None,
            ),
            "persistent, field false, still registered": (
                "persistent", runner(22, "runner-a", ephemeral=False), done,
                f"{row}false\tself-hosted,Linux,repository-build\n{persistent_observation}Runner 22 (runner-a) is persistent and has no custom self-hosted label\n", None,
            ),
            "persistent, gone after the job": (
                "persistent", NOT_FOUND, done, "",
                "Error: runner 22 (runner-a) is declared persistent, but GitHub answers 404 after completed job 7 of run 100\n",
            ),
            "ephemeral, gone after the job": (
                "ephemeral", NOT_FOUND, done,
                "observation: runner 22 (runner-a) gone after completed job 7 of run 100; consistent with ephemeral, not a proof of mode\n", None,
            ),
            "ephemeral, still registered": (
                "ephemeral", runner(22, "runner-a", ephemeral=None), done, f"{row}unknown\tself-hosted,Linux,repository-build\n",
                "Error: runner 22 (runner-a) is declared ephemeral, but it is still registered after completed job 7 of run 100\n",
            ),
            "ephemeral, field false wins over the observation": (
                "ephemeral", runner(22, "runner-a", ephemeral=False), done, f"{row}false\tself-hosted,Linux,repository-build\n",
                "Error: runner 22 (runner-a) is declared ephemeral, but GitHub reports ephemeral=false\n",
            ),
            "persistent, field true wins over the observation": (
                "persistent", runner(22, "runner-a"), done, f"{row}true\tself-hosted,Linux,repository-build\n",
                "Error: runner 22 (runner-a) is declared persistent, but GitHub reports ephemeral=true\n",
            ),
            "the job ran on another runner ID with the same name": (
                "persistent", runner(22, "runner-a", ephemeral=None), on_other_id, f"{row}unknown\tself-hosted,Linux,repository-build\n",
                "Error: run 100 has no completed job on runner 22 ('runner-a'); the observation needs a job that ran on that exact runner ID and name\n",
            ),
            "the matched job is not completed": (
                "persistent", runner(22, "runner-a", ephemeral=None), still_running, f"{row}unknown\tself-hosted,Linux,repository-build\n",
                "Error: run 100 has no completed job on runner 22 ('runner-a'); the observation needs a job that ran on that exact runner ID and name\n",
            ),
            "the registration now carries another name": (
                "persistent", runner(22, "runner-b", ephemeral=None), done, "22\trunner-b\tlinux\tonline\tfalse\tunknown\tself-hosted,Linux,repository-build\n",
                "Error: runner 22 is now named 'runner-b', not 'runner-a': not the registration the job ran on\n",
            ),
            "custom self-hosted label after the job": (
                "persistent", runner(22, "runner-a", ephemeral=None, labels=DEFAULT_LABELS + (("self-hosted", "custom"),)), done,
                f"{row}unknown\tself-hosted,Linux,repository-build,self-hosted\n",
                "Error: runner 22 (runner-a) has the reserved label 'self-hosted' as a custom label\n",
            ),
        }
        for name, (mode, record, run_job, stdout, error) in cases.items():
            with self.subTest(case=name):
                self.set_fixture({RUNNER_22: record if record is NOT_FOUND else {"body": record}, JOBS_100: jobs_page(run_job)})
                result = self.run_in_repo("check", "22", mode, "100", "runner-a")
                self.assertEqual(result.stdout, stdout)
                if error is None:
                    self.assert_succeeded(result)
                else:
                    self.assertEqual(result.returncode, 1)
                    self.assertEqual(result.stderr, error)

    def test_check_after_the_job_explains_a_missing_run(self) -> None:
        self.set_fixture({RUNNER_22: {"body": runner(22, "runner-a", ephemeral=None)}, JOBS_100: NOT_FOUND})
        result = self.run_in_repo("check", "22", "persistent", "100", "runner-a")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, f"Error: GitHub answers 404 for run 100 in {REPO}: gh: Not Found (HTTP 404)\n")
        self.assertEqual(self.api_endpoints(), [RUNNER_22, JOBS_100])

    def test_runner_lookups_explain_failures_the_same_way(self) -> None:
        commands = {"get": ("get", "22"), "check": ("check", "22", "ephemeral"), "delete": ("delete", "22", "retired-runner")}
        responses = {
            "404": (NOT_FOUND, f"Error: {RUNNER_404}\n"),
            "404 without JSON body": (NOT_FOUND_HTML, f"Error: {RUNNER_404}\n"),
            "403": (FORBIDDEN, f"Error: could not read runner 22 from {REPO}: {GH_403}\n"),
            "403 with hint": (
                {**FORBIDDEN, "hints": ['This API operation needs the "repo" scope. To request it, run: gh auth refresh -s repo']},
                f"Error: could not read runner 22 from {REPO}: {GH_403}; gh: This API operation needs the \"repo\" scope. To request it, run: gh auth refresh -s repo\n",
            ),
            "401": (UNAUTHORIZED, f"Error: could not read runner 22 from {REPO}: gh: Bad credentials (HTTP 401)\n"),
            "network error": (
                NETWORK_ERROR,
                f"Error: could not read runner 22 from {REPO}: error connecting to api.github.com; check your internet connection\n",
            ),
            "empty body": ({"raw": ""}, f"Error: could not read runner 22 from {REPO}: the runner response was empty\n"),
            "non-JSON body": ({"raw": "<html>maintenance</html>\n"}, "could not be parsed: jq: parse error"),
        }
        for command_name, args in commands.items():
            for response_name, (response, expected) in responses.items():
                with self.subTest(command=command_name, response=response_name):
                    self.set_fixture({RUNNER_22: response})
                    result = self.run_in_repo(*args)
                    self.assertEqual(result.returncode, 1)
                    if expected.startswith("Error: "):
                        self.assertEqual(result.stderr, expected)
                    else:
                        self.assertIn(expected, result.stderr)
                    self.assertEqual(result.stdout, "")
                    self.assert_no_deletes()

    def test_parsed_requests_ignore_gh_output_settings(self) -> None:
        environments = {
            "GH_DEBUG": {"GH_DEBUG": "api"},
            "DEBUG": {"DEBUG": "1"},
            "GH_TELEMETRY=log": {"GH_TELEMETRY": "log"},
            "CLICOLOR_FORCE": {"CLICOLOR_FORCE": "1"},
            "GH_FORCE_TTY": {"GH_FORCE_TTY": "1"},
        }
        for name, env in environments.items():
            with self.subTest(setting=name):
                self.set_fixture(
                    {
                        RUNNER_22: {"body": runner(22, "runner-a")},
                        RUNNERS: runners_page(runner(22, "runner-a")),
                        JOBS_100: jobs_page(job(9, "smoke", "in_progress", 22, "runner-a")),
                    }
                )
                fetched = self.run_in_repo("get", "22", env=env)
                self.assert_succeeded(fetched)
                self.assertEqual(fetched.stdout, f"{RUNNER_ROW}\n")
                checked = self.run_in_repo("check", "22", "ephemeral", env=env)
                self.assert_succeeded(checked)
                listed = self.run_in_repo("list", env=env)
                self.assert_succeeded(listed)
                self.assertEqual(listed.stdout.splitlines()[1], RUNNER_ROW)
                waited = self.run_in_repo("wait-assignment", "22", "runner-a", "100", "5", env=env)
                self.assert_succeeded(waited)
                self.assertIn("100\t9\tsmoke\tin_progress\t22\trunner-a\t", waited.stdout)
                for result in (fetched, checked, listed, waited):
                    self.assertNotIn("Telemetry payload", result.stderr)
                    self.assertNotIn('"labels"', result.stderr)
                    self.assertNotIn("\x1b[", result.stdout)

    def test_runs_and_jobs_are_listed(self) -> None:
        self.set_fixture(
            {
                RUNS: runs_page(100),
                JOBS_100: jobs_page(job(7, "build", "in_progress", 22, "runner-a"), job(8, "lint", "queued", None, None)),
            }
        )
        runs = self.run_in_repo("runs")
        self.assert_succeeded(runs)
        self.assertEqual(
            runs.stdout.splitlines(),
            ["run_id\tworkflow\tstatus\thead_sha\turl", f"100\tCI\tin_progress\t{SHA}\thttps://github.com/{REPO}/actions/runs/100"],
        )
        self.assertIn("exclude_pull_requests=true", self.calls()[-1][2])

        jobs = self.run_in_repo("jobs", "100")
        self.assert_succeeded(jobs)
        self.assertEqual(
            jobs.stdout.splitlines(),
            ["job_id\tname\tstatus\trunner_id\trunner_name\trunner_group", "7\tbuild\tin_progress\t22\trunner-a\t", "8\tlint\tqueued\t\t\t"],
        )

    LISTING = workflow_runs_key("verify.yml")
    DISPATCH = dispatch_key("verify.yml")
    DISPATCHED = f"Dispatched workflow verify.yml on main in {REPO}; waiting for its run to appear\n"
    TIMED_OUT = "Error: no new run of workflow verify.yml with marker abc-1 appeared within 60s of the dispatch\n"

    def dispatch_fixture(self, *listings: dict) -> None:
        self.set_fixture({USER: USER_RESPONSE, self.LISTING: list(listings), self.DISPATCH: {"body": None}})

    def test_dispatch_run_prints_the_id_of_the_new_run_that_carries_the_marker(self) -> None:
        # An unrelated run by the same user with a marker of the same prefix appears first and stays the only new run for a while.
        other = run(101, title="verify abc-10")
        self.dispatch_fixture(
            runs_page(100, 99),
            runs_page(other, 100, 99),
            runs_page(other, 100, 99),
            runs_page(other, 100, 99),
            runs_page(run(102, title="verify abc-1"), other, 100, 99),
        )
        result = self.run_in_repo("dispatch-run", "verify.yml", "main", "marker=abc-1", "runner=runner-1")
        self.assert_succeeded(result)
        self.assertEqual(result.stdout, "102\n")
        self.assertEqual(result.stderr, self.DISPATCHED)
        self.assertEqual(self.api_endpoints(), [USER, self.LISTING, self.DISPATCH] + [self.LISTING] * 4)
        dispatch = next(call for call in self.calls() if "--method" in call and "POST" in call)
        self.assertEqual(dispatch[dispatch.index("-f") :], ["-f", "ref=main", "-f", "inputs[marker]=abc-1", "-f", "inputs[runner]=runner-1"])
        self.assertEqual(self.sleeps(), [2, 2, 2])

    def test_dispatch_run_matches_the_complete_marker_as_the_last_word_of_the_title(self) -> None:
        cases = {
            "the documented run-name": ("abc-1", "verify abc-1"),
            "the marker alone": ("abc-1", "abc-1"),
            "a title with more words before the marker": ("abc-1", "verify on runner-1 abc-1"),
            "a marker with every allowed character class": ("A_b.9-z", "verify A_b.9-z"),
        }
        for name, (marker, title) in cases.items():
            with self.subTest(case=name):
                self.dispatch_fixture(runs_page(100), runs_page(run(101, title=title), 100))
                result = self.run_in_repo("dispatch-run", "verify.yml", "main", f"marker={marker}")
                self.assert_succeeded(result)
                self.assertEqual(result.stdout, "101\n")
                self.assertIn(f"inputs[marker]={marker}", next(call for call in self.calls() if "POST" in call))
                self.assertEqual(self.sleeps(), [])

    def test_dispatch_run_ignores_a_run_whose_title_carries_the_marker_elsewhere(self) -> None:
        # The marker equals a fixed word of every title: another dispatch's run must not match on that word.
        other = run(101, title="verify other")
        self.dispatch_fixture(runs_page(100), runs_page(other, 100), runs_page(run(102, title="verify verify"), other, 100))
        result = self.run_in_repo("dispatch-run", "verify.yml", "main", "marker=verify")
        self.assert_succeeded(result)
        self.assertEqual(result.stdout, "102\n")
        self.assertEqual(self.sleeps(), [2])

    def test_dispatch_run_ignores_runs_by_other_users_and_runs_listed_before_the_dispatch(self) -> None:
        earlier = run(100, title="verify abc-1")
        self.dispatch_fixture(
            runs_page(earlier),
            runs_page(run(102, actor="someone-else", title="verify abc-1"), run(101, title="verify abc-1"), earlier),
        )
        result = self.run_in_repo("dispatch-run", "verify.yml", "main", "marker=abc-1")
        self.assert_succeeded(result)
        self.assertEqual(result.stdout, "101\n")

    def test_dispatch_run_fails_when_the_new_run_is_ambiguous_or_missing(self) -> None:
        ambiguous = "Error: more than one run of workflow verify.yml carries marker abc-1 (101, 102); dispatch again with a marker no other dispatch uses\n"
        cases = {
            "two new runs carry the marker": (
                [runs_page(100), runs_page(run(102, title="verify abc-1"), run(101, title="verify abc-1"), 100)],
                ambiguous,
                [],
            ),
            "no new run": ([runs_page(100)], self.TIMED_OUT, [2] * 30),
            "the marker never appears": ([runs_page(100), runs_page(run(101, title="verify other"), 100)], self.TIMED_OUT, [2] * 30),
            "only a longer marker with the same prefix appears": (
                [runs_page(100), runs_page(run(101, title="verify abc-10"), 100)],
                self.TIMED_OUT,
                [2] * 30,
            ),
            "the marker is only part of a word": (
                [runs_page(100), runs_page(run(101, title="verify-abc-1"), 100)],
                self.TIMED_OUT,
                [2] * 30,
            ),
            "the marker is followed by more words": (
                [runs_page(100), runs_page(run(101, title="verify abc-1 on runner-1"), 100)],
                self.TIMED_OUT,
                [2] * 30,
            ),
        }
        for name, (listings, message, sleeps) in cases.items():
            with self.subTest(case=name):
                self.dispatch_fixture(*listings)
                result = self.run_in_repo("dispatch-run", "verify.yml", "main", "marker=abc-1")
                self.assertEqual(result.returncode, 1)
                self.assertTrue(result.stderr.endswith(message), result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual(self.sleeps(), sleeps)

    COMMIT_RUNS = commit_runs_key("ci.yml", SHA)
    NO_RUN_FOR_COMMIT = f"Error: no run of workflow ci.yml for commit {SHA} was listed within 60s\n"

    def test_run_for_commit_prints_the_one_run_of_the_commit(self) -> None:
        cases = {
            "one run": ([runs_page(run(101))], [], "101\n"),
            "one run among two pages": ([paged(runs_page(), runs_page(run(101)))], [], "101\n"),
            "created after the third poll": ([runs_page(), runs_page(), runs_page(), runs_page(run(101))], [2, 2, 2], "101\n"),
            "a completed run counts": ([runs_page(run(101, status="completed"))], [], "101\n"),
        }
        for name, (listings, sleeps, stdout) in cases.items():
            with self.subTest(case=name):
                self.set_fixture({self.COMMIT_RUNS: list(listings) if len(listings) > 1 else listings[0]})
                result = self.run_in_repo("run-for-commit", "ci.yml", SHA)
                self.assert_succeeded(result)
                self.assertEqual(result.stdout, stdout)
                self.assertEqual(self.sleeps(), sleeps)

    def test_run_for_commit_narrows_by_event(self) -> None:
        self.set_fixture(
            {
                self.COMMIT_RUNS: runs_page(run(102, event="workflow_dispatch"), run(101)),
                commit_runs_key("ci.yml", SHA, "workflow_dispatch"): runs_page(run(102, event="workflow_dispatch")),
            }
        )
        result = self.run_in_repo("run-for-commit", "ci.yml", SHA, "workflow_dispatch")
        self.assert_succeeded(result)
        self.assertEqual(result.stdout, "102\n")
        self.assertEqual(self.api_endpoints(), [commit_runs_key("ci.yml", SHA, "workflow_dispatch")])

    def test_run_for_commit_fails_when_the_run_is_ambiguous_or_missing(self) -> None:
        cases = {
            "two runs for the commit": (
                {self.COMMIT_RUNS: runs_page(run(102), run(101))},
                f"Error: commit {SHA} has more than one run of workflow ci.yml for commit {SHA} (101, 102); a run of a commit is identified only when it is the only one; for a dispatch, use dispatch-run with a marker\n",
                [],
            ),
            "no run for a minute": ({self.COMMIT_RUNS: runs_page()}, self.NO_RUN_FOR_COMMIT, [2] * 30),
            "workflow not found": (
                {self.COMMIT_RUNS: NOT_FOUND},
                f"Error: GitHub answers 404 for workflow ci.yml in {REPO}: gh: Not Found (HTTP 404)\n",
                [],
            ),
            "transient failure then no run": ({self.COMMIT_RUNS: [BAD_GATEWAY] + [runs_page()] * 30}, self.NO_RUN_FOR_COMMIT, [2] * 30),
        }
        for name, (fixture, message, sleeps) in cases.items():
            with self.subTest(case=name):
                self.set_fixture(fixture)
                result = self.run_in_repo("run-for-commit", "ci.yml", SHA)
                self.assertEqual(result.returncode, 1)
                self.assertTrue(result.stderr.endswith(message), result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual(self.sleeps(), sleeps)

    def test_dispatch_run_reports_listing_and_dispatch_failures(self) -> None:
        listing = self.LISTING
        cases = {
            "user lookup fails": (
                {USER: BAD_GATEWAY},
                "Error: could not read the authenticated user: gh: Bad Gateway (HTTP 502)\n",
                [USER],
            ),
            "workflow not found": (
                {USER: USER_RESPONSE, listing: NOT_FOUND},
                f"Error: GitHub answers 404 for workflow verify.yml in {REPO}: gh: Not Found (HTTP 404)\n",
                [USER, listing],
            ),
            "dispatch rejected": (
                {USER: USER_RESPONSE, listing: runs_page(100), self.DISPATCH: {"status": 422, "message": "Workflow does not have 'workflow_dispatch' trigger"}},
                f"Error: could not dispatch workflow verify.yml on main in {REPO}: gh: Workflow does not have 'workflow_dispatch' trigger (HTTP 422)\n",
                [USER, listing, self.DISPATCH],
            ),
        }
        for name, (fixture, message, endpoints) in cases.items():
            with self.subTest(case=name):
                self.set_fixture(fixture)
                result = self.run_in_repo("dispatch-run", "verify.yml", "main", "marker=abc-1")
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stderr, message)
                self.assertEqual(result.stdout, "")
                self.assertEqual(self.api_endpoints(), endpoints)

    def test_wait_assignment_reports_the_first_job_on_the_exact_runner(self) -> None:
        self.set_fixture(
            {
                JOBS_100: paged(
                    jobs_page(
                        job(7, "build", "in_progress", 23, "runner-1"),
                        job(8, "lint", "queued", 22, "runner-1"),
                        job(9, "test", "in_progress", 22, "runner-10"),
                    ),
                    jobs_page(job(10, "smoke", "in_progress", 22, "runner-1"), job(11, "later", "completed", 22, "runner-1", conclusion="success")),
                )
            }
        )
        result = self.run_in_repo("wait-assignment", "22", "runner-1", "100", "5")
        self.assert_succeeded(result)
        self.assertEqual(result.stdout.splitlines(), [ASSIGNMENT_HEADER, "100\t10\tsmoke\tin_progress\t22\trunner-1\t"])
        self.assertEqual(self.api_endpoints(), [JOBS_100])
        self.assertIn("--paginate", self.calls()[-1])
        self.assertEqual(self.sleeps(), [])

    def test_wait_assignment_accepts_a_completed_job_after_the_runner_deregistered(self) -> None:
        self.set_fixture(
            {
                RUNNER_22: NOT_FOUND,
                JOBS_100: jobs_page(job(9, "smoke", "completed", 22, "runner-1", conclusion="success")),
            }
        )
        result = self.run_in_repo("wait-assignment", "22", "runner-1", "100", "5")
        self.assert_succeeded(result)
        self.assertEqual(result.stdout.splitlines(), [ASSIGNMENT_HEADER, "100\t9\tsmoke\tcompleted\t22\trunner-1\tsuccess"])
        self.assertNotIn(RUNNER_22, self.api_endpoints())

    def test_wait_assignment_polls_until_the_job_starts(self) -> None:
        self.set_fixture(
            {
                JOBS_100: [
                    jobs_page(),
                    jobs_page(job(9, "smoke", "queued", None, None)),
                    jobs_page(job(9, "smoke", "in_progress", 22, "runner-1")),
                ]
            }
        )
        result = self.run_in_repo("wait-assignment", "22", "runner-1", "100", "60")
        self.assert_succeeded(result)
        self.assertIn("100\t9\tsmoke\tin_progress\t22\trunner-1\t", result.stdout)
        self.assertEqual(self.sleeps(), [5, 5])
        self.assertEqual(result.stderr, "")

    def test_wait_assignment_stops_when_the_run_finished_without_the_runner(self) -> None:
        self.set_fixture(
            {
                JOBS_100: jobs_page(
                    job(7, "build", "completed", 23, "runner-1", conclusion="success"),
                    job(8, "lint", "completed", 22, "runner-10", conclusion="failure"),
                )
            }
        )
        result = self.run_in_repo("wait-assignment", "22", "runner-1", "100", "120")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "Error: run 100 finished without a job on runner 22 ('runner-1')\n")
        self.assertEqual(self.sleeps(), [])

    def test_wait_assignment_stops_at_once_on_404_and_401(self) -> None:
        cases = {
            "run not found": (NOT_FOUND, f"Error: GitHub answers 404 for run 100 in {REPO}: gh: Not Found (HTTP 404)\n"),
            "run not found without JSON body": (NOT_FOUND_HTML, f"Error: GitHub answers 404 for run 100 in {REPO}: gh: HTTP 404\n"),
            "unauthorized": (UNAUTHORIZED, "Error: gh: Bad credentials (HTTP 401)\n"),
        }
        for name, (response, message) in cases.items():
            with self.subTest(case=name):
                self.set_fixture({JOBS_100: response})
                result = self.run_in_repo("wait-assignment", "22", "runner-1", "100", "120")
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stderr, message)
                self.assertEqual(self.sleeps(), [])

    def test_wait_assignment_retries_other_failures_with_gh_error_text(self) -> None:
        good = jobs_page(job(9, "smoke", "in_progress", 22, "runner-1"))
        failures = {
            "502": (BAD_GATEWAY, "Warning: gh: Bad Gateway (HTTP 502); retrying"),
            "502 without JSON body": ({"status": 502, "raw": "<html>bad gateway</html>"}, "Warning: gh: HTTP 502; retrying"),
            "403": (FORBIDDEN, f"Warning: {GH_403}; retrying"),
            "403 with hint": (
                {**FORBIDDEN, "hints": ["This API operation needs the \"repo\" scope."]},
                f"Warning: {GH_403}; gh: This API operation needs the \"repo\" scope.; retrying",
            ),
            "network error": (NETWORK_ERROR, "Warning: error connecting to api.github.com; check your internet connection; retrying"),
            "empty body": ({"raw": ""}, "Warning: the job listing of run 100 was empty; retrying"),
            "whitespace body": ({"raw": "  \n"}, "Warning: the job listing of run 100 was empty; retrying"),
            "non-JSON body": ({"raw": "<html>maintenance</html>\n"}, "Warning: the job listing of run 100 could not be parsed: jq: parse error"),
            "error after a full page": ({**BAD_GATEWAY, **paged(jobs_page(job(7, "build", "in_progress", 23, "runner-1")))}, "Warning: gh: Bad Gateway (HTTP 502); retrying"),
        }
        for name, (failure, warning) in failures.items():
            with self.subTest(case=name):
                self.set_fixture({JOBS_100: [failure, good]})
                result = self.run_in_repo("wait-assignment", "22", "runner-1", "100", "2")
                self.assert_succeeded(result)
                self.assertIn(warning, result.stderr)
                self.assertIn("100\t9\tsmoke\tin_progress\t22\trunner-1\t", result.stdout)
                self.assertEqual(self.sleeps(), [2])

    def test_wait_assignment_pauses_for_rate_limits(self) -> None:
        good = jobs_page(job(9, "smoke", "in_progress", 22, "runner-1"))
        cases = {
            "primary limit uses the reset time": (RATE_LIMITED, {"body": {"resources": {"core": {"reset": START_TIME + 37}}}}, 37, "pausing 37s"),
            "primary limit clamps to the remaining timeout": (RATE_LIMITED, {"body": {"resources": {"core": {"reset": START_TIME + 500}}}}, 120, "pausing 500s"),
            "reset in the past falls back": (RATE_LIMITED, {"body": {"resources": {"core": {"reset": START_TIME - 1}}}}, 60, "pausing 60s"),
            "rate limit lookup failure falls back": (RATE_LIMITED, BAD_GATEWAY, 60, "pausing 60s"),
            "secondary limit waits a minute": (SECONDARY_RATE_LIMITED, None, 60, "pausing 60s"),
        }
        for name, (limited, rate_limit_response, expected_sleep, notice) in cases.items():
            with self.subTest(case=name):
                fixture = {JOBS_100: [limited, good]}
                if rate_limit_response is not None:
                    fixture[RATE_LIMIT] = rate_limit_response
                self.set_fixture(fixture)
                result = self.run_in_repo("wait-assignment", "22", "runner-1", "100", "120")
                self.assert_succeeded(result)
                self.assertIn(notice, result.stderr)
                self.assertIn("for the rate limit", result.stderr)
                self.assertEqual(self.sleeps(), [expected_sleep])
                self.assertEqual(self.api_endpoints().count(RATE_LIMIT), 1 if rate_limit_response is not None else 0)

    def test_wait_assignment_never_sleeps_past_the_timeout(self) -> None:
        for timeout, expected_sleeps in (("1", [1]), ("7", [5, 2]), ("10", [5, 5])):
            with self.subTest(timeout=timeout):
                self.set_fixture({JOBS_100: jobs_page()})
                result = self.run_in_repo("wait-assignment", "22", "runner-1", "100", timeout)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stderr, f"Error: no in_progress or completed job of run 100 on runner 22 ('runner-1') within {timeout}s\n")
                self.assertEqual(self.sleeps(), expected_sleeps)

    def test_wait_assignment_timeout_names_the_last_failure(self) -> None:
        target = "job of run 100 on runner 22 ('runner-1') within 6s"
        cases = {
            "every poll failed": (FORBIDDEN, f"Error: could not check for a {target}; every poll failed, last failure: {GH_403}\n"),
            "last poll failed": ([jobs_page(), FORBIDDEN], f"Error: no in_progress or completed {target}; the last poll failed: {GH_403}\n"),
            "last poll completed": ([FORBIDDEN, jobs_page()], f"Error: no in_progress or completed {target}\n"),
        }
        for name, (jobs_responses, message) in cases.items():
            with self.subTest(case=name):
                self.set_fixture({JOBS_100: jobs_responses})
                result = self.run_in_repo("wait-assignment", "22", "runner-1", "100", "6")
                self.assertEqual(result.returncode, 1)
                self.assertTrue(result.stderr.endswith(message), result.stderr)
                self.assertEqual(self.sleeps(), [5, 1])

    def test_delete_stops_when_runner_name_does_not_match(self) -> None:
        self.set_fixture({RUNNER_22: {"body": runner(22, "actual-runner")}})
        result = self.run_in_repo("delete", "22", "wrong-runner")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "Error: runner name mismatch for ID 22; expected 'wrong-runner', GitHub reports 'actual-runner'\n")
        self.assert_no_deletes()

    def test_delete_request_failure_shows_gh_error(self) -> None:
        self.set_fixture({RUNNER_22: {"body": runner(22, "retired-runner")}, DELETE_22: FORBIDDEN})
        result = self.run_in_repo("delete", "22", "retired-runner")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, f"Error: could not delete runner 22 (retired-runner) from {REPO}: {GH_403}\n")
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.api_endpoints(), [RUNNER_22, DELETE_22])

    def test_delete_treats_404_on_the_request_as_already_gone(self) -> None:
        for name, response in (("JSON 404", NOT_FOUND), ("404 without JSON body", NOT_FOUND_HTML)):
            with self.subTest(case=name):
                self.set_fixture({RUNNER_22: {"body": runner(22, "retired-runner")}, DELETE_22: response})
                result = self.run_in_repo("delete", "22", "retired-runner")
                self.assert_succeeded(result)
                self.assertEqual(result.stdout, f"Runner 22 (retired-runner) is gone from {REPO}: it deregistered before the delete request\n")
                self.assertEqual(result.stderr, "")
                self.assertEqual(self.api_endpoints(), [RUNNER_22, DELETE_22])
                self.assertEqual(self.sleeps(), [])

    def delete_fixture(self, *confirmations: dict, delete: dict | None = None) -> None:
        self.set_fixture(
            {
                RUNNER_22: [{"body": runner(22, "retired-runner")}, *confirmations],
                DELETE_22: {"body": None} if delete is None else delete,
            }
        )

    def test_delete_removes_confirmed_runner_and_verifies_absence(self) -> None:
        for name, confirmation in (("JSON 404", NOT_FOUND), ("404 without JSON body", NOT_FOUND_HTML)):
            with self.subTest(case=name):
                self.delete_fixture(confirmation)
                result = self.run_in_repo("delete", "22", "retired-runner")
                self.assert_succeeded(result)
                self.assertEqual(result.stdout, f"Deleted runner 22 (retired-runner) from {REPO}\n")
                self.assertEqual(result.stderr, "")
                self.assertEqual(self.api_endpoints(), [RUNNER_22, DELETE_22, RUNNER_22])
                self.assertEqual(self.sleeps(), [])

    def test_delete_confirmation_retries_a_stale_response(self) -> None:
        self.delete_fixture({"body": runner(22, "retired-runner")}, NOT_FOUND)
        result = self.run_in_repo("delete", "22", "retired-runner")
        self.assert_succeeded(result)
        self.assertEqual(self.api_endpoints().count(RUNNER_22), 3)
        self.assertEqual(self.sleeps(), [2])

    def test_delete_confirmation_failure_reports_accepted_deletion(self) -> None:
        cases = {
            "runner still returned": ({"body": runner(22, "retired-runner")}, "but GitHub still returns the runner"),
            "confirmation request fails": (BAD_GATEWAY, "but the confirmation request failed: gh: Bad Gateway (HTTP 502)"),
        }
        for name, (confirmation, reason) in cases.items():
            with self.subTest(case=name):
                self.delete_fixture(confirmation)
                result = self.run_in_repo("delete", "22", "retired-runner")
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stderr, f"Error: {ACCEPTED_DELETION}, {reason}; {CONFIRM_HINT}\n")
                self.assertEqual(self.api_endpoints().count(RUNNER_22), 4)
                self.assertEqual(self.sleeps(), [2, 2])

    def test_interrupted_delete_confirmation_reports_accepted_deletion(self) -> None:
        for name, signum in SIGNALS.items():
            with self.subTest(signal=name):
                self.install_stub("sleep", f'#!/usr/bin/env bash\nkill -{name} "$PPID"\n')
                self.delete_fixture({"body": runner(22, "retired-runner")})
                result = self.run_in_repo("delete", "22", "retired-runner", timeout=5)
                self.assertEqual(result.returncode, 128 + signum)
                self.assertEqual(result.stderr, f"Error: {ACCEPTED_DELETION}, but confirmation was interrupted; {CONFIRM_HINT}\n")
                self.assertEqual(result.stdout, "")

    def test_interrupted_delete_request_reports_possible_deletion(self) -> None:
        for name, signum in SIGNALS.items():
            with self.subTest(signal=name):
                self.delete_fixture(delete={"body": None, "signal_parent": int(signum)})
                result = self.run_in_repo("delete", "22", "retired-runner", timeout=5)
                self.assertEqual(result.returncode, 128 + signum)
                self.assertEqual(
                    result.stderr,
                    f"Error: GitHub may have applied the deletion of runner 22 (retired-runner) from {REPO}; "
                    f"the request was interrupted; {CONFIRM_HINT}, and if it still shows the runner, run delete again\n",
                )
                self.assertEqual(list(self.temp_dir.glob("github-runner-gh-stderr.*")), [])
                self.assertEqual(result.stdout, "")
                self.assertEqual(self.api_endpoints().count(RUNNER_22), 1)
                request_pid = int(self.request_pid.read_text())
                with self.assertRaises(ProcessLookupError):
                    os.kill(request_pid, 0)


if __name__ == "__main__":
    unittest.main()
