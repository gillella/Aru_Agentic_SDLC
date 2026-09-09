"""Generate a fresh nonsecret pilot example; runtime JSON stays outside Git."""
from __future__ import annotations

import argparse
import copy
import json

from .private import absolute, write_new

# These are explicit pilot defaults, not discovered host configuration.
_DEFAULTS = {
    "schema": "chopin-pilot/v1",
    "app_name": "Chopin Catalog Pilot gillella",
    "app_owner": "gillella",
    "app_browser_settings_verified": False,
    "public_origin": "https://aravinds-mac-mini-1.tail3df1c1.ts.net:8443",
    "setup_origin": "http://100.95.239.18:8766",
    "setup_ttl_seconds": 600,
    "credentials_dir": "/Users/gillella/.hermes/credentials/chopin-catalog-pilot",
    "state_dir": "/Users/gillella/.hermes/workspace/chopin-pilot/runtime",
    "receipts_dir": "/Users/gillella/.hermes/workspace/chopin-pilot/receipts",
    "source_dir": "/Users/gillella/services/chopin",
    "repository": "gillella/unum-catalog",
    "base_branch": "main",
    "allowed_users": [
        "gillella"
    ],
    "telegram": {
        "platform": "telegram",
        "chat_id": "-5325492504",
        "user_id": "6431233670"
    },
    "mcp_bearer": {
        "file": "/Users/gillella/.hermes/credentials/chopin-mcp/bearer"
    },
    "github_app_wrapper": "/Users/gillella/.local/bin/aru-code-factory-app-run",
    "database": {
        "host": "127.0.0.1",
        "port": 5432,
        "name": "chopin_catalog_pilot",
        "role": "chopin_catalog_pilot",
        "admin_user": "gillella"
    },
    "psql": "/opt/homebrew/opt/postgresql@17/bin/psql",
    "pg_dump": "/opt/homebrew/opt/postgresql@17/bin/pg_dump"
}



def example_config() -> dict:
    return copy.deepcopy(_DEFAULTS)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, help='New absolute private config path; never overwrite')
    args = parser.parse_args()
    write_new(absolute(args.output), (json.dumps(example_config(), indent=2) + '\n').encode())


if __name__ == '__main__':
    main()
