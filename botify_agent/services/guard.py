"""CSRF guard for every session-authenticated botify_agent route.

Odoo checks CSRF only for ``type='http'`` routes; ``jsonrpc`` routes are
never checked (odoo/http.py, JsonRPCDispatcher). So each route calls
``check`` first: the web client's CSRF token in ``X-Botify-CSRF``, a
same-origin fetch, and a JSON body. The only route without the token check is
the one that hands a stale page a fresh token: it insists on the same-origin
and JSON checks (a cross-site page can neither send nor read that request).
"""

from .errors import OperationError


def check_origin(request, strict=False):
    """Same-origin fetch with a JSON body. ``strict`` also refuses a missing Sec-Fetch-Site."""
    headers = request.httprequest.headers
    site = headers.get("Sec-Fetch-Site")
    if site != "same-origin" and (strict or site is not None):
        raise OperationError("CSRF_FAILED", request.env._("cross-site request refused"))
    if not (headers.get("Content-Type") or "").startswith("application/json"):
        raise OperationError("CSRF_FAILED", request.env._("a JSON request is required"))


def check(request):
    check_origin(request)
    headers = request.httprequest.headers
    if not request.validate_csrf(headers.get("X-Botify-CSRF") or ""):
        raise OperationError("CSRF_FAILED", request.env._("missing or invalid CSRF token"))
