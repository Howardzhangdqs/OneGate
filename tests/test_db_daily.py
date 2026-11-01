import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import unittest

from src.db import (
    get_daily_usage,
    get_daily_usage_by_provider,
    init_db,
    rollup_and_purge,
    rollup_day,
)

DAY = 1700000000 - 1700000000 % 86400
DETAIL_TABLES = (
    "request_log",
    "status_log",
    "success_log",
    "token_usage_log",
    "token_rate_usage",
    "client_usage_log",
    "client_token_usage_log",
)


def _insert_detail(conn, ts):
    conn.execute(
        "INSERT INTO request_log (token, provider, ts, count) VALUES (?, ?, ?, ?)",
        ("t1", "p1", ts, 3),
    )
    conn.execute(
        "INSERT INTO status_log (provider, status_code, ts, count) VALUES (?, ?, ?, ?)",
        ("p1", 200, ts, 1),
    )
    conn.execute(
        "INSERT INTO success_log (token, provider, ts, count) VALUES (?, ?, ?, ?)",
        ("t1", "p1", ts, 2),
    )
    conn.execute(
        "INSERT INTO token_usage_log "
        "(token, provider, model, ts, input_tokens, output_tokens, cache_read, "
        "cache_write) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("t1", "p1", "m1", ts, 10, 20, 1, 2),
    )
    conn.execute(
        "INSERT INTO token_rate_usage (token, ts, tokens) VALUES (?, ?, ?)",
        ("t1", ts, 30),
    )
    conn.execute(
        "INSERT INTO client_usage_log (token, provider, client, ts, count) "
        "VALUES (?, ?, ?, ?, ?)",
        ("t1", "p1", "c1", ts, 1),
    )
    conn.execute(
        "INSERT INTO client_token_usage_log "
        "(token, provider, client, model, ts, input_tokens, output_tokens, "
        "cache_read, cache_write, count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("t1", "p1", "c1", "m1", ts, 10, 20, 1, 2, 1),
    )
    conn.commit()


class TestRollupDay(unittest.TestCase):
    def setUp(self):
        self.conn, _ = init_db(":memory:")

    def tearDown(self):
        self.conn.close()

    def test_rollup_day_numbers(self):
        self.conn.execute(
            "INSERT INTO request_log (token, provider, ts, count) VALUES (?, ?, ?, ?)",
            ("t1", "p1", DAY + 3600, 3),
        )
        self.conn.execute(
            "INSERT INTO request_log (token, provider, ts, count) VALUES (?, ?, ?, ?)",
            ("t1", "p1", DAY + 7200, 2),
        )
        self.conn.execute(
            "INSERT INTO success_log (token, provider, ts, count) VALUES (?, ?, ?, ?)",
            ("t1", "p1", DAY + 60, 4),
        )
        self.conn.execute(
            "INSERT INTO token_usage_log "
            "(token, provider, model, ts, input_tokens, output_tokens, cache_read, "
            "cache_write) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("t1", "p1", "m1", DAY + 3600, 100, 200, 10, 20),
        )
        self.conn.execute(
            "INSERT INTO token_usage_log "
            "(token, provider, model, ts, input_tokens, output_tokens, cache_read, "
            "cache_write) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("t1", "p1", "m2", DAY + 7200, 50, 100, 5, 10),
        )
        self.conn.commit()

        rollup_day(self.conn, DAY)

        req = self.conn.execute(
            "SELECT token, provider, day, requests, successes FROM daily_requests"
        ).fetchall()
        self.assertEqual(req, [("t1", "p1", DAY, 5, 4)])
        tok = self.conn.execute(
            "SELECT token, provider, model, day, input_tokens, output_tokens, "
            "cache_read, cache_write FROM daily_tokens ORDER BY model"
        ).fetchall()
        self.assertEqual(
            tok,
            [
                ("t1", "p1", "m1", DAY, 100, 200, 10, 20),
                ("t1", "p1", "m2", DAY, 50, 100, 5, 10),
            ],
        )

    def test_rollup_day_idempotent(self):
        self.conn.execute(
            "INSERT INTO request_log (token, provider, ts, count) VALUES (?, ?, ?, ?)",
            ("t1", "p1", DAY + 3600, 3),
        )
        self.conn.execute(
            "INSERT INTO success_log (token, provider, ts, count) VALUES (?, ?, ?, ?)",
            ("t1", "p1", DAY + 60, 2),
        )
        self.conn.execute(
            "INSERT INTO token_usage_log "
            "(token, provider, model, ts, input_tokens, output_tokens, cache_read, "
            "cache_write) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("t1", "p1", "m1", DAY + 3600, 10, 20, 1, 2),
        )
        self.conn.commit()

        rollup_day(self.conn, DAY)
        first_req = self.conn.execute(
            "SELECT * FROM daily_requests ORDER BY token, provider, day"
        ).fetchall()
        first_tok = self.conn.execute(
            "SELECT * FROM daily_tokens ORDER BY token, provider, model, day"
        ).fetchall()

        rollup_day(self.conn, DAY)
        second_req = self.conn.execute(
            "SELECT * FROM daily_requests ORDER BY token, provider, day"
        ).fetchall()
        second_tok = self.conn.execute(
            "SELECT * FROM daily_tokens ORDER BY token, provider, model, day"
        ).fetchall()

        self.assertEqual(first_req, second_req)
        self.assertEqual(first_tok, second_tok)
        self.assertEqual(second_req[0][3], 3)


class TestRollupAndPurge(unittest.TestCase):
    def setUp(self):
        self.conn, _ = init_db(":memory:")

    def tearDown(self):
        self.conn.close()

    def test_purges_detail_and_keeps_daily(self):
        now1 = DAY + 10 * 86400
        today1 = now1 - now1 % 86400
        _insert_detail(self.conn, DAY)
        self.conn.execute(
            "INSERT INTO request_log (token, provider, ts, count) VALUES (?, ?, ?, ?)",
            ("t1", "p1", today1 + 100, 2),
        )
        self.conn.commit()

        rollup_and_purge(self.conn, retain_days=35, now=now1)

        marks = dict(
            self.conn.execute("SELECT day, complete FROM daily_rollup").fetchall()
        )
        self.assertEqual(marks[DAY], 1)
        self.assertEqual(marks[today1], 0)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM daily_requests").fetchone()[0], 2
        )

        now2 = now1 + 40 * 86400
        rollup_and_purge(self.conn, retain_days=35, now=now2)

        for table in DETAIL_TABLES:
            count = self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            self.assertEqual(count, 0, table)
        self.assertEqual(
            self.conn.execute(
                "SELECT requests, successes FROM daily_requests "
                "WHERE token = ? AND provider = ? AND day = ?",
                ("t1", "p1", DAY),
            ).fetchone(),
            (3, 2),
        )
        self.assertIsNotNone(
            self.conn.execute(
                "SELECT 1 FROM daily_rollup WHERE day = ?", (DAY,)
            ).fetchone()
        )

    def test_completed_day_not_recomputed_from_partial_detail(self):
        now = DAY + 10 * 86400
        self.conn.execute(
            "INSERT INTO request_log (token, provider, ts, count) VALUES (?, ?, ?, ?)",
            ("t1", "p1", DAY + 3600, 5),
        )
        self.conn.execute(
            "INSERT INTO request_log (token, provider, ts, count) VALUES (?, ?, ?, ?)",
            ("t1", "p1", DAY + 7200, 7),
        )
        self.conn.execute(
            "INSERT INTO success_log (token, provider, ts, count) VALUES (?, ?, ?, ?)",
            ("t1", "p1", DAY + 60, 12),
        )
        self.conn.commit()

        rollup_and_purge(self.conn, retain_days=35, now=now)
        before = self.conn.execute(
            "SELECT requests, successes FROM daily_requests WHERE day = ?", (DAY,)
        ).fetchone()
        self.assertEqual(before, (12, 12))

        self.conn.execute(
            "DELETE FROM request_log WHERE token = ? AND provider = ? AND ts = ?",
            ("t1", "p1", DAY + 7200),
        )
        self.conn.commit()

        rollup_and_purge(self.conn, retain_days=35, now=now)
        after = self.conn.execute(
            "SELECT requests, successes FROM daily_requests WHERE day = ?", (DAY,)
        ).fetchone()
        self.assertEqual(after, (12, 12))


class TestDailyQueries(unittest.TestCase):
    def setUp(self):
        self.conn, _ = init_db(":memory:")
        rows = [
            ("t1", "p1", DAY, 10, 9),
            ("t1", "p1", DAY - 86400, 5, 5),
            ("t2", "p1", DAY - 2 * 86400, 7, 6),
        ]
        for row in rows:
            self.conn.execute(
                "INSERT INTO daily_requests "
                "(token, provider, day, requests, successes) VALUES (?, ?, ?, ?, ?)",
                row,
            )
        token_rows = [
            ("t1", "p1", "m1", DAY, 100, 200, 0, 0),
            ("t1", "p1", "m2", DAY, 11, 22, 3, 4),
            ("t1", "p1", "m1", DAY - 3 * 86400, 50, 60, 1, 2),
        ]
        for row in token_rows:
            self.conn.execute(
                "INSERT INTO daily_tokens "
                "(token, provider, model, day, input_tokens, output_tokens, "
                "cache_read, cache_write) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_window_and_shape(self):
        usage = get_daily_usage(self.conn, days=1, now=DAY)
        self.assertEqual(len(usage), 1)
        self.assertEqual(
            usage[0],
            {
                "day": DAY,
                "date": "2023-11-14",
                "requests": 10,
                "successes": 9,
                "input_tokens": 111,
                "output_tokens": 222,
                "cache_read": 3,
                "cache_write": 4,
            },
        )

    def test_window_includes_prior_days(self):
        usage = get_daily_usage(self.conn, days=2, now=DAY)
        self.assertEqual([u["day"] for u in usage], [DAY - 86400, DAY])
        self.assertEqual(usage[0]["requests"], 5)
        self.assertEqual(usage[1]["requests"], 10)

    def test_ascending_and_token_only_day(self):
        usage = get_daily_usage(self.conn, days=4, now=DAY)
        self.assertEqual(
            [u["day"] for u in usage],
            [DAY - 3 * 86400, DAY - 2 * 86400, DAY - 86400, DAY],
        )
        only_tokens = usage[0]
        self.assertEqual(only_tokens["date"], "2023-11-11")
        self.assertEqual(only_tokens["requests"], 0)
        self.assertEqual(only_tokens["successes"], 0)
        self.assertEqual(only_tokens["input_tokens"], 50)
        self.assertEqual(only_tokens["output_tokens"], 60)
        self.assertEqual(only_tokens["cache_read"], 1)
        self.assertEqual(only_tokens["cache_write"], 2)

    def test_token_filter(self):
        usage = get_daily_usage(self.conn, days=10, token="t2", now=DAY)
        self.assertEqual(len(usage), 1)
        self.assertEqual(usage[0]["day"], DAY - 2 * 86400)
        self.assertEqual(usage[0]["requests"], 7)
        self.assertEqual(usage[0]["input_tokens"], 0)

    def test_days_clamped(self):
        self.assertEqual(len(get_daily_usage(self.conn, days=0, now=DAY)), 1)
        self.assertEqual(len(get_daily_usage(self.conn, days=-5, now=DAY)), 1)

    def test_by_provider(self):
        self.conn.execute(
            "INSERT INTO daily_requests "
            "(token, provider, day, requests, successes) VALUES (?, ?, ?, ?, ?)",
            ("t1", "alpha", DAY, 3, 2),
        )
        self.conn.execute(
            "INSERT INTO daily_requests "
            "(token, provider, day, requests, successes) VALUES (?, ?, ?, ?, ?)",
            ("t1", "beta", DAY, 4, 4),
        )
        self.conn.execute(
            "INSERT INTO daily_requests "
            "(token, provider, day, requests, successes) VALUES (?, ?, ?, ?, ?)",
            ("t1", "alpha", DAY - 86400, 1, 1),
        )
        self.conn.execute(
            "INSERT INTO daily_tokens "
            "(token, provider, model, day, input_tokens, output_tokens, "
            "cache_read, cache_write) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("t1", "alpha", "m", DAY, 10, 20, 0, 0),
        )
        self.conn.execute(
            "INSERT INTO daily_tokens "
            "(token, provider, model, day, input_tokens, output_tokens, "
            "cache_read, cache_write) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("t1", "beta", "m", DAY, 5, 6, 0, 0),
        )
        self.conn.commit()

        rows = get_daily_usage_by_provider(self.conn, days=2, now=DAY)
        keys = [(r["day"], r["provider"]) for r in rows]
        self.assertEqual(
            keys,
            [
                (DAY - 86400, "alpha"),
                (DAY - 86400, "p1"),
                (DAY, "alpha"),
                (DAY, "beta"),
                (DAY, "p1"),
            ],
        )
        alpha_today = [r for r in rows if r["day"] == DAY and r["provider"] == "alpha"][
            0
        ]
        self.assertEqual(alpha_today["requests"], 3)
        self.assertEqual(alpha_today["successes"], 2)
        self.assertEqual(alpha_today["input_tokens"], 10)
        self.assertEqual(alpha_today["output_tokens"], 20)
        beta_today = [r for r in rows if r["day"] == DAY and r["provider"] == "beta"][0]
        self.assertEqual(beta_today["input_tokens"], 5)


if __name__ == "__main__":
    unittest.main()
