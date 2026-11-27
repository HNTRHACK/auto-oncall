"""Report crashes from any Python service to Auto-OnCall. Standard library only.

    import auto_oncall_client
    auto_oncall_client.init(
        url="https://oncall.example.com",
        secret=os.environ["AUTO_ONCALL_SECRET"],
        service="payments-api",
        environment="production",
    )

After init, every uncaught exception (main thread and other threads) is reported. Call
`capture_exception()` inside an `except` block for handled errors, or attach
`AutoOnCallHandler` to a logger so `log.exception(...)` reports too.

Reporting never raises and never blocks the app for long: handled errors are sent from a
background thread; uncaught ones are sent synchronously (the process is about to die)
with a short timeout.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import sys
import threading
import time
import traceback
import urllib.request
from datetime import datetime, timezone
from types import TracebackType
from typing import Callable

__all__ = ["init", "capture_exception", "build_payload", "sign", "AutoOnCallHandler", "Client"]

_LIBRARY_MARKERS = ("site-packages", "dist-packages", f"{os.sep}lib{os.sep}python")
_client: Client | None = None
_internal_log = logging.getLogger("auto_oncall_client")


def sign(secret: str, body: bytes, timestamp: str) -> str:
    """Same scheme the server verifies: HMAC-SHA256 over "<timestamp>.<body>"."""
    return (
        "sha256="
        + hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    )


def _relative(path: str, root: str) -> str:
    try:
        rel = os.path.relpath(path, root)
    except ValueError:  # different drive on Windows
        return path.replace("\\", "/")
    return (path if rel.startswith("..") else rel).replace("\\", "/")


def build_payload(
    exc: BaseException, service: str, environment: str, root: str | None = None
) -> dict:
    """Crash report in Auto-OnCall's format. Frames are relative to `root` (default: cwd)
    so they line up with repository paths, and the raising frame comes first."""
    root = os.path.abspath(root or os.getcwd())
    frames = traceback.extract_tb(exc.__traceback__)
    app_frames = [f for f in frames if not any(m in f.filename for m in _LIBRARY_MARKERS)]
    chosen = app_frames or list(frames)
    stack = [
        {"filename": _relative(f.filename, root), "lineno": f.lineno or 0, "function": f.name}
        for f in reversed(chosen)
    ][:200]
    if not stack:  # raised without a traceback (e.g. constructed and passed in)
        stack = [{"filename": "<unknown>", "lineno": 0, "function": "<unknown>"}]
    return {
        "service_name": service,
        "environment": environment,
        "error_type": type(exc).__name__,
        "error_message": str(exc)[:10_000],
        "stack_trace": stack,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _urllib_transport(url: str, body: bytes, headers: dict[str, str], timeout: float) -> int:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return response.status


class Client:
    def __init__(
        self,
        url: str,
        secret: str,
        service: str,
        environment: str,
        root: str | None = None,
        timeout: float = 3.0,
        transport: Callable[[str, bytes, dict[str, str], float], int] | None = None,
    ):
        if not url.startswith(("https://", "http://localhost", "http://127.0.0.1")):
            raise ValueError("use https:// (plain http is only allowed for localhost)")
        self.endpoint = url.rstrip("/") + "/webhook/crash"
        self.secret = secret
        self.service = service
        self.environment = environment
        self.root = root
        self.timeout = timeout
        self.transport = transport or _urllib_transport

    def send(self, exc: BaseException) -> int | None:
        """Send synchronously. Returns the HTTP status, or None if sending failed."""
        try:
            body = json.dumps(
                build_payload(exc, self.service, self.environment, self.root)
            ).encode()
            timestamp = str(int(time.time()))
            headers = {
                "Content-Type": "application/json",
                "X-Webhook-Timestamp": timestamp,
                "X-Signature-256": sign(self.secret, body, timestamp),
            }
            return self.transport(self.endpoint, body, headers, self.timeout)
        except Exception as err:  # noqa: BLE001 - reporting must never break the app
            _internal_log.warning("auto-oncall report failed: %r", err)
            return None

    def send_async(self, exc: BaseException) -> threading.Thread:
        thread = threading.Thread(target=self.send, args=(exc,), name="auto-oncall", daemon=True)
        thread.start()
        return thread


def init(url: str, secret: str, service: str, environment: str, **kwargs) -> Client:
    """Configure reporting and hook uncaught exceptions. Safe to call more than once."""
    global _client
    _client = Client(url, secret, service, environment, **kwargs)
    _install_hooks()
    return _client


def capture_exception(exc: BaseException | None = None) -> threading.Thread | None:
    """Report a handled exception in the background. Without an argument, the one being
    handled. Returns the sending thread (join it in scripts that exit right away)."""
    exc = exc or sys.exc_info()[1]
    if _client is None or exc is None:
        return None
    return _client.send_async(exc)


_hooks_installed = False


def _install_hooks() -> None:
    global _hooks_installed
    if _hooks_installed:
        return
    previous_excepthook = sys.excepthook
    previous_thread_hook = threading.excepthook

    def excepthook(exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None):
        if _client is not None and not issubclass(exc_type, KeyboardInterrupt):
            _client.send(exc.with_traceback(tb))  # synchronous: the process is exiting
        previous_excepthook(exc_type, exc, tb)

    def thread_hook(args: threading.ExceptHookArgs):
        if _client is not None and args.exc_value is not None and args.exc_type is not SystemExit:
            _client.send(args.exc_value)
        previous_thread_hook(args)

    sys.excepthook = excepthook
    threading.excepthook = thread_hook
    _hooks_installed = True


class AutoOnCallHandler(logging.Handler):
    """Report records that carry an exception, e.g. `log.exception("charge failed")`."""

    def __init__(self, level: int = logging.ERROR):
        super().__init__(level)

    def emit(self, record: logging.LogRecord) -> None:
        if record.exc_info and record.exc_info[1] is not None and record.name != _internal_log.name:
            capture_exception(record.exc_info[1])
