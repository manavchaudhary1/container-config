import os
import sqlite3
import threading
import time


WINDOWS = {
    "1h": (60 * 60, 60),
    "6h": (6 * 60 * 60, 5 * 60),
    "24h": (24 * 60 * 60, 15 * 60),
    "7d": (7 * 24 * 60 * 60, 2 * 60 * 60),
}


class MetricsStore:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _initialize(self):
        with self.lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS request_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp INTEGER NOT NULL,
                    endpoint TEXT NOT NULL,
                    username TEXT,
                    model_id TEXT,
                    cache_state TEXT NOT NULL,
                    adapter_status INTEGER NOT NULL,
                    upstream_status INTEGER,
                    latency_ms INTEGER NOT NULL,
                    cloudflare_block INTEGER NOT NULL,
                    retry_count INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS request_events_timestamp
                    ON request_events(timestamp);
                CREATE INDEX IF NOT EXISTS request_events_username
                    ON request_events(username);
                CREATE INDEX IF NOT EXISTS request_events_model_id
                    ON request_events(model_id);

                CREATE TABLE IF NOT EXISTS system_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    detail TEXT
                );

                CREATE INDEX IF NOT EXISTS system_events_timestamp_kind
                    ON system_events(timestamp, kind);
                """
            )
            cutoff = int(time.time()) - 30 * 24 * 60 * 60
            connection.execute("DELETE FROM request_events WHERE timestamp < ?", (cutoff,))
            connection.execute("DELETE FROM system_events WHERE timestamp < ?", (cutoff,))

    def record_request(
        self,
        *,
        endpoint,
        username,
        model_id,
        cache_state,
        adapter_status,
        upstream_status,
        latency_ms,
        cloudflare_block,
        retry_count,
    ):
        with self.lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO request_events (
                    timestamp, endpoint, username, model_id, cache_state,
                    adapter_status, upstream_status, latency_ms,
                    cloudflare_block, retry_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(time.time()),
                    endpoint,
                    username,
                    model_id,
                    cache_state,
                    adapter_status,
                    upstream_status,
                    latency_ms,
                    int(cloudflare_block),
                    retry_count,
                ),
            )

    def record_event(self, kind, detail=None):
        with self.lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO system_events (timestamp, kind, detail) VALUES (?, ?, ?)",
                (int(time.time()), kind, detail),
            )

    def associate_model(self, username, model_id):
        with self.lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE request_events
                SET username = ?
                WHERE model_id = ? AND username IS NULL
                """,
                (username, model_id),
            )

    def dashboard(self, window_name):
        window_seconds, bucket_seconds = WINDOWS.get(window_name, WINDOWS["1h"])
        since = int(time.time()) - window_seconds

        with self.lock, self._connect() as connection:
            summary = dict(
                connection.execute(
                    """
                    SELECT
                        COUNT(*) AS total,
                        COALESCE(SUM(adapter_status BETWEEN 200 AND 299), 0) AS successful,
                        COALESCE(SUM(adapter_status BETWEEN 400 AND 499), 0) AS client_errors,
                        COALESCE(SUM(adapter_status >= 500), 0) AS server_errors,
                        COALESCE(SUM(cache_state = 'hit'), 0) AS cache_hits,
                        COALESCE(SUM(cache_state = 'miss'), 0) AS cache_misses,
                        COALESCE(SUM(cache_state = 'stale'), 0) AS stale_hits,
                        COALESCE(SUM(cloudflare_block), 0) AS cloudflare_blocks,
                        COALESCE(SUM(retry_count), 0) AS retries,
                        COALESCE(ROUND(AVG(latency_ms)), 0) AS average_latency_ms
                    FROM request_events
                    WHERE timestamp >= ?
                    """,
                    (since,),
                ).fetchone()
            )

            cache_lookups = (
                summary["cache_hits"]
                + summary["cache_misses"]
                + summary["stale_hits"]
            )
            summary["cache_hit_rate"] = round(
                100 * (summary["cache_hits"] + summary["stale_hits"]) / cache_lookups,
                1,
            ) if cache_lookups else 0

            summary["session_rotations"] = connection.execute(
                """
                SELECT COUNT(*)
                FROM system_events
                WHERE timestamp >= ? AND kind = 'session_rotation'
                """,
                (since,),
            ).fetchone()[0]

            statuses = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT adapter_status AS status, COUNT(*) AS total
                    FROM request_events
                    WHERE timestamp >= ?
                    GROUP BY adapter_status
                    ORDER BY adapter_status
                    """,
                    (since,),
                )
            ]

            timeline = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT
                        (timestamp / ?) * ? AS timestamp,
                        COUNT(*) AS total,
                        COALESCE(SUM(adapter_status BETWEEN 200 AND 299), 0) AS successful,
                        COALESCE(SUM(adapter_status BETWEEN 400 AND 499), 0) AS client_errors,
                        COALESCE(SUM(adapter_status >= 500), 0) AS server_errors,
                        COALESCE(SUM(cache_state = 'hit'), 0) AS cache_hits
                    FROM request_events
                    WHERE timestamp >= ?
                    GROUP BY timestamp / ?
                    ORDER BY timestamp
                    """,
                    (bucket_seconds, bucket_seconds, since, bucket_seconds),
                )
            ]

            models = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT
                        COALESCE(username, 'Model ' || model_id) AS username,
                        MAX(model_id) AS model_id,
                        COUNT(*) AS requests,
                        COALESCE(SUM(cache_state = 'hit'), 0) AS cache_hits,
                        COALESCE(SUM(cache_state = 'miss'), 0) AS cache_misses,
                        COALESCE(SUM(adapter_status BETWEEN 200 AND 299), 0) AS successful,
                        COALESCE(SUM(adapter_status BETWEEN 400 AND 499), 0) AS client_errors,
                        COALESCE(SUM(adapter_status >= 500), 0) AS server_errors,
                        COALESCE(SUM(cloudflare_block), 0) AS cloudflare_blocks
                    FROM request_events
                    WHERE timestamp >= ? AND (username IS NOT NULL OR model_id IS NOT NULL)
                    GROUP BY COALESCE(username, 'Model ' || model_id)
                    ORDER BY requests DESC, username
                    """,
                    (since,),
                )
            ]

        return {
            "window": window_name if window_name in WINDOWS else "1h",
            "generated_at": int(time.time()),
            "summary": summary,
            "statuses": statuses,
            "timeline": timeline,
            "models": models,
        }
