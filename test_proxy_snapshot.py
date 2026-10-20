import json
import logging
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch

import wechat_openid_strategy as strategy

ORIGINAL = strategy.ProxySettingsSnapshot(1, "127.0.0.1:7897", None, 0, "<local>")
CAPTURED = strategy.ProxySettingsSnapshot(1, "127.0.0.1:18080", None, 0, "<local>")
TARGET = "127.0.0.1:18080"


class ProxySnapshotTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.c = strategy.CVWeChatOpenIdCollector(SimpleNamespace(browser_timeout_seconds=10), logging.getLogger("test"))
        self.c.auto_switch_system_proxy = True
        self.c.capture_proxy_host, self.c.capture_proxy_port = "127.0.0.1", 18080
        self.c.proxy_snapshot_path = Path(tmp.name) / "proxy_snapshot.json"
        # Never touch the real registry or network in tests.
        for name in ("_read_system_proxy_settings", "_apply_capture_proxy", "_restore_system_proxy_settings",
                     "ensure_capture_proxy_ready"):
            patcher = patch.object(self.c, name)
            setattr(self, name.strip("_"), patcher.start())
            self.addCleanup(patcher.stop)

    def write_snapshot(self, original=ORIGINAL, target=TARGET):
        self.c.proxy_snapshot_path.write_text(json.dumps({"target_proxy": target, "original": asdict(original)}))

    def test_snapshot_written_before_switch_and_removed_after_restore(self):
        self.read_system_proxy_settings.return_value = ORIGINAL
        def apply(target):
            saved = json.loads(self.c.proxy_snapshot_path.read_text())
            self.assertEqual(saved, {"target_proxy": TARGET, "original": asdict(ORIGINAL)})
        self.apply_capture_proxy.side_effect = apply
        with self.c.temporary_capture_proxy():
            pass
        self.apply_capture_proxy.assert_called_once_with(TARGET)
        self.restore_system_proxy_settings.assert_called_once_with(ORIGINAL)
        self.assertFalse(self.c.proxy_snapshot_path.exists())

    def test_failed_restore_keeps_snapshot_for_later_recovery(self):
        self.read_system_proxy_settings.return_value = ORIGINAL
        self.restore_system_proxy_settings.side_effect = OSError("registry locked")
        with self.c.temporary_capture_proxy():
            pass
        self.assertTrue(self.c.proxy_snapshot_path.exists())

    def test_proxy_left_by_killed_run_is_restored_not_treated_as_original(self):
        self.write_snapshot()
        # First read sees the leftover capture proxy; after recovery the real original is back.
        self.read_system_proxy_settings.side_effect = [CAPTURED, ORIGINAL]
        with self.c.temporary_capture_proxy():
            pass
        self.assertEqual(self.restore_system_proxy_settings.call_args_list, [call(ORIGINAL), call(ORIGINAL)])
        self.apply_capture_proxy.assert_called_once_with(TARGET)
        self.assertFalse(self.c.proxy_snapshot_path.exists())

    def test_recovery_never_overwrites_proxy_user_changed_since(self):
        self.write_snapshot()
        self.read_system_proxy_settings.return_value = strategy.ProxySettingsSnapshot(1, "10.0.0.1:3128", None, 0, None)
        self.assertFalse(self.c.recover_stale_capture_proxy())
        self.restore_system_proxy_settings.assert_not_called()
        self.assertFalse(self.c.proxy_snapshot_path.exists())

    def test_recovery_without_snapshot_or_with_corrupt_snapshot_changes_nothing(self):
        self.assertFalse(self.c.recover_stale_capture_proxy())
        self.c.proxy_snapshot_path.write_text("{not json")
        self.assertFalse(self.c.recover_stale_capture_proxy())
        self.read_system_proxy_settings.assert_not_called()
        self.restore_system_proxy_settings.assert_not_called()
        self.assertTrue(self.c.proxy_snapshot_path.exists())


if __name__ == "__main__":
    unittest.main()
