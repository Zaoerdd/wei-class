import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import cv2
import numpy as np
import wechat_openid_strategy as strategy
from wechat_service_search import WeChatServiceSearch, has_search_focus_border, is_service_search_url, select_all_entry


class ServiceSearchTests(unittest.TestCase):
    def test_topmost_pseudo_handle_uses_pointer_sized_win32_argument(self):
        self.assertIs(strategy.user32.SetWindowPos.argtypes[1], strategy.wintypes.HWND)

    def test_notification_with_same_title_and_class_is_excluded(self):
        c = strategy.CVWeChatOpenIdCollector(SimpleNamespace(browser_timeout_seconds=10), logging.getLogger("test"))
        win32 = Mock()
        win32.EnumWindows.side_effect = lambda callback, _: (callback(1, 0), callback(2, 0))
        win32.GetWindowLongW.side_effect = lambda hwnd, _: 0x188 if hwnd == 1 else 0
        win32.IsWindowVisible.return_value = True
        with patch.object(strategy, "user32", win32), patch.object(c, "_get_class_name", return_value="Qt51514QWindowIcon"), patch.object(c, "_get_window_text", return_value="微信"), patch.object(c, "_get_process_name", return_value="Weixin.exe"), patch.object(c, "_get_window_rect", return_value=strategy.WindowRect(-32000, -32000, -31840, -31972)):
            windows = c._list_wechat_windows()
        self.assertEqual([w.hwnd for w in windows], [2])

    def test_other_chromium_apps_with_wechat_title_are_excluded(self):
        c = strategy.CVWeChatOpenIdCollector(SimpleNamespace(browser_timeout_seconds=10), logging.getLogger("test"))
        win32 = Mock()
        win32.EnumWindows.side_effect = lambda callback, _: (callback(1, 0), callback(2, 0), callback(3, 0))
        win32.GetWindowLongW.return_value = 0
        win32.IsWindowVisible.return_value = True
        processes = {1: "msedge.exe", 2: "Code.exe", 3: "WeChatAppEx.exe"}
        with patch.object(strategy, "user32", win32), patch.object(c, "_get_class_name", return_value="Chrome_WidgetWin_1"), patch.object(c, "_get_window_text", return_value="微信网页版"), patch.object(c, "_get_process_name", side_effect=lambda hwnd: processes[hwnd]), patch.object(c, "_get_window_rect", return_value=strategy.WindowRect(0, 0, 1000, 800)):
            browser = c.find_browser_window()
        self.assertEqual(browser.hwnd, 3)

    def test_close_browser_never_targets_main_window_or_full_screen(self):
        c = strategy.CVWeChatOpenIdCollector(SimpleNamespace(browser_timeout_seconds=10), logging.getLogger("test"))
        main = strategy.WindowInfo(1, "微信", "Qt51514QWindowIcon", strategy.WindowRect(0, 0, 1000, 800))
        with patch.object(c, "find_browser_window", return_value=None), patch.object(c, "activate_window") as activate, patch.object(c, "find_template_location") as find, patch.object(strategy, "user32") as win32:
            c.close_browser(main)
        activate.assert_not_called()
        find.assert_not_called()
        win32.PostMessageW.assert_not_called()

        browser = strategy.WindowInfo(2, "微信", "Chrome_WidgetWin_0", strategy.WindowRect(100, 50, 900, 700))
        c._pyautogui = Mock()
        located = SimpleNamespace(x=880, y=60)
        with patch.object(c, "find_browser_window", return_value=browser), patch.object(c, "activate_window"), patch.object(c, "find_template_location", side_effect=[located, located]) as find, patch.object(strategy.time, "sleep"):
            c.close_browser(main)
        self.assertEqual(find.call_args_list[0].args, ("close", browser.rect))
        c._pyautogui.click.assert_called_once_with(880, 60)

    def test_only_exact_wechat_search_page_is_accepted(self):
        self.assertTrue(is_service_search_url("weixin://resourceid/Search/app.html?query=微助教服务号", "微助教服务号"))
        for url in ["https://example.com/?query=微助教服务号", "weixin://evil/Search/app.html?query=微助教服务号",
                    "weixin://resourceid/Search/app.html?query=微助教", "weixin://resourceid/Search/app.html?query=微助教服务号&query=别的号"]:
            self.assertFalse(is_service_search_url(url, "微助教服务号"))

    def test_entry_needs_service_identity_and_no_ambiguity(self):
        button = lambda name: SimpleNamespace(ControlTypeName="ButtonControl", Name=name)
        service = button("微助教服务号 服务号 账号描述:")
        entry = button("全部(A)")
        self.assertIs(select_all_entry([service, entry], "微助教服务号", "全部"), entry)
        entry.Name = "全部(A) \u00a0"
        self.assertIs(select_all_entry([service, entry], "微助教服务号", "全部"), entry)
        for controls in ([entry], [service, entry, button("全部")], [button("相似微助教服务号 服务号"), entry]):
            self.assertIsNone(select_all_entry(controls, "微助教服务号", "全部"))

    def test_focus_border_not_gray_inactive_search_or_green_text(self):
        blank = np.full((100, 330, 3), 240, dtype=np.int16)
        self.assertFalse(has_search_focus_border(blank, 1.5, cv2))
        green_text = blank.copy()
        cv2.rectangle(green_text, (20, 20), (40, 40), (80, 175, 5), 1)
        self.assertFalse(has_search_focus_border(green_text, 1.5, cv2))
        focused = blank.copy()
        cv2.rectangle(focused, (20, 20), (240, 60), (80, 175, 5), 1)
        self.assertTrue(has_search_focus_border(focused, 1.5, cv2))
        inactive = blank.copy()
        cv2.rectangle(inactive, (20, 20), (240, 60), (150, 150, 150), 1)
        self.assertFalse(has_search_focus_border(inactive, 1.5, cv2))

    def test_focus_timeout_saves_diagnostics_then_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            capture = MagicMock()
            capture.__enter__.return_value.grab.return_value = np.full((130, 420, 3), 240, dtype=np.uint8)
            c = SimpleNamespace(window_dpi_scale=lambda w: 2.0, assert_window_ready=Mock(), base_dir=Path(tmp),
                                _mss=SimpleNamespace(mss=lambda: capture), _numpy=np, _cv2=cv2, logger=Mock())
            window = strategy.WindowInfo(1, "微信", "Qt51514QWindowIcon", strategy.WindowRect(0, 0, 2880, 1704))
            with self.assertRaisesRegex(strategy.OpenIdCollectorError, "搜索框未就绪"):
                WeChatServiceSearch(c)._wait_focus(window, timeout=0)
            self.assertTrue((Path(tmp) / "logs" / "search_focus_fail.png").exists())
            self.assertIn("search box focus not detected", c.logger.warning.call_args.args[0])

    def test_main_window_selection_excludes_larger_browser(self):
        c = strategy.CVWeChatOpenIdCollector(SimpleNamespace(browser_timeout_seconds=10), logging.getLogger("test"))
        main = strategy.WindowInfo(1, "微信", "Qt51514QWindowIcon", strategy.WindowRect(-32000, -32000, -31840, -31972))
        browser = strategy.WindowInfo(2, "微信", "Chrome_WidgetWin_0", strategy.WindowRect(0, 0, 2000, 1500))
        with patch.object(c, "_list_wechat_windows", return_value=[browser, main]):
            self.assertIs(c.find_wechat_window(), main)
        with patch.object(c, "_list_wechat_windows", return_value=[browser]):
            with self.assertRaises(strategy.OpenIdCollectorError):
                c.find_wechat_window()

    def test_restore_refreshes_coordinates_and_rejects_lost_focus(self):
        c = strategy.CVWeChatOpenIdCollector(SimpleNamespace(browser_timeout_seconds=10), logging.getLogger("test"))
        old = strategy.WindowInfo(1, "微信", "Qt51514QWindowIcon", strategy.WindowRect(0, 0, 160, 28))
        restored = strategy.WindowRect(200, 100, 1200, 900)
        win32 = Mock()
        win32.IsIconic.return_value = True
        win32.GetWindowLongW.return_value = 0
        win32.GetForegroundWindow.return_value = 1
        with patch.object(strategy, "user32", win32), patch.object(c, "_get_window_rect", return_value=restored), patch.object(strategy.time, "sleep"):
            c.activate_window(old)
            self.assertEqual(old.rect, restored)
            win32.ShowWindow.assert_called_once_with(1, strategy.SW_RESTORE)
            win32.GetForegroundWindow.return_value = 2
            with self.assertRaisesRegex(strategy.OpenIdCollectorError, "失去焦点"):
                c.assert_window_ready(old)

    def test_activation_click_keeps_window_on_top_and_restores_on_failure(self):
        c = strategy.CVWeChatOpenIdCollector(SimpleNamespace(browser_timeout_seconds=10), logging.getLogger("test"))
        window = strategy.WindowInfo(1, "微信", "Qt51514QWindowIcon", strategy.WindowRect(300, 200, 1300, 1000))
        win32 = Mock()
        win32.GetWindowLongW.return_value = 0
        win32.GetForegroundWindow.return_value = 2
        win32.WindowFromPoint.return_value = 10
        win32.GetAncestor.return_value = 1
        c._pyautogui = Mock()
        def click(x, y):
            self.assertEqual((x, y), (900, 218))
            self.assertEqual(win32.SetWindowPos.call_args.args[1], strategy.HWND_TOPMOST)
            raise RuntimeError("input failed")
        c._pyautogui.click.side_effect = click
        with patch.object(strategy, "user32", win32), patch.object(c, "window_dpi_scale", return_value=1.5), patch.object(c, "_get_window_rect", return_value=window.rect), patch.object(strategy.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "input failed"):
                c.activate_window(window)
        self.assertEqual(win32.SetWindowPos.call_args.args[1], strategy.HWND_NOTOPMOST)

    def test_no_foreground_window_is_reported_as_lost_focus_not_crash(self):
        # uiautomation makes GetForegroundWindow return None when no window is in front.
        c = strategy.CVWeChatOpenIdCollector(SimpleNamespace(browser_timeout_seconds=10), logging.getLogger("test"))
        window = strategy.WindowInfo(1, "微信", "Qt51514QWindowIcon", strategy.WindowRect(300, 200, 1300, 1000))
        win32 = Mock()
        win32.GetForegroundWindow.return_value = None
        with patch.object(strategy, "user32", win32):
            with self.assertRaisesRegex(strategy.OpenIdCollectorError, "失去焦点"):
                c.assert_window_ready(window)
        with patch.object(strategy, "user32", win32), patch.object(strategy.time, "sleep"), \
                patch.object(c, "_get_window_rect", return_value=window.rect), patch.object(c, "window_dpi_scale", return_value=1.0):
            win32.GetWindowLongW.return_value = 0
            win32.WindowFromPoint.return_value = None
            c._pyautogui = Mock()
            with self.assertRaisesRegex(strategy.OpenIdCollectorError, "失去焦点"):
                c.activate_window(window)
        c._pyautogui.click.assert_not_called()

    def test_move_during_operation_rejects_stale_coordinates(self):
        c = strategy.CVWeChatOpenIdCollector(SimpleNamespace(browser_timeout_seconds=10), logging.getLogger("test"))
        window = strategy.WindowInfo(1, "微信", "Qt51514QWindowIcon", strategy.WindowRect(300, 200, 1300, 1000))
        with patch.object(strategy, "user32") as win32, patch.object(c, "_get_window_rect", return_value=strategy.WindowRect(400, 300, 1400, 1100)):
            win32.GetForegroundWindow.return_value = 1
            with self.assertRaisesRegex(strategy.OpenIdCollectorError, "位置或尺寸已变化"):
                c.assert_window_ready(window)


if __name__ == "__main__":
    unittest.main()
