# -*- coding: utf-8 -*-
import datetime
import unittest

import ig_token_refresh as m

UTC = datetime.timezone.utc


class ExpiryMath(unittest.TestCase):
    def test_expiry_from_now(self):
        now = datetime.datetime(2026, 8, 1, tzinfo=UTC)
        self.assertEqual(m.expiry_from_now(60 * 86400, now),
                         datetime.datetime(2026, 9, 30, tzinfo=UTC))

    def test_parse_expiry_roundtrip(self):
        dt = datetime.datetime(2026, 9, 30, 12, tzinfo=UTC)
        self.assertEqual(m.parse_expiry(dt.isoformat()), dt)

    def test_parse_expiry_empty_is_none(self):
        self.assertIsNone(m.parse_expiry(""))

    def test_parse_expiry_naive_becomes_utc(self):
        self.assertEqual(m.parse_expiry("2026-09-30T00:00:00").tzinfo, UTC)

    def test_parse_expiry_invalid_is_none(self):
        self.assertIsNone(m.parse_expiry("not-a-date"))

    def test_days_remaining_positive(self):
        now = datetime.datetime(2026, 8, 1, tzinfo=UTC)
        exp = datetime.datetime(2026, 8, 15, tzinfo=UTC)
        self.assertEqual(m.days_remaining(exp, now), 14)

    def test_days_remaining_none(self):
        self.assertIsNone(m.days_remaining(None, datetime.datetime(2026, 8, 1, tzinfo=UTC)))

    def test_days_remaining_expired_is_negative(self):
        now = datetime.datetime(2026, 8, 10, tzinfo=UTC)
        exp = datetime.datetime(2026, 8, 1, tzinfo=UTC)
        self.assertEqual(m.days_remaining(exp, now), -9)

    def test_days_remaining_across_timezones(self):
        JST = datetime.timezone(datetime.timedelta(hours=9))
        now = datetime.datetime(2026, 8, 1, 9, tzinfo=JST)       # = 2026-08-01T00:00Z
        exp = datetime.datetime(2026, 8, 15, 0, tzinfo=UTC)
        self.assertEqual(m.days_remaining(exp, now), 14)


class Decisions(unittest.TestCase):
    def test_should_warn_true_when_short(self):
        self.assertTrue(m.should_warn(40 * 86400, 50))

    def test_should_warn_false_when_full(self):
        self.assertFalse(m.should_warn(60 * 86400, 50))

    def test_should_create_task(self):
        self.assertTrue(m.should_create_task(10, 14))
        self.assertFalse(m.should_create_task(20, 14))
        self.assertFalse(m.should_create_task(None, 14))

    def test_should_warn_boundary_is_false(self):
        self.assertFalse(m.should_warn(50 * 86400, 50))

    def test_should_create_task_boundary_is_false(self):
        self.assertFalse(m.should_create_task(14, 14))


class Messages(unittest.TestCase):
    def test_heartbeat_normal(self):
        exp = datetime.datetime(2026, 9, 30, tzinfo=UTC)
        msg = m.heartbeat_message(exp, 59, False)
        self.assertIn("✅", msg)
        self.assertIn("あと59日", msg)
        self.assertIn("2026-09-30", msg)
        self.assertNotIn("⚠️", msg)

    def test_heartbeat_warn(self):
        exp = datetime.datetime(2026, 8, 20, tzinfo=UTC)
        msg = m.heartbeat_message(exp, 19, True)
        self.assertIn("⚠️", msg)

    def test_failure_message_known_days(self):
        msg = m.failure_message(5, "boom")
        self.assertIn("残り5日", msg)
        self.assertIn("boom", msg)

    def test_failure_message_unknown_days(self):
        self.assertIn("不明", m.failure_message(None, "boom"))


class NotionPayload(unittest.TestCase):
    def test_payload_shape(self):
        p = m.notion_task_payload("db123", "2026-08-15", 10)
        self.assertEqual(p["parent"]["database_id"], "db123")
        self.assertEqual(p["properties"]["期限"]["date"]["start"], "2026-08-15")
        title = p["properties"]["タスク名"]["title"][0]["text"]["content"]
        self.assertIn(m.NOTION_TASK_MARKER, title)
        self.assertIn("残り10日", title)
        # プライベートタスクDBは ステータス/優先度 が select 型
        self.assertEqual(p["properties"]["ステータス"]["select"]["name"], "未着手")
        self.assertEqual(p["properties"]["優先度"]["select"]["name"], "高")

    def test_update_props_shape(self):
        up = m.notion_update_props("2026-08-15", 3)
        self.assertEqual(up["期限"]["date"]["start"], "2026-08-15")
        self.assertIn("残り3日", up["タスク名"]["title"][0]["text"]["content"])


class AuthError(unittest.TestCase):
    def test_is_auth_error_true_code190(self):
        self.assertTrue(m.is_auth_error('{"error":{"code":190,"message":"expired"}}'))

    def test_is_auth_error_true_oauth(self):
        self.assertTrue(m.is_auth_error('OAuthException: session invalidated'))

    def test_is_auth_error_false(self):
        self.assertFalse(m.is_auth_error('{"error":{"code":500,"message":"boom"}}'))


if __name__ == "__main__":
    unittest.main()
