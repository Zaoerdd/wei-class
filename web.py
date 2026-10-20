from __future__ import annotations

import argparse
import asyncio
import html
import ipaddress
import json
import logging
import os
import queue
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlsplit

import requests
from flask import Flask, jsonify, make_response, render_template, request

import ad
from getSocket import TeacherMateWebSocketClient
from getdata import getData, get_student_profile, submit_sign
from wechat_openid_collector import (
    DEFAULT_LOG_PATH as COLLECTOR_LOG_PATH,
    DEFAULT_OUTPUT_PATH as COLLECTOR_OUTPUT_PATH,
    CollectorConfig,
    OpenIdCollectorError,
    build_logger as build_collector_logger,
)
from wechat_openid_strategy import build_openid_collector, normalize_openid_method, recover_stale_capture_proxy
from wechat_qr_sign import WECHAT_UI_LOCK, WeChatQrSigner, validate_qr_url
from local_state import LocalState


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - [%(threadName)s] - %(message)s",
)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
FAYE_LOG_PATH = LOG_DIR / "faye_history.log"
OPENID_CACHE_PATH = BASE_DIR / "logs" / "latest_openid.json"
LOCAL_CONFIG_PATH = BASE_DIR / "local_config.json"
local_state = LocalState(BASE_DIR / "instance" / "attendance.sqlite3")
OPENID_HEX_RE = re.compile(r"^[a-fA-F0-9]{32}$")
OPENID_INVALID_MESSAGE_KEYWORDS = (
    "登录信息失效",
    "登录失效",
    "openid 无效",
    "openid失效",
    "openid invalid",
    "login invalid",
    "login expired",
    "unauthorized",
)


def load_local_config(config_path: Path) -> Dict[str, Any]:
    if not config_path.exists():
        return {}

    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("加载本地配置文件失败 %s: %s", config_path, exc)
        return {}

    if not isinstance(payload, dict):
        logger.warning("本地配置文件格式无效，应为 JSON 对象: %s", config_path)
        return {}

    return payload


LOCAL_CONFIG = load_local_config(LOCAL_CONFIG_PATH)


def get_local_config_value(*keys: str, default: Optional[Any] = None) -> Optional[Any]:
    current: Any = LOCAL_CONFIG
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def get_runtime_setting(
    env_name: str,
    *config_keys: str,
    default: Optional[str] = None,
) -> Optional[str]:
    env_value = os.getenv(env_name)
    if env_value is not None:
        stripped = str(env_value).strip()
        if stripped:
            return stripped

    if config_keys:
        config_value = get_local_config_value(*config_keys)
        if config_value is not None:
            stripped = str(config_value).strip()
            if stripped:
                return stripped

    return default


def _build_faye_file_logger() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    file_logger = logging.getLogger("faye_history")
    file_logger.setLevel(logging.INFO)
    file_logger.propagate = False

    if not any(
        isinstance(handler, RotatingFileHandler)
        and os.path.abspath(getattr(handler, "baseFilename", "")) == os.path.abspath(FAYE_LOG_PATH)
        for handler in file_logger.handlers
    ):
        handler = RotatingFileHandler(
            FAYE_LOG_PATH,
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        file_logger.addHandler(handler)

    return file_logger


faye_file_logger = _build_faye_file_logger()


def mask_openid(openid: Optional[str]) -> Optional[str]:
    if not openid:
        return None
    if len(openid) <= 10:
        return openid
    return f"{openid[:6]}***{openid[-4:]}"


def mask_openid_in_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return url
    return re.sub(r"([?&]openid=)([^&#]+)", lambda m: m.group(1) + (mask_openid(m.group(2)) or ""), url)


def get_sign_type(item: Dict[str, Any]) -> str:
    if item.get("isQR"):
        return "二维码签到"
    if item.get("isGPS"):
        return "GPS签到"
    return "普通签到"


def normalize_sign_item(item: Dict[str, Any]) -> Dict[str, Any]:
    normalized = {
        "name": item.get("name") or "未命名签到",
        "courseId": item.get("courseId"),
        "signId": item.get("signId"),
        "isQR": item.get("isQR", 0),
        "isGPS": item.get("isGPS", 0),
        "signType": get_sign_type(item),
    }

    for key in (
        "courseName",
        "teacherName",
        "startTime",
        "endTime",
        "lat",
        "lon",
        "latitude",
        "longitude",
    ):
        if key in item:
            normalized[key] = item.get(key)

    return normalized


def get_sign_key(item: Dict[str, Any]) -> Tuple[Any, Any]:
    return item.get("courseId"), item.get("signId")


def safe_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def safe_float(value: Any, fallback: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def collector_error_message(exc: Exception) -> str:
    detail = str(exc)
    if 'cv template "session"' in detail:
        return "未识别到微助教服务号，请在微信中打开该会话，保持窗口可见后重新获取"
    if 'cv template "menu_button"' in detail:
        return "未识别到“学生”菜单，请打开微助教服务号并保持底部菜单可见后重新获取"
    if 'cv template "menu_item"' in detail:
        return "未识别到“全部”入口，请检查微信菜单后重新获取"
    return detail


def extract_response_message(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None

    for field_name in ("msgClient", "message", "msg", "errorMsg", "detail"):
        value = payload.get(field_name)
        if isinstance(value, str):
            message = value.strip()
            if message:
                return message
    return None


def is_openid_invalid_response(payload: Any) -> bool:
    message = extract_response_message(payload)
    if not message:
        return False

    lowered = message.lower()
    return any(keyword.lower() in lowered for keyword in OPENID_INVALID_MESSAGE_KEYWORDS)


def normalize_signed_student(student: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(student, dict):
        return None

    student_id = safe_int(student.get("id"))
    rank = safe_int(student.get("rank"))
    team_id = safe_int(student.get("teamId"))
    is_out_of_bound = safe_int(student.get("isOutOfBound"))
    distance_value = student.get("distance")
    distance = None if distance_value in (None, "") else safe_float(distance_value, 0.0)

    normalized = {
        "id": student_id if student_id is not None else student.get("id"),
        "name": student.get("name") or "未命名学生",
        "avatar": student.get("avatar"),
        "student_number": student.get("studentNumber"),
        "rank": rank,
        "team_id": team_id,
        "is_new": bool(student.get("isNew")) if student.get("isNew") is not None else None,
        "distance": distance,
        "is_out_of_bound": is_out_of_bound,
    }

    if not any(normalized.get(key) not in (None, "") for key in ("id", "student_number", "name")):
        return None

    return normalized


def get_signed_student_key(student: Dict[str, Any]) -> Optional[str]:
    for field_name in ("id", "student_number", "name"):
        value = student.get(field_name)
        if value not in (None, ""):
            return f"{field_name}:{value}"
    return None


def extract_signed_students_from_event(event: Dict[str, Any]) -> List[Dict[str, Any]]:
    data = event.get("data")
    if not isinstance(data, dict):
        return []

    raw_students: List[Dict[str, Any]] = []
    for key in ("student", "students"):
        value = data.get(key)
        if isinstance(value, dict):
            raw_students.append(value)
        elif isinstance(value, list):
            raw_students.extend(item for item in value if isinstance(item, dict))

    normalized_students: List[Dict[str, Any]] = []
    seen_keys: Set[str] = set()

    for raw_student in raw_students:
        normalized_student = normalize_signed_student(raw_student)
        if not normalized_student:
            continue

        student_key = get_signed_student_key(normalized_student)
        if student_key is None:
            student_key = json.dumps(normalized_student, sort_keys=True, ensure_ascii=False)

        if student_key in seen_keys:
            continue

        seen_keys.add(student_key)
        normalized_students.append(normalized_student)

    return normalized_students


def build_sign_result_text(result: Dict[str, Any]) -> str:
    msg_client = str(result.get("msgClient") or "").strip()
    if msg_client:
        return msg_client

    sign_rank = safe_int(result.get("signRank"))
    if sign_rank and sign_rank > 0:
        return f"签到成功，你是第 {sign_rank} 个签到！"

    message = str(result.get("msg") or result.get("message") or "").strip()
    if message:
        return message

    return "签到完成"


def is_sign_result_success(result: Dict[str, Any], frontend_message: str, sign_rank: Optional[int]) -> bool:
    if sign_rank and sign_rank > 0:
        return True

    error_code = safe_int(result.get("errorCode"))
    if error_code:
        return False

    if any(keyword in frontend_message for keyword in ("失败", "错误", "关闭")):
        return False

    return "成功" in frontend_message


def build_result_meta(
    item: Optional[Dict[str, Any]],
    status_message: str,
    *,
    qr_ready: bool = False,
    sign_completed: bool = False,
    result_ready: bool = False,
    is_error: bool = False,
    frontend_message: Optional[str] = None,
    sign_rank: Optional[int] = None,
    qr_url: Optional[str] = None,
    faye: Optional[Dict[str, Any]] = None,
    faye_history: Optional[List[Dict[str, Any]]] = None,
    faye_subscriptions: Optional[List[str]] = None,
    signed_students: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    return {
        "qr_ready": qr_ready,
        "sign_completed": sign_completed,
        "result_ready": result_ready,
        "is_error": is_error,
        "status_message": status_message,
        "frontend_message": frontend_message,
        "sign_rank": sign_rank,
        "task_name": item.get("name") if item else None,
        "course_name": item.get("courseName") if item else None,
        "course_id": item.get("courseId") if item else None,
        "sign_id": item.get("signId") if item else None,
        "sign_type": item.get("signType") if item else None,
        "qr_url": qr_url,
        "faye": faye,
        "faye_history": list(faye_history or []),
        "faye_subscriptions": list(faye_subscriptions or []),
        "signed_students": [
            dict(student) if isinstance(student, dict) else student
            for student in (signed_students or [])
        ],
    }


def clone_result_meta(result_meta: Dict[str, Any]) -> Dict[str, Any]:
    cloned = dict(result_meta)
    if isinstance(cloned.get("faye"), dict):
        cloned["faye"] = dict(cloned["faye"])
    cloned["faye_history"] = [
        dict(item) if isinstance(item, dict) else item
        for item in cloned.get("faye_history", [])
    ]
    cloned["faye_subscriptions"] = list(cloned.get("faye_subscriptions", []))
    cloned["signed_students"] = [
        dict(student) if isinstance(student, dict) else student
        for student in cloned.get("signed_students", [])
    ]
    return cloned


def load_profile(openid: str) -> Optional[Dict[str, Any]]:
    try:
        profile = get_student_profile(openid)
        if isinstance(profile, dict) and "message" not in profile:
            return profile
    except Exception as exc:
        logger.warning("获取用户资料失败 %s: %s", mask_openid(openid), exc)
    return None


class PushPlusNotifier:
    def __init__(self, token: Optional[str], topic: Optional[str] = None):
        self.token = (token or "").strip()
        self.topic = (topic or "").strip() or None
        self.endpoint = "https://www.pushplus.plus/send"
        self._sent_cache: Dict[Tuple[Any, Any, str], float] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def send_qr_url(self, openid: str, item: Dict[str, Any], qr_url: str) -> bool:
        if not self.enabled:
            return False

        key = (item.get("courseId"), item.get("signId"), qr_url)
        now = time.time()

        with self._lock:
            self._cleanup_cache(now)
            if key in self._sent_cache:
                return False

        title = f"微助教签到二维码 - {item.get('courseName') or item.get('name') or '未知课程'}"
        content = self._build_content(openid, item, qr_url)
        payload = {
            "token": self.token,
            "title": title,
            "content": content,
            "template": "html",
        }
        if self.topic:
            payload["topic"] = self.topic

        try:
            response = requests.post(self.endpoint, json=payload, timeout=10)
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            logger.warning("PushPlus 推送失败: %s", exc)
            return False

        if str(body.get("code")) != "200":
            logger.warning("PushPlus 推送失败，返回内容: %s", body)
            return False

        with self._lock:
            self._sent_cache[key] = now

        logger.info(
            "PushPlus 推送成功: course=%s sign=%s",
            item.get("courseId"),
            item.get("signId"),
        )
        return True

    def _build_content(self, openid: str, item: Dict[str, Any], qr_url: str) -> str:
        def row(label: str, value: Any) -> str:
            safe_value = "-" if value in (None, "") else html.escape(str(value))
            return (
                "<tr>"
                f"<td style='padding:6px 12px;border:1px solid #ddd;background:#fafafa;'><strong>{html.escape(label)}</strong></td>"
                f"<td style='padding:6px 12px;border:1px solid #ddd;'>{safe_value}</td>"
                "</tr>"
            )

        escaped_url = html.escape(qr_url)
        pushed_at = html.escape(datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S"))
        rows = [
            row("课程名", item.get("courseName")),
            row("任务名", item.get("name")),
            row("教师", item.get("teacherName")),
            row("签到类型", item.get("signType")),
            row("courseId", item.get("courseId")),
            row("signId", item.get("signId")),
            row("OpenID", mask_openid(openid) or "-"),
            row("推送时间", pushed_at),
        ]

        return (
            "<div style='font-family:Segoe UI,Microsoft YaHei,sans-serif;'>"
            "<h2 style='margin-bottom:12px;'>微助教签到二维码</h2>"
            "<table style='border-collapse:collapse;margin-bottom:16px;'>"
            + "".join(rows)
            + "</table>"
            "<p><strong>二维码链接：</strong></p>"
            f"<p><a href='{escaped_url}' target='_blank'>{escaped_url}</a></p>"
            "</div>"
        )

    def _cleanup_cache(self, now: float) -> None:
        expired_keys = [key for key, value in self._sent_cache.items() if now - value > 24 * 3600]
        for key in expired_keys:
            self._sent_cache.pop(key, None)


PIPELINE_WATCHDOG_TICKS = 15  # scheduler loop ticks, about 1 s each
PIPELINE_HANDOVER_TIMEOUT = 30
PIPELINE_ERROR_BACKOFF_MAX = 60
SIGN_IN_SHUTDOWN_GRACE = 15
SIGN_STATE_RETENTION_SECONDS = 3600
WS_RECONNECT_DELAY_SECONDS = 5


class Pipeline:
    def __init__(self, openid: str, profile: Optional[Dict[str, Any]] = None,
                 predecessor: Optional[threading.Thread] = None):
        self.openid = openid
        self.profile = profile
        self.message: Optional[str] = "初始化中..."
        self.is_running = False
        self.thread: Optional[threading.Thread] = None
        self._predecessor = predecessor
        self.websocket_clients: List[TeacherMateWebSocketClient] = []
        self.ws_sign_keys: Set[Tuple[Any, Any]] = set()
        self.ws_ended_at: Dict[Tuple[Any, Any], float] = {}
        self.sign_last_seen: Dict[Tuple[Any, Any], float] = {}
        self.sign_tasks: Set[asyncio.Task] = set()
        self.asyncio_tasks: List[asyncio.Task] = []
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.shutdown_event = threading.Event()
        self.result_queue = queue.Queue()
        self.active_signs: List[Dict[str, Any]] = []
        self.pending_sign_keys: Set[Tuple[Any, Any]] = set()
        self.sign_results: Dict[Tuple[Any, Any], Dict[str, Any]] = {}
        self.latest_result_meta: Optional[Dict[str, Any]] = None
        self.invalid_openid_refresh_requested = False
        self.qr_auto_pending = set()
        self.qr_auto_attempts = {}

    def start(self) -> None:
        if self.is_running:
            return
        self.is_running = True
        thread = threading.Thread(target=self._run_async, daemon=True, name=f"Pipeline-{self.openid[-4:]}")
        self.thread = thread
        thread.start()
        logger.info("用户 %s 的监听管道已启动", mask_openid(self.openid))

    def stop(self) -> None:
        self.shutdown_event.set()
        self.is_running = False

    def _run_async(self) -> None:
        if self._predecessor is not None:
            # A stopped listener for the same account may still be finishing a
            # sign-in request; overlapping loops could submit the same sign twice.
            self._predecessor.join(PIPELINE_HANDOVER_TIMEOUT)
            if self._predecessor.is_alive():
                logger.warning("上一个监听管道仍未退出，继续启动: %s", mask_openid(self.openid))
            self._predecessor = None
        try:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(self._main_async())
        except Exception as exc:
            logger.error("异步运行失败 %s: %s", mask_openid(self.openid), exc)
        finally:
            if self.loop and not self.loop.is_closed():
                try:
                    # Let in-flight to_thread work (e.g. a sign-in POST) finish before
                    # this thread ends, so a successor's handover join covers it.
                    self.loop.run_until_complete(self.loop.shutdown_default_executor(timeout=PIPELINE_HANDOVER_TIMEOUT))
                except Exception as exc:
                    logger.warning("等待后台请求结束失败: %s", exc)
                self.loop.close()
            self.is_running = False

    async def _sleep_unless_stopped(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not self.shutdown_event.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(min(1.0, deadline - time.monotonic()))

    async def _main_async(self) -> None:
        failures = 0
        try:
            while not self.shutdown_event.is_set():
                # One bad iteration (e.g. "database is locked") must not end the listener.
                try:
                    data = await self.wait_data()
                    if self.shutdown_event.is_set():
                        break
                    self._prune_stale_sign_state({get_sign_key(item) for item in self.active_signs})

                    if isinstance(data, list) and data:
                        self.message = "检测到签到任务，正在处理中..."
                        await self.process_signatures(data)
                    else:
                        self.message = data if isinstance(data, str) else "暂无活跃签到"
                    failures = 0
                    delay = 2.0
                except Exception as exc:
                    failures += 1
                    delay = min(PIPELINE_ERROR_BACKOFF_MAX, 2.0 * 2 ** failures)
                    logger.exception("监听循环出错，%.0f 秒后重试", delay)
                    self.message = f"监听出错，{delay:.0f} 秒后自动重试: {exc}"
                await self._sleep_unless_stopped(delay)
        finally:
            await self.shutdown()

    def _prune_stale_sign_state(self, active_keys: Set[Tuple[Any, Any]]) -> None:
        # Keep per-sign state for a while after a sign disappears (it may come back
        # briefly), then drop it so a long-running listener does not grow unbounded.
        now = time.monotonic()
        for key in active_keys:
            self.sign_last_seen[key] = now
        stale = {key for key, seen in self.sign_last_seen.items() if now - seen >= SIGN_STATE_RETENTION_SECONDS}
        stale -= self.pending_sign_keys | self.qr_auto_pending | self.ws_sign_keys
        if not stale:
            return
        # Rebuild instead of deleting in place: Flask threads read these concurrently.
        self.sign_last_seen = {k: v for k, v in self.sign_last_seen.items() if k not in stale}
        self.sign_results = {k: v for k, v in self.sign_results.items() if k not in stale}
        self.qr_auto_attempts = {k: v for k, v in self.qr_auto_attempts.items() if k not in stale}
        self.ws_ended_at = {k: v for k, v in self.ws_ended_at.items() if k not in stale}

    async def wait_data(self) -> Any:
        try:
            data = await asyncio.to_thread(getData, self.openid)
            if data and isinstance(data, list) and len(data) > 0:
                self.invalid_openid_refresh_requested = False
                result = []
                for item in data:
                    if all(key in item for key in ["courseId", "signId", "isQR", "isGPS"]):
                        result.append(normalize_sign_item(item))
                if result:
                    self.active_signs = result
                    active_keys = {get_sign_key(item) for item in result}
                    self.pending_sign_keys.intersection_update(active_keys)
                    return result
                self.active_signs = []
                self.pending_sign_keys.clear()
                return "当前没有进行中的签到"
            if is_openid_invalid_response(data):
                invalid_message = extract_response_message(data) or "登录信息失效，请退出后重试"
                return await self._handle_invalid_openid(invalid_message)
            if isinstance(data, dict) and "message" in data:
                self.active_signs = []
                self.pending_sign_keys.clear()
                return data["message"]
            self.active_signs = []
            self.pending_sign_keys.clear()
            return "暂无签到"
        except Exception as exc:
            logger.error("获取签到数据失败: %s", exc)
            self.active_signs = []
            self.pending_sign_keys.clear()
            return "网络请求失败"

    async def _handle_invalid_openid(self, server_message: Optional[str] = None) -> str:
        waiting_message = "检测到当前 OpenID 已失效，正在自动重新获取..."
        if server_message:
            waiting_message = f"{server_message}，正在自动重新获取..."

        self.active_signs = []
        self.pending_sign_keys.clear()
        self.message = waiting_message
        self.latest_result_meta = build_result_meta(None, waiting_message)

        if not self.invalid_openid_refresh_requested and openid_refresh_manager is not None:
            self.invalid_openid_refresh_requested = True
            logger.warning("当前 OpenID 已失效，准备自动刷新: %s", mask_openid(self.openid))
            openid_refresh_manager.invalidate_openid(
                self.openid,
                reason=server_message,
                refresh_reason="openid-invalid",
            )

        return waiting_message

    async def process_signatures(self, data: List[Dict[str, Any]]) -> None:
        for item in data:
            if self.shutdown_event.is_set():
                return
            sign_key = get_sign_key(item)
            if await asyncio.to_thread(local_state.has_success, self.openid, self.profile, *sign_key):
                self._store_result_meta(sign_key, build_result_meta(
                    item, "本机记录显示此签到已成功完成", sign_completed=True, result_ready=True,
                    frontend_message="本机记录显示此签到已成功完成"))
                continue

            if item.get("isQR"):
                # Reserved before the task starts: the handshake now runs off-loop,
                # so the next poll must not open a second client for this sign.
                ended_at = self.ws_ended_at.get(sign_key)
                if sign_key in self.ws_sign_keys or (
                        ended_at is not None and time.monotonic() - ended_at < WS_RECONNECT_DELAY_SECONDS):
                    continue

                self.ws_sign_keys.add(sign_key)
                self.message = "检测到二维码签到，正在获取二维码..."
                task = asyncio.create_task(self.run_websocket_client(item))
                self._track_task(task)
                continue

            if sign_key in self.pending_sign_keys or sign_key in self.sign_results:
                continue

            self.pending_sign_keys.add(sign_key)
            self.message = f"检测到 {item.get('signType') or '普通签到'}，正在自动签到..."
            task = asyncio.create_task(self.run_common_sign(item))
            self.sign_tasks.add(task)
            task.add_done_callback(self.sign_tasks.discard)
            self._track_task(task)

    def _track_task(self, task: asyncio.Task) -> None:
        self.asyncio_tasks.append(task)

        def _cleanup(done_task: asyncio.Task) -> None:
            if done_task in self.asyncio_tasks:
                self.asyncio_tasks.remove(done_task)

            if done_task.cancelled():
                return

            exc = done_task.exception()
            if exc:
                logger.error("后台任务执行失败: %s", exc)

        task.add_done_callback(_cleanup)

    def _ensure_result_meta(
        self,
        item: Dict[str, Any],
        status_message: Optional[str] = None,
    ) -> Tuple[Tuple[Any, Any], Dict[str, Any]]:
        sign_key = get_sign_key(item)
        existing = self.sign_results.get(sign_key)
        if existing:
            result_meta = clone_result_meta(existing)
            if status_message is not None:
                result_meta["status_message"] = status_message
            return sign_key, result_meta

        return sign_key, build_result_meta(item, status_message or self.message or "暂无状态")

    def _store_result_meta(self, sign_key: Tuple[Any, Any], result_meta: Dict[str, Any]) -> None:
        self.sign_results[sign_key] = clone_result_meta(result_meta)
        self.latest_result_meta = clone_result_meta(result_meta)
        try:
            local_state.record_success(self.openid, self.profile, result_meta)
        except Exception:
            logger.exception("签到成功记录保存失败")

    def _build_faye_snapshot(self, event: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "event_kind": event.get("event_kind"),
            "summary": event.get("summary"),
            "channel": event.get("channel"),
            "subscription": event.get("subscription"),
            "successful": event.get("successful"),
            "data_type": event.get("data_type"),
            "qr_url": event.get("qr_url"),
            "inner_faye_token": event.get("inner_faye_token"),
            "advice": event.get("advice"),
            "data": event.get("data"),
            "ext": event.get("ext"),
            "raw": event.get("raw"),
        }

    def _log_faye_event(self, item: Dict[str, Any], event: Dict[str, Any]) -> None:
        try:
            payload = {
                "timestamp": datetime.now(timezone.utc).astimezone().isoformat(),
                "openid": self.openid,
                "openid_masked": mask_openid(self.openid),
                "task_name": item.get("name"),
                "course_id": item.get("courseId"),
                "sign_id": item.get("signId"),
                "sign_type": item.get("signType"),
                "event": {
                    **self._build_faye_snapshot(event),
                    "raw_message": event.get("raw_message"),
                },
            }
            faye_file_logger.info(json.dumps(payload, ensure_ascii=False))
        except Exception as exc:
            logger.warning("写入 Faye 日志失败: %s", exc)

    def _append_faye_event(self, result_meta: Dict[str, Any], event: Dict[str, Any]) -> None:
        history = list(result_meta.get("faye_history") or [])
        snapshot = self._build_faye_snapshot(event)
        history.append(snapshot)
        result_meta["faye_history"] = history[-10:]
        result_meta["faye"] = snapshot

        if event.get("event_kind") == "meta_subscribe" and event.get("successful") and event.get("subscription"):
            subscriptions = list(result_meta.get("faye_subscriptions") or [])
            subscription = str(event["subscription"])
            if subscription not in subscriptions:
                subscriptions.append(subscription)
            result_meta["faye_subscriptions"] = subscriptions

    def _merge_signed_students(self, result_meta: Dict[str, Any], event: Dict[str, Any]) -> None:
        incoming_students = extract_signed_students_from_event(event)
        if not incoming_students:
            return

        signed_students = [
            dict(student)
            for student in result_meta.get("signed_students", [])
            if isinstance(student, dict)
        ]
        student_index_map: Dict[str, int] = {}

        for index, student in enumerate(signed_students):
            student_key = get_signed_student_key(student)
            if student_key:
                student_index_map[student_key] = index

        for student in incoming_students:
            student_key = get_signed_student_key(student)
            if student_key and student_key in student_index_map:
                signed_students[student_index_map[student_key]] = {
                    **signed_students[student_index_map[student_key]],
                    **student,
                }
                continue

            signed_students.append(student)
            if student_key:
                student_index_map[student_key] = len(signed_students) - 1

        signed_students.sort(
            key=lambda current_student: (
                current_student.get("rank") is None,
                current_student.get("rank") if current_student.get("rank") is not None else 10**9,
                str(current_student.get("name") or ""),
                str(current_student.get("student_number") or ""),
            )
        )
        result_meta["signed_students"] = signed_students

    async def run_websocket_client(self, item: Dict[str, Any]) -> None:
        sign_key = get_sign_key(item)
        sign_id = item["signId"]
        course_id = item["courseId"]
        client = None
        try:
            if self.shutdown_event.is_set():
                return
            client_id = await asyncio.to_thread(ad.creatClientId, sign_id, courseId=course_id)
            client = TeacherMateWebSocketClient(
                sign_id=sign_id,
                course_id=course_id,
                qr_callback=lambda url, sign_item=item: self.callback(sign_item, url),
                event_callback=lambda event, sign_item=item: self.handle_ws_event(sign_item, event),
            )
            client.client_id = client_id
            client.webT = True
            self.websocket_clients.append(client)
            await client.start()
        except Exception as exc:
            logger.error("WebSocket 客户端错误: %s", exc)
        finally:
            # However the client ended (server closed it, errors, retries used up),
            # release the sign so the next poll reconnects while it is still active.
            if client in self.websocket_clients:
                self.websocket_clients.remove(client)
            self.ws_sign_keys.discard(sign_key)
            self.ws_ended_at[sign_key] = time.monotonic()

    async def run_common_sign(self, item: Dict[str, Any]) -> None:
        sign_key = get_sign_key(item)
        try:
            if self.shutdown_event.is_set():
                return
            lat = safe_float(item.get("lat", item.get("latitude")), 0.0)
            lon = safe_float(item.get("lon", item.get("longitude")), 0.0)
            sign_result = await asyncio.to_thread(
                submit_sign,
                self.openid,
                item["courseId"],
                item["signId"],
                lat=lat,
                lon=lon,
            )
            if is_openid_invalid_response(sign_result):
                invalid_message = extract_response_message(sign_result) or "登录信息失效，请退出后重试"
                frontend_message = await self._handle_invalid_openid(invalid_message)
                result_meta = build_result_meta(
                    item,
                    frontend_message,
                    result_ready=True,
                    is_error=True,
                    frontend_message=frontend_message,
                )
                self._store_result_meta(sign_key, result_meta)
                return

            sign_rank = safe_int(sign_result.get("signRank"))
            frontend_message = build_sign_result_text(sign_result)
            sign_completed = is_sign_result_success(sign_result, frontend_message, sign_rank)
            result_meta = build_result_meta(
                item,
                frontend_message,
                sign_completed=sign_completed,
                result_ready=True,
                is_error=not sign_completed,
                frontend_message=frontend_message,
                sign_rank=sign_rank,
            )
            self._store_result_meta(sign_key, result_meta)
            self.message = frontend_message
        except Exception as exc:
            logger.error("自动签到失败 %s: %s", item.get("signId"), exc)
            self.message = f"自动签到失败: {exc}"
        finally:
            self.pending_sign_keys.discard(sign_key)

    def handle_ws_event(self, item: Dict[str, Any], event: Dict[str, Any]) -> None:
        if self.loop and not self.loop.is_closed():
            asyncio.run_coroutine_threadsafe(self._process_ws_event(item, event), self.loop)

    async def _process_ws_event(self, item: Dict[str, Any], event: Dict[str, Any]) -> None:
        sign_key, result_meta = self._ensure_result_meta(
            item,
            event.get("summary") or self.message or "暂无状态",
        )
        self._append_faye_event(result_meta, event)
        self._merge_signed_students(result_meta, event)
        own_number = str((self.profile or {}).get("student_number") or "").strip()
        own_sign_channels = {f"/attendance/{item['courseId']}/{item['signId']}", f"/sign/{item['signId']}"}
        if own_number and event.get("event_kind") == "student_signed" and event.get("channel") in own_sign_channels and any(
            str(student.get("student_number") or "").strip() == own_number
            and student.get("is_out_of_bound") != 1
            for student in extract_signed_students_from_event(event)
        ):
            result_meta.update(sign_completed=True, result_ready=True, is_error=False,
                               qr_auto_state="completed", frontend_message="微助教签到记录确认：本人已签到",
                               status_message="本人已签到")
        self._log_faye_event(item, event)

        if not result_meta.get("result_ready"):
            result_meta["status_message"] = event.get("summary") or result_meta.get("status_message")

        if event.get("channel"):
            result_meta["task_name"] = result_meta.get("task_name") or item.get("name")

        self._store_result_meta(sign_key, result_meta)

    def callback(self, item: Dict[str, Any], url: str) -> None:
        self.result_queue.put(url)
        if self.loop and not self.loop.is_closed():
            asyncio.run_coroutine_threadsafe(self._process_callback_result(item, url), self.loop)

    async def _process_callback_result(self, item: Dict[str, Any], url: str) -> None:
        if self.shutdown_event.is_set():
            return
        try:
            qr_url = str(url)
            sign_key, result_meta = self._ensure_result_meta(item, "获取成功")
            if result_meta.get("sign_completed"):
                return
            result_meta.update(
                {
                    "qr_ready": True,
                    "result_ready": True,
                    "frontend_message": result_meta.get("frontend_message") if result_meta.get("qr_auto_state") else "二维码已获取，请扫码签到",
                    "qr_url": qr_url,
                    "status_message": "获取成功",
                }
            )
            self._store_result_meta(sign_key, result_meta)
            self.message = "获取成功"

            if qr_auto_enabled:
                self._schedule_qr_auto(item, qr_url)

            if pushplus_notifier.enabled:
                notify_task = asyncio.create_task(
                    asyncio.to_thread(pushplus_notifier.send_qr_url, self.openid, item, qr_url)
                )
                self._track_task(notify_task)
        except Exception as exc:
            self.message = f"解析二维码失败: {exc}"

    def _schedule_qr_auto(self, item: Dict[str, Any], url: str) -> None:
        key = get_sign_key(item)
        if self.shutdown_event.is_set() or key in self.qr_auto_pending:
            return
        attempts = self.qr_auto_attempts.setdefault(key, {"urls": set(), "last_at": 0.0})
        if url in attempts["urls"] or len(attempts["urls"]) >= 3 or time.monotonic() - attempts["last_at"] < 45:
            return
        if self.sign_results.get(key, {}).get("sign_completed"):
            return
        try:
            validate_qr_url(url)
        except ValueError as exc:
            self._set_qr_auto_state(key, "manual", str(exc))
            return
        attempts["urls"].add(url)
        attempts["last_at"] = time.monotonic()
        self.qr_auto_pending.add(key)
        self._set_qr_auto_state(key, "queued", "已收到二维码，等待自动在微信打开")
        self._track_task(asyncio.create_task(self._run_qr_auto(item, url)))

    def _set_qr_auto_state(self, key, state, message) -> None:
        meta = self.sign_results.get(key)
        if meta is None or meta.get("sign_completed"):
            return
        meta = clone_result_meta(meta)
        meta.update(qr_auto_state=state, frontend_message=message, status_message=message)
        if state == "completed":
            meta.update(sign_completed=True, result_ready=True, is_error=False)
        self._store_result_meta(key, meta)

    async def _run_qr_auto(self, item: Dict[str, Any], url: str) -> None:
        key = get_sign_key(item)

        def cancelled():
            return (self.shutdown_event.is_set() or self.sign_results.get(key, {}).get("sign_completed", False)
                    or key not in {get_sign_key(active) for active in self.active_signs})

        def progress(state, message):
            if self.loop and not self.loop.is_closed():
                self.loop.call_soon_threadsafe(self._set_qr_auto_state, key, state, message)

        try:
            if not hasattr(collector, "find_template_location"):
                raise RuntimeError("自动打开二维码需要 CV 微信模式")
            outcome = await asyncio.to_thread(WeChatQrSigner(collector).run, url, cancelled, progress)
            self._set_qr_auto_state(key, outcome.state, outcome.message)
        except Exception as exc:
            logger.warning("二维码自动打开未完成: %s", type(exc).__name__)
            self._set_qr_auto_state(key, "manual", f"自动处理已停止：{exc}；请手动扫码")
        finally:
            self.qr_auto_pending.discard(key)

    def _get_result_meta(self, current_sign: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if current_sign:
            result_meta = self.sign_results.get(get_sign_key(current_sign))
            if result_meta:
                return clone_result_meta(result_meta)
            return build_result_meta(current_sign, self.message or "暂无状态")

        if self.latest_result_meta:
            return clone_result_meta(self.latest_result_meta)

        return build_result_meta(None, self.message or "暂无状态")

    async def shutdown(self) -> None:
        self.shutdown_event.set()
        if self.sign_tasks:
            # Let in-flight sign-in requests finish so their result is recorded;
            # cancelling would drop a success and let the next listener resubmit.
            await asyncio.wait(list(self.sign_tasks), timeout=SIGN_IN_SHUTDOWN_GRACE)
        for client in list(self.websocket_clients):
            if not client.is_shutting_down:
                await client.graceful_shutdown()
        for task in list(self.asyncio_tasks):
            task.cancel()
        self.pending_sign_keys.clear()
        self.websocket_clients.clear()
        self.asyncio_tasks.clear()

    def get_status(self) -> Dict[str, Any]:
        if not self.active_signs:
            result_meta = (clone_result_meta(self.latest_result_meta)
                           if self.latest_result_meta and self.latest_result_meta.get("qr_auto_state")
                           else build_result_meta(None, self.message or "暂无状态"))
            return {
                "success": 0,
                "message": result_meta.get("status_message") or self.message,
                "qr_url": None,
                "is_running": self.is_running,
                "profile": self.profile,
                "active_sign_count": 0,
                "active_signs": [],
                "current_sign": None,
                "result_meta": result_meta,
            }

        current_sign = None
        for item in self.active_signs:
            result_meta = self.sign_results.get(get_sign_key(item))
            if result_meta and result_meta.get("result_ready"):
                current_sign = item
                break
        if current_sign is None:
            for item in self.active_signs:
                result_meta = self.sign_results.get(get_sign_key(item))
                if result_meta:
                    current_sign = item
                    break
        if current_sign is None and self.active_signs:
            current_sign = self.active_signs[0]

        result_meta = self._get_result_meta(current_sign)
        return {
            "success": 1 if result_meta.get("result_ready") else 0,
            "message": result_meta.get("status_message") or self.message,
            "qr_url": result_meta.get("qr_url"),
            "is_running": self.is_running,
            "profile": self.profile,
            "active_sign_count": len(self.active_signs),
            "active_signs": self.active_signs,
            "current_sign": current_sign,
            "result_meta": result_meta,
        }


active_pipelines: Dict[str, Pipeline] = {}
# Last listener thread per account, so a replacement can wait for it to exit.
last_pipeline_threads: Dict[str, threading.Thread] = {}
active_pipeline_lock = threading.RLock()


def stop_pipeline(openid: str) -> None:
    with active_pipeline_lock:
        pipeline = active_pipelines.pop(openid, None)
    if pipeline:
        pipeline.stop()


def stop_all_pipelines(except_openid: Optional[str] = None) -> None:
    with active_pipeline_lock:
        stale_openids = [openid for openid in active_pipelines if openid != except_openid]
    for openid in stale_openids:
        stop_pipeline(openid)


def get_or_create_pipeline(openid: str, profile: Optional[Dict[str, Any]] = None) -> Pipeline:
    with active_pipeline_lock:
        pipeline = active_pipelines.get(openid)
        if pipeline and pipeline.is_running:
            if profile:
                pipeline.profile = profile
            return pipeline

        pipeline = Pipeline(openid, profile=profile, predecessor=last_pipeline_threads.get(openid))
        active_pipelines[openid] = pipeline
        pipeline.start()
        last_pipeline_threads[openid] = pipeline.thread
        return pipeline


def activate_pipeline_for_openid(openid: str, profile: Optional[Dict[str, Any]] = None) -> Pipeline:
    stop_all_pipelines(except_openid=openid)
    return get_or_create_pipeline(openid, profile)


def get_pipeline(openid: Optional[str]) -> Optional[Pipeline]:
    if not openid:
        return None
    with active_pipeline_lock:
        return active_pipelines.get(openid)


class OpenIdRefreshManager:
    def __init__(
        self,
        collector: Any,
        cache_path: Path,
        interval_hours: float = 2.0,
    ):
        self.collector = collector
        self.cache_path = cache_path
        self.interval_seconds = max(60.0, interval_hours * 3600.0)
        self._state_lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._started = False

        self.current_openid: Optional[str] = None
        self.current_profile: Optional[Dict[str, Any]] = None
        self.current_source: Optional[str] = None
        self.current_url: Optional[str] = None
        self.last_refresh_at: Optional[str] = None
        self.next_refresh_at: Optional[str] = None
        self.last_error: Optional[str] = None
        self.is_refreshing = False
        self.used_file_fallback = False
        self.refresh_failures = 0
        self._next_refresh_deadline = time.monotonic()
        self.listening_enabled = local_state.listening_enabled()

    def start(self) -> None:
        with self._state_lock:
            if self._started:
                return
            self._started = True

        if self.listening_enabled:
            self.request_refresh_async(reason="startup", allow_file_fallback=True)
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="OpenIdRefresh")
        self._thread.start()

    def _run_loop(self) -> None:
        ticks = 0
        while not self._stop_event.wait(1.0):
            with self._state_lock:
                due = self.listening_enabled and time.monotonic() >= self._next_refresh_deadline
            if due:
                self.refresh_openid(reason="scheduled", allow_file_fallback=False)
            ticks += 1
            if ticks % PIPELINE_WATCHDOG_TICKS == 0:
                self._restart_dead_pipeline()

    def _restart_dead_pipeline(self) -> None:
        # A listener thread that died unexpectedly would otherwise stay dead until
        # the next OpenID refresh, which can be up to 2 hours away.
        with self._state_lock:
            if not self.listening_enabled or not self.current_openid:
                return
            pipeline = get_pipeline(self.current_openid)
            if pipeline and pipeline.is_running:
                return
            logger.warning("监听管道已停止，正在重新启动: %s", mask_openid(self.current_openid))
            activate_pipeline_for_openid(self.current_openid, self.current_profile)

    def refresh_openid(self, reason: str, allow_file_fallback: bool, _reserved: bool = False) -> bool:
        if not _reserved and not self._refresh_lock.acquire(blocking=False):
            logger.info("OpenID 刷新跳过：已有刷新任务正在执行")
            return False

        with self._state_lock:
            self.is_refreshing = True

        errors: List[str] = []
        succeeded = False
        try:
            if reason != "manual" and not self.listening_enabled:
                return False
            record = None
            source = None

            try:
                with WECHAT_UI_LOCK:
                    if reason != "manual" and not self.listening_enabled:
                        return False
                    collected = self.collector.run_once()
                record = self._validate_record(collected)
                source = "collector"
            except Exception as exc:
                logger.exception("OpenID 采集失败 (%s): %s", reason, exc)
                errors.append(f"OpenID 采集失败: {collector_error_message(exc)}")

            if record is None and allow_file_fallback:
                try:
                    cached = self._load_cached_record()
                    record = self._validate_record(cached)
                    source = "file"
                    logger.warning("启动采集未拿到新的 OpenID，已回退到缓存文件")
                except Exception as exc:
                    logger.exception("OpenID 缓存回退失败 (%s): %s", reason, exc)
                    errors.append(f"缓存回退失败: {exc}")

            if record is not None and source is not None:
                self._apply_record(record, source=source)
                succeeded = True
                return True

            with self._state_lock:
                if errors:
                    self.last_error = "；".join(errors)
            return False
        finally:
            with self._state_lock:
                self.is_refreshing = False
                self.refresh_failures = 0 if succeeded else self.refresh_failures + 1
                self._set_next_refresh_time()
            self._refresh_lock.release()

    def request_refresh_async(self, reason: str, allow_file_fallback: bool = False) -> bool:
        if not self._refresh_lock.acquire(blocking=False):
            return False
        with self._state_lock:
            self.is_refreshing = True
        def _runner() -> None:
            self.refresh_openid(reason=reason, allow_file_fallback=allow_file_fallback, _reserved=True)

        refresh_thread = threading.Thread(
            target=_runner,
            daemon=True,
            name=f"OpenIdRefreshRequest-{int(time.time() * 1000)}",
        )
        try:
            refresh_thread.start()
        except Exception:
            self.is_refreshing = False
            self._refresh_lock.release()
            raise
        return True

    def set_listening(self, enabled: bool) -> None:
        with self._state_lock:
            local_state.set_listening(enabled)
            self.listening_enabled = enabled
            if not enabled:
                stop_all_pipelines()
            elif self.current_openid:
                activate_pipeline_for_openid(self.current_openid, self.current_profile)
            else:
                self.request_refresh_async(reason="resume", allow_file_fallback=True)

    def invalidate_openid(
        self,
        openid: str,
        *,
        reason: Optional[str] = None,
        refresh_reason: str = "openid-invalid",
        allow_file_fallback: bool = False,
    ) -> bool:
        sanitized_reason = (reason or "").strip()
        public_message = "检测到当前 OpenID 已失效，正在自动重新获取..."
        if sanitized_reason:
            public_message = f"{sanitized_reason}，正在自动重新获取..."

        with self._state_lock:
            if self.current_openid != openid:
                return False
            self.current_openid = None
            self.current_source = None
            self.current_url = None
            self.used_file_fallback = False
            self.last_error = public_message

        stop_pipeline(openid)
        logger.warning("已标记失效 OpenID，并开始自动刷新: %s", mask_openid(openid))
        self.request_refresh_async(reason=refresh_reason, allow_file_fallback=allow_file_fallback)
        return True

    def use_manual_openid(self, openid: str) -> Optional[Pipeline]:
        record = self._validate_record({"openid": openid, "captured_at": datetime.now().astimezone().isoformat()})
        self._apply_record(record, source="manual")
        with self._state_lock:
            self.refresh_failures = 0
            self._set_next_refresh_time()
        pipeline = self.get_current_pipeline()
        return pipeline

    def clear_current_state(self, reason: Optional[str] = None) -> None:
        self.set_listening(False)
        with self._state_lock:
            self.current_openid = None
            self.current_profile = None
            self.current_source = None
            self.current_url = None
            self.used_file_fallback = False
            self.last_error = reason or self.last_error
            self.refresh_failures = 0
            self._set_next_refresh_time()

    def get_current_pipeline(self) -> Optional[Pipeline]:
        with self._state_lock:
            openid = self.current_openid
        return get_pipeline(openid)

    def get_public_state(self) -> Dict[str, Any]:
        # Returned to the browser: never include the full OpenID.
        with self._state_lock:
            return {
                "has_openid": bool(self.current_openid),
                "openid_masked": mask_openid(self.current_openid),
                "collector_method": getattr(self.collector, "method_name", "unknown"),
                "current_source": self.current_source,
                "current_url": mask_openid_in_url(self.current_url),
                "last_refresh_at": self.last_refresh_at,
                "next_refresh_at": self.next_refresh_at,
                "listening_enabled": self.listening_enabled,
                "last_error": self.last_error,
                "is_refreshing": self.is_refreshing,
                "refresh_failures": self.refresh_failures,
                "used_file_fallback": self.used_file_fallback,
                "profile": self.current_profile,
                "qr_auto_enabled": qr_auto_enabled,
                "wechat_ui_busy": WECHAT_UI_LOCK.locked(),
            }

    def build_waiting_message(self) -> str:
        state = self.get_public_state()
        if state["is_refreshing"]:
            return "正在自动获取 OpenID，请稍候..."
        if not state["listening_enabled"]:
            return "监听已暂停，点击开始监听后继续"
        if state["last_error"]:
            return state["last_error"]
        return "未登录"

    def _validate_record(self, record: Dict[str, Any]) -> Dict[str, Any]:
        openid = str(record.get("openid") or "").strip()
        if not OPENID_HEX_RE.fullmatch(openid):
            raise OpenIdCollectorError("OpenID 格式不正确，应为 32 位十六进制字符串")

        api_response = getData(openid)
        if is_openid_invalid_response(api_response):
            invalid_message = extract_response_message(api_response) or "登录信息失效，请退出后重试"
            raise OpenIdCollectorError(f"OpenID 无效: {invalid_message}")
        if isinstance(api_response, dict) and "message" in api_response:
            raise OpenIdCollectorError(f"OpenID 验证失败: {api_response['message']}")
        if not isinstance(api_response, list):
            raise OpenIdCollectorError("OpenID 验证结果格式异常")

        profile = load_profile(openid)
        return {
            "openid": openid,
            "url": record.get("url"),
            "captured_at": record.get("captured_at") or datetime.now().astimezone().isoformat(),
            "profile": profile,
        }

    def _apply_record(self, record: Dict[str, Any], source: str) -> None:
        refresh_time = datetime.now().astimezone().isoformat()

        with self._state_lock:
            pipeline = (activate_pipeline_for_openid(record["openid"], record.get("profile"))
                        if self.listening_enabled else None)
            profile = (pipeline.profile if pipeline else None) or record.get("profile")
            self.current_openid = record["openid"]
            self.current_profile = profile
            self.current_source = source
            self.current_url = record.get("url")
            self.last_refresh_at = refresh_time
            self.last_error = None
            self.used_file_fallback = source == "file"

        self._persist_record(
            {
                "openid": record["openid"],
                "url": record.get("url"),
                "captured_at": record.get("captured_at") or refresh_time,
                "source": source,
            }
        )
        logger.info(
            "当前 OpenID 已更新: %s (source=%s)",
            mask_openid(record["openid"]),
            source,
        )

    def _persist_record(self, record: Dict[str, Any]) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(
            json.dumps(record, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _load_cached_record(self) -> Dict[str, Any]:
        if not self.cache_path.exists():
            raise FileNotFoundError(f"缓存文件不存在: {self.cache_path}")
        return json.loads(self.cache_path.read_text(encoding="utf-8"))

    def _set_next_refresh_time(self) -> None:
        with self._state_lock:
            delay = self.interval_seconds
            if self.refresh_failures:
                delay = min(self.interval_seconds, (30, 60, 120, 300)[min(self.refresh_failures - 1, 3)])
            self._next_refresh_deadline = time.monotonic() + delay
            next_time = datetime.now().astimezone() + timedelta(seconds=delay)
            self.next_refresh_at = next_time.isoformat()


collector_logger = build_collector_logger(COLLECTOR_LOG_PATH)
collector_config = CollectorConfig(
    session_name=os.getenv("WECHAT_SESSION_NAME", "微助教服务号"),
    menu_button_prefix=os.getenv("WECHAT_MENU_BUTTON", "学生"),
    menu_item_prefix=os.getenv("WECHAT_MENU_ITEM", "全部"),
    interval_hours=2.0,
    control_timeout_seconds=float(os.getenv("WECHAT_CONTROL_TIMEOUT", "10")),
    browser_timeout_seconds=float(os.getenv("WECHAT_BROWSER_TIMEOUT", "15")),
    output_path=COLLECTOR_OUTPUT_PATH,
    log_path=COLLECTOR_LOG_PATH,
)
pushplus_notifier = PushPlusNotifier(
    token=get_runtime_setting("PUSHPLUS_TOKEN", "pushplus", "token"),
    topic=get_runtime_setting("PUSHPLUS_TOPIC", "pushplus", "topic"),
)
qr_auto_enabled = str(get_runtime_setting("WECHAT_QR_AUTO_ENABLED", "qr_auto", "enabled", default="false")).lower() in ("true", "1", "yes")
collector_method = normalize_openid_method(os.getenv("WECHAT_OPENID_METHOD", "uiautomation"))
collector = None
openid_refresh_manager = None

app = Flask(__name__)
runtime_init_lock = threading.Lock()
runtime_initialized = False


def configure_runtime(openid_method: Optional[str] = None) -> None:
    global collector_method, collector, openid_refresh_manager, runtime_initialized

    selected_method = normalize_openid_method(openid_method or os.getenv("WECHAT_OPENID_METHOD", "uiautomation"))
    collector_method = selected_method
    collector = build_openid_collector(selected_method, collector_config, collector_logger)
    openid_refresh_manager = OpenIdRefreshManager(
        collector=collector,
        cache_path=OPENID_CACHE_PATH,
        interval_hours=2.0,
    )
    runtime_initialized = False


configure_runtime()


def ensure_runtime_initialized() -> None:
    global runtime_initialized
    if runtime_initialized:
        return

    with runtime_init_lock:
        if runtime_initialized:
            return
        if openid_refresh_manager is None:
            raise RuntimeError("OpenID runtime is not configured")
        try:
            recover_stale_capture_proxy(collector_logger)
        except Exception as exc:
            logger.error("恢复上次残留的系统代理失败: %s", exc)
        openid_refresh_manager.start()
        runtime_initialized = True


def build_empty_status(message: Optional[str] = None) -> Dict[str, Any]:
    status_message = message or openid_refresh_manager.build_waiting_message()
    result_meta = build_result_meta(None, status_message)
    return {
        "success": 0,
        "message": status_message,
        "qr_url": None,
        "is_running": False,
        "profile": openid_refresh_manager.get_public_state().get("profile"),
        "active_sign_count": 0,
        "active_signs": [],
        "current_sign": None,
        "result_meta": result_meta,
        "openid_status": openid_refresh_manager.get_public_state(),
    }


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/login", methods=["POST"])
def login():
    ensure_runtime_initialized()
    data = request.json or {}
    openid = str(data.get("openid") or "").strip()

    if not openid:
        return jsonify({"success": False, "message": "请输入 OpenID"})

    try:
        pipeline = openid_refresh_manager.use_manual_openid(openid)
        resp = make_response(
            jsonify(
                {
                    "success": True,
                    "message": "登录成功",
                    "profile": openid_refresh_manager.current_profile,
                    "current_sign": pipeline.active_signs[0] if pipeline and pipeline.active_signs else None,
                    "openid_status": openid_refresh_manager.get_public_state(),
                }
            )
        )
        return resp
    except Exception as exc:
        logger.error("登录验证错误: %s", exc)
        return jsonify({"success": False, "message": str(exc)})


@app.route("/api/check_session")
def check_session():
    ensure_runtime_initialized()
    state = openid_refresh_manager.get_public_state()
    pipeline = openid_refresh_manager.get_current_pipeline()

    if state.get("has_openid") and pipeline:
        return jsonify(
            {
                "valid": True,
                "openid_masked": state["openid_masked"],
                "profile": pipeline.profile,
                "current_sign": pipeline.active_signs[0] if pipeline.active_signs else None,
                "openid_status": state,
                "auto_mode": True,
            }
        )

    message = openid_refresh_manager.build_waiting_message()
    return jsonify(
        {
            "valid": False,
            "message": message,
            "openid_status": state,
            "auto_mode": True,
        }
    )


@app.route("/api/logout", methods=["POST"])
def logout():
    ensure_runtime_initialized()
    openid_refresh_manager.clear_current_state("已停止自动监听")
    resp = make_response(jsonify({"success": True}))
    # Older versions stored the OpenID in this cookie; clear any leftover copy.
    resp.delete_cookie("user_openid")
    return resp


@app.route("/api/openid_status")
def openid_status():
    ensure_runtime_initialized()
    return jsonify(openid_refresh_manager.get_public_state())


@app.route("/api/listening", methods=["POST"])
def set_listening():
    ensure_runtime_initialized()
    enabled = (request.get_json(silent=True) or {}).get("enabled")
    if not isinstance(enabled, bool):
        return jsonify(success=False, message="请选择开始或暂停监听"), 400
    openid_refresh_manager.set_listening(enabled)
    return jsonify(success=True, message="已开始监听，正在准备登录信息" if enabled else "已暂停监听；正在进行的请求可能仍会返回结果")


@app.route("/api/openid/refresh", methods=["POST"])
def refresh_openid_now():
    ensure_runtime_initialized()
    started = openid_refresh_manager.request_refresh_async(reason="manual", allow_file_fallback=False)
    return jsonify(success=started, message="正在重新获取 OpenID，请保持微信可见" if started else "已有获取流程正在执行，请稍候"), (202 if started else 409)


@app.route("/api/sign_history")
def sign_history():
    return jsonify(local_state.history())


@app.route("/qr_code")
def qr_code_status():
    ensure_runtime_initialized()
    pipeline = openid_refresh_manager.get_current_pipeline()
    if not pipeline:
        return jsonify(build_empty_status())

    status = pipeline.get_status()
    status["openid_status"] = openid_refresh_manager.get_public_state()
    return jsonify(status)


@app.before_request
def reject_cross_site_requests():
    # Only localhost or a raw IP may be used as Host; a domain name here means
    # DNS rebinding, which would let another site read this local API.
    hostname = urlsplit(f"//{request.host}").hostname or ""
    if hostname != "localhost":
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            return jsonify(success=False, message="只允许通过 localhost 或 IP 地址访问本服务"), 403
    # Block cross-site forms/fetches from other pages from changing state.
    origin = request.headers.get("Origin")
    if request.method not in ("GET", "HEAD", "OPTIONS") and origin is not None:
        if urlsplit(origin).netloc.lower() != request.host.lower():
            return jsonify(success=False, message="拒绝来自其他网页的请求"), 403
    return None


@app.after_request
def add_header(response):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


@app.route("/usage")
def usage():
    return render_template("usage.html")


def parse_runtime_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="TeacherMate sign listener service")
    parser.add_argument(
        "--openid-method",
        choices=["uiautomation", "cv"],
        default=None,
        help="choose how to collect openid at startup: uiautomation or cv (template click + mitmproxy)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="bind host, default 127.0.0.1")
    parser.add_argument("--port", type=int, default=5000, help="bind port, default 5000")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_runtime_args()
    configure_runtime(args.openid_method)
    ensure_runtime_initialized()
    app.run(host=args.host, port=args.port, debug=False, threaded=True)
