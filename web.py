from flask import Flask, render_template, jsonify, request, make_response,send_from_directory
import ad
from getdata import getData
import asyncio
from getSocket import TeacherMateWebSocketClient
import threading
import os
import logging
import requests
from typing import Optional, List, Dict, Any
import queue

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(threadName)s] - %(message)s'
)
logger = logging.getLogger(__name__)


class Pipeline:
    """负责特定 OpenID 的处理管道"""

    def __init__(self, openid: str):
        self.openid = openid
        self.success = 0
        self.result: Optional[str] = None
        self.message: Optional[str] = "初始化中..."
        self.is_running = False
        self.websocket_clients: List[TeacherMateWebSocketClient] = []
        self.asyncio_tasks: List[asyncio.Task] = []
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.shutdown_event = threading.Event()
        self.result_queue = queue.Queue()

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
                    self.message = "检测到签到任务，正在连接..."
                    await self.process_signatures(data)
                else:
                    self.message = data if isinstance(data, str) else "暂无活跃签到"
                    if self.success == 1:
                        self.success = 0
                        self.result = None

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
                        result.append(item)
                if result:
                    return result
                return "当前无正在进行的签到"
            elif isinstance(data, dict) and "message" in data:
                return data["message"]
            return "暂无签到"
        except Exception as e:
            logger.error(f"获取数据失败: {e}")
            return "网络请求失败"

    async def process_signatures(self, data: List[Dict[str, Any]]):
        tasks = []
        for item in data:
            if item.get("isQR"):
                if any(ws.sign_id == item["signId"] and not ws.is_shutting_down for ws in self.websocket_clients):
                    continue

                task = asyncio.create_task(self.run_websocket_client(item["signId"], item["courseId"]))
                self.asyncio_tasks.append(task)
                tasks.append(task)

        if not tasks:
            return

        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

    async def run_websocket_client(self, sign_id: int, course_id: int):
        try:
            client_id = ad.creatClientId(sign_id, courseId=course_id)
            client = TeacherMateWebSocketClient(sign_id=sign_id, qr_callback=self.callback)
            client.client_id = client_id
            client.webT = True
            self.websocket_clients.append(client)
            await client.start()
        except Exception as e:
            logger.error(f"WS客户端错误: {e}")

    def callback(self, url: str):
        self.result_queue.put(url)
        if self.loop and not self.loop.is_closed():
            asyncio.run_coroutine_threadsafe(self._process_callback_result(url), self.loop)

    async def _process_callback_result(self, url: str):
        try:
            headers = {"User-Agent": "Mozilla/5.0"}

            def sync_request():
                return requests.get(url, headers=headers, timeout=10).url

            loop = asyncio.get_event_loop()
            final_url = await loop.run_in_executor(None, sync_request)

            self.result = str(final_url)
            self.success = 1
            self.message = "获取成功"
        except Exception as e:
            self.message = f"解析二维码失败: {e}"

    async def shutdown(self):
        self.shutdown_event.set()
        for client in self.websocket_clients:
            if not client.is_shutting_down:
                await client.graceful_shutdown()
        for task in self.asyncio_tasks:
            task.cancel()
        self.websocket_clients.clear()
        self.asyncio_tasks.clear()

    def get_status(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "message": self.message,
            "qr_url": self.result,
            "is_running": self.is_running
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
            if openid not in active_pipelines or not active_pipelines[openid].is_running:
                pipeline = Pipeline(openid)
                active_pipelines[openid] = pipeline
                pipeline.start()

            resp = make_response(jsonify({"success": True, "message": "登录成功"}))
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
            if openid not in active_pipelines or not active_pipelines[openid].is_running:
                pipeline = Pipeline(openid)
                active_pipelines[openid] = pipeline
                pipeline.start()

            return jsonify({"valid": True, "openid": openid})

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