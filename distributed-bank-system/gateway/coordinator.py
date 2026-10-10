"""2PC coordinator for transfers between two servers.

Phase 1 prepares the debit on the source, then the credit on the destination.
The decision is written to the log before phase 2 starts, so it survives a crash.
Any error in phase 1 (NO vote, timeout, dropped connection) aborts the transfer.
SUCCESS is only returned when both servers have acknowledged COMMIT; otherwise the
transfer stays PENDING and the recovery thread keeps retrying.
"""

import logging
import threading
import uuid
from datetime import datetime

import httpx

import config
from common.errors import source_not_found_msg, dest_not_found_msg
from gateway.router import route
from gateway.txn_log import TxnLog

logger = logging.getLogger(__name__)


def generate_txn_id() -> str:
    """Generate a transaction ID: T-YYYYMMDD-<6 hex>."""
    date_str = datetime.now().strftime("%Y%m%d")
    hex_part = uuid.uuid4().hex[:6]
    return f"T-{date_str}-{hex_part}"


class Coordinator:
    """2PC coordinator for cross-server transfers."""

    def __init__(self, txn_log: TxnLog, http_client: httpx.Client):
        self.txn_log = txn_log
        self.client = http_client
        self._timeout = httpx.Timeout(config.CONNECT_TIMEOUT, read=config.READ_TIMEOUT)
        # Transfers currently being driven by a request thread; the recovery
        # thread leaves these alone so it does not race the live coordinator.
        self._in_flight: set[str] = set()
        self._in_flight_lock = threading.Lock()

    def transfer(self, txn_id: str, source: str, destination: str, amount: int) -> dict:
        """Execute a cross-server transfer using 2PC."""
        src_sid, src_url = route(source)
        dst_sid, dst_url = route(destination)

        # Route validation (should be done upstream, but double-check)
        if src_url is None:
            return {"status": "FAILED", "message": source_not_found_msg()}
        if dst_url is None:
            return {"status": "FAILED", "message": dest_not_found_msg()}

        with self._in_flight_lock:
            self._in_flight.add(txn_id)
        try:
            return self._run_2pc(txn_id, source, destination, amount,
                                 src_sid, src_url, dst_sid, dst_url)
        finally:
            with self._in_flight_lock:
                self._in_flight.discard(txn_id)

    def _run_2pc(self, txn_id: str, source: str, destination: str, amount: int,
                 src_sid: int, src_url: str, dst_sid: int, dst_url: str) -> dict:
        # Log as PENDING (no decision yet)
        self.txn_log.log(
            txn_id=txn_id, txn_type="TRANSFER", amount=amount,
            status="PENDING", source=source, destination=destination,
            participants=f"{src_sid},{dst_sid}",
        )

        # Phase 1. Source first, so a transfer without funds never touches the destination.

        contacted = [("source", src_url)]
        ok, abort_reason = self._prepare(txn_id, src_url, source, "debit", amount)
        if not ok:
            if "not found" in abort_reason.lower():
                abort_reason = source_not_found_msg()
            elif abort_reason == "Server unavailable":
                abort_reason = f"Source server unavailable (Server {src_sid})"
            elif abort_reason == "Timed out":
                abort_reason = f"Source server (Server {src_sid}) did not respond in time"
        else:
            contacted.append(("dest", dst_url))
            ok, abort_reason = self._prepare(txn_id, dst_url, destination, "credit", amount)
            if not ok:
                if "not found" in abort_reason.lower():
                    abort_reason = dest_not_found_msg()
                elif abort_reason == "Server unavailable":
                    abort_reason = f"Destination server unavailable (Server {dst_sid})"
                elif abort_reason == "Timed out":
                    abort_reason = f"Destination server (Server {dst_sid}) did not respond in time"

        # Decision

        if ok:
            # Durably log COMMIT decision BEFORE phase 2
            self.txn_log.update_status(txn_id, "PENDING", decision="COMMIT")
            return self._phase2_commit(txn_id, source, destination, amount, src_url, dst_url, src_sid, dst_sid)

        # Durably log ABORT decision, then release everything that may be prepared
        self.txn_log.update_status(txn_id, "FAILED", failure_reason=abort_reason, decision="ABORT")
        self._phase2_abort(txn_id, contacted)
        return {
            "status": "FAILED",
            "message": abort_reason,
            "data": None,
        }

    def _prepare(self, txn_id: str, url: str, account_id: str, op: str, amount: int) -> tuple[bool, str]:
        """Send PREPARE to one participant. Returns (voted_commit, reason)."""
        try:
            resp = self.client.post(
                f"{url}/2pc/prepare",
                json={"txn_id": txn_id, "account_id": account_id, "op": op, "amount": amount},
                timeout=self._timeout,
            )
        except httpx.TimeoutException as e:
            logger.warning(f"2PC PREPARE {op} timed out for {txn_id}: {e!r}")
            return False, "Timed out"
        except httpx.TransportError as e:
            # Refused, or the server died mid-request: a NO vote
            logger.warning(f"2PC PREPARE {op} failed for {txn_id}: {e!r}")
            return False, "Server unavailable"
        if resp.status_code != 200:
            return False, f"Participant error: HTTP {resp.status_code}"
        vote = resp.json()
        if vote.get("vote") == "VOTE_COMMIT":
            return True, ""
        return False, vote.get("reason") or "Participant voted ABORT"

    def _phase2_commit(self, txn_id: str, source: str, dest: str, amount: int,
                        src_url: str, dst_url: str, src_sid: int, dst_sid: int) -> dict:
        """Send COMMIT to both participants."""
        src_ok = self._send_commit(txn_id, src_url, src_sid)
        dst_ok = self._send_commit(txn_id, dst_url, dst_sid)

        if src_ok and dst_ok:
            self.txn_log.update_status(txn_id, "SUCCESS")

            # Fetch final balances
            balances = {}
            for acct, url in ((source, src_url), (dest, dst_url)):
                try:
                    r = self.client.get(f"{url}/accounts/{acct}", timeout=2.0)
                    if r.status_code == 200:
                        balances[acct] = r.json()["data"]["balance"]
                except httpx.TransportError:
                    pass

            return {
                "status": "SUCCESS",
                "message": f"Transfer of ₹{amount} from {source} to {dest} completed",
                "data": {"balances": balances},
            }

        # At least one commit didn't go through — stay PENDING
        # Gateway recovery will retry
        pending_msg = "Transfer committed"
        if not src_ok:
            pending_msg += f"; debit on Server {src_sid} pending (will be applied when it recovers)"
        if not dst_ok:
            pending_msg += f"; credit on Server {dst_sid} pending (will be applied when it recovers)"
        return {
            "status": "PENDING",
            "message": pending_msg,
            "data": None,
        }

    def _send_commit(self, txn_id: str, server_url: str, server_id: int) -> bool:
        """Send COMMIT to a single participant. Returns True if acknowledged."""
        try:
            resp = self.client.post(
                f"{server_url}/2pc/commit",
                json={"txn_id": txn_id},
                timeout=self._timeout,
            )
            return resp.status_code == 200
        except httpx.TransportError as e:
            logger.warning(f"2PC COMMIT to server {server_id} failed for {txn_id}: {e!r}")
            return False

    def _phase2_abort(self, txn_id: str, participants: list[tuple[str, str]]) -> None:
        """Send ABORT to every server that got a PREPARE, even if its vote never arrived.
        If one is unreachable, its own recovery will ask for the decision later.
        """
        for role, url in participants:
            try:
                self.client.post(
                    f"{url}/2pc/abort",
                    json={"txn_id": txn_id},
                    timeout=self._timeout,
                )
            except httpx.TransportError as e:
                logger.warning(f"2PC ABORT to {role} failed for {txn_id}: {e!r}")

    def retry_pending_commits(self) -> int:
        """Retry COMMIT for all PENDING transactions with decision=COMMIT."""
        pending = self.txn_log.get_pending_commits()
        resolved = 0
        for txn in pending:
            txn_id = txn["txn_id"]
            with self._in_flight_lock:
                if txn_id in self._in_flight:
                    continue
            participants = txn.get("participants", "")
            if not participants:
                continue

            parts = participants.split(",")
            if len(parts) != 2:
                continue

            src_sid, dst_sid = int(parts[0]), int(parts[1])
            src_url = config.server_url(src_sid)
            dst_url = config.server_url(dst_sid)

            src_ok = self._send_commit(txn_id, src_url, src_sid)
            dst_ok = self._send_commit(txn_id, dst_url, dst_sid)

            if src_ok and dst_ok:
                self.txn_log.update_status(txn_id, "SUCCESS")
                logger.info(f"Recovery: resolved PENDING txn {txn_id} → SUCCESS")
                resolved += 1

        return resolved
