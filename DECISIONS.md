# Decisions

Choices made where the brief was ambiguous or where following it literally would not work.

## Repository
- The project lives in `auto-oncall/auto-oncall` (the folder with `.git`). The brief mentions
  `CLAUDE.md` and `.claude/settings.json`; neither existed, so nothing was created in their place.
- First pass used `venv` + pip. Second pass added `uv.lock` (`uv sync --frozen`), used by CI.
  The Dockerfile still installs with pip from `pyproject.toml` (not yet from the lock).

## Claude API
- Default model `claude-sonnet-5-5` (cheaper per incident than Opus; switch with `ANTHROPIC_MODEL`).
- **"Forcing" the schema:** this model rejects forced `tool_choice`. Used `tool_choice: auto`
  (parallel calls disabled), a `strict: true` tool, a prompt requiring the call, and Pydantic
  validation with one retry.
- Strict schemas cannot express numeric bounds, so 0–1 is enforced by Pydantic.
- Streaming, adaptive thinking, effort `high`, and server-side refusal fallbacks
  (`fallbacks="default"`, a beta) are enabled. A fallback model may bill at different rates.
  Eager tool-input streaming is left off so the strict schema applies.
- Non-retryable API errors, refusals and truncation produce the low-confidence fallback, so
  on-call is still alerted.

## Behaviour
- `stack_trace[0]` is the top frame. The fingerprint uses its filename, function and line.
- Fingerprint follows the brief exactly (no `environment`). Parts are NUL-separated.
- Dedup uses `SET NX GET`, which needs Redis 7.
- Claude also receives the **current content** of up to 6 changed files; it cannot return a full
  file from truncated diffs.
- Extra PR guards: commit and file must be known, author taken from GitHub, no no-op patches,
  no `.github/` edits, refusal if the file changed since triage. Total diff size is capped.
- Retries use `self.retry` with a classifier rather than `autoretry_for`, because the delay
  depends on response headers.
- Empty secrets reject all requests. `DRY_RUN` defaults to `true`.
- A second button click re-renders the Slack message with the existing PR link.

## DRY_RUN
- "Zero network calls" means no external services; Redis is still required.
- Dry run uses a local hashing embedding, because Chroma's default downloads a model.
- The fake LLM blames the newest commit touching the highest matching stack frame. Two eval
  scenarios are built to defeat it, so the dry-run score is 8/10 by design.

## Tooling
- `simulate_crash.py --approve` was added so the demo covers the Slack click and PR; it reads
  Redis to know when triage finished.
- Compose builds the image once (on `api`); `worker` reuses it. Ports bind to localhost.
- Added `.dockerignore`.

## Second pass (hardening)
- **Webhook replay:** signature now covers `<timestamp>.<body>` with an `X-Webhook-Timestamp`
  header and a 5-minute window. This is a breaking change for senders. No nonce store: within
  the window a replay is a duplicate and the fingerprint dedups it.
- **Slack replay:** seen signatures stored with `SET NX` for 6 minutes; replays get `200 {}` and
  are dropped (a 4xx would show the user an error for what may be a double-submit).
- **Approver allowlist:** `SLACK_APPROVER_IDS`. Empty means anyone in `DRY_RUN` (so the demo
  works) and nobody in live mode, matching how empty secrets fail closed. Unauthorised users
  get an ephemeral reply via `response_url`, sent from a Celery task to keep the 3-second ack;
  only `https://hooks.slack.com/` URLs are ever called.
- **Failure alerts:** a task that gives up (retries exhausted, or a permanent error) posts a
  separate red Slack message rather than editing the original alert, because for triage there
  is no original alert yet. Alerting never raises.
- **Metrics:** counters in a Redis hash, not `prometheus_client`, so API and workers share
  totals without multiprocess mode. `/metrics` is unauthenticated; keep it internal.
- **Default model** changed to `claude-sonnet-5-5` for cost. The request options (adaptive
  thinking, effort, server-side fallbacks beta) were written for the first default and have not
  been checked against Sonnet with a live key.
- **Lint:** ruff (E, F, W, I, B), line length 100, enforced in CI.

## Third pass (real-world integration)
- **Sentry adapter** (`POST /webhook/sentry`): built from Sentry's public webhook docs and a
  fixture shaped like their `event_alert` example, **not tested against a live Sentry org**.
  Accepts `event_alert` and `error` resources; everything else is `200 ignored` so Sentry does
  not count it as a failure. Uses the last exception value, in-app frames, reversed order.
- Enqueue failures on the Sentry route return 500, because Sentry does not retry 503/504.
- Sentry event ids are kept for the incident TTL to stop replays (no signed timestamp).
- `CrashPayload.source_url` (optional, https only) links the Slack alert to the original report.
- **SDK** (`auto_oncall_client`): stdlib only so it can be vendored into any service without
  dependency conflicts. Uncaught exceptions are sent synchronously (the process is dying);
  handled ones on a daemon thread. Frames made repo-relative so path matching works.
- **Patch checks** run only static parsing; executing model-written code in the worker would be
  a remote-code-execution hole. Running the target repo's tests belongs in an isolated sandbox
  or simply in the PR's CI.
- **Slack manifest** added so Slack setup is one paste.
