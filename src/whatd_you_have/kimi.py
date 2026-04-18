from __future__ import annotations

import logging

import httpx

from .config import settings
from .prompts import SUMMARY_SYSTEM_PROMPT

log = logging.getLogger(__name__)


class KimiError(RuntimeError):
    pass


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.kimi_api_key}",
        "Content-Type": "application/json",
    }


async def write_daily_summary(meals_brief: str, goal_kcal: int) -> str:
    user_msg = (
        f"今日饮食记录：\n{meals_brief}\n\n"
        f"每日目标热量约 {goal_kcal} kcal。请写一段当日总结。"
    )
    payload = {
        "model": settings.kimi_text_model,
        "messages": [
            {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 1,
    }
    url = f"{settings.kimi_base_url.rstrip('/')}/chat/completions"
    async with httpx.AsyncClient(timeout=90.0) as client:
        resp = await client.post(url, headers=_headers(), json=payload)
        if resp.status_code >= 400:
            raise KimiError(f"Kimi API {resp.status_code}: {resp.text[:500]}")
        body = resp.json()
    return body["choices"][0]["message"]["content"].strip()
