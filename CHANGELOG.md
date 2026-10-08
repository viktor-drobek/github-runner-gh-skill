# Changelog

All notable changes to this project are documented in this file.

## 1.0.1 — 2026-10-08

### Fixed

- Passed the explicit repository and documented labels to `runnerctl register` examples.
- Passed the exact registration slug to the persistent `runnerctl remove` example.
- Required `RUNNER_IMAGE` and passed it to the opt-in integration test.
- Added documentation contract coverage for the required manager arguments.

## 1.0.0 — 2026-10-08

### Public release

- Removed browser runtime image sources, build recipes, isolation profiles, and their tests.
- Removed the embedded runtime image reference from `runnerctl`.
- Made `runnerctl register --image` mandatory and retained digest validation.
- Added public contribution and security-reporting guidance.
- Added the MIT license.
- Updated the README, skill guide, acceptance scenarios, and test coverage for the public contract.
