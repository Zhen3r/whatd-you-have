from __future__ import annotations

import asyncio
import logging
import signal

from wechatbot import IncomingMessage, WeChatBot

from . import agent, scheduler, storage
from .config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("whatd_you_have")

_pending_images: dict[str, tuple[bytes, asyncio.Task, IncomingMessage]] = {}


def make_bot() -> WeChatBot:
    return WeChatBot(
        cred_path=settings.wechatbot_cred_path,
        on_qr_url=lambda url: log.info("Scan QR to login: %s", url),
        on_scanned=lambda: log.info("QR scanned"),
        on_expired=lambda: log.warning("QR expired, generating a new one"),
        on_error=lambda err: log.error("SDK error: %s", err),
    )


def register_handlers(bot: WeChatBot) -> None:
    @bot.on_message
    async def handle(msg: IncomingMessage) -> None:
        await storage.upsert_user(msg.user_id)
        try:
            if msg.type == "image":
                await _handle_image(bot, msg)
            elif msg.type == "text":
                await _handle_text(bot, msg)
            else:
                await bot.reply(msg, "目前只支持文字描述或图片哦。发张饭照试试？")
        except Exception as e:
            log.exception("handler failed")
            try:
                await bot.reply(msg, f"呃…我这边出了点问题：{e}")
            except Exception:
                pass


async def _handle_image(bot: WeChatBot, msg: IncomingMessage) -> None:
    media = await bot.download(msg)
    if media is None or media.type != "image":
        await bot.reply(msg, "图片下载失败，再发一次？")
        return

    if msg.user_id in _pending_images:
        _, old_task, _ = _pending_images[msg.user_id]
        old_task.cancel()

    wait = settings.image_caption_wait_secs
    await bot.reply(msg, f"收到图片啦～{wait}s 内发文字备注可以帮我更准确 🔍")
    task = asyncio.create_task(_image_timeout(bot, msg, media.data))
    _pending_images[msg.user_id] = (media.data, task, msg)


async def _image_timeout(bot: WeChatBot, img_msg: IncomingMessage, image_bytes: bytes) -> None:
    await asyncio.sleep(settings.image_caption_wait_secs)
    _pending_images.pop(img_msg.user_id, None)
    await _run_and_reply(bot, img_msg, text=None, image_bytes=image_bytes)


async def _handle_text(bot: WeChatBot, msg: IncomingMessage) -> None:
    text = (msg.text or "").strip()
    if not text:
        return

    if msg.user_id in _pending_images:
        image_bytes, task, img_msg = _pending_images.pop(msg.user_id)
        task.cancel()
        await _run_and_reply(bot, msg, text=text, image_bytes=image_bytes)
        return

    if text in ("/help", "帮助", "help"):
        await bot.reply(
            msg,
            "我是「吃了啥」小助手。\n"
            f"- 发图：我来识别并估算热量（可在 {settings.image_caption_wait_secs}s 内补发文字备注）\n"
            "- 发文字：描述你吃了啥\n"
            "- 说【刚才记错了】可修改上一条，说【删掉那个】可删除\n"
            "- /today 查今日汇总\n"
            "- /help 查看帮助\n"
            "每晚 21 点我会把一天的饮食总结发给你。长时间没发饭，我会花式催。",
        )
        return

    await _run_and_reply(bot, msg, text=text)


async def _run_and_reply(
    bot: WeChatBot,
    msg: IncomingMessage,
    *,
    text: str | None,
    image_bytes: bytes | None = None,
) -> None:
    await bot.send_typing(msg.user_id)
    try:
        response = await agent.run_agent(msg.user_id, text=text, image_bytes=image_bytes)
    except Exception as e:
        log.exception("agent failed")
        await bot.reply(msg, f"出了点问题：{e}")
        return
    await storage.reset_nag_state(msg.user_id)
    await bot.reply(msg, response)


async def amain() -> None:
    await storage.init_db()
    bot = make_bot()
    register_handlers(bot)
    await bot.login()
    scheduler.start_scheduler(bot)

    stop_event = asyncio.Event()

    def _request_stop() -> None:
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            pass

    run_task = asyncio.create_task(bot.start())
    stop_task = asyncio.create_task(stop_event.wait())
    done, _pending = await asyncio.wait(
        {run_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
    )
    if stop_task in done:
        log.info("stopping bot...")
        bot.stop()
        try:
            await asyncio.wait_for(run_task, timeout=5.0)
        except (asyncio.TimeoutError, Exception):
            run_task.cancel()
    scheduler.stop_scheduler()
    log.info("bye")


def cli() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    cli()
