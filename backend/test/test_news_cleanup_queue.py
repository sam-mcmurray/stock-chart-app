from threading import Event

from services import news_cleanup_queue


class FakePipeline:
    def __init__(self, client):
        self.client = client
        self.calls = []

    def __getattr__(self, name):
        def queue(*args):
            self.calls.append((name, args))
            return self
        return queue

    def execute(self):
        for name, args in self.calls:
            getattr(self.client, name)(*args)


class FakeRedis:
    def __init__(self):
        self.lists = {}
        self.sets = {}

    def sadd(self, key, value):
        values = self.sets.setdefault(key, set())
        if value in values:
            return 0
        values.add(value)
        return 1

    def srem(self, key, value):
        self.sets.setdefault(key, set()).discard(value)

    def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)

    def rpush(self, key, value):
        self.lists.setdefault(key, []).append(value)

    def rpoplpush(self, source, destination):
        values = self.lists.get(source, [])
        if not values:
            return None
        value = values.pop()
        self.lists.setdefault(destination, []).insert(0, value)
        return value

    def lrem(self, key, count, value):
        values = self.lists.get(key, [])
        if value in values:
            values.remove(value)

    def pipeline(self):
        return FakePipeline(self)


def test_cleanup_queue_deduplicates_and_acknowledges():
    client = FakeRedis()
    assert news_cleanup_queue.enqueue(["one", "one", "two"], client=client) == 2
    deleted = []
    stop = Event()

    def delete_batch(ids):
        deleted.extend(ids)
        stop.set()

    news_cleanup_queue.consume_forever(delete_batch, stop, client=client)

    assert deleted == ["one", "two"]
    assert client.lists[news_cleanup_queue.PROCESSING_KEY] == []
    assert client.sets[news_cleanup_queue.QUEUED_KEY] == set()
