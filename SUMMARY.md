# Summary

## Third pass (works with real teams' setups)
Goal: a team can adopt this without changing how they report crashes.

- **Sentry integration:** `POST /webhook/sentry` accepts Sentry issue-alert and error
  webhooks, verifies Sentry's signature, keeps in-app frames, links the Slack alert back to the
  Sentry issue, and drops redelivered events. Setup steps are in the README.
- **Python SDK:** `auto_oncall_client` (stdlib only). `init(...)` reports every uncaught
  exception; `capture_exception()` and `AutoOnCallHandler` cover handled errors.
- **Patch safety check:** the PR button only appears if the patched file still parses
  (Python/JSON/TOML) and the patch does not rewrite most of the file. The alert says why not.
- **Slack app manifest:** `slack-app-manifest.yml`, so Slack setup is one paste.

Verification:
```
pytest: 158 passed (Python 3.12, and Python 3.11 from uv.lock); ruff: clean
E2E (API + Celery worker + Redis protocol server, DRY_RUN):
  Sentry fixture webhook -> 202, redelivery -> 200 duplicate_event,
  installation resource -> 200 ignored, triage complete, Slack alert links to Sentry
  Real Python process with the SDK crashing (KeyError) -> report accepted -> triaged
```
**Not tested against a live Sentry org.** The adapter follows Sentry's documented webhook
format and their example payload; check the first real delivery in the API logs.


## Second pass (overnight hardening)
Added on top of the first build. Still nothing staged or committed.

- **Failure alerting:** a triage or hotfix task that gives up posts a red Slack alert with the
  incident id and last error, and marks the incident `triage_failed` / `hotfix_failed`.
- **Approver allowlist:** `SLACK_APPROVER_IDS`; anyone else gets an ephemeral "not authorised"
  reply and no PR. Empty list = nobody in live mode.
- **Replay protection:** crash webhooks are now signed over `<timestamp>.<body>` with
  `X-Webhook-Timestamp` (5-minute window). **Breaking change for senders**; `simulate_crash.py`
  and the README show the new scheme. Replayed Slack interactions are acknowledged and ignored.
- **`/metrics`:** Prometheus counters shared across API and workers via Redis.
- **CI:** `.github/workflows/ci.yml` runs ruff + pytest on Python 3.11 and 3.12 and builds the
  Docker image. README has the badge.
- **Lockfile:** `uv.lock`; `uv sync --extra dev` installs exact versions.
- **Lint/format:** ruff config in `pyproject.toml`; code reformatted.
- **Default model:** `claude-sonnet-5-5`.

Verification:
```
pytest: 109 passed (Python 3.12 and Python 3.11, from uv.lock)
ruff check: all passed; ruff format --check: 29 files already formatted
E2E dry run (API + Celery worker + Redis protocol server): 202 -> 200 deduplicated ->
  triage -> two clicks -> one PR; metrics showed accepted 1, deduplicated 1,
  triage_completed 1, hotfix_prs_created 1
E2E with SLACK_APPROVER_IDS=U_BOSS: click from another user -> ephemeral reply, no PR
```
The E2E ran against fakeredis's TCP server (no Redis/Docker in that sandbox), not real Redis 7.
The Dockerfile and CI workflow were not run (no Docker there); the CI job runs on first push.


## Built
- **API:** signed crash webhook with fingerprint dedup, signed Slack actions, health check.
- **Worker:** idempotent triage and hotfix-PR tasks with rate-limit-aware retries.
- **Services:** GitHub, ChromaDB runbooks, Claude RCA with validation and fallback, Redis
  incident store, Slack Block Kit. Each external client has a dry-run fake.
- **Scripts:** `seed_runbooks.py`, `simulate_crash.py`, `eval_rca.py`.
- **Packaging:** Dockerfile, docker-compose, pyproject, `.env.example`, README, DECISIONS.

Nothing was committed or staged; git history is untouched.

## pytest
```
platform win32 -- Python 3.12.10, pytest-9.2.0
collected 81 items
============================= 81 passed in 1.11s ==============================
```
All offline: fakeredis, hash embeddings, mocked clients, and a guard that fails any
non-loopback socket connection.

## Live dry-run E2E (passed)
Run twice: on Windows (Docker Redis 7, Celery `--pool=solo`, uvicorn) and with
`docker compose` (prefork worker). Output of the final Windows run:

```
GET /health -> {"status":"ok","redis":"up"}
Seeded 5 runbooks (5 in collection)
POST /webhook/crash -> 202 {"status":"accepted","incident_id":"d45ce41399bf"}
Sending the same crash with a different order number...
POST /webhook/crash -> 200 {"status":"deduplicated","incident_id":"d45ce41399bf"}
Triage finished. Clicking 'Create Hotfix PR' twice...
POST /slack/actions -> 200
POST /slack/actions -> 200
Hotfix PR: https://github.example/dry-run/repo/pull/d45ce41399bf
```
Worker events (trimmed):
```
triage_context        commits=3 files=[app/payments/processor.py] runbooks=[null-pointer-after-refactor, third-party-429]
triage_complete       confidence=0.87 commit=4a7be19c0d2f pr_offered=true
hotfix_pr_created     .../pull/d45ce41399bf
hotfix_already_exists .../pull/d45ce41399bf
```
PR diff printed by the worker:
```diff
 def charge(order):
     customer = find_customer(order.customer_id)
-    receipt_email = customer.email
+    receipt_email = customer.email if customer else order.guest_email
     return gateway.charge(order.total_cents, receipt_email)
```

## Eval
```
LLM: FakeLLM heuristic (DRY_RUN)
Accuracy: 8/10 (80%)   misses: inventory, gateway (built to defeat the heuristic)
```
**This says nothing about Claude's accuracy.** For a real score, set `DRY_RUN=false` and
`ANTHROPIC_API_KEY`, then run `eval_rca.py` (costs credits; GitHub data stays synthetic, and
live mode needs `seed_runbooks.py` run first for runbook context).

## Not verified
- **No live call to Claude, GitHub or Slack was made** (no credentials). The Claude request was
  checked through the real SDK against a mocked HTTP transport; GitHub and Slack only against
  mocks. Expect to shake out issues on the first live run.
- Chroma's default embedding model (live mode) was never downloaded or run.
- Python 3.11 is now tested (second pass) and dependencies are locked in `uv.lock`.

## Known gaps
- Failed tasks alert but are not kept in a dead-letter queue for replay.
- `/metrics` is unauthenticated (keep it internal).
- The Dockerfile installs from `pyproject.toml`, not from `uv.lock`.
- See the README trade-offs for the rest.

## What you need to set up for live mode
1. **Secrets:** new random `WEBHOOK_SECRET`; do not keep the dev values.
2. **Anthropic:** `ANTHROPIC_API_KEY`.
3. **Approvers:** `SLACK_APPROVER_IDS` with the Slack member IDs allowed to create PRs.
4. **GitHub:** fine-grained token on the target repo (Contents + Pull requests: read/write),
   `GITHUB_TOKEN`, `GITHUB_REPO=owner/name`; protect `main`.
5. **Slack:** app with `chat:write`, installed and invited to the channel; Interactivity URL
   `https://<public-host>/slack/actions`; `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`,
   `SLACK_CHANNEL`.
6. Update any real crash senders to the timestamped signature, then `DRY_RUN=false`, seed
   runbooks, restart.

## Machine notes
- `CLAUDE.md` and `.claude/settings.json` did not exist in the repo.
- Docker Desktop was off. I started it for the E2E runs and stopped it afterwards; temporary
  containers and the compose volumes were removed. The `auto-oncall` and `redis:7` images remain.
- `docker compose build` failed from Git Bash (`additional privileges requested`) and worked
  from PowerShell.
