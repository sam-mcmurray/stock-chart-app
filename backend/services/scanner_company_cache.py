"""Bounded background refresh for scanner company metadata."""
import logging
import queue
import threading
import time


class CompanyCache:
    def __init__(self, loader, ttl, capacity=80):
        self.loader = loader
        self.ttl = ttl
        self.queue = queue.Queue(maxsize=capacity)
        self.lock = threading.Lock()
        self.records = {}
        self.pending = set()
        self.thread = None
        self.active = None
        self.started = None
        self.completed = 0
        self.failed = 0

    def get(self, symbol, *, force_refresh=False):
        now = time.time()
        with self.lock:
            cached = self.records.get(symbol)
            fresh = not force_refresh and cached is not None and 0 <= now - cached[0] < self.ttl
            if not fresh and symbol not in self.pending:
                try:
                    self.queue.put_nowait(symbol)
                    self.pending.add(symbol)
                except queue.Full:
                    pass  # A later scan retries; never block or grow an unbounded backlog.
            if self.pending and (self.thread is None or not self.thread.is_alive()):
                self.thread = threading.Thread(target=self._run, daemon=True,
                                               name="scanner-company-refresh")
                self.thread.start()
            status = "fresh" if fresh else "pending" if symbol in self.pending else "deferred"
            return (dict(cached[1]) if cached else {}), status

    def _run(self):
        while True:
            symbol = self.queue.get()
            with self.lock:
                self.active, self.started = symbol, time.time()
            try:
                result = self.loader(symbol)
                with self.lock:
                    self.records[symbol] = (time.time(), dict(result))
                    # Bound retained metadata as well as outstanding work.
                    if len(self.records) > 2000:
                        oldest = min(self.records, key=lambda key: self.records[key][0])
                        del self.records[oldest]
                    self.completed += 1
            except Exception:
                logging.getLogger(__name__).exception("Company refresh failed for %s", symbol)
                with self.lock:
                    self.failed += 1
            finally:
                with self.lock:
                    self.pending.discard(symbol)
                    self.active = self.started = None
                self.queue.task_done()

    def status(self):
        with self.lock:
            return {"pending": len(self.pending), "active_symbol": self.active,
                    "active_age_seconds": round(time.time() - self.started, 1) if self.started else None,
                    "completed": self.completed, "failed": self.failed}
