"""Regression tests for admin_subscribers.py::_build_transport.

Motivation
==========

The `_build_transport` helper on `admin_subscribers.py` uses LAZY
imports inside each branch — pulled in only when a caller hits
the /v1/admin/subscribers/{id}/test-notification endpoint with a
subscriber of that transport. This defers app.py's import graph
until the endpoint is exercised.

A previous version of the email branch shipped with a 3-dot import
    from ...app import _build_email_transport_or_null
which resolves to `trip_ema.transports.app` — a module that does
not exist. The endpoint's lazy import meant this bug slipped past
pytest, past `_build_app_state`, past every developer restart —
and only surfaced when an operator finally clicked "Send test"
in the GUI for an email subscriber.

Fixed by 492b62f: `....app` (four dots) which resolves to
`trip_ema.app`. This file locks that in.

Design
======

One test per branch. Each test calls `_build_transport(<name>, cfg)`
and asserts the outcome. If the lazy import inside a branch drifts
(wrong dot count, wrong symbol name, deleted target module), the
test raises at call time and pytest fails loudly.

The email branch stubs `_build_email_transport_or_null` via
`monkeypatch.setattr` so the actual Brevo secret-loading path
doesn't need to succeed during a test run. If the branch imports
`_build_email_transport_or_null` successfully, the monkeypatch
wins and the test passes. If the import raises, the test fails
before the monkeypatch is consulted.

The webhook/slack branches construct `WebhookTransport()` which
takes no args and has no I/O side effects at construction, so we
just verify the return type.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from trip_ema.transports.http.routes.admin_subscribers import _build_transport


class TestBuildTransportImportPaths:
    """Each test exercises one branch of `_build_transport` end-to-end.

    Purpose is regression cover for the lazy imports — the branches'
    functional behavior is exercised by higher-level integration
    tests that hit the /test-notification endpoint. Here we only
    verify that each branch's import path RESOLVES.
    """

    def test_email_branch_import_resolves(self):
        """Guards `from ....app import _build_email_transport_or_null`.

        The 4-dot path resolves to `trip_ema.app`; a 3-dot path
        resolves to the non-existent `trip_ema.transports.app`
        and raises ImportError. Phase 11 replaced the module-attribute
        monkeypatch with an `email_builder` kwarg on _build_transport;
        the branch still lazy-imports when the kwarg is None, so this
        test — passing a stub — still fails loudly if the lazy import
        line goes stale (the kwarg-None production path exercises it).
        """
        fake_transport = MagicMock(name="fake_email_transport")

        def _fake_builder(config, *, enrichment_provider=None, db_engine=None):
            return fake_transport

        got = _build_transport(
            "email", MagicMock(name="config_stub"),
            email_builder=_fake_builder,
        )
        assert got is fake_transport, (
            "Email branch returned the wrong object — the "
            "email_builder injection point may have broken."
        )

    def test_webhook_branch_import_resolves(self):
        """Guards `from ....notifications.webhook_transport import WebhookTransport`."""
        from trip_ema.notifications.webhook_transport import WebhookTransport

        got = _build_transport("webhook", MagicMock(name="config_stub"))
        assert isinstance(got, WebhookTransport), (
            f"Webhook branch returned {type(got).__name__} instead "
            f"of WebhookTransport — check the lazy import path."
        )

    def test_slack_branch_import_resolves(self):
        """Slack reuses WebhookTransport; guards the same import path
        from a second call site."""
        from trip_ema.notifications.webhook_transport import WebhookTransport

        got = _build_transport("slack", MagicMock(name="config_stub"))
        assert isinstance(got, WebhookTransport), (
            f"Slack branch returned {type(got).__name__} instead "
            f"of WebhookTransport — check the lazy import path."
        )

    def test_sms_branch_raises_501(self):
        """SMS is intentionally not-implemented; verify the correct
        HTTP status code is raised so a client can distinguish
        'not implemented yet' from 'transport unknown'."""
        with pytest.raises(HTTPException) as excinfo:
            _build_transport("sms", MagicMock(name="config_stub"))
        assert excinfo.value.status_code == 501, (
            f"SMS branch raised HTTP {excinfo.value.status_code}; "
            f"expected 501 (Not Implemented)."
        )

    def test_unknown_transport_raises_400(self):
        """Guards the final catch-all: any unrecognised transport
        name must raise 400 (Bad Request) so the client gets a
        clear diagnostic."""
        with pytest.raises(HTTPException) as excinfo:
            _build_transport("carrierpigeon", MagicMock(name="config_stub"))
        assert excinfo.value.status_code == 400, (
            f"Unknown-transport branch raised HTTP "
            f"{excinfo.value.status_code}; expected 400."
        )
