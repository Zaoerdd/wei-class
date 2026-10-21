import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import web
from local_state import LocalState


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = LocalState(Path(self.tmp.name) / "state.sqlite3")
        self.local = patch.object(web, "local_state", self.store)
        self.local.start()
        self.addCleanup(self.local.stop)
        self.collector = Mock(method_name="cv")
        self.manager = web.OpenIdRefreshManager(self.collector, Path(self.tmp.name) / "cache.json")
        self.record = {"openid": "a" * 32, "profile": {"student_number": "test-123", "name": "测试用户"}}
        self.activate = patch.object(web, "activate_pipeline_for_openid", return_value=Mock(profile=self.record["profile"]))
        self.activation = self.activate.start()
        self.addCleanup(self.activate.stop)
        self.stop = patch.object(web, "stop_all_pipelines")
        self.stop.start()
        self.addCleanup(self.stop.stop)

    def test_first_launch_paused_and_pause_persists(self):
        self.assertFalse(self.manager.listening_enabled)
        self.manager.current_openid = self.record["openid"]
        self.manager.set_listening(True)
        self.activation.assert_called_once()
        self.manager.set_listening(False)
        restarted = web.OpenIdRefreshManager(self.collector, self.manager.cache_path)
        self.assertFalse(restarted.listening_enabled)

    def test_manual_refresh_while_paused_does_not_start_listener(self):
        with patch.object(self.manager, "_validate_record", return_value=self.record):
            self.assertTrue(self.manager.refresh_openid("manual", False))
        self.activation.assert_not_called()
        self.assertEqual(self.manager.current_openid, self.record["openid"])
        self.assertFalse(self.manager.listening_enabled)

    def test_pause_during_refresh_wins_over_late_completion(self):
        entered, release = threading.Event(), threading.Event()
        def collect():
            entered.set()
            release.wait(3)
            return self.record
        self.manager.listening_enabled = True
        self.collector.run_once.side_effect = collect
        with patch.object(self.manager, "_validate_record", return_value=self.record):
            thread = threading.Thread(target=self.manager.refresh_openid, args=("manual", False))
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertFalse(self.manager.request_refresh_async("manual"))
                self.manager.set_listening(False)
            finally:
                release.set()
                thread.join(3)
        self.assertFalse(thread.is_alive())
        self.activation.assert_not_called()
        self.assertFalse(self.manager.is_refreshing)

    def test_paused_scheduler_never_collects(self):
        with patch.object(self.manager._stop_event, "wait", side_effect=[False, True]), patch.object(self.manager, "refresh_openid") as refresh:
            self.manager._run_loop()
        refresh.assert_not_called()

    def test_api_pause_start_and_refresh_conflict(self):
        with patch.object(web, "openid_refresh_manager", self.manager), patch.object(web, "ensure_runtime_initialized"), patch.object(self.manager, "request_refresh_async", return_value=True) as refresh:
            client = web.app.test_client()
            self.assertEqual(client.post("/api/listening", json={"enabled": "false"}).status_code, 400)
            self.assertEqual(client.post("/api/listening", json={"enabled": True}).status_code, 200)
            self.assertTrue(self.manager.listening_enabled)
            self.assertEqual(client.post("/api/listening", json={"enabled": False}).status_code, 200)
            self.assertFalse(self.manager.listening_enabled)
            self.assertEqual(client.post("/api/openid/refresh").status_code, 202)
            refresh.return_value = False
            self.assertEqual(client.post("/api/openid/refresh").status_code, 409)
            self.assertFalse(client.get("/qr_code").json["openid_status"]["listening_enabled"])

    def test_only_confirmed_success_saved_once_across_restarts(self):
        item = {"courseId": 1, "signId": 2, "name": "测试签到", "courseName": "测试课程", "signType": "二维码签到"}
        pipeline = web.Pipeline(self.record["openid"], self.record["profile"])
        pending = web.build_result_meta(item, "等待扫码", qr_ready=True)
        pipeline._store_result_meta((1, 2), pending)
        self.assertEqual(self.store.history()["total"], 0)
        success = web.build_result_meta(item, "微助教确认成功", sign_completed=True, result_ready=True)
        pipeline._store_result_meta((1, 2), success)
        pipeline._store_result_meta((1, 2), success)
        restarted = LocalState(self.store.path)
        restarted.record_success("new-openid", self.record["profile"], success)
        self.assertEqual(restarted.history()["total"], 1)
        self.assertEqual(restarted.history()["records"][0]["course_name"], "测试课程")
        with web.app.test_client() as client:
            response = client.get("/api/sign_history")
        self.assertEqual(response.json["total"], 1)
        self.assertNotIn("openid", str(response.json))
        with patch.object(pipeline, "run_websocket_client") as submit:
            asyncio.run(pipeline.process_signatures([dict(item, isQR=1)]))
        submit.assert_not_called()

    def test_account_isolation_and_failed_result_excluded(self):
        meta = {"sign_completed": True, "course_id": 1, "sign_id": 2}
        self.store.record_success("one", {"student_number": "1"}, meta)
        self.assertFalse(self.store.has_success("two", {"student_number": "2"}, 1, 2))
        self.store.record_success("two", {}, dict(meta, is_error=True))
        self.assertEqual(self.store.history()["total"], 1)

    def test_startup_reuses_valid_cache_without_operating_wechat(self):
        self.manager.listening_enabled = True
        self.manager.cache_path.write_text(json.dumps({"openid": self.record["openid"]}), encoding="utf-8")
        with patch.object(self.manager, "_validate_record", return_value=self.record):
            self.assertTrue(self.manager.refresh_openid("startup", True))
        self.collector.run_once.assert_not_called()
        self.assertEqual(self.manager.current_source, "file")
        self.assertIsNone(self.manager._next_refresh_deadline)

    def test_missing_cache_falls_back_to_collecting(self):
        self.manager.listening_enabled = True
        self.collector.run_once.return_value = self.record
        with patch.object(self.manager, "_validate_record", return_value=self.record):
            self.assertTrue(self.manager.refresh_openid("startup", True))
        self.collector.run_once.assert_called_once()
        self.assertEqual(self.manager.current_source, "collector")
        self.assertIsNone(self.manager.last_error)

    def test_manual_refresh_always_collects_even_with_valid_cache(self):
        self.manager.cache_path.write_text(json.dumps({"openid": self.record["openid"]}), encoding="utf-8")
        self.collector.run_once.return_value = self.record
        with patch.object(self.manager, "_validate_record", return_value=self.record):
            self.assertTrue(self.manager.refresh_openid("manual", False))
        self.collector.run_once.assert_called_once()

    def test_failure_retries_only_when_no_openid_is_left(self):
        self.manager.listening_enabled = True
        self.collector.run_once.side_effect = RuntimeError("window unavailable")
        self.manager.current_openid = self.record["openid"]
        with patch.object(web.logger, "exception"):
            self.assertFalse(self.manager.refresh_openid("manual", False))
            # The current OpenID still works, so do not keep grabbing the WeChat window.
            self.assertIsNone(self.manager._next_refresh_deadline)
            self.assertIsNotNone(self.manager.last_error)
            self.manager.current_openid = None
            self.assertFalse(self.manager.refresh_openid("manual", False))
        self.assertIsNotNone(self.manager._next_refresh_deadline)
        self.assertIsNotNone(self.manager.next_refresh_at)

    def test_runtime_status_reads_openid_and_listener_in_one_snapshot(self):
        # P0-01: the page status, /qr_code and /api/check_session all come from snapshot().
        with patch.object(web, "openid_refresh_manager", self.manager):
            empty = web.build_runtime_status()
            self.assertEqual(empty["active_sign_count"], 0)
            self.assertFalse(empty["openid_status"]["has_openid"])
            self.manager.current_openid = self.record["openid"]
            pipeline = Mock()
            pipeline.get_status.return_value = {"is_running": True, "active_sign_count": 1}
            with patch.object(web, "get_pipeline", return_value=pipeline) as lookup:
                status = web.build_runtime_status()
            lookup.assert_called_once_with(self.record["openid"])
            self.assertTrue(status["is_running"])
            self.assertTrue(status["openid_status"]["has_openid"])
            self.assertNotIn(self.record["openid"], str(status))

    def test_template_error_is_actionable_without_local_path(self):
        message = web.collector_error_message(RuntimeError('cv template "session" not found. template=D:\\secret\\session.png'))
        self.assertIn("微助教服务号", message)
        self.assertNotIn("secret", message)

    def test_public_state_never_exposes_full_openid(self):
        openid = self.record["openid"]
        self.manager.current_openid = openid
        self.manager.current_url = f"https://v18.teachermate.cn/wechat-pro-ssr/?openid={openid}&from=wzj"
        state = self.manager.get_public_state()
        self.assertTrue(state["has_openid"])
        self.assertNotIn(openid, str(state))
        self.assertIn("from=wzj", state["current_url"])

    def test_rebinding_hosts_and_cross_site_posts_are_rejected(self):
        with patch.object(web, "openid_refresh_manager", self.manager), patch.object(web, "ensure_runtime_initialized"), patch.object(self.manager, "request_refresh_async", return_value=True) as refresh:
            client = web.app.test_client()
            self.assertEqual(client.get("/api/sign_history", base_url="http://evil.example:5000").status_code, 403)
            self.assertEqual(client.get("/api/sign_history", base_url="http://127.0.0.1:5000").status_code, 200)
            for origin in ("http://evil.example", "null", "http://localhost:6000"):
                self.assertEqual(client.post("/api/openid/refresh", headers={"Origin": origin}).status_code, 403)
            refresh.assert_not_called()
            self.assertEqual(client.post("/api/openid/refresh", headers={"Origin": "http://localhost"}).status_code, 202)
            self.assertEqual(client.post("/api/openid/refresh").status_code, 202)


if __name__ == "__main__":
    unittest.main()
