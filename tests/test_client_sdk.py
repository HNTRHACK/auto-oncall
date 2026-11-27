"""The stdlib-only reporting SDK talks the same protocol the API verifies."""

import logging
import os
import sys
import threading
from unittest.mock import MagicMock

import pytest

import auto_oncall_client as sdk
from app.core.security import verify_webhook_signature


def boom():
    customer = None
    return customer.email  # AttributeError raised here


def raised() -> BaseException:
    try:
        boom()
    except AttributeError as exc:
        return exc
    raise AssertionError


@pytest.fixture(autouse=True)
def reset_sdk(monkeypatch):
    monkeypatch.setattr(sdk, "_client", None)
    monkeypatch.setattr(sdk, "_hooks_installed", False)
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)


def via_test_client(client):
    """Transport that posts into the FastAPI app instead of over the network."""

    def transport(url, body, headers, timeout):
        assert url.endswith("/webhook/crash") and timeout == 3.0
        return client.post("/webhook/crash", content=body, headers=headers).status_code

    return transport


def test_payload_is_relative_and_top_frame_first():
    payload = sdk.build_payload(
        raised(), "svc", "prod", root=os.path.dirname(os.path.dirname(__file__))
    )
    top = payload["stack_trace"][0]
    assert top["filename"] == "tests/test_client_sdk.py" and top["function"] == "boom"
    assert payload["stack_trace"][1]["function"] == "raised"
    assert payload["error_type"] == "AttributeError"


def test_library_frames_are_dropped_when_app_frames_exist(monkeypatch):
    exc = raised()
    payload = sdk.build_payload(exc, "svc", "prod")
    assert all("site-packages" not in f["filename"] for f in payload["stack_trace"])


def test_exception_without_traceback_still_reports():
    payload = sdk.build_payload(ValueError("x"), "svc", "prod")
    assert payload["stack_trace"] == [
        {"filename": "<unknown>", "lineno": 0, "function": "<unknown>"}
    ]


def test_signature_matches_server(settings):
    body, ts = b'{"x": 1}', "1700000000"
    sig = sdk.sign(settings.webhook_secret, body, ts)
    assert verify_webhook_signature(settings.webhook_secret, body, ts, sig, now=1700000000)


def test_end_to_end_report_is_accepted(client, settings, monkeypatch):
    delay = MagicMock()
    from app.worker import tasks

    monkeypatch.setattr(tasks.triage_incident, "delay", delay)
    reporter = sdk.Client(
        "http://localhost:8000",
        settings.webhook_secret,
        "payments-api",
        "production",
        transport=via_test_client(client),
    )
    assert reporter.send(raised()) == 202
    assert reporter.send(raised()) == 200  # same crash again: deduplicated
    payload = delay.call_args.args[1]
    assert payload["service_name"] == "payments-api"
    assert payload["stack_trace"][0]["function"] == "boom"


def test_wrong_secret_is_rejected(client):
    reporter = sdk.Client("http://localhost", "wrong", "s", "e", transport=via_test_client(client))
    assert reporter.send(raised()) == 401


def test_send_never_raises():
    def broken(*_args):
        raise OSError("network down")

    assert sdk.Client("https://x.io", "s", "svc", "e", transport=broken).send(raised()) is None


def test_plain_http_refused_except_localhost():
    with pytest.raises(ValueError):
        sdk.Client("http://oncall.example.com", "s", "svc", "e")
    sdk.Client("http://127.0.0.1:8000", "s", "svc", "e")


def test_uncaught_exception_hook_reports_and_chains(monkeypatch):
    previous = MagicMock()
    monkeypatch.setattr(sys, "excepthook", previous)
    transport = MagicMock(return_value=202)
    sdk.init("https://x.io", "s", "svc", "prod", transport=transport)
    exc = raised()
    sys.excepthook(type(exc), exc, exc.__traceback__)
    transport.assert_called_once()
    previous.assert_called_once()


def test_keyboard_interrupt_is_not_reported(monkeypatch):
    monkeypatch.setattr(sys, "excepthook", MagicMock())
    transport = MagicMock()
    sdk.init("https://x.io", "s", "svc", "prod", transport=transport)
    sys.excepthook(KeyboardInterrupt, KeyboardInterrupt(), None)
    transport.assert_not_called()


def test_thread_crash_is_reported(monkeypatch):
    monkeypatch.setattr(threading, "excepthook", MagicMock())
    transport = MagicMock(return_value=202)
    sdk.init("https://x.io", "s", "svc", "prod", transport=transport)
    worker = threading.Thread(target=boom)
    worker.start()
    worker.join()
    transport.assert_called_once()


def test_init_twice_does_not_double_wrap(monkeypatch):
    monkeypatch.setattr(sys, "excepthook", MagicMock())
    transport = MagicMock(return_value=202)
    sdk.init("https://x.io", "s", "svc", "prod", transport=transport)
    sdk.init("https://x.io", "s", "svc", "prod", transport=transport)
    exc = raised()
    sys.excepthook(type(exc), exc, exc.__traceback__)
    assert transport.call_count == 1


def test_capture_exception_and_logging_handler():
    transport = MagicMock(return_value=202)
    sdk.init("https://x.io", "s", "svc", "prod", transport=transport)
    try:
        boom()
    except AttributeError:
        sdk.capture_exception().join()
    logger = logging.getLogger("sdk-test")
    logger.addHandler(sdk.AutoOnCallHandler())
    try:
        boom()
    except AttributeError:
        logger.exception("charge failed")
    for thread in threading.enumerate():
        if thread.name == "auto-oncall":
            thread.join()
    assert transport.call_count == 2
    logger.error("no exception attached")  # ignored
    assert transport.call_count == 2


def test_capture_without_init_is_a_no_op():
    assert sdk.capture_exception(ValueError("x")) is None
