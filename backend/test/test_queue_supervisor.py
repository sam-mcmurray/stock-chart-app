from threading import Event

from services.queue_supervisor import run_resilient_consumer


def test_supervisor_restarts_consumer_after_unexpected_error():
    stop = Event()
    calls = []

    def consumer(handler, stop_event):
        calls.append(handler)
        if len(calls) == 1:
            raise ConnectionError("temporary Redis failure")
        stop_event.set()

    handler = object()
    run_resilient_consumer(
        consumer,
        handler,
        stop,
        "test queue",
        retry_delay_seconds=0.001,
    )

    assert calls == [handler, handler]
