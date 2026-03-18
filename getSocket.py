import asyncio
import json
import logging
from typing import Any, Callable, Dict, List, Optional

import websockets

logger = logging.getLogger(__name__)


class TeacherMateWebSocketClient:
    """改进的 WebSocket 客户端，保留更多 Faye 消息细节。"""

    def __init__(
        self,
        sign_id: int,
        course_id: Optional[int] = None,
        qr_callback: Optional[Callable] = None,
        event_callback: Optional[Callable] = None,
    ):
        self.qr_callback = qr_callback
        self.event_callback = event_callback
        self.sign_id = sign_id
        self.course_id = course_id
        self.last_qr_url: Optional[str] = None
        self.client_id = ""
        self.counter = 3
        self.done = asyncio.Event()
        self.websocket: Optional[websockets.WebSocketClientProtocol] = None
        self.is_shutting_down = False
        self.wait_time = 1
        self.reconnect_delay = 5
        self.max_reconnect_attempts = 3
        self.reconnect_attempts = 0
        self.receive_task: Optional[asyncio.Task] = None

    def _build_subscriptions(self) -> List[str]:
        subscriptions: List[str] = []
        if self.course_id is not None:
            subscriptions.extend(
                [
                    f"/attendance/{self.course_id}/{self.sign_id}/qr",
                    f"/attendance/{self.course_id}/{self.sign_id}",
                    f"/attendance/{self.course_id}",
                ]
            )
        subscriptions.append(f"/sign/{self.sign_id}")

        unique_subscriptions: List[str] = []
        for subscription in subscriptions:
            if subscription not in unique_subscriptions:
                unique_subscriptions.append(subscription)
        return unique_subscriptions

    def _build_event(self, payload: Dict[str, Any], raw_message: str) -> Dict[str, Any]:
        data = payload.get("data")
        if not isinstance(data, dict):
            data = {}

        ext = payload.get("ext")
        if not isinstance(ext, dict):
            ext = {}

        channel = str(payload.get("channel") or "")
        subscription = payload.get("subscription")
        data_type = data.get("type")
        qr_url = data.get("qrUrl")
        raw_students: List[Dict[str, Any]] = []

        for key in ("student", "students"):
            value = data.get(key)
            if isinstance(value, dict):
                raw_students.append(value)
            elif isinstance(value, list):
                raw_students.extend(item for item in value if isinstance(item, dict))

        has_student_update = len(raw_students) > 0

        if channel == "/meta/subscribe":
            event_kind = "meta_subscribe"
            summary = "Faye 订阅已建立" if payload.get("successful") else "Faye 订阅失败"
        elif channel == "/meta/connect":
            event_kind = "meta_connect"
            summary = "Faye 连接保持中"
        elif channel == "/meta/handshake":
            event_kind = "meta_handshake"
            summary = "Faye 握手完成"
        elif qr_url:
            event_kind = "qr_update"
            summary = "获取到二维码 URL"
        elif data_type == 2:
            event_kind = "attendance_closed"
            summary = "签到通道提示已关闭"
        elif data_type == 3 and has_student_update:
            event_kind = "student_signed"
            if len(raw_students) == 1 and raw_students[0].get("name"):
                summary = f"{raw_students[0].get('name')} 已签到"
            else:
                summary = f"已收到 {len(raw_students)} 条签到记录"
        elif data_type == 3:
            event_kind = "attendance_pending"
            summary = "二维码暂未生成"
        elif channel.startswith("/sign/"):
            event_kind = "sign_update"
            summary = "收到 sign 通道更新"
        elif channel.startswith("/attendance/"):
            event_kind = "attendance_update"
            summary = "收到签到通道更新"
        else:
            event_kind = "message"
            summary = "收到 Faye 消息"

        return {
            "event_kind": event_kind,
            "summary": summary,
            "channel": channel or None,
            "subscription": subscription,
            "id": payload.get("id"),
            "client_id": payload.get("clientId"),
            "successful": payload.get("successful"),
            "data_type": data_type,
            "qr_url": qr_url,
            "data": data or None,
            "ext": ext or None,
            "inner_faye_token": ext.get("innerFayeToken"),
            "advice": payload.get("advice"),
            "raw": payload,
            "raw_message": raw_message,
        }

    def _emit_event(self, event: Dict[str, Any]) -> None:
        if not self.event_callback or self.is_shutting_down:
            return

        try:
            self.event_callback(event)
        except Exception as e:
            logger.warning(f"处理 Faye 事件回调失败: {e}")

    async def receive_handler(self) -> None:
        """接收并解析所有 Faye 消息。"""
        try:
            async for message in self.websocket:
                if self.is_shutting_down:
                    break

                msg_str = message.decode("utf-8") if isinstance(message, bytes) else message
                logger.debug(f"收到消息: {msg_str}")

                try:
                    payload = json.loads(msg_str)
                except json.JSONDecodeError as e:
                    logger.error(f"JSON 解析失败: {e}")
                    continue

                message_items = payload if isinstance(payload, list) else [payload]
                for item in message_items:
                    if not isinstance(item, dict):
                        continue

                    event = self._build_event(item, msg_str)
                    self._emit_event(event)
                    await self._handle_event(event)

        except websockets.exceptions.ConnectionClosed:
            if not self.is_shutting_down:
                logger.info("WebSocket 连接已关闭")
            self.done.set()
        except Exception as e:
            if not self.is_shutting_down:
                logger.error(f"接收消息错误: {e}")
            self.done.set()

    async def _handle_event(self, event: Dict[str, Any]) -> None:
        qr_url = event.get("qr_url")
        data_type = event.get("data_type")

        if qr_url:
            logger.info(f"获取到二维码 URL: {str(qr_url)[:50]}...")
            if self.qr_callback and not self.is_shutting_down and qr_url != self.last_qr_url:
                self.last_qr_url = str(qr_url)
                self.qr_callback(str(qr_url))
            return

        if event.get("event_kind") == "student_signed":
            logger.info("收到学生签到更新")
        elif data_type == 3:
            logger.info("二维码暂未生成，等待后续推送")
        elif data_type == 2:
            logger.info("检测到签到关闭消息")
            await self.graceful_shutdown()
        elif event.get("event_kind") == "meta_subscribe":
            logger.info(f"订阅响应: {event.get('channel')} -> {event.get('subscription')}")

    async def start(self) -> None:
        """启动 WebSocket 客户端，支持重连。"""
        while self.reconnect_attempts < self.max_reconnect_attempts and not self.is_shutting_down:
            try:
                await self._connect_and_run()
                break
            except (websockets.exceptions.ConnectionClosed, ConnectionRefusedError, asyncio.TimeoutError) as e:
                self.reconnect_attempts += 1
                if self.reconnect_attempts < self.max_reconnect_attempts and not self.is_shutting_down:
                    logger.warning(f"连接失败，{self.reconnect_delay} 秒后重试... ({e})")
                    await asyncio.sleep(self.reconnect_delay)
                else:
                    logger.error(f"达到最大重连次数，放弃连接: {e}")
            except Exception as e:
                if not self.is_shutting_down:
                    logger.error(f"WebSocket 客户端意外错误: {e}")
                break

    async def _connect_and_run(self) -> None:
        """连接并运行 WebSocket 客户端。"""
        socket_url = "wss://www.teachermate.com.cn/faye"

        try:
            self.websocket = await asyncio.wait_for(
                websockets.connect(socket_url),
                timeout=10.0,
            )
        except asyncio.TimeoutError:
            logger.error("WebSocket 连接超时")
            raise

        logger.info("WebSocket 连接建立成功")
        self.reconnect_attempts = 0
        self.receive_task = asyncio.create_task(self.receive_handler())

        try:
            for subscription in self._build_subscriptions():
                self.counter += 1
                subscribe_msg = json.dumps(
                    [
                        {
                            "channel": "/meta/subscribe",
                            "clientId": self.client_id,
                            "subscription": subscription,
                            "id": str(self.counter),
                        }
                    ]
                )
                await self.websocket.send(subscribe_msg)
                logger.info(f"已订阅通道: {subscription}")

            while not self.done.is_set() and not self.is_shutting_down:
                try:
                    self.counter += 1
                    connect_msg = json.dumps(
                        [
                            {
                                "channel": "/meta/connect",
                                "clientId": self.client_id,
                                "connectionType": "websocket",
                                "id": str(self.counter),
                            }
                        ]
                    )

                    await asyncio.wait_for(self.websocket.send(connect_msg), timeout=5.0)
                    await asyncio.sleep(self.wait_time)

                except asyncio.TimeoutError:
                    if not self.is_shutting_down:
                        logger.warning("发送心跳超时")
                except websockets.exceptions.ConnectionClosed:
                    break
                except Exception as e:
                    if not self.is_shutting_down:
                        logger.error(f"发送消息错误: {e}")
                    break
        finally:
            await self._cleanup_tasks()

    async def _cleanup_tasks(self) -> None:
        """清理任务。"""
        if self.receive_task and not self.receive_task.done():
            self.receive_task.cancel()
            try:
                await self.receive_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                if not self.is_shutting_down:
                    logger.error(f"接收任务清理错误: {e}")

        await self.close_connection()

    async def graceful_shutdown(self) -> None:
        """优雅关闭连接。"""
        if self.is_shutting_down:
            return

        logger.info(f"开始优雅关闭 WebSocket 客户端 (sign_id: {self.sign_id})")
        self.is_shutting_down = True
        self.done.set()
        await self._cleanup_tasks()
        logger.info(f"WebSocket 客户端已关闭 (sign_id: {self.sign_id})")

    async def close_connection(self) -> None:
        """关闭连接。"""
        if self.websocket:
            try:
                await self.websocket.close()
            except Exception as e:
                if not self.is_shutting_down:
                    logger.debug(f"关闭连接时出现预期外错误: {e}")
            finally:
                self.websocket = None
