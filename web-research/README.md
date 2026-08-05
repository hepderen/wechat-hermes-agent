# WeChat Hermes Web Research

This release adds a production web provider without exposing a public search
endpoint or requiring a third-party API key.

## Runtime

- `wechat-searxng.service` listens on `127.0.0.1:8651` only.
- The image is pinned to SearXNG `2026.7.15-7b2199ecd` by OCI digest.
- `wechat-cloud-web` is an opt-in Hermes backend plugin.
- Global search uses Bing HTML with RSS fallback. Freshness and news queries
  prioritize Bing News RSS before interleaving general web results, after safely
  unwrapping the publisher URL. SearXNG merging is optional and disabled by
  default. Chinese searches use fixed HTTPS mobile endpoints for Sogou, 360
  Search, and Baidu when the global route has too few useful results. Each
  domestic source has an independent circuit breaker, so a CAPTCHA on one
  source does not disable the others.
- Successful searches are cached by query hash in memory and in a private
  SQLite database. Fresh entries survive gateway restarts; expired entries may
  be used for up to 24 hours only when every live upstream fails. Query text is
  never stored in the cache.
- Extraction is performed in the Hermes process with URL safety, redirect
  re-validation, website policy, content-type and byte limits.
- Neither search queries nor page bodies are logged.

## Build a candidate

Extraction dependencies are downloaded into the candidate package from
`requirements-extract.lock` with versions and SHA-256 hashes enforced. They are
not committed as a generated vendor tree.

```bash
sudo bash scripts/build_candidate.sh "$PWD" RELEASE_ID
```

The builder requires an existing Hermes deployment and creates:

```text
/var/lib/wechat-hermes/candidates/web-research/RELEASE_ID
```

The candidate contains the private dependency directory and an isolated Hermes
home for read-only probes. After probes pass, install it with an explicit active
WeChat PID:

```bash
sudo WECHAT_PID=PID bash scripts/install_production.sh \
  /var/lib/wechat-hermes/candidates/web-research/RELEASE_ID RELEASE_ID
```

## Production gates

1. SearXNG health and JSON search must pass from the host and Hermes service user.
2. The plugin must expose both `web_search` and `web_extract` in a temporary
   Hermes home before production config is changed.
3. Unit tests cover retries, persistent and stale cache behavior, global and
   per-source circuit breakers, domestic parser fallbacks, URL filtering,
   redirect SSRF, unsupported MIME types, byte limits, extraction and URL-count
   limits.
4. Integration tests use direct tool calls and a fake Adapter request. They do
   not send messages to the real WeChat group.
5. Rollback restores the previous Hermes config and environment, then removes
   only this plugin and service. It never touches WeChat or Adapter state.

All production services remain loopback-only. Probe scripts do not call the
real Chat API or send WeChat messages.
