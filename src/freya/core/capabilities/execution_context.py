"""Broker-scoped execution identity, never accepted from model arguments."""
from contextvars import ContextVar

_broker_context = ContextVar("freya_broker_context", default=None)


def current_broker_context():
    """Return the trusted invocation context, or None outside broker dispatch."""
    return _broker_context.get()


def enter_broker_context(principal, policy):
    return _broker_context.set((principal, policy))


def exit_broker_context(token):
    _broker_context.reset(token)
