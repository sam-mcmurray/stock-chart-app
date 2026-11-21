import logging
from threading import Event
from typing import Callable


logger = logging.getLogger(__name__)


def run_resilient_consumer(
    consumer: Callable,
    handler: Callable,
    stop_event: Event,
    name: str,
    retry_delay_seconds: float = 5.0,
) -> None:
    """Restart a durable queue consumer after an unexpected return or error."""
    while not stop_event.is_set():
        try:
            consumer(handler, stop_event)
        except Exception:
            logger.exception("%s consumer exited unexpectedly; restarting", name)
        else:
            if stop_event.is_set():
                return
            logger.error("%s consumer returned unexpectedly; restarting", name)
        stop_event.wait(max(0.1, retry_delay_seconds))
