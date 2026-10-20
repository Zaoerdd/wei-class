"""Navigate to TeacherMate through WeChat's verified search entry."""
import ctypes
import re
import time
from urllib.parse import parse_qs, urlsplit

from wechat_openid_collector import OpenIdCollectorError, WeChatOpenIdCollector


def is_service_search_url(url, session_name):
    parsed = urlsplit(url or "")
    return (parsed.scheme == "weixin" and parsed.netloc == "resourceid"
            and parsed.path == "/Search/app.html"
            and parse_qs(parsed.query).get("query") == [session_name])


def select_all_entry(controls, session_name, entry_name):
    """Require the service identity and exactly one matching menu button."""
    buttons = [c for c in controls if c.ControlTypeName == "ButtonControl"]
    services = [c for c in buttons if re.sub(r"\s+", " ", c.Name).strip().startswith(session_name + " 服务号")]
    entries = [c for c in buttons if re.fullmatch(re.escape(entry_name) + r"(?:\([A-Za-z]\))?", c.Name.strip())]
    return entries[0] if len(services) == 1 and len(entries) == 1 else None


class WeChatServiceSearch:
    def __init__(self, collector):
        self.c = collector

    def _grab_focus_region(self, window):
        c = self.c
        r = window.rect
        scale = c.window_dpi_scale(window)
        region = {"left": r.left + int(60 * scale), "top": r.top + int(15 * scale),
                  "width": int(210 * scale), "height": int(65 * scale)}
        with c._mss.mss() as capture:
            return region, c._numpy.array(capture.grab(region))[:, :, :3]

    def _field_focused(self, window):
        _, pixels = self._grab_focus_region(window)
        return has_search_focus_border(pixels.astype("int16"), self.c.window_dpi_scale(window), self.c._cv2)

    def _wait_focus(self, window, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.c.assert_window_ready(window)
            if self._field_focused(window):
                return
            time.sleep(.25)
        self._save_focus_diagnostics(window)
        raise OpenIdCollectorError("微信搜索框未就绪，已停止输入；请确认微信已登录且没有弹窗遮挡")

    def _save_focus_diagnostics(self, window):
        # Keep what the detector saw, so a timeout shows whether the field never got
        # focus or the border was missed. logs/*.png stays local (git-ignored).
        c = self.c
        try:
            scale = c.window_dpi_scale(window)
            region, pixels = self._grab_focus_region(window)
            path = c.base_dir / "logs" / "search_focus_fail.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            c._cv2.imwrite(str(path), c._numpy.ascontiguousarray(pixels))
            boxes = [(round(w / scale), round(h / scale))
                     for w, h in green_component_sizes(pixels.astype("int16"), c._cv2)]
            c.logger.warning("search box focus not detected: rect=%s maximized=%s scale=%.2f region=%s "
                             "green_boxes(logical w,h)=%s crop=%s", window.rect,
                             bool(ctypes.windll.user32.IsZoomed(window.hwnd)), scale, region, boxes[:5], path)
        except Exception as exc:
            c.logger.warning("failed to save search focus diagnostics: %s", exc)

    def open(self, window):
        import uiautomation as auto
        c = self.c
        c.logger.info("search start: rect=%s maximized=%s scale=%.2f", window.rect,
                      bool(ctypes.windll.user32.IsZoomed(window.hwnd)), c.window_dpi_scale(window))
        with auto.UIAutomationInitializerInThread():
            c.assert_window_ready(window)
            c._pyautogui.hotkey("ctrl", "f")
            self._wait_focus(window)
            # Only clear a verified search field, never a chat draft or clipboard.
            c.assert_window_ready(window)
            c._pyautogui.hotkey("ctrl", "a")
            for char in c.config.session_name:
                c.assert_window_ready(window)
                if not self._field_focused(window):
                    raise OpenIdCollectorError("微信搜索框失去焦点，已停止输入")
                auto.SendUnicodeChar(char, charMode=True)
            time.sleep(.6)
            c.assert_window_ready(window)
            if not self._field_focused(window):
                raise OpenIdCollectorError("微信搜索框失去焦点，已停止搜索")
            c._pyautogui.press("down")
            time.sleep(.2)
            c.assert_window_ready(window)
            if not self._field_focused(window):
                raise OpenIdCollectorError("微信搜索框失去焦点，已停止搜索")
            c._pyautogui.press("enter")
            self._open_all_entry(auto)

    def _open_all_entry(self, auto):
        c = self.c
        reader = WeChatOpenIdCollector(c.config, c.logger)
        deadline = time.monotonic() + max(15, c.config.browser_timeout_seconds)
        while time.monotonic() < deadline:
            browser = c.find_browser_window()
            if browser is not None:
                pane = auto.ControlFromHandle(browser.hwnd)
                url = reader.try_get_browser_url(pane)
                if is_service_search_url(url, c.config.session_name):
                    document = reader.find_browser_document(pane)
                    controls = [control for control, _ in auto.WalkControl(document, maxDepth=12)
                                if reader.is_visible(control)]
                    entry = select_all_entry(controls, c.config.session_name, c.config.menu_item_prefix)
                    if entry is not None:
                        c.assert_window_ready(browser)
                        entry.Click(simulateMove=False)
                        c.logger.info("opened TeacherMate from verified WeChat search result")
                        return
            time.sleep(.3)
        raise OpenIdCollectorError("微信搜索页未出现微助教的“全部”入口；可能是网络未加载或微信页面已变化")


def green_component_sizes(pixels, cv2):
    blue, green, red = pixels[:, :, 0], pixels[:, :, 1], pixels[:, :, 2]
    mask = ((green > 110) & (green > red + 30) & (green > blue + 20)).astype("uint8")
    return [(int(width), int(height)) for x, y, width, height, area in cv2.connectedComponentsWithStats(mask)[2][1:]]


def has_search_focus_border(pixels, scale, cv2):
    # WeChat 4.x draws a green rectangular outline only around the focused search field.
    return any(80 * scale < width < 280 * scale and 15 * scale < height < 45 * scale
               for width, height in green_component_sizes(pixels, cv2))
