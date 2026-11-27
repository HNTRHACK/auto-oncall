"""Load the incident runbooks into ChromaDB (safe to re-run)."""

from app.models.schemas import Runbook
from app.services.runbook_store import get_runbook_store

RUNBOOKS = [
    Runbook(
        id="null-pointer-after-refactor",
        title="Null pointer / NoneType AttributeError after a refactor",
        content="""Symptoms: AttributeError: 'NoneType' object has no attribute ..., \
TypeError: cannot read properties of null, right after a deploy.
Cause: a refactor replaced a call that raised on a missing record (get_or_404, .one()) with one \
that returns None.
Check: diff the changed lookup helpers; find callers that dereference the result.
Fix: handle the None case or restore the raising lookup. Roll back if payments are affected.""",
    ),
    Runbook(
        id="db-pool-exhaustion",
        title="Database connection pool exhaustion",
        content="""Symptoms: TimeoutError: QueuePool limit of size reached, connection timed out, \
too many connections.
Cause: sessions not closed on error paths, long transactions, pool size lowered, traffic spike.
Check: pg_stat_activity for idle in transaction; recent changes to session handling or pool config.
Fix: use context managers for sessions, revert pool settings, restart pods as a stopgap.""",
    ),
    Runbook(
        id="n-plus-one-timeout",
        title="N+1 query causing request timeouts",
        content="""Symptoms: request or statement timeout, latency grows with list size, thousands \
of near-identical SELECT queries.
Cause: lazy-loaded relationship accessed inside a loop, often after a serializer change.
Check: query count per request in APM; recent ORM or serializer commits.
Fix: eager load with joinedload/selectinload or batch the lookup.""",
    ),
    Runbook(
        id="bad-config-deploy",
        title="Bad configuration deploy",
        content="""Symptoms: KeyError or ValueError reading settings, missing environment variable, \
crash loop on startup, connection refused to a wrong host.
Cause: config or environment change shipped without the matching code, typo in a key.
Check: diff config files, helm values and .env templates in the last deploy.
Fix: revert the config change; add startup validation for required settings.""",
    ),
    Runbook(
        id="third-party-429",
        title="Third-party API rate limiting (HTTP 429)",
        content="""Symptoms: 429 Too Many Requests, RateLimitError from an external provider, \
Retry-After header.
Cause: retries without backoff, a removed cache, or a new loop calling the API per item.
Check: outbound request rate; recent changes to client retry or caching code.
Fix: honour Retry-After, exponential backoff with jitter, restore caching, batch requests.""",
    ),
]

if __name__ == "__main__":
    store = get_runbook_store()
    store.add(RUNBOOKS)
    print(f"Seeded {len(RUNBOOKS)} runbooks ({store.collection.count()} in collection)")
