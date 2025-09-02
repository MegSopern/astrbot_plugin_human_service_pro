import re
import time
from typing import Dict

from astrbot import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, register
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import Reply
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)


@register(
    "astrbot_plugin_human_service_pro",
    "MegSopern",
    "人工客服插件增强版",
    "1.1.0",
    "https://github.com/MegSopern/astrbot_plugin_human_service_pro",
)
class HumanServicePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)

        # 从配置中读取参数
        self.servicers_id: list[str] = config.get("servicers_id", [])
        # 用户等待人工接入的超时时间(秒)
        self.waiting_timeout = config.get("waiting_timeout", 300)
        # 人工对话最大持续时间(秒)
        self.conversation_timeout = config.get("conversation_timeout", 600)

        # 初始化会话管理器
        self.session_map: Dict[str, Dict] = {}
        # {user_id: {servicer_id, status, group_id, start_time, user_umo}}
        # 如果未配置客服，使用管理员作为默认客服
        if not self.servicers_id:
            for admin_id in context.get_config()["admins_id"]:
                if admin_id.isdigit():
                    self.servicers_id.append(admin_id)

    def _get_waiting_count(self) -> int:
        """获取当前等待队列长度"""
        return sum(
            1 for session in self.session_map.values() if session["status"] == "waiting"
        )

    def _get_user_position(self, user_id: str) -> int:
        """获取用户在等待队列中的位置"""
        waiting_users = [
            uid
            for uid, session in self.session_map.items()
            if session["status"] == "waiting"
        ]
        if user_id in waiting_users:
            return waiting_users.index(user_id) + 1  # 排名从1开始
        return

    async def _check_session_timeout(self):
        """检查并清理超时会话"""
        current_time = time.time()
        timeout_sessions = []

        for user_id, session in self.session_map.items():
            # 计算会话持续时间(秒)
            duration = current_time - session["start_time"]
            # 根据状态使用不同的超时阈值
            if (
                session["status"] == "waiting" and duration >= self.waiting_timeout
            ) or (
                session["status"] == "connected"
                and duration >= self.conversation_timeout
            ):
                timeout_sessions.append(user_id)

        # 处理超时会话
        for user_id in timeout_sessions:
            session = self.session_map[user_id]
            logger.info(
                f"会话超时: 用户 {user_id} 与客服 {session['servicer_id'] or '未分配'}"
            )

            # 通知双方会话超时
            if session["status"] == "connected":
                await self._send_timeout_notification(user_id, session)
            elif session["status"] == "waiting":
                try:
                    message_chain = MessageChain().message(
                        f"【{user_id}】用户，很抱歉：\n您转人工排队超时，请重新请求"
                    )
                    await self.context.send_message(session["user_umo"], message_chain)
                except Exception as e:
                    logger.error(f"通知用户 {user_id} 排队超时的消息发送失败: {str(e)}")

            del self.session_map[user_id]

    async def _send_timeout_notification(self, user_id: str, session: Dict):
        """发送会话超时通知"""
        try:
            # 通知用户
            user_chain = MessageChain().message("会话已超时结束")
            await self.context.send_message(session["user_umo"], user_chain)

            # 通知客服
            servicer_chain = MessageChain().message(
                f"您与用户 {user_id} 的会话已超时结束"
            )
            await self.context.send_message(
                f"private:{session['servicer_id']}", servicer_chain
            )
        except Exception as e:
            logger.error(f"发送超时通知失败: {str(e)}")

    @filter.command("转人工", priority=1)
    async def transfer_to_human(self, event: AiocqhttpMessageEvent):
        """请求接入人工服务，进入排队队列等待"""
        sender_id = event.get_sender_id()
        send_name = event.get_sender_name()
        group_id = event.get_group_id() or "0"

        # 存储用户的unified_msg_origin
        user_umo = event.unified_msg_origin

        if sender_id in self.session_map:
            status = self.session_map[sender_id]["status"]
            if status == "waiting":
                position = self._get_user_position(sender_id)
                yield event.plain_result(f"⚠ 您已在排队中，当前排名: {position}")
            else:
                yield event.plain_result("⚠ 您已在对话中")
            return

        # 无论是否有客服，都加入等待队列，同时存储umo
        self.session_map[sender_id] = {
            "servicer_id": "",
            "status": "waiting",
            "group_id": group_id,
            "start_time": time.time(),
            "user_umo": user_umo,  # 新增存储用户的umo
        }
        # 获取当前排队位置
        position = self._get_user_position(sender_id)
        waiting_count = self._get_waiting_count()
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

    @filter.command("转人机", priority=1)
    async def transfer_to_bot(self, event: AiocqhttpMessageEvent):
        """用户取消人工服务，退出排队或结束对话"""
        sender_id = event.get_sender_id()
        sender_name = event.get_sender_name()
        session = self.session_map.get(sender_id)

        if not session:
            yield event.plain_result("您当前没有正在进行的人工会话或排队")
            return

        # 通知客服
        if session["status"] == "connected" and session["servicer_id"]:
            try:
                await self.send(
                    event,
                    message=f"{sender_name} 已主动结束人工对话",
                    user_id=session["servicer_id"],
                )
            except Exception as e:
                logger.error(f"通知客服会话取消失败: {str(e)}")

        # 从队列中移除
        del self.session_map[sender_id]

        # 通知其他排队用户位置变化
        await self._notify_position_change()
        if session["status"] == "waiting":
            yield event.plain_result("已取消人工服务排队请求")
        else:
            yield event.plain_result("好的，已结束人工对话，我现在是bot啦！")

    async def _notify_position_change(self):
        """通知排队用户位置变化"""
        waiting_users = [
            uid
            for uid, session in self.session_map.items()
            if session["status"] == "waiting"
        ]
        for idx, user_id in enumerate(waiting_users):
            new_position = idx + 1
            user_session = self.session_map[user_id]
            try:
                message_chain = MessageChain().message(
                    f"排队位置更新: 您当前排名 {new_position}\n(前方还有 {new_position - 1} 人)"
                )
                await self.context.send_message(user_session["user_umo"], message_chain)
            except Exception as e:
                logger.error(f"通知用户 {user_id} 位置变化失败: {str(e)}")

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

        target_id = str(target_id)
        session = self.session_map.get(target_id)

        # 验证会话状态
        if not session or session["status"] != "waiting":
            yield event.plain_result(f"用户({target_id})未在排队或对话中")
            return

        if session["status"] == "connected":
            yield event.plain_result("您正在与该用户对话")

        # 更新会话状态时保留用户umo
        session["status"] = "connected"
        session["servicer_id"] = sender_id
        session["start_time"] = time.time()  # 重置计时
        # 保留用户的umo信息
        session["user_umo"] = session.get("user_umo", "")

        # 通知用户
        try:
            conversation_timeout = round(self.conversation_timeout / 60, 2)
            await self.send(
                event,
                message=(
                    f"超级管理员👤:{sender_name}\n已接入对话⚠️⚠️⚠️\n您最多有{conversation_timeout}分钟的时间进行对话\n(请用简洁的话描述所遇到的问题)"
                ),
                group_id=session["group_id"],
                user_id=target_id,
            )
        except Exception as e:
            logger.error(f"通知用户 {target_id} 客服接入失败: {str(e)}")
            session["status"] = "waiting"  # 恢复状态
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

        for uid, session in self.session_map.items():
            if session["servicer_id"] == sender_id:
                await self.send(
                    event,
                    message=(f"超级管理员👤：{sender_name}\n❗已结束与你的对话❗"),
                    group_id=session["group_id"],
                    user_id=uid,
                )
                del self.session_map[uid]
                yield event.plain_result(f"已结束与用户({uid})的对话")
                return

        yield event.plain_result("当前无对话需要结束")
        return

    # 管理员指令：查看当前所有对话
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("查看对话", alias={"查看会话", "查看排队"})
    async def list_active_sessions(self, event: AiocqhttpMessageEvent):
        """查看当前所有活跃的客服对话和排队队列"""
        # 先清理超时会话
        await self._check_session_timeout()
        if not self.session_map:
            yield event.plain_result("当前没有活跃会话和排队请求")
            return

        # 分离等待队列和活跃对话
        waiting_sessions = [
            (uid, session)
            for uid, session in self.session_map.items()
            if session["status"] == "waiting"
        ]
        active_sessions = [
            (uid, session)
            for uid, session in self.session_map.items()
            if session["status"] == "connected"
        ]

        msg_lines = []
        if waiting_sessions:
            msg_lines.append("📋 排队队列：")
            for idx, (uid, session) in enumerate(waiting_sessions):
                duration = int(time.time() - session["start_time"]) // 60
                msg_lines.append(f"{idx + 1}. 用户 {uid}（等待时间：{duration}分钟）")

        if active_sessions:
            msg_lines.append("\n🔗 活跃对话：")
            for uid, session in active_sessions:
                duration = int(time.time() - session["start_time"]) // 60
                msg_lines.append(
                    f"- 用户 {uid}（客服：{session['servicer_id']}，时长：{duration}分钟）"
                )
        yield event.plain_result("\n".join(msg_lines))

    async def send(
        self,
        event: AiocqhttpMessageEvent,
        message,
        group_id: int | str | None = None,
        user_id: int | str | None = None,
    ):
        """向用户发消息，兼容群聊或私聊"""
        if group_id and str(group_id) != "0":
            await event.bot.send_group_msg(group_id=int(group_id), message=message)
        elif user_id:
            await event.bot.send_private_msg(user_id=int(user_id), message=message)

    async def send_ob(
        self,
        event: AiocqhttpMessageEvent,
        group_id: int | str | None = None,
        user_id: int | str | None = None,
    ):
        """向用户发onebot格式的消息，兼容群聊或私聊"""
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
            for user_id, session in self.session_map.items():
                if (
                    session["servicer_id"] == sender_id
                    and session["status"] == "connected"
                ):
                    await self.send_ob(
                        event,
                        group_id=session["group_id"],
                        user_id=user_id,
                    )
                    event.stop_event()
                    break

        # 用户 → 管理员
        elif session := self.session_map.get(sender_id):
            if session["status"] == "connected" and session["servicer_id"]:
                await self.send_ob(
                    event,
                    user_id=session["servicer_id"],
                )
                event.stop_event()

    async def terminate(self):
        """插件卸载时调用，清理会话"""
        logger.info("人工客服插件正在卸载，清理会话中...")
        self.session_map.clear()
        logger.info("人工客服插件卸载完成")
