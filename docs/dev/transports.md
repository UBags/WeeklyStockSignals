# Adding a new notifier transport

The notifier worker dispatches signal events to subscribers via
pluggable "transports" — email, webhook, Slack, etc. Adding a new
one is a small, well-bounded change.

## The contract

A transport implements `trip_ema.notifications.base.NotificationTransport`:

```python
class NotificationTransport(Protocol):
    @property
    def name(self) -> str: ...
    def send(self, address: str, batch: NotificationBatch) -> bool: ...
```

- `name` is a stable string used as a label in metrics and in the
  `subscribers.transport` column. Lowercase, no spaces.
- `send` returns `True` on success, `False` on failure. Raises
  only for bugs — transient network errors should be caught
  internally and surfaced as `False`.

## Where to put it

- Implementation: `trip_ema/notifications/<name>_transport.py`
- Tests: `tests/notifications/test_<name>_transport.py` (DB-free,
  uses test doubles for the upstream service)

## Wiring it in

1. Add the transport's config to `NotifierConfig` if it has tunable
   fields (timeout, batch size, etc.).
2. Add a builder in `_build_transports_from_env` in
   `trip_ema/notifier_worker.py`.
3. Wrap with the circuit breaker via
   `_wrap_transports_with_breakers` (handles failure isolation).

## Add tests for

- Successful send returns `True`
- Transient failure returns `False` (no exception)
- Permanent failure (bad address) returns `False` (or escalates
  through the DLQ — depends on the transport's semantics)
- Batching behavior if the transport supports bundling multiple
  events into one delivery

The existing `email_transport.py` is a good template — it covers
SMTP connection management, retry on timeout, and HTML formatting.
