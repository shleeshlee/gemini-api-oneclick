# Security Policy

## Supported Versions

This project is pre-1.0. Security fixes are applied to the latest `main` branch.

## Reporting a Vulnerability

Please do **not** open a public issue for sensitive security reports.

Report by email to your own security contact mailbox for this project and include:

1. A clear description of the issue
2. Steps to reproduce
3. Impact assessment
4. Suggested mitigation if available

Response targets:

1. Initial acknowledgement: within 72 hours
2. Triage result: within 7 days
3. Fix plan or temporary mitigation: as soon as feasible

## Secrets Handling Rules

This repository must never contain real secrets.

1. Do not commit `.env`
2. Do not commit `envs/*.env`
3. Do not commit `cookie-cache/`
4. Do not commit runtime state/log files under `state/`
5. Use `.env.example` and `envs/account.env.example` only

## Threat Model Notes

This project uses browser-session cookies for upstream authentication.
Treat those cookies as high-risk credentials.

Operational recommendations:

1. Restrict server/network access
2. Rotate credentials regularly
3. Use least privilege on host and CI runners
4. Keep Docker and dependencies up to date
