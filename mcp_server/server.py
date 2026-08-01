# -*- coding: utf-8 -*-
"""MCP-сервер поверх Agent API Вайб-Маркетолога.

Отдаёт Claude Code 8 инструментов: 5 бесплатных read-only (аккаунт, бренды,
цены, локальный preflight с локальным фиксером длины) и 3 платных
write-инструмента (генерация объявлений, аудит лендинга, sitelinks) — все
с идемпотентностью.

Запуск: python mcp_server/server.py
Регистрация в Claude Code: claude mcp add vibe -- python <путь>/mcp_server/server.py
"""
from __future__ import annotations

import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "vendor" / "ad-preflight"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "orchestrator"))

import vibe_client as vc
import preflight as pf  # vendor/ad-preflight/preflight.py — не переписываем чек-логику
import run_agent as orch  # общий с оркестратором run_preflight()+локальный фиксер длины

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
    обещаний с текстом лендинга — и локальный фиксер: объявления, забракованные
    только за превышение длины, механически обрезаются по границе слова и
    перепроверяются (просить генератор словами короче — доказанно не работает,
    см. docs/boundaries.md). Полностью бесплатно и локально — не бьёт в API.
    `groups` — это gen["result"]["groups"] из ответа generate_ads."""
    pfres = orch.run_preflight(groups, landing_url)
    return {
        "total": pfres["total"],
        "counts": pfres["counts"],
        "fixed_locally": pfres["fixed_locally"],
        "top_rules": pfres["top_rules"][:5],
        "details": pfres["details"],
    }


if __name__ == "__main__":
    mcp.run()
