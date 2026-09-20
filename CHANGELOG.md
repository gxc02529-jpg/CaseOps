# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- MIT license, `.editorconfig`, and Dependabot configuration.
- Contributing guide, changelog, and GitHub issue / pull request templates.
### Changed

- Dependabot now groups only Python minor and patch updates. Major version
  bumps arrive as individual pull requests so each migration gets its own
  review instead of being buried in an unreviewable batch.
- Bumped `actions/checkout` to v7 and `actions/setup-python` to v7.
- CI now cancels superseded runs before starting a new one, and byte-compiles
  sources before running the tests.
- Coverage is reported on the Python test matrix (CaseOps).
## [0.1.0] - 2026-09-19

### Added

- Initial public reference implementation.