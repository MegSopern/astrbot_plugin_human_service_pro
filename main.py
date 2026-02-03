import re
import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

from astrbot import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import Reply
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)


@dataclass
class Session:
    """会话数据模型：记录用户与客服的会话状态"""

    user_id: str
    servicer_id: str
    status: str
    group_id: str
    start_time: float
    user_umo: str


class SessionManager:
    """会话管理器：集中处理会话增删查改与排队/超时逻辑"""

    def __init__(self, waiting_timeout: int, conversation_timeout: int):
        self.waiting_timeout = waiting_timeout
        self.conversation_timeout = conversation_timeout
        self._sessions: Dict[str, Session] = {}

    def has_session(self, user_id: str) -> bool:
        """判断用户是否已存在会话"""
        return user_id in self._sessions

    def is_empty(self) -> bool:
        """
        是否无任何会话\n
        :return: True表示无会话，False表示有会话
        """
        return not self._sessions

    def get(self, user_id: str) -> Optional[Session]:
        """
        获取指定用户会话\n
        :param user_id: 用户ID
        :return: 会话对象或None
        """
        return self._sessions.get(user_id)

    def add_waiting(self, user_id: str, group_id: str, user_umo: str) -> Session:
        """
        新增排队会话\n
        :param user_id: 用户ID
        :param group_id: 群组ID
        :param user_umo: 用户UMO
        :return: 新增的会话对象
        """
        session = Session(
            user_id=user_id,
            servicer_id="",
            status="waiting",
            group_id=group_id,
            start_time=time.time(),
            user_umo=user_umo,
        )
        self._sessions[user_id] = session
        return session

    def remove(self, user_id: str) -> None:
        """
        删除指定用户会话\n
        :param user_id: 用户ID
        :return: None
        """
        if user_id in self._sessions:
            del self._sessions[user_id]

    def list_waiting(self) -> List[Session]:
        """
        获取当前排队会话列表\n
        :param user_id: 用户ID
        :return: 排队会话列表
        """
        return [s for s in self._sessions.values() if s.status == "waiting"]

    def list_connected(self) -> List[Session]:
        """
        获取当前已接入对话的会话列表\n
        :return: 已接入对话会话列表
        """
        return [s for s in self._sessions.values() if s.status == "connected"]

    def waiting_count(self) -> int:
        """
        当前排队人数\n
        :return: 排队人数
        """
        return len(self.list_waiting())

    def waiting_position(self, user_id: str) -> Optional[int]:
        """
        获取用户在排队中的位置（从1开始）\n
        :param user_id: 用户ID
        :return: 排队位置（从1开始），如果不在排队中返回None
        """
        waiting_users = [s.user_id for s in self.list_waiting()]
        if user_id in waiting_users:
            return waiting_users.index(user_id) + 1
        return None

    def connect(self, user_id: str, servicer_id: str) -> Optional[Session]:
        """
        将排队会话标记为已接入并重置开始时间\n
        :param user_id: 用户ID
        :param servicer_id: 客服ID
        :return: 更新后的会话对象或None
        """
        session = self.get(user_id)
        if not session:
            return None
        session.status = "connected"
        session.servicer_id = servicer_id
        session.start_time = time.time()
        return session

    def iter_timeout_sessions(self) -> Iterable[Session]:
        """
        遍历超时会话（等待或对话超时）\n
        :return: 超时会话生成器
        """
        current_time = time.time()
        for session in list(self._sessions.values()):
            duration = current_time - session.start_time
            if (session.status == "waiting" and duration >= self.waiting_timeout) or (
                session.status == "connected" and duration >= self.conversation_timeout
            ):
                yield session


class HumanServicePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)

        # 从配置中读取参数
        self.servicers_id: list[str] = config.get("servicers_id", [])
        # 用户等待人工接入的超时时间(秒)
        self.waiting_timeout = config.get("waiting_timeout", 300)
        # 人工对话最大持续时间(秒)
        self.conversation_timeout = config.get("conversation_timeout", 300)

        # 初始化会话管理器
        self.sessions = SessionManager(
            waiting_timeout=self.waiting_timeout,
            conversation_timeout=self.conversation_timeout,
        )
        # 如果未配置客服，使用管理员作为默认客服
        if not self.servicers_id:
            for admin_id in context.get_config()["admins_id"]:
                if admin_id.isdigit():
                    self.servicers_id.append(admin_id)

    async def _check_session_timeout(self) -> None:
        """检查并清理超时会话"""
        # 处理超时会话
        for session in self.sessions.iter_timeout_sessions():
            user_id = session.user_id

            # 通知双方会话超时
            if session.status == "connected":
                await self._send_timeout_notification(session)
            elif session.status == "waiting":
                try:
                    message_chain = MessageChain().message(
                        f"【{user_id}】用户，很抱歉：\n您转人工排队超时，请重新请求"
                    )
                    await self.context.send_message(session.user_umo, message_chain)
                except Exception as e:
                    logger.error(f"通知用户 {user_id} 排队超时的消息发送失败: {str(e)}")

            self.sessions.remove(user_id)

    async def _send_timeout_notification(self, session: Session) -> None:
        """
        发送会话超时通知\n
        :param session: 会话对象
        :return: None
        """
        try:
            # 通知用户
            user_chain = MessageChain().message("会话已超时结束")
            await self.context.send_message(session.user_umo, user_chain)
            # 通知客服
            servicer_chain = MessageChain().message(
                f"您与用户 {session.user_id} 的会话已超时结束"
            )
            await self.context.send_message(
                f"private:{session.servicer_id}", servicer_chain
            )
        except Exception as e:
            logger.error(f"发送超时通知失败: {str(e)}")

    @filter.command("转人工", alias={"请求人工服务", "转客服"}, priority=1)
    async def transfer_to_human(self, event: AiocqhttpMessageEvent):
        """请求接入人工服务，进入排队队列等待"""
        sender_id = event.get_sender_id()
        send_name = event.get_sender_name()
        group_id = event.get_group_id() or "0"

        # 存储用户的unified_msg_origin
        user_umo = event.unified_msg_origin

        if self.sessions.has_session(sender_id):
            status = self.sessions.get(sender_id).status
            if status == "waiting":
                position = self.sessions.waiting_position(sender_id)
                yield event.plain_result(f"⚠ 您已在排队中，当前排名: {position}")
            else:
                yield event.plain_result("⚠ 您已在对话中")
            return

        # 无论是否有客服，都加入等待队列，同时存储umo
        self.sessions.add_waiting(sender_id, group_id, user_umo)
        # 获取当前排队位置
        position = self.sessions.waiting_position(sender_id)
        waiting_count = self.sessions.waiting_count()
        waiting_timeout = round(self.waiting_timeout / 60, 2)
        yield event.plain_result(
            f"已加入人工服务排队队列👥\n当前排队人数: {waiting_count} 人\n您的排名: {position}\n请耐心等待超级管理员接入，超时{waiting_timeout}分钟未接入将自动取消请求\n(注意：恶意转人工将会被拉黑)"
        )
        for servicer_id in self.servicers_id:
            try:
                await self.send(
                    event,
                    message=f"{send_name}【{sender_id}】\n请求转人工服务\n当前等待队列长度: {waiting_count}",
                    user_id=servicer_id,
                )
            except Exception as e:
                logger.error(f"通知客服 {servicer_id} 新排队用户失败: {str(e)}")

    @filter.command("转人机", alias={"取消人工服务", "取消转人工"}, priority=1)
    async def transfer_to_bot(self, event: AiocqhttpMessageEvent):
        """用户取消人工服务，退出排队或结束对话"""
        sender_id = event.get_sender_id()
        sender_name = event.get_sender_name()
        session = self.sessions.get(sender_id)

        if not session:
            yield event.plain_result("您当前没有正在进行的人工会话或排队")
            return

        # 通知客服
        if session.status == "connected" and session.servicer_id:
            try:
                await self.send(
                    event,
                    message=f"{sender_name} 已主动结束人工对话",
                    user_id=session.servicer_id,
                )
            except Exception as e:
                logger.error(f"通知客服会话取消失败: {str(e)}")

        # 从队列中移除
        self.sessions.remove(sender_id)

        # 通知其他排队用户位置变化
        await self._notify_position_change()
        if session.status == "waiting":
            yield event.plain_result("已取消人工服务排队请求")
        else:
            yield event.plain_result("好的，已结束人工对话，我现在是bot啦！")

    async def _notify_position_change(self) -> None:
        """通知排队用户位置变化"""
        waiting_sessions = self.sessions.list_waiting()
        for idx, session in enumerate(waiting_sessions):
            new_position = idx + 1
            try:
                message_chain = MessageChain().message(
                    f"排队位置更新: 您当前排名 {new_position}\n(前方还有 {new_position - 1} 人)"
                )
                await self.context.send_message(session.user_umo, message_chain)
            except Exception as e:
                logger.error(f"通知用户 {session.user_id} 位置变化失败: {str(e)}")

    @filter.command("接入对话", priority=1)
    async def accept_conversation(
        self, event: AiocqhttpMessageEvent, target_id: str | int | None = None
    ):
        """客服接入指定用户的对话，支持从会话列表选择"""
        sender_id = event.get_sender_id()
        sender_name = event.get_sender_name()

        # 验证客服权限
        if sender_id not in self.servicers_id:
            yield event.plain_result("❌ 您没有权限接入对话")
            return

        # 从回复消息中提取目标用户ID
        if reply_seg := next(
            (seg for seg in event.get_messages() if isinstance(seg, Reply)), None
        ):
            if text := reply_seg.message_str:
                if match := re.search(r"[(\[【](\d+)[)\]】]", text):
                    target_id = match.group(1)

        if target_id is None:
            yield event.plain_result("请指定要接入的用户ID或回复包含用户ID的消息")
            return

        target_id = str(target_id)
        session = self.sessions.get(target_id)

        # 验证会话状态
        if not session:
            yield event.plain_result(f"用户({target_id})未在排队或对话中")
            return

        if session.status == "connected":
            if session.servicer_id == sender_id:
                yield event.plain_result("您正在与该用户对话")
            else:
                yield event.plain_result(f"用户({target_id})已被其他客服接入")
            return

        if session.status != "waiting":
            yield event.plain_result(f"用户({target_id})未在排队中")
            return

        # 更新会话状态并重置计时
        self.sessions.connect(target_id, sender_id)

        # 通知用户
        try:
            conversation_timeout = round(self.conversation_timeout / 60, 2)
            await self.send(
                event,
                message=(
                    f"超级管理员👤:{sender_name}\n已接入对话⚠️⚠️⚠️\n您最多有{conversation_timeout}分钟的时间进行对话\n(请用简洁的话描述所遇到的问题)"
                ),
                group_id=session.group_id,
                user_id=target_id,
            )
        except Exception as e:
            logger.error(f"通知用户 {target_id} 客服接入失败: {str(e)}")
            session.status = "waiting"  # 恢复状态
            yield event.plain_result("接入失败，请重试")
            return

        # 通知其他排队用户位置变化
        await self._notify_position_change()

        yield event.plain_result(
            f"好的，您现在已成功接入\n与用户 {target_id} 的对话\n请开始对话："
        )
        event.stop_event()

    @filter.command("结束对话", priority=1)
    async def end_conversation(self, event: AiocqhttpMessageEvent):
        """客服结束当前人工对话"""
        sender_id = event.get_sender_id()
        sender_name = event.get_sender_name()
        if sender_id not in self.servicers_id:
            return

        for session in self.sessions.list_connected():
            if session.servicer_id == sender_id:
                await self.send(
                    event,
                    message=(f"超级管理员👤：{sender_name}\n❗已结束与你的对话❗"),
                    group_id=session.group_id,
                    user_id=session.user_id,
                )
                self.sessions.remove(session.user_id)
                yield event.plain_result(f"已结束与用户({session.user_id})的对话")
                return

        yield event.plain_result("当前无对话需要结束")
        return

    # 管理员指令：查看当前所有对话
    @filter.command("查看对话", alias={"查看会话", "查看排队"})
    async def list_active_sessions(self, event: AiocqhttpMessageEvent):
        """查看当前所有活跃的客服对话和排队队列"""
        # 验证客服权限
        sender_id = event.get_sender_id()
        if sender_id not in self.servicers_id:
            yield event.plain_result("❌ 您没有权限查看对话")
            return
        # 先清理超时会话
        await self._check_session_timeout()
        if self.sessions.is_empty():
            yield event.plain_result("当前没有活跃会话和排队请求")
            return

        # 分离等待队列和活跃对话
        waiting_sessions = self.sessions.list_waiting()
        active_sessions = self.sessions.list_connected()

        msg_lines = []
        if waiting_sessions:
            msg_lines.append("📋 排队队列：")
            for idx, session in enumerate(waiting_sessions):
                duration = int(time.time() - session.start_time) // 60
                msg_lines.append(
                    f"{idx + 1}. 用户 {session.user_id}\n（等待时间：{duration}分钟）"
                )

        if active_sessions:
            msg_lines.append("\n🔗 活跃对话：")
            for session in active_sessions:
                duration = int(time.time() - session.start_time) // 60
                msg_lines.append(
                    f"- 用户 {session.user_id}\n（客服：{session.servicer_id}，时长：{duration}分钟）"
                )
        yield event.plain_result("\n".join(msg_lines))

    async def send(
        self,
        event: AiocqhttpMessageEvent,
        message,
        group_id: int | str | None = None,
        user_id: int | str | None = None,
    ) -> None:
        """
        向用户发消息，兼容群聊或私聊\n
        :param event: 事件对象
        :param message: 消息内容
        :param group_id: 目标群组ID
        :param user_id: 目标用户ID
        :return: None
        """
        if group_id and str(group_id) != "0":
            await event.bot.send_group_msg(group_id=int(group_id), message=message)
        elif user_id:
            await event.bot.send_private_msg(user_id=int(user_id), message=message)

    async def send_ob(
        self,
        event: AiocqhttpMessageEvent,
        group_id: int | str | None = None,
        user_id: int | str | None = None,
    ) -> None:
        """
        向用户发onebot格式的消息，兼容群聊或私聊\n
        :param event: 事件对象
        :param group_id: 目标群组ID
        :param user_id: 目标用户ID
        :return: None
        """
        ob_message = await event._parse_onebot_json(
            MessageChain(chain=event.message_obj.message)
        )
        if group_id and str(group_id) != "0":
            await event.bot.send_group_msg(group_id=int(group_id), message=ob_message)
        elif user_id:
            await event.bot.send_private_msg(user_id=int(user_id), message=ob_message)

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def handle_match(self, event: AiocqhttpMessageEvent):
        """监听对话消息转发"""
        await self._check_session_timeout()
        chain = event.get_messages()
        sender_id: str = event.get_sender_id()

        # 忽略空消息和包含回复的消息（避免循环转发）
        if not chain or any(isinstance(seg, (Reply)) for seg in chain):
            return

        # 管理员 → 用户 (仅私聊生效)
        if (
            sender_id in self.servicers_id
            and event.is_private_chat()
            and event.message_str
            not in ("接入对话", "结束对话", "查看对话", "查看会话", "查看排队")
        ):
            # 仅转发当前客服已接入的会话
            for session in self.sessions.list_connected():
                if session.servicer_id == sender_id:
                    await self.send_ob(
                        event,
                        group_id=session.group_id,
                        user_id=session.user_id,
                    )
                    event.stop_event()
                    break

        # 用户 → 管理员
        elif session := self.sessions.get(sender_id):
            if session.status == "connected" and session.servicer_id:
                await self.send_ob(
                    event,
                    user_id=session.servicer_id,
                )
                event.stop_event()

    async def terminate(self):
        """插件卸载时调用，清理会话"""
        logger.info("人工客服插件正在卸载，清理会话中...")
        self.sessions = SessionManager(
            waiting_timeout=self.waiting_timeout,
            conversation_timeout=self.conversation_timeout,
        )
        logger.info("人工客服插件卸载完成")
