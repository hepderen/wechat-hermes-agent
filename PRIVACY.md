# Privacy and Data Handling

## Data processed

The system may process:

- WeChat room IDs, sender wxids, message IDs and timestamps;
- message text, quoted-message context and attachment metadata;
- task prompts, model output and tool result summaries;
- generated files, MIME types, sizes and SHA-256 hashes;
- usage estimates, task status and delivery confirmation IDs;
- explicitly saved room or private-session memory;
- search result titles, snippets and URLs.

## Where data is stored

### Existing WeChat data

Chat API reads the configured WeChat message database and database-key file. It creates a private read-only snapshot in its cache directory. The repository contains neither file.

### Adapter database

Adapter SQLite stores inbound identities, request responses, task prompts and output, plans, events, tool evidence, Outbox content, usage and memory. Relationship profiles additionally retain stable preferences, interaction counts, opt-out choices and room/message ordering timestamps; proactive-message state never retains the source chat text. This content supports deduplication and crash recovery, so the database is sensitive even though application logs redact message bodies.

Default cleanup removes terminal task/audit records after 30 days. Stable memory has its own expiry, typically 90 days for project facts and 180 days for preferences. Operators should adjust retention to their requirements.

### Artifacts

Generated files live under a task-specific Artifact directory and are retained for seven days by default. Intermediate, cache and debug Artifacts are never selected for automatic delivery.

### Chat API state

Chat API stores send-state, database snapshots, temporary media and the outbound-control SQLite in private directories. These files are required for confirmation, idempotency and stop barriers.

### Web research cache

Search cache keys are hashes of normalized queries. Cached values may include result titles, snippets and public URLs; raw query text and extracted page bodies are not written to logs. Expired cache entries can be used briefly during total upstream failure according to configuration.

## Logging

Production logs are designed to contain IDs, status, duration, byte counts, exit codes and error types. Message text, prompts, model output, tokens, keys, file bodies and internal system prompts should stay out of logs.

Exception paths are security-sensitive. New logging statements must pass content through the existing redaction helpers and should log a bounded error type rather than the original payload.

## Memory

Room memory is shared only inside the same `room_id`; private memory is isolated by `sender_id`. Memory tools reject credential-like and sensitive personal values. Users can request `记住` and `忘记`; operators can also remove records directly from the private Adapter database during an approved maintenance procedure.

## External services

Hermes sends task content to the configured model provider. Research tasks contact public search engines and requested websites. The applicable provider privacy terms and regional data rules are part of the operator's deployment decision.

SearXNG is local and loopback-only, but its upstream engines still receive search queries. Domestic fallback endpoints and Bing receive only queries needed for the requested research task.

## Public repository hygiene

Before opening an issue, pull request or release, remove:

- `.env`, production JSON and provider configuration;
- database/key files, snapshots and state;
- logs, request dumps, screenshots and browser profiles;
- Artifacts, archives and generated media;
- real room IDs, wxids, message IDs, hostnames and IP addresses;
- tokens in current files and every Git commit.

Deleting a secret from the latest commit does not remove it from history. Rotate the secret first, then rewrite unpublished history or follow the hosting provider's sensitive-data removal process.
