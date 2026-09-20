# -*- coding: utf-8 -*-
"""Phase 2.8 - the scoring boundary and its trust rules.

The AI/CV pipeline runs in the BROWSER. Everything a client sends about a
score is therefore untrusted input, and these tests pin the consequences:

  * the server recomputes `overall` itself and never stores a client's;
  * `trusted` is decided by the provider and cannot be asserted by a request;
  * an invalid, incomplete or unsupported payload is refused, not defaulted;
  * the boundary is implementation-agnostic - a different provider can be
    installed and the route, the repository and the DTO all keep working.
"""

import os

import pytest

import app as appmod
from athletix.errors import ValidationError
from athletix import scoring
from athletix.scoring import (CONTRACT_VERSION, ScoreInput, ScoreResult,
                              ScoringService)
from athletix.scoring.base import ScoringProvider
from athletix.scoring.client_submitted import ClientSubmittedScoring

from conftest import user_id

GOOD = {"speed": 70, "agility": 60, "strength": 80, "stamina": 55,
        "technique": 90}      # mean = 71.0


def post_report(client, **body):
    payload = {"m": dict(GOOD)}
    payload.update(body)
    return client.post("/api/reports", payload)


@pytest.fixture
def restore_provider():
    previous = scoring.get_service()
    yield
    scoring.set_provider(previous.provider)


class ServerVerifiedFake(ScoringProvider):
    """A stand-in for a future server-side provider. Proves the boundary is
    implementation-agnostic: nothing above it changes."""

    name = "server-verified-fake"
    version = CONTRACT_VERSION
    trusted = True

    def score(self, score_input):
        metrics = {k: min(100, v + 1) for k, v in score_input.metrics.items()}
        return ScoreResult(metrics=metrics, sport=score_input.sport,
                           provider=self.name, version=self.version,
                           trusted=True, confidence=0.9,
                           quality="server-verified", ai=None)


# ==========================================================================
# The contract objects
# ==========================================================================
class TestContracts:

    def test_overall_is_always_recomputed_by_the_server(self):
        result = ScoreResult(metrics=dict(GOOD), sport="Cricket",
                             provider="p", version=CONTRACT_VERSION,
                             trusted=False)
        assert result.overall == 71.0

    def test_a_partial_metric_set_cannot_become_a_result(self):
        with pytest.raises(ValueError):
            ScoreResult(metrics={"speed": 10}, sport="Cricket", provider="p",
                        version=CONTRACT_VERSION, trusted=False)

    def test_trust_must_be_an_explicit_bool(self):
        for bad in (1, "yes", None):
            with pytest.raises(ValueError):
                ScoreResult(metrics=dict(GOOD), sport="Cricket", provider="p",
                            version=CONTRACT_VERSION, trusted=bad)

    def test_confidence_must_be_a_fraction(self):
        with pytest.raises(ValueError):
            ScoreResult(metrics=dict(GOOD), sport="Cricket", provider="p",
                        version=CONTRACT_VERSION, trusted=False,
                        confidence=42)

    @pytest.mark.parametrize("metrics", [
        None, [], "seventy", {}, {"speed": 70},
        dict(GOOD, speed=101), dict(GOOD, speed=-1), dict(GOOD, speed="abc"),
        dict(GOOD, speed=2.5), dict(GOOD, speed=True), dict(GOOD, speed=None),
    ])
    def test_invalid_metrics_are_refused_not_defaulted(self, metrics):
        with pytest.raises(ValidationError) as exc:
            ScoreInput.from_request(1, "Cricket", {"m": metrics})
        assert exc.value.message == "Invalid metric values."

    def test_unsupported_version_is_refused(self):
        with pytest.raises(ValidationError) as exc:
            ScoreInput.from_request(1, "Cricket",
                                    {"m": dict(GOOD),
                                     "scoring": {"version": "99.0"}})
        assert exc.value.message == "Unsupported scoring version."

    def test_unknown_source_is_refused(self):
        with pytest.raises(ValidationError) as exc:
            ScoreInput.from_request(1, "Cricket",
                                    {"m": dict(GOOD),
                                     "scoring": {"source": "trust-me"}})
        assert exc.value.message == "Unknown scoring source."

    def test_the_current_version_is_accepted_and_carried(self):
        si = ScoreInput.from_request(1, "Cricket",
                                     {"m": dict(GOOD),
                                      "scoring": {"version": CONTRACT_VERSION}})
        assert si.requested_version == CONTRACT_VERSION

    def test_a_body_with_no_scoring_block_still_works(self):
        """The shipped frontend sends only {m, live} and must keep working."""
        si = ScoreInput.from_request(1, "Cricket", {"m": dict(GOOD),
                                                    "live": True})
        assert si.source == "client-live" and si.live is True

    def test_client_ai_is_bounded(self):
        result = ClientSubmittedScoring().score(ScoreInput.from_request(
            1, "Cricket",
            {"m": dict(GOOD),
             "ai": {"potential": "N" * 500, "medal": 9999,
                    "risk": "<script>", "best": "x" * 500}}))
        assert len(result.ai["potential"]) <= 40
        assert result.ai["medal"] == 0          # out of range -> default
        assert "<" not in result.ai["risk"]
        assert len(result.ai["best"]) <= 60

    def test_a_provider_must_return_a_score_result(self):
        class Rogue(ScoringProvider):
            name = "rogue"

            def score(self, score_input):
                return {"overall": 100}
        with pytest.raises(TypeError):
            ScoringService(Rogue()).score_request(1, "Cricket",
                                                  {"m": dict(GOOD)})


# ==========================================================================
# The trust boundary, over HTTP
# ==========================================================================
class TestTrustBoundary:

    def test_a_stored_report_records_its_provenance(self, athlete):
        report = post_report(athlete).get_json()["report"]
        assert report["scoring"] == {"provider": "client-submitted",
                                     "version": CONTRACT_VERSION,
                                     "trusted": False, "confidence": None}

    def test_the_client_cannot_declare_its_own_score_trusted(self, athlete):
        report = post_report(athlete, scoring={
            "trusted": True, "provider": "server-verified",
            "confidence": 1.0, "quality": "server-verified"}
        ).get_json()["report"]
        assert report["scoring"]["trusted"] is False
        assert report["scoring"]["provider"] == "client-submitted"
        assert report["scoring"]["confidence"] is None

    def test_the_database_row_agrees_with_the_response(self, flask_app,
                                                       athlete):
        report = post_report(athlete).get_json()["report"]
        with flask_app.app_context():
            row = appmod.get_db().execute(
                "SELECT * FROM reports WHERE id=?", (report["id"],)).fetchone()
        assert row["scoring_provider"] == "client-submitted"
        assert row["scoring_trusted"] == 0
        assert row["scoring_version"] == CONTRACT_VERSION

    def test_a_client_supplied_overall_is_ignored(self, athlete):
        report = post_report(athlete, overall=99.9).get_json()["report"]
        assert report["overall"] == 71.0

    def test_metrics_are_stored_exactly_as_validated(self, athlete):
        report = post_report(athlete).get_json()["report"]
        assert report["m"] == GOOD

    @pytest.mark.parametrize("body", [
        {}, {"m": {}}, {"m": {"speed": 70}}, {"m": dict(GOOD, speed=101)},
        {"m": dict(GOOD, technique="abc")}, {"m": "seventy"},
    ])
    def test_invalid_bodies_are_rejected_over_http(self, athlete, body):
        r = athlete.post("/api/reports", body)
        assert r.status_code == 400
        assert r.get_json()["error"] == "Invalid metric values."

    def test_unsupported_version_is_rejected_over_http(self, athlete):
        r = post_report(athlete, scoring={"version": "0.9"})
        assert r.status_code == 400
        assert r.get_json()["error"] == "Unsupported scoring version."

    def test_nothing_is_persisted_when_scoring_refuses(self, flask_app,
                                                      athlete):
        before = len(athlete.get("/api/my/reports").get_json()["reports"])
        assert post_report(athlete, scoring={"version": "0.9"}).status_code == 400
        athlete.post("/api/reports", {"m": {"speed": 1}})
        after = len(athlete.get("/api/my/reports").get_json()["reports"])
        assert after == before

    def test_only_athletes_may_submit_a_score(self, coach, client):
        assert post_report(coach).status_code == 403
        client.refresh_csrf()
        assert post_report(client).status_code == 401

    def test_a_score_can_never_name_another_athlete(self, flask_app, athlete,
                                                    athlete_b):
        victim = user_id(flask_app, "athlete.b@test.local")
        report = post_report(athlete, athleteId=victim).get_json()["report"]
        assert report["athleteId"] == user_id(flask_app,
                                              "athlete.a@test.local")

    def test_the_ai_verdict_is_stored_but_stays_untrusted(self, athlete):
        report = post_report(athlete, ai={"potential": "National",
                                          "medal": 88, "risk": "Low",
                                          "best": "Athletics"}
                             ).get_json()["report"]
        assert report["ai"]["potential"] == "National"
        assert report["ai"]["medal"] == 88
        assert report["scoring"]["trusted"] is False


# ==========================================================================
# Implementation-agnosticism
# ==========================================================================
class TestProviderSwap:

    def test_a_different_provider_changes_nothing_above_the_boundary(
            self, flask_app, athlete, restore_provider):
        scoring.set_provider(ServerVerifiedFake())
        report = post_report(athlete).get_json()["report"]
        assert report["scoring"] == {"provider": "server-verified-fake",
                                     "version": CONTRACT_VERSION,
                                     "trusted": True, "confidence": 0.9}
        # The provider's own metrics were stored, and overall follows them.
        assert report["m"]["speed"] == 71
        assert report["overall"] == 72.0
        with flask_app.app_context():
            row = appmod.get_db().execute(
                "SELECT * FROM reports WHERE id=?", (report["id"],)).fetchone()
        assert row["scoring_trusted"] == 1
        assert row["scoring_confidence"] == 0.9

    def test_the_default_provider_is_the_configured_one(self):
        from athletix import config as cfg
        assert cfg.SCORING_PROVIDER == "client-submitted"
        assert scoring.get_service().provider.name == cfg.SCORING_PROVIDER

    def test_an_unknown_provider_name_is_a_hard_error(self, monkeypatch):
        monkeypatch.setattr("athletix.config.SCORING_PROVIDER", "magic")
        with pytest.raises(RuntimeError):
            scoring._configured_provider()

    def test_a_provider_refuses_a_version_it_does_not_implement(self):
        assert ClientSubmittedScoring().supports(CONTRACT_VERSION) is True
        assert ClientSubmittedScoring().supports("99.0") is False


# ==========================================================================
# Isolation
# ==========================================================================
class TestBoundaryIsolation:

    def _sources(self, *folders):
        root = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "athletix")
        for folder in folders:
            directory = os.path.join(root, folder) if folder else root
            for name in sorted(os.listdir(directory)):
                if not name.endswith(".py"):
                    continue
                path = os.path.join(directory, name)
                with open(path, encoding="utf-8") as fh:
                    yield ("%s/%s" % (folder, name) if folder else name,
                           fh.read())

    def test_no_cv_library_is_imported_anywhere_on_the_server(self):
        """Routes must not depend on MediaPipe/OpenCV. Today nothing does -
        the pipeline is client-side - and this keeps it that way."""
        banned = ("import mediapipe", "import cv2", "from mediapipe",
                  "from cv2", "import torch", "import tensorflow")
        offenders = []
        for name, text in self._sources("", "api", "services", "repositories",
                                        "scoring", "schemas", "security",
                                        "jobs"):
            for needle in banned:
                if needle in text:
                    offenders.append((name, needle))
        assert not offenders, offenders

    def test_routes_do_not_name_a_scoring_implementation(self):
        for name, text in self._sources("api"):
            assert "client_submitted" not in text, name
            assert "ClientSubmittedScoring" not in text, name

    def test_services_talk_to_the_interface_not_an_implementation(self):
        for name, text in self._sources("services"):
            assert "ClientSubmittedScoring" not in text, name
