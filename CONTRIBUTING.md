# Contributing

## Development setup

Use Python 3.10 or 3.11:

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements-ci.txt
```

Run tests from each component directory because imports intentionally match the production working directories:

```bash
(cd adapter && ../.venv/bin/python -m pytest -q)
(cd chat-api && ../.venv/bin/python -m pytest -q)
(cd web-research && ../.venv/bin/python -m pytest -q)
.venv/bin/python adapter/scripts/live_fake_stack.py
```

On Windows, replace `.venv/bin/python` with `.venv\Scripts\python`.

## Change guidelines

- Keep Bridge identity fields server-derived and authenticated.
- Preserve compatibility for older `/api/chat` payloads unless a versioned migration is included.
- Keep control commands local to Adapter and independent of Hermes availability.
- Do not turn `run.completed` into success without Verifier evidence.
- Keep `uncertain` media terminal until an explicit user retry creates a new generation.
- Register generated files through Artifact APIs; do not introduce text markers that trigger sending.
- Keep all service listeners on loopback by default.
- Add abstractions only where they simplify a real state or trust boundary.
- Add tests proportional to race, recovery and cross-room impact.

## Required tests by area

| Change | Minimum coverage |
| --- | --- |
| Structured parsing | Realistic XML/row fixture, forged body, duplicate IDs |
| Control/stop | Before-send, UI-submit race, future message, task generation |
| Outbox | Idempotency, crash recovery, `409`, explicit retry |
| Artifact | MIME, extension, traversal, symlink, byte/count limit |
| Verifier | Success evidence, missing evidence, failed tool, bad exit code |
| Search | Global/domestic route, fallback, cache, circuit, redirect SSRF |
| Skill install | Static scan, sandbox contract, failed activation rollback |
| Deployment | Installer/service static contract and shell syntax |

## Pull request checklist

- Explain the user-visible behavior and failure mode.
- Include focused tests and the commands/results used.
- Note schema, environment, service or retention changes.
- Confirm fake-stack tests did not contact real WeChat.
- Confirm the diff contains no production identifiers, credentials or state.
- Update README/examples when configuration changes.

## Repository hygiene

Never commit live secrets, `config.json`, databases, logs, snapshots, generated Artifacts, browser profiles, release archives or production request dumps. Use `replace-*`, `fake-*`, `TARGET`, `ROOM_ID`, `BOT_WXID` and other obvious placeholders in fixtures and documentation.

Third-party Skills and vendored Python packages need license review. Prefer reproducible download/build steps with versions and hashes over generated source trees.

## Commit style

Use short imperative subjects, for example:

```text
Harden media confirmation recovery
Add structured reply identity tests
Document search candidate build
```

Keep unrelated formatting and deployment changes in separate commits.
