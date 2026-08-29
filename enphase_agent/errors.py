"""Domain exceptions. Callers catch these — never pyenphase's or pybreaker's."""

from __future__ import annotations


class EnphaseAgentError(Exception):
    """Base for everything this package raises on purpose."""


class AuthError(EnphaseAgentError):
    """Trust-boundary failure: Enlighten/entrez rejected our credentials."""


class StaleStateError(EnphaseAgentError):
    """Write refused: last known state is too old to act on safely."""


class PolicyRejected(EnphaseAgentError):
    """A guardrail said no; the message carries the specific reason."""


class CircuitOpen(EnphaseAgentError):
    """Breaker is open; the gateway gets a rest before we try again."""
