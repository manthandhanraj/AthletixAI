# -*- coding: utf-8 -*-
"""The provider that describes what actually happens today.

The browser runs MediaPipe, derives five metrics, and POSTs them. This class
is the honest model of that: it accepts those numbers, re-derives everything
the server is willing to own, and records that the result is not verified.

What it deliberately does NOT do:

  * invent a confidence. The client reports no calibrated confidence, and a
    fabricated one would be worse than none - it would look like evidence.
  * trust the client's `overall`. `ScoreResult` recomputes it from the
    metrics, every time.
  * trust the client's AI verdict. It is bounded and stored as a
    presentation field on an untrusted report, not as a server judgement.
"""

from athletix.scoring.base import ScoringProvider
from athletix.scoring.contracts import (CONTRACT_VERSION, QUALITY_UNVERIFIED,
                                        ScoreResult, sanitize_ai)


class ClientSubmittedScoring(ScoringProvider):
    """Scores computed in the browser and submitted over the API."""

    name = "client-submitted"
    version = CONTRACT_VERSION
    trusted = False

    def score(self, score_input):
        return ScoreResult(
            metrics=score_input.metrics,
            sport=score_input.sport,
            provider=self.name,
            version=self.version,
            # Not a placeholder: this is the finding. The number came from a
            # machine the user controls.
            trusted=False,
            confidence=None,
            quality=QUALITY_UNVERIFIED,
            ai=sanitize_ai(score_input.client_ai),
        )
