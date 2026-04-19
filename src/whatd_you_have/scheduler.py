from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from wechatbot import NoContextError, WeChatBot

from . import kimi, storage
from .config import settings
from .nagging import next_nag

log = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


def tz() -> ZoneInfo:
    return ZoneInfo(settings.timezone)


def local_now() -> datetime:
    return datetime.now(tz())


def format_meals_brief(meals: list[storage.Meal]) -> str:
    lines = []
    for m in meals:
        t = m.eaten_at.astimezone(tz()).strftime("%H:%M")
        items = "、".join(i.get("name", "?") for i in m.items) or (m.summary or "（未识别）")
        lines.append(f"- {t} {m.meal_type or ''}：{items}（约 {m.total_kcal:.0f} kcal）")
    return "\n".join(lines) if lines else "（今日无记录）"


async def _safe_send(bot: WeChatBot, user_id: str, text: str) -> bool:
    """bot.send needs a prior context_token. If we've lost it (e.g. server
    restart with an old user), NoContextError bubbles up — log and skip."""
    try:
        await bot.send(user_id, text)
        return True
    except NoContextError:
        log.warning("no context_token for %s; cannot push proactively", user_id)
        return False
    except Exception:
        log.exception("bot.send failed for %s", user_id)
        return False


async def send_daily_summary(bot: WeChatBot) -> None:
    today = local_now().strftime("%Y-%m-%d")
    for wxid in await storage.list_user_wxids():
        meals = await storage.meals_for_date(wxid, today)
        if not meals:
            await _safe_send(
                bot,
                wxid,
                f"今日({today})没有收到任何餐食记录。明天记得发给我呀，不然我要开始花式催了。",
            )
            continue
        brief = format_meals_brief(meals)
        total_kcal = sum(m.total_kcal for m in meals)
        total_p = sum(m.protein_g for m in meals)
        total_f = sum(m.fat_g for m in meals)
        total_c = sum(m.carbs_g for m in meals)
        try:
            narrative = await kimi.write_daily_summary(brief, settings.default_daily_calorie_goal)
        except Exception:
            log.exception("kimi summary failed; using fallback")
            narrative = "今日小结"
        text = (
            f"📊 今日饮食总结 {today}\n"
            f"{brief}\n\n"
            f"合计：{total_kcal:.0f} kcal  "
            f"P {total_p:.0f}g / F {total_f:.0f}g / C {total_c:.0f}g\n\n"
            f"{narrative}"
        )
        await _safe_send(bot, wxid, text)


async def nag_tick(bot: WeChatBot) -> None:
    now_local = local_now()
    if not (settings.nag_start_hour <= now_local.hour < settings.nag_end_hour):
        log.debug(
            "nag_tick skipped: local hour %s not in [%s, %s)",
            now_local.hour,
            settings.nag_start_hour,
            settings.nag_end_hour,
        )
        return
    threshold = timedelta(hours=settings.nag_after_hours)
    min_gap = timedelta(minutes=settings.nag_interval_minutes)
    now_utc = datetime.now(timezone.utc)
    wxids = await storage.list_user_wxids()
    log.debug("nag_tick: checking %d user(s)", len(wxids))
    for wxid in wxids:
        last_meal = await storage.last_meal_time(wxid)
        if last_meal is None:
            log.debug("nag_tick %s: skip (no meals yet)", wxid)
            continue
        since_meal = now_utc - last_meal
        if since_meal < threshold:
            log.debug(
                "nag_tick %s: skip (%.1fh since last meal < nag_after %sh)",
                wxid,
                since_meal.total_seconds() / 3600,
                settings.nag_after_hours,
            )
            continue
        last_nag_at, level = await storage.get_nag_state(wxid)
        if last_nag_at is not None and now_utc - last_nag_at < min_gap:
            log.debug(
                "nag_tick %s: skip (last nag %.1f min ago < interval %s min)",
                wxid,
                (now_utc - last_nag_at).total_seconds() / 60,
                settings.nag_interval_minutes,
            )
            continue
        msg, new_level = next_nag(level)
        if await _safe_send(bot, wxid, msg):
            await storage.set_nag_state(wxid, now_utc, new_level)
            log.info("nag sent %s level %s -> %s", wxid, level, new_level)


def start_scheduler(bot: WeChatBot) -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    sched = AsyncIOScheduler(timezone=str(tz()))
    sched.add_job(
        send_daily_summary,
        CronTrigger(
            hour=settings.daily_summary_hour,
            minute=settings.daily_summary_minute,
            timezone=str(tz()),
        ),
        args=[bot],
        id="daily_summary",
        replace_existing=True,
    )
    sched.add_job(
        nag_tick,
        IntervalTrigger(minutes=max(1, settings.nag_interval_minutes // 3),
                        start_date=local_now() + timedelta(seconds=10)),
        args=[bot],
        id="nag_tick",
        replace_existing=True,
    )
    sched.start()
    _scheduler = sched
    log.info("scheduler started")
    return sched


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
