# -*- coding: utf-8 -*-
"""Scoring boundary.

    route -> schema -> service -> ScoringService -> ScoringProvider

`ScoringService` is the only thing the assessment service talks to. It
validates the request into a `ScoreInput`, asks the configured provider for a
`ScoreResult`, and hands that back. Nothing above it names an implementation,
so swapping `ClientSubmittedScoring` for a future server-side provider is a
configuration change, not a refactor.

Today there is exactly one provider, and it says out loud that its numbers
are client-computed and unverified. That is the accurate description of the
system - see `contracts.py` for why recording it matters.
"""

from athletix.scoring.base import ScoringProvider
from athletix.scoring.client_submitted import ClientSubmittedScoring
from athletix.scoring.contracts import (CONTRACT_VERSION, SOURCES,
                                        SUPPORTED_VERSIONS, ScoreInput,
                                        ScoreResult, parse_metrics)

__all__ = ["ScoringService", "ScoringProvider", "ClientSubmittedScoring",
           "ScoreInput", "ScoreResult", "CONTRACT_VERSION",
           "SUPPORTED_VERSIONS", "SOURCES", "get_service", "set_provider",
           "PROVIDERS", "parse_metrics"]


PROVIDERS = {
    ClientSubmittedScoring.name: ClientSubmittedScoring,
}


class ScoringService(object):
    """Validate, then delegate. The only entry point above the providers."""

    def __init__(self, provider=None):
        self._provider = provider or ClientSubmittedScoring()

    @property
    def provider(self):
        return self._provider

    def score_request(self, athlete_id, sport, data):
        """Score one assessment request body.

        `data` is a loaded `ReportCreateRequest`. Validation happens in
        `ScoreInput.from_request`, which raises ValidationError for a missing
        metric, an out-of-range value, an unknown source or an unsupported
        contract version.
        """
        score_input = ScoreInput.from_request(athlete_id, sport, data)
        if not self._provider.supports(score_input.requested_version):
            from athletix.errors import ValidationError
            raise ValidationError("Unsupported scoring version.")
        result = self._provider.score(score_input)
        if not isinstance(result, ScoreResult):
            raise TypeError("%r did not return a ScoreResult"
                            % self._provider)
        return result


_service = None


def get_service():
    """The process-wide scoring service."""
    global _service
    if _service is None:
        _service = ScoringService(_configured_provider())
    return _service


def _configured_provider():
    from athletix import config as cfg
    name = (getattr(cfg, "SCORING_PROVIDER", "") or
            ClientSubmittedScoring.name)
    provider_cls = PROVIDERS.get(name)
    if provider_cls is None:
        # An unknown name must not silently fall back to something that
        # claims a different trust level.
        raise RuntimeError("unknown scoring provider: %r" % name)
    return provider_cls()


def set_provider(provider):
    """Install a provider. Returns the previous service.

    This is the single seam an implementation is swapped through - used by
    configuration and by the tests that prove the boundary is
    implementation-agnostic.
    """
    global _service
    previous = _service
    _service = ScoringService(provider)
    return previous
