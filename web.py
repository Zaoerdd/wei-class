from flask import Flask, render_template, jsonify, request, make_response,send_from_directory
import ad
from getdata import getData, get_student_profile, submit_sign
import asyncio
from getSocket import TeacherMateWebSocketClient
import threading
import os
import logging
from typing import Optional, List, Dict, Any, Set, Tuple
import queue

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(threadName)s] - %(message)s'
)
logger = logging.getLogger(__name__)


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

    # 保留少量常见补充字段，便于前端直接展示。
    for key in ("courseName", "teacherName", "startTime", "endTime", "lat", "lon", "latitude", "longitude"):
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


def build_sign_result_text(result: Dict[str, Any]) -> str:
    msg_client = str(result.get("msgClient") or "").strip()
    if msg_client:
        return msg_client

    sign_rank = safe_int(result.get("signRank"))
    if sign_rank and sign_rank > 0:
        return f"签到成功，你是第{sign_rank}个签到！"

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

    if "失败" in frontend_message or "错误" in frontend_message or "关闭" in frontend_message:
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
    return cloned


def load_profile(openid: str) -> Optional[Dict[str, Any]]:
    try:
        profile = get_student_profile(openid)
        if isinstance(profile, dict) and "message" not in profile:
            return profile
    except Exception as e:
        logger.warning(f"获取用户资料失败 {openid[-4:]}: {e}")
    return None


def get_or_create_pipeline(openid: str, profile: Optional[Dict[str, Any]] = None) -> "Pipeline":
    pipeline = active_pipelines.get(openid)
    if pipeline and pipeline.is_running:
        if profile:
            pipeline.profile = profile
        return pipeline

    pipeline = Pipeline(openid, profile=profile)
    active_pipelines[openid] = pipeline
    pipeline.start()
    return pipeline


class Pipeline:
    """负责特定 OpenID 的处理管道"""

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

    def start(self):
        """在单独的线程中启动管道"""
        if self.is_running:
            return
        self.is_running = True
        thread = threading.Thread(target=self._run_async, daemon=True, name=f"Pipeline-{self.openid[-4:]}")
        thread.start()
        logger.info(f"用户 {self.openid[-4:]} 的管道已启动")

    def stop(self):
        """外部调用的停止方法"""
        self.shutdown_event.set()
        self.is_running = False

    def _run_async(self):
        try:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(self._main_async())
        except Exception as e:
            logger.error(f"异步运行失败 {self.openid}: {e}")
        finally:
            self.is_running = False
            if self.loop and not self.loop.is_closed():
                self.loop.close()

    async def _main_async(self):
        try:
            while not self.shutdown_event.is_set():
                data = await self.wait_data()
                if self.shutdown_event.is_set(): break

                if isinstance(data, list) and data:
                    self.message = "检测到签到任务，正在处理..."
                    await self.process_signatures(data)
                else:
                    self.message = data if isinstance(data, str) else "暂无活跃签到"

                await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"主循环失败: {e}")
            self.message = f"错误: {str(e)}"
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
                return "当前无正在进行的签到"
            elif isinstance(data, dict) and "message" in data:
                self.active_signs = []
                self.pending_sign_keys.clear()
                return data["message"]
            self.active_signs = []
            self.pending_sign_keys.clear()
            return "暂无签到"
        except Exception as e:
            logger.error(f"获取数据失败: {e}")
            self.active_signs = []
            self.pending_sign_keys.clear()
            return "网络请求失败"

    async def process_signatures(self, data: List[Dict[str, Any]]):
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
            self.message = f"检测到{item.get('signType') or '普通签到'}，正在自动签到..."
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
                logger.error(f"后台任务执行失败: {exc}")

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

        return sign_key, build_result_meta(
            item,
            status_message or self.message or "暂无状态",
        )

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

    async def run_websocket_client(self, item: Dict[str, Any]):
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
        except Exception as e:
            logger.error(f"WS客户端错误: {e}")

    async def run_common_sign(self, item: Dict[str, Any]):
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
        except Exception as e:
            logger.error(f"自动签到失败 {item.get('signId')}: {e}")
            self.message = f"自动签到失败: {e}"
        finally:
            self.pending_sign_keys.discard(sign_key)

    def handle_ws_event(self, item: Dict[str, Any], event: Dict[str, Any]):
        if self.loop and not self.loop.is_closed():
            asyncio.run_coroutine_threadsafe(self._process_ws_event(item, event), self.loop)

    async def _process_ws_event(self, item: Dict[str, Any], event: Dict[str, Any]):
        sign_key, result_meta = self._ensure_result_meta(
            item,
            event.get("summary") or self.message or "暂无状态",
        )
        self._append_faye_event(result_meta, event)
        if not result_meta.get("result_ready"):
            result_meta["status_message"] = event.get("summary") or result_meta.get("status_message")

        if event.get("channel"):
            result_meta["task_name"] = result_meta.get("task_name") or item.get("name")

        self._store_result_meta(sign_key, result_meta)

    def callback(self, item: Dict[str, Any], url: str):
        self.result_queue.put(url)
        if self.loop and not self.loop.is_closed():
            asyncio.run_coroutine_threadsafe(self._process_callback_result(item, url), self.loop)

    async def _process_callback_result(self, item: Dict[str, Any], url: str):
        try:
            # WZJ-Sign 直接将 teachermate 返回的原始 qrUrl 编码成二维码。
            # 这里不能再跟随浏览器跳转，否则桌面环境会落到
            # “请用微信、企业微信、或易班客户端扫码”的提示页。
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
        except Exception as e:
            self.message = f"解析二维码失败: {e}"

    def _get_result_meta(self, current_sign: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if current_sign:
            result_meta = self.sign_results.get(get_sign_key(current_sign))
            if result_meta:
                return clone_result_meta(result_meta)
            return build_result_meta(current_sign, self.message or "暂无状态")

        if self.latest_result_meta:
            return clone_result_meta(self.latest_result_meta)

        return build_result_meta(None, self.message or "暂无状态")

    async def shutdown(self):
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
        current_sign = None
        for item in self.active_signs:
            result_meta = self.sign_results.get(get_sign_key(item))
            if result_meta and result_meta.get("result_ready"):
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


# --- 全局状态 ---
app = Flask(__name__)
active_pipelines: Dict[str, Pipeline] = {}


# --- 路由 ---

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    openid = data.get('openid')

    if not openid:
        return jsonify({"success": False, "message": "请输入OpenID"})

    try:
        api_response = getData(openid)
        if isinstance(api_response, dict) and "message" in api_response:
            return jsonify({"success": False, "message": f"无效的OpenID: {api_response['message']}"})

        if isinstance(api_response, list):
            profile = load_profile(openid)
            pipeline = get_or_create_pipeline(openid, profile)

            resp = make_response(jsonify({
                "success": True,
                "message": "登录成功",
                "profile": pipeline.profile,
                "current_sign": pipeline.active_signs[0] if pipeline.active_signs else None,
            }))
            # 设置 Cookie，有效期2小时
            resp.set_cookie('user_openid', openid, max_age= 7200)
            return resp

        return jsonify({"success": False, "message": "未知响应格式"})

    except Exception as e:
        logger.error(f"登录验证错误: {e}")
        return jsonify({"success": False, "message": "验证服务器连接失败"})


@app.route('/api/check_session')
def check_session():
    """每次进入主页时调用，强制检查 OpenID 有效性"""
    openid = request.cookies.get('user_openid')
    if not openid:
        return jsonify({"valid": False, "message": "未登录"})

    try:
        # 1. 强制从源头验证 (getData)
        res = getData(openid)

        # 2. 如果返回是 Dict 且包含 message，说明 Token 过期或无效
        if isinstance(res, dict) and "message" in res:
            logger.warning(f"检测到 OpenID 过期或无效: {openid}")

            # 停止并清理该用户的管道
            if openid in active_pipelines:
                active_pipelines[openid].stop()
                del active_pipelines[openid]

            # 清除 Cookie 并返回无效状态
            resp = make_response(jsonify({"valid": False, "message": res['message']}))
            resp.delete_cookie('user_openid')
            return resp

        # 3. 如果是 List (哪怕是空List)，说明有效
        if isinstance(res, list):
            # 确保管道正在运行
            profile = None
            if openid in active_pipelines and active_pipelines[openid].profile:
                profile = active_pipelines[openid].profile
            else:
                profile = load_profile(openid)

            pipeline = get_or_create_pipeline(openid, profile)

            return jsonify({
                "valid": True,
                "openid": openid,
                "profile": pipeline.profile,
                "current_sign": pipeline.active_signs[0] if pipeline.active_signs else None,
            })

        return jsonify({"valid": False, "message": "数据格式异常"})

    except Exception as e:
        logger.error(f"Session check error: {e}")
        # 如果仅仅是网络错误，暂时不登出，但也提示验证失败
        return jsonify({"valid": False, "message": "连接验证服务器失败"})


@app.route('/api/logout', methods=['POST'])
def logout():
    openid = request.cookies.get('user_openid')
    if openid and openid in active_pipelines:
        active_pipelines[openid].stop()
        del active_pipelines[openid]

    resp = make_response(jsonify({"success": True}))
    resp.delete_cookie('user_openid')
    return resp


@app.route('/qr_code')
def qr_code_status():
    openid = request.cookies.get('user_openid')
    if not openid or openid not in active_pipelines:
        return jsonify({"success": 0, "message": "未登录或服务未启动"})
    return jsonify(active_pipelines[openid].get_status())


@app.after_request
def add_header(response):
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return response

@app.route('/usage')
def usage():
    """使用说明页面"""
    return render_template('usage.html')
if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, debug=False, threaded=True)
