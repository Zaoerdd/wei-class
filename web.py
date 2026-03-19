from __future__ import annotations

import argparse
import asyncio
import html
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
from wechat_openid_strategy import build_openid_collector, normalize_openid_method


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - [%(threadName)s] - %(message)s",
)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
FAYE_LOG_PATH = LOG_DIR / "faye_history.log"
OPENID_CACHE_PATH = BASE_DIR / "logs" / "latest_openid.json"
OPENID_HEX_RE = re.compile(r"^[a-fA-F0-9]{32}$")


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


class Pipeline:
    def __init__(self, openid: str, profile: Optional[Dict[str, Any]] = None):
        self.openid = openid
        self.profile = profile
        self.message: Optional[str] = "初始化中..."
        self.is_running = False
        self.websocket_clients: List[TeacherMateWebSocketClient] = []
        self.asyncio_tasks: List[asyncio.Task] = []
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.shutdown_event = threading.Event()
        self.result_queue = queue.Queue()
        self.active_signs: List[Dict[str, Any]] = []
        self.pending_sign_keys: Set[Tuple[Any, Any]] = set()
        self.sign_results: Dict[Tuple[Any, Any], Dict[str, Any]] = {}
        self.latest_result_meta: Optional[Dict[str, Any]] = None

    def start(self) -> None:
        if self.is_running:
            return
        self.is_running = True
        thread = threading.Thread(target=self._run_async, daemon=True, name=f"Pipeline-{self.openid[-4:]}")
        thread.start()
        logger.info("用户 %s 的监听管道已启动", mask_openid(self.openid))

    def stop(self) -> None:
        self.shutdown_event.set()
        self.is_running = False

    def _run_async(self) -> None:
        try:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(self._main_async())
        except Exception as exc:
            logger.error("异步运行失败 %s: %s", self.openid, exc)
        finally:
            self.is_running = False
            if self.loop and not self.loop.is_closed():
                self.loop.close()

    async def _main_async(self) -> None:
        try:
            while not self.shutdown_event.is_set():
                data = await self.wait_data()
                if self.shutdown_event.is_set():
                    break

                if isinstance(data, list) and data:
                    self.message = "检测到签到任务，正在处理中..."
                    await self.process_signatures(data)
                else:
                    self.message = data if isinstance(data, str) else "暂无活跃签到"

                await asyncio.sleep(2)
        except Exception as exc:
            logger.error("主循环失败: %s", exc)
            self.message = f"错误: {exc}"
        finally:
            await self.shutdown()

    async def wait_data(self) -> Any:
        try:
            data = getData(self.openid)
            if data and isinstance(data, list) and len(data) > 0:
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

    async def process_signatures(self, data: List[Dict[str, Any]]) -> None:
        for item in data:
            sign_key = get_sign_key(item)

            if item.get("isQR"):
                if any(ws.sign_id == item["signId"] and not ws.is_shutting_down for ws in self.websocket_clients):
                    continue

                self.message = "检测到二维码签到，正在获取二维码..."
                task = asyncio.create_task(self.run_websocket_client(item))
                self._track_task(task)
                continue

            if sign_key in self.pending_sign_keys or sign_key in self.sign_results:
                continue

            self.pending_sign_keys.add(sign_key)
            self.message = f"检测到 {item.get('signType') or '普通签到'}，正在自动签到..."
            task = asyncio.create_task(self.run_common_sign(item))
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
        sign_id = item["signId"]
        course_id = item["courseId"]
        try:
            client_id = ad.creatClientId(sign_id, courseId=course_id)
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

    async def run_common_sign(self, item: Dict[str, Any]) -> None:
        sign_key = get_sign_key(item)
        try:
            lat = safe_float(item.get("lat", item.get("latitude")), 0.0)
            lon = safe_float(item.get("lon", item.get("longitude")), 0.0)
            sign_result = submit_sign(
                self.openid,
                item["courseId"],
                item["signId"],
                lat=lat,
                lon=lon,
            )
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
        try:
            qr_url = str(url)
            sign_key, result_meta = self._ensure_result_meta(item, "获取成功")
            result_meta.update(
                {
                    "qr_ready": True,
                    "result_ready": True,
                    "frontend_message": "二维码已获取，请扫码签到",
                    "qr_url": qr_url,
                    "status_message": "获取成功",
                }
            )
            self._store_result_meta(sign_key, result_meta)
            self.message = "获取成功"

            if pushplus_notifier.enabled:
                notify_task = asyncio.create_task(
                    asyncio.to_thread(pushplus_notifier.send_qr_url, self.openid, item, qr_url)
                )
                self._track_task(notify_task)
        except Exception as exc:
            self.message = f"解析二维码失败: {exc}"

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
        for client in self.websocket_clients:
            if not client.is_shutting_down:
                await client.graceful_shutdown()
        for task in list(self.asyncio_tasks):
            task.cancel()
        self.pending_sign_keys.clear()
        self.websocket_clients.clear()
        self.asyncio_tasks.clear()

    def get_status(self) -> Dict[str, Any]:
        if not self.active_signs:
            result_meta = build_result_meta(None, self.message or "暂无状态")
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

        pipeline = Pipeline(openid, profile=profile)
        active_pipelines[openid] = pipeline
        pipeline.start()
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

    def start(self) -> None:
        with self._state_lock:
            if self._started:
                return
            self._started = True

        self.refresh_openid(reason="startup", allow_file_fallback=True)
        self._set_next_refresh_time()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="OpenIdRefresh")
        self._thread.start()

    def _run_loop(self) -> None:
        while not self._stop_event.wait(self.interval_seconds):
            self.refresh_openid(reason="scheduled", allow_file_fallback=False)
            self._set_next_refresh_time()

    def refresh_openid(self, reason: str, allow_file_fallback: bool) -> bool:
        if not self._refresh_lock.acquire(blocking=False):
            logger.info("OpenID 刷新跳过：已有刷新任务正在执行")
            return False

        with self._state_lock:
            self.is_refreshing = True

        errors: List[str] = []
        try:
            record = None
            source = None

            try:
                collected = self.collector.run_once()
                record = self._validate_record(collected)
                source = "collector"
            except Exception as exc:
                logger.exception("OpenID 采集失败 (%s): %s", reason, exc)
                errors.append(f"OpenID 采集失败: {exc}")

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
                return True

            with self._state_lock:
                if errors:
                    self.last_error = "；".join(errors)
            return False
        finally:
            with self._state_lock:
                self.is_refreshing = False
            self._refresh_lock.release()

    def use_manual_openid(self, openid: str) -> Pipeline:
        record = self._validate_record({"openid": openid, "captured_at": datetime.now().astimezone().isoformat()})
        self._apply_record(record, source="manual")
        pipeline = self.get_current_pipeline()
        if pipeline:
            return pipeline
        return activate_pipeline_for_openid(record["openid"], record.get("profile"))

    def clear_current_state(self, reason: Optional[str] = None) -> None:
        stop_all_pipelines()
        with self._state_lock:
            self.current_openid = None
            self.current_profile = None
            self.current_source = None
            self.current_url = None
            self.used_file_fallback = False
            self.last_error = reason or self.last_error

    def get_current_pipeline(self) -> Optional[Pipeline]:
        with self._state_lock:
            openid = self.current_openid
        return get_pipeline(openid)

    def get_public_state(self) -> Dict[str, Any]:
        with self._state_lock:
            return {
                "openid": self.current_openid,
                "openid_masked": mask_openid(self.current_openid),
                "collector_method": getattr(self.collector, "method_name", "unknown"),
                "current_source": self.current_source,
                "current_url": self.current_url,
                "last_refresh_at": self.last_refresh_at,
                "next_refresh_at": self.next_refresh_at,
                "last_error": self.last_error,
                "is_refreshing": self.is_refreshing,
                "used_file_fallback": self.used_file_fallback,
                "profile": self.current_profile,
            }

    def build_waiting_message(self) -> str:
        state = self.get_public_state()
        if state["is_refreshing"]:
            return "正在自动获取 OpenID，请稍候..."
        if state["last_error"]:
            return state["last_error"]
        return "未登录"

    def _validate_record(self, record: Dict[str, Any]) -> Dict[str, Any]:
        openid = str(record.get("openid") or "").strip()
        if not OPENID_HEX_RE.fullmatch(openid):
            raise OpenIdCollectorError("OpenID 格式不正确，应为 32 位十六进制字符串")

        api_response = getData(openid)
        if isinstance(api_response, dict) and "message" in api_response:
            raise OpenIdCollectorError(f"OpenID 无效: {api_response['message']}")
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
        pipeline = activate_pipeline_for_openid(record["openid"], record.get("profile"))
        profile = pipeline.profile or record.get("profile")
        refresh_time = datetime.now().astimezone().isoformat()

        with self._state_lock:
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
        next_time = datetime.now().astimezone() + timedelta(seconds=self.interval_seconds)
        with self._state_lock:
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
    token=os.getenv("PUSHPLUS_TOKEN"),
    topic=os.getenv("PUSHPLUS_TOPIC"),
)
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
                    "profile": pipeline.profile,
                    "current_sign": pipeline.active_signs[0] if pipeline.active_signs else None,
                    "openid_status": openid_refresh_manager.get_public_state(),
                }
            )
        )
        resp.set_cookie("user_openid", openid, max_age=7200)
        return resp
    except Exception as exc:
        logger.error("登录验证错误: %s", exc)
        return jsonify({"success": False, "message": str(exc)})


@app.route("/api/check_session")
def check_session():
    ensure_runtime_initialized()
    state = openid_refresh_manager.get_public_state()
    pipeline = openid_refresh_manager.get_current_pipeline()

    if state.get("openid") and pipeline:
        return jsonify(
            {
                "valid": True,
                "openid": state["openid"],
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
    resp.delete_cookie("user_openid")
    return resp


@app.route("/api/openid_status")
def openid_status():
    ensure_runtime_initialized()
    return jsonify(openid_refresh_manager.get_public_state())


@app.route("/qr_code")
def qr_code_status():
    ensure_runtime_initialized()
    pipeline = openid_refresh_manager.get_current_pipeline()
    if not pipeline:
        return jsonify(build_empty_status())

    status = pipeline.get_status()
    status["openid_status"] = openid_refresh_manager.get_public_state()
    return jsonify(status)


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
