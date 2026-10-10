"""Resolves in-doubt (PREPARED) transactions by asking the coordinator for its decision.
Runs once at startup, then periodically for rows older than IN_DOUBT_TIMEOUT.
"""

import threading
import logging

import httpx

logger = logging.getLogger(__name__)


class ServerRecovery:
    """Resolves in-doubt (PREPARED) transactions at startup and periodically."""

    def __init__(self, store, gateway_url: str, retry_interval: float = 2.0,
                 in_doubt_timeout: float = 10.0):
        self.store = store
        self.gateway_url = gateway_url
        self.retry_interval = retry_interval
        self.in_doubt_timeout = in_doubt_timeout
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the recovery process in a background thread."""
        prepared = self.store.get_prepared_txns()
        if prepared:
            logger.info(f"Found {len(prepared)} in-doubt transactions. Starting recovery...")
        else:
            logger.info("No in-doubt transactions to recover.")
        self._thread = threading.Thread(target=self._recover_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Signal the recovery thread to stop."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _recover_loop(self) -> None:
        """Resolve all PREPARED rows once, then keep resolving stale ones."""
        min_age = 0.0
        while not self._stop_event.is_set():
            pending = self.store.get_prepared_txns(min_age_s=min_age)
            unresolved = 0
            for txn in pending:
                if self._stop_event.is_set():
                    break
                if not self._resolve_one(txn):
                    unresolved += 1
            if unresolved:
                logger.info(f"{unresolved} in-doubt txns still unresolved. Retrying in {self.retry_interval}s...")
            min_age = self.in_doubt_timeout
            self._stop_event.wait(self.retry_interval)

    def _resolve_one(self, txn: dict) -> bool:
        """Ask the gateway for the decision and apply it. Returns False to retry later
        (gateway down, or still deciding: never abort on a guess).
        """
        txn_id = txn["txn_id"]
        try:
            resp = httpx.get(
                f"{self.gateway_url}/2pc/decision/{txn_id}",
                timeout=3.0,
            )
        except httpx.TransportError as e:
            logger.warning(f"Recovery: gateway unreachable for {txn_id}: {e}")
            return False

        if resp.status_code == 404:
            # Not found → presumed abort
            logger.info(f"Recovery: ABORT {txn_id} (not found at gateway, presumed abort)")
            self.store.abort_txn(txn_id)
            return True
        if resp.status_code != 200:
            logger.warning(f"Recovery: unexpected status {resp.status_code} for {txn_id}")
            return False

        decision = resp.json().get("decision")
        if decision == "COMMIT":
            logger.info(f"Recovery: COMMIT {txn_id}")
            self.store.commit_txn(txn_id)
            return True
        if decision == "ABORT":
            logger.info(f"Recovery: ABORT {txn_id}")
            self.store.abort_txn(txn_id)
            return True
        logger.info(f"Recovery: {txn_id} not decided yet by coordinator, waiting")
        return False
