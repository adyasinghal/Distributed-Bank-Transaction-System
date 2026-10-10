"""Fault injection: crash (os._exit) or delay at named points in the server code."""

import os
import time
import threading
import logging

logger = logging.getLogger(__name__)


class FaultInjector:
    """Manages named fault injection points."""

    def __init__(self):
        self._faults: dict[str, dict] = {}
        self._lock = threading.Lock()

    def arm(self, point: str, action: str, delay_ms: int = 0, once: bool = True) -> None:
        """Arm a fault at the given hook point."""
        with self._lock:
            self._faults[point] = {
                "action": action,
                "delay_ms": delay_ms,
                "once": once,
            }
        logger.warning(f"FAULT ARMED: point={point}, action={action}, delay_ms={delay_ms}, once={once}")

    def check(self, point: str) -> None:
        """Check if a fault is armed at this point and trigger it."""
        with self._lock:
            fault = self._faults.get(point)
            if fault is None:
                return
            if fault["once"]:
                del self._faults[point]
            action = fault["action"]
            delay_ms = fault["delay_ms"]

        if action == "crash":
            logger.critical(f"FAULT TRIGGERED: CRASH at {point}")
            os._exit(1)
        elif action == "delay":
            logger.warning(f"FAULT TRIGGERED: DELAY {delay_ms}ms at {point}")
            time.sleep(delay_ms / 1000.0)

    def clear_all(self) -> None:
        """Remove all armed faults."""
        with self._lock:
            self._faults.clear()
