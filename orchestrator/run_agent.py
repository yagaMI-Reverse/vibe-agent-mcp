# -*- coding: utf-8 -*-
"""Автономный цикл: генерация объявлений → preflight → локальный фикс → стоп.

Задача агента: «сделай кампанию для <лендинга>» без участия человека —
1. проверить деньги (account_status, price_table) — бюджетный гейт ДО каждого
   платного вызова, не после;
2. сгенерировать объявления (direct/ads-generate, 49₽ фикс.);
3. прогнать через preflight (детерминированные проверки, ad-preflight, 0₽);
4. механически починить то, что чинится локально и бесплатно (превышение
   длины заголовка/текста — просто обрезка по слову), а не просить генератор
   переписать словами: живой прогон 31.07.2026 показал, что текстовая
   инструкция в utp про длину заголовка НЕ снижает брак, а увеличивает его
   (90→111→153 за 3 итерации, см. docs/boundaries.md). Переформулировка utp
   оставлена только для содержательных дефектов (непроверенные превосходные
   степени, обещания без подтверждения на лендинге) — их локально не почини,
   там правда нужна другая генерация;
5. остановиться, когда выдача чистая либо упёрлись в лимит бюджета/итераций;
6. записать отчёт: итерации, потрачено ₽, брак по итерациям, сколько починено
   локально, топ правил.

Запуск:
  python orchestrator/run_agent.py --url https://example.ru --utp "..." \
      --budget-limit 186 --max-iterations 3 --audit --out runs/2026-07-31/report.md

  --dry-run  — не бьёт в платное API вообще, гоняет цикл на
               fixtures/raw_response_sample.json (для отладки логики на 0₽).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp_server"))
sys.path.insert(0, str(ROOT / "vendor" / "ad-preflight"))

import vibe_client as vc  # noqa: E402
import preflight as pf  # noqa: E402

# Дефолтный бюджет: 3 генерации + 1 аудит лендинга, с запасом от баланса 453₽.
DEFAULT_BUDGET_RUB = 3 * vc.KNOWN_PRICES_RUB["direct/ads-generate"] + vc.KNOWN_PRICES_RUB["direct/landing-audit"]

CLEAN_VERDICTS = {"ГОДНО", "ИСПРАВИМО"}  # то, с чем можно запускать кампанию

# Только содержательные дефекты — то, что локальный фиксер (обрезка длины)
# принципиально не может починить, потому что это не про символы, а про смысл.
# "заголовок"/"текст"/"слово" сюда намеренно не входят: доказано (31.07.2026),
# что просьба словами в utp не помогает и даже вредит — эти дефекты чинятся
# только локальной обрезкой (см. try_fix_length ниже).
REFORMULATIONS = {
    "превосходная степень без подтверждения": (
        "Не используй превосходную степень («лучший», «№1», «самый», «100%») без цифр на лендинге."
    ),
    "обещание": (
        "Упоминай только те скидки, сроки, гарантии и цены, которые буквально есть на лендинге; "
        "остальное — не выдумывай."
    ),
}


def build_utp(base_utp: str, top_rules: list) -> str:
    extra = []
    for rule_key, _ in top_rules:
        for trigger, addition in REFORMULATIONS.items():
            if trigger in rule_key.lower() and addition not in extra:
                extra.append(addition)
    if not extra:
        return base_utp
    return (base_utp + " " if base_utp else "") + " ".join(extra)


def truncate_at_word(s: str, limit: int) -> str:
    """Обрезает строку по границе слова, не разрывая слово посередине."""
    if len(s) <= limit:
        return s
    cut = s[:limit]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,.–—-")


LENGTH_ERROR_PREFIXES = ("заголовок", "текст")


def is_fixable_length_only(errors: list) -> bool:
    """True, если единственная причина БРАК — превышение длины заголовка/текста
    (не длинное слово без пробелов — его обрезкой не почини, там нужна другая
    формулировка, а таких случаев мало)."""
    return bool(errors) and all(e.startswith(LENGTH_ERROR_PREFIXES) for e in errors)


def apply_local_fixes(results: list, landing: str) -> tuple:
    """Механическая пост-обработка: только обрезка по длине, без изменения
    смысла. Не трогает ГАЛЛЮЦИНАЦИЯ/превосходные степени — там подделка
    вердикта обрезкой была бы нечестной, эти дефекты остаются как есть."""
    fixed_count = 0
    out = []
    for r in results:
        if r["verdict"] == "БРАК" and not r["halluc"] and is_fixable_length_only(r["errors"]):
            new_title = truncate_at_word(r["title"], pf.TITLE1_MAX)
            new_text = truncate_at_word(r["text"], pf.TEXT_MAX)
            candidate = pf.check_ad(new_title, new_text, r.get("_keywords", []), landing)
            if candidate["verdict"] != "БРАК":
                candidate["fixed_locally"] = True
                candidate["original_title"] = r["title"]
                out.append(candidate)
                fixed_count += 1
                continue
        out.append(r)
    return out, fixed_count


def categorize_rule(msg: str) -> str:
    """Группирует сообщение проверки в устойчивую категорию: числа (длина
    заголовка, % покрытия) отличаются от объявления к объявлению, но причина
    одна и та же — иначе топ-правил рассыпается на десятки почти дублей."""
    if ":" in msg:
        key = msg.split(":")[0]
    elif "«" in msg:
        key = msg.split("«")[0]
    else:
        key = msg
    key = re.sub(r"\d+%?", "", key)
    return re.sub(r"\s+", " ", key).strip()


def run_preflight(groups: list, landing_url: str) -> dict:
    landing = pf.fetch_landing_text(landing_url)
    results = []
    for g in groups:
        combo = g.get("combinatorial", {})
        kws = [pf.as_str(k, "keyword", "phrase", "text", "name") for k in g.get("keywords", [])]
        for t in [pf.as_str(x, "title", "text") for x in combo.get("titles", [])]:
            for x in [pf.as_str(y, "text", "body") for y in combo.get("texts", [])]:
                r = pf.check_ad(t.strip(), x.strip(), kws, landing)
                r["_keywords"] = kws
                results.append(r)

    results, fixed_count = apply_local_fixes(results, landing)

    counts: dict = {}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    top_rules: dict = {}
    for r in results:
        for msg in r["errors"] + r["warnings"] + r["halluc"]:
            key = categorize_rule(msg)
            top_rules[key] = top_rules.get(key, 0) + 1
    ranked = sorted(top_rules.items(), key=lambda kv: -kv[1])
    return {
        "total": len(results),
        "counts": counts,
        "top_rules": ranked,
        "details": results,
        "fixed_locally": fixed_count,
    }


def load_fixture_groups() -> list:
    data = json.loads((ROOT / "fixtures" / "raw_response_sample.json").read_text(encoding="utf-8"))
    return data.get("result", {}).get("groups", [])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--utp", default="")
    ap.add_argument("--budget-limit", type=float, default=DEFAULT_BUDGET_RUB)
    ap.add_argument("--max-iterations", type=int, default=3)
    ap.add_argument("--audit", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="0₽: без реальных вызовов, на fixtures")
    ap.add_argument("--out", default="runs/latest/report.md")
    a = ap.parse_args()

    run_id = str(uuid.uuid4())[:8]
    log: list = []
    spent = 0.0
    raw_dir = Path(a.out).resolve().parent / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    if a.dry_run:
        status = {"balance": 999999, "daily_spend_remaining": 999999}
        prices = dict(vc.KNOWN_PRICES_RUB)
    else:
        token = vc.load_token()
        status = vc.account_status(token)
        prices = vc.price_table(token)

    ads_price = prices.get("direct/ads-generate", vc.KNOWN_PRICES_RUB["direct/ads-generate"])
    audit_price = prices.get("direct/landing-audit", vc.KNOWN_PRICES_RUB["direct/landing-audit"])

    print(f"[gate] баланс={status['balance']}₽ дневной остаток={status['daily_spend_remaining']}₽ "
          f"лимит прогона={a.budget_limit}₽")
    if status["balance"] < ads_price or status["daily_spend_remaining"] < ads_price:
        sys.exit(f"[gate] СТОП: денег не хватает даже на одну генерацию ({ads_price}₽).")

    utp = a.utp
    stop_reason = "исчерпаны итерации"
    iteration = 0
    for iteration in range(1, a.max_iterations + 1):
        if spent + ads_price > a.budget_limit:
            stop_reason = f"бюджетный лимит прогона ({a.budget_limit}₽) не позволяет ещё одну генерацию"
            iteration -= 1
            break

        idem = f"run-{run_id}-iter-{iteration}"
        print(f"\n[{iteration}/{a.max_iterations}] generate_ads(url={a.url!r}, utp={utp!r}) "
              f"idempotency={idem}")
        if a.dry_run:
            gen = {"result": {"groups": load_fixture_groups()}}
            cost = ads_price
        else:
            gen = vc.generate_ads(token, url=a.url, utp=utp, idempotency_key=idem)
            cost = gen.get("cost", ads_price)
        spent += cost
        (raw_dir / f"iter_{iteration}.json").write_text(
            json.dumps(gen, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        groups = gen.get("result", {}).get("groups", [])
        pfres = run_preflight(groups, a.url)
        print(f"    потрачено на шаге: {cost}₽ (всего {spent}₽) | вердикты: {pfres['counts']} "
              f"| починено локально: {pfres['fixed_locally']}")

        log.append({
            "iteration": iteration,
            "utp_used": utp,
            "cost_rub": cost,
            "counts": pfres["counts"],
            "top_rules": pfres["top_rules"],
            "total_ads": pfres["total"],
            "fixed_locally": pfres["fixed_locally"],
        })

        defective = pfres["counts"].get("БРАК", 0) + pfres["counts"].get("ГАЛЛЮЦИНАЦИЯ", 0)
        if defective == 0 and pfres["total"] > 0:
            stop_reason = "выдача чистая (0 БРАК/ГАЛЛЮЦИНАЦИЯ)"
            break
        if iteration == a.max_iterations:
            stop_reason = "исчерпаны итерации, брак остаётся"
            break
        utp = build_utp(a.utp, pfres["top_rules"])

    audit_result = None
    if a.audit:
        if spent + audit_price > a.budget_limit:
            print(f"\n[audit] пропущен: бюджет ({a.budget_limit}₽) не позволяет ещё {audit_price}₽")
        else:
            print(f"\n[audit] landing-audit ({audit_price}₽)…")
            if a.dry_run:
                audit_result = {"score": "N/A (dry-run)", "summary": "офлайн-фикстура без аудита"}
            else:
                raw = vc.audit_landing(token, a.url, idempotency_key=f"run-{run_id}-audit")
                (raw_dir / "audit.json").write_text(
                    json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                audit_result = raw.get("result", raw)
            spent += audit_price

    write_report(a.out, a.url, a.utp, log, stop_reason, spent, audit_result, a.dry_run)
    print(f"\n=== ИТОГ === потрачено {spent}₽, итераций {len(log)}, стоп: {stop_reason}")
    print(f"Отчёт: {a.out}")


def write_report(out_path, url, base_utp, log, stop_reason, spent, audit_result, dry_run):
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    lines = [
        "# Отчёт автономного прогона",
        "",
        f"- Режим: {'DRY-RUN (0₽, fixtures)' if dry_run else 'LIVE (реальные деньги)'}",
        f"- Время: {now}",
        f"- Лендинг: {url}",
        f"- Базовый utp: {base_utp!r}",
        f"- Причина остановки: **{stop_reason}**",
        f"- Потрачено всего: **{spent}₽**",
        f"- Итераций: **{len(log)}**",
        "",
        "## Итерации",
        "",
        "| # | стоимость,₽ | всего объявл. | ГОДНО | ИСПРАВИМО | БРАК | ГАЛЛЮЦИНАЦИЯ | починено локально | топ-правило |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for it in log:
        c = it["counts"]
        top = it["top_rules"][0][0] if it["top_rules"] else "—"
        lines.append(
            f"| {it['iteration']} | {it['cost_rub']} | {it['total_ads']} | "
            f"{c.get('ГОДНО', 0)} | {c.get('ИСПРАВИМО', 0)} | {c.get('БРАК', 0)} | "
            f"{c.get('ГАЛЛЮЦИНАЦИЯ', 0)} | {it['fixed_locally']} | {top} |"
        )
    lines += ["", "## utp по итерациям", ""]
    for it in log:
        lines.append(f"{it['iteration']}. `{it['utp_used'] or '(пусто, только url)'}`")
    lines += ["", "## Топ сработавших правил (последняя итерация)", ""]
    if log:
        for rule, cnt in log[-1]["top_rules"]:
            lines.append(f"- {rule}: {cnt}")
    if audit_result:
        lines += ["", "## Landing-audit", "", f"Score: {audit_result.get('score')}",
                  f"{audit_result.get('summary', '')}"]
    p.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
