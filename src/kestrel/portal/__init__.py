# SPDX-License-Identifier: AGPL-3.0-only
"""Kestrel localhost web portal (Deliverable D).

FastAPI + HTMX server-rendered UI over the shared SQLite store, bound to
127.0.0.1 behind a `[portal]` optional dependency. Entry: `kestrel portal`.
"""

from kestrel.portal.app import create_app

__all__ = ["create_app"]
