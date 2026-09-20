# -*- coding: utf-8 -*-
"""Direct messaging, with the Phase 1 communication policy enforced
server-side by may_message()."""

from flask import Blueprint, jsonify, request

from athletix.pagination import page_params
from athletix.repositories import messages
from athletix.schemas import requests as rq
from athletix.schemas import responses as dto
from athletix.security.ratelimit import too_many
from athletix.security.session import current_user, login_required
from athletix.services import messaging as messaging_service
from athletix.validation import body

bp = Blueprint('messaging', __name__)


@bp.post("/messages")
@login_required
def send_message():
    u = current_user()
    limited = too_many("message", str(u["id"]))
    if limited:
        return limited
    d = rq.SendMessageRequest.load(body())
    message_id = messaging_service.send_message(u, d["toId"], d["text"])
    return jsonify(ok=True, id=message_id, message="Message sent.")


@bp.get("/messages/<int:other_id>")
@login_required
def get_conversation(other_id):
    """One conversation thread - only if the caller is a party to it."""
    u = current_user()
    limit, offset = page_params(request.args)
    rows = messages.conversation(u["id"], other_id, limit, offset)
    return jsonify(dto.paged(
        "messages", dto.messages_dto(rows),
        messages.count_conversation(u["id"], other_id), limit, offset))
