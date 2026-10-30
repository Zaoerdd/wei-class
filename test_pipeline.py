import asyncio
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import ad
import web
from local_state import LocalState


class PipelineReliabilityTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        local = patch.object(web, "local_state", LocalState(self.tmp / "state.sqlite3"))
        local.start()
        self.addCleanup(local.stop)
        self.pipeline = web.Pipeline("a" * 32, {"student_number": "test-123"})
        # The manager is built at import time and reads this machine's saved delay;
        # without pinning it, every sign-in test would wait for the local setting.
        self.addCleanup(setattr, web.openid_refresh_manager, "sign_delay",
                        web.openid_refresh_manager.sign_delay)
        web.openid_refresh_manager.sign_delay = (0, 0)

    def test_listener_survives_an_iteration_error(self):
        calls = []
        async def wait_data():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("database is locked")
            self.pipeline.shutdown_event.set()
            return "暂无签到"
        async def no_sleep(_seconds):
            return None
        with patch.object(self.pipeline, "wait_data", wait_data), \
                patch.object(self.pipeline, "_sleep_unless_stopped", no_sleep, create=True):
            asyncio.run(self.pipeline._main_async())
        self.assertEqual(len(calls), 2)

    def test_watchdog_restarts_dead_listener_only_while_listening(self):
        manager = web.OpenIdRefreshManager(Mock(method_name="cv"), self.tmp / "cache.json")
        manager.current_openid = "a" * 32
        dead = Mock(is_running=False)
        with patch.object(web, "get_pipeline", return_value=dead), \
                patch.object(web, "activate_pipeline_for_openid") as activate:
            manager.listening_enabled = False
            manager._restart_dead_pipeline()
            activate.assert_not_called()
            manager.listening_enabled = True
            manager._restart_dead_pipeline()
            activate.assert_called_once_with("a" * 32, None)
            dead.is_running = True
            manager._restart_dead_pipeline()
            activate.assert_called_once()

    def test_scheduler_loop_runs_watchdog_periodically(self):
        manager = web.OpenIdRefreshManager(Mock(method_name="cv"), self.tmp / "cache.json")
        ticks = [False] * (web.PIPELINE_WATCHDOG_TICKS * 2) + [True]
        with patch.object(manager._stop_event, "wait", side_effect=ticks), \
                patch.object(manager, "_restart_dead_pipeline") as watchdog:
            manager._run_loop()
        self.assertEqual(watchdog.call_count, 2)

    def test_closed_qr_client_is_released_and_reconnects_after_cooldown(self):
        clients = []
        class FakeClient:
            def __init__(self, sign_id, course_id, qr_callback, event_callback):
                self.sign_id, self.is_shutting_down = sign_id, False
                clients.append(self)
            async def start(self):
                return None  # the server closed the connection
        item = {"courseId": 1, "signId": 2, "isQR": 1, "isGPS": 0}
        async def scenario():
            await self.pipeline.process_signatures([item])
            await self.pipeline.process_signatures([item])  # must not open a second client
            await asyncio.gather(*self.pipeline.asyncio_tasks)
            self.assertEqual(len(clients), 1)
            self.assertEqual(self.pipeline.websocket_clients, [])
            await self.pipeline.process_signatures([item])  # still inside the cooldown
            self.assertEqual(len(clients), 1)
            self.pipeline.ws_ended_at[(1, 2)] -= web.WS_RECONNECT_DELAY_SECONDS + 1
            await self.pipeline.process_signatures([item])
            await asyncio.gather(*self.pipeline.asyncio_tasks)
            self.assertEqual(len(clients), 2)
        with patch.object(web, "TeacherMateWebSocketClient", FakeClient), \
                patch.object(web.ad, "creatClientId", return_value="client-id"):
            asyncio.run(scenario())

    def test_shutdown_waits_for_in_flight_sign_in(self):
        def slow_submit(*args, **kwargs):
            time.sleep(0.3)
            return {"signRank": 5}
        item = {"courseId": 1, "signId": 3, "isQR": 0, "isGPS": 0}
        async def scenario():
            await self.pipeline.process_signatures([item])
            await asyncio.sleep(0.05)
            await self.pipeline.shutdown()
        with patch.object(web, "submit_sign", slow_submit):
            asyncio.run(scenario())
        self.assertTrue(self.pipeline.sign_results[(1, 3)]["sign_completed"])

    def test_replacement_listener_waits_for_previous_thread(self):
        release, started = threading.Event(), threading.Event()
        old = threading.Thread(target=release.wait, args=(5,), daemon=True)
        old.start()
        replacement = web.Pipeline("a" * 32, None, predecessor=old)
        async def main():
            started.set()
        try:
            with patch.object(replacement, "_main_async", main):
                replacement.start()
                self.assertFalse(started.wait(0.3))
                release.set()
                self.assertTrue(started.wait(5))
                replacement.thread.join(5)
        finally:
            release.set()
        self.assertFalse(replacement.is_running)

    def test_new_listener_for_same_account_gets_previous_thread(self):
        previous = threading.Thread(target=lambda: None)
        with patch.object(web, "active_pipelines", {}), \
                patch.object(web, "last_pipeline_threads", {"a" * 32: previous}), \
                patch.object(web.Pipeline, "start"):
            pipeline = web.get_or_create_pipeline("a" * 32)
        self.assertIs(pipeline._predecessor, previous)

    def test_stale_sign_state_is_pruned_after_retention(self):
        p = self.pipeline
        p.sign_results = {(1, 1): {}, (1, 2): {}}
        p.qr_auto_attempts = {(1, 1): {}}
        p._prune_stale_sign_state({(1, 1), (1, 2)})
        p.sign_last_seen[(1, 1)] -= web.SIGN_STATE_RETENTION_SECONDS + 1
        p._prune_stale_sign_state({(1, 2)})
        self.assertEqual(set(p.sign_results), {(1, 2)})
        self.assertEqual(p.qr_auto_attempts, {})

    def test_faye_log_never_contains_full_openid(self):
        with patch.object(web.faye_file_logger, "info") as log:
            self.pipeline._log_faye_event({"name": "签到", "courseId": 1, "signId": 2}, {"event_kind": "meta_connect"})
        self.assertNotIn("a" * 32, log.call_args.args[0])

    def test_atomic_write_keeps_old_file_when_write_fails(self):
        path = self.tmp / "latest_openid.json"
        web.write_json_atomic(path, {"openid": "old"})
        with patch("wechat_openid_collector.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                web.write_json_atomic(path, {"openid": "new"})
        self.assertEqual(web.json.loads(path.read_text(encoding="utf-8")), {"openid": "old"})

    def test_sign_in_waits_the_random_delay_then_submits(self):
        manager = Mock(sign_delay=(30, 90))
        waited = []
        async def fake_sleep(seconds):
            waited.append(seconds)
        submit = Mock(return_value={"signRank": 12})
        item = {"courseId": 1, "signId": 4, "isQR": 0, "isGPS": 1, "name": "GPS 签到"}
        with patch.object(web, "openid_refresh_manager", manager), patch.object(web, "submit_sign", submit), \
                patch.object(web.random, "uniform", return_value=45.0), \
                patch.object(self.pipeline, "_sleep_unless_stopped", fake_sleep):
            asyncio.run(self.pipeline.run_common_sign(item))
        self.assertEqual(waited, [45.0])
        submit.assert_called_once()
        self.assertTrue(self.pipeline.sign_results[(1, 4)]["sign_completed"])

    def test_pause_during_delay_submits_nothing(self):
        manager = Mock(sign_delay=(30, 90))
        async def pause_while_waiting(seconds):
            self.pipeline.shutdown_event.set()
        submit = Mock()
        item = {"courseId": 1, "signId": 5, "isQR": 0, "isGPS": 0}
        with patch.object(web, "openid_refresh_manager", manager), patch.object(web, "submit_sign", submit), \
                patch.object(self.pipeline, "_sleep_unless_stopped", pause_while_waiting):
            asyncio.run(self.pipeline.run_common_sign(item))
        submit.assert_not_called()

    def test_pause_during_delay_leaves_the_sign_retryable(self):
        manager = Mock(sign_delay=(30, 90))
        async def pause_while_waiting(seconds):
            self.pipeline.shutdown_event.set()
        item = {"courseId": 1, "signId": 7, "isQR": 0, "isGPS": 0}
        with patch.object(web, "openid_refresh_manager", manager), patch.object(web, "submit_sign"), \
                patch.object(self.pipeline, "_sleep_unless_stopped", pause_while_waiting):
            asyncio.run(self.pipeline.run_common_sign(item))
        # A result stored while waiting would make process_signatures skip this sign
        # after the listener resumes.
        self.assertNotIn((1, 7), self.pipeline.sign_results)

    def test_delay_does_not_block_retry_after_a_failed_submit(self):
        manager = Mock(sign_delay=(30, 90))
        attempts = []
        def failing_submit(*args, **kwargs):
            attempts.append(1)
            raise RuntimeError("network down")
        async def no_sleep(seconds):
            return None
        item = {"courseId": 1, "signId": 6, "isQR": 0, "isGPS": 0}
        async def scenario():
            await self.pipeline.process_signatures([item])
            await asyncio.gather(*self.pipeline.asyncio_tasks)
            await self.pipeline.process_signatures([item])
            await asyncio.gather(*self.pipeline.asyncio_tasks)
        with patch.object(web, "openid_refresh_manager", manager), \
                patch.object(web, "submit_sign", failing_submit), \
                patch.object(self.pipeline, "_sleep_unless_stopped", no_sleep):
            asyncio.run(scenario())
        self.assertEqual(len(attempts), 2)

    def test_no_delay_by_default(self):
        manager = web.OpenIdRefreshManager(Mock(method_name="cv"), self.tmp / "cache.json")
        self.assertEqual(manager.sign_delay, (0, 0))
        manager.set_sign_delay(20, 60)
        self.assertEqual(web.OpenIdRefreshManager(Mock(method_name="cv"), self.tmp / "cache.json").sign_delay, (20, 60))

    def test_sign_delay_api_validates_range(self):
        manager = web.OpenIdRefreshManager(Mock(method_name="cv"), self.tmp / "cache.json")
        with patch.object(web, "openid_refresh_manager", manager), patch.object(web, "ensure_runtime_initialized"):
            client = web.app.test_client()
            for bad in ({"min": 90, "max": 30}, {"min": -1, "max": 5}, {"min": 0, "max": 601}, {"min": "x", "max": 5}):
                self.assertEqual(client.post("/api/sign_delay", json=bad).status_code, 400)
            self.assertEqual(manager.sign_delay, (0, 0))
            self.assertEqual(client.post("/api/sign_delay", json={"min": 30, "max": 90}).status_code, 200)
        self.assertEqual(manager.sign_delay, (30, 90))
        self.assertEqual(manager.get_public_state()["sign_delay"], {"min": 30, "max": 90})

    def test_failed_faye_handshake_raises_clear_error_with_timeout(self):
        response = Mock(text='[{"successful": false, "error": "403::Forbidden"}]')
        with patch.object(ad.requests, "post", return_value=response) as post:
            with self.assertRaisesRegex(RuntimeError, "Faye 握手失败"):
                ad.creatClientId(2, courseId=1)
        self.assertEqual(post.call_args.kwargs["timeout"], 10)


if __name__ == "__main__":
    unittest.main()
