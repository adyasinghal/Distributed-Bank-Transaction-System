"""Ports, server map and timeouts. Every value can be overridden with a BANK_* environment variable."""

import os
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("BANK_DATA_DIR", str(PROJECT_ROOT / "data")))
LOG_DIR = Path(os.getenv("BANK_LOG_DIR", str(PROJECT_ROOT / "logs")))
# Tests and their outputs live in folders next to the source code
TEST_CASES_DIR = PROJECT_ROOT.parent / "test-cases"
TEST_RESULTS_DIR = Path(os.getenv("BANK_TEST_RESULTS_DIR", str(PROJECT_ROOT.parent / "results")))
PERF_RESULTS_DIR = Path(os.getenv("BANK_PERF_RESULTS_DIR", str(PROJECT_ROOT.parent / "performance-results")))

BASE_PORT = int(os.getenv("BANK_BASE_PORT", "8000"))
GATEWAY_PORT = BASE_PORT                        # 8000
SERVER_PORTS = {
    1: BASE_PORT + 1,                           # 8001
    2: BASE_PORT + 2,                           # 8002
    3: BASE_PORT + 3,                           # 8003
}
GATEWAY_HOST = os.getenv("BANK_GATEWAY_HOST", "127.0.0.1")
SERVER_HOST = os.getenv("BANK_SERVER_HOST", "127.0.0.1")

SERVERS: dict[int, str] = {
    sid: f"http://{SERVER_HOST}:{port}"
    for sid, port in SERVER_PORTS.items()
}

SEED_ACCOUNTS: list[dict] = [
    {"account_id": "A101", "owner": "Ram",   "balance": 10000},
    {"account_id": "A102", "owner": "Sita",  "balance": 15000},
    {"account_id": "A201", "owner": "Mohan", "balance": 20000},
    {"account_id": "A202", "owner": "Priya", "balance": 25000},
    {"account_id": "A301", "owner": "Arjun", "balance": 30000},
    {"account_id": "A302", "owner": "Lakshmi", "balance": 35000},
]

CONNECT_TIMEOUT = float(os.getenv("BANK_CONNECT_TIMEOUT", "1.0"))
READ_TIMEOUT = float(os.getenv("BANK_READ_TIMEOUT", "3.0"))
RECOVERY_RETRY_INTERVAL = float(os.getenv("BANK_RECOVERY_INTERVAL", "2.0"))
# A PREPARED row older than this is treated as in-doubt and resolved by asking
# the coordinator (covers lost ABORT/COMMIT messages while the server stays up).
IN_DOUBT_TIMEOUT = float(os.getenv("BANK_IN_DOUBT_TIMEOUT", "10.0"))

THREAD_POOL_SIZE = int(os.getenv("BANK_THREAD_POOL_SIZE", "100"))

ACCOUNT_ID_PATTERN = re.compile(r"A[1-9]\d{2}")

def server_for(account_id: str) -> int | None:
    """Server that owns the account: "A101" -> 1, "A201" -> 2, "A999" -> None."""
    if not ACCOUNT_ID_PATTERN.fullmatch(account_id):
        return None
    sid = int(account_id[1])
    return sid if sid in SERVERS else None


def server_url(server_id: int) -> str:
    """Return the base URL for the given server ID."""
    return SERVERS[server_id]


def ensure_dirs() -> None:
    """Create data and log directories if they don't exist."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
