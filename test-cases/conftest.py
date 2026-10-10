"""Test cluster on ports 9000-9003, started once per session and reset before every test."""

import json
import os
import subprocess
import sys
import time
import shutil
import tempfile
from pathlib import Path

import httpx
import pytest

# Use test ports to avoid conflicts with a running system
TEST_BASE_PORT = 9000
TEST_GATEWAY_PORT = TEST_BASE_PORT
TEST_SERVER_PORTS = {1: TEST_BASE_PORT + 1, 2: TEST_BASE_PORT + 2, 3: TEST_BASE_PORT + 3}
TEST_GATEWAY_URL = f"http://127.0.0.1:{TEST_GATEWAY_PORT}"
TEST_SERVER_URLS = {sid: f"http://127.0.0.1:{port}" for sid, port in TEST_SERVER_PORTS.items()}

# Short in-doubt timeout so recovery tests finish quickly
TEST_IN_DOUBT_TIMEOUT = 3.0
TEST_RECOVERY_INTERVAL = 1.0

PROJECT_ROOT = Path(__file__).resolve().parent.parent / "distributed-bank-system"
sys.path.insert(0, str(PROJECT_ROOT))


class TestCluster:
    """Manages a test cluster of bank servers and a gateway."""

    __test__ = False  # not a test class, despite the name

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.proc_log_dir = data_dir / "proc-logs"
        self.proc_log_dir.mkdir(parents=True, exist_ok=True)
        self.procs: dict[str, subprocess.Popen] = {}
        self.gateway_url = TEST_GATEWAY_URL
        self.server_urls = TEST_SERVER_URLS
        self.http = httpx.Client(timeout=15.0)

    def _env(self) -> dict:
        env = os.environ.copy()
        env["BANK_BASE_PORT"] = str(TEST_BASE_PORT)
        env["BANK_DATA_DIR"] = str(self.data_dir)
        env["BANK_LOG_DIR"] = str(self.data_dir / "logs")
        env["BANK_IN_DOUBT_TIMEOUT"] = str(TEST_IN_DOUBT_TIMEOUT)
        env["BANK_RECOVERY_INTERVAL"] = str(TEST_RECOVERY_INTERVAL)
        return env

    def _spawn(self, name: str, args: list[str]) -> subprocess.Popen:
        # Output goes to a file: an unread PIPE fills up and freezes the child.
        log = open(self.proc_log_dir / f"{name}.log", "a")
        proc = subprocess.Popen(
            [sys.executable, "-m", *args],
            cwd=str(PROJECT_ROOT),
            env=self._env(),
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        self.procs[name] = proc
        return proc

    def _spawn_server(self, sid: int) -> None:
        self._spawn(f"server_{sid}", ["server.bank_server", "--id", str(sid),
                                      "--port", str(TEST_SERVER_PORTS[sid]),
                                      "--data-dir", str(self.data_dir)])

    def _spawn_gateway(self) -> None:
        self._spawn("gateway", ["gateway.gateway", "--port", str(TEST_GATEWAY_PORT),
                                "--data-dir", str(self.data_dir)])

    def start_all(self):
        """Start all servers and the gateway."""
        for sid in TEST_SERVER_PORTS:
            self._spawn_server(sid)
        self._spawn_gateway()

        for sid, url in self.server_urls.items():
            self._wait_health(f"{url}/health")
        self._wait_health(f"{self.gateway_url}/health")

    def _wait_health(self, url: str, timeout: float = 10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                resp = self.http.get(url, timeout=1.0)
                if resp.status_code == 200:
                    return
            except Exception:
                pass
            time.sleep(0.2)
        raise RuntimeError(f"Health check failed: {url}")

    def stop_all(self):
        """Stop all processes."""
        for name, proc in self.procs.items():
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
        self.procs.clear()
        self.http.close()

    def ensure_all_up(self):
        """Restart any process a previous (possibly failed) test left dead."""
        for sid in TEST_SERVER_PORTS:
            proc = self.procs.get(f"server_{sid}")
            if proc is None or proc.poll() is not None:
                self.start(sid)
        proc = self.procs.get("gateway")
        if proc is None or proc.poll() is not None:
            self.start_gateway()

    def kill(self, server_id: int):
        """Kill a specific server with SIGKILL (simulates a crash)."""
        proc = self.procs.pop(f"server_{server_id}", None)
        if proc:
            proc.kill()
            proc.wait()

    def stop(self, server_id: int):
        """Stop a specific server gracefully (SIGTERM)."""
        proc = self.procs.pop(f"server_{server_id}", None)
        if proc:
            proc.terminate()
            proc.wait(timeout=10)

    def start(self, server_id: int):
        """(Re)start a specific server."""
        self._spawn_server(server_id)
        self._wait_health(f"{self.server_urls[server_id]}/health")

    def kill_gateway(self):
        proc = self.procs.pop("gateway", None)
        if proc:
            proc.kill()
            proc.wait()

    def start_gateway(self):
        self._spawn_gateway()
        self._wait_health(f"{self.gateway_url}/health")

    def wait_dead(self, server_id: int, timeout: float = 5.0) -> bool:
        """Wait until a server process has exited (e.g. after a crash fault)."""
        proc = self.procs.get(f"server_{server_id}")
        if proc is None:
            return True
        try:
            proc.wait(timeout=timeout)
            return True
        except subprocess.TimeoutExpired:
            return False

    def arm_fault(self, server_id: int, point: str, action: str = "crash",
                  delay_ms: int = 0, once: bool = True):
        """Arm a fault on a server."""
        url = self.server_urls[server_id]
        resp = self.http.post(f"{url}/admin/fault", json={
            "point": point, "action": action, "delay_ms": delay_ms, "once": once,
        })
        assert resp.status_code == 200, resp.text

    def reset(self, seed: bool = True):
        """Reset all state via the gateway."""
        resp = self.http.post(f"{self.gateway_url}/admin/reset", json={"seed": seed}, timeout=5.0)
        assert resp.status_code == 200 and not resp.json()["errors"], resp.text

    # Convenience methods for tests
    def create(self, account_id: str, owner: str, balance: int) -> httpx.Response:
        return self.http.post(f"{self.gateway_url}/accounts", json={
            "account_id": account_id, "owner": owner, "initial_balance": balance,
        })

    def deposit(self, account_id: str, amount: int) -> httpx.Response:
        return self.http.post(f"{self.gateway_url}/accounts/{account_id}/deposit",
                              json={"amount": amount})

    def withdraw(self, account_id: str, amount: int) -> httpx.Response:
        return self.http.post(f"{self.gateway_url}/accounts/{account_id}/withdraw",
                              json={"amount": amount})

    def balance(self, account_id: str) -> int:
        resp = self.http.get(f"{self.gateway_url}/accounts/{account_id}/balance")
        return resp.json()["data"]["balance"]

    def transfer(self, source: str, dest: str, amount: int) -> httpx.Response:
        return self.http.post(f"{self.gateway_url}/transfers", json={
            "source": source, "destination": dest, "amount": amount,
        })

    def get_held(self, account_id: str) -> int:
        """Get the held amount for an account by querying the server directly."""
        from config import server_for
        sid = server_for(account_id)
        url = self.server_urls[sid]
        resp = self.http.get(f"{url}/accounts/{account_id}")
        return resp.json()["data"]["held"]

    def prepared(self, server_id: int) -> list[dict]:
        """In-doubt (PREPARED) 2PC rows on a server."""
        resp = self.http.get(f"{self.server_urls[server_id]}/admin/prepared")
        return resp.json()["prepared"]

    def txn(self, txn_id: str) -> dict:
        """One record from the global transaction log."""
        return self.http.get(f"{self.gateway_url}/transactions/{txn_id}").json()["data"]

    def txn_log(self, limit: int = 100) -> list[dict]:
        resp = self.http.get(f"{self.gateway_url}/transactions", params={"limit": limit})
        return resp.json()["data"]["transactions"]

    def wait_until(self, predicate, timeout: float = 15.0, interval: float = 0.25) -> bool:
        """Poll predicate() until it is truthy or the timeout expires."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if predicate():
                    return True
            except httpx.TransportError:
                pass
            time.sleep(interval)
        return False


_observations: list[dict] = []


def record(tc_id: str, input_desc: str, expected: str, actual: str, passed: bool,
           log: list[dict] | None = None):
    """Record a test observation (and optionally the related log records) for the report."""
    _observations.append({
        "tc_id": tc_id,
        "input": input_desc,
        "expected": expected,
        "actual": actual,
        "passed": passed,
        "log": log or [],
    })


@pytest.fixture(scope="session")
def cluster():
    """Session-scoped fixture: launches and tears down the test cluster."""
    data_dir = Path(tempfile.mkdtemp(prefix="bank_test_"))
    c = TestCluster(data_dir)
    c.start_all()
    yield c
    c.stop_all()
    shutil.rmtree(data_dir, ignore_errors=True)


@pytest.fixture(autouse=True)
def fresh(cluster):
    """Reset all state before each test."""
    cluster.ensure_all_up()
    cluster.reset(seed=True)
    yield


@pytest.fixture
def obs():
    """Provides the record function for test observations."""
    return record


# Save observations at session end
def pytest_sessionfinish(session, exitstatus):
    if not _observations:
        return
    import config
    config.ensure_dirs()
    obs_path = config.DATA_DIR / "test_observations.json"
    with open(obs_path, "w") as f:
        json.dump(_observations, f, indent=2, ensure_ascii=False)
