# github-runner-gh-skill

An agent skill for managing repository-level GitHub Actions self-hosted runners through GitHub CLI. It covers authenticated runner inspection, private registration-token files, job-assignment verification, and explicit deregistration by runner ID.

## Safety model

- All GitHub interactions use `gh`.
- Registration and removal tokens go to temporary files with mode `0600` and never to standard output or diagnostic logs. Token requests disable GitHub CLI diagnostics and clean up on failure or HUP, INT, or TERM before handoff. `with-registration-token` and `with-remove-token` run the local manager with the file path and remove the file afterwards, so no caller-side cleanup script is needed; the manager runs in its own process group, which a signal to the helper stops as a whole (TERM, then KILL after `GITHUB_RUNNER_GH_STOP_GRACE_SECONDS`, default 5), and holds the terminal's foreground when the helper runs on one, so its prompts and interrupt keys work as in a shell.
- The operator states whether the runner is ephemeral or persistent (constant) before anything is registered, verified, or removed; the skill never infers it. `check RUNNER_ID MODE` confirms the declared mode through GitHub when GitHub reports it, and `check RUNNER_ID MODE RUN_ID EXACT_RUNNER_NAME` records, after the verification job, whether the registration's state is consistent with the declared mode, as an observation and never as a proof. An ephemeral runner is registered with `--ephemeral` or `EPHEMERAL=true` and deregisters after one job; a persistent runner is registered without them, keeps its registration until it is stopped and removed, and needs labels no other workload matches.
- `self-hosted` remains GitHub's reserved default label and is never added as a custom label. `check` fails when it appears as a custom label.
- Readiness requires both the local `Listening for Jobs` log and a job of the dispatched run, `in_progress` or `completed`, whose runner ID and exact name match the registered runner.
- Every command except `auth` needs an explicit repository from `--repo` or `GH_REPO`; the helper never infers it from the current directory.
- Removal confirms both runner ID and exact name before calling the delete endpoint and fails unless GitHub then answers 404 for the runner ID, stating that GitHub accepted the deletion. A runner that deregistered before the request is reported as gone. Confirm an unconfirmed deletion with `get RUNNER_ID`, whose 404 message says `GitHub answers 404`, not with the paged `list`; after an interrupted request, `delete` may be run again when `get` still shows the runner.
- Errors repeat GitHub CLI's own error lines, such as `gh: Forbidden (HTTP 403)` and its scope hints. Parsed responses are read with diagnostics, telemetry output, forced colour, and forced terminal output disabled. `wait-assignment` stops at once on a 401, on a 404 for the run, or when the run finished without a job on the runner; a rate limit pauses it until GitHub's reset time; other failures are retried until the timeout. Invocation errors exit with status 2, other failures with status 1.
- Local Docker or service management stays outside this project, except for the reference manager `scripts/runnerctl` and its systemd user unit template. The manager reaches GitHub only through the helper, receives a token only on the configuration container's standard input, read from the helper's `0600` file by the docker client, and is tested against fakes; real Docker and systemd runs are the opt-in `make integration`. The operator supplies a compatible runtime image pinned by digest when registering a runner.

## Install

Clone the repository:

```bash
gh repo clone viktor-drobek/github-runner-gh-skill
cd github-runner-gh-skill
make test
```

To install it as a project skill, copy or link the repository directory to the skill location used by the target agent. Keep the repository root intact because `SKILL.md` refers to `scripts/github-runner-gh` by a relative path. The helper needs bash 4.3 or newer (macOS ships 3.2; install a current bash and keep it first on `PATH`), `gh`, and `jq` for `get`, `check`, `delete`, `dispatch-run`, and `wait-assignment`.

## Quick start

```bash
./scripts/github-runner-gh auth
./scripts/github-runner-gh --repo OWNER/REPOSITORY permission
./scripts/github-runner-gh --repo OWNER/REPOSITORY list
```

Decide with the operator whether the runner is ephemeral or persistent, then register through the helper. It writes the registration token to a private `0600` file, runs the reviewed local runner manager with the path in `GITHUB_RUNNER_TOKEN_FILE` and in place of `{}`, and removes the file when the manager exits, fails, or is interrupted. The manager must read the file without copying the token into command arguments, persistent configuration, compose files, or Docker environment settings. Pass `--ephemeral` only for an ephemeral runner.

When using the included `runnerctl`, set `RUNNER_IMAGE` to the operator-selected compatible runner image. The manager requires a manifest digest and never supplies, builds, or updates an image itself.

```bash
repo=OWNER/REPOSITORY
labels=repository-build
: "${RUNNERCTL:=./scripts/runnerctl}"
./scripts/github-runner-gh --repo "$repo" with-registration-token \
  "$RUNNERCTL" register --repo "$repo" --token-file {} --image "$RUNNER_IMAGE" --ephemeral --labels "$labels"
```

[SKILL.md](SKILL.md) describes the persistent variant and the manual `registration-token-file` handoff for a manager that cannot take the path.

After the runner starts and its local logs contain `Listening for Jobs`, confirm its name, declared mode, and labels through GitHub, dispatch a harmless workflow run that selects the runner, and prove that a job of that run ran on the runner. The helper polls that run's jobs every 5 seconds; a completed job on the exact runner ID and name counts, so an ephemeral runner that already finished still leaves evidence.

```bash
mode=ephemeral   # or persistent, exactly as the operator stated
./scripts/github-runner-gh --repo OWNER/REPOSITORY check RUNNER_ID "$mode"
marker="verify-$(date +%s)-$$-$RANDOM"
run_id="$(./scripts/github-runner-gh --repo OWNER/REPOSITORY dispatch-run verify.yml main "marker=$marker")" || exit 1
./scripts/github-runner-gh --repo OWNER/REPOSITORY wait-assignment RUNNER_ID EXACT_RUNNER_NAME "$run_id" 180 || exit 1
./scripts/github-runner-gh --repo OWNER/REPOSITORY check RUNNER_ID "$mode" "$run_id" EXACT_RUNNER_NAME || exit 1
```

[templates/verify-runner.yml](templates/verify-runner.yml) is a verification workflow with the `marker` input and a required runner label; adding it to a repository is the operator's decision.

The first `check` fails when GitHub does not report the ephemeral field, which it does not for the observed persistent runners; the last one then records the after-job observation for either mode.

For a workflow without a `marker` input, such as one triggered by `push`, `run-for-commit WORKFLOW HEAD_SHA [EVENT]` prints the ID of the one run of the workflow for that full commit SHA, and fails as ambiguous when the commit has several runs.

`dispatch-run` prints the ID of the run this dispatch created, so the wait cannot be satisfied by an earlier run's job. The `marker` input is required: the verification workflow declares a `marker` input and `run-name: verify ${{ inputs.marker }}`, and the helper returns only the new run whose title ends with the complete marker, so the run is identified exactly even under concurrent dispatches. A listing with more than one new run carrying the marker is reported as ambiguous, so use a fresh marker every time.

Remove a stale or retired runner only after inspecting its ID and exact name:

```bash
./scripts/github-runner-gh --repo OWNER/REPOSITORY list
./scripts/github-runner-gh --repo OWNER/REPOSITORY delete RUNNER_ID EXACT_RUNNER_NAME
```

See [SKILL.md](SKILL.md) for the complete workflow and failure rules.

## Changes to output and arguments

- `wait-assignment` takes the run ID as its third argument: `wait-assignment RUNNER_ID EXACT_RUNNER_NAME RUN_ID [TIMEOUT_SECONDS]`. The timeout defaults to 120 and must not exceed 86400. Its output gained a `conclusion` column after `runner_name`.
- `run-for-commit WORKFLOW HEAD_SHA [EVENT]` is new.
- `dispatch-run` requires a `marker=VALUE` input of ASCII letters, digits, `.`, `_`, and `-`, and matches it as the last word of the run title.
- `list` and `get` gained an `ephemeral` column between `busy` and `labels`; `labels` moved from field 6 to field 7. `check RUNNER_ID MODE` takes the declared mode, `ephemeral` or `persistent`, and prints the same row before its verdict; `check RUNNER_ID MODE RUN_ID EXACT_RUNNER_NAME` adds the after-job observation.
- `jobs` gained a `runner_id` column before `runner_name`.

## Reference manager

`scripts/runnerctl` registers, runs, verifies, and removes one runner per registration in a Docker container on Linux, under the systemd user unit template in `systemd/user/`. It reaches GitHub only through the helper, feeds the helper's token file to a short-lived configuration container on its standard input and answers `config.sh`'s prompt from it under a pseudo-terminal with echo off, supervises the listener as a notify service, and stops it through a bounded stop path of 75 seconds at most, phase by phase, under the unit's `TimeoutStopSec=90`. It requires an operator-supplied runtime image pinned by digest. [SKILL.md](SKILL.md) describes the commands; `make integration` runs a real transient unit only after `RUNNERCTL_INTEGRATION_REPO` and `RUNNER_IMAGE` are set for a scratch repository and image.

## Development

The acceptance scenarios live in `features/github-runner-gh-skill.feature`. Contract tests use Python's standard library and need `jq`. The test files build on `tests/gh_harness.py`, which runs the helper against the offline `gh` fake in `tests/fake_gh.py` with a fake clock; the manager's tests add a fake `docker`, `timeout`, and `systemd-notify` through `tests/runnerctl_harness.py`. The fake reproduces gh's error output and its diagnostics, telemetry, and colour settings; its `--jq` emulation uses the system `jq`, which renders `null`, objects, and large integers differently from gh, so keep script filters to scalars and `@tsv`.

```bash
make test
```
