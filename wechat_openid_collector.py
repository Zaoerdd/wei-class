from __future__ import annotations

import argparse
import ctypes
import json
import logging
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable, Iterable, List, Optional

import uiautomation as auto

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_PATH = BASE_DIR / "logs" / "latest_openid.json"
DEFAULT_LOG_PATH = BASE_DIR / "logs" / "wechat_openid_collector.log"
OPENID_PATTERN = re.compile(r"[?&]openid=([^&#]+)")

WECHAT_WINDOW_CLASS = "WeChatMainWndForPC"
WECHAT_BROWSER_PANE_CLASS = "Chrome_WidgetWin_0"
WECHAT_BROWSER_DOCUMENT_CLASS = "Chrome_RenderWidgetHostHWND"

SW_RESTORE = 9
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_SHOWWINDOW = 0x0040

user32 = ctypes.windll.user32


class OpenIdCollectorError(RuntimeError):
    """Raised when the WeChat automation flow cannot complete safely."""


@dataclass
class CollectorConfig:
    session_name: str
    menu_button_prefix: str
    menu_item_prefix: str
    interval_hours: float
    control_timeout_seconds: float
    browser_timeout_seconds: float
    output_path: Path
    log_path: Path


def build_logger(log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("wechat_openid_collector")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=1 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


class WeChatOpenIdCollector:
    def __init__(self, config: CollectorConfig, logger: logging.Logger):
        self.config = config
        self.logger = logger
        auto.SetGlobalSearchTimeout(2)

    def run_forever(self) -> None:
        interval_seconds = max(1.0, self.config.interval_hours * 3600.0)
        self.logger.info("collector loop started, interval=%.2f hours", self.config.interval_hours)

        while True:
            started_at = time.monotonic()
            try:
                result = self.run_once()
                self.logger.info(
                    "openid captured successfully: %s",
                    self._mask_openid(result["openid"]),
                )
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                self.logger.exception("collector run failed: %s", exc)

            elapsed = time.monotonic() - started_at
            sleep_seconds = max(1.0, interval_seconds - elapsed)
            self.logger.info("next run in %.0f seconds", sleep_seconds)
            time.sleep(sleep_seconds)

    def run_once(self) -> dict:
        self.logger.info("starting one collection run")
        stale_browser = self.find_browser_pane()
        if stale_browser:
            stale_url = self.try_get_browser_url(stale_browser) or ""
            if "teachermate" in stale_url:
                self.logger.warning("closing stale teachermate browser window before continuing")
                self.close_browser_pane(stale_browser)
            else:
                raise OpenIdCollectorError("detected an existing WeChat browser window; skipped to avoid interrupting manual use")

        wechat_window = self.require_wechat_window()
        self.activate_window(wechat_window)
        self.open_target_session(wechat_window)
        self.open_student_all_menu(wechat_window)

        browser_pane = None
        try:
            browser_pane = self.wait_for(
                self.find_browser_pane,
                "WeChat built-in browser",
                self.config.control_timeout_seconds,
            )
            url = self.wait_for_browser_url(browser_pane)
            openid = self.extract_openid(url)
            result = self.build_result(openid, url)
            self.write_result(result)
            return result
        finally:
            if browser_pane is None:
                browser_pane = self.find_browser_pane()
            if browser_pane:
                try:
                    self.close_browser_pane(browser_pane)
                except Exception as exc:
                    self.logger.warning("failed to close WeChat browser window cleanly: %s", exc)

    def build_result(self, openid: str, url: str) -> dict:
        return {
            "openid": openid,
            "url": url,
            "session_name": self.config.session_name,
            "menu_path": [self.config.menu_button_prefix, self.config.menu_item_prefix],
            "captured_at": datetime.now().astimezone().isoformat(),
        }

    def write_result(self, result: dict) -> None:
        self.config.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.config.output_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.logger.info(
            "result written to %s (%s)",
            self.config.output_path,
            self._mask_openid(result["openid"]),
        )

    def require_wechat_window(self):
        window = self.find_wechat_window()
        if not window:
            raise OpenIdCollectorError("WeChat window not found")
        return window

    def activate_window(self, window) -> None:
        hwnd = int(self.safe_get(lambda: window.NativeWindowHandle, 0))
        if hwnd <= 0:
            raise OpenIdCollectorError("invalid WeChat window handle")

        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
        user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
        user32.SetForegroundWindow(hwnd)
        window.SetActive()
        window.SetFocus()
        time.sleep(0.5)

    def open_target_session(self, wechat_window) -> None:
        session_control = self.wait_for(
            lambda: self.find_session_control(wechat_window, self.config.session_name),
            f'chat session "{self.config.session_name}" in the left sidebar (pin it first if needed)',
            self.config.control_timeout_seconds,
        )
        session_control.Click(simulateMove=False)
        time.sleep(0.8)

    def open_student_all_menu(self, wechat_window) -> None:
        student_button = self.wait_for(
            lambda: self.find_bottom_button(wechat_window, self.config.menu_button_prefix),
            f'bottom button "{self.config.menu_button_prefix}"',
            self.config.control_timeout_seconds,
        )
        student_button.Click(simulateMove=False)
        time.sleep(0.5)

        all_menu_item = self.wait_for(
            lambda: self.find_menu_item(self.config.menu_item_prefix),
            f'menu item "{self.config.menu_item_prefix}"',
            self.config.control_timeout_seconds,
        )
        all_menu_item.Click(simulateMove=False)

    def find_wechat_window(self):
        root = auto.GetRootControl()
        for control, _ in auto.WalkControl(root, maxDepth=2):
            if self.safe_get(lambda: control.ControlTypeName) != "WindowControl":
                continue
            if self.safe_get(lambda: control.ClassName) != WECHAT_WINDOW_CLASS:
                continue
            if self.is_visible(control):
                return control
        return None

    def find_session_control(self, wechat_window, session_name: str):
        sidebar_limit = self.sidebar_limit(wechat_window)
        candidates = []

        for control, _ in auto.WalkControl(wechat_window, maxDepth=14):
            if not self.is_visible(control):
                continue
            rect = self.safe_get(lambda: control.BoundingRectangle)
            if rect.left >= sidebar_limit:
                continue

            name = self.safe_get(lambda: control.Name, "")
            control_type = self.safe_get(lambda: control.ControlTypeName, "")
            if control_type == "ListItemControl" and session_name in name:
                candidates.append((0, rect.top, control))
            elif control_type == "ButtonControl" and name == session_name:
                candidates.append((1, rect.top, control))

        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], item[1]))
        return candidates[0][2]

    def find_bottom_button(self, wechat_window, label_prefix: str):
        window_rect = self.safe_get(lambda: wechat_window.BoundingRectangle)
        for control, _ in auto.WalkControl(wechat_window, maxDepth=16):
            if not self.is_visible(control):
                continue
            if self.safe_get(lambda: control.ControlTypeName) != "ButtonControl":
                continue

            name = self.safe_get(lambda: control.Name, "")
            rect = self.safe_get(lambda: control.BoundingRectangle)
            if name.startswith(label_prefix) and rect.top >= window_rect.bottom - 120:
                return control
        return None

    def find_menu_item(self, label_prefix: str):
        root = auto.GetRootControl()
        candidates = []

        for control, _ in auto.WalkControl(root, maxDepth=10):
            if not self.is_visible(control):
                continue
            if self.safe_get(lambda: control.ControlTypeName) != "MenuItemControl":
                continue

            name = self.safe_get(lambda: control.Name, "")
            if not name.startswith(label_prefix):
                continue

            rect = self.safe_get(lambda: control.BoundingRectangle)
            candidates.append((rect.top, rect.left, control))

        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], item[1]))
        return candidates[0][2]

    def find_browser_pane(self):
        root = auto.GetRootControl()
        for control, _ in auto.WalkControl(root, maxDepth=3):
            if self.safe_get(lambda: control.ControlTypeName) != "PaneControl":
                continue
            if self.safe_get(lambda: control.ClassName) != WECHAT_BROWSER_PANE_CLASS:
                continue
            if not self.is_visible(control):
                continue

            rect = self.safe_get(lambda: control.BoundingRectangle)
            if rect.width() > 500 and rect.height() > 300:
                return control
        return None

    def find_browser_document(self, browser_pane):
        for control, _ in auto.WalkControl(browser_pane, maxDepth=6):
            if not self.is_visible(control):
                continue
            if self.safe_get(lambda: control.ControlTypeName) != "DocumentControl":
                continue
            if self.safe_get(lambda: control.ClassName) == WECHAT_BROWSER_DOCUMENT_CLASS:
                return control
        return None

    def try_get_browser_url(self, browser_pane) -> Optional[str]:
        document = self.find_browser_document(browser_pane)
        if not document:
            return None

        getters: List[Callable[[], Optional[str]]] = [
            lambda: getattr(document.GetValuePattern(), "Value", None),
            lambda: getattr(document.GetLegacyIAccessiblePattern(), "Value", None),
        ]
        for getter in getters:
            try:
                value = getter()
            except Exception:
                continue
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def wait_for_browser_url(self, browser_pane) -> str:
        def _get_url_with_openid() -> Optional[str]:
            url = self.try_get_browser_url(browser_pane)
            if url and OPENID_PATTERN.search(url):
                return url
            return None

        return self.wait_for(
            _get_url_with_openid,
            "browser url containing openid",
            self.config.browser_timeout_seconds,
        )

    def extract_openid(self, url: str) -> str:
        match = OPENID_PATTERN.search(url)
        if not match:
            raise OpenIdCollectorError(f"openid not found in url: {url}")
        return match.group(1)

    def close_browser_pane(self, browser_pane) -> None:
        browser_handle = int(self.safe_get(lambda: browser_pane.NativeWindowHandle, 0))
        close_buttons = []

        for control, _ in auto.WalkControl(browser_pane, maxDepth=10):
            if not self.is_visible(control):
                continue
            if self.safe_get(lambda: control.ControlTypeName) != "ButtonControl":
                continue
            if self.safe_get(lambda: control.Name, "") != "关闭":
                continue

            rect = self.safe_get(lambda: control.BoundingRectangle)
            close_buttons.append((rect.right, -rect.top, control))

        if not close_buttons:
            raise OpenIdCollectorError("close button not found in WeChat browser window")

        close_buttons.sort(reverse=True)
        target = close_buttons[0][2]
        target.Click(simulateMove=False)

        self.wait_for(
            lambda: None if self.browser_handle_exists(browser_handle) else True,
            "browser window to close",
            self.config.control_timeout_seconds,
        )

    def browser_handle_exists(self, browser_handle: int) -> bool:
        if browser_handle <= 0:
            return False
        pane = self.find_browser_pane()
        if not pane:
            return False
        return int(self.safe_get(lambda: pane.NativeWindowHandle, 0)) == browser_handle

    def wait_for(self, callback: Callable[[], object], description: str, timeout_seconds: float):
        deadline = time.monotonic() + timeout_seconds
        last_error = None

        while time.monotonic() < deadline:
            try:
                result = callback()
            except Exception as exc:
                last_error = exc
                result = None

            if result:
                return result
            time.sleep(0.3)

        if last_error:
            raise OpenIdCollectorError(f"timed out waiting for {description}: {last_error}")
        raise OpenIdCollectorError(f"timed out waiting for {description}")

    @staticmethod
    def safe_get(callback: Callable[[], object], default=None):
        try:
            return callback()
        except Exception:
            return default

    @staticmethod
    def is_visible(control) -> bool:
        rect = WeChatOpenIdCollector.safe_get(lambda: control.BoundingRectangle)
        if rect is None:
            return False
        return rect.width() > 0 and rect.height() > 0

    @staticmethod
    def _mask_openid(openid: str) -> str:
        if len(openid) <= 10:
            return openid
        return f"{openid[:6]}***{openid[-4:]}"

    def sidebar_limit(self, wechat_window) -> int:
        rect = self.safe_get(lambda: wechat_window.BoundingRectangle)
        width = rect.width()
        return rect.left + min(450, max(320, int(width * 0.35)))


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Use uiautomation to open WeChat -> 微助教服务号 -> 学生 -> 全部 and extract openid.",
    )
    parser.add_argument("--once", action="store_true", help="run exactly once and print the captured result")
    parser.add_argument("--interval-hours", type=float, default=2.0, help="repeat interval in hours, default: 2")
    parser.add_argument("--session-name", default="微助教服务号", help="visible WeChat chat/session name in the left sidebar")
    parser.add_argument("--menu-button", default="学生", help="bottom menu button prefix, default: 学生")
    parser.add_argument("--menu-item", default="全部", help="menu item prefix after clicking the bottom button, default: 全部")
    parser.add_argument("--control-timeout", type=float, default=10.0, help="timeout for control lookup/click flow")
    parser.add_argument("--browser-timeout", type=float, default=15.0, help="timeout for browser page/url loading")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH, help="where to save the latest openid json")
    parser.add_argument("--log-file", type=Path, default=DEFAULT_LOG_PATH, help="collector log path")
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    logger = build_logger(args.log_file)

    config = CollectorConfig(
        session_name=args.session_name,
        menu_button_prefix=args.menu_button,
        menu_item_prefix=args.menu_item,
        interval_hours=args.interval_hours,
        control_timeout_seconds=args.control_timeout,
        browser_timeout_seconds=args.browser_timeout,
        output_path=args.output,
        log_path=args.log_file,
    )
    collector = WeChatOpenIdCollector(config, logger)

    try:
        if args.once:
            result = collector.run_once()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0

        collector.run_forever()
        return 0
    except KeyboardInterrupt:
        logger.info("collector stopped by user")
        return 130
    except Exception as exc:
        logger.exception("collector exited with error: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
