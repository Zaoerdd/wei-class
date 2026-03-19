import io
import json
import os
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import web


class RuntimeDiagnosticsSafetyTests(unittest.TestCase):
    def test_health_endpoint_does_not_start_runtime(self) -> None:
        starts = []
        dummy_manager = SimpleNamespace(start=lambda: starts.append("start"))

        with patch.object(web, "runtime_initialized", False), patch.object(
            web, "openid_refresh_manager", dummy_manager
        ), patch.object(web, "build_health_report", return_value={"ok": True}):
            response = web.app.test_client().get("/api/health")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json(), {"ok": True})
            self.assertEqual(starts, [])
            self.assertFalse(web.runtime_initialized)

    def test_support_bundle_endpoint_does_not_start_runtime(self) -> None:
        starts = []
        dummy_manager = SimpleNamespace(start=lambda: starts.append("start"))
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("manifest.json", "{}")
        archive.seek(0)

        with patch.object(web, "runtime_initialized", False), patch.object(
            web, "openid_refresh_manager", dummy_manager
        ), patch.object(
            web, "build_support_bundle_archive", return_value=(archive, "bundle.zip")
        ):
            response = web.app.test_client().get("/api/support_bundle")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.mimetype, "application/zip")
            self.assertEqual(starts, [])
            self.assertFalse(web.runtime_initialized)

    def test_support_bundle_redacts_proxy_credentials(self) -> None:
        runtime_state = {
            "schema_version": 1,
            "generated_at": "2026-03-20T12:34:56+08:00",
            "openid_status": {
                "collector_method": "cv",
                "current_source": "collector",
                "openid": "1234567890abcdef1234567890abcdef",
                "openid_masked": "123456***cdef",
                "used_file_fallback": False,
                "is_refreshing": False,
                "last_refresh_at": "2026-03-20T12:00:00+08:00",
                "next_refresh_at": "2026-03-20T14:00:00+08:00",
                "last_error": None,
            },
            "pipeline_status": {
                "has_pipeline": True,
                "is_running": True,
                "message": "collector ready",
                "success": 1,
                "active_sign_count": 0,
                "active_signs": [],
                "current_sign": None,
                "result_meta": {
                    "qr_ready": False,
                    "result_ready": False,
                    "sign_completed": False,
                },
            },
            "summary": {
                "session_valid": True,
                "has_openid": True,
                "has_pipeline": True,
                "collector_method": "cv",
                "current_source": "collector",
                "is_refreshing": False,
                "used_file_fallback": False,
                "active_sign_count": 0,
                "has_active_signs": False,
                "qr_ready": False,
                "sign_completed": False,
                "result_ready": False,
                "has_error": False,
                "status_message": "collector ready",
            },
        }
        health_report = {
            "generated_at": "2026-03-20T12:34:57+08:00",
            "runtime_state": runtime_state,
            "summary": {
                "overall_status": "ready",
                "tone": "success",
                "title": "部署环境已就绪",
                "description": "demo",
                "next_action": "返回首页继续使用",
                "counts": {"pass": 1, "warn": 0, "fail": 0, "skip": 0},
                "collector_method": "cv",
            },
            "checks": [],
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            logs_dir = tmp / "logs"
            logs_dir.mkdir()
            collector_log = logs_dir / "collector.log"
            collector_log.write_text("collector ready", encoding="utf-8")
            faye_log = logs_dir / "faye_history.log"
            faye_log.write_text("faye ready", encoding="utf-8")
            openid_cache = logs_dir / "latest_openid.json"
            openid_cache.write_text(
                json.dumps(
                    {
                        "openid": "1234567890abcdef1234567890abcdef",
                        "source": "collector",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            collector_output = logs_dir / "collector_output.json"
            collector_output.write_text(
                json.dumps({"openid": "1234567890abcdef1234567890abcdef"}, ensure_ascii=False),
                encoding="utf-8",
            )
            mitm_result = logs_dir / "mitm_openid_result.txt"
            mitm_result.write_text("openid captured", encoding="utf-8")
            local_config = tmp / "local_config.json"
            local_config.write_text(
                json.dumps({"pushplus_token": "demo-token"}, ensure_ascii=False),
                encoding="utf-8",
            )
            template_dir = tmp / "cv_templates"
            override_dir = tmp / "cv_templates_local"
            template_dir.mkdir()
            override_dir.mkdir()
            (template_dir / "session.png").write_bytes(b"session")

            with patch.object(web, "build_runtime_state", return_value=runtime_state), patch.object(
                web, "build_health_report", return_value=health_report
            ), patch.object(
                web,
                "build_frontend_status_payload",
                return_value={
                    "openid_status": runtime_state["openid_status"],
                    "runtime_summary": runtime_state["summary"],
                },
            ), patch.object(
                web,
                "inspect_capture_proxy_listener_state",
                return_value={
                    "host": "127.0.0.1",
                    "port": 8080,
                    "reachable": True,
                    "mitmdump_path": "C:/demo/mitmdump.exe",
                    "mitmdump_exists": True,
                    "result_path": str(mitm_result),
                    "result_file_exists": True,
                },
            ), patch.object(
                web,
                "read_system_proxy_settings",
                return_value={
                    "proxy_enable": 0,
                    "proxy_server": None,
                    "effective_proxy_server": None,
                },
            ), patch.object(
                web,
                "inspect_mitm_certificate_state",
                return_value={
                    "cert_path": "C:/demo/mitmproxy-ca-cert.cer",
                    "exists": True,
                    "trusted": True,
                },
            ), patch.object(
                web,
                "collector",
                SimpleNamespace(
                    method_name="cv",
                    template_dir=template_dir,
                    template_override_dir=override_dir,
                    template_names={
                        "session": "session.png",
                        "menu_button": "student_button.png",
                    },
                ),
            ), patch.object(web, "COLLECTOR_LOG_PATH", collector_log), patch.object(
                web, "FAYE_LOG_PATH", faye_log
            ), patch.object(
                web, "OPENID_CACHE_PATH", openid_cache
            ), patch.object(
                web, "LOCAL_CONFIG_PATH", local_config
            ), patch.object(
                web, "COLLECTOR_OUTPUT_PATH", collector_output
            ), patch.dict(
                os.environ,
                {"HTTP_PROXY": "http://user:pass@example.com:8080"},
                clear=False,
            ):
                archive, filename = web.build_support_bundle_archive()

            self.assertTrue(filename.endswith(".zip"))
            with zipfile.ZipFile(archive) as bundle:
                environment_snapshot = json.loads(
                    bundle.read("config/environment_snapshot.json").decode("utf-8")
                )

            proxy_value = environment_snapshot["environment"]["HTTP_PROXY"]
            self.assertNotIn("user:pass", proxy_value)
            self.assertEqual(proxy_value, "http://[redacted]@example.com:8080")

    def test_template_status_endpoint_reports_sources_and_missing_templates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            temp_root = Path(tmp_dir)
            template_dir = temp_root / "cv_templates"
            override_dir = temp_root / "cv_templates_local"
            template_dir.mkdir()
            override_dir.mkdir()

            (template_dir / "session.png").write_bytes(b"session")
            (template_dir / "all_item.png").write_bytes(b"menu-item")
            (override_dir / "student_button.png").write_bytes(b"menu-button")

            dummy_collector = SimpleNamespace(
                method_name="cv",
                template_dir=template_dir,
                template_override_dir=override_dir,
                template_names={
                    "session": "session.png",
                    "menu_button": "student_button.png",
                    "menu_item": "all_item.png",
                    "close": "close_button.png",
                },
            )

            with patch.object(web, "collector", dummy_collector):
                response = web.app.test_client().get("/api/template_status")

            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload["counts"]["total"], 4)
            self.assertEqual(payload["counts"]["default"], 2)
            self.assertEqual(payload["counts"]["override"], 1)
            self.assertEqual(payload["counts"]["missing"], 1)
            self.assertEqual(payload["missing_roles"], ["close"])
            self.assertEqual(payload["override_roles"], ["menu_button"])

            templates = {item["role"]: item for item in payload["templates"]}
            self.assertEqual(templates["session"]["source"], "default")
            self.assertEqual(templates["menu_button"]["source"], "override")
            self.assertEqual(templates["close"]["source"], "missing")

    def test_template_capture_endpoint_returns_capture_result(self) -> None:
        capture_calls = []
        dummy_collector = SimpleNamespace(
            method_name="cv",
            capture_local_templates=lambda **kwargs: capture_calls.append(kwargs)
            or {
                "chat_state": kwargs["chat_state"],
                "saved_count": 4,
                "override_dir": "D:/wei-class/cv_templates_local",
                "saved_templates": [
                    {
                        "role": "session",
                        "path": "D:/wei-class/cv_templates_local/session.png",
                        "capture_source": "matched-template",
                        "image_size": {"width": 180, "height": 60},
                    }
                ],
            },
        )

        with patch.object(web, "collector", dummy_collector), patch.object(
            web,
            "openid_refresh_manager",
            SimpleNamespace(is_refreshing=False),
        ), patch.object(
            web,
            "build_template_snapshot",
            return_value={"counts": {"total": 4, "override": 4}},
        ):
            response = web.app.test_client().post("/api/template_capture", json={"chat_state": "visible"})

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["capture"]["saved_count"], 4)
        self.assertEqual(payload["capture"]["saved_templates"][0]["role"], "session")
        self.assertEqual(capture_calls, [{"chat_state": "visible", "overwrite": True}])

    def test_template_capture_endpoint_rejects_when_refresh_is_running(self) -> None:
        dummy_collector = SimpleNamespace(method_name="cv", capture_local_templates=lambda **kwargs: None)

        with patch.object(web, "collector", dummy_collector), patch.object(
            web,
            "openid_refresh_manager",
            SimpleNamespace(is_refreshing=True),
        ):
            response = web.app.test_client().post("/api/template_capture", json={"chat_state": "open"})

        self.assertEqual(response.status_code, 409)
        payload = response.get_json()
        self.assertFalse(payload["success"])
        self.assertIn("自动刷新 OpenID", payload["message"])

    def test_template_capture_endpoint_rejects_when_refresh_lock_is_busy(self) -> None:
        dummy_collector = SimpleNamespace(method_name="cv", capture_local_templates=lambda **kwargs: None)
        refresh_lock = threading.Lock()
        self.assertTrue(refresh_lock.acquire(blocking=False))

        try:
            with patch.object(web, "collector", dummy_collector), patch.object(
                web,
                "openid_refresh_manager",
                SimpleNamespace(is_refreshing=False, _refresh_lock=refresh_lock),
            ):
                response = web.app.test_client().post("/api/template_capture", json={"chat_state": "open"})
        finally:
            refresh_lock.release()

        self.assertEqual(response.status_code, 409)
        payload = response.get_json()
        self.assertFalse(payload["success"])
        self.assertIn("准备执行", payload["message"])

    def test_template_capture_click_endpoint_returns_capture_result(self) -> None:
        capture_calls = []
        dummy_collector = SimpleNamespace(
            method_name="cv",
            capture_template_from_user_click=lambda **kwargs: capture_calls.append(kwargs)
            or {
                "role": kwargs["role"],
                "path": "D:/wei-class/cv_templates_local/menu_item.png",
                "capture_source": "user-click",
                "image_size": {"width": 220, "height": 100},
            },
        )

        with patch.object(web, "collector", dummy_collector), patch.object(
            web,
            "openid_refresh_manager",
            SimpleNamespace(is_refreshing=False),
        ), patch.object(
            web,
            "build_template_snapshot",
            return_value={"counts": {"total": 4, "override": 2}},
        ):
            response = web.app.test_client().post("/api/template_capture_click", json={"role": "menu_item"})

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["capture"]["role"], "menu_item")
        self.assertIn("/api/template_capture_preview/menu_item", payload["capture"]["preview_url"])
        self.assertEqual(
            capture_calls,
            [{"role": "menu_item", "target": "main", "overwrite": True}],
        )

    def test_template_capture_preview_endpoint_returns_local_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            override_dir = Path(tmp_dir) / "cv_templates_local"
            override_dir.mkdir()
            preview_path = override_dir / "session.png"
            preview_path.write_bytes(b"preview-image")

            dummy_collector = SimpleNamespace(
                template_override_dir=override_dir,
                template_names={"session": "session.png"},
            )

            with patch.object(web, "collector", dummy_collector):
                response = web.app.test_client().get("/api/template_capture_preview/session")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"preview-image")


if __name__ == "__main__":
    unittest.main()
