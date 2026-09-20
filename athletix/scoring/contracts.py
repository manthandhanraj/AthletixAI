# -*- coding: utf-8 -*-
"""The scoring contract: what goes in, what comes out, and what is trusted.

Where the AI/CV actually runs today
-----------------------------------
In the browser. `templates/index.html` loads MediaPipe Tasks Vision, extracts
33 pose keypoints per frame, and derives five biomechanic metrics from them.
The server has never computed a score and this phase does not change that.

What this module is for is therefore narrow and specific: it makes the
*shape* of a score explicit, and it makes the *trust* question explicit, so
that a future server-side provider can be dropped in without touching a
route, a repository or the frontend.

The trust rule
--------------
A score that arrived over HTTP was computed on a machine the user controls.
It can be replayed, edited or invented. So:

  * the server ALWAYS recomputes `overall` from the metrics it stores - a
    client-supplied overall is never persisted;
  * every metric is range-checked and every derived text field is bounded;
  * `trusted` is decided by the provider, never read from the request. A body
    that says `{"scoring": {"trusted": true}}` changes nothing.

`trusted=False` is not a defect: it is an accurate statement that the number
came from the client. Recording it is what makes a later server-verified
score distinguishable from today's.
"""

from athletix.config import METRICS
from athletix.errors import ValidationError
from athletix.validation import as_int, clean, overall_of

# The version of THIS contract - the shape of ScoreInput/ScoreResult - not of
# any particular model. A client may name the version it was built against;
# anything we do not support is refused rather than guessed at.
CONTRACT_VERSION = "1.0"
SUPPORTED_VERSIONS = ("1.0",)

# How a set of metrics reached us. All of these are client-side origins
# today; a server-side provider would add its own.
SOURCES = ("client", "client-cv", "client-live", "manual")

METRIC_MIN = 0
METRIC_MAX = 100

# Quality is a coarse, honest label - not a fabricated percentage.
QUALITY_UNVERIFIED = "unverified"
QUALITY_VERIFIED = "server-verified"


class ScoreInput(object):
    """A validated request to score one assessment.

    Built by `from_request`, which is the ONLY place client JSON becomes a
    ScoreInput. Anything malformed is rejected there, so a provider never has
    to defend itself against a missing metric or a string where an integer
    belongs.
    """

    __slots__ = ("athlete_id", "sport", "metrics", "live", "source",
                 "client_ai", "requested_version", "has_video")

    def __init__(self, athlete_id, sport, metrics, live=False,
                 source="client", client_ai=None, requested_version=None,
                 has_video=False):
        self.athlete_id = athlete_id
        self.sport = sport
        self.metrics = metrics
        self.live = bool(live)
        self.source = source
        self.client_ai = client_ai
        self.requested_version = requested_version or CONTRACT_VERSION
        self.has_video = bool(has_video)

    # ── construction ──────────────────────────────────────────────────
    @classmethod
    def from_request(cls, athlete_id, sport, data):
        """Build from a loaded `ReportCreateRequest`.

        Raises ValidationError - never returns a partially valid input.
        """
        data = data if isinstance(data, dict) else {}
        meta = data.get("scoring")
        meta = meta if isinstance(meta, dict) else {}

        version = meta.get("version") or CONTRACT_VERSION
        if version not in SUPPORTED_VERSIONS:
            # Refused, not silently reinterpreted: a client built against a
            # contract we do not implement must be told, or it will believe
            # numbers that mean something else.
            raise ValidationError("Unsupported scoring version.")

        source = meta.get("source") or ("client-live" if data.get("live")
                                        else "client")
        if source not in SOURCES:
            raise ValidationError("Unknown scoring source.")

        return cls(
            athlete_id=athlete_id,
            sport=sport,
            metrics=parse_metrics(data.get("m")),
            live=bool(data.get("live")),
            source=source,
            client_ai=(data.get("ai") if isinstance(data.get("ai"), dict)
                       and data.get("ai") else None),
            requested_version=version,
            has_video=bool(isinstance(data.get("video"), dict)
                           and data.get("video")),
        )


class ScoreResult(object):
    """What a provider returns, and what the assessment service persists.

    `trusted` and `provider` are set by the provider itself. Nothing in this
    object can be dictated by a request body.
    """

    __slots__ = ("metrics", "overall", "sport", "provider", "version",
                 "trusted", "confidence", "quality", "ai")

    def __init__(self, metrics, sport, provider, version, trusted,
                 confidence=None, quality=QUALITY_UNVERIFIED, ai=None):
        if not isinstance(metrics, dict) or set(metrics) != set(METRICS):
            raise ValueError("a ScoreResult must carry every metric")
        if not isinstance(trusted, bool):
            raise ValueError("trusted must be an explicit bool")
        if confidence is not None and not 0.0 <= float(confidence) <= 1.0:
            raise ValueError("confidence must be a fraction between 0 and 1")
        self.metrics = metrics
        # Server-owned, always. A client-supplied overall is never persisted.
        self.overall = overall_of(metrics)
        self.sport = sport
        self.provider = provider
        self.version = version
        self.trusted = trusted
        self.confidence = None if confidence is None else float(confidence)
        self.quality = quality
        self.ai = ai

    def as_dict(self):
        return {"metrics": dict(self.metrics), "overall": self.overall,
                "sport": self.sport, "provider": self.provider,
                "version": self.version, "trusted": self.trusted,
                "confidence": self.confidence, "quality": self.quality,
                "ai": dict(self.ai) if self.ai else None}


def parse_metrics(raw):
    """Every metric must be present and in range.

    A partial set is rejected rather than defaulted, so a malformed or
    truncated client cannot silently record a 0 that later reads as a real
    measurement.
    """
    if not isinstance(raw, dict):
        raise ValidationError("Invalid metric values.")
    metrics = {}
    for key in METRICS:
        value = as_int(raw.get(key), METRIC_MIN, METRIC_MAX)
        if value is None:
            raise ValidationError("Invalid metric values.")
        metrics[key] = value
    return metrics


def sanitize_ai(raw):
    """Bound a client-derived AI verdict.

    These four fields are rendered in the UI and in the PDF export, so they
    are cleaned and length-capped like any other stored client text. The
    verdict remains UNTRUSTED - see the module docstring - and the report it
    is attached to records that.
    """
    if not isinstance(raw, dict) or not raw:
        return None
    return {
        "potential": clean(raw.get("potential"), 40),
        "medal": as_int(raw.get("medal"), 0, 100, 0),
        "risk": clean(raw.get("risk"), 40),
        "best": clean(raw.get("best"), 60),
    }
