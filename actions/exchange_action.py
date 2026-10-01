#!/usr/bin/env python3
"""Shared stdin JSON entry point for Exchange actions."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import attune  # noqa: E402
from lib.exchange import create_account, execute, fetch_key  # noqa: E402


def main(**params):
    operation = params.pop("operation")
    credentials = fetch_key(params.pop("credential_key", "pack.msexchange.credentials"))
    account, timezone = create_account(credentials)
    return {"operation": operation, "result": execute(operation, params, account, timezone)}


if __name__ == "__main__":
    attune.run_action(main)
