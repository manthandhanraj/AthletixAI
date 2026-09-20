# -*- coding: utf-8 -*-
"""The scoring provider interface.

One method, one contract. A provider takes a validated `ScoreInput` and
returns a `ScoreResult`; it knows nothing about HTTP, sessions, SQL or Flask,
and nothing above it knows how it computes anything.

That is the whole point of this phase: `athletix/api/assessments.py` must not
import MediaPipe, OpenCV, numpy or any successor to them - and today it does
not import any of them, because the pipeline runs in the browser. When a
server-side implementation eventually exists it lands here as one more class,
with no change to routes, repositories, serializers or the frontend.
"""

from athletix.scoring.contracts import CONTRACT_VERSION, SUPPORTED_VERSIONS


class ScoringProvider(object):
    """Interface for anything that can turn an assessment into a score."""

    #: Stored on every report this provider scores, so a row can always be
    #: traced back to what produced it.
    name = "abstract"

    #: The contract version this provider implements.
    version = CONTRACT_VERSION

    #: Whether the server vouches for the numbers. False means "computed
    #: somewhere we do not control" - see contracts.py.
    trusted = False

    def supports(self, version):
        return version in SUPPORTED_VERSIONS and version == self.version

    def score(self, score_input):
        """Return a ScoreResult for a validated ScoreInput."""
        raise NotImplementedError

    def __repr__(self):                    # pragma: no cover - diagnostics
        return "<%s name=%s version=%s trusted=%s>" % (
            type(self).__name__, self.name, self.version, self.trusted)
