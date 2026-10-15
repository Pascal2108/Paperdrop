# Security policy

## Reporting a vulnerability

Please **do not open a public issue** for a security problem. Report it privately instead:

1. Open the [Security tab](https://github.com/Pascal2108/paperdrop/security) of this repository.
2. Click **Report a vulnerability** and fill in the form.

Only the maintainer can see the report. A good report contains:

- what an attacker can do, and what they need for it (e.g. network access, a valid login)
- the steps to reproduce it, or a proof of concept
- the version you tested (the image tag, e.g. `1.0.2` or `beta`)
- whether you have published it anywhere, or plan to

Leave out tokens, passwords, host names and documents of a real setup.

## What happens next

Paperdrop is maintained in spare time, so the times below are aims, not promises:

- an answer within **7 days**, saying whether the report is confirmed
- for a confirmed problem, a fixed version as soon as possible, released first as a beta
  and then as a stable version
- a GitHub security advisory once the fix is released, crediting you if you wish

Please give the fix time to be released before you publish details.

## Supported versions

| Version | Security fixes |
|---|---|
| The newest stable release (image tag `latest`) | Yes |
| `beta` | Yes, it receives the fix first |
| Older releases | No; update to `latest` |

The image is also rebuilt every week, so that security updates of the base image and the
libraries reach `latest` without a new release.

## Scope

In scope is Paperdrop itself: the code in this repository and the published image
`ghcr.io/pascal2108/paperdrop`.

Not in scope:

- Paperless-ngx, the reverse proxy and other software Paperdrop works with; please report
  problems there to their own projects
- setups that ignore the [README](README.md#not-for-the-public-internet), e.g. Paperdrop
  reachable from the public internet, or running without HTTPS or without the container
  hardening of `docker-compose.yml`
- guessing a weak password; Paperdrop already limits the attempts per address

Paperdrop is built for private networks: it has one shared login, no two-factor
authentication, and it has not been audited.
