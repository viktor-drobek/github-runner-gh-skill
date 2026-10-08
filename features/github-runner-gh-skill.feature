Feature: Manage repository-level self-hosted runners through GitHub CLI

  Background:
    Given GitHub CLI is authenticated
    And the operator has admin access to the target repository

  Scenario: Create a registration token without exposing it
    When the operator requests a runner registration token
    Then the token is written to a temporary file with mode 0600
    And the token is not printed to standard output
    And the operator removes the file after local runner registration

  Scenario: Hand a token file to the local runner manager without a caller-side script
    When the operator runs with-registration-token or with-remove-token with the manager command
    Then the helper writes the token to a private temporary file
    And it runs the command with the file path in GITHUB_RUNNER_TOKEN_FILE and in place of {} arguments
    And it removes the file when the command ends, fails, or is interrupted
    And on HUP, INT, or TERM it stops the command and every process it started before removing the file, with KILL after a grace period when they ignore TERM
    And the command runs in its own process group and, when the helper runs on a terminal, in its foreground, so it reads the terminal and receives its interrupt keys
    And the helper removes the file when the command exits, however it was interrupted
    And the command's own process reports its process group before it starts, and cleanup ends the supervisor that started it as well, so a stopped supervisor can neither hide the group nor block the cleanup
    And a token file path containing & reaches the command unchanged
    And it exits with the command's status

  Scenario: Give the manager a configurable grace period on interruption
    Given GITHUB_RUNNER_GH_STOP_GRACE_SECONDS is set to a positive integer of at most 600 seconds
    When the helper stops the command it started with a token file or a pending request
    Then it waits that long between TERM and KILL instead of the default 5 seconds
    And a value that is not a positive integer within that range is an invocation error before any request

  Scenario: Scope of the reference manager
    Given local container and service management stays outside this repository by default
    Then the reference manager in scripts/runnerctl and its unit template in systemd/ are the only exceptions
    And the manager never calls GitHub itself: every GitHub interaction goes through scripts/github-runner-gh
    And token values never appear in host or container arguments, environment variable values, Docker configuration, compose files, logs, state files, or generated files, while token file paths remain the handoff
    And the manager feeds the token to the configuration container on its standard input, read from the 0600 file by the docker client as the helper's user, since the container's user cannot read that file, and answers config.sh's prompt through a pseudo-terminal with echo off, so the token is never echoed
    And make test uses fakes and never starts a container or a unit; real Docker and systemd runs happen only through make integration
    And the operator supplies a compatible runtime image pinned by digest when registering a runner

  Scenario: Request tokens while GitHub CLI diagnostics are enabled
    Given GH_DEBUG or DEBUG enables GitHub CLI diagnostics
    When the operator requests a registration or removal token
    Then the token is not printed to standard output or standard error
    And only the private token file path is returned

  Scenario: Keep GitHub CLI output settings out of parsed responses
    Given GH_DEBUG, GH_TELEMETRY=log, CLICOLOR_FORCE, or GH_FORCE_TTY is set in the environment
    When the helper parses a GitHub response for get, check, delete, or wait-assignment
    Then diagnostics, telemetry, colour codes, and update notices do not reach the parsed output
    And only GitHub CLI's own error lines decide how a failure is classified, never a response body

  Scenario: Clean up an interrupted or failed token request
    Given the helper has created a temporary token file
    When token creation fails or the helper receives HUP, INT, or TERM before handoff
    Then the helper removes the temporary token file
    And any active token request is stopped
    And the helper exits unsuccessfully without returning a token file path

  Scenario: Reject a token response without a token value
    When GitHub returns a token response whose token is missing, null, or empty
    Then the helper fails
    And no token file is left behind

  Scenario: Require the operator to state the runner mode
    Given the operator has not said whether the runner is ephemeral or persistent
    When the skill is asked to register, verify, or remove a runner
    Then it asks for the mode before contacting GitHub and never infers it from a manager or from GitHub
    And every later step follows the rules of the declared mode

  Scenario: Register a runner in the declared mode
    When the local runner manager consumes the registration token file
    Then an ephemeral runner is configured with EPHEMERAL=true or --ephemeral
    And a persistent runner is configured without them
    And custom labels do not include the reserved label "self-hosted"
    And no secret is stored in a compose file or Docker environment setting

  Scenario: Confirm the declared mode and labels through GitHub
    When the operator checks a registered runner by ID and declared mode
    Then the check prints the runner record and fails unless GitHub reports the declared mode
    And it fails when GitHub does not report the runner's ephemeral state
    And it fails when "self-hosted" is present as a custom label in any letter case
    And a mode other than ephemeral or persistent is an invocation error
    When the operator adds the run ID of the completed verification job and the exact runner name captured before it
    Then the helper requires a completed job of that run on that exact runner ID and name
    And it records an observation, never a proof of mode: a persistent runner still registered under that name, or an ephemeral runner that GitHub answers 404 for
    And it fails on a contradiction: a persistent runner gone, an ephemeral runner still registered, a registration now carrying another name, or a reported ephemeral state that differs from the declared mode
    And runner listings show the ephemeral state of every runner

  Scenario: Bind the verification run to the dispatch
    When the operator dispatches the verification workflow through dispatch-run
    Then the helper requires a marker input of ASCII letters, digits, ".", "_", and "-", since nothing else ties a listed run to this dispatch
    And it lists the workflow's dispatch runs before triggering it
    And it prints the ID of the new run by the authenticated user whose title ends with the complete marker as its last word, never an earlier run's ID, another user's run, a run whose marker merely starts with it, nor a run whose title carries the marker elsewhere
    And the title is compared as GitHub returned it, never through an escaped listing
    And it fails when one listing shows more than one new run with the marker or no such run appears within the dispatch timeout

  Scenario: Offer a verification workflow template
    Given a target repository has no workflow with a marker input
    When the operator agrees to add one
    Then the template in templates/verify-runner.yml declares the marker input, a run-name that ends with it, a required runner label with no default, no permissions, and a timeout
    And adding it is presented as a repository content change the operator approves, never done silently

  Scenario: Identify the run of a commit without a dispatch
    When the operator names a workflow and the full commit SHA whose run should serve as evidence, with an optional event
    Then the helper lists the workflow's runs for that commit through gh and prints the one run ID
    And it fails as ambiguous, naming every ID, when the commit has more than one run of the workflow, since only a single run identifies itself
    And it keeps polling for up to a minute when no run is listed yet, then fails
    And a SHA that is not 40 ASCII hexadecimal characters, a workflow path, or an unknown event is an invocation error before any request

  Scenario: Verify that a runner can accept work
    When the runner process reports "Listening for Jobs"
    And the operator dispatches a harmless workflow run with dispatch-run and captures its run ID
    And the operator supplies the registered runner ID, exact name, and that run ID
    Then the helper inspects only that run's jobs, one request per poll
    And it reports the first job whose runner ID and name match and whose status is in_progress or completed
    And the evidence includes the run ID, job ID, status, runner ID, runner name, and conclusion
    And API runner status alone is not accepted as proof

  Scenario: Reject a job assigned to another runner with the same name
    Given another runner has the same name as the registered runner
    When a job of the run is assigned to the other runner ID
    Then that job is not accepted as assignment evidence
    And verification fails if no matching job appears before the timeout

  Scenario: Stop waiting when the run cannot produce evidence
    When every job of the run has completed and none ran on the runner
    Then the helper fails at once and says the run finished without a job on the runner
    When GitHub answers 404 for the run or 401 for the request
    Then the helper fails at once with GitHub CLI's own error text

  Scenario: Keep waiting for job assignment through transient API failures
    When a GitHub API request fails while waiting for job assignment
    Then the helper reports the failure with GitHub CLI's own error lines, including hints, and keeps polling until the timeout
    And a response that is empty or cannot be parsed counts as a failed request, never as an empty one
    And a rate-limit response pauses until GitHub's reset time, or one minute for a secondary limit, never past the timeout
    And the wait never sleeps past the requested timeout
    And the final timeout message names the last failure when no poll completed
    And the documented verification script stops when wait-assignment fails

  Scenario: Require an explicit repository
    When the operator omits both --repo and GH_REPO for any command except auth
    Then the helper fails without calling GitHub
    And it never infers the repository from the current directory

  Scenario: Reject invalid input before contacting GitHub
    When the operator passes an unknown command, a wrong number of arguments, an empty repository through --repo or GH_REPO, or an ID or timeout that is not a positive integer within range
    Then the helper fails without calling GitHub
    And every invocation error exits with status 2, whether the argument count or a value is wrong
    And digits outside ASCII are rejected in every locale

  Scenario: Read numeric IDs without shell arithmetic
    When the operator passes a runner or run ID with leading zeros
    Then the helper uses the same decimal ID without the leading zeros
    And an ID beyond the 64-bit range is rejected, since GitHub IDs are 64-bit integers
    And job runner IDs are compared as decimal text, so a large ID cannot match a neighbouring ID

  Scenario: Remove a runner according to its mode
    When the runner is ephemeral and its job has ended
    Then GitHub answers 404 for it and only the local runtime is left to remove
    And delete is used only for an ephemeral registration that never ran or crashed
    When the runner is persistent
    Then the local runtime is stopped through the reviewed manager first
    And a removal token is handed to the manager with with-remove-token
    And any remaining registration is deleted by ID and exact name

  Scenario: Remove a runner without leaving a ghost registration
    Given the operator knows the runner ID and exact runner name
    When the operator confirms both values
    Then the runner is deleted with the repository runners API through gh
    And the helper confirms that GitHub answers 404 for the runner ID, retrying briefly
    And if that confirmation fails or is interrupted, the helper reports that GitHub accepted the deletion
    And if the delete request itself is interrupted, the helper reports that GitHub may have applied it and that delete may be run again when get still shows the runner
    And if GitHub answers 404 to the delete request because the runner deregistered first, the helper reports the runner as gone and succeeds
    And the operator confirms an unconfirmed deletion with get RUNNER_ID, whose 404 message says "GitHub answers 404"
    And get, check, and delete explain a 404 the same way
    And the local runtime is removed separately

  Scenario: Document complete manager commands
    When the skill documents a runner registration handoff
    Then the manager command carries the explicit repository, narrow custom labels, and a digest-pinned image
    And the persistent removal handoff carries the exact registration slug
