#!/usr/bin/env python3
"""Rule-targeted unread Exchange item polling sensor."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.exchange import create_account, plain  # noqa: E402

SENSOR_CREDENTIALS_ROOT = Path("/run/secrets")


def read_credentials(path_value: Any) -> dict[str, Any]:
    if not isinstance(path_value, str) or not os.path.isabs(path_value):
        raise ValueError("credential_file must be an absolute path")
    root = SENSOR_CREDENTIALS_ROOT.resolve()
    candidate = Path(path_value).resolve()
    if candidate == root or root not in candidate.parents:
        raise ValueError("credential_file must be below /run/secrets")
    try:
        value = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("credential_file must contain a readable JSON object") from exc
    if not isinstance(value, dict):
        raise ValueError("credential_file must contain a JSON object")
    return value


def poll_once(account: Any, config: Mapping[str, Any], emit: Callable[[dict[str, Any]], Any]) -> int:
    folder_name = str(config.get("folder", "Inbox"))
    page_size = max(1, min(500, int(config.get("page_size", 50))))
    items = account.root.get_folder_by_name(folder_name).filter(is_read=False)[:page_size]
    emitted = 0
    for item in items:
        payload = {
            "item_id": str(item.item_id),
            "change_key": str(item.changekey),
            "subject": str(item.subject or ""),
            "body": str(item.body or ""),
            "datetime_received": str(plain(item.datetime_received) or ""),
            "folder": folder_name,
        }
        if emit(payload) is None:
            raise RuntimeError("Attune event emission failed")
        if bool(config.get("mark_read", True)):
            item.is_read = True
            item.save(update_fields=["is_read"])
        emitted += 1
    return emitted


def production_sensor() -> type:
    import attune

    class ItemSensor(attune.PollingSensor):
        def setup(self) -> None:
            self.interval = 5.0
            self._next_due: dict[int, float] = {}
            self._failures: dict[int, int] = {}
            self._locks: dict[int, threading.Lock] = {}

        def poll(self, rule: Any) -> None:
            rule_id = int(rule.rule_id)
            config = dict(rule.trigger_params or {})
            now = time.monotonic()
            if now < self._next_due.get(rule_id, 0):
                return
            interval = max(5, min(3600, int(config.get("poll_interval_seconds", 60))))
            self._next_due[rule_id] = now + interval
            lock = self._locks.setdefault(rule_id, threading.Lock())
            if not lock.acquire(blocking=False):
                return
            try:
                credentials = read_credentials(config.get("credential_file"))
                account, _ = create_account(credentials)

                def emit(payload: dict[str, Any]) -> int:
                    event_id = self.emit(payload, rule=rule, target_rule=True)
                    if event_id is None:
                        raise RuntimeError("Attune event emission failed")
                    return event_id

                count = poll_once(account, config, emit)
                self._failures[rule_id] = 0
                if count:
                    self.logger.info("rule %s emitted %s Exchange item event(s)", rule_id, count)
            except Exception as exc:
                failures = self._failures.get(rule_id, 0) + 1
                self._failures[rule_id] = failures
                self._next_due[rule_id] = time.monotonic() + min(600, interval * (2 ** min(failures - 1, 5)))
                self.logger.warning("rule %s Exchange poll failed: %s", rule_id, type(exc).__name__)
            finally:
                lock.release()

    return ItemSensor


if __name__ == "__main__":
    import attune
    attune.run_sensor(production_sensor())
