# -*- coding: utf-8 -*-
"""Тонкий HTTP-клиент к Agent API Вайб-Маркетолога.

Общий для MCP-сервера и оркестратора. Инкапсулирует грабли,
проверенные на первом тестовом (ad-preflight):
  - /direct/* синхронные и с локом на аккаунт — только последовательные вызовы;
  - среднее время ответа ~114с, таймаут 600, не 180;
  - обязателен заголовок Idempotency-Key на платных вызовах;
  - на 429/503 — уважать Retry-After, backoff 1→2→4→8с.
"""
from __future__ import annotations

import os
import time
import uuid
from pathlib import Path
from typing import Any

import requests

BASE = "https://lk.vibemarketolog.ru/api/agent"
TOKEN_FILE = Path("C:/Users/Ilay/vibe_token.txt")

# Фиксированные цены direct/*-инструментов — здесь НЕТ бесплатного /generate/estimate
# (это подтверждено вызовом живого API 31.07.2026: /generate/estimate валиден только
# для общей мультимедиа-фабрики image/video/text/voice/music и для brand_id-полей).
# Источник истины в рантайме — /capabilities.direct_tools, это лишь офлайн-фолбэк.
KNOWN_PRICES_RUB = {
    "direct/landing-audit": 39,
    "direct/ads-generate": 49,
    "direct/sitelinks": 29,
    "direct/forecast": 49,
}


class VibeApiError(RuntimeError):
    def __init__(self, path: str, status: int, data: dict):
        self.path = path
        self.status = status
        self.data = data
        super().__init__(
            f"{path}: HTTP {status} / {data.get('error')}: {data.get('message')} "
            f"(request_id={data.get('request_id')})"
        )


def load_token() -> str:
    tok = os.environ.get("VIBE_API_TOKEN", "").strip()
    if not tok and TOKEN_FILE.exists():
        tok = TOKEN_FILE.read_text(encoding="utf-8").strip()
    if not tok:
        raise RuntimeError(
            "Нет токена: задайте VIBE_API_TOKEN или положите его в C:/Users/Ilay/vibe_token.txt"
        )
    return tok


def _headers(token: str, idempotency_key: str | None) -> dict:
    h = {"Authorization": f"Bearer {token}"}
    if idempotency_key:
        h["Idempotency-Key"] = idempotency_key
    return h


def api_get(path: str, token: str) -> dict:
    r = requests.get(f"{BASE}{path}", headers=_headers(token, None), timeout=60)
    return _parse(path, r)


def api_post(
    path: str,
    payload: dict,
    token: str,
    idempotency_key: str | None = None,
    max_attempts: int = 5,
) -> dict:
    """POST с ретраями на 429/503 (backoff 1,2,4,8с, уважая Retry-After)."""
    idem = idempotency_key or str(uuid.uuid4())
    delay = 1
    for attempt in range(1, max_attempts + 1):
        r = requests.post(
            f"{BASE}{path}", json=payload, headers=_headers(token, idem), timeout=600
        )
        if r.status_code in (429, 503) and attempt < max_attempts:
            retry_after = r.headers.get("Retry-After")
            wait = float(retry_after) if retry_after else delay
            time.sleep(wait)
            delay = min(delay * 2, 8)
            continue
        return _parse(path, r)
    return _parse(path, r)


def _parse(path: str, r: requests.Response) -> dict:
    try:
        data = r.json()
    except ValueError:
        raise VibeApiError(path, r.status_code, {"error": "non_json", "message": r.text[:300]})
    if r.status_code != 200 or data.get("status") == "error":
        raise VibeApiError(path, r.status_code, data)
    return data


# --- Высокоуровневые обёртки, используемые и MCP-tools, и оркестратором ---

def account_status(token: str) -> dict:
    """/me + /balance слитые вместе — единая точка правды по деньгам."""
    me = api_get("/me", token)
    bal = api_get("/balance", token)
    return {
        "balance": bal.get("balance"),
        "balance_real": bal.get("balance_real"),
        "balance_bonus": bal.get("balance_bonus"),
        "daily_spend_limit": me.get("daily_spend_limit"),
        "daily_spend_today": me.get("daily_spend_today"),
        "daily_spend_remaining": bal.get("daily_spend_remaining"),
        "requests_today": me.get("requests_today"),
        "token_expires_at": me.get("expires_at"),
    }


def price_table(token: str) -> dict:
    """Цены direct/*-инструментов из /capabilities; при сбое — офлайн-таблица."""
    try:
        cap = api_get("/capabilities", token)
        tools = cap.get("direct_tools", {}).get("endpoints", [])
        if tools:
            return {t["path"]: t["price_rub"] for t in tools if "path" in t}
    except Exception:
        pass
    return dict(KNOWN_PRICES_RUB)


def list_brands(token: str) -> dict:
    return api_get("/brands", token)


def get_brand(token: str, brand_id: str) -> dict:
    return api_get(f"/brand/{brand_id}", token)


def generate_ads(token: str, url: str = "", utp: str = "", idempotency_key: str | None = None) -> dict:
    payload = {}
    if url:
        payload["url"] = url
    if utp:
        payload["utp"] = utp
    return api_post("/direct/ads-generate", payload, token, idempotency_key)


def audit_landing(token: str, url: str, idempotency_key: str | None = None) -> dict:
    return api_post("/direct/landing-audit", {"url": url}, token, idempotency_key)


def generate_sitelinks(token: str, url: str, idempotency_key: str | None = None) -> dict:
    """8 быстрых ссылок + 6 уточнений (direct/sitelinks, 29₽) — бонус, не в брифе."""
    return api_post("/direct/sitelinks", {"url": url}, token, idempotency_key)
