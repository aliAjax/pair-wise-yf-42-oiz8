import json
import random
import sqlite3
import time
from datetime import datetime, timezone

from .domain import ConflictError, NotFoundError


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Two writers that both issue BEGIN IMMEDIATE can otherwise deadlock: the
# holder cannot upgrade its lock while the second connection waits for it.
# Transactions therefore fail fast on a locked database and the whole unit is
# retried with exponential backoff + jitter.
TX_LOCK_DEADLINE_SECONDS = 30.0


class SQLiteRepository:
    def __init__(self, path):
        self.path = str(path)
        # Test hook: when set, the matching repository write raises this
        # exception. Consumed on trigger so a retry can succeed. ``fail_after``
        # lets a test let the first N writes through (e.g. the durable
        # planning transaction) and crash the coordination afterwards.
        self.pending_write_failure = None
        self.fail_after = 0
        self._write_count = 0
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    def _fail_if_injected(self):
        if self.pending_write_failure is not None:
            if self._write_count < self.fail_after:
                self._write_count += 1
                return
            failure = self.pending_write_failure
            self.pending_write_failure = None
            self.fail_after = 0
            self._write_count = 0
            raise failure
        self._write_count += 1

    def arm_write_failure(self, exception, after=0):
        """Test hook: fail the (after+1)-th write from this point on."""
        self.pending_write_failure = exception
        self.fail_after = after
        self._write_count = 0

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_entities_kind_status
                    ON entities(kind, status);
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_entity
                    ON audit_log(entity_id, id);
                CREATE TABLE IF NOT EXISTS idempotency (
                    actor_id TEXT NOT NULL,
                    idem_key TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(actor_id, idem_key)
                );
                CREATE TABLE IF NOT EXISTS quarantine_keys (
                    scope TEXT NOT NULL,
                    key TEXT NOT NULL,
                    quarantine_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(scope, key)
                );
                CREATE TABLE IF NOT EXISTS coordination_locks (
                    name TEXT PRIMARY KEY,
                    quarantine_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS coordination_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    quarantine_id TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    target_id TEXT,
                    state TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_items_order
                    ON coordination_items(quarantine_id, id);
                CREATE INDEX IF NOT EXISTS idx_items_state
                    ON coordination_items(state);
            """)

    @staticmethod
    def _entity_from_row(row):
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "version": int(row["version"]),
            "data": json.loads(row["data"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _item_from_row(row):
        return {
            "id": row["id"],
            "quarantine_id": row["quarantine_id"],
            "phase": row["phase"],
            "kind": row["kind"],
            "target_id": row["target_id"],
            "state": row["state"],
            "detail": json.loads(row["detail"]),
            "attempts": int(row["attempts"]),
            "last_error": row["last_error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    # ------------------------------------------------------------------
    # Entities
    # ------------------------------------------------------------------

    def create_entity(self, entity_id, kind, status, data, actor_id, connection=None):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)

        def _insert(conn):
            self._fail_if_injected()
            conn.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                (entity_id, kind, status, payload, actor_id, now, now),
            )

        if connection is not None:
            _insert(connection)
            return self.get_entity(entity_id, connection=connection)
        with self._connect() as conn:
            _insert(conn)
        return self.get_entity(entity_id)

    def get_entity(self, entity_id, connection=None):
        sql = "SELECT * FROM entities WHERE id = ?"
        if connection is not None:
            row = connection.execute(sql, (entity_id,)).fetchone()
        else:
            with self._connect() as connection:
                row = connection.execute(sql, (entity_id,)).fetchone()
        return self._entity_from_row(row) if row else None

    def list_entities(self, kind=None, status=None, connection=None):
        clauses = []
        params = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = "SELECT * FROM entities" + where + " ORDER BY created_at, id"

        def _query(conn):
            return [self._entity_from_row(row) for row in conn.execute(sql, params).fetchall()]

        if connection is not None:
            return _query(connection)
        with self._connect() as conn:
            return _query(conn)

    def find_entities(self, kind, field, value):
        return [
            entity
            for entity in self.list_entities(kind=kind)
            if (entity["id"] == value if field == "id" else entity["data"].get(field) == value)
        ]

    def update_entity(self, entity_id, expected_version, status, data):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)

        def _work(connection):
            self._update_entity_row(
                connection, entity_id, expected_version, status, payload, now
            )

        self.run_tx(_work)
        return self.get_entity(entity_id)

    def _update_entity_row(self, conn, entity_id, expected_version, status, payload, now):
        """Version-checked UPDATE usable inside an existing transaction."""
        self._fail_if_injected()
        if payload is None:
            row = conn.execute("SELECT data FROM entities WHERE id = ?", (entity_id,)).fetchone()
            payload = row["data"] if row else "{}"
        row = conn.execute(
            "SELECT version FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        if not row:
            raise NotFoundError("entity not found: " + entity_id)
        current_version = int(row["version"])
        if expected_version is not None and current_version != int(expected_version):
            raise ConflictError(
                "version conflict: expected %s, found %s"
                % (expected_version, current_version)
            )
        conn.execute(
            "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
            "WHERE id = ? AND version = ?",
            (status, payload, now, entity_id, current_version),
        )
        return current_version

    def update_entity_in_tx(self, connection, entity_id, expected_version, status, data):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        self._update_entity_row(
            connection, entity_id, expected_version, status, payload, now
        )
        return self.get_entity(entity_id, connection=connection)

    # ------------------------------------------------------------------
    # Transactions
    # ------------------------------------------------------------------

    def transaction(self):
        return _Transaction(self)

    def run_tx(self, work):
        """Run ``work(connection)`` in an immediate write transaction.

        Lock conflicts are retried at the *whole transaction* level, which
        avoids the BEGIN IMMEDIATE writer deadlock. All coordination work is
        idempotent, so a full replay is safe.
        """
        deadline = time.monotonic() + TX_LOCK_DEADLINE_SECONDS
        attempt = 0
        while True:
            connection = self._connect()
            try:
                connection.execute("PRAGMA busy_timeout = 250")
                connection.execute("BEGIN IMMEDIATE")
                result = work(connection)
                connection.execute("COMMIT")
                return result
            except sqlite3.OperationalError as exc:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                # Only genuine lock contention is retried at the transaction
                # level; other write failures (disk I/O, injected faults,
                # corruption, ...) surface immediately so the unfinished item
                # is retained rather than silently replayed.
                message = str(exc).lower()
                locked = message == "database is locked" or message.startswith(
                    "database table is locked"
                ) or message == "database schema is locked"
                if not locked or time.monotonic() >= deadline:
                    raise
                attempt += 1
                delay = min(0.05 * (2 ** attempt), 1.0)
                time.sleep(delay + random.random() * 0.02)
            except BaseException:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            finally:
                connection.close()

    # ------------------------------------------------------------------
    # Coordination items (the recoverable workflow log)
    # ------------------------------------------------------------------

    def add_item(self, quarantine_id, phase, kind, state, target_id=None,
                 detail=None, connection=None):
        now = utcnow()
        payload = json.dumps(detail or {}, ensure_ascii=False, sort_keys=True)

        def _insert(conn):
            self._fail_if_injected()
            cursor = conn.execute(
                "INSERT INTO coordination_items(quarantine_id, phase, kind, target_id, state, "
                "detail, attempts, last_error, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 0, NULL, ?, ?)",
                (quarantine_id, phase, kind, target_id, state, payload, now, now),
            )
            return cursor.lastrowid

        if connection is not None:
            return _insert(connection)
        with self._connect() as conn:
            return _insert(conn)

    def list_items(self, quarantine_id=None, states=None, phase=None, connection=None):
        clauses = []
        params = []
        if quarantine_id:
            clauses.append("quarantine_id = ?")
            params.append(quarantine_id)
        if phase:
            clauses.append("phase = ?")
            params.append(phase)
        if states:
            clauses.append("state IN (%s)" % ",".join("?" for _ in states))
            params.extend(states)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = "SELECT * FROM coordination_items" + where + " ORDER BY id"

        def _query(conn):
            return [self._item_from_row(row) for row in conn.execute(sql, params).fetchall()]

        if connection is not None:
            return _query(connection)
        with self._connect() as conn:
            return _query(conn)

    def get_item(self, item_id, connection=None):
        sql = "SELECT * FROM coordination_items WHERE id = ?"
        if connection is not None:
            row = connection.execute(sql, (item_id,)).fetchone()
        else:
            with self._connect() as conn:
                row = conn.execute(sql, (item_id,)).fetchone()
        return self._item_from_row(row) if row else None

    def mark_item(self, item_id, state, error=None, connection=None):
        now = utcnow()

        def _update(conn):
            self._fail_if_injected()
            conn.execute(
                "UPDATE coordination_items SET state = ?, last_error = ?, "
                "attempts = attempts + 1, updated_at = ? WHERE id = ?",
                (state, error, now, item_id),
            )

        if connection is not None:
            _update(connection)
        else:
            with self._connect() as conn:
                _update(conn)
        return self.get_item(item_id, connection=connection)

    # ------------------------------------------------------------------
    # Quarantine uniqueness keys
    # ------------------------------------------------------------------

    def hold_coordination_lock(self, name, quarantine_id, connection):
        """Advisory mutex serializing a coordination phase.

        Unlike the entity version check, contending inserts fail fast (they are
        not ordinary lock contention): only the first submitter/releaser takes
        effect, the other gets the latest blocking list.
        """
        self._fail_if_injected()
        try:
            connection.execute(
                "INSERT INTO coordination_locks(name, quarantine_id, created_at) "
                "VALUES (?, ?, ?)",
                (name, quarantine_id, utcnow()),
            )
        except sqlite3.IntegrityError:
            raise ConflictError("coordination phase already taken: " + name)

    def has_coordination_lock(self, name, quarantine_id, connection=None):
        sql = (
            "SELECT 1 FROM coordination_locks "
            "WHERE name = ? AND quarantine_id = ?"
        )
        if connection is not None:
            row = connection.execute(sql, (name, quarantine_id)).fetchone()
        else:
            with self._connect() as conn:
                row = conn.execute(sql, (name, quarantine_id)).fetchone()
        return row is not None

    def delete_coordination_lock(self, name, quarantine_id, connection=None):
        sql = "DELETE FROM coordination_locks WHERE name = ? AND quarantine_id = ?"
        if connection is not None:
            connection.execute(sql, (name, quarantine_id))
        else:
            with self._connect() as conn:
                conn.execute(sql, (name, quarantine_id))

    def hold_quarantine_key(self, scope, key, quarantine_id, connection):
        self._fail_if_injected()
        try:
            connection.execute(
                "INSERT INTO quarantine_keys(scope, key, quarantine_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (scope, key, quarantine_id, utcnow()),
            )
        except sqlite3.IntegrityError:
            raise ConflictError(
                "open quarantine already exists for %s=%s" % (scope, key)
            )

    def get_quarantine_key(self, scope, key):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT quarantine_id FROM quarantine_keys WHERE scope = ? AND key = ?",
                (scope, key),
            ).fetchone()
        return row["quarantine_id"] if row else None

    def release_quarantine_keys(self, quarantine_id, connection=None):
        def _delete(conn):
            conn.execute(
                "DELETE FROM quarantine_keys WHERE quarantine_id = ?",
                (quarantine_id,),
            )

        if connection is not None:
            _delete(connection)
        else:
            with self._connect() as conn:
                _delete(conn)

    # ------------------------------------------------------------------
    # Audit / idempotency
    # ------------------------------------------------------------------

    def append_audit(self, entity_id, actor_id, actor_role, action, from_status,
                     to_status, detail, connection=None):
        payload = json.dumps(detail or {}, ensure_ascii=False, sort_keys=True)

        def _insert(conn):
            conn.execute(
                "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, "
                "to_status, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entity_id,
                    actor_id,
                    actor_role,
                    action,
                    from_status,
                    to_status,
                    payload,
                    utcnow(),
                ),
            )

        if connection is not None:
            _insert(connection)
        else:
            with self._connect() as conn:
                _insert(conn)

    def list_audit(self, entity_id=None):
        with self._connect() as connection:
            if entity_id:
                rows = connection.execute(
                    "SELECT * FROM audit_log WHERE entity_id = ? ORDER BY id", (entity_id,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        return [
            {
                "id": row["id"],
                "entity_id": row["entity_id"],
                "actor_id": row["actor_id"],
                "actor_role": row["actor_role"],
                "action": row["action"],
                "from_status": row["from_status"],
                "to_status": row["to_status"],
                "detail": json.loads(row["detail"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_idempotency(self, actor_id, idem_key):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT entity_id FROM idempotency WHERE actor_id = ? AND idem_key = ?",
                (actor_id, idem_key),
            ).fetchone()
        return row["entity_id"] if row else None

    def save_idempotency(self, actor_id, idem_key, entity_id):
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (actor_id, idem_key, entity_id, utcnow()),
            )

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True


class _Transaction:
    """Context manager wrapping an immediate SQLite write transaction."""

    def __init__(self, repository):
        self.repository = repository
        self.connection = None

    def __enter__(self):
        self.connection = self.repository._connect()
        self.connection.execute("PRAGMA busy_timeout = 250")
        self.connection.execute("BEGIN IMMEDIATE")
        return self.connection

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.connection.execute("COMMIT")
            else:
                self.connection.execute("ROLLBACK")
        finally:
            self.connection.close()
        return False
