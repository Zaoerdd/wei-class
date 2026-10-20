import asyncio
import threading
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace
from pathlib import Path

import web
import getdata
import requests
from wechat_qr_sign import QrOutcome, WeChatQrSigner, parse_result_url, validate_qr_url, WECHAT_UI_LOCK

URL = "https://v18.teachermate.cn/qr/test?code=sample"
ITEM = {"courseId": 1, "signId": 2, "isQR": 1, "isGPS": 0, "signType": "二维码签到"}
KEY = (1, 2)


class RefreshRetryTests(unittest.TestCase):
    def setUp(self):
        self.c = Mock()
        self.manager = web.OpenIdRefreshManager(self.c, Path("unused-test-cache"))
        self.manager.listening_enabled = True

    @patch("web.time.monotonic", return_value=100)
    def test_failed_refresh_backoff_and_success_reset(self, clock):
        self.c.run_once.side_effect = [RuntimeError("window unavailable")] * 5 + [{"openid": "fake"}]
        with patch.object(self.manager, "_validate_record", return_value={"openid": "fake"}), patch.object(self.manager, "_apply_record"), patch.object(web.logger, "exception"):
            for delay in (30, 60, 120, 300, 300):
                self.assertFalse(self.manager.refresh_openid("test", False))
                self.assertEqual(self.manager._next_refresh_deadline, 100 + delay)
                self.assertFalse(self.manager.is_refreshing)
            self.assertTrue(self.manager.refresh_openid("test", False))
        self.assertEqual(self.manager.refresh_failures, 0)
        # Success schedules nothing: the listener reports when the OpenID stops working.
        self.assertIsNone(self.manager._next_refresh_deadline)
        self.assertIsNone(self.manager.next_refresh_at)

    def test_parallel_request_does_not_postpone_retry(self):
        self.manager._next_refresh_deadline = 123
        self.manager._refresh_lock.acquire()
        try:
            self.assertFalse(self.manager.refresh_openid("test", False))
        finally:
            self.manager._refresh_lock.release()
        self.assertEqual(self.manager._next_refresh_deadline, 123)
        self.c.run_once.assert_not_called()

    def test_scheduler_uses_retry_deadline(self):
        self.manager._next_refresh_deadline = 30
        with patch.object(self.manager._stop_event, "wait", side_effect=[False, False, True]), patch("web.time.monotonic", side_effect=[29, 30]), patch.object(self.manager, "refresh_openid") as refresh:
            self.manager._run_loop()
        refresh.assert_called_once_with(reason="retry", use_cache=True)


class SessionOpeningTests(unittest.TestCase):
    def setUp(self):
        self.old = SimpleNamespace(hwnd=1, rect="before restore")
        self.restored = SimpleNamespace(hwnd=1, rect="after restore")
        self.c = Mock()
        self.c.find_wechat_window.return_value = self.restored
        self.signer = WeChatQrSigner(self.c)
        self.signer._assert_foreground = Mock()

    def test_window_move_during_header_match_stops_before_send(self):
        self.c.assert_window_ready = Mock(side_effect=[None, RuntimeError("window moved")])
        with self.assertRaisesRegex(RuntimeError, "window moved"):
            self.signer._assert_target(self.restored, lambda: False)
        self.c._pyautogui.click.assert_not_called()
        self.c._pyautogui.press.assert_not_called()

    @patch("wechat_qr_sign.time.sleep")
    def test_repaint_delay_and_restored_coordinates(self, sleep):
        point = SimpleNamespace(x=100, y=200)
        self.c.find_template_location.side_effect = [None, None, None, point, object()]
        self.assertIs(self.signer._open_file_transfer(self.old, lambda: False), self.restored)
        self.c._click_location.assert_called_once_with(point)
        self.assertTrue(all(call.args[1] == "after restore" for call in self.c.find_template_location.call_args_list))

    @patch("wechat_qr_sign.time.sleep")
    def test_missing_session_has_actionable_error_and_no_click(self, sleep):
        self.c.find_template_location.return_value = None
        with self.assertRaisesRegex(RuntimeError, "未找到文件传输助手"):
            self.signer._open_file_transfer(self.old, lambda: False)
        self.c._click_location.assert_not_called()

    @patch("wechat_qr_sign.time.sleep")
    def test_focus_loss_before_click_stops(self, sleep):
        self.c.find_template_location.side_effect = [None, object()]
        self.signer._assert_foreground.side_effect = [None, RuntimeError("focus lost")]
        with self.assertRaisesRegex(RuntimeError, "focus lost"):
            self.signer._open_file_transfer(self.old, lambda: False)
        self.c._click_location.assert_not_called()

    @patch("wechat_qr_sign.time.sleep")
    def test_window_move_invalidates_click(self, sleep):
        moved = SimpleNamespace(hwnd=1, rect="moved again")
        self.c.find_wechat_window.side_effect = [self.restored, moved, moved]
        self.c.find_template_location.side_effect = [None, object(), object()]
        self.assertIs(self.signer._open_file_transfer(self.old, lambda: False), moved)
        self.c._click_location.assert_not_called()


class UrlTests(unittest.TestCase):
    def test_read_retries_transient_tls_failure(self):
        response = type("Response", (), {"text": "[]"})()
        with patch.object(getdata.requests, "get", side_effect=[requests.exceptions.SSLError("EOF"), response]) as call, patch.object(getdata.time, "sleep"):
            self.assertEqual(getdata.getData("test"), [])
            self.assertEqual(call.call_count, 2)

    def test_domain_allowlist(self):
        self.assertEqual(validate_qr_url(URL), URL)
        for url in ["https://teachermate.cn.evil.test/x", "http://v18.teachermate.cn/x",
                    "https://user@v18.teachermate.cn/x", "https://v18.teachermate.cn:444/x",
                    "javascript:alert(1)", URL + "\n", URL + "\\x", URL + " 空格"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_qr_url(url)

    def test_results_are_explicit_and_fresh(self):
        url = "https://v18.teachermate.cn/wechat-pro-ssr/student/signresult?success=1&studentRank=2"
        self.assertEqual(parse_result_url(url).state, "completed")
        self.assertIsNone(parse_result_url(url, url))
        self.assertIsNone(parse_result_url(URL + "&success=1"))
        self.assertIsNone(parse_result_url(url.replace("v18.teachermate.cn", "evil.test")))
        self.assertIsNone(parse_result_url(url.replace("success=1", "success=1&success=0")))
        self.assertEqual(parse_result_url(url.replace("success=1", "success=0")).state, "manual")
        with self.assertRaises(ValueError):
            validate_qr_url(url)

    def test_cancellation_while_waiting_for_ui(self):
        WECHAT_UI_LOCK.acquire()
        try:
            outcome = WeChatQrSigner(None).run(URL, lambda: True)
            self.assertEqual(outcome.state, "cancelled")
        finally:
            WECHAT_UI_LOCK.release()


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.local = patch.object(web, "local_state", Mock())
        self.local.start().has_success.return_value = False
        self.addCleanup(self.local.stop)
        self.p = web.Pipeline("test-openid", {"student_number": "123"})
        self.p._log_faye_event = lambda *args: None
        self.p.active_signs = [dict(ITEM)]
        self.p.loop = asyncio.get_running_loop()
        self.enabled = patch.object(web, "qr_auto_enabled", True)
        self.enabled.start()
        self.notifier = patch.object(web.pushplus_notifier, "token", "")
        self.notifier.start()
        self.collector = patch.object(web, "collector", type("Collector", (), {"find_template_location": lambda: None})())
        self.collector.start()

    def tearDown(self):
        self.enabled.stop()
        self.notifier.stop()
        self.collector.stop()

    async def drain(self):
        await asyncio.gather(*list(self.p.asyncio_tasks))

    async def test_duplicate_and_rotation_do_not_send_during_attempt(self):
        gate = threading.Event()
        def run(*args):
            gate.wait(2)
            return QrOutcome("manual", "not confirmed")
        with patch.object(WeChatQrSigner, "run", side_effect=run) as sender:
            await self.p._process_callback_result(ITEM, URL)
            await self.p._process_callback_result(ITEM, URL)
            await self.p._process_callback_result(ITEM, URL + "2")
            gate.set()
            await self.drain()
            self.assertEqual(sender.call_count, 1)
            self.assertFalse(self.p.sign_results[KEY]["sign_completed"])
            await self.p._process_callback_result(ITEM, URL)
            await self.drain()
            self.assertEqual(sender.call_count, 1)

    async def test_confirmed_success_not_overwritten_by_next_qr(self):
        with patch.object(WeChatQrSigner, "run", return_value=QrOutcome("completed", "confirmed")) as sender:
            await self.p._process_callback_result(ITEM, URL)
            await self.drain()
            await self.p._process_callback_result(ITEM, URL + "2")
            self.assertEqual(sender.call_count, 1)
            self.assertTrue(self.p.sign_results[KEY]["sign_completed"])
            self.p.active_signs = []
            self.assertTrue(self.p.get_status()["result_meta"]["sign_completed"])

    async def test_disabled_does_not_send(self):
        with patch.object(web, "qr_auto_enabled", False), patch.object(WeChatQrSigner, "run") as sender:
            await self.p._process_callback_result(ITEM, URL)
            await self.drain()
            sender.assert_not_called()

    async def test_failure_preserves_manual_qr(self):
        with patch.object(WeChatQrSigner, "run", side_effect=RuntimeError("draft exists")):
            await self.p._process_callback_result(ITEM, URL)
            await self.drain()
            meta = self.p.sign_results[KEY]
            self.assertEqual(meta["qr_url"], URL)
            self.assertEqual(meta["qr_auto_state"], "manual")
            self.assertFalse(meta["sign_completed"])

    async def test_retry_budget_and_cooldown(self):
        with patch.object(WeChatQrSigner, "run", return_value=QrOutcome("manual", "unconfirmed")) as sender:
            for index in range(5):
                if KEY in self.p.qr_auto_attempts:
                    self.p.qr_auto_attempts[KEY]["last_at"] = 0
                await self.p._process_callback_result(ITEM, URL + str(index))
                await self.drain()
                await self.p._process_callback_result(ITEM, URL + str(index) + "rotated")
                await self.drain()
            self.assertEqual(sender.call_count, 3)

    async def test_rejected_url_never_reaches_wechat(self):
        with patch.object(WeChatQrSigner, "run") as sender:
            await self.p._process_callback_result(ITEM, "https://example.com/qr")
            await self.drain()
            sender.assert_not_called()
            self.assertEqual(self.p.sign_results[KEY]["qr_auto_state"], "manual")

    async def test_late_progress_cannot_overwrite_success(self):
        self.p.sign_results[KEY] = web.build_result_meta(ITEM, "confirmed", sign_completed=True)
        self.p._set_qr_auto_state(KEY, "manual", "late timeout")
        self.assertTrue(self.p.sign_results[KEY]["sign_completed"])
        self.assertEqual(self.p.sign_results[KEY]["status_message"], "confirmed")

    async def test_only_own_student_on_exact_sign_channel_confirms(self):
        event = {"event_kind": "student_signed", "channel": "/attendance/1/2",
                 "data": {"student": {"studentNumber": "456", "name": "Same Name"}}}
        await self.p._process_ws_event(ITEM, event)
        self.assertFalse(self.p.sign_results[KEY]["sign_completed"])
        event["data"]["student"]["studentNumber"] = "123"
        event["channel"] = "/attendance/1"
        await self.p._process_ws_event(ITEM, event)
        self.assertFalse(self.p.sign_results[KEY]["sign_completed"])
        event["channel"] = "/attendance/1/2"
        event["data"]["student"]["isOutOfBound"] = 1
        await self.p._process_ws_event(ITEM, event)
        self.assertFalse(self.p.sign_results[KEY]["sign_completed"])
        event["data"]["student"]["isOutOfBound"] = 0
        await self.p._process_ws_event(ITEM, event)
        self.assertTrue(self.p.sign_results[KEY]["sign_completed"])


if __name__ == "__main__":
    unittest.main()
