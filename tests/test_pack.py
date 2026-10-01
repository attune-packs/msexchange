from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import yaml

PACK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACK_ROOT))

from lib import exchange


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeQuery(list):
    def __getitem__(self, value):
        result = super().__getitem__(value)
        return FakeQuery(result) if isinstance(value, slice) else result


class PackTests(unittest.TestCase):
    def test_key_lookup_uses_current_sdk_signature(self):
        calls = {}
        get_key = ModuleType("attune.api_client.api.secrets.get_key")

        def sync_detailed(ref, *, client):
            calls.update(ref=ref, client=client)
            data = SimpleNamespace(value={"username": "user@example.invalid"})
            return SimpleNamespace(status_code=200, parsed=SimpleNamespace(data=data))

        get_key.sync_detailed = sync_detailed
        secrets = ModuleType("attune.api_client.api.secrets")
        secrets.get_key = get_key
        attune = ModuleType("attune")
        attune.context = SimpleNamespace(client="execution-client")
        modules = {
            "attune": attune,
            "attune.api_client": ModuleType("attune.api_client"),
            "attune.api_client.api": ModuleType("attune.api_client.api"),
            "attune.api_client.api.secrets": secrets,
        }
        with patch.dict(sys.modules, modules):
            value = exchange.fetch_key("pack.msexchange.credentials")
        self.assertEqual(value["username"], "user@example.invalid")
        self.assertEqual(calls, {"ref": "pack.msexchange.credentials", "client": "execution-client"})

    def test_action_metadata_covers_source_inventory(self):
        expected = {
            "do_attachment_directory_maintenance", "get_calendar_items", "get_folder",
            "list_folders", "save_attachments", "search_items", "send_email",
        }
        documents = [yaml.safe_load(path.read_text()) for path in sorted((PACK_ROOT / "actions").glob("*.yaml"))]
        self.assertEqual({doc["ref"].split(".", 1)[1] for doc in documents}, expected)
        for document in documents:
            self.assertEqual(document["runner_type"], "python")
            self.assertEqual(document["parameter_delivery"], "stdin")
            self.assertEqual(document["parameter_format"], "json")
            self.assertEqual(document["output_format"], "json")

    def test_trigger_sensor_rule_contracts(self):
        trigger = yaml.safe_load((PACK_ROOT / "triggers/exchange_new_item.yaml").read_text())
        sensor = yaml.safe_load((PACK_ROOT / "sensors/item_sensor.yaml").read_text())
        rule = yaml.safe_load((PACK_ROOT / "rules/attachment_directory_maintenance.yaml").read_text())
        self.assertEqual(sensor["trigger_types"], [trigger["ref"]])
        self.assertEqual(rule["trigger_ref"], "core.crontimer")
        self.assertFalse(rule["enabled"])
        self.assertEqual(
            set(trigger["output"]),
            {"item_id", "change_key", "subject", "body", "datetime_received", "folder"},
        )

    def test_key_creation_example_uses_local_ref_and_canonical_reads(self):
        readme = (PACK_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn('--local-ref credentials --name "Microsoft Exchange credentials"', readme)
        self.assertNotIn("--ref msexchange.credentials", readme)
        for path in (PACK_ROOT / "actions").glob("*.yaml"):
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            credential = document.get("parameters", {}).get("credential_key")
            if credential is not None:
                self.assertEqual(credential["default"], "pack.msexchange.credentials")

    def test_credentials_validation_rejects_invalid_input(self):
        with self.assertRaises(exchange.ExchangePackError):
            exchange.validate_credentials({"username": "user", "password": "secret"})
        with self.assertRaises(exchange.ExchangePackError):
            exchange.validate_credentials({
                "primary_smtp_address": "a@example.invalid", "username": "user",
                "password": "secret", "verify_ssl": "false",
            })

    def test_folder_and_item_normalization(self):
        folder = SimpleNamespace(folder_id="f1", name="Inbox", folder_class="IPF.Note", total_count=4, child_folder_count=1, unread_count=2)
        self.assertEqual(exchange.folder_to_dict(folder)["unread_count"], 2)
        item = SimpleNamespace(
            item_id="i1", changekey="c1", subject="Subject", sensitivity="Normal",
            attachments=[], datetime_received=None, categories=[], importance="Normal",
            is_draft=False, datetime_sent=None, datetime_created=None, body="private",
            text_body="private", sender=SimpleNamespace(email_address="sender@example.invalid"),
            to_recipients=[SimpleNamespace(email_address="to@example.invalid")],
        )
        normalized = exchange.item_to_dict(item, include_body=False, folder_name="Inbox")
        self.assertNotIn("body", normalized)
        self.assertEqual(normalized["change_key"], "c1")
        self.assertEqual(normalized["email_recipient_addresses"], ["to@example.invalid"])

    def test_attachment_path_stays_in_artifacts_and_is_unique(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"ATTUNE_ARTIFACTS_DIR": directory}):
            used: set[str] = set()
            first = exchange.safe_attachment_path("../../ report.txt", "_", used)
            first.write_text("first", encoding="utf-8")
            second = exchange.safe_attachment_path("../../ report.txt", "_", used)
            self.assertEqual(first.parent, Path(directory).resolve())
            self.assertEqual(second.parent, Path(directory).resolve())
            self.assertNotEqual(first, second)
            self.assertNotIn("..", first.name)

    def test_save_attachments_writes_synthetic_content(self):
        attachment = SimpleNamespace(name="../invoice.txt", content=b"safe fixture")
        message = SimpleNamespace(
            attachments=[attachment], subject="Invoice", datetime_sent="2026-01-01T00:00:00Z",
            sender=SimpleNamespace(email_address="sender@example.invalid"),
        )
        account = SimpleNamespace(fetch=lambda ids: [message])
        params = {"message_id": "id", "change_key": "key", "attachment_format": "BINARY"}
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"ATTUNE_ARTIFACTS_DIR": directory}):
            result = exchange.execute("save_attachments", params, account, None)
            output = Path(result[0]["attachment_files"][0])
            self.assertEqual(output.read_bytes(), b"safe fixture")
            self.assertEqual(output.parent, Path(directory).resolve())

    def test_item_sensor_marks_read_only_after_emission(self):
        sensor = load_module("msexchange_item_sensor", PACK_ROOT / "sensors/item_sensor.py")
        saved = []
        item = SimpleNamespace(
            item_id="id", changekey="key", subject="Subject", body="Body",
            datetime_received="2026-01-01T00:00:00Z", is_read=False,
            save=lambda **kwargs: saved.append(kwargs),
        )
        folder = SimpleNamespace(filter=lambda **kwargs: FakeQuery([item]))
        account = SimpleNamespace(root=SimpleNamespace(get_folder_by_name=lambda name: folder))
        with self.assertRaises(RuntimeError):
            sensor.poll_once(account, {}, lambda payload: None)
        self.assertFalse(item.is_read)
        self.assertEqual(saved, [])
        event_payloads = []
        count = sensor.poll_once(account, {}, lambda payload: event_payloads.append(payload) or 42)
        self.assertEqual(count, 1)
        self.assertTrue(item.is_read)
        self.assertEqual(saved, [{"update_fields": ["is_read"]}])
        self.assertEqual(event_payloads[0]["change_key"], "key")

    def test_maintenance_removes_old_then_largest_files(self):
        attune_stub = SimpleNamespace(run_action=lambda function: None)
        with patch.dict(sys.modules, {"attune": attune_stub}):
            maintenance = load_module("msexchange_maintenance", PACK_ROOT / "actions/attachment_maintenance.py")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "old.bin"
            large = root / "large.bin"
            small = root / "small.bin"
            old.write_bytes(b"o" * 8)
            large.write_bytes(b"l" * 12)
            small.write_bytes(b"s" * 4)
            old_time = time.time() - 3 * 86400
            os.utime(old, (old_time, old_time))
            result = maintenance.main(directory, attachment_directory_maximum_size=0, attachment_days_to_keep=1)
            self.assertEqual(result["deleted_files"], 3)
            self.assertEqual(result["remaining_bytes"], 0)

    def test_pack_contains_no_plaintext_secret_fixture(self):
        forbidden = [
            "B0bs" + "Password!",
            "BEGIN PRIVATE" + " KEY",
            "Authorization:" + " Bearer",
        ]
        for path in PACK_ROOT.rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts:
                text = path.read_text(encoding="utf-8", errors="ignore")
                for value in forbidden:
                    self.assertNotIn(value, text, str(path))


if __name__ == "__main__":
    unittest.main()
