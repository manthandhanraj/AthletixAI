# -*- coding: utf-8 -*-
"""Phase 2.6 - request schemas and response DTOs.

The schema layer is where hostile input stops being ambiguous. These tests
pin the two properties the rest of the code depends on:

  * `load()` is total - it returns a safe value or raises ValidationError,
    and never lets a half-parsed body through;
  * `load()` returns EXACTLY the declared keys, so an undeclared client key
    can never be forwarded into a service call.
"""

import pytest

from athletix.errors import ValidationError
from athletix.schemas import requests as rq
from athletix.schemas import responses as dto
from athletix.schemas.fields import (Choice, Code, DataUrl, Email, Flag,
                                     Identifier, Integer, Mapping, Password,
                                     Phone, Schema, StringList, Text)


class TestFieldSemantics:

    def test_text_is_sanitised_and_capped(self):
        class S(Schema):
            fields = (("v", Text(10)),)
        assert S.load({"v": "  hello  "})["v"] == "hello"
        assert S.load({"v": "<script>x"})["v"].startswith("&lt;")
        assert len(S.load({"v": "x" * 500})["v"]) <= 10

    def test_text_strips_control_characters(self):
        class S(Schema):
            fields = (("v", Text(50)),)
        assert "\x00" not in S.load({"v": "a\x00b"})["v"]

    def test_password_is_never_mangled(self):
        """A password containing '<' must survive verbatim, or the user can
        never log in again."""
        class S(Schema):
            fields = (("p", Password()),)
        raw = "  <My Pass'word> "
        assert S.load({"p": raw})["p"] == raw

    def test_email_is_normalised(self):
        class S(Schema):
            fields = (("email", Email()),)
        assert S.load({"email": "  MiXeD@Test.LOCAL "})["email"] == \
            "mixed@test.local"

    def test_email_validation_rejects_a_malformed_address(self):
        class S(Schema):
            fields = (("email", Email(validate=True)),)
        with pytest.raises(ValidationError) as exc:
            S.load({"email": "not-an-email"})
        assert exc.value.status == 400
        assert exc.value.message == "Please enter a valid email address."

    def test_code_keeps_digits_only(self):
        class S(Schema):
            fields = (("code", Code()),)
        assert S.load({"code": "1a2b3c4d5e6f7"})["code"] == "123456"
        assert S.load({"code": {"nested": 1}})["code"] == ""

    def test_integer_bounds_and_rejection(self):
        class S(Schema):
            fields = (("n", Integer(0, 80, default=None)),)

        class Strict(Schema):
            fields = (("n", Integer(0, 80, error="Invalid years.")),)
        assert S.load({"n": 40})["n"] == 40
        assert S.load({"n": 900})["n"] is None
        assert S.load({"n": True})["n"] is None          # bool is not an int
        assert S.load({"n": 2.5})["n"] is None
        with pytest.raises(ValidationError) as exc:
            Strict.load({"n": "abc"})
        assert exc.value.message == "Invalid years."

    def test_identifier_rejects_non_positive_values(self):
        class S(Schema):
            fields = (("id", Identifier()),)
        assert S.load({"id": 7})["id"] == 7
        for bad in (0, -1, "abc", None, [1], {"a": 1}, True):
            assert S.load({"id": bad})["id"] is None

    def test_choice_falls_back_or_rejects(self):
        class Soft(Schema):
            fields = (("v", Choice(("a", "b"), default=None)),)

        class Hard(Schema):
            fields = (("v", Choice(("a", "b"), error="Invalid role.")),)
        assert Soft.load({"v": "zzz"})["v"] is None
        with pytest.raises(ValidationError) as exc:
            Hard.load({"v": "owner"})
        assert exc.value.message == "Invalid role."

    def test_mapping_and_flag(self):
        class S(Schema):
            fields = (("m", Mapping(default={})), ("live", Flag()))
        assert S.load({"m": [1, 2]})["m"] == {}
        assert S.load({"m": {"a": 1}})["m"] == {"a": 1}
        assert S.load({"live": "yes"})["live"] is True
        assert S.load({})["live"] is False

    def test_string_list_is_bounded_and_sanitised(self):
        class S(Schema):
            fields = (("a", StringList(max_items=2, max_len=5)),)
        out = S.load({"a": ["aaaaaaaaaa", "<b>", "third"]})["a"]
        assert len(out) == 2 and len(out[0]) <= 5
        assert S.load({"a": "not a list"})["a"] is None

    def test_phone_uses_the_same_policy_as_the_service(self):
        class S(Schema):
            fields = (("phone", Phone()),)
        assert S.load({"phone": "+91 98765 4321"})["phone"]
        with pytest.raises(ValidationError) as exc:
            S.load({"phone": "not a phone"})
        assert exc.value.message == "Please enter a valid phone number."

    def test_data_url_is_never_truncated_or_escaped(self):
        class S(Schema):
            fields = (("photo", DataUrl()),)
        raw = "data:image/png;base64," + "A" * 5000
        assert S.load({"photo": raw})["photo"] == raw
        assert S.load({"photo": None})["photo"] == ""
        assert S.load({})["photo"] == ""


class TestSchemaContract:

    def test_load_returns_only_declared_keys(self):
        loaded = rq.SignupRequest.load(
            {"role": "athlete", "name": "A", "email": "a@test.local",
             "password": "x", "is_admin": True, "verified": 1, "id": 99})
        assert set(loaded) == set(rq.SignupRequest.field_names())
        for forbidden in ("is_admin", "verified", "id"):
            assert forbidden not in loaded

    def test_role_escalation_keys_cannot_reach_a_service(self):
        """The profile schema has no `role` field at all, so a body carrying
        one is structurally incapable of changing a role."""
        assert "role" not in rq.ProfileUpdateRequest.field_names()
        assert "verified" not in rq.ProfileUpdateRequest.field_names()
        loaded = rq.ProfileUpdateRequest.load({"role": "owner",
                                               "verified": 1, "name": "n"})
        assert "role" not in loaded and "verified" not in loaded

    def test_owner_is_not_in_the_role_change_vocabulary(self):
        with pytest.raises(ValidationError):
            rq.RoleChangeRequest.load({"role": "owner"})

    def test_load_tolerates_a_non_dict_body(self):
        for junk in (None, [], "string", 5):
            assert rq.LoginRequest.load(junk)["email"] == ""

    def test_load_present_keeps_partial_update_semantics(self):
        sent = rq.CoachProfileRequest.load_present({"bio": ""})
        assert sent == {"bio": ""}
        assert "specialty" not in sent
        assert rq.CoachProfileRequest.load_present(
            {"achievements": "not a list"}) == {}

    def test_report_schema_marks_client_supplied_blocks(self):
        loaded = rq.ReportCreateRequest.load(
            {"m": {"speed": 1}, "ai": {"potential": "National"},
             "live": 1, "athleteId": 42})
        assert "athleteId" not in loaded, "a report can never name its athlete"
        assert loaded["live"] is True
        assert loaded["video"] is None


class TestResponseDTOs:

    def test_page_envelope_shape(self):
        payload = dto.paged("things", [1, 2], total=10, limit=2, offset=0)
        assert payload["ok"] is True
        assert payload["things"] == [1, 2]
        assert payload["page"] == {"limit": 2, "offset": 0, "total": 10,
                                   "returned": 2, "has_more": True,
                                   "next_offset": 2}

    def test_message_dto_shape_is_stable(self):
        row = {"id": 1, "to_id": 2, "from_id": 3, "from_nm": "C",
               "body": "hi", "date": "2026-01-01T00:00:00"}
        assert dto.message_dto(row) == {"id": 1, "toId": 2, "fromId": 3,
                                        "from": "C", "text": "hi",
                                        "date": "2026-01-01T00:00:00"}

    def test_rating_dto_hides_attribution_from_third_parties(self):
        row = {"coach_id": 5, "athlete_id": 9, "stars": 4}
        assert dto.rating_dto(row, viewer_id=9, privileged=False)["byId"] == 9
        assert dto.rating_dto(row, viewer_id=1, privileged=False)["byId"] is None
        assert dto.rating_dto(row, viewer_id=1, privileged=True)["byId"] == 9

    def test_dtos_never_expose_credential_columns(self):
        row = {"id": 1, "to_id": 2, "from_id": 3, "from_nm": "C",
               "body": "hi", "date": "d", "pass_hash": "$2b$secret",
               "sess_epoch": 4}
        rendered = str(dto.message_dto(row))
        assert "pass_hash" not in rendered and "sess_epoch" not in rendered
