import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import web
from wechat_openid_strategy import ProxySettingsSnapshot


class ProxyHealthTests(unittest.TestCase):
    def _collector(self, snapshot):
        collector = Mock()
        collector.method_name = "cv"
        collector._read_system_proxy_settings.return_value = snapshot
        return collector

    def test_proxy_left_on_capture_port_is_a_failure(self):
        # The classic leftover: capture proxy applied, mitmproxy already gone.
        snapshot = ProxySettingsSnapshot(1, "127.0.0.1:18080", None, 0, None)
        with patch.object(web, "collector", self._collector(snapshot)), \
                patch.object(web, "_port_is_listening", return_value=False):
            check = web._check_proxy_health(18080)
        self.assertEqual(check["status"], "fail")
        self.assertIn("127.0.0.1:18080", check["detail"])
        self.assertTrue(check["hint"])

    def test_proxy_on_capture_port_while_capturing_is_normal(self):
        snapshot = ProxySettingsSnapshot(1, "127.0.0.1:18080", None, 0, None)
        with patch.object(web, "collector", self._collector(snapshot)), \
                patch.object(web, "_port_is_listening", return_value=True):
            check = web._check_proxy_health(18080)
        self.assertEqual(check["status"], "ok")

    def test_pac_url_is_checked_too(self):
        snapshot = ProxySettingsSnapshot(0, None, "http://127.0.0.1:18080/proxy.pac", 0, None)
        with patch.object(web, "collector", self._collector(snapshot)), \
                patch.object(web, "_port_is_listening", return_value=False):
            check = web._check_proxy_health(18080)
        self.assertEqual(check["status"], "fail")

    def test_untouched_proxy_passes(self):
        snapshot = ProxySettingsSnapshot(0, None, None, 0, None)
        with patch.object(web, "collector", self._collector(snapshot)), \
                patch.object(web, "_port_is_listening", return_value=False):
            check = web._check_proxy_health(18080)
        self.assertEqual(check["status"], "ok")

    def test_registry_failure_does_not_raise(self):
        collector = Mock()
        collector.method_name = "cv"
        collector._read_system_proxy_settings.side_effect = OSError("registry denied")
        with patch.object(web, "collector", collector):
            check = web._check_proxy_health(18080)
        self.assertEqual(check["status"], "warn")


class TemplateHealthTests(unittest.TestCase):
    def _collector(self, base: Path, override: Path):
        collector = Mock()
        collector.method_name = "cv"
        collector.template_dir = base
        collector.template_override_dir = override
        collector.template_names = {"session": "session.png", "menu_item": "all_item.png"}
        return collector

    def test_missing_template_is_named(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, override = Path(tmp) / "base", Path(tmp) / "override"
            base.mkdir()
            (base / "session.png").write_bytes(b"x")
            with patch.object(web, "collector", self._collector(base, override)):
                check = web._check_template_health()
        self.assertEqual(check["status"], "fail")
        self.assertIn("all_item.png", check["detail"])
        self.assertNotIn("session.png", check["detail"])

    def test_override_directory_satisfies_the_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, override = Path(tmp) / "base", Path(tmp) / "override"
            base.mkdir()
            override.mkdir()
            (base / "session.png").write_bytes(b"x")
            (override / "all_item.png").write_bytes(b"x")
            with patch.object(web, "collector", self._collector(base, override)):
                check = web._check_template_health()
        self.assertEqual(check["status"], "ok")

    def test_other_collection_methods_skip_the_check(self):
        collector = Mock()
        collector.method_name = "uiautomation"
        with patch.object(web, "collector", collector):
            check = web._check_template_health()
        self.assertEqual(check["status"], "skip")


class HealthReportTests(unittest.TestCase):
    def _report(self, state, pipeline):
        manager = Mock()
        manager.snapshot.return_value = (state, pipeline)
        with patch.object(web, "openid_refresh_manager", manager), \
                patch.object(web, "_check_proxy_health", return_value=web._health_item("proxy", "系统代理", "ok", "ok")), \
                patch.object(web, "_check_certificate_health", return_value=web._health_item("cert", "抓包证书", "ok", "ok")), \
                patch.object(web, "_check_template_health", return_value=web._health_item("templates", "界面模板", "ok", "ok")), \
                patch.object(web, "_check_wechat_health", return_value=web._health_item("wechat", "微信进程", "ok", "ok")), \
                patch.object(web, "_check_storage_health", return_value=web._health_item("storage", "日志与磁盘", "ok", "ok")):
            return web.build_health_report()

    def test_ready_environment_is_all_ok(self):
        state = {"has_openid": True, "openid_masked": "abc123***", "current_source": "file",
                 "listening_enabled": True, "last_error": None}
        report = self._report(state, Mock())
        self.assertEqual(report["overall"], "ok")
        self.assertTrue(all(check["status"] == "ok" for check in report["checks"]))

    def test_missing_openid_is_reported_as_a_failure(self):
        state = {"has_openid": False, "listening_enabled": True, "last_error": None}
        report = self._report(state, Mock())
        self.assertEqual(report["overall"], "fail")
        openid = next(check for check in report["checks"] if check["id"] == "openid")
        self.assertEqual(openid["status"], "fail")
        self.assertTrue(openid["hint"])

    def test_listening_without_pipeline_is_a_warning(self):
        state = {"has_openid": True, "openid_masked": "abc123***", "current_source": "file",
                 "listening_enabled": True, "last_error": None}
        report = self._report(state, None)
        self.assertEqual(report["overall"], "warn")
        listener = next(check for check in report["checks"] if check["id"] == "listener")
        self.assertEqual(listener["status"], "warn")

    def test_paused_listening_is_a_warning_not_a_failure(self):
        state = {"has_openid": True, "openid_masked": "abc123***", "current_source": "file",
                 "listening_enabled": False, "last_error": None}
        report = self._report(state, Mock())
        self.assertEqual(report["overall"], "warn")

    def test_last_error_is_surfaced(self):
        state = {"has_openid": True, "openid_masked": "abc123***", "current_source": "file",
                 "listening_enabled": True, "last_error": "搜索页没有打开"}
        report = self._report(state, Mock())
        error = next(check for check in report["checks"] if check["id"] == "last_error")
        self.assertIn("搜索页没有打开", error["detail"])


if __name__ == "__main__":
    unittest.main()
