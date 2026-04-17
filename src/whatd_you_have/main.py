from __future__ import annotations

import asyncio
import logging
import signal
from datetime import datetime, timezone
from typing import Any

from wechatbot import IncomingMessage, WeChatBot

from . import kimi, scheduler, storage
from .config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("whatd_you_have")


def _f(d: dict[str, Any], key: str, default: float = 0.0) -> float:
    v = d.get(key)
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


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


async def _handle_text(bot: WeChatBot, msg: IncomingMessage) -> None:
    text = (msg.text or "").strip()
    if not text:
        return

    if text in ("/help", "帮助", "help"):
        await bot.reply(
            msg,
            "我是「吃了啥」小助手。\n"
            "- 发图：我来识别并估算热量\n"
            "- 发文字：描述你吃了啥（比如 '中午一碗牛肉面加半份小菜'）\n"
            "- /today 查今日汇总\n"
            "- /help 查看帮助\n"
            "每晚 21 点我会把一天的饮食总结发给你。长时间没发饭，我会花式催。",
        )
        return

    if text in ("/today", "今日", "/summary"):
        await _send_today_summary(bot, msg)
        return

    await bot.send_typing(msg.user_id)
    await bot.reply(msg, "收到～正在帮你分析热量，稍等 🍚")
    try:
        data = await kimi.analyze_food_text(text)
    except kimi.KimiError as e:
        await bot.reply(msg, f"识别失败：{e}")
        return
    await _save_and_reply(bot, msg, source="text", raw=text, data=data)


async def _handle_image(bot: WeChatBot, msg: IncomingMessage) -> None:
    await bot.send_typing(msg.user_id)
    await bot.reply(msg, "收到饭照啦～正在识别 🔍")
    media = await bot.download(msg)
    if media is None or media.type != "image":
        await bot.reply(msg, "图片下载失败，再发一次？")
        return
    try:
        data = await kimi.analyze_food_image(media.data)
    except kimi.KimiError as e:
        await bot.reply(msg, f"识别失败：{e}")
        return
    caption = (msg.text or "").strip() or None
    await _save_and_reply(bot, msg, source="image", raw=caption, data=data)


async def _save_and_reply(
    bot: WeChatBot,
    msg: IncomingMessage,
    *,
    source: str,
    raw: str | None,
    data: dict[str, Any],
) -> None:
    items = data.get("items") or []
    now = datetime.now(timezone.utc)
    local_date = scheduler.local_now().strftime("%Y-%m-%d")
    meal = storage.Meal(
        id=None,
        wxid=msg.user_id,
        eaten_at=now,
        local_date=local_date,
        meal_type=(data.get("meal_type") or None),
        source=source,
        raw_input=raw,
        summary=data.get("summary"),
        items=items,
        total_kcal=_f(data, "total_kcal"),
        protein_g=_f(data, "total_protein_g"),
        fat_g=_f(data, "total_fat_g"),
        carbs_g=_f(data, "total_carbs_g"),
    )
    await storage.insert_meal(meal)
    await storage.reset_nag_state(msg.user_id)

    if not items:
        await bot.reply(msg, "没太看清吃了啥…要不补一段文字描述？")
        return

    item_lines = "\n".join(
        f"  • {i.get('name', '?')}（{i.get('portion', '')}）约 {_f(i, 'kcal'):.0f} kcal"
        for i in items
    )
    reply = (
        f"📝 {data.get('summary', '')}\n"
        f"{item_lines}\n"
        f"小计：{meal.total_kcal:.0f} kcal"
        f"  P {meal.protein_g:.0f}g / F {meal.fat_g:.0f}g / C {meal.carbs_g:.0f}g"
    )
    if (data.get("confidence") or "").lower() == "low":
        reply += "\n（置信度较低，仅供参考）"
    await bot.reply(msg, reply)


async def _send_today_summary(bot: WeChatBot, msg: IncomingMessage) -> None:
    today = scheduler.local_now().strftime("%Y-%m-%d")
    meals = await storage.meals_for_date(msg.user_id, today)
    if not meals:
        await bot.reply(msg, "今天还没有任何记录哦。")
        return
    brief = scheduler.format_meals_brief(meals)
    total = sum(m.total_kcal for m in meals)
    await bot.reply(msg, f"📊 今日({today})已记录：\n{brief}\n合计：{total:.0f} kcal")


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
