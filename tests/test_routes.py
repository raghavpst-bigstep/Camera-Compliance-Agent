"""Route wiring.

Forgetting to include the router is a silent failure: the service starts, Cloud
Run reports healthy, and every Pub/Sub push 404s into a retry loop.

The check reads the OpenAPI schema rather than walking ``app.routes``, because
newer FastAPI versions keep included routers nested rather than flattened.
"""

from __future__ import annotations

import main

EXPECTED = {
    ("/mail", "post"),
    ("/chat", "post"),
    ("/sweep", "post"),
    ("/admin/start-watch", "post"),
    ("/healthz", "get"),
}


def _served() -> set[tuple[str, str]]:
    schema = main.app.openapi()
    return {
        (path, method)
        for path, operations in schema.get("paths", {}).items()
        for method in operations
        if method in ("get", "post")
    }


def test_every_expected_route_is_served():
    missing = EXPECTED - _served()
    assert not missing, f"routes not served: {sorted(missing)}"


def test_no_unexpected_write_routes():
    """Anything that mutates state should be one of the four known endpoints."""
    served_posts = {path for path, method in _served() if method == "post"}
    expected_posts = {path for path, method in EXPECTED if method == "post"}
    assert served_posts == expected_posts
