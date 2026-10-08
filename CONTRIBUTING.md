# Contributing

Thank you for improving this skill.

## Before opening a change

- Discuss significant behaviour changes in an issue first.
- Keep GitHub interactions in `scripts/github-runner-gh`; the local manager must not call GitHub directly.
- Never commit tokens, credentials, private repository data, image definitions, or build recipes.
- Describe the runner mode explicitly. Do not infer ephemeral or persistent mode.

## Development workflow

1. Create a branch from the current default branch.
2. Add or update an acceptance scenario in `features/github-runner-gh-skill.feature` before changing behaviour.
3. Keep documentation and tests aligned with the command contract.
4. Run the complete offline suite:

   ```bash
   make test
   ```

5. Run `make integration` only when you deliberately have a Docker daemon and user systemd available. It is not part of the ordinary test suite.

## Pull requests

Explain the user-visible change, its security implications, and the tests you ran. Keep each pull request focused. Do not include generated runtime state or local machine paths.
