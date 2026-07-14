import asyncio


class FakeAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return FakeAcquire(self.conn)


class FakeConn:
    def __init__(self, lock_acquired=True):
        self.lock_acquired = lock_acquired
        self.queries = []

    async def fetchval(self, query, *args):
        self.queries.append(query)
        if "pg_try_advisory_lock" in query:
            return self.lock_acquired
        if "pg_advisory_unlock" in query:
            return True
        return 3

    async def fetch(self, query, *args):
        self.queries.append(query)
        return []


def test_stale_processing_reconcile_skips_when_advisory_lock_busy(monkeypatch):
    from video_review import db

    conn = FakeConn(lock_acquired=False)

    async def fake_get_pool():
        return FakePool(conn)

    monkeypatch.setattr(db, "get_pool", fake_get_pool)

    rows = asyncio.run(db.mark_stale_processing_jobs_failed())

    assert rows == []
    assert len(conn.queries) == 1
    assert "pg_try_advisory_lock" in conn.queries[0]


def test_stale_processing_reconcile_unlocks_after_update(monkeypatch):
    from video_review import db

    conn = FakeConn(lock_acquired=True)

    async def fake_get_pool():
        return FakePool(conn)

    monkeypatch.setattr(db, "get_pool", fake_get_pool)

    rows = asyncio.run(db.mark_stale_processing_jobs_failed())

    assert rows == []
    assert "pg_try_advisory_lock" in conn.queries[0]
    assert "WITH stale AS" in conn.queries[1]
    assert "pg_advisory_unlock" in conn.queries[2]
