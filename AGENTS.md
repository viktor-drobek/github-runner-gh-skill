# Repository agent rules

This repository contains one standalone agent skill for repository-level GitHub Actions self-hosted runner management.

- Use `gh` for every GitHub interaction. Do not add another HTTP client for GitHub API calls.
- Keep local container and service management outside this repository, except the reference manager in `scripts/runnerctl` and its unit template in `systemd/`.
- The manager never calls GitHub itself: every GitHub interaction, including reconciliation and registration checks, goes through `scripts/github-runner-gh`.
- Token values never appear in process arguments on the host, in container arguments, in environment variable values, in Docker configuration or `docker inspect` output, in compose files, in logs, in state files, or in generated files. Token file paths in `GITHUB_RUNNER_TOKEN_FILE` and `{}` are the handoff and are allowed; the manager reads that file once, as the user the helper ran as, to feed the configuration container's standard input, and never copies or mounts it.
- `make test` uses fakes and never starts a container or a unit. Tests that need a real Docker daemon or systemd run only through `make integration`, which the operator runs deliberately.
- The operator supplies the runtime image. `runnerctl` accepts it only when it is pinned by digest; do not add image definitions or build recipes to this repository.
- Never print, commit, persist, or place runner tokens in process arguments, compose files, or Docker environment settings.
- Preserve the operator-declared runner mode (ephemeral or persistent, never inferred), exact-name deletion confirmation, and actual job-assignment verification.
- Add or update a scenario in `features/github-runner-gh-skill.feature` before changing behavior.
- Run `make test` before committing.
- Do not put AI model names in commit authors, generated files, or documentation credits.
