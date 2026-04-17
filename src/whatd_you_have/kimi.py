from __future__ import annotations

import base64
import json
import logging
import re
from typing import Any

import httpx

from .config import settings
from .prompts import SUMMARY_SYSTEM_PROMPT, VISION_SYSTEM_PROMPT

log = logging.getLogger(__name__)


class KimiError(RuntimeError):
    pass


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.kimi_api_key}",
        "Content-Type": "application/json",
    }


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    else:
        first = text.find("{")
        last = text.rfind("}")
        if first != -1 and last != -1 and last > first:
            text = text[first : last + 1]
    return json.loads(text)


def _sniff_image_mime(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


async def analyze_food_image(image_bytes: bytes) -> dict[str, Any]:
    """Call Kimi vision. Kimi requires base64 data URL (not a public URL)."""
    mime = _sniff_image_mime(image_bytes)
    b64 = base64.b64encode(image_bytes).decode("ascii")
    data_url = f"data:{mime};base64,{b64}"
    payload = {
        "model": settings.kimi_vision_model,
        "messages": [
            {"role": "system", "content": VISION_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": "请识别这张图片里的食物并按要求输出 JSON。"},
                ],
            },
        ],
        "temperature": 0.2,
    }
    return await _chat_for_json(payload)


async def analyze_food_text(description: str) -> dict[str, Any]:
    payload = {
        "model": settings.kimi_text_model,
        "messages": [
            {"role": "system", "content": VISION_SYSTEM_PROMPT},
            {"role": "user", "content": f"用户文字描述了自己吃的：{description}\n按要求输出 JSON。"},
        ],
        "temperature": 0.2,
    }
    return await _chat_for_json(payload)


async def _chat_for_json(payload: dict[str, Any]) -> dict[str, Any]:
    url = f"{settings.kimi_base_url.rstrip('/')}/chat/completions"
    async with httpx.AsyncClient(timeout=90.0) as client:
        resp = await client.post(url, headers=_headers(), json=payload)
        if resp.status_code >= 400:
            raise KimiError(f"Kimi API {resp.status_code}: {resp.text[:500]}")
        body = resp.json()
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        raise KimiError(f"Unexpected Kimi response: {body}") from e
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    try:
        return _extract_json(content)
    except json.JSONDecodeError as e:
        raise KimiError(f"Kimi did not return valid JSON: {content[:500]}") from e


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
        "temperature": 0.6,
    }
    url = f"{settings.kimi_base_url.rstrip('/')}/chat/completions"
    async with httpx.AsyncClient(timeout=90.0) as client:
        resp = await client.post(url, headers=_headers(), json=payload)
        if resp.status_code >= 400:
            raise KimiError(f"Kimi API {resp.status_code}: {resp.text[:500]}")
        body = resp.json()
    return body["choices"][0]["message"]["content"].strip()
