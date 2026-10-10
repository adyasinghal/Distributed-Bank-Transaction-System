"""Background thread that retries COMMIT for PENDING transfers."""

import threading
import logging

logger = logging.getLogger(__name__)


class GatewayRecovery:
    """Background thread that retries pending commits."""

    def __init__(self, coordinator, retry_interval: float = 2.0):
        self.coordinator = coordinator
        self.retry_interval = retry_interval
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the recovery background thread."""
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info("Gateway recovery thread started")

    def stop(self) -> None:
        """Signal the recovery thread to stop."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        """Periodically retry pending commits."""
        while not self._stop_event.is_set():
            self._stop_event.wait(self.retry_interval)
            if self._stop_event.is_set():
                break
            try:
                resolved = self.coordinator.retry_pending_commits()
                if resolved > 0:
                    logger.info(f"Gateway recovery resolved {resolved} pending transaction(s)")
            except Exception as e:
                logger.error(f"Gateway recovery error: {e}")
