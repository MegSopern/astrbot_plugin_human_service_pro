import re

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
    "人工客服插件",
    "1.0.5",
    "https://github.com/MegSopern/astrbot_plugin_human_service_pro",
)
class HumanServicePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.servicers_id: list[str] = config.get("servicers_id", "")
        if not self.servicers_id:
            for admin_id in context.get_config()["admins_id"]:
                if admin_id.isdigit():
                    self.servicers_id.append(admin_id)

        self.session_map = {}

    @filter.command("转人工", priority=1)
    async def transfer_to_human(self, event: AiocqhttpMessageEvent):
        sender_id = event.get_sender_id()
        send_name = event.get_sender_name()
        group_id = event.get_group_id() or "0"

        if sender_id in self.session_map:
            yield event.plain_result("⚠ 您已在等待接入或正在对话")
            return

        self.session_map[sender_id] = {
            "servicer_id": "",
            "status": "waiting",
            "group_id": group_id,
        }
        yield event.plain_result(
            "正在等待超级管理员👤接入...\n(注意：恶意转人工将会被拉黑)"
        )
        for servicer_id in self.servicers_id:
            await self.send(
                event,
                message=f"{send_name}({sender_id}) 请求转人工",
                user_id=servicer_id,
            )

    @filter.command("转人机", priority=1)
    async def transfer_to_bot(self, event: AiocqhttpMessageEvent):
        sender_id = event.get_sender_id()
        sender_name = event.get_sender_name()
        session = self.session_map.get(sender_id)

        if session and session["status"] == "connected":
            await self.send(
                event,
                message=f"❗{sender_name} 已取消人工请求",
                user_id=session["servicer_id"],
            )
            del self.session_map[sender_id]
            yield event.plain_result("好的，我现在是人机啦！")

    @filter.command("接入对话", priority=1)
    async def accept_conversation(
        self, event: AiocqhttpMessageEvent, target_id: str | int | None = None
    ):
        sender_id = event.get_sender_id()
        sender_name = event.get_sender_name()
        if sender_id not in self.servicers_id:
            return

        if reply_seg := next(
            (seg for seg in event.get_messages() if isinstance(seg, Reply)), None
        ):
            if text := reply_seg.message_str:
                if match := re.search(r"\((\d+)\)", text):
                    target_id = match.group(1)

        session = self.session_map.get(target_id)

        if not session or session["status"] != "waiting":
            yield event.plain_result(f"用户({target_id})未请求人工")
            return

        if session["status"] == "connected":
            yield event.plain_result("您正在与该用户对话")

        session["status"] = "connected"
        session["servicer_id"] = sender_id

        await self.send(
            event,
            message=(
                f"超级管理员👤:{sender_name}\n已接入对话⚠️⚠️⚠️\n(请用简洁的话描述所遇到的问题)"
            ),
            group_id=session["group_id"],
            user_id=target_id,
        )
        yield event.plain_result("好的，接下来我将转发你的消息给对方，请开始对话：")
        event.stop_event()

    @filter.command("结束对话")
    async def end_conversation(self, event: AiocqhttpMessageEvent):
        sender_id = event.get_sender_id()
        send_name = event.get_sender_name()
        if sender_id not in self.servicers_id:
            return

        for uid, session in self.session_map.items():
            if session["servicer_id"] == sender_id:
                await self.send(
                    event,
                    message="超级管理员👤已结束对话",
                    group_id=session["group_id"],
                    user_id=uid,
                )
                del self.session_map[uid]
                yield event.plain_result(f"已结束与用户{send_name}({uid})的对话")
                return

        yield event.plain_result("当前无对话需要结束")

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
        chain = event.get_messages()
        if not chain or any(isinstance(seg, (Reply)) for seg in chain):
            return
        sender_id = event.get_sender_id()
        # 管理员 → 用户 (仅私聊生效)
        if (
            sender_id in self.servicers_id
            and event.is_private_chat()
            and event.message_str not in ("接入对话", "结束对话")
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
            if session["status"] == "connected":
                await self.send_ob(
                    event,
                    user_id=session["servicer_id"],
                )
                event.stop_event()
