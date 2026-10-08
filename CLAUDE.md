@AGENTS.md

## Working notes

- A behavior change touches, in order: `features/github-runner-gh-skill.feature`, `scripts/github-runner-gh`, the tests in `tests/`, and the command descriptions in `SKILL.md` and `README.md`. The documented timeout and poll constants are checked against the script by a test.
- `make test` needs `jq`: `tests/fake_gh.py` uses it to emulate `gh --jq`, and the helper calls it for `get`, `check`, `delete`, and `wait-assignment`. The emulation is approximate (`null`, objects, and big integers render differently from gh), so script filters stay scalar or `@tsv`.
- Tests never reach GitHub. Both test files build on `tests/gh_harness.py`: `set_fixture` starts a scenario and resets the clock and logs; responses are keyed by `METHOD endpoint` (`runner_key`, `jobs_key`, `RUNNERS`, `RUNS`, `RATE_LIMIT`); shared fixtures cover HTTP errors (`{"status": 404}`, optionally with `"message"`, `"hints"`, or `"raw"` for a non-JSON body), failures without a response (`{"exit": 1, "stderr": ...}`), non-JSON bodies (`{"raw": ...}`), and multi-page results (`paged(...)`, also on an error for pages fetched before it). Read `sleeps()` instead of measuring wall time; `date` and `sleep` are always faked, `install_stub` fakes another command.
- The reference manager `scripts/runnerctl` is tested on the same harness with a fake `docker` and a fake `systemd-notify` (`tests/fake_docker.py`, scripted like the fake `gh`); `make test` never starts a container or a unit, `make integration` needs a real Docker daemon and a user systemd and is run by hand.
- The AGENTS.md rule on AI model names in commit authors covers `Co-Authored-By` trailers: do not add one that names a model.
