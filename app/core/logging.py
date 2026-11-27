import logging
import sys

import structlog


def configure_logging(level: str = "INFO") -> None:
    """Route structlog and stdlib (celery, libraries) logs to stdout as JSON lines."""
    if hasattr(sys.stdout, "reconfigure"):  # Windows consoles default to cp1252
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    shared = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
    ]
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.format_exc_info,
                structlog.processors.JSONRenderer(),
            ],
        )
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):  # use the JSON handler too
        server_log = logging.getLogger(name)
        server_log.handlers = []
        server_log.propagate = True


def pretty(title: str, text: str) -> None:
    """Human-readable multi-line block for DRY_RUN output (JSON log lines would escape it)."""
    print(f"----- {title} -----\n{text}\n----- end {title} -----", flush=True)
