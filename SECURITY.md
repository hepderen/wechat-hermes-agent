# Security Policy

## Supported version

Security fixes are applied to the current `main` branch. Production operators should pin a reviewed commit or release and retain a tested rollback package.

## Reporting a vulnerability

Use the repository's **Security** tab to open a private security advisory. Include:

- affected commit or release;
- component and deployment topology;
- reproduction steps using synthetic IDs and data;
- expected and observed behavior;
- impact and any suggested fix.

Do not include live tokens, message databases, database keys, private chat content, server addresses or user identifiers. Revoke an exposed credential at its provider before preparing the report.

Public issues are suitable for ordinary bugs that contain no sensitive deployment data.

## High-impact areas

Changes in these areas require focused tests and review:

- Bridge authentication and trusted identity metadata;
- room allowlists and cross-room task access;
- stop barriers, UI submit locks and send confirmation;
- Outbox idempotency and `uncertain` media recovery;
- Artifact paths, symbolic links, MIME checks and archive extraction;
- URL redirects, SSRF filters and cloud metadata blocking;
- Hermes API scopes, MCP tokens and systemd path isolation;
- Skill audit, integrity locks and atomic activation;
- logs, memory, cleanup and secret redaction.

## Deployment expectations

- Bind Chat API, Adapter, Hermes and SearXNG to loopback interfaces.
- Use four independent random service tokens and provider-specific model credentials.
- Store secrets in root-owned `0600` environment/configuration files.
- Run Hermes and Adapter as separate non-root users.
- Keep SSH keys, Docker Socket, cloud metadata and WeChat state outside Worker access.
- Treat the WeChat database key file and message snapshots as credentials.
- Test stop races and rollback after every WeChat client upgrade.
- Keep `ALLOW_PRIVATE_WECHAT_CHAT=false` unless private-chat behavior has been reviewed.

The defaults are defense in depth, not a substitute for host patching, network controls, provider budgets and operator monitoring.

## Dependency and supply-chain policy

- Python runtime dependencies are version-pinned.
- Web extraction dependencies are installed with `--require-hashes` into a candidate package.
- Container images are pinned by digest.
- Third-party Skills are installed separately, scanned and locked to an audited snapshot.
- Pull requests should not add generated vendor trees, opaque binaries or install-time remote shell execution.
