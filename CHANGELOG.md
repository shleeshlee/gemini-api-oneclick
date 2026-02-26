# Changelog

All notable changes to this project will be documented in this file.

The format is based on Keep a Changelog and this project follows Semantic Versioning.

## [Unreleased]

### Added
- Placeholder for upcoming changes.

## [0.1.0] - 2026-02-26

### Added
- Initial one-click deployment template for multi-account Gemini API containers.
- `scripts/install.sh` for environment validation, compose generation, deployment, and optional guard setup.
- `scripts/generate_compose.py` to generate `docker-compose.accounts.yml` from `envs/account*.env`.
- `scripts/healthcheck.sh` for per-account health probing.
- `ops/channel_guard.py` with channel auto-disable on repeated failures and auto-recover on container restart.
- `ops/install_cron.sh` to install/remove the guard loop cron task.
- `Makefile` shortcuts for deploy/ops workflows.
- `SECURITY.md` with reporting and secret-handling policy.
- GitHub Actions workflow for syntax and compose-generation checks.

### Security
- Added default ignore rules for secrets and runtime artifacts (`.env`, `envs/*.env`, `cookie-cache/`, `state/`).

