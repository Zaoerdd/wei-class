from __future__ import annotations

import ctypes
import json
import logging
import os
import re
import socket
import time
import winreg
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from wechat_openid_collector import CollectorConfig, OpenIdCollectorError, WeChatOpenIdCollector

SUPPORTED_OPENID_METHODS = ("uiautomation", "cv")

SW_RESTORE = 9
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_SHOWWINDOW = 0x0040

OPENID_URL_RE = re.compile(r"[?&]openid=([^&#]+)")
OPENID_JSON_RE = re.compile(r'["\']openid["\']\s*:\s*["\']([^"\']+)["\']', re.IGNORECASE)
OPENID_HEX_RE = re.compile(r"\b[a-fA-F0-9]{32}\b")

user32 = ctypes.windll.user32
wininet = ctypes.windll.wininet

INTERNET_OPTION_REFRESH = 37
INTERNET_OPTION_SETTINGS_CHANGED = 39
INTERNET_SETTINGS_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"


def normalize_openid_method(method: Optional[str]) -> str:
    value = (method or "uiautomation").strip().lower()
    aliases = {
        "ui": "uiautomation",
        "uia": "uiautomation",
        "automation": "uiautomation",
        "uiautomation": "uiautomation",
        "cv": "cv",
        "vision": "cv",
        "opencv": "cv",
        "computer-vision": "cv",
    }
    normalized = aliases.get(value)
    if not normalized:
        supported = ", ".join(SUPPORTED_OPENID_METHODS)
        raise ValueError(f"unsupported openid method: {method}. expected one of: {supported}")
    return normalized


@dataclass
class WindowRect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)


@dataclass
class WindowInfo:
    hwnd: int
    title: str
    class_name: str
    rect: WindowRect


@dataclass
class FileSnapshot:
    exists: bool
    size: int
    mtime_ns: int


@dataclass
class ProxySettingsSnapshot:
    proxy_enable: int
    proxy_server: Optional[str]
    auto_config_url: Optional[str]
    auto_detect: Optional[int]
    proxy_override: Optional[str]


class CVWeChatOpenIdCollector:
    method_name = "cv"

    def __init__(self, config: CollectorConfig, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self.base_dir = Path(__file__).resolve().parent
        self.template_dir = Path(os.getenv("WECHAT_CV_TEMPLATE_DIR", self.base_dir / "cv_templates"))
        self.match_confidence = float(os.getenv("WECHAT_CV_MATCH_THRESHOLD", "0.82"))
        self.click_delay = float(os.getenv("WECHAT_CV_CLICK_DELAY", "0.8"))
        self.browser_delay = float(os.getenv("WECHAT_CV_BROWSER_DELAY", "3.0"))
        self.capture_timeout = float(
            os.getenv("WECHAT_CV_MITM_TIMEOUT", str(max(15.0, self.config.browser_timeout_seconds)))
        )
        self.poll_interval = float(os.getenv("WECHAT_CV_MITM_POLL_INTERVAL", "0.4"))
        self.close_timeout = float(os.getenv("WECHAT_CV_CLOSE_TIMEOUT", "6.0"))
        self.close_poll_interval = float(os.getenv("WECHAT_CV_CLOSE_POLL_INTERVAL", "0.4"))
        self.capture_proxy_host = os.getenv("WECHAT_CV_PROXY_HOST", "127.0.0.1").strip() or "127.0.0.1"
        self.capture_proxy_port = int(os.getenv("WECHAT_CV_PROXY_PORT", "8080"))
        self.capture_proxy_connect_timeout = float(os.getenv("WECHAT_CV_PROXY_CONNECT_TIMEOUT", "2.0"))
        self.auto_switch_system_proxy = self._read_bool_env("WECHAT_CV_AUTO_SWITCH_SYSTEM_PROXY", True)
        self.mitm_result_path = Path(
            os.getenv("WECHAT_CV_MITM_RESULT_PATH", self.base_dir / "logs" / "mitm_openid_result.txt")
        )
        self.window_title_contains = os.getenv("WECHAT_CV_WINDOW_TITLE", "微信").strip() or "微信"
        raw_classes = os.getenv("WECHAT_CV_WINDOW_CLASSES", "WeChatMainWndForPC")
        self.window_classes = [item.strip() for item in raw_classes.split(",") if item.strip()]
        self.template_names: Dict[str, str] = {
            "session": os.getenv("WECHAT_CV_SESSION_TEMPLATE", "session.png"),
            "menu_button": os.getenv("WECHAT_CV_MENU_BUTTON_TEMPLATE", "student_button.png"),
            "menu_item": os.getenv("WECHAT_CV_MENU_ITEM_TEMPLATE", "all_item.png"),
            "close": os.getenv("WECHAT_CV_CLOSE_TEMPLATE", "close_button.png"),
        }
        self.relative_regions: Dict[str, Tuple[float, float, float, float]] = {
            "session": (0.00, 0.00, 0.38, 1.00),
            "menu_button": (0.38, 0.80, 0.62, 0.20),
            "menu_item": (0.45, 0.68, 0.30, 0.32),
            "close": (0.70, 0.00, 0.30, 0.20),
        }
        self._pyautogui = None

    def run_once(self) -> dict:
        self.logger.info("starting one cv collection run")
        self._ensure_dependencies()
        with self.temporary_capture_proxy():
            window = self.find_wechat_window()
            self.activate_window(window)

            baseline = self._snapshot_file(self.mitm_result_path)
            self.logger.info("waiting for mitmproxy capture file at %s", self.mitm_result_path)

            self.click_template("session", window.rect)
            time.sleep(self.click_delay)
            self.click_template("menu_button", window.rect)
            time.sleep(self.click_delay)
            self.click_template("menu_item", window.rect)
            time.sleep(self.browser_delay)

            try:
                openid = self.wait_for_openid_from_mitm(baseline)
            finally:
                try:
                    self.close_browser(window)
                except Exception as exc:
                    self.logger.warning("cv close button click failed: %s", exc)

        result = {
            "openid": openid,
            "url": None,
            "session_name": self.config.session_name,
            "menu_path": [self.config.menu_button_prefix, self.config.menu_item_prefix],
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "method": self.method_name,
            "capture_source": "mitmproxy",
            "mitm_result_path": str(self.mitm_result_path),
            "proxy_server": f"{self.capture_proxy_host}:{self.capture_proxy_port}",
        }
        self.write_result(result)
        return result

    def write_result(self, result: dict) -> None:
        self.config.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.config.output_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.logger.info("result written to %s", self.config.output_path)

    def _ensure_dependencies(self) -> None:
        if self._pyautogui is None:
            try:
                import pyautogui
            except ImportError as exc:
                raise OpenIdCollectorError(
                    "cv collector dependencies missing. install pyautogui first."
                ) from exc

            pyautogui.FAILSAFE = False
            pyautogui.PAUSE = 0.1
            self._pyautogui = pyautogui

    def find_wechat_window(self) -> WindowInfo:
        candidates: List[WindowInfo] = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def callback(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True

            title = self._get_window_text(hwnd)
            if self.window_title_contains not in title:
                return True

            rect = self._get_window_rect(hwnd)
            if rect.width < 200 or rect.height < 200:
                return True

            class_name = self._get_class_name(hwnd)
            candidates.append(
                WindowInfo(
                    hwnd=int(hwnd),
                    title=title,
                    class_name=class_name,
                    rect=rect,
                )
            )
            return True

        user32.EnumWindows(callback, 0)
        if not candidates:
            raise OpenIdCollectorError(f'WeChat window not found for title containing "{self.window_title_contains}"')

        def sort_key(item: WindowInfo) -> Tuple[int, int]:
            class_priority = 0 if item.class_name in self.window_classes else 1
            area_score = -(item.rect.width * item.rect.height)
            return class_priority, area_score

        candidates.sort(key=sort_key)
        return candidates[0]

    def activate_window(self, window: WindowInfo) -> None:
        user32.ShowWindow(window.hwnd, SW_RESTORE)
        user32.SetWindowPos(window.hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
        user32.SetWindowPos(window.hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
        user32.SetForegroundWindow(window.hwnd)
        time.sleep(0.4)

    def click_template(self, role: str, window_rect: Optional[WindowRect]) -> None:
        location = self.find_template_location(role, window_rect)
        if location is None:
            image_path = self._resolve_template(role)
            region = self._build_region(role, window_rect)
            raise OpenIdCollectorError(
                f'cv template "{role}" not found on screen. template={image_path} region={region}'
            )

        self._click_location(location)

    def find_template_location(self, role: str, window_rect: Optional[WindowRect]):
        pyautogui = self._pyautogui
        if pyautogui is None:
            raise OpenIdCollectorError("pyautogui not initialized")

        image_path = self._resolve_template(role)
        region = self._build_region(role, window_rect)
        try:
            return pyautogui.locateCenterOnScreen(
                str(image_path),
                confidence=self.match_confidence,
                region=region,
                grayscale=True,
            )
        except Exception as exc:
            if exc.__class__.__name__.endswith("ImageNotFoundException"):
                return None
            raise

    def close_browser(self, window: WindowInfo) -> None:
        deadline = time.monotonic() + self.close_timeout
        last_error: Optional[str] = None

        while time.monotonic() < deadline:
            self.activate_window(window)
            try:
                location = self.find_template_location("close", None)
                if location is None:
                    raise OpenIdCollectorError("close template not visible yet")
                self._click_location(location)
                time.sleep(max(0.2, self.click_delay))
                if self.find_template_location("close", None) is None:
                    return
            except Exception as exc:
                last_error = str(exc) or exc.__class__.__name__
            time.sleep(self.close_poll_interval)

        detail = f"last error: {last_error}" if last_error else "close template was never clickable"
        raise OpenIdCollectorError(f"failed to close WeChat browser after capture, {detail}")

    def _click_location(self, location) -> None:
        pyautogui = self._pyautogui
        if pyautogui is None:
            raise OpenIdCollectorError("pyautogui not initialized")
        pyautogui.click(int(location.x), int(location.y))

    @contextmanager
    def temporary_capture_proxy(self):
        if not self.auto_switch_system_proxy:
            yield
            return

        target_proxy = f"{self.capture_proxy_host}:{self.capture_proxy_port}"
        self.ensure_capture_proxy_ready(target_proxy)
        original = self._read_system_proxy_settings()

        if original.proxy_enable and self._proxy_matches(original.proxy_server, target_proxy):
            self.logger.info("system proxy already points to capture proxy %s", target_proxy)
            yield
            return

        self.logger.info(
            "temporarily switching system proxy from %s to %s",
            original.proxy_server or "<disabled>",
            target_proxy,
        )
        self._apply_capture_proxy(target_proxy)
        try:
            yield
        finally:
            try:
                self._restore_system_proxy_settings(original)
                self.logger.info("system proxy restored to %s", original.proxy_server or "<disabled>")
            except Exception as exc:
                self.logger.error("failed to restore system proxy: %s", exc)

    def ensure_capture_proxy_ready(self, target_proxy: str) -> None:
        try:
            with socket.create_connection(
                (self.capture_proxy_host, self.capture_proxy_port),
                timeout=self.capture_proxy_connect_timeout,
            ):
                return
        except OSError as exc:
            raise OpenIdCollectorError(
                f"capture proxy {target_proxy} is not reachable. start start_mitmproxy_openid.ps1 first."
            ) from exc

    def _read_system_proxy_settings(self) -> ProxySettingsSnapshot:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, INTERNET_SETTINGS_REG_PATH) as key:
            return ProxySettingsSnapshot(
                proxy_enable=int(self._query_registry_value(key, "ProxyEnable", 0) or 0),
                proxy_server=self._normalize_registry_string(self._query_registry_value(key, "ProxyServer", None)),
                auto_config_url=self._normalize_registry_string(self._query_registry_value(key, "AutoConfigURL", None)),
                auto_detect=self._query_registry_value(key, "AutoDetect", None),
                proxy_override=self._normalize_registry_string(self._query_registry_value(key, "ProxyOverride", None)),
            )

    def _apply_capture_proxy(self, target_proxy: str) -> None:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            INTERNET_SETTINGS_REG_PATH,
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, target_proxy)
            self._set_registry_string(key, "AutoConfigURL", "")
            winreg.SetValueEx(key, "AutoDetect", 0, winreg.REG_DWORD, 0)
        self._refresh_system_proxy_settings()

    def _restore_system_proxy_settings(self, snapshot: ProxySettingsSnapshot) -> None:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            INTERNET_SETTINGS_REG_PATH,
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, int(snapshot.proxy_enable))
            self._set_registry_string(key, "ProxyServer", snapshot.proxy_server)
            self._set_registry_string(key, "AutoConfigURL", snapshot.auto_config_url)

            if snapshot.auto_detect is None:
                self._delete_registry_value(key, "AutoDetect")
            else:
                winreg.SetValueEx(key, "AutoDetect", 0, winreg.REG_DWORD, int(snapshot.auto_detect))

            self._set_registry_string(key, "ProxyOverride", snapshot.proxy_override)
        self._refresh_system_proxy_settings()

    def _refresh_system_proxy_settings(self) -> None:
        wininet.InternetSetOptionW(0, INTERNET_OPTION_SETTINGS_CHANGED, 0, 0)
        wininet.InternetSetOptionW(0, INTERNET_OPTION_REFRESH, 0, 0)

    def _proxy_matches(self, proxy_server: Optional[str], target_proxy: str) -> bool:
        if not proxy_server:
            return False
        normalized_target = target_proxy.lower()
        for part in proxy_server.split(";"):
            value = part.strip()
            if not value:
                continue
            if "=" in value:
                _, value = value.split("=", 1)
            if value.strip().lower() == normalized_target:
                return True
        return False

    def _query_registry_value(self, key, name: str, default):
        try:
            value, _ = winreg.QueryValueEx(key, name)
            return value
        except FileNotFoundError:
            return default

    def _set_registry_string(self, key, name: str, value: Optional[str]) -> None:
        if value is None:
            self._delete_registry_value(key, name)
            return
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)

    def _delete_registry_value(self, key, name: str) -> None:
        try:
            winreg.DeleteValue(key, name)
        except FileNotFoundError:
            pass

    def _normalize_registry_string(self, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = str(value).strip()
        return cleaned or None

    def _read_bool_env(self, name: str, default: bool) -> bool:
        raw = os.getenv(name)
        if raw is None:
            return default
        return raw.strip().lower() not in {"0", "false", "no", "off"}

    def wait_for_openid_from_mitm(self, baseline: FileSnapshot) -> str:
        deadline = time.monotonic() + self.capture_timeout
        last_parse_error: Optional[str] = None

        while time.monotonic() < deadline:
            current = self._snapshot_file(self.mitm_result_path)
            if self._is_new_capture(baseline, current):
                try:
                    openid = self._extract_latest_openid(self.mitm_result_path)
                except Exception as exc:
                    last_parse_error = str(exc)
                else:
                    self.logger.info("openid captured from mitmproxy file: %s", self._mask_openid(openid))
                    return openid

            time.sleep(self.poll_interval)

        detail = f" last parse error: {last_parse_error}" if last_parse_error else ""
        raise OpenIdCollectorError(
            f"timed out waiting for mitmproxy openid capture from {self.mitm_result_path}.{detail}"
        )

    def _extract_latest_openid(self, path: Path) -> str:
        if not path.exists():
            raise OpenIdCollectorError(f"mitmproxy result file not found: {path}")

        text = path.read_text(encoding="utf-8", errors="ignore")
        if not text.strip():
            raise OpenIdCollectorError(f"mitmproxy result file is empty: {path}")

        candidates: List[str] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue

            parsed_from_json = self._extract_openid_from_json_line(line)
            if parsed_from_json:
                candidates.append(parsed_from_json)
                continue

            parsed_from_text = self._extract_openid_from_text(line)
            if parsed_from_text:
                candidates.append(parsed_from_text)

        if not candidates:
            raise OpenIdCollectorError(f"no openid found in mitmproxy result file: {path}")
        return candidates[-1]

    def _extract_openid_from_json_line(self, line: str) -> Optional[str]:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return None

        if isinstance(payload, dict):
            openid = str(payload.get("openid") or "").strip()
            if openid:
                return openid
        return None

    def _extract_openid_from_text(self, text: str) -> Optional[str]:
        url_match = OPENID_URL_RE.search(text)
        if url_match:
            return url_match.group(1)

        json_match = OPENID_JSON_RE.search(text)
        if json_match:
            return json_match.group(1)

        hex_match = OPENID_HEX_RE.search(text)
        if hex_match:
            return hex_match.group(0)

        return None

    def _snapshot_file(self, path: Path) -> FileSnapshot:
        try:
            stat = path.stat()
        except FileNotFoundError:
            return FileSnapshot(exists=False, size=0, mtime_ns=0)
        return FileSnapshot(exists=True, size=stat.st_size, mtime_ns=stat.st_mtime_ns)

    def _is_new_capture(self, baseline: FileSnapshot, current: FileSnapshot) -> bool:
        if not current.exists:
            return False
        if not baseline.exists:
            return current.size > 0
        return current.mtime_ns > baseline.mtime_ns or current.size != baseline.size

    def _resolve_template(self, role: str) -> Path:
        filename = self.template_names.get(role)
        if not filename:
            raise OpenIdCollectorError(f"unknown cv template role: {role}")
        path = Path(filename)
        if not path.is_absolute():
            path = self.template_dir / path
        if not path.exists():
            raise OpenIdCollectorError(f"cv template missing for role {role}: {path}")
        return path

    def _build_region(self, role: str, window_rect: Optional[WindowRect]) -> Optional[Tuple[int, int, int, int]]:
        env_name = f"WECHAT_CV_{role.upper()}_REGION"
        override = os.getenv(env_name)
        if override:
            parts = [part.strip() for part in override.split(",")]
            if len(parts) != 4:
                raise OpenIdCollectorError(f"invalid region format for {env_name}, expected x,y,width,height")
            return tuple(int(part) for part in parts)  # type: ignore[return-value]

        if window_rect is None:
            return None

        left_ratio, top_ratio, width_ratio, height_ratio = self.relative_regions.get(role, (0.0, 0.0, 1.0, 1.0))
        left = window_rect.left + int(window_rect.width * left_ratio)
        top = window_rect.top + int(window_rect.height * top_ratio)
        width = max(1, int(window_rect.width * width_ratio))
        height = max(1, int(window_rect.height * height_ratio))
        return left, top, width, height

    def _get_window_text(self, hwnd: int) -> str:
        length = user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        return buffer.value

    def _get_class_name(self, hwnd: int) -> str:
        buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buffer, 256)
        return buffer.value

    def _get_window_rect(self, hwnd: int) -> WindowRect:
        rect = ctypes.wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        return WindowRect(rect.left, rect.top, rect.right, rect.bottom)

    def _mask_openid(self, openid: str) -> str:
        if len(openid) <= 10:
            return openid
        return f"{openid[:6]}***{openid[-4:]}"


def build_openid_collector(method: Optional[str], config: CollectorConfig, logger: logging.Logger):
    normalized = normalize_openid_method(method)
    if normalized == "cv":
        return CVWeChatOpenIdCollector(config, logger)

    collector = WeChatOpenIdCollector(config, logger)
    setattr(collector, "method_name", "uiautomation")
    return collector
