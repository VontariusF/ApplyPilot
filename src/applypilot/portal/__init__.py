"""ApplyPilot localhost web portal (Deliverable D).

FastAPI + HTMX server-rendered UI over the shared SQLite store, bound to
127.0.0.1 behind a `[portal]` optional dependency. Entry: `applypilot portal`.
"""

from applypilot.portal.app import create_app

__all__ = ["create_app"]
