from celery import Celery
from celery.signals import setup_logging

from app.core.config import get_settings
from app.core.logging import configure_logging

celery_app = Celery("auto_oncall", broker=get_settings().redis_url, include=["app.worker.tasks"])
celery_app.conf.update(
    task_ignore_result=True,
    task_serializer="json",
    accept_content=["json"],
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    worker_redirect_stdouts=False,  # keep DRY_RUN pretty-printed blocks intact
    broker_connection_retry_on_startup=True,
    # Must exceed the longest retry countdown (GitHub resets within 1h) or Redis redelivers.
    broker_transport_options={"visibility_timeout": 7200, "socket_connect_timeout": 2},
    # Fail fast on publish so the webhook can answer 503 (and Slack gets its ack within 3s).
    task_publish_retry_policy={"max_retries": 1, "interval_start": 0, "interval_max": 0.5},
)


@setup_logging.connect
def _setup_logging(**_kwargs) -> None:
    configure_logging(get_settings().log_level)
