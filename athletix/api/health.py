# -*- coding: utf-8 -*-
"""Liveness and readiness endpoints.

Deliberately split:

  /health        liveness  — is this process up? No dependencies touched, so
                             it stays fast and cannot be made to fail by a
                             database hiccup. Safe for a load balancer.
  /health/ready  readiness — can this process actually serve traffic? Runs a
                             trivial query against the database.

Neither reveals infrastructure detail: no paths, versions, hostnames,
connection strings or configuration values. A failing readiness check reports
*that* it failed, never *why* — the reason goes to the log.
"""

from flask import Blueprint, current_app, jsonify

bp = Blueprint("health", __name__)


@bp.get("/health")
def liveness():
    """Is the process alive? Touches nothing."""
    return jsonify(status="ok"), 200


@bp.get("/health/ready")
def readiness():
    """Is the process able to serve? Verifies the database answers."""
    try:
        from athletix.database import ping
        ping()
    except Exception:
        current_app.logger.exception("readiness check failed")
        return jsonify(status="unavailable"), 503
    return jsonify(status="ready"), 200
