# -*- coding: utf-8 -*-
"""Server-rendered pages and PWA assets."""

from flask import (Blueprint, current_app, make_response, render_template,
                   send_from_directory)

from athletix.security.http import ensure_csrf

bp = Blueprint('pages', __name__)


@bp.route("/")
def home():
    ensure_csrf()
    return render_template("index.html")


@bp.get("/privacy")
def privacy_page():
    """Public privacy policy (DPDP-aligned)."""
    return render_template("privacy.html")


@bp.get("/terms")
def terms_page():
    """Public terms of service."""
    return render_template("terms.html")


@bp.get("/manifest.json")
def manifest():
    """PWA manifest served from root so the app is installable.

    Uses the app's configured static folder rather than the relative string
    "static": inside a package, a relative path resolves against the
    blueprint's root (athletix/), not the project root.
    """
    return send_from_directory(current_app.static_folder, "manifest.json",
                               mimetype="application/manifest+json")


@bp.get("/sw.js")
def service_worker():
    """Service worker must be served from root to control the whole scope."""
    resp = make_response(send_from_directory(current_app.static_folder, "sw.js",
                                             mimetype="application/javascript"))
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp
