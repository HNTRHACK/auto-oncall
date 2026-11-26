# Auto-OnCall 🚨

Automated incident triage & root-cause analysis engine.

When a service crashes, Auto-OnCall:
1. Ingests the crash webhook (deduplicated via Redis)
2. Pulls recent commits & diffs from GitHub
3. Correlates the stack trace with code changes using an LLM + RAG over past incident runbooks
4. Posts an interactive Slack alert with the suspected commit and root cause
5. Opens a hotfix PR when an engineer approves

**Stack:** FastAPI · Celery · Redis · PyGithub · Slack Block Kit · Claude API · ChromaDB · Docker

🚧 Work in progress
