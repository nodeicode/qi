# Security Policy

Qi is a public, MIT-licensed SDK. It is a read-only foundation: the live
trading deployment (credentials, EC2, SSM) lives in a private superset that is
not part of this repository.

## Reporting a vulnerability

Please **do not** open a public issue for a security bug. Report it privately
via the GitHub security advisory form:

https://github.com/nodeicode/qi/security/advisories/new

Include what you found (affected files/flows, impact) and, when possible, a
minimal reproduction. Never include live credentials or API keys in a report.

## What we commit to

- No secrets are ever committed to this repo (see `AGENTS.md` §2). `.env`,
  `*.pem`, `*.key`, `*.p12` are gitignored, and history is kept free of
  credential material.
- Real credentials are fetched at runtime from AWS SSM in the private ops
  path — never from this repo.
- Reports are triaged promptly; expect an acknowledgement within 7 days.
