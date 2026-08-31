"""
Alert dispatcher — typed webhook delivery with versioned envelope.

All outbound notifications from the control plane flow through this module.
Callers supply an event_type and payload dict; this module:
  1. Resolves which AlertRules match (team-specific, then platform fallback)
  2. Wraps the payload in a WebhookEnvelope (schema_version, event_id, timestamp)
  3. Dispatches to each matching webhook URL (3 retries, exponential backoff)
  4. Records the outcome in alert_history

Webhook envelope schema version: "1.0"
  Increment to "1.1" for non-breaking additions (new optional payload fields).
  Increment to "2.0" for breaking changes (field renames, removals, type changes).
  Consumers should always check schema_version before parsing payload.

Alert rule resolution order (for a given team_id + event_type + severity):
  1. Active rules matching this team_id and event_type with severity ≥ min_severity
  2. Active platform-wide fallback rules (team_id IS NULL) matching event_type

webhook_override: if supplied by the caller (e.g. an escalation webhook stored
on a WorkflowItem), that URL is used instead of looking up alert_rules.
This allows escalation routing to an individual team's webhook without
requiring a registered AlertRule for every team/event combination.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime

from core.models import (
    AlertEventType,
    AlertHistory,
    AlertRule,
    Severity,
    WebhookEnvelope,
)
from storage.sqlite import SqliteBackend

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1.0"
MAX_RETRIES = 3
RETRY_BASE_SECONDS = 1.0  # doubles on each retry: 1s, 2s, 4s


_SEVERITY_ORDER = {
    Severity.LOW: 0,
    Severity.MEDIUM: 1,
    Severity.HIGH: 2,
    Severity.CRITICAL: 3,
}


class AlertDispatcher:
    """Resolves alert rules and dispatches typed webhook envelopes."""

    def __init__(self, storage: SqliteBackend):
        self._storage = storage

    def dispatch(
        self,
        event_type: AlertEventType,
        severity: Severity,
        payload: dict,
        team_id: str | None = None,
        webhook_override: str | None = None,
    ) -> list[AlertHistory]:
        """Dispatch an alert to all matching webhook destinations.

        Returns a list of AlertHistory records (one per destination attempted),
        which are also written to storage.

        webhook_override: if set, this URL is used directly (bypass rule lookup).
        Useful for escalation webhooks stored on WorkflowItems."""

        envelope = WebhookEnvelope(
            schema_version=SCHEMA_VERSION,
            event_id=str(uuid.uuid4()),
            event_type=event_type,
            severity=severity,
            timestamp=datetime.now(),
            source="ai-control-plane",
            payload=payload,
        )

        if webhook_override:
            destinations = [webhook_override]
        else:
            destinations = self._resolve_destinations(event_type, severity, team_id)

        if not destinations:
            logger.debug(
                "No webhook destinations for event %s / team %s",
                event_type.value, team_id,
            )
            return []

        results: list[AlertHistory] = []
        for url in destinations:
            record = self._send(url, envelope)
            self._storage.insert_alert_history(record)
            results.append(record)

        return results

    def _resolve_destinations(
        self,
        event_type: AlertEventType,
        severity: Severity,
        team_id: str | None,
    ) -> list[str]:
        """Return a deduplicated list of webhook URLs that should receive
        this event, based on active alert rules."""
        rules: list[AlertRule] = []

        if team_id:
            rules = self._storage.get_alert_rules(
                team_id=team_id,
                event_type=event_type,
                active_only=True,
            )

        # Always include platform-wide fallback rules
        fallback_rules = self._storage.get_alert_rules(
            team_id=None,
            event_type=event_type,
            active_only=True,
        )
        # Deduplicate: fallback only if team-specific rule doesn't already cover it
        existing_urls = {r.webhook_url for r in rules}
        for rule in fallback_rules:
            if rule.team_id is None and rule.webhook_url not in existing_urls:
                rules.append(rule)

        # Filter by minimum severity
        sev_level = _SEVERITY_ORDER.get(severity, 0)
        matching = [
            r for r in rules
            if _SEVERITY_ORDER.get(r.min_severity, 0) <= sev_level
        ]

        return list({r.webhook_url for r in matching})

    def _send(self, url: str, envelope: WebhookEnvelope) -> AlertHistory:
        """Send the envelope to a single URL with retry logic."""
        body = json.dumps({
            "schema_version": envelope.schema_version,
            "event_id": envelope.event_id,
            "event_type": envelope.event_type.value,
            "severity": envelope.severity.value,
            "timestamp": envelope.timestamp.isoformat(),
            "source": envelope.source,
            "payload": envelope.payload,
        }).encode("utf-8")

        http_status: int | None = None
        error_message: str | None = None
        success = False

        for attempt in range(MAX_RETRIES):
            try:
                req = urllib.request.Request(
                    url,
                    data=body,
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "ai-control-plane/1.0",
                        "X-Event-Id": envelope.event_id,
                        "X-Schema-Version": envelope.schema_version,
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    http_status = resp.status
                    success = 200 <= http_status < 300
                    if success:
                        break
                    error_message = f"HTTP {http_status}"
            except urllib.error.HTTPError as exc:
                http_status = exc.code
                error_message = str(exc)
            except Exception as exc:
                error_message = str(exc)

            if attempt < MAX_RETRIES - 1:
                wait = RETRY_BASE_SECONDS * (2 ** attempt)
                logger.debug(
                    "Webhook to %s failed (attempt %d/%d), retrying in %.1fs: %s",
                    url, attempt + 1, MAX_RETRIES, wait, error_message,
                )
                time.sleep(wait)

        if not success:
            logger.warning(
                "Webhook dispatch failed after %d attempts to %s: %s",
                MAX_RETRIES, url, error_message,
            )

        # Find the rule_id for this URL (best effort — may be None for overrides)
        rule_id = self._find_rule_id(url)

        return AlertHistory(
            history_id=str(uuid.uuid4()),
            rule_id=rule_id or "override",
            event_id=envelope.event_id,
            event_type=envelope.event_type,
            dispatched_at=datetime.now(),
            http_status=http_status,
            success=success,
            error_message=error_message if not success else None,
        )

    def _find_rule_id(self, url: str) -> str | None:
        """Look up a rule_id for a given webhook URL. Returns None if the URL
        came from a webhook_override rather than a registered rule."""
        all_rules = self._storage.get_alert_rules(active_only=False)
        for rule in all_rules:
            if rule.webhook_url == url:
                return rule.rule_id
        return None
