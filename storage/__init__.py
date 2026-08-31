"""
Storage backends for the AI Control Plane.

Three backends are provided:

  SqliteBackend   — zero-setup, stdlib-only. Use for local dev and the demo.
  SnowflakeBackend — production analytics, trend queries, budget rollups.
  AzureBlobBackend — append-only audit event store. Immutable; always written
                     alongside whichever primary backend is configured.

Usage:
    from storage.sqlite import SqliteBackend
    from storage.snowflake import SnowflakeBackend
    from storage.azure_blob import AzureBlobAuditBackend

    db = SqliteBackend()                     # primary
    audit = AzureBlobAuditBackend(...)       # audit trail (always on in prod)
"""

from storage.sqlite import SqliteBackend

__all__ = ["SqliteBackend"]
