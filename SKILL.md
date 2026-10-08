---
name: github-runner-gh-skill
description: Manage repository-level GitHub Actions self-hosted runners through GitHub CLI, including secure token handoff, status inspection, job-assignment verification, and ghost-free deregistration for ephemeral and persistent runners. Use when registering, auditing, testing, or removing a self-hosted runner for a GitHub repository.
---

# GitHub runner management through gh

Use GitHub CLI for every interaction with GitHub. Local runtime tools may manage a container or service, but they must not call GitHub directly.

## Hard requirements

- Run `gh auth status` first and require `ADMIN` repository permission.
- Resolve the target as an explicit `OWNER/REPOSITORY`. Do not guess when more than one repository is plausible.
- Ask the operator whether the runner is ephemeral or persistent before registering, verifying, or removing it, and record the answer as `mode`. Do not infer it from a manager or GitHub. A persistent runner is what operators also call a constant runner. Every later step follows the declared mode.
  - Ephemeral: GitHub deregisters the runner after one job. For images that expose the switch, set `EPHEMERAL=true`; with the official runner configuration script, require `--ephemeral`. A finished job is its only lasting evidence, and `delete` is only for a registration that never ran or crashed.
  - Persistent: the runner keeps its registration and its credentials until it is removed, so never pass `--ephemeral` or `EPHEMERAL=true`, stop it through the reviewed manager before removing it, and hand the manager a removal token so it can unconfigure itself before the registration is deleted.
- Confirm the declared mode through GitHub with `check RUNNER_ID MODE`. It fails when GitHub reports the other mode or does not report the ephemeral state at all. After the verification job, `check RUNNER_ID MODE RUN_ID EXACT_RUNNER_NAME` records whether the registration's state is consistent with the declared mode; that is an observation, never a proof, and the mode itself rests on the registration flags the operator passed.
- Obtain registration and removal tokens only with `gh api`.
- Write each short-lived token to a temporary file with mode `0600`. Never print it, store it in a compose file, commit it, or put it in Docker `-e` or `--env` settings. Disable GitHub CLI diagnostics for token requests; `GH_DEBUG=api` exposes response bodies on stderr.
- Do not add `self-hosted` as a custom label. It is a reserved default label added by GitHub. Use narrow labels that identify the repository, host capability, or workload; a persistent runner picks up every job its labels select, so its labels must not overlap another workload's by accident. `check RUNNER_ID MODE` fails when `self-hosted` appears as a custom label in any letter case.
- Do not accept API status alone as proof that the runner works. Require the local log line `Listening for Jobs` and an actual job of the workflow run you dispatched whose `runner_id` and runner name match the new registration and whose GitHub status is `in_progress` or `completed`.
- Deregister a stale or retired runner with `gh api --method DELETE repos/OWNER/REPOSITORY/actions/runners/RUNNER_ID`. Confirm both runner ID and exact runner name before deletion.
- Keep machine-specific runtime paths outside this skill. Accept a validated `RUNNERCTL` or equivalent path from the operator or project configuration; the reference manager `scripts/runnerctl` in this repository satisfies the contract below, and another reviewed manager that satisfies it is equally acceptable.

## Included helper

Use `scripts/github-runner-gh` from this repository. It fails unless `gh` is authenticated and the current account has repository admin permission. It rejects unknown commands, wrong argument counts, an empty `--repo` or `GH_REPO` value, a mode other than `ephemeral` or `persistent`, and IDs or timeouts that are not positive integers within range before it calls GitHub; only ASCII digits count, in every locale. It ignores leading zeros in IDs. Every command except `auth` requires `--repo` or `GH_REPO`; the helper never infers the repository from the current directory. `get`, `check`, `delete`, and `wait-assignment` need `jq`.

When a request fails, the error repeats GitHub CLI's own error lines, including hints such as a missing token scope. The requests whose responses the helper parses run with GitHub CLI diagnostics, telemetry output, update notices, forced colour, and forced terminal output disabled, so those settings cannot reach a parsed response, and only GitHub CLI's error lines decide how a failure is classified. Use `gh api` directly when you need `GH_DEBUG` output.

```bash
gh auth status
scripts/github-runner-gh --repo OWNER/REPOSITORY permission
scripts/github-runner-gh --repo OWNER/REPOSITORY list
```

The helper supports these commands:

- `auth` checks GitHub CLI authentication.
- `permission` prints the verified repository permission.
- `list` lists repository runners with IDs, status, busy state, ephemeral state, and labels.
- `get RUNNER_ID` returns one runner in the same format. A 404 is reported as `GitHub answers 404 for runner RUNNER_ID`.
- `check RUNNER_ID MODE` prints the runner in the same format, then fails unless GitHub reports the declared mode (`ephemeral` or `persistent`) and no `self-hosted` custom label. With `RUN_ID EXACT_RUNNER_NAME`, the run ID of the completed verification job and the exact name captured before it (the same values `wait-assignment` took), it requires a completed job of that run on that exact runner ID and name and records an observation line: a persistent runner still registered under that name, or an ephemeral runner that GitHub answers 404 for, each marked "not a proof of mode". It fails on a contradiction: a persistent runner gone, an ephemeral runner still registered, a registration now carrying another name, or a reported ephemeral state that differs from the declared mode; the reported field, when present, wins over the observation.
- `registration-token-file` creates a private temporary registration-token file and prints only its path. It fails and leaves no file when GitHub returns no token.
- `remove-token-file` creates a private temporary removal-token file and prints only its path under the same rules.
- `with-registration-token COMMAND [ARGS...]` creates the registration-token file, runs `COMMAND` with the file path in `GITHUB_RUNNER_TOKEN_FILE` and in place of every `{}` argument, removes the file when the command ends, fails, or is interrupted, and exits with the command's status. On HUP, INT, or TERM it stops the command and every process it started first, with TERM and then KILL after the grace period, so a hung command or a child it left behind cannot keep the file alive, and the supervisor that starts the command is ended with it. The command runs in its own process group and, when the helper runs on a terminal, in the foreground of that terminal, so a manager that prompts can read the answer; the terminal's interrupt keys then reach the manager rather than the helper, and a manager that ignores them keeps the file until it exits, while a signal sent to the helper itself stops the group as described. The token file path reaches the command unchanged, even when it contains `&`. The token itself never appears in an argument.
- `with-remove-token COMMAND [ARGS...]` does the same with a removal token.
- `runs` lists workflow runs that are currently in progress.
- `jobs RUN_ID` lists jobs and their assigned runner IDs and names.
- `dispatch-run WORKFLOW REF marker=MARKER [KEY=VALUE...]` triggers the workflow (a file name such as `verify.yml` or a numeric ID) on `REF` with the given inputs and prints the ID of the `workflow_dispatch` run this dispatch created. The `marker` input is required, because nothing else in a run listing ties a run to a dispatch: the workflow must declare a `marker` input and a `run-name` that ends with it, such as `run-name: verify ${{ inputs.marker }}`. The marker is one word of ASCII letters, digits, `.`, `_`, and `-`. The helper returns the one run, new since the listing it took before the dispatch and dispatched by the authenticated user, whose title is the marker or ends with a space and the marker; `abc-10` does not match `abc-1`, and a title that merely contains the marker elsewhere does not count. When one listing shows more than one new run with the marker, the helper reports them as ambiguous instead of guessing; a reused marker whose runs surface in different listings cannot be told apart, so every dispatch needs a fresh marker. It waits up to 60 seconds for the run to be listed.
- `run-for-commit WORKFLOW HEAD_SHA [EVENT]` prints the ID of the one run of the workflow for the full 40-character commit SHA, optionally narrowed to one event (`push`, `workflow_dispatch`, `pull_request`, or `schedule`). GitHub ties a run to its commit, so the run identifies itself only when it is the only one for that commit: several runs, from repeated triggers or dispatches on the same commit, are reported as ambiguous with their IDs, never picked. Any status counts; re-runs are attempts of one run. An empty listing is polled every 2 seconds for up to 60 seconds, since the run of a fresh push takes a moment to appear. This is the path for a workflow that has no `marker` input, such as one triggered by `push`; for a dispatch, `dispatch-run` with a marker remains the exact path.
- `wait-assignment RUNNER_ID EXACT_RUNNER_NAME RUN_ID [TIMEOUT_SECONDS]` polls the jobs of run `RUN_ID`, one request every 5 seconds, until a job whose runner ID and name both match is `in_progress` or `completed`, and prints it with its conclusion. It fails at once when every job of the run has completed without such a job, when GitHub answers 404 for the run, or on a 401. A rate-limit response pauses until GitHub's reset time, or for one minute on a secondary limit; any other failure prints a warning with GitHub CLI's error lines and is retried until the timeout, and the wait never sleeps past the timeout. `TIMEOUT_SECONDS` defaults to 120 and must not exceed 86400.
- `delete RUNNER_ID EXACT_RUNNER_NAME` deletes a runner only after its current name matches the confirmation value, then confirms that GitHub answers 404 for the runner ID, retrying briefly. If GitHub answers 404 to the delete request itself because the runner deregistered first, the helper reports the runner as gone and succeeds. If confirmation fails or is interrupted, the error states that GitHub accepted the deletion; if the delete request is interrupted, it states that GitHub may have applied it.
- Invocation errors, whether an argument count or a value is wrong, exit with status 2; failures reported by GitHub or the helper exit with status 1.

## Manager contract

What a local runner manager can rely on when the helper runs it through `with-registration-token` or `with-remove-token`, and what it owes in return:

- The token file exists, with mode `0600`, from before the manager starts until the manager exits, however it exits; the helper removes it afterwards, so the manager must not depend on it later and must not copy the token anywhere.
- The path arrives in `GITHUB_RUNNER_TOKEN_FILE` and in place of every `{}` argument, unchanged, even when it contains `&`. The manager reads the file itself; the token never appears in an argument or an environment variable value.
- The manager runs in its own process group and, when the helper runs on a terminal, in that terminal's foreground: it can prompt and read the answer, and the terminal's interrupt keys reach the manager rather than the helper. A manager that ignores them keeps the file until it exits.
- When the helper itself receives HUP, INT, or TERM, it sends TERM to the manager's whole process group and KILL after the grace period, `GITHUB_RUNNER_GH_STOP_GRACE_SECONDS` (a positive integer up to 600, default 5); a value outside that range is an invocation error. A manager that must run `config.sh remove` on interruption has that long, so set the variable to the removal's budget before the call.
- The helper exits with the manager's exit status; on a signal to the helper, with the signal's status.

## Reference manager

`scripts/runnerctl` is the reference manager for one runner per registration in a Docker container on Linux, supervised by the systemd user unit `systemd/user/runnerctl@.service`. It satisfies the manager contract above and reaches GitHub only through `scripts/github-runner-gh`.

- `runnerctl register --repo OWNER/REPOSITORY --labels LABELS --image IMAGE [--ephemeral] [--token-file PATH] [--seccomp FILE] [--require-userns] [--slug SLUG]` creates a labelled volume, runs `config.sh` in a short-lived container with the token file's content on the container's standard input, reads the runner ID from the `.runner` file the configuration wrote, requires its ephemeral flag to match the declared mode, confirms the ID and exact name through the helper, and prints the registration's `SLUG`, which is also the runner's exact name. The operator supplies `IMAGE`, which must include a manifest digest. The token file comes from `--token-file` or `GITHUB_RUNNER_TOKEN_FILE`, as `with-registration-token` hands it over; it is never copied. The docker client reads the `0600` token file as the helper's user and hands its content over on the container's standard input. The token reaches `config.sh` through its own prompt, answered under a pseudo-terminal with echo off. A registration that cannot be confirmed is left in state `incomplete` for `reconcile`.
- `runnerctl run SLUG` is the unit's main process. Every docker call that sets the listener up is bounded (30 seconds); one that fails or hangs ends `run` with status 1 and leaves the registration as it is for the unit to retry, since a daemon that did not answer once is no reason to deregister a runner, while a stop signal during setup goes to the stop path. It creates and starts the listener container with all capabilities dropped and with any registered runtime policy, waits up to 120 seconds for `Listening for Jobs`, notifies systemd, and waits on `docker wait` in the background so that HUP, INT, or TERM run the stop path at once. A persistent listener that exits on its own makes `run` exit 1 for the unit to restart it; an ephemeral listener that ends runs the cleanup and exits 0 only when `verify` recorded assignment evidence, otherwise 3.
- `runnerctl verify SLUG RUN_ID` runs the helper's `wait-assignment` for the registration and records its evidence line beside the state; the runbook calls it in verification step 4 when this manager is used.
- Every stop of the unit, a reboot included, runs the stop path, so a persistent runner is deregistered on stop and registered again by the operator before the unit starts again; `run` exits 4 without a registration and the unit does not restart on it. `runnerctl remove SLUG [--token-file PATH]` and the stop path share the same bounded phases, 75 seconds at most under the unit's `TimeoutStopSec=90`: `docker stop`, the removal token through `with-remove-token` into `config.sh remove` (persistent only, skipped when a token file is given directly only in the sense that the helper handoff is not needed), `get` and, when GitHub still lists the exact ID and name, `delete` through the helper, `docker rm`, and the volume only after GitHub confirmed the removal. Every phase runs under `timeout` with the KILL grace inside its budget and is recorded in the state's phase log. A container started again during the path is stopped once more, then the path ends as `incomplete`.
- `runnerctl reconcile SLUG` finishes an interrupted stop or registration without a removal token: it deletes the registration by exact ID and name when GitHub still lists it, leaves a registration with another name alone, and removes the local objects. `runnerctl status SLUG` prints the state, the container state, the helper's view of the registration, and the phase log.
- State lives in `RUNNERCTL_STATE_DIR` (the unit sets `%S/runnerctl`), one `0600` JSON file per registration with no token in it. `make test` covers the manager with a fake `docker`, `timeout`, and `systemd-notify`; `make integration` runs a real transient user unit against a scratch repository named in `RUNNERCTL_INTEGRATION_REPO`.

## Registration workflow

1. Confirm authentication, repository, permission, and the declared mode.

   ```bash
   repo=OWNER/REPOSITORY
   mode=ephemeral   # or persistent, exactly as the operator stated
   scripts/github-runner-gh --repo "$repo" permission
   scripts/github-runner-gh --repo "$repo" list
   ```

2. Validate the local runtime manager before using it. Do not hardcode a user home directory.

   ```bash
   : "${RUNNERCTL:=scripts/runnerctl}"   # the reference manager, or another reviewed one the operator names
   : "${RUNNER_IMAGE:?Set RUNNER_IMAGE to a compatible image pinned by digest}"
   test -x "$RUNNERCTL"
   "$RUNNERCTL" --help
   ```

3. Continue only when the local manager has a documented token-file input. The manager must mount or read the file without copying the token into persistent configuration, a compose file, a Docker environment variable, or the host process arguments. If it accepts only a token value in an argument or environment variable, stop and fix that manager first.

4. Register through the helper, so the token file never outlives the handoff. The helper writes the token to a private file, runs the manager with the path in `GITHUB_RUNNER_TOKEN_FILE` and in place of `{}`, and removes the file when the manager exits, fails, or is interrupted. Pass the manager the flags of the declared mode and only purpose-specific custom labels.

   ```bash
   case "$mode" in
     ephemeral)
       scripts/github-runner-gh --repo "$repo" with-registration-token \
         "$RUNNERCTL" register --token-file {} --image "$RUNNER_IMAGE" --ephemeral --labels "$labels"
       ;;
     persistent)
       scripts/github-runner-gh --repo "$repo" with-registration-token \
         "$RUNNERCTL" register --token-file {} --image "$RUNNER_IMAGE" --labels "$labels"
       ;;
   esac
   ```

   Only when the manager can take neither the path argument nor `GITHUB_RUNNER_TOKEN_FILE`, request the file yourself and run the whole handoff as one script with `repo` set, not block by block: an `EXIT` trap set in a separate shell process fires as soon as that process ends and deletes the file before the manager can read it, and the `exit` calls would close an interactive shell.

   ```bash
   : "${repo:?Set repo to OWNER/REPOSITORY}"
   registration_file=''
   trap 'rm -f -- "$registration_file"' EXIT
   trap 'exit 129' HUP
   trap 'exit 130' INT
   trap 'exit 143' TERM
   registration_file="$(scripts/github-runner-gh --repo "$repo" registration-token-file)" || exit 1
   test -n "$(find "$registration_file" -perm 600)" || exit 1
   # Pass "$registration_file" to the reviewed manager's token-file input here.
   rm -f -- "$registration_file"
   trap - EXIT HUP INT TERM
   ```

## Verification workflow

1. Read the local runtime logs and require the exact readiness signal `Listening for Jobs`.
2. Capture the new registration's runner ID from its local registration metadata or the reviewed manager. Confirm its exact name, declared mode, and labels through GitHub in one call; a matching name alone can refer to another runner.

   ```bash
   scripts/github-runner-gh --repo "$repo" check RUNNER_ID "$mode"
   ```

3. Dispatch a harmless workflow run whose `runs-on` labels select the new runner through the helper, which binds the run ID to this dispatch. Listing the latest run instead can return an earlier run, whose old completed job would satisfy the next step on a persistent runner. The verification workflow must declare a `marker` input and `run-name: verify ${{ inputs.marker }}`; the helper returns only the new run whose title ends with the marker, so the run is identified exactly even when someone else dispatches the same workflow at the same time. Use a fresh marker for every dispatch; the helper cannot tell two runs with the same marker apart when they surface in different listings.

   ```bash
   marker="verify-$(date +%s)-$$-$RANDOM"
   run_id="$(scripts/github-runner-gh --repo "$repo" dispatch-run "$workflow" "$ref" "marker=$marker")" || exit 1
   ```

   When no workflow of the repository declares a `marker` input, offer the template in [templates/verify-runner.yml](templates/verify-runner.yml): it has the `marker` input, the matching `run-name`, a required `runner_label` input with no default, `permissions: {}`, and a five-minute timeout. Adding it under `.github/workflows/` is a repository content change; ask the operator and do it only with their approval. Dispatch it with the runner's custom label: `dispatch-run verify-runner.yml "$ref" "marker=$marker" "runner_label=$label"`.

   When the workflow that selects the runner has no `marker` input and cannot be changed, use the run of a commit instead: push the commit, or take the full SHA of the commit whose run should serve as evidence, and let the helper identify its run. The commit must have exactly one run of that workflow; two dispatches on one commit are reported as ambiguous and must not be resolved by hand.

   ```bash
   run_id="$(scripts/github-runner-gh --repo "$repo" run-for-commit "$workflow" "$head_sha" push)" || exit 1
   ```

4. Prove assignment through GitHub, not merely runner presence. With the reference manager, `scripts/runnerctl verify SLUG "$run_id"` runs this same wait and records the evidence in the registration's state, which an ephemeral `run` needs before it can end successfully. A completed job on the exact runner ID and name counts, so an ephemeral runner that already finished and deregistered still leaves evidence, and the order of dispatch and wait does not matter.

   ```bash
   scripts/github-runner-gh --repo "$repo" wait-assignment RUNNER_ID EXACT_RUNNER_NAME "$run_id" 180 || exit 1
   ```

5. Record the run ID, job ID, runner ID, runner name, status, and conclusion. An `online` API value without the log and job evidence is incomplete. The helper cannot read local logs, so a successful `wait-assignment` does not replace step 1.

6. Once the job has completed, record the registration's state after it for both modes. The observation is consistent with the declared mode or a contradiction; it is not a proof of the mode. When GitHub does not report the ephemeral field, this line is the only after-the-fact check available, and the mode still rests on the registration flags of step 4.

   ```bash
   scripts/github-runner-gh --repo "$repo" check RUNNER_ID "$mode" "$run_id" EXACT_RUNNER_NAME || exit 1
   ```

## Removal workflow

1. Stop new local work and identify the exact GitHub runner record.

   ```bash
   scripts/github-runner-gh --repo "$repo" list
   scripts/github-runner-gh --repo "$repo" get RUNNER_ID
   ```

2. Follow the declared mode.
   - Ephemeral: once its job has ended, `get RUNNER_ID` answers `GitHub answers 404`, nothing remains on GitHub, and only the local runtime is left to remove through the reviewed manager. Continue to step 3 only for a registration that never ran or crashed, which `get` still returns.
   - Persistent: stop the runner through the reviewed manager, then hand it a removal token so it can unconfigure itself; the helper removes the token file when the manager exits.

     ```bash
     scripts/github-runner-gh --repo "$repo" with-remove-token "$RUNNERCTL" remove --token-file {}
     ```

3. Delete any remaining GitHub registration by ID and exact name. This is the required ghost-runner cleanup.

   ```bash
   scripts/github-runner-gh --repo "$repo" delete RUNNER_ID EXACT_RUNNER_NAME
   ```

4. The helper fails unless GitHub answers 404 for the runner ID after deletion. If the error says GitHub accepted the deletion, do not repeat the delete; run `get RUNNER_ID` once the API catches up, and treat `GitHub answers 404` as confirmation. If it says GitHub may have applied the deletion, the request was interrupted and may never have been sent: run `get RUNNER_ID`, and if it still shows the runner, run `delete` again; the name check keeps a repeated delete safe. Do not rely on `list` for this: its pages shift while runners register and deregister. Remove local containers, services, work directories, and volumes only through the reviewed local manager.

## Failure handling

- Treat a missing mode declaration, missing authentication, non-admin permission, ambiguous repository selection, token-file creation failure, mismatched runner name, a mode that GitHub does not confirm, missing readiness logs, and absent job assignment as blocked states.
- Never weaken token handling to make an incompatible local manager work.
- Never delete by name alone. Names are not stable identifiers.
- Never claim success from a delayed or cached API status.
- A rate-limit response is not a permission problem; the helper pauses and retries it within the timeout.
