from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from . import storage
from .config import settings

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是一个微信饮食追踪助手。用户会发送餐食照片或文字描述，也可能要求修改、删除之前的记录，或查询今日饮食。

规则：
- 看到食物图片或文字描述时，调用 record_meals 记录。若一张图或一段文字描述了多顿饭，可一次传入多个 meal。
- 用户要求修改或删除记录（如"刚才记错了"、"删掉那个"），先调用 get_recent_meals 获取上下文，再 update_meal 或 delete_meal。
- 用户发 /today 或询问今天吃了什么，调用 get_today_summary。
- 回复简短友好，确认操作结果，列出关键营养数据。
- 始终用中文回复。"""

_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "食物名（中文）"},
        "portion": {"type": "string", "description": "份量描述，如 '一碗约200g'"},
        "kcal": {"type": "number"},
        "protein_g": {"type": "number"},
        "fat_g": {"type": "number"},
        "carbs_g": {"type": "number"},
    },
    "required": ["name", "portion", "kcal", "protein_g", "fat_g", "carbs_g"],
}

_MEAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "meal_type": {"type": "string", "enum": ["breakfast", "lunch", "dinner", "snack", "unknown"]},
        "summary": {"type": "string", "description": "一句话概述（中文）"},
        "items": {"type": "array", "items": _ITEM_SCHEMA},
        "total_kcal": {"type": "number"},
        "total_protein_g": {"type": "number"},
        "total_fat_g": {"type": "number"},
        "total_carbs_g": {"type": "number"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
    "required": ["meal_type", "summary", "items", "total_kcal", "total_protein_g", "total_fat_g", "total_carbs_g", "confidence"],
}

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "record_meals",
            "description": "记录一顿或多顿餐食到数据库。",
            "parameters": {
                "type": "object",
                "properties": {
                    "meals": {
                        "type": "array",
                        "items": _MEAL_SCHEMA,
                        "description": "要记录的餐食列表，通常1个，一段文字/图片描述多顿饭时传多个。",
                    }
                },
                "required": ["meals"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recent_meals",
            "description": "获取用户最近 n 条餐食记录（含 meal_id），修改或删除前先查看上下文。",
            "parameters": {
                "type": "object",
                "properties": {
                    "n": {"type": "integer", "description": "返回最近几条，默认3"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_meal",
            "description": "修改指定 meal_id 的记录，需传入完整的更新后字段。",
            "parameters": {
                "type": "object",
                "properties": {
                    "meal_id": {"type": "integer"},
                    **_MEAL_SCHEMA["properties"],
                },
                "required": ["meal_id", "meal_type", "summary", "items", "total_kcal", "total_protein_g", "total_fat_g", "total_carbs_g"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_meal",
            "description": "删除指定 meal_id 的记录。",
            "parameters": {
                "type": "object",
                "properties": {"meal_id": {"type": "integer"}},
                "required": ["meal_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_today_summary",
            "description": "获取用户今日所有餐食的热量和营养汇总。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.kimi_api_key}",
        "Content-Type": "application/json",
    }


def _f(d: dict, key: str, default: float = 0.0) -> float:
    v = d.get(key)
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


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


# ── Tool implementations ──────────────────────────────────────────────────────

async def _tool_record_meals(wxid: str, meals: list[dict]) -> str:
    now = datetime.now(timezone.utc)
    from zoneinfo import ZoneInfo
    local_date = datetime.now(ZoneInfo(settings.timezone)).strftime("%Y-%m-%d")
    ids = []
    for m in meals:
        meal = storage.Meal(
            id=None,
            wxid=wxid,
            eaten_at=now,
            local_date=local_date,
            meal_type=m.get("meal_type"),
            source="agent",
            raw_input=None,
            summary=m.get("summary"),
            items=m.get("items", []),
            total_kcal=_f(m, "total_kcal"),
            protein_g=_f(m, "total_protein_g"),
            fat_g=_f(m, "total_fat_g"),
            carbs_g=_f(m, "total_carbs_g"),
        )
        meal_id = await storage.insert_meal(meal)
        ids.append(meal_id)
    return json.dumps({"ok": True, "meal_ids": ids}, ensure_ascii=False)


async def _tool_get_recent_meals(wxid: str, n: int = 3) -> str:
    rows = await storage.get_recent_meals(wxid, n)
    result = [
        {"meal_id": r[0], "meal_type": r[1], "summary": r[2], "total_kcal": r[3], "eaten_at": r[4]}
        for r in rows
    ]
    return json.dumps(result, ensure_ascii=False)


async def _tool_update_meal(wxid: str, meal_id: int, args: dict) -> str:
    await storage.update_meal(
        meal_id,
        summary=args.get("summary", ""),
        meal_type=args.get("meal_type", "unknown"),
        items=args.get("items", []),
        total_kcal=_f(args, "total_kcal"),
        protein_g=_f(args, "total_protein_g"),
        fat_g=_f(args, "total_fat_g"),
        carbs_g=_f(args, "total_carbs_g"),
    )
    return json.dumps({"ok": True, "meal_id": meal_id}, ensure_ascii=False)


async def _tool_delete_meal(wxid: str, meal_id: int) -> str:
    await storage.delete_meal(meal_id)
    return json.dumps({"ok": True, "meal_id": meal_id}, ensure_ascii=False)


async def _tool_get_today_summary(wxid: str) -> str:
    summary = await storage.get_today_summary(wxid)
    return json.dumps(summary, ensure_ascii=False)


async def _dispatch(wxid: str, name: str, args: dict) -> str:
    try:
        if name == "record_meals":
            return await _tool_record_meals(wxid, args.get("meals", []))
        if name == "get_recent_meals":
            return await _tool_get_recent_meals(wxid, int(args.get("n", 3)))
        if name == "update_meal":
            meal_id = int(args.pop("meal_id"))
            return await _tool_update_meal(wxid, meal_id, args)
        if name == "delete_meal":
            return await _tool_delete_meal(wxid, int(args["meal_id"]))
        if name == "get_today_summary":
            return await _tool_get_today_summary(wxid)
        return json.dumps({"error": f"unknown tool: {name}"})
    except Exception as e:
        log.exception("tool %s failed", name)
        return json.dumps({"error": str(e)})


# ── Agent loop ────────────────────────────────────────────────────────────────

async def run_agent(wxid: str, text: str | None, image_bytes: bytes | None = None) -> str:
    user_content: list[dict] = []
    if image_bytes:
        mime = _sniff_image_mime(image_bytes)
        b64 = base64.b64encode(image_bytes).decode("ascii")
        user_content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
    if text:
        user_content.append({"type": "text", "text": text})
    if not user_content:
        user_content = [{"type": "text", "text": "（空消息）"}]

    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]

    url = f"{settings.kimi_base_url.rstrip('/')}/chat/completions"
    async with httpx.AsyncClient(timeout=120.0) as client:
        for _ in range(5):
            payload = {
                "model": settings.kimi_vision_model,
                "messages": messages,
                "tools": TOOLS,
                "temperature": 1,
            }
            resp = await client.post(url, headers=_headers(), json=payload)
            if resp.status_code >= 400:
                raise RuntimeError(f"Kimi API {resp.status_code}: {resp.text[:400]}")
            body = resp.json()
            choice = body["choices"][0]
            msg = choice["message"]
            messages.append(msg)

            if choice["finish_reason"] != "tool_calls":
                return msg.get("content") or "（无回复）"

            for tc in msg.get("tool_calls", []):
                fn = tc["function"]
                args = json.loads(fn.get("arguments", "{}"))
                result = await _dispatch(wxid, fn["name"], args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                })

    return "处理时间过长，请稍后重试。"
