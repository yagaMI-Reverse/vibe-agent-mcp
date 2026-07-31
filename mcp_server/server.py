# -*- coding: utf-8 -*-
"""MCP-сервер поверх Agent API Вайб-Маркетолога.

Отдаёт Claude Code 6 инструментов: 4 бесплатных read-only (аккаунт, бренды,
цены, локальный preflight) и 2 платных write-инструмента (генерация
объявлений, аудит лендинга) — оба с бюджетным гейтом и идемпотентностью.

Запуск: python mcp_server/server.py
Регистрация в Claude Code: claude mcp add vibe -- python <путь>/mcp_server/server.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "vendor" / "ad-preflight"))

import vibe_client as vc
import preflight as pf  # vendor/ad-preflight/preflight.py — не переписываем чек-логику

mcp = MCPServer("vibe-marketolog")


@mcp.tool()
def account_status() -> dict:
    """Баланс, дневной лимит и остаток дневного лимита. Бесплатно. Вызывать
    перед любой серией платных операций."""
    token = vc.load_token()
    return vc.account_status(token)


@mcp.tool()
def price_table() -> dict:
    """Фиксированные цены direct/*-инструментов (ads-generate, landing-audit,
    sitelinks, forecast) в рублях. Бесплатно. У direct/* НЕТ бесплатного
    /generate/estimate — цена известна заранее, гейт строится на сравнении
    с этой таблицей, а не на dry-run вызове."""
    token = vc.load_token()
    return vc.price_table(token)


@mcp.tool()
def list_brands() -> dict:
    """Список брендов и товаров аккаунта с активной парой. Бесплатно.
    Внимание: direct/ads-generate НЕ принимает brand_id — бренд/товар нужно
    прочитать и вручную собрать из них текст utp для генератора."""
    token = vc.load_token()
    return vc.list_brands(token)


@mcp.tool()
def get_brand(brand_id: str) -> dict:
    """Профиль одного бренда с товарами. Бесплатно."""
    token = vc.load_token()
    return vc.get_brand(token, brand_id)


@mcp.tool()
def generate_ads(url: str = "", utp: str = "", idempotency_key: str = "") -> dict:
    """Генерирует пакет объявлений (direct/ads-generate). ПЛАТНО — 49₽,
    списывается сразу при вызове. Нужен url и/или utp (хотя бы один).
    Вызывающий обязан сам свериться с account_status()/price_table() ДО
    вызова — сервер не делает скрытых проверок баланса за вас."""
    token = vc.load_token()
    return vc.generate_ads(token, url=url, utp=utp, idempotency_key=idempotency_key or None)


@mcp.tool()
def audit_landing(url: str, idempotency_key: str = "") -> dict:
    """Аудит лендинга (direct/landing-audit). ПЛАТНО — 39₽. Возвращает
    score 1-10, сильные и слабые стороны, рекомендации."""
    token = vc.load_token()
    return vc.audit_landing(token, url=url, idempotency_key=idempotency_key or None)


@mcp.tool()
def generate_sitelinks(url: str, idempotency_key: str = "") -> dict:
    """Быстрые ссылки и уточнения для объявления (direct/sitelinks). ПЛАТНО —
    29₽. 8 sitelinks (title<=30, description<=60) + 6 callouts (<=25)."""
    token = vc.load_token()
    return vc.generate_sitelinks(token, url=url, idempotency_key=idempotency_key or None)


@mcp.tool()
def preflight_check(groups: list, landing_url: str) -> dict:
    """Прогоняет результат generate_ads через детерминированный чек
    (ad-preflight): лимиты Яндекс.Директа, ст.5 ФЗ «О рекламе», сверка
    обещаний с текстом лендинга. Полностью бесплатно и локально — не бьёт
    в API. `groups` — это gen["result"]["groups"] из ответа generate_ads."""
    landing = pf.fetch_landing_text(landing_url)
    results = []
    for g in groups:
        combo = g.get("combinatorial", {})
        kws = [pf.as_str(k, "keyword", "phrase", "text", "name") for k in g.get("keywords", [])]
        for t in [pf.as_str(x, "title", "text") for x in combo.get("titles", [])]:
            for x in [pf.as_str(y, "text", "body") for y in combo.get("texts", [])]:
                results.append(pf.check_ad(t.strip(), x.strip(), kws, landing))
    counts: dict = {}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    top_rules: dict = {}
    for r in results:
        for msg in r["errors"] + r["warnings"] + r["halluc"]:
            key = msg.split(":")[0] if ":" in msg else msg.split("«")[0] if "«" in msg else msg
            key = re.sub(r"\s+", " ", re.sub(r"\d+%?", "", key)).strip()
            top_rules[key] = top_rules.get(key, 0) + 1
    return {
        "total": len(results),
        "counts": counts,
        "top_rules": sorted(top_rules.items(), key=lambda kv: -kv[1])[:5],
        "details": results,
    }


if __name__ == "__main__":
    mcp.run()
