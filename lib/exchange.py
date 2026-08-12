"""Exchange authentication, operations, normalization, and attachment safety.

Adapted from StackStorm Exchange's Apache-2.0 msexchange pack version 1.1.3.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


class ExchangePackError(RuntimeError):
    """Safe operator-facing configuration or operation error."""


def fetch_key(ref: str) -> dict[str, Any]:
    if not isinstance(ref, str) or not ref:
        raise ExchangePackError("credential_key must be a non-empty string")
    try:
        import attune
        from attune.api_client.api.secrets import get_key
    except ImportError as exc:
        raise ExchangePackError("attune-sdk is required to resolve credential_key") from exc
    try:
        response = get_key.sync_detailed(ref, client=attune.context.client, decrypt=True)
    except Exception as exc:
        raise ExchangePackError(f"unable to read credential Key {ref!r}") from exc
    if int(response.status_code) >= 400 or not response.parsed:
        raise ExchangePackError(f"credential Key lookup failed with status {response.status_code}")
    value = response.parsed.data.value
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ExchangePackError("credential Key must contain a JSON object") from exc
    if not isinstance(value, dict):
        raise ExchangePackError("credential Key must contain an object")
    return value


def validate_credentials(config: Mapping[str, Any]) -> dict[str, Any]:
    required = ("primary_smtp_address", "username", "password")
    result = dict(config)
    for field in required:
        if not isinstance(result.get(field), str) or not result[field]:
            raise ExchangePackError(f"Exchange credentials require {field}")
    result.setdefault("timezone", "UTC")
    result.setdefault("verify_ssl", True)
    if not isinstance(result["timezone"], str) or not result["timezone"]:
        raise ExchangePackError("timezone must be a non-empty string")
    if not isinstance(result["verify_ssl"], bool):
        raise ExchangePackError("verify_ssl must be a boolean")
    if result.get("server") is not None and not isinstance(result["server"], str):
        raise ExchangePackError("server must be a string")
    return result


def create_account(config: Mapping[str, Any]) -> tuple[Any, Any]:
    config = validate_credentials(config)
    try:
        from exchangelib import Account, Configuration, DELEGATE, Credentials, EWSTimeZone
        from exchangelib.protocol import BaseProtocol, NoVerifyHTTPAdapter
    except ImportError as exc:
        raise ExchangePackError("exchangelib is not installed") from exc

    if not config["verify_ssl"]:
        BaseProtocol.HTTP_ADAPTER_CLS = NoVerifyHTTPAdapter
    credentials = Credentials(username=config["username"], password=config["password"])
    server = config.get("server")
    kwargs: dict[str, Any] = {
        "primary_smtp_address": config["primary_smtp_address"],
        "access_type": DELEGATE,
    }
    if server:
        kwargs.update(config=Configuration(server=server, credentials=credentials), autodiscover=False)
    else:
        kwargs.update(credentials=credentials, autodiscover=True)
    try:
        return Account(**kwargs), EWSTimeZone(config["timezone"])
    except Exception as exc:
        raise ExchangePackError(f"unable to connect to Exchange ({type(exc).__name__})") from exc


def plain(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [plain(item) for item in value]
    formatter = getattr(value, "ewsformat", None)
    return formatter() if callable(formatter) else str(value)


def folder_to_dict(folder: Any) -> dict[str, Any]:
    return {
        "id": plain(getattr(folder, "folder_id", None)),
        "name": plain(getattr(folder, "name", None)),
        "class": plain(getattr(folder, "folder_class", None)),
        "total_count": getattr(folder, "total_count", None),
        "child_folder_count": getattr(folder, "child_folder_count", None),
        "unread_count": getattr(folder, "unread_count", None),
    }


def item_to_dict(item: Any, include_body: bool = False, folder_name: str | None = None) -> dict[str, Any]:
    result = {
        "id": plain(getattr(item, "item_id", None)),
        "change_key": plain(getattr(item, "changekey", None)),
        "subject": plain(getattr(item, "subject", None)),
        "sensitivity": plain(getattr(item, "sensitivity", None)),
        "attachments": len(getattr(item, "attachments", []) or []),
        "datetime_received": plain(getattr(item, "datetime_received", None)),
        "categories": plain(getattr(item, "categories", []) or []),
        "importance": plain(getattr(item, "importance", None)),
        "is_draft": getattr(item, "is_draft", None),
        "datetime_sent": plain(getattr(item, "datetime_sent", None)),
        "datetime_created": plain(getattr(item, "datetime_created", None)),
    }
    if include_body:
        result["body"] = plain(getattr(item, "body", None))
        result["text_body"] = plain(getattr(item, "text_body", None))
    if folder_name:
        result["folder_name"] = folder_name
    sender = getattr(item, "sender", None)
    if sender is not None:
        result["sender_email_address"] = plain(getattr(sender, "email_address", None))
        result["email_recipient_addresses"] = [
            plain(getattr(recipient, "email_address", recipient))
            for recipient in (getattr(item, "to_recipients", []) or [])
        ]
    return result


def parse_date(value: str, tz: Any) -> Any:
    try:
        from dateutil.parser import isoparse
        from exchangelib import EWSDateTime
        parsed = isoparse(value)
    except (ImportError, TypeError, ValueError) as exc:
        raise ExchangePackError("search_start_date must be a valid ISO 8601 date-time") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return EWSDateTime.from_datetime(parsed.astimezone(tz))


def search_items(account: Any, tz: Any, folder_name: str, subject: str | None, start: str | None) -> Any:
    folder = account.root.get_folder_by_name(folder_name)
    filters: dict[str, Any] = {}
    if subject:
        filters["subject__contains"] = subject
    if start:
        filters["datetime_received__range"] = (parse_date(start, tz), datetime.now(tz))
    try:
        return folder.filter(**filters) if filters else folder.all()
    except Exception:
        if start:
            filters.pop("datetime_received__range", None)
            filters["start__gte"] = parse_date(start, tz)
            return folder.filter(**filters)
        raise


def safe_attachment_path(name: Any, replacement: str | None, used: set[str]) -> Path:
    root_value = os.environ.get("ATTUNE_ARTIFACTS_DIR")
    if not root_value:
        raise ExchangePackError("ATTUNE_ARTIFACTS_DIR is required to save attachments")
    root = Path(root_value).resolve()
    root.mkdir(parents=True, exist_ok=True)
    filename = Path(str(name)).name
    if replacement:
        filename = filename.replace(" ", replacement)
    filename = re.sub(r"[\x00-\x1f]", "_", filename)
    if filename in {"", ".", ".."}:
        filename = "attachment"
    candidate = filename
    stem, suffix = Path(filename).stem, Path(filename).suffix
    count = 2
    while candidate in used or (root / candidate).exists():
        candidate = f"{stem}_{count}{suffix}"
        count += 1
    used.add(candidate)
    return root / candidate


def execute(operation: str, params: Mapping[str, Any], account: Any, tz: Any) -> Any:
    if operation == "get_folder":
        return folder_to_dict(account.root.get_folder_by_name(params["folder_name"]))
    if operation == "list_folders":
        root = params.get("root")
        folders = account.root.get_folder_by_name(root).children if root else account.root.get_folders()
        return [folder_to_dict(folder) for folder in folders]
    if operation == "search_items":
        items = search_items(account, tz, params.get("folder", "Inbox"), params.get("subject"), params.get("search_start_date"))
        return [item_to_dict(item, bool(params.get("include_body", True)), params.get("folder", "Inbox")) for item in items]
    if operation == "get_calendar_items":
        from exchangelib import EWSDateTime
        start = tz.localize(EWSDateTime(params["start_year"], params["start_month"], params["start_day"]))
        end = tz.localize(EWSDateTime(params["end_year"], params["end_month"], params["end_day"]))
        return [{"start": plain(item.start), "end": plain(item.end), "subject": plain(item.subject), "body": plain(item.body), "location": plain(item.location)} for item in account.calendar.filter(start__lt=end, end__gt=start)]
    if operation == "send_email":
        from exchangelib import Mailbox, Message
        message = Message(account=account, subject=params["subject"], body=params["body"], to_recipients=[Mailbox(email_address=value) for value in params["to_recipients"]])
        message.send_and_save() if params.get("store", True) else message.send()
        return {"sent": True, "stored": bool(params.get("store", True)), "recipient_count": len(params["to_recipients"])}
    if operation == "save_attachments":
        if bool(params.get("message_id")) != bool(params.get("change_key")):
            raise ExchangePackError("message_id and change_key must be supplied together")
        if params.get("message_id"):
            messages = account.fetch(ids=[(params["message_id"], params["change_key"])])
        else:
            messages = search_items(account, tz, params.get("folder", "Inbox"), params.get("subject"), params.get("search_start_date"))
        replacements = {"NONE": None, "UNDERSCORE": "_", "OCTOTHORPE/HASH": "#", "PIPE": "|"}
        replacement = replacements[params.get("replace_spaces_in_filename", "NONE")]
        binary = params.get("attachment_format", "BINARY") == "BINARY"
        used: set[str] = set()
        results = []
        for message in messages:
            files = []
            for attachment in getattr(message, "attachments", []) or []:
                content = getattr(attachment, "content", None)
                if content is None:
                    continue
                path = safe_attachment_path(getattr(attachment, "name", "attachment"), replacement, used)
                if binary:
                    path.write_bytes(content if isinstance(content, bytes) else str(content).encode())
                else:
                    path.write_text(content.decode() if isinstance(content, bytes) else str(content), encoding="utf-8")
                files.append(str(path))
            if files:
                results.append({"email_subject": plain(getattr(message, "subject", None)), "email_sent": plain(getattr(message, "datetime_sent", None)), "sender_email_address": plain(getattr(getattr(message, "sender", None), "email_address", None)), "attachment_files": files})
        return results
    raise ExchangePackError(f"unsupported operation {operation!r}")
