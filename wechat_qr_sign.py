"""Open a received TeacherMate QR URL in the user's File Transfer Assistant."""
from __future__ import annotations

import ctypes
import logging
import re
import threading
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

WECHAT_UI_LOCK = threading.Lock()
TRUSTED_DOMAINS = ("teachermate.cn", "teachermate.com.cn")
RESULT_PATHS = {"/wechat-pro-ssr/student/signresult", "/wechat-pro/student/signresult"}


def trusted_host(host: str | None) -> bool:
    return bool(host) and any(host == domain or host.endswith("." + domain) for domain in TRUSTED_DOMAINS)


def validate_qr_url(url: str) -> str:
    if not isinstance(url, str) or not url or len(url) > 2048:
        raise ValueError("二维码链接为空或过长")
    if not url.isascii() or re.search(r"[\s\x00-\x1f\x7f\\]", url):
        raise ValueError("二维码链接包含不支持的字符")
    p = urlsplit(url)
    if p.scheme != "https" or not trusted_host(p.hostname) or p.username or p.password or p.port not in (None, 443):
        raise ValueError("仅自动打开 HTTPS 微助教官方链接")
    if p.path.rstrip("/") in RESULT_PATHS:
        raise ValueError("不能把结果页当作签到二维码")
    return url


@dataclass(frozen=True)
class QrOutcome:
    state: str
    message: str


def parse_result_url(url: str, previous_url: str | None = None) -> QrOutcome | None:
    if not url or url == previous_url:
        return None
    p = urlsplit(url)
    if p.scheme != "https" or not trusted_host(p.hostname) or p.path.rstrip("/") not in RESULT_PATHS:
        return None
    query = parse_qs(p.query)
    if query.get("success") == ["1"]:
        return QrOutcome("completed", "微助教结果页确认：二维码签到成功")
    if query.get("success") == ["0"]:
        reason = (query.get("message") or ["微助教未接受本次签到"])[0][:180]
        return QrOutcome("manual", f"自动签到未成功：{reason}；可手动扫码")
    return None


class WeChatQrSigner:
    def __init__(self, collector, timeout: float = 35):
        self.collector = collector
        self.timeout = timeout
        self.logger = logging.getLogger(__name__)

    def run(self, url: str, cancelled, progress=lambda state, message: None) -> QrOutcome:
        url = validate_qr_url(url)
        while not WECHAT_UI_LOCK.acquire(timeout=0.3):
            if cancelled():
                return QrOutcome("cancelled", "签到任务已结束，取消自动打开")
        try:
            if cancelled():
                return QrOutcome("cancelled", "签到任务已结束，取消自动打开")
            return self._run_locked(url, cancelled, progress)
        finally:
            WECHAT_UI_LOCK.release()

    def _run_locked(self, url, cancelled, progress):
        import uiautomation as auto
        from wechat_openid_collector import WeChatOpenIdCollector

        c = self.collector
        c._ensure_dependencies()
        # These templates must be calibrated for this PC. Never send without a verified header.
        c.template_names.update({
            "file_transfer_session": "file_transfer_session.png",
            "file_transfer_header": "file_transfer_header.png",
            "sent_url_prefix": "sent_url_prefix.png",
        })
        c.relative_regions.update({
            "file_transfer_session": (0, 0, .38, 1),
            "file_transfer_header": (.25, 0, .5, .12),
        })
        for role in ("file_transfer_session", "file_transfer_header", "sent_url_prefix"):
            c._resolve_template(role)
        with auto.UIAutomationInitializerInThread():
            reader = WeChatOpenIdCollector(c.config, c.logger)
            previous_url = self._document_url(reader)
            w = c.find_wechat_window()
            c.activate_window(w)
            w = self._open_file_transfer(w, cancelled)
            self._assert_target(w, cancelled)
            self._assert_empty_draft(w)
            # The calibrated input pane is beneath the chat toolbar; don't select/delete drafts.
            self._assert_target(w, cancelled)
            c._pyautogui.click(w.rect.left + int(w.rect.width * .45), w.rect.top + int(w.rect.height * .84))
            self._assert_target(w, cancelled)
            progress("sending", "正在将二维码链接发送给文件传输助手")
            # Unicode input bypasses the Chinese IME without replacing the user's clipboard.
            for char in url:
                self._assert_foreground(w, cancelled)
                c.assert_window_ready(w)
                auto.SendUnicodeChar(char, charMode=True)
            self._assert_target(w, cancelled)
            c._pyautogui.press("enter")
            time.sleep(.8)
            self._assert_target(w, cancelled)
            self._assert_empty_draft(w)
            self._click_latest_link(w, cancelled)
            progress("confirming", "已在微信打开链接，正在等待微助教确认签到结果")
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                if cancelled():
                    return QrOutcome("cancelled", "任务已结束或已确认完成")
                current = self._document_url(reader)
                result = parse_result_url(current or "", previous_url)
                if result:
                    return result
                time.sleep(.5)
        return QrOutcome("manual", "链接已打开，但未收到明确成功结果；请查看微信页面或手动扫码")

    def _open_file_transfer(self, w, cancelled):
        """Wait for repaint and reread geometry after restoring the main window."""
        c = self.collector
        clicked = False
        for _ in range(12):
            self._assert_foreground(w, cancelled)
            current = c.find_wechat_window()
            if current.hwnd != w.hwnd:
                raise RuntimeError("微信主窗口已变化，请重新打开文件传输助手后重试")
            w = current
            if c.find_template_location("file_transfer_header", w.rect) is not None:
                self._assert_foreground(w, cancelled)
                return w
            if not clicked:
                location = c.find_template_location("file_transfer_session", w.rect)
                if location is not None:
                    self._assert_foreground(w, cancelled)
                    # A move during screenshot matching invalidates its coordinates.
                    if c.find_wechat_window().rect != w.rect:
                        continue
                    c._click_location(location)
                    clicked = True
            time.sleep(.35)
        if clicked:
            raise RuntimeError("已选择文件传输助手，但未能确认会话标题；请检查微信是否被遮挡")
        raise RuntimeError("未找到文件传输助手；请在微信中打开该会话，并保持窗口可见")

    @staticmethod
    def _document_url(reader):
        pane = reader.find_browser_pane()
        return reader.try_get_browser_url(pane) if pane else None

    def _assert_target(self, w, cancelled):
        self._assert_foreground(w, cancelled)
        self.collector.assert_window_ready(w)
        if self.collector.find_template_location("file_transfer_header", w.rect) is None:
            raise RuntimeError("无法确认当前会话是文件传输助手，未继续发送")
        self.collector.assert_window_ready(w)

    @staticmethod
    def _assert_foreground(w, cancelled):
        if cancelled():
            raise RuntimeError("任务已取消，停止微信操作")
        if int(ctypes.windll.user32.GetForegroundWindow()) != w.hwnd:
            raise RuntimeError("微信窗口失去焦点，已停止操作，请手动扫码")

    def _assert_empty_draft(self, w):
        c = self.collector
        region = (w.rect.left + int(w.rect.width * .285), w.rect.top + int(w.rect.height * .795),
                  int(w.rect.width * .68), int(w.rect.height * .14))
        pixels = c._capture_match_image(region)
        # A caret is permitted; typed text, attachments and existing drafts are not.
        background = float(c._numpy.median(pixels))
        mask = (c._numpy.abs(pixels.astype(float) - background) > 35).astype("uint8")
        scale = w.rect.width / 1001
        components = c._cv2.connectedComponentsWithStats(mask)[2][1:]
        for x, y, width, height, area in components:
            caret = x <= 8 * scale and width <= 3 * scale and 8 * scale <= height <= 30 * scale
            if area > 4 and not caret:
                raise RuntimeError("文件传输助手输入框已有内容或布局变化，已停止自动发送")

    def _click_latest_link(self, w, cancelled):
        c = self.collector
        cv2, np = c._cv2, c._numpy
        region = (w.rect.left + int(w.rect.width * .40), w.rect.top + int(w.rect.height * .10),
                  int(w.rect.width * .57), int(w.rect.height * .63))
        with c._mss.mss() as capture:
            shot = np.array(capture.grab(dict(zip(("left", "top", "width", "height"), region))))
        # Identify the bottommost outgoing green message, then its HTTPS link prefix.
        mask = cv2.inRange(shot[:, :, :3], np.array([35, 170, 95], dtype=np.uint8), np.array([160, 255, 205], dtype=np.uint8))
        count, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
        bubbles = [(x, y, width, height) for x, y, width, height, area in stats[1:]
                   if width > 70 and height > 24 and area > 900]
        if not bubbles:
            raise RuntimeError("无法确认已发送的链接气泡，请在文件传输助手手动打开")
        x, y, width, height = max(bubbles, key=lambda b: b[1] + b[3])
        gray = cv2.cvtColor(shot[y:y+height, x:x+width], cv2.COLOR_BGRA2GRAY)
        for path in c._iter_template_candidates("sent_url_prefix"):
            template = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
            if template is None or template.shape[0] > height or template.shape[1] > width:
                continue
            _, score, _, point = cv2.minMaxLoc(cv2.matchTemplate(gray, template, cv2.TM_CCOEFF_NORMED))
            if score >= .86:
                self._assert_target(w, cancelled)
                c._pyautogui.click(region[0] + x + point[0] + template.shape[1] // 2,
                                    region[1] + y + point[1] + template.shape[0] // 2)
                return
        raise RuntimeError("未在最新消息中识别到链接，不会点击历史消息")
