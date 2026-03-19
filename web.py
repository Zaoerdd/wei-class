from __future__ import annotations

import argparse
import asyncio
import html
import io
import json
import logging
import os
import queue
import re
import socket
import subprocess
import tempfile
import threading
import time
import sys
import winreg
import zipfile
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlsplit, urlunsplit

import requests
from flask import Flask, jsonify, make_response, render_template, request, send_file

import ad
from getSocket import TeacherMateWebSocketClient
from getdata import getData, get_student_profile, submit_sign
from runtime_config import LOCAL_CONFIG_PATH, get_runtime_settings
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
HEX32_TEXT_RE = re.compile(r"(?<![a-fA-F0-9])[a-fA-F0-9]{32}(?![a-fA-F0-9])")
OPENID_QUERY_RE = re.compile(r"(?i)(openid=)([a-fA-F0-9]{32})")
SUPPORT_BUNDLE_SCHEMA_VERSION = 1
SUPPORT_BUNDLE_MAX_TEXT_BYTES = 128 * 1024
SUPPORT_BUNDLE_ENV_KEYS = {
    "ALL_PROXY",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
}
PROXY_RELATED_KEYS = {
    "allproxy",
    "autoconfigurl",
    "effectiveproxyserver",
    "httpproxy",
    "httpsproxy",
    "noproxy",
    "proxyoverride",
    "proxyserver",
}
SENSITIVE_STRUCTURED_KEYS = {
    "accesstoken",
    "authorization",
    "cookie",
    "cookies",
    "manualopenid",
    "openid",
    "password",
    "pushplustoken",
    "refreshtoken",
    "secret",
    "token",
    "useropenid",
}
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


def normalize_sensitive_key(key: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(key or "").lower())


def redact_secret_token(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "[redacted]"
    if len(text) <= 8:
        return "[redacted]"
    return f"{text[:2]}***{text[-2:]}"


def redact_sensitive_text(value: str) -> str:
    redacted = OPENID_QUERY_RE.sub(lambda match: f"{match.group(1)}{mask_openid(match.group(2)) or '[redacted]'}", value)
    return HEX32_TEXT_RE.sub(lambda match: mask_openid(match.group(0)) or "[redacted]", redacted)


def redact_proxy_endpoint(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return text

    try:
        parsed = urlsplit(text)
    except Exception:
        parsed = None

    if parsed and parsed.scheme and (parsed.username or parsed.password):
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        if parsed.port:
            host = f"{host}:{parsed.port}"
        netloc = f"[redacted]@{host}" if host else "[redacted]"
        return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))

    if "@" in text:
        userinfo, rest = text.rsplit("@", 1)
        if ":" in userinfo and rest:
            return f"[redacted]@{rest}"
    return text


def redact_proxy_value(value: str) -> str:
    parts: List[str] = []
    for segment in str(value or "").split(";"):
        item = segment.strip()
        if not item:
            continue
        if "=" in item and "://" not in item:
            key, item_value = item.split("=", 1)
            parts.append(f"{key}={redact_proxy_endpoint(item_value)}")
            continue
        parts.append(redact_proxy_endpoint(item))
    return ";".join(parts)


def redact_sensitive_value(value: Any, key: Optional[str] = None) -> Any:
    if isinstance(value, dict):
        return {item_key: redact_sensitive_value(item_value, item_key) for item_key, item_value in value.items()}

    if isinstance(value, list):
        return [redact_sensitive_value(item, key) for item in value]

    if value in (None, "", False):
        return value

    normalized_key = normalize_sensitive_key(key)
    if normalized_key.endswith("masked"):
        return value

    if isinstance(value, str):
        if normalized_key in PROXY_RELATED_KEYS:
            return redact_sensitive_text(redact_proxy_value(value))
        if "openid" in normalized_key:
            return mask_openid(value) or "[redacted]"
        if normalized_key in SENSITIVE_STRUCTURED_KEYS:
            return redact_secret_token(value)
        return redact_sensitive_text(value)

    if normalized_key in SENSITIVE_STRUCTURED_KEYS:
        return "[redacted]"

    return value


def read_text_tail(path: Path, max_bytes: int = SUPPORT_BUNDLE_MAX_TEXT_BYTES) -> str:
    data = path.read_bytes()
    if len(data) > max_bytes:
        data = data[-max_bytes:]
    return data.decode("utf-8", errors="replace")


def build_support_runtime_snapshot(runtime_state: Dict[str, Any]) -> Dict[str, Any]:
    openid_status = runtime_state.get("openid_status") or {}
    pipeline_status = runtime_state.get("pipeline_status") or {}
    summary = runtime_state.get("summary") or {}
    current_sign = get_runtime_current_sign(pipeline_status)
    sign_snapshot = None
    if isinstance(current_sign, dict):
        sign_snapshot = {
            "name": current_sign.get("name"),
            "courseId": current_sign.get("courseId"),
            "signId": current_sign.get("signId"),
            "signType": current_sign.get("signType"),
            "startTime": current_sign.get("startTime"),
        }

    return redact_sensitive_value(
        {
            "schema_version": runtime_state.get("schema_version"),
            "generated_at": runtime_state.get("generated_at"),
            "summary": summary,
            "openid_status": {
                "collector_method": openid_status.get("collector_method"),
                "current_source": openid_status.get("current_source"),
                "openid": openid_status.get("openid"),
                "openid_masked": openid_status.get("openid_masked"),
                "used_file_fallback": openid_status.get("used_file_fallback"),
                "is_refreshing": openid_status.get("is_refreshing"),
                "last_refresh_at": openid_status.get("last_refresh_at"),
                "next_refresh_at": openid_status.get("next_refresh_at"),
                "last_error": openid_status.get("last_error"),
            },
            "pipeline_status": {
                "has_pipeline": pipeline_status.get("has_pipeline"),
                "is_running": pipeline_status.get("is_running"),
                "message": pipeline_status.get("message"),
                "success": pipeline_status.get("success"),
                "active_sign_count": pipeline_status.get("active_sign_count"),
                "result_meta": pipeline_status.get("result_meta"),
                "current_sign": sign_snapshot,
            },
            "home_status": build_frontend_home_status(runtime_state),
        }
    )


def build_template_snapshot() -> Dict[str, Any]:
    runtime_settings = get_runtime_settings(reload=True)
    template_dir = Path(getattr(collector, "template_dir", None) or runtime_settings.get("cv.template_dir"))
    override_dir = Path(getattr(collector, "template_override_dir", None) or runtime_settings.get("cv.template_override_dir"))
    template_names = dict(
        getattr(
            collector,
            "template_names",
            {
                "session": runtime_settings.get("cv.templates.session"),
                "menu_button": runtime_settings.get("cv.templates.menu_button"),
                "menu_item": runtime_settings.get("cv.templates.menu_item"),
                "close": runtime_settings.get("cv.templates.close"),
            },
        )
    )
    source_labels = {
        "override": "本机覆盖",
        "default": "默认模板",
        "configured": "自定义路径",
        "missing": "缺失",
    }
    counts = {
        "total": len(template_names),
        "default": 0,
        "override": 0,
        "configured": 0,
        "missing": 0,
    }
    latest_updated_at: Optional[str] = None
    latest_timestamp: Optional[float] = None
    missing_roles: List[str] = []
    override_roles: List[str] = []
    default_roles: List[str] = []
    configured_roles: List[str] = []
    items: List[Dict[str, Any]] = []

    for role, filename in sorted(template_names.items()):
        role_path = Path(filename)
        override_path = override_dir / role_path.name
        default_path = template_dir / role_path
        resolved_path = None
        source = "missing"
        exists = False

        if role_path.is_absolute():
            default_path = role_path
            if role_path.exists():
                resolved_path = role_path
                source = "configured"
                exists = True
        else:
            if override_path.exists():
                resolved_path = override_path
                source = "override"
                exists = True
            elif default_path.exists():
                resolved_path = default_path
                source = "default"
                exists = True

        if exists and resolved_path:
            counts[source] += 1
            updated_at = datetime.fromtimestamp(resolved_path.stat().st_mtime).astimezone().isoformat()
            if latest_timestamp is None or resolved_path.stat().st_mtime > latest_timestamp:
                latest_timestamp = resolved_path.stat().st_mtime
                latest_updated_at = updated_at
        else:
            counts["missing"] += 1
            updated_at = None
            missing_roles.append(role)

        if source == "override":
            override_roles.append(role)
        elif source == "default":
            default_roles.append(role)
        elif source == "configured":
            configured_roles.append(role)

        items.append(
            {
                "role": role,
                "filename": filename,
                "source": source,
                "source_label": source_labels.get(source, source),
                "exists": exists,
                "default_path": str(default_path),
                "default_exists": default_path.exists(),
                "override_path": str(override_path),
                "override_exists": override_path.exists(),
                "resolved_path": str(resolved_path) if resolved_path else None,
                "file_size": resolved_path.stat().st_size if resolved_path and resolved_path.exists() else None,
                "updated_at": updated_at,
            }
        )

    if counts["missing"]:
        status = "fail"
        summary = f"还有 {counts['missing']} 个模板文件缺失。"
    elif counts["override"] or counts["configured"]:
        status = "pass"
        summary = "所有模板都已就绪，当前包含本机覆盖或自定义路径。"
    else:
        status = "pass"
        summary = "所有模板都来自默认模板目录。"

    return {
        "generated_at": datetime.now().astimezone().isoformat(),
        "template_dir": str(template_dir),
        "template_override_dir": str(override_dir),
        "template_dir_exists": template_dir.exists(),
        "template_override_dir_exists": override_dir.exists(),
        "counts": counts,
        "status": status,
        "summary": summary,
        "missing_roles": missing_roles,
        "override_roles": override_roles,
        "default_roles": default_roles,
        "configured_roles": configured_roles,
        "latest_updated_at": latest_updated_at,
        "templates": items,
    }


def build_environment_snapshot() -> Dict[str, Any]:
    tracked_env = {}
    for key in sorted(os.environ):
        if key.startswith("WECHAT_") or key.startswith("PUSHPLUS_") or key in SUPPORT_BUNDLE_ENV_KEYS:
            tracked_env[key] = redact_sensitive_value(os.environ.get(key), key)

    return {
        "generated_at": datetime.now().astimezone().isoformat(),
        "base_dir": str(BASE_DIR),
        "python_executable": sys.executable,
        "python_version": sys.version,
        "platform": sys.platform,
        "local_config_path": str(LOCAL_CONFIG_PATH),
        "collector_method": getattr(collector, "method_name", "unknown"),
        "environment": tracked_env,
    }


def build_local_config_snapshot() -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "path": str(LOCAL_CONFIG_PATH),
        "exists": LOCAL_CONFIG_PATH.exists(),
    }
    if not LOCAL_CONFIG_PATH.exists():
        return payload

    try:
        payload["content"] = redact_sensitive_value(json.loads(LOCAL_CONFIG_PATH.read_text(encoding="utf-8")))
    except Exception as exc:
        payload["read_error"] = str(exc)
        payload["raw_preview"] = redact_sensitive_text(read_text_tail(LOCAL_CONFIG_PATH, max_bytes=16 * 1024))
    return payload


def build_support_bundle_health_report(
    runtime_state: Dict[str, Any],
    listener_state: Dict[str, Any],
) -> Dict[str, Any]:
    report = build_health_report(runtime_state=runtime_state, listener_state=listener_state)
    report["runtime_state"] = build_support_runtime_snapshot(runtime_state)
    return redact_sensitive_value(report)


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
        self.invalid_openid_refresh_requested = False

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

    def request_refresh_async(self, reason: str, allow_file_fallback: bool = False) -> bool:
        def _runner() -> None:
            try:
                self.refresh_openid(reason=reason, allow_file_fallback=allow_file_fallback)
            finally:
                self._set_next_refresh_time()

        refresh_thread = threading.Thread(
            target=_runner,
            daemon=True,
            name=f"OpenIdRefreshRequest-{int(time.time() * 1000)}",
        )
        refresh_thread.start()
        return True

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
collector_config = None
pushplus_notifier = None
collector_method = "uiautomation"
collector = None
openid_refresh_manager = None

app = Flask(__name__)
runtime_init_lock = threading.Lock()
template_capture_lock = threading.Lock()
runtime_initialized = False
RUNTIME_STATE_SCHEMA_VERSION = 1
HEALTH_STATUS_ORDER = {"fail": 0, "warn": 1, "pass": 2, "skip": 3}


def configure_runtime(openid_method: Optional[str] = None) -> None:
    global collector_config, pushplus_notifier, collector_method, collector, openid_refresh_manager, runtime_initialized

    runtime_settings = get_runtime_settings(reload=True)
    collector_config = CollectorConfig(
        session_name=runtime_settings.get("wechat.session_name"),
        menu_button_prefix=runtime_settings.get("wechat.menu_button"),
        menu_item_prefix=runtime_settings.get("wechat.menu_item"),
        interval_hours=2.0,
        control_timeout_seconds=runtime_settings.get("wechat.control_timeout_seconds"),
        browser_timeout_seconds=runtime_settings.get("wechat.browser_timeout_seconds"),
        output_path=COLLECTOR_OUTPUT_PATH,
        log_path=COLLECTOR_LOG_PATH,
    )
    pushplus_notifier = PushPlusNotifier(
        token=runtime_settings.get("pushplus.token"),
        topic=runtime_settings.get("pushplus.topic"),
    )

    selected_method = normalize_openid_method(openid_method or runtime_settings.get("openid.method"))
    collector_method = selected_method
    collector = build_openid_collector(selected_method, collector_config, collector_logger)
    openid_refresh_manager = OpenIdRefreshManager(
        collector=collector,
        cache_path=OPENID_CACHE_PATH,
        interval_hours=2.0,
    )
    runtime_initialized = False


configure_runtime()


def ensure_runtime_configured() -> None:
    if openid_refresh_manager is None:
        raise RuntimeError("OpenID runtime is not configured")


def ensure_runtime_initialized() -> None:
    global runtime_initialized
    if runtime_initialized:
        return

    with runtime_init_lock:
        if runtime_initialized:
            return
        ensure_runtime_configured()
        openid_refresh_manager.start()
        runtime_initialized = True


def build_pipeline_runtime_state(
    pipeline: Optional[Pipeline],
    *,
    fallback_message: str,
    fallback_profile: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if pipeline:
        pipeline_state = dict(pipeline.get_status())
        pipeline_state["has_pipeline"] = True
        return pipeline_state

    result_meta = build_result_meta(None, fallback_message)
    return {
        "has_pipeline": False,
        "success": 0,
        "message": fallback_message,
        "qr_url": None,
        "is_running": False,
        "profile": fallback_profile,
        "active_sign_count": 0,
        "active_signs": [],
        "current_sign": None,
        "result_meta": result_meta,
    }


def build_runtime_summary(
    openid_state: Dict[str, Any],
    pipeline_state: Dict[str, Any],
) -> Dict[str, Any]:
    result_meta = pipeline_state.get("result_meta") or {}
    active_sign_count = safe_int(pipeline_state.get("active_sign_count")) or 0
    status_message = str(
        pipeline_state.get("message")
        or openid_state.get("last_error")
        or openid_refresh_manager.build_waiting_message()
    )

    return {
        "session_valid": bool(openid_state.get("openid") and pipeline_state.get("has_pipeline")),
        "has_openid": bool(openid_state.get("openid")),
        "has_pipeline": bool(pipeline_state.get("has_pipeline")),
        "collector_method": openid_state.get("collector_method"),
        "current_source": openid_state.get("current_source"),
        "is_refreshing": bool(openid_state.get("is_refreshing")),
        "used_file_fallback": bool(openid_state.get("used_file_fallback")),
        "active_sign_count": active_sign_count,
        "has_active_signs": active_sign_count > 0,
        "qr_ready": bool(result_meta.get("qr_ready")),
        "sign_completed": bool(result_meta.get("sign_completed")),
        "result_ready": bool(result_meta.get("result_ready")),
        "has_error": bool(result_meta.get("is_error") or openid_state.get("last_error")),
        "status_message": status_message,
    }


def build_runtime_state(message: Optional[str] = None) -> Dict[str, Any]:
    openid_state = openid_refresh_manager.get_public_state()
    pipeline = get_pipeline(openid_state.get("openid"))
    fallback_message = message or openid_refresh_manager.build_waiting_message()
    pipeline_state = build_pipeline_runtime_state(
        pipeline,
        fallback_message=fallback_message,
        fallback_profile=openid_state.get("profile"),
    )
    return {
        "schema_version": RUNTIME_STATE_SCHEMA_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(),
        "openid_status": openid_state,
        "pipeline_status": pipeline_state,
        "summary": build_runtime_summary(openid_state, pipeline_state),
    }


def get_runtime_current_sign(pipeline_state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    active_signs = pipeline_state.get("active_signs") or []
    if active_signs:
        return active_signs[0]
    return pipeline_state.get("current_sign")


def build_frontend_mitm_state(runtime_state: Dict[str, Any]) -> Dict[str, Any]:
    method = runtime_state["summary"].get("collector_method")
    if method != "cv":
        return {
            "required": False,
            "status": "skip",
            "summary": "当前模式不需要 mitmproxy。",
            "detail": "旧版微信的 uiautomation 模式不会依赖本地抓包代理。",
            "endpoint": None,
            "mitmdump_exists": None,
        }

    listener_state = inspect_capture_proxy_listener_state()
    endpoint = f"{listener_state['host']}:{listener_state['port']}"
    if listener_state["reachable"]:
        return {
            "required": True,
            "status": "pass",
            "summary": "mitmproxy 监听正常。",
            "detail": f"当前可连接到 {endpoint}。",
            "endpoint": endpoint,
            "mitmdump_exists": listener_state["mitmdump_exists"],
        }

    if listener_state["mitmdump_exists"]:
        return {
            "required": True,
            "status": "fail",
            "summary": "mitmproxy 还没有启动。",
            "detail": f"{endpoint} 当前不可连接，cv 模式无法抓取新的 OpenID。",
            "endpoint": endpoint,
            "mitmdump_exists": True,
        }

    return {
        "required": True,
        "status": "fail",
        "summary": "还没有找到 mitmproxy 环境。",
        "detail": "未检测到 mitmdump，可先创建 .venv-mitm 并安装 mitmproxy。",
        "endpoint": endpoint,
        "mitmdump_exists": False,
    }


def build_frontend_home_status(runtime_state: Dict[str, Any]) -> Dict[str, Any]:
    summary = runtime_state["summary"]
    openid_state = runtime_state["openid_status"]
    method = summary.get("collector_method")
    mitm_state = build_frontend_mitm_state(runtime_state)
    last_error = openid_state.get("last_error")

    if summary["session_valid"] and not openid_state.get("used_file_fallback"):
        readiness = "ready"
        title = "现在可以直接用"
        description = "当前已经拿到有效 OpenID，首页会继续自动监听新的签到任务。"
        next_action = "保持微信和本页面打开即可。"
    elif summary["session_valid"] and openid_state.get("used_file_fallback"):
        readiness = "attention"
        title = "当前能用，但依赖缓存回退"
        description = "服务已经恢复到可用状态，不过这次不是刚采到的新 OpenID。"
        next_action = "如果想恢复自动采集，先把微信退回到微助教聊天页，再点上方环境体检或刷新。"
    elif method == "cv" and mitm_state["status"] == "fail" and not summary["has_openid"]:
        readiness = "blocked"
        title = "现在还不能稳定使用"
        description = "当前是新版微信 cv 模式，但抓包链路还没就绪，所以拿不到新的 OpenID。"
        next_action = "先启动 mitmproxy，必要时再打开环境体检页逐项检查。"
    elif summary["is_refreshing"]:
        readiness = "attention"
        title = "正在准备自动监听"
        description = "系统正在自动获取 OpenID，这一轮完成后会继续进入监听状态。"
        next_action = "保持微信窗口可见，等待本轮刷新完成。"
    elif last_error:
        readiness = "blocked"
        title = "当前自动化链路有阻塞"
        description = "服务已经启动，但最近一次自动获取 OpenID 失败了。"
        next_action = "优先查看最近错误、mitm 状态和环境体检页给出的下一步。"
    else:
        readiness = "attention"
        title = "还在等待运行条件满足"
        description = "服务本身已启动，但现在还没有拿到可用 OpenID。"
        next_action = "打开微信并进入微助教服务号聊天页，然后再刷新首页。"

    return {
        "readiness": readiness,
        "title": title,
        "description": description,
        "next_action": next_action,
        "mitm": mitm_state,
    }


def build_frontend_status_payload(runtime_state: Dict[str, Any]) -> Dict[str, Any]:
    pipeline_state = dict(runtime_state["pipeline_status"])
    pipeline_state["openid_status"] = runtime_state["openid_status"]
    pipeline_state["runtime_summary"] = runtime_state["summary"]
    pipeline_state["home_status"] = build_frontend_home_status(runtime_state)
    pipeline_state["generated_at"] = runtime_state["generated_at"]
    return pipeline_state


def build_openid_status_payload(runtime_state: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(runtime_state["openid_status"])
    payload["runtime_summary"] = runtime_state["summary"]
    payload["generated_at"] = runtime_state["generated_at"]
    return payload


def build_session_payload(runtime_state: Dict[str, Any]) -> Dict[str, Any]:
    openid_state = runtime_state["openid_status"]
    pipeline_state = runtime_state["pipeline_status"]
    summary = runtime_state["summary"]
    payload = {
        "valid": summary["session_valid"],
        "openid_status": openid_state,
        "runtime_summary": summary,
        "generated_at": runtime_state["generated_at"],
        "auto_mode": True,
    }
    if summary["session_valid"]:
        payload.update(
            {
                "openid": openid_state.get("openid"),
                "profile": pipeline_state.get("profile") or openid_state.get("profile"),
                "current_sign": get_runtime_current_sign(pipeline_state),
            }
        )
    else:
        payload["message"] = summary["status_message"]
    return payload


def build_empty_status(message: Optional[str] = None) -> Dict[str, Any]:
    openid_state = openid_refresh_manager.get_public_state()
    fallback_message = message or openid_refresh_manager.build_waiting_message()
    pipeline_state = build_pipeline_runtime_state(
        None,
        fallback_message=fallback_message,
        fallback_profile=openid_state.get("profile"),
    )
    runtime_state = {
        "schema_version": RUNTIME_STATE_SCHEMA_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(),
        "openid_status": openid_state,
        "pipeline_status": pipeline_state,
        "summary": build_runtime_summary(openid_state, pipeline_state),
    }
    return build_frontend_status_payload(runtime_state)


def build_health_check(
    check_id: str,
    title: str,
    status: str,
    summary: str,
    *,
    category: str = "environment",
    detail: Optional[str] = None,
    action: Optional[str] = None,
    facts: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    return {
        "id": check_id,
        "title": title,
        "category": category,
        "status": status,
        "summary": summary,
        "detail": detail,
        "action": action,
        "facts": list(facts or []),
    }


def is_path_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except Exception:
        return False


def get_capture_proxy_endpoint() -> Tuple[str, int]:
    runtime_settings = get_runtime_settings(reload=True)
    host = str(getattr(collector, "capture_proxy_host", None) or runtime_settings.get("cv.proxy_host")).strip()
    port = safe_int(getattr(collector, "capture_proxy_port", None) or runtime_settings.get("cv.proxy_port")) or 8080
    return host or "127.0.0.1", port


def normalize_proxy_server(proxy_server: Optional[str]) -> Optional[str]:
    raw = str(proxy_server or "").strip()
    if not raw:
        return None
    if "=" not in raw:
        return raw

    entries: Dict[str, str] = {}
    first_value = None
    for item in raw.split(";"):
        item = item.strip()
        if not item or "=" not in item:
            continue
        scheme, value = item.split("=", 1)
        scheme = scheme.strip().lower()
        value = value.strip()
        if not value:
            continue
        entries[scheme] = value
        if first_value is None:
            first_value = value

    for preferred in ("http", "https"):
        if entries.get(preferred):
            return entries[preferred]
    return first_value


def read_system_proxy_settings() -> Dict[str, Any]:
    values = {
        "proxy_enable": 0,
        "proxy_server": None,
        "effective_proxy_server": None,
        "auto_config_url": None,
        "auto_detect": 0,
        "proxy_override": None,
    }
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            for name in ("ProxyEnable", "ProxyServer", "AutoConfigURL", "AutoDetect", "ProxyOverride"):
                try:
                    value, _ = winreg.QueryValueEx(key, name)
                except OSError:
                    continue
                if name == "ProxyEnable":
                    values["proxy_enable"] = safe_int(value) or 0
                elif name == "ProxyServer":
                    values["proxy_server"] = str(value).strip() or None
                elif name == "AutoConfigURL":
                    values["auto_config_url"] = str(value).strip() or None
                elif name == "AutoDetect":
                    values["auto_detect"] = safe_int(value) or 0
                elif name == "ProxyOverride":
                    values["proxy_override"] = str(value).strip() or None
    except OSError as exc:
        values["read_error"] = str(exc)

    values["effective_proxy_server"] = normalize_proxy_server(values.get("proxy_server"))
    return values


def is_tcp_endpoint_reachable(host: str, port: int, timeout_seconds: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_seconds):
            return True
    except OSError:
        return False


def run_powershell_json(script: str, args: List[str], timeout_seconds: float = 8.0) -> Dict[str, Any]:
    temp_script_path = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".ps1", delete=False) as temp_script:
            temp_script.write("param([string[]]$PythonArgs)\n")
            temp_script.write("Set-Variable -Name args -Value $PythonArgs -Scope Local\n")
            temp_script.write(script)
            temp_script_path = temp_script.name

        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                temp_script_path,
                *args,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    finally:
        if temp_script_path:
            Path(temp_script_path).unlink(missing_ok=True)

    stdout_lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if completed.returncode != 0:
        error_text = completed.stderr.strip() or (stdout_lines[-1] if stdout_lines else "") or f"powershell exited {completed.returncode}"
        raise RuntimeError(error_text)
    if not stdout_lines:
        raise RuntimeError("powershell returned no json output")
    return json.loads(stdout_lines[-1])


def inspect_mitm_certificate_state() -> Dict[str, Any]:
    runtime_settings = get_runtime_settings(reload=True)
    cert_path = runtime_settings.get("mitm.cert_path")
    result = {
        "cert_path": str(cert_path),
        "exists": cert_path.exists(),
        "trusted": False,
        "subject": None,
        "thumbprint": None,
        "not_after": None,
        "error": None,
    }
    if not cert_path.exists():
        return result

    script = (
        "$cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($args[0]); "
        "$existing = Get-ChildItem Cert:\\CurrentUser\\Root | Where-Object { $_.Thumbprint -eq $cert.Thumbprint }; "
        "[pscustomobject]@{"
        "trusted = [bool]$existing; "
        "subject = $cert.Subject; "
        "thumbprint = $cert.Thumbprint; "
        "not_after = $cert.NotAfter.ToString('o')"
        "} | ConvertTo-Json -Compress"
    )
    try:
        payload = run_powershell_json(script, [str(cert_path)])
        result.update(
            {
                "trusted": bool(payload.get("trusted")),
                "subject": payload.get("subject"),
                "thumbprint": payload.get("thumbprint"),
                "not_after": payload.get("not_after"),
            }
        )
    except Exception as exc:
        result["error"] = str(exc)
    return result


def inspect_capture_proxy_listener_state() -> Dict[str, Any]:
    runtime_settings = get_runtime_settings(reload=True)
    host, port = get_capture_proxy_endpoint()
    mitmdump_path = runtime_settings.get("mitm.dump_path")
    result_path = Path(
        getattr(collector, "mitm_result_path", None)
        or runtime_settings.get("mitm.output_path")
    )
    return {
        "host": host,
        "port": port,
        "reachable": is_tcp_endpoint_reachable(host, port, timeout_seconds=1.0),
        "mitmdump_path": str(mitmdump_path),
        "mitmdump_exists": mitmdump_path.exists(),
        "result_path": str(result_path),
        "result_file_exists": result_path.exists(),
    }


def build_runtime_health_check(runtime_state: Dict[str, Any]) -> Dict[str, Any]:
    summary = runtime_state["summary"]
    openid_state = runtime_state["openid_status"]
    collector_method_name = openid_state.get("collector_method") or "unknown"
    facts = [
        {"label": "运行方式", "value": collector_method_name},
        {"label": "当前来源", "value": openid_state.get("current_source") or "-"},
        {"label": "上次刷新", "value": openid_state.get("last_refresh_at") or "-"},
        {"label": "状态消息", "value": summary.get("status_message") or "-"},
    ]
    last_error = openid_state.get("last_error")
    if last_error:
        facts.append({"label": "最近错误", "value": last_error})

    if summary["session_valid"] and not last_error and not openid_state.get("used_file_fallback"):
        status = "pass"
        health_summary = "当前已拿到有效 OpenID，服务正在自动监听。"
        action = "可以直接回首页继续使用。"
    elif summary["session_valid"] and openid_state.get("used_file_fallback"):
        status = "warn"
        health_summary = "服务已运行，但当前依赖缓存 OpenID 回退。"
        action = "如果想恢复自动采集，先把微信手动退回到微助教聊天页，再刷新首页。"
    elif summary["is_refreshing"]:
        status = "warn"
        health_summary = "服务正在自动获取 OpenID。"
        action = "保持微信窗口可见，等待本轮采集完成。"
    elif last_error:
        status = "fail"
        health_summary = "自动获取 OpenID 失败。"
        action = "优先查看下面的微信窗口、代理和模式相关检查项。"
    else:
        status = "warn"
        health_summary = "服务已启动，但还没有可用 OpenID。"
        action = "打开微信并进入微助教服务号聊天页，然后再刷新体检页。"

    return build_health_check(
        "runtime",
        "自动化运行状态",
        status,
        health_summary,
        category="runtime",
        detail=summary.get("status_message"),
        action=action,
        facts=facts,
    )


def build_python_environment_health_check() -> Dict[str, Any]:
    executable = Path(sys.executable)
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    in_virtualenv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    project_venv_root = BASE_DIR / ".venv"
    using_project_venv = is_path_within(executable, project_venv_root)

    facts = [
        {"label": "Python", "value": version},
        {"label": "解释器", "value": str(executable)},
        {"label": "虚拟环境", "value": "项目 .venv" if using_project_venv else ("其他虚拟环境" if in_virtualenv else "未使用")},
    ]

    if sys.version_info < (3, 9):
        status = "fail"
        summary = "当前 Python 版本过低。"
        action = "请使用 Python 3.9+ 重新创建 .venv 后再启动服务。"
    elif using_project_venv:
        status = "pass"
        summary = "当前服务运行在项目自己的虚拟环境中。"
        action = None
    elif in_virtualenv:
        status = "warn"
        summary = "服务运行在虚拟环境中，但不是项目的 .venv。"
        action = "建议统一使用 start_web_app.ps1，让项目自己维护依赖环境。"
    else:
        status = "warn"
        summary = "当前解释器不是虚拟环境。"
        action = "建议使用 start_web_app.ps1 或一键启动脚本，避免依赖污染。"

    return build_health_check(
        "python",
        "Python 环境",
        status,
        summary,
        action=action,
        facts=facts,
    )


def build_wechat_health_check(runtime_state: Dict[str, Any]) -> Dict[str, Any]:
    method = runtime_state["summary"].get("collector_method") or getattr(collector, "method_name", "unknown")
    facts: List[Dict[str, Any]] = [
        {"label": "运行方式", "value": method},
        {"label": "会话名", "value": collector_config.session_name},
    ]

    try:
        if method == "cv" and hasattr(collector, "_list_wechat_windows"):
            windows = collector._list_wechat_windows()
            if not windows:
                return build_health_check(
                    "wechat",
                    "微信窗口",
                    "fail",
                    "未检测到可见的微信主窗口。",
                    detail="新版微信的 cv 模式需要桌面微信窗口处于可见状态，不能最小化。",
                    action="先打开桌面微信，并把主窗口保持在前台或至少保持可见。",
                    facts=facts,
                )

            primary = windows[0]
            facts.extend(
                [
                    {"label": "窗口数量", "value": str(len(windows))},
                    {"label": "窗口标题", "value": primary.title or "-"},
                    {"label": "窗口类名", "value": primary.class_name or "-"},
                ]
            )
            return build_health_check(
                "wechat",
                "微信窗口",
                "pass",
                "已检测到可见的微信窗口。",
                detail="如果首页仍然拿不到 OpenID，通常是当前不在微助教聊天页，或模板/代理相关步骤还没完成。",
                action="确认微信当前停留在微助教服务号聊天页。",
                facts=facts,
            )

        if hasattr(collector, "find_wechat_window"):
            window = collector.find_wechat_window()
            if not window:
                return build_health_check(
                    "wechat",
                    "微信窗口",
                    "fail",
                    "未检测到微信主窗口。",
                    detail="旧版微信的 uiautomation 模式需要能直接找到桌面微信窗口。",
                    action="先打开桌面微信，并保持窗口可见。",
                    facts=facts,
                )

            title = getattr(window, "Name", "") if hasattr(window, "Name") else ""
            class_name = getattr(window, "ClassName", "") if hasattr(window, "ClassName") else ""
            if hasattr(collector, "safe_get"):
                title = collector.safe_get(lambda: window.Name, title)
                class_name = collector.safe_get(lambda: window.ClassName, class_name)

            facts.extend(
                [
                    {"label": "窗口标题", "value": title or "-"},
                    {"label": "窗口类名", "value": class_name or "-"},
                ]
            )

            if method == "uiautomation" and hasattr(collector, "find_session_control"):
                session_control = collector.find_session_control(window, collector_config.session_name)
                if session_control is None:
                    return build_health_check(
                        "wechat",
                        "微信窗口",
                        "warn",
                        "已检测到微信窗口，但还没在左侧列表中找到微助教服务号。",
                        detail="uiautomation 模式依赖左侧聊天列表里能直接看到目标会话。",
                        action="把微助教服务号固定到微信左侧列表，并保持在可见区域。",
                        facts=facts,
                    )

            return build_health_check(
                "wechat",
                "微信窗口",
                "pass",
                "已检测到微信窗口。",
                action="保持窗口可见即可。",
                facts=facts,
            )
    except Exception as exc:
        return build_health_check(
            "wechat",
            "微信窗口",
            "warn",
            "检测微信窗口时遇到异常。",
            detail=str(exc),
            action="先手动确认微信窗口已打开，再根据运行方式检查后续项。",
            facts=facts,
        )

    return build_health_check(
        "wechat",
        "微信窗口",
        "warn",
        "当前运行方式没有提供可复用的微信窗口检测器。",
        action="请手动确认桌面微信窗口已打开并保持可见。",
        facts=facts,
    )


def build_system_proxy_health_check(runtime_state: Dict[str, Any], listener_state: Dict[str, Any]) -> Dict[str, Any]:
    proxy_state = read_system_proxy_settings()
    capture_target = f"{listener_state['host']}:{listener_state['port']}".lower()
    effective_target = str(proxy_state.get("effective_proxy_server") or "").strip().lower()

    facts = [
        {"label": "代理启用", "value": "是" if proxy_state.get("proxy_enable") else "否"},
        {"label": "代理地址", "value": proxy_state.get("proxy_server") or "-"},
        {"label": "PAC", "value": proxy_state.get("auto_config_url") or "-"},
        {"label": "自动检测", "value": "是" if proxy_state.get("auto_detect") else "否"},
    ]
    if proxy_state.get("proxy_override"):
        facts.append({"label": "代理例外", "value": proxy_state["proxy_override"]})

    if proxy_state.get("read_error"):
        return build_health_check(
            "system_proxy",
            "系统代理",
            "warn",
            "读取系统代理设置时遇到异常。",
            detail=proxy_state["read_error"],
            action="如果你怀疑代理有问题，可以手动打开 Windows 代理设置检查。",
            facts=facts,
        )

    if proxy_state.get("proxy_enable") and effective_target == capture_target:
        if listener_state["reachable"]:
            if runtime_state["summary"].get("is_refreshing"):
                return build_health_check(
                    "system_proxy",
                    "系统代理",
                    "pass",
                    "系统代理当前临时指向本机 mitmproxy，且代理端口可用。",
                    detail="这在 cv 模式采集进行中是正常现象。",
                    action="等本轮采集结束后，系统代理应自动恢复。",
                    facts=facts,
                )
            return build_health_check(
                "system_proxy",
                "系统代理",
                "warn",
                "系统代理仍然指向本机 mitmproxy。",
                detail="如果这不是采集中间状态，通常说明上一次采集结束时没有完全恢复代理。",
                action="如果长时间保持这样，先停止采集或手动关闭系统代理。",
                facts=facts,
            )

        return build_health_check(
            "system_proxy",
            "系统代理",
            "fail",
            "系统代理指向本机 mitmproxy，但监听端口不可用。",
            detail="这会导致微信或浏览器流量走到一个不可用的本地代理。",
            action="先运行 start_mitmproxy_openid.ps1，或者手动关闭 Windows 系统代理。",
            facts=facts,
        )

    if proxy_state.get("proxy_enable"):
        return build_health_check(
            "system_proxy",
            "系统代理",
            "pass",
            "系统代理已启用，但没有卡在本机抓包代理上。",
            detail="如果你本来就在使用其他代理，这通常是正常状态。",
            facts=facts,
        )

    return build_health_check(
        "system_proxy",
        "系统代理",
        "pass",
        "系统代理未启用。",
        facts=facts,
    )


def build_mitm_listener_health_check(runtime_state: Dict[str, Any], listener_state: Dict[str, Any]) -> Dict[str, Any]:
    method = runtime_state["summary"].get("collector_method")
    facts = [
        {"label": "监听地址", "value": f"{listener_state['host']}:{listener_state['port']}"},
        {"label": "mitmdump", "value": listener_state["mitmdump_path"]},
        {"label": "结果文件", "value": listener_state["result_path"]},
    ]
    if method != "cv":
        return build_health_check(
            "mitm_listener",
            "mitmproxy 监听",
            "skip",
            "当前运行方式不是 cv，不需要 mitmproxy。",
            facts=facts,
        )

    if listener_state["reachable"]:
        return build_health_check(
            "mitm_listener",
            "mitmproxy 监听",
            "pass",
            "本机 mitmproxy 监听端口可连接。",
            detail="cv 模式采集时可以直接复用这个监听端口。",
            facts=facts,
        )

    action = (
        "先创建 .venv-mitm 并安装 mitmproxy，再运行 start_mitmproxy_openid.ps1。"
        if not listener_state["mitmdump_exists"]
        else "先运行 start_mitmproxy_openid.ps1，让 127.0.0.1:8080 开始监听。"
    )
    detail = (
        "未找到 mitmdump 可执行文件。"
        if not listener_state["mitmdump_exists"]
        else "监听地址当前不可连接。"
    )
    return build_health_check(
        "mitm_listener",
        "mitmproxy 监听",
        "fail",
        "cv 模式需要的 mitmproxy 监听还没有就绪。",
        detail=detail,
        action=action,
        facts=facts,
    )


def build_mitm_certificate_health_check(runtime_state: Dict[str, Any]) -> Dict[str, Any]:
    method = runtime_state["summary"].get("collector_method")
    cert_state = inspect_mitm_certificate_state()
    facts = [
        {"label": "证书路径", "value": cert_state["cert_path"]},
        {"label": "Thumbprint", "value": cert_state.get("thumbprint") or "-"},
        {"label": "过期时间", "value": cert_state.get("not_after") or "-"},
    ]

    if method != "cv":
        return build_health_check(
            "mitm_cert",
            "mitmproxy 证书",
            "skip",
            "当前运行方式不是 cv，不需要 mitmproxy 证书。",
            facts=facts,
        )

    if not cert_state["exists"]:
        return build_health_check(
            "mitm_cert",
            "mitmproxy 证书",
            "fail",
            "还没有找到 mitmproxy 根证书文件。",
            detail="第一次运行 mitmproxy 之前，证书文件不会自动出现。",
            action="先运行 .\\.venv-mitm\\Scripts\\mitmproxy.exe 生成证书，再执行 install_mitmproxy_cert.ps1。",
            facts=facts,
        )

    if cert_state["trusted"]:
        return build_health_check(
            "mitm_cert",
            "mitmproxy 证书",
            "pass",
            "mitmproxy 根证书已导入当前用户信任库。",
            detail=cert_state.get("subject"),
            facts=facts,
        )

    detail = cert_state.get("error") or "证书文件存在，但当前用户证书库里还没信任它。"
    return build_health_check(
        "mitm_cert",
        "mitmproxy 证书",
        "fail",
        "mitmproxy 根证书还没有完成信任。",
        detail=detail,
        action="运行 .\\install_mitmproxy_cert.ps1，把 mitmproxy 根证书导入 CurrentUser\\Root。",
        facts=facts,
    )


def build_cv_template_health_check(
    runtime_state: Dict[str, Any],
    template_snapshot: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    method = runtime_state["summary"].get("collector_method")
    template_snapshot = template_snapshot or build_template_snapshot()
    counts = template_snapshot.get("counts") or {}
    facts = [
        {"label": "默认目录", "value": template_snapshot.get("template_dir") or "-"},
        {"label": "本机覆盖", "value": template_snapshot.get("template_override_dir") or "-"},
        {"label": "模板数量", "value": str(counts.get("total") or 0)},
    ]

    if method != "cv":
        return build_health_check(
            "cv_templates",
            "CV 模板",
            "skip",
            "当前运行方式不是 cv，不需要模板文件。",
            facts=facts,
        )

    missing_roles = list(template_snapshot.get("missing_roles") or [])
    resolved_items = [
        f"{item.get('role')}={item.get('source_label')}"
        for item in template_snapshot.get("templates") or []
    ]
    facts.append({"label": "覆盖命中", "value": str(counts.get("override") or 0)})
    detail = "；".join(resolved_items)

    if missing_roles:
        return build_health_check(
            "cv_templates",
            "CV 模板",
            "fail",
            f"缺少 {len(missing_roles)} 个 cv 模板文件。",
            detail=detail,
            action="优先去体检页底部使用“自动采集本机模板”，或手动把缺少模板补到 cv_templates_local 后再重试。",
            facts=facts,
        )

    return build_health_check(
        "cv_templates",
        "CV 模板",
        "pass",
        "cv 模式需要的模板文件都已就绪。",
        detail=detail,
        action="如果按钮样式不匹配，可以去体检页底部重新自动采集本机模板。",
        facts=facts,
    )


def build_openid_cache_health_check(runtime_state: Dict[str, Any]) -> Dict[str, Any]:
    facts = [{"label": "缓存路径", "value": str(OPENID_CACHE_PATH)}]
    if not OPENID_CACHE_PATH.exists():
        return build_health_check(
            "openid_cache",
            "OpenID 缓存",
            "warn",
            "还没有 latest_openid.json 缓存文件。",
            detail="首次成功采集一次 OpenID 后，这里才会生成回退缓存。",
            action="先至少成功采集一次 OpenID，后续服务才能在启动时回退到缓存。",
            facts=facts,
        )

    try:
        payload = json.loads(OPENID_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        return build_health_check(
            "openid_cache",
            "OpenID 缓存",
            "fail",
            "缓存文件存在，但无法读取。",
            detail=str(exc),
            action="删除损坏的 latest_openid.json 后重新采集一次 OpenID。",
            facts=facts,
        )

    openid = str(payload.get("openid") or "").strip()
    facts.extend(
        [
            {"label": "缓存来源", "value": str(payload.get("source") or "-")},
            {"label": "采集时间", "value": str(payload.get("captured_at") or "-")},
            {"label": "OpenID", "value": mask_openid(openid) or "-"},
        ]
    )

    if not OPENID_HEX_RE.fullmatch(openid):
        return build_health_check(
            "openid_cache",
            "OpenID 缓存",
            "fail",
            "缓存文件存在，但其中的 OpenID 格式无效。",
            action="重新采集一次 OpenID，让缓存文件被覆盖成有效内容。",
            facts=facts,
        )

    current_source = runtime_state["openid_status"].get("current_source")
    detail = "当前服务正在使用这条缓存回退。" if current_source == "file" else "这条缓存可在下次启动失败时作为回退。"
    return build_health_check(
        "openid_cache",
        "OpenID 缓存",
        "pass",
        "已找到可用的 OpenID 缓存文件。",
        detail=detail,
        facts=facts,
    )


def summarize_health_checks(checks: List[Dict[str, Any]], runtime_state: Dict[str, Any]) -> Dict[str, Any]:
    counts = {"pass": 0, "warn": 0, "fail": 0, "skip": 0}
    for check in checks:
        status = check.get("status") or "warn"
        counts[status] = counts.get(status, 0) + 1

    first_fail = next((item for item in checks if item.get("status") == "fail"), None)
    first_warn = next((item for item in checks if item.get("status") == "warn"), None)

    if counts["fail"] > 0:
        overall_status = "blocked"
        tone = "danger"
        title = "还有关键部署步骤未完成"
        description = f"发现 {counts['fail']} 项关键检查未通过，当前还不适合依赖自动化稳定运行。"
        next_action = (first_fail or first_warn or {}).get("action") or "优先处理第一项失败检查。"
    elif counts["warn"] > 0:
        overall_status = "attention"
        tone = "warning"
        title = "环境基本可用，但还有注意项"
        description = f"关键能力大体已具备，但还有 {counts['warn']} 项建议处理。"
        next_action = (first_warn or {}).get("action") or "优先处理带黄色提示的检查项。"
    else:
        overall_status = "ready"
        tone = "success"
        title = "部署环境已就绪"
        description = "关键检查都已通过，可以回首页继续使用。"
        next_action = "可以返回首页继续监听和签到。"

    return {
        "overall_status": overall_status,
        "tone": tone,
        "title": title,
        "description": description,
        "next_action": next_action,
        "counts": counts,
        "collector_method": runtime_state["summary"].get("collector_method"),
    }


def build_health_report(
    runtime_state: Optional[Dict[str, Any]] = None,
    listener_state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    runtime_state = runtime_state or build_runtime_state()
    listener_state = listener_state or inspect_capture_proxy_listener_state()
    template_snapshot = build_template_snapshot()
    checks = [
        build_runtime_health_check(runtime_state),
        build_python_environment_health_check(),
        build_wechat_health_check(runtime_state),
        build_system_proxy_health_check(runtime_state, listener_state),
        build_mitm_listener_health_check(runtime_state, listener_state),
        build_mitm_certificate_health_check(runtime_state),
        build_cv_template_health_check(runtime_state, template_snapshot),
        build_openid_cache_health_check(runtime_state),
    ]
    checks.sort(key=lambda item: (HEALTH_STATUS_ORDER.get(item.get("status"), 99), item.get("title", "")))
    return {
        "generated_at": datetime.now().astimezone().isoformat(),
        "runtime_state": runtime_state,
        "template_status": template_snapshot,
        "summary": summarize_health_checks(checks, runtime_state),
        "checks": checks,
    }


def json_dumps_pretty(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def append_bundle_json(
    archive: zipfile.ZipFile,
    manifest_entries: List[Dict[str, Any]],
    bundle_path: str,
    payload: Any,
    *,
    source_path: Optional[Path] = None,
) -> None:
    data = json_dumps_pretty(payload).encode("utf-8")
    archive.writestr(bundle_path, data)
    manifest_entries.append(
        {
            "bundle_path": bundle_path,
            "kind": "json",
            "size_bytes": len(data),
            "source_path": str(source_path) if source_path else None,
        }
    )


def append_bundle_text_file(
    archive: zipfile.ZipFile,
    manifest_entries: List[Dict[str, Any]],
    bundle_path: str,
    source_path: Path,
) -> None:
    entry = {
        "bundle_path": bundle_path,
        "kind": "text",
        "source_path": str(source_path),
        "exists": source_path.exists(),
    }
    if not source_path.exists():
        manifest_entries.append(entry)
        return

    try:
        original_size = source_path.stat().st_size
        content = redact_sensitive_text(read_text_tail(source_path))
        data = content.encode("utf-8")
        archive.writestr(bundle_path, data)
        entry["size_bytes"] = len(data)
        entry["truncated"] = original_size > SUPPORT_BUNDLE_MAX_TEXT_BYTES
    except Exception as exc:
        entry["error"] = str(exc)
    manifest_entries.append(entry)


def append_bundle_json_file(
    archive: zipfile.ZipFile,
    manifest_entries: List[Dict[str, Any]],
    bundle_path: str,
    source_path: Path,
) -> None:
    entry = {
        "bundle_path": bundle_path,
        "kind": "json",
        "source_path": str(source_path),
        "exists": source_path.exists(),
    }
    if not source_path.exists():
        manifest_entries.append(entry)
        return

    try:
        payload = json.loads(source_path.read_text(encoding="utf-8"))
        data = json_dumps_pretty(redact_sensitive_value(payload)).encode("utf-8")
        archive.writestr(bundle_path, data)
        entry["size_bytes"] = len(data)
    except Exception as exc:
        entry["error"] = str(exc)
    manifest_entries.append(entry)


def build_support_bundle_archive() -> Tuple[io.BytesIO, str]:
    runtime_state = build_runtime_state()
    listener_state = inspect_capture_proxy_listener_state()
    system_proxy_state = read_system_proxy_settings()
    mitm_certificate_state = inspect_mitm_certificate_state()
    support_runtime_state = build_support_runtime_snapshot(runtime_state)
    support_health_report = build_support_bundle_health_report(runtime_state, listener_state)
    frontend_status = redact_sensitive_value(build_frontend_status_payload(runtime_state))
    template_snapshot = build_template_snapshot()
    environment_snapshot = build_environment_snapshot()
    local_config_snapshot = build_local_config_snapshot()

    log_entries = [
        ("logs/collector.log", Path(COLLECTOR_LOG_PATH), "text"),
        ("logs/faye_history.log", Path(FAYE_LOG_PATH), "text"),
        ("logs/latest_openid.json", OPENID_CACHE_PATH, "json"),
        ("logs/collector_output.json", Path(COLLECTOR_OUTPUT_PATH), "json"),
        ("logs/mitm_openid_result.txt", Path(listener_state["result_path"]), "text"),
    ]

    buffer = io.BytesIO()
    manifest_entries: List[Dict[str, Any]] = []
    generated_at = datetime.now().astimezone()

    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        append_bundle_json(archive, manifest_entries, "runtime/runtime_state.json", support_runtime_state)
        append_bundle_json(archive, manifest_entries, "runtime/health_report.json", support_health_report)
        append_bundle_json(archive, manifest_entries, "runtime/frontend_status.json", frontend_status)
        append_bundle_json(
            archive,
            manifest_entries,
            "diagnostics/proxy_listener_state.json",
            redact_sensitive_value(listener_state),
        )
        append_bundle_json(
            archive,
            manifest_entries,
            "diagnostics/system_proxy_state.json",
            redact_sensitive_value(system_proxy_state),
        )
        append_bundle_json(
            archive,
            manifest_entries,
            "diagnostics/mitm_certificate_state.json",
            redact_sensitive_value(mitm_certificate_state),
        )
        append_bundle_json(archive, manifest_entries, "collector/template_snapshot.json", template_snapshot)
        append_bundle_json(archive, manifest_entries, "config/environment_snapshot.json", environment_snapshot)
        append_bundle_json(
            archive,
            manifest_entries,
            "config/local_config.json",
            local_config_snapshot,
            source_path=LOCAL_CONFIG_PATH,
        )

        for bundle_path, source_path, file_kind in log_entries:
            if file_kind == "json":
                append_bundle_json_file(archive, manifest_entries, bundle_path, source_path)
            else:
                append_bundle_text_file(archive, manifest_entries, bundle_path, source_path)

        append_bundle_json(
            archive,
            manifest_entries,
            "manifest.json",
            {
                "schema_version": SUPPORT_BUNDLE_SCHEMA_VERSION,
                "generated_at": generated_at.isoformat(),
                "bundle_name": f"wei-class-support-bundle-{generated_at.strftime('%Y%m%d-%H%M%S')}.zip",
                "collector_method": runtime_state.get("summary", {}).get("collector_method"),
                "current_source": runtime_state.get("openid_status", {}).get("current_source"),
                "notes": [
                    "日志文件默认只保留末尾 128 KiB，并已对 OpenID 等敏感字段做脱敏。",
                    "诊断包不包含本机模板图片，只包含模板状态与路径快照。",
                ],
                "files": manifest_entries,
            },
        )

    buffer.seek(0)
    filename = f"wei-class-support-bundle-{generated_at.strftime('%Y%m%d-%H%M%S')}.zip"
    return buffer, filename


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/health")
def health():
    return render_template("health.html")


@app.route("/api/login", methods=["POST"])
def login():
    ensure_runtime_initialized()
    data = request.json or {}
    openid = str(data.get("openid") or "").strip()

    if not openid:
        return jsonify({"success": False, "message": "请输入 OpenID"})

    try:
        pipeline = openid_refresh_manager.use_manual_openid(openid)
        runtime_state = build_runtime_state()
        resp = make_response(
            jsonify(
                {
                    "success": True,
                    "message": "登录成功",
                    "profile": pipeline.profile,
                    "current_sign": get_runtime_current_sign(runtime_state["pipeline_status"]),
                    "openid_status": runtime_state["openid_status"],
                    "runtime_summary": runtime_state["summary"],
                    "generated_at": runtime_state["generated_at"],
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
    return jsonify(build_session_payload(build_runtime_state()))


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
    return jsonify(build_openid_status_payload(build_runtime_state()))


@app.route("/api/health")
def health_status():
    ensure_runtime_configured()
    return jsonify(build_health_report())


@app.route("/api/template_status")
def template_status():
    ensure_runtime_configured()
    return jsonify(build_template_snapshot())


@app.route("/api/template_capture", methods=["POST"])
def template_capture():
    ensure_runtime_configured()

    if getattr(collector, "method_name", None) != "cv" or not hasattr(collector, "capture_local_templates"):
        return jsonify({"success": False, "message": "当前运行方式不是 cv，不能自动采集本机模板。"}), 400

    if openid_refresh_manager is not None and getattr(openid_refresh_manager, "is_refreshing", False):
        return jsonify({"success": False, "message": "当前正在自动刷新 OpenID，请等本轮结束后再采集模板。"}), 409

    if not template_capture_lock.acquire(blocking=False):
        return jsonify({"success": False, "message": "已有模板采集任务正在运行，请稍候刷新页面。"}), 409

    refresh_lock = getattr(openid_refresh_manager, "_refresh_lock", None)
    refresh_lock_acquired = False
    if refresh_lock is not None:
        refresh_lock_acquired = refresh_lock.acquire(blocking=False)
        if not refresh_lock_acquired:
            template_capture_lock.release()
            return jsonify({"success": False, "message": "当前有 OpenID 刷新任务正在准备执行，请稍候再试。"}), 409

    payload = request.get_json(silent=True) or {}
    chat_state = str(payload.get("chat_state") or "open").strip().lower()
    overwrite = bool(payload.get("overwrite", True))

    try:
        capture_result = collector.capture_local_templates(chat_state=chat_state, overwrite=overwrite)
        template_snapshot = build_template_snapshot()
        return jsonify(
            {
                "success": True,
                "message": f"已完成 {capture_result.get('saved_count', 0)} 张本机模板采集。",
                "capture": capture_result,
                "template_status": template_snapshot,
            }
        )
    except OpenIdCollectorError as exc:
        return jsonify({"success": False, "message": str(exc)}), 400
    except Exception as exc:
        logger.exception("自动采集本机模板失败: %s", exc)
        return jsonify({"success": False, "message": f"自动采集本机模板失败: {exc}"}), 500
    finally:
        if refresh_lock_acquired:
            refresh_lock.release()
        template_capture_lock.release()


@app.route("/api/support_bundle")
def support_bundle():
    ensure_runtime_configured()
    try:
        archive, filename = build_support_bundle_archive()
        return send_file(
            archive,
            mimetype="application/zip",
            as_attachment=True,
            download_name=filename,
        )
    except Exception as exc:
        logger.exception("导出诊断包失败: %s", exc)
        return jsonify({"success": False, "message": f"导出诊断包失败: {exc}"}), 500


@app.route("/api/runtime_state")
def runtime_state():
    ensure_runtime_initialized()
    return jsonify(build_runtime_state())


@app.route("/qr_code")
def qr_code_status():
    ensure_runtime_initialized()
    return jsonify(build_frontend_status_payload(build_runtime_state()))


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
