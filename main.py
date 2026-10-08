import asyncio
import argparse
import sys
from datetime import datetime, timedelta

from config import FILTERS, SCRAPING, SCHEDULE
from analyzer import analyze_car
from market_context import fetch_market_context
from notifier import format_report, send_telegram_message
from parsers.avito import parse_avito
from parsers.autoru import parse_autoru
from parsers.drom import parse_drom
from quality_filters import is_acceptable_private_car


async def run_parser(collection_hours=None, seen_ids=None):
    """Запускает парсинг всех площадок и возвращает топ-объявлений с анализом.

    collection_hours: сколько часов брать для Avito/Auto.ru (None = из настроек).
    seen_ids: множество уже показанных source_id — такие объявления пропускаются.
    """
    seen_ids = seen_ids or set()
    print("🚀 Запуск парсера...")
    all_ads = []

    # Переопределяем окно сбора только для Avito/Auto.ru (Дром живёт в своём 24ч-окне)
    original_lookbacks = {}
    if collection_hours:
        for key in ("avito_lookback_hours", "autoru_lookback_hours"):
            original_lookbacks[key] = FILTERS[key]
            FILTERS[key] = int(collection_hours)

    try:
        # Avito (REST-App API)
        print("📋 Парсинг Авито...")
        try:
            avito_ads = await parse_avito()
            print(f"   Найдено: {len(avito_ads)}")
            all_ads.extend(avito_ads)
        except Exception as e:
            print(f"   Ошибка: {e}")

        # Авто.ру (REST-App API)
        print("📋 Парсинг Авто.ру...")
        try:
            autoru_ads = await parse_autoru()
            print(f"   Найдено: {len(autoru_ads)}")
            all_ads.extend(autoru_ads)
        except Exception as e:
            print(f"   Ошибка: {e}")

        # Дром (REST-App API)
        print("📋 Парсинг Дром...")
        try:
            drom_ads = await parse_drom()
            print(f"   Найдено: {len(drom_ads)}")
            all_ads.extend(drom_ads)
        except Exception as e:
            print(f"   Ошибка: {e}")
    finally:
        FILTERS.update(original_lookbacks)

    gathered_count = len(all_ads)
    all_ads = [ad for ad in all_ads if is_acceptable_private_car(ad)]
    print(f"\nВсего собрано: {gathered_count} объявлений")
    print(f"Частных и без признаков тотала: {len(all_ads)}")

    # Убираем дубли по URL
    seen_urls = set()
    unique_ads = []
    for ad in all_ads:
        if ad["url"] and ad["url"] not in seen_urls:
            seen_urls.add(ad["url"])
            unique_ads.append(ad)
        elif not ad["url"]:
            unique_ads.append(ad)

    print(f"Уникальных: {len(unique_ads)}")

    if seen_ids:
        before = len(unique_ads)
        unique_ads = [
            ad for ad in unique_ads
            if str(ad.get("source_id") or ad.get("url") or "") not in seen_ids
        ]
        print(f"Отсеяно уже показанных: {before - len(unique_ads)}; новых: {len(unique_ads)}")

    # Анализ с живым контекстом рынка
    analyses = []
    for ad in unique_ads:
        comparables = None
        try:
            comparables = await fetch_market_context(ad)
            if not comparables:
                comparables = None  # откат на накопленную историю SQLite
        except Exception as exc:
            print(f"   Рынок {ad.get('brand')} {ad.get('model')} {ad.get('year')}: {exc}")
            comparables = None
        analysis = analyze_car(ad, comparables=comparables)
        analyses.append(analysis)

    analyses.sort(
        key=lambda analysis: (
            not analysis["is_below_market"],
            -analysis["discount_pct"] if analysis["discount_pct"] is not None else 0,
            -analysis["profit"],
        )
    )

    good_analyses = [
        analysis
        for analysis in analyses
        if analysis["is_below_market"] and not analysis["red_flags"]
    ]
    if good_analyses:
        top = good_analyses[:SCRAPING["top_count"]]
    else:
        unvalued_candidates = [
            analysis
            for analysis in analyses
            if analysis["market_price"] is None and not analysis["red_flags"]
        ]
        unvalued_candidates.sort(key=lambda analysis: (analysis["price"], analysis["mileage"]))
        for analysis in unvalued_candidates:
            analysis["priority"] = "⚪ РЫНОК НЕ ПОДТВЕРЖДЕН"
            analysis["recommendation"] = (
                "Объявление прошло фильтры продавца и состояния, "
                "но недостаточно аналогов для подтверждения цены ниже рынка."
            )
        top = unvalued_candidates[:SCRAPING["top_count"]]

    estimated_count = sum(analysis["market_price"] is not None for analysis in analyses)
    print(f"Оценено по аналогам: {estimated_count} из {len(analyses)}")
    print(f"Ниже рынка на заданный порог: {len(good_analyses)}")
    if not good_analyses:
        print(f"Показано кандидатов без оценки рынка: {len(top)}")
    print(f"Показано топ-{len(top)}")

    for analysis in top:
        sid = str(analysis.get("source_id") or "")
        if sid:
            seen_ids.add(sid)

    return top


def _run_hours(schedule):
    hours = []
    hour = schedule["first_run_hour"]
    while hour < schedule["quiet_start_hour"]:
        hours.append(hour)
        hour += schedule["run_interval_hours"]
    return hours


def _seconds_until_next_run(now, run_hours, first_run_hour):
    current = now.hour + now.minute / 60.0 + now.second / 3600.0
    for hour in run_hours:
        if current < hour:
            target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
            return max(0.0, (target - now).total_seconds())
    target = (now + timedelta(days=1)).replace(hour=first_run_hour, minute=0, second=0, microsecond=0)
    return (target - now).total_seconds()


async def main():
    parser = argparse.ArgumentParser(description="Парсер авто для перекупа")
    parser.add_argument("--console", action="store_true", help="Вывод в консоль вместо Telegram")
    parser.add_argument("--once", action="store_true", help="Один прогон без расписания")
    parser.add_argument("--loop", action="store_true", help="Циклический режим (включён по умолчанию)")
    parser.add_argument("--interval", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--interval-minutes", type=int, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.once:
        top = await run_parser()
        if args.console:
            print("\n" + "=" * 60)
            print(format_report(top))
            print("=" * 60)
        else:
            await send_telegram_message(format_report(top))
        return

    run_hours = _run_hours(SCHEDULE)
    seen_ids = set()
    print(
        f"Расписание: отчёты в {', '.join(f'{h}:00' for h in run_hours)}, "
        f"тишина с {SCHEDULE['quiet_start_hour']}:00 до {SCHEDULE['first_run_hour']}:00"
    )

    while True:
        now = datetime.now()
        delay = _seconds_until_next_run(now, run_hours, SCHEDULE["first_run_hour"])
        print(f"\n⏳ Следующий запуск через ~{int(delay // 60)} мин...")
        await asyncio.sleep(delay)

        now = datetime.now()
        if now.hour == SCHEDULE["first_run_hour"]:
            seen_ids.clear()
            collection_hours = SCHEDULE["morning_lookback_hours"]
            print("🌅 Утренний прогон: собираем за ночь")
        else:
            collection_hours = SCHEDULE["regular_lookback_hours"]

        try:
            top = await run_parser(collection_hours=collection_hours, seen_ids=seen_ids)
            if top:
                text = format_report(top)
                if args.console:
                    print("\n" + "=" * 60)
                    print(text)
                    print("=" * 60)
                else:
                    await send_telegram_message(text)
            else:
                print("Нет новых объявлений в этом цикле.")
        except KeyboardInterrupt:
            print("\nОстановка парсера.")
            sys.exit(0)
        except Exception as e:
            print(f"\nОшибка в цикле: {e}")
            print("Следующая попытка по расписанию...")


if __name__ == "__main__":
    asyncio.run(main())
