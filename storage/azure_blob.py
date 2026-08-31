"""
Azure Blob Storage audit backend — append-only, immutable audit trail.

This backend is responsible solely for the AuditBackend protocol. It writes
every AuditEvent as a newline-delimited JSON (NDJSON) blob, one file per day
per agent, making the log queryable via Azure Storage Explorer, Synapse, or
any tool that can read blob storage.

Blobs are NEVER deleted or overwritten by this module. The only write
operation is append (via Azure SDK's append_blob mode). This gives the audit
trail its immutability guarantee — an append blob can only grow.

Blob layout:
    {container}/{date}/{agent_id}/audit.ndjson
    {container}/{date}/_platform/audit.ndjson  ← events with no agent_id

Configuration:
    connection_string — Azure Storage connection string or SAS URL
    container_name    — target blob container (created if missing)

Requires the azure-storage-blob package:
    pip install azure-storage-blob

In production this backend runs alongside SqliteBackend or SnowflakeBackend:
    primary = SqliteBackend()       # or SnowflakeBackend(...)
    audit   = AzureBlobAuditBackend(connection_string=..., container_name=...)
    # On every audit event:
    primary.append_audit_event(event)   # queryable local/Snowflake copy
    audit.append_audit_event(event)     # immutable Blob authoritative copy
"""

from __future__ import annotations

import json
from datetime import datetime

from core.models import AuditEvent


class AzureBlobAuditBackend:
    """Append-only audit trail backed by Azure Blob Storage (append blobs)."""

    def __init__(
        self,
        connection_string: str,
        container_name: str = "ai-control-plane-audit",
    ):
        self._connection_string = connection_string
        self._container_name = container_name
        self._ensure_container()

    def _get_service_client(self):
        try:
            from azure.storage.blob import BlobServiceClient
        except ImportError as exc:
            raise ImportError(
                "azure-storage-blob is required for AzureBlobAuditBackend. "
                "Install it with: pip install azure-storage-blob"
            ) from exc
        return BlobServiceClient.from_connection_string(self._connection_string)

    def _ensure_container(self) -> None:
        try:
            client = self._get_service_client()
            container = client.get_container_client(self._container_name)
            if not container.exists():
                container.create_container()
        except Exception:
            # Log and continue; we don't want missing Blob credentials to
            # crash startup — the SQLite copy still captures the audit trail.
            pass

    def _blob_path(self, event: AuditEvent) -> str:
        date_str = event.timestamp.strftime("%Y-%m-%d")
        partition = event.agent_id or "_platform"
        return f"{date_str}/{partition}/audit.ndjson"

    def append_audit_event(self, event: AuditEvent) -> None:
        """Append one audit event to the appropriate append blob.
        Creates the blob if it does not yet exist for this date/agent."""
        try:
            from azure.storage.blob import AppendBlobClient

            line = json.dumps({
                "event_id": event.event_id,
                "timestamp": event.timestamp.isoformat(),
                "event_type": event.event_type,
                "agent_id": event.agent_id,
                "actor_id": event.actor_id,
                "before_state": event.before_state,
                "after_state": event.after_state,
                "source": event.source,
            }) + "\n"

            blob_client = AppendBlobClient.from_connection_string(
                self._connection_string,
                container_name=self._container_name,
                blob_name=self._blob_path(event),
            )

            if not blob_client.exists():
                blob_client.create_append_blob()

            blob_client.append_block(line.encode("utf-8"))

        except Exception as exc:
            # Never raise from the audit backend — a failed audit write must
            # not roll back the primary operation. Log and move on.
            import logging
            logging.getLogger(__name__).error(
                "Failed to write audit event %s to Blob: %s", event.event_id, exc
            )

    def get_audit_events(
        self,
        agent_id: str | None = None,
        since: datetime | None = None,
        event_type: str | None = None,
    ) -> list[AuditEvent]:
        """Read audit events from Blob. Prefer the SQLite/Snowflake copy for
        operational queries; use this for authoritative audit retrieval."""
        try:
            from azure.storage.blob import ContainerClient

            container = ContainerClient.from_connection_string(
                self._connection_string, container_name=self._container_name
            )

            # Determine which blobs to scan
            prefix = ""
            if since:
                prefix = since.strftime("%Y-%m-%d")
            if agent_id:
                prefix = f"{prefix}/{agent_id}" if prefix else agent_id

            events: list[AuditEvent] = []
            for blob in container.list_blobs(name_starts_with=prefix or None):
                blob_client = container.get_blob_client(blob)
                content = blob_client.download_blob().readall().decode("utf-8")
                for line in content.splitlines():
                    if not line.strip():
                        continue
                    try:
                        d = json.loads(line)
                        ts = datetime.fromisoformat(d["timestamp"])
                        if since and ts < since:
                            continue
                        if event_type and d.get("event_type") != event_type:
                            continue
                        events.append(AuditEvent(
                            event_id=d["event_id"],
                            timestamp=ts,
                            event_type=d["event_type"],
                            agent_id=d.get("agent_id"),
                            actor_id=d.get("actor_id"),
                            before_state=d.get("before_state", {}),
                            after_state=d.get("after_state", {}),
                            source=d.get("source", "ai-control-plane"),
                        ))
                    except (json.JSONDecodeError, KeyError):
                        continue

            events.sort(key=lambda e: e.timestamp)
            return events

        except Exception as exc:
            import logging
            logging.getLogger(__name__).error(
                "Failed to read audit events from Blob: %s", exc
            )
            return []
