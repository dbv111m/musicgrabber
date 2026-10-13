"""Connection pool checkout behaviour, mostly guarding against the cold-start stall."""

import os
import queue
import sqlite3
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db


def _fresh_pool(monkeypatch, tmp_path, size=8):
    """Point the pool at a throwaway database and empty it, as at process start."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "pool.db")
    monkeypatch.setattr(db, "_DB_POOL_SIZE", size)
    monkeypatch.setattr(db, "_db_pool", queue.LifoQueue(maxsize=size))
    monkeypatch.setattr(db, "_db_pool_created", 0)
    monkeypatch.setattr(db, "_db_pool_lock", threading.Lock())


def test_first_connection_on_a_cold_pool_is_immediate(monkeypatch, tmp_path):
    """The original sin: an empty pool made the first query wait out a 15s timeout."""
    _fresh_pool(monkeypatch, tmp_path)
    start = time.perf_counter()
    with db.db_conn() as conn:
        conn.execute("SELECT 1")
    assert time.perf_counter() - start < 1.0


def test_failed_connection_creation_does_not_consume_pool_capacity(monkeypatch, tmp_path):
    _fresh_pool(monkeypatch, tmp_path, size=2)
    real_get_db = db.get_db

    def fail_open():
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(db, "get_db", fail_open)
    for _ in range(3):
        with pytest.raises(sqlite3.OperationalError):
            db._get_pooled_conn()
        assert db._db_pool_created == 0

    # When storage returns, there must be no phantom borrowers to wait for.
    monkeypatch.setattr(db, "get_db", real_get_db)
    with db.db_conn() as conn:
        assert conn.execute("SELECT 1").fetchone()[0] == 1
    assert db._db_pool_created == 1


def test_pool_grows_to_its_ceiling_without_waiting(monkeypatch, tmp_path):
    """Every connection up to the ceiling is created on demand, none of them blocking."""
    _fresh_pool(monkeypatch, tmp_path, size=4)
    start = time.perf_counter()
    held = [db._get_pooled_conn() for _ in range(4)]
    elapsed = time.perf_counter() - start
    assert len({id(c) for c in held}) == 4
    assert elapsed < 1.0
    assert db._db_pool_created == 4
    for conn in held:
        db._return_pooled_conn(conn)


def test_returned_connections_are_reused_rather_than_multiplying(monkeypatch, tmp_path):
    """Checkout/return should not keep minting connections; that is the whole point."""
    _fresh_pool(monkeypatch, tmp_path, size=4)
    for _ in range(10):
        with db.db_conn() as conn:
            conn.execute("SELECT 1")
    assert db._db_pool_created == 1


def test_a_fully_loaned_out_pool_still_blocks(monkeypatch, tmp_path):
    """Past the ceiling we deliberately wait, so sqlite is not flooded with writers."""
    _fresh_pool(monkeypatch, tmp_path, size=2)
    held = [db._get_pooled_conn() for _ in range(2)]
    waited = threading.Event()

    def borrow():
        conn = db._get_pooled_conn()
        waited.set()
        db._return_pooled_conn(conn)

    t = threading.Thread(target=borrow, daemon=True)
    t.start()
    assert not waited.wait(timeout=0.3)  # nothing spare, so it is queueing patiently
    db._return_pooled_conn(held.pop())
    assert waited.wait(timeout=5)  # handed one back, waiter wakes up
    t.join(timeout=5)
    for conn in held:
        db._return_pooled_conn(conn)


def _seeded_table(rows=50):
    """A table with enough rows that one fetchone() leaves the read unfinished."""
    with db.db_conn() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.executemany(
            "INSERT INTO t (v) VALUES (?)", [(f"row{i}",) for i in range(rows)]
        )
        conn.commit()


def test_a_half_read_select_does_not_poison_the_next_borrower(monkeypatch, tmp_path):
    """The bug behind the bulk-import crash: an abandoned SELECT held a WAL snapshot
    open on the pooled connection, so the next write on it failed instantly with
    "database is locked" the moment anybody else committed. busy_timeout never
    helped, because there was nothing to queue behind."""
    _fresh_pool(monkeypatch, tmp_path, size=2)
    _seeded_table()

    with db.db_conn() as conn:
        conn.execute("SELECT * FROM t").fetchone()  # rest of the rows left unread

    # Somebody else commits, staling any snapshot the pooled connection kept.
    other = db.get_db()
    other.execute("INSERT INTO t (v) VALUES ('elsewhere')")
    other.commit()
    other.reset_lease_state()

    with db.db_conn() as conn:  # LIFO, so this is the very connection from above
        conn.execute("INSERT INTO t (v) VALUES ('after')")
        conn.commit()


def test_a_stale_snapshot_mid_lease_is_cleared_and_the_write_retried(monkeypatch, tmp_path):
    """Same poison, but self-inflicted inside one lease: read, someone commits, write."""
    _fresh_pool(monkeypatch, tmp_path, size=2)
    _seeded_table()
    other = db.get_db()

    with db.db_conn() as conn:
        conn.execute("SELECT * FROM t").fetchone()
        other.execute("INSERT INTO t (v) VALUES ('elsewhere')")
        other.commit()
        other.reset_lease_state()
        conn.execute("INSERT INTO t (v) VALUES ('same lease')")
        conn.commit()


def test_a_write_waits_for_a_busy_writer_rather_than_erroring(monkeypatch, tmp_path):
    """Honest contention should queue, which is what busy_timeout is for."""
    _fresh_pool(monkeypatch, tmp_path, size=2)
    _seeded_table()

    holder = db.get_db()
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO t (v) VALUES ('holder')")

    def release():
        time.sleep(0.5)
        holder.commit()
        holder.reset_lease_state()

    threading.Thread(target=release, daemon=True).start()
    start = time.perf_counter()
    with db.db_conn() as conn:
        conn.execute("INSERT INTO t (v) VALUES ('patient')")
        conn.commit()
    assert time.perf_counter() - start >= 0.4  # it really did wait its turn


def test_uncommitted_writes_die_with_the_lease(monkeypatch, tmp_path):
    """Cleaning up cursors must not accidentally commit what nobody asked to keep."""
    _fresh_pool(monkeypatch, tmp_path, size=2)
    _seeded_table()

    with db.db_conn() as conn:
        conn.execute("INSERT INTO t (v) VALUES ('forgotten')")  # no commit

    with db.db_conn() as conn:
        rows = conn.execute(
            "SELECT COUNT(*) FROM t WHERE v = 'forgotten'"
        ).fetchall()
    assert rows[0][0] == 0


def test_sloppy_readers_and_writers_can_share_the_pool(monkeypatch, tmp_path):
    """Bulk import plus download threads, in miniature: every write must land."""
    _fresh_pool(monkeypatch, tmp_path, size=4)
    _seeded_table()
    errors = []

    def worker(n):
        for i in range(40):
            try:
                with db.db_conn() as conn:
                    conn.execute("SELECT * FROM t").fetchone()  # deliberately sloppy
                    conn.execute("INSERT INTO t (v) VALUES (?)", (f"t{n}-{i}",))
                    conn.commit()
            except Exception as e:
                errors.append(f"thread {n} op {i}: {e}")

    threads = [threading.Thread(target=worker, args=(n,), daemon=True) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors
    with db.db_conn() as conn:
        written = conn.execute(
            "SELECT COUNT(*) FROM t WHERE v LIKE 't%-%'"
        ).fetchall()[0][0]
    assert written == 6 * 40


def test_concurrent_cold_start_never_exceeds_the_ceiling(monkeypatch, tmp_path):
    """Twelve threads off the blocks at once must not create thirteen connections."""
    _fresh_pool(monkeypatch, tmp_path, size=4)
    go = threading.Event()
    errors = []

    def worker():
        go.wait()
        try:
            with db.db_conn() as conn:
                conn.execute("SELECT 1")
        except Exception as e:  # a locked or missing DB would surface here
            errors.append(e)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(12)]
    for t in threads:
        t.start()
    go.set()
    for t in threads:
        t.join(timeout=30)

    assert not errors
    assert db._db_pool_created <= 4
