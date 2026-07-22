"""SQLAlchemy models for the `billing` module.

Tenant-owned tables MUST be created via the create_tenant_table() Alembic helper so
that RLS is enabled + forced automatically (RFC-002 §7). Never import this module
from another module — go through `service`.
"""

from __future__ import annotations
