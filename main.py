import asyncio
import argparse
import sys
from datetime import datetime, timedelta

from config import FILTERS, SCRAPING, SCHEDULE, now_moscow
from analyzer import analyze_car
from market_context import fetch_market_context
from market_context import begin_market_context_run
from notifier import format_report, send_telegram_message
from market_data import (
    daily_report_was_sent,
    filter_previously_sent_ads,
    load_daily_report_candidates,
    load_listing_catalog,
    mark_daily_report_sent,
    mark_listings_sent,
    save_daily_report_candidates,
)
from parsers.avito import parse_avito
from parsers.autoru import parse_autoru
from parsers.drom import parse_drom
from quality_filters import is_acceptable_private_car


async def run_parser(collection_hours=None, seen_ids=None, archive_lookback_hours=None):
    """Запускает парсинг всех площадок и возвращает топ-объявлений с анализом.

    collection_hours: сколько часов брать для Avito/Auto.ru (None = из настроек).
    seen_ids: legacy set для совместимости; повторные отправки контролирует SQLite.
    """
    if seen_ids is None:
        seen_ids = set()
    print("🚀 Запуск парсера...")
    all_ads = []

    # Переопределяем окно сбора только для Avito/Auto.ru (Дром живёт в своём 24ч-окне)
    original_lookbacks = {}
    original_listing_limit = SCRAPING["listing_api_limit"]
    begin_market_context_run()
    if collection_hours:
        for key in ("avito_lookback_hours", "autoru_lookback_hours"):
            original_lookbacks[key] = FILTERS[key]
            FILTERS[key] = int(collection_hours)
    if archive_lookback_hours is not None:
        for key in ("avito_lookback_hours", "autoru_lookback_hours", "drom_lookback_hours"):
            original_lookbacks.setdefault(key, FILTERS[key])
            FILTERS[key] = int(archive_lookback_hours)
        SCRAPING["listing_api_limit"] = SCHEDULE["archive_listing_api_limit"]

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
        SCRAPING["listing_api_limit"] = original_listing_limit

    all_ads.extend(
        load_listing_catalog(
            history_days=FILTERS["market_history_days"],
            max_price=FILTERS["max_price"],
            max_mileage=FILTERS["max_mileage"],
        )
    )
    all_ads = filter_previously_sent_ads(all_ads)
    gathered_count = len(all_ads)
    all_ads = [
        ad for ad in all_ads
        if is_acceptable_private_car(ad)
        and 0 < int(ad.get("price") or 0) <= FILTERS["max_price"]
        and int(ad.get("mileage") or 0) <= FILTERS["max_mileage"]
    ]
    print(f"\nВсего собрано: {gathered_count} объявлений")
    print(f"Частных и без признаков тотала: {len(all_ads)}")

    # Убираем дубли по URL
    seen_urls = set()
    unique_ads = []
    for ad in all_ads:
        identity = (
            str(ad.get("site") or "").casefold(),
            str(ad.get("url") or ad.get("source_id") or ""),
        )
        if identity[1] and identity not in seen_urls:
            seen_urls.add(identity)
            unique_ads.append(ad)
        elif not identity[1]:
            unique_ads.append(ad)

    print(f"Уникальных (новые и ранее сохранённые): {len(unique_ads)}")

    # Анализ с живым контекстом рынка
    analyses = []
    for ad in unique_ads:
        comparables = None
        try:
            comparables = await fetch_market_context(ad)
            if not comparables or len(comparables) < FILTERS["market_min_samples"]:
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
        )
    )

    good_analyses = [
        analysis
        for analysis in analyses
        if analysis["is_below_market"]
        and not analysis["red_flags"]
        and int(analysis.get("price") or 0) <= FILTERS["max_price"]
        and int(analysis.get("market_price") or 0) > int(analysis.get("price") or 0)
        and int(analysis.get("market_samples") or 0) >= FILTERS["market_min_samples"]
        and float(analysis.get("discount_pct") or 0) >= FILTERS["min_market_discount_pct"]
    ]
    good_analyses.sort(key=lambda analysis: -float(analysis["discount_pct"]))
    top = good_analyses[:SCRAPING["top_count"]]

    estimated_count = sum(analysis["market_price"] is not None for analysis in analyses)
    print(f"Оценено по аналогам: {estimated_count} из {len(analyses)}")
    print(f"Ниже рынка на заданный порог: {len(good_analyses)}")
    print(f"Показано топ-{len(top)}")

    for analysis in top:
        sid = str(analysis.get("source_id") or "")
        if sid:
            seen_ids.add(sid)

    return top


async def send_daily_report(report_day, *, console=False):
    if daily_report_was_sent(report_day):
        return False

    candidates = load_daily_report_candidates(report_day)
    candidates.sort(key=lambda analysis: -float(analysis.get("discount_pct") or 0))
    candidates = candidates[:SCRAPING["top_count"]]
    text = format_report(candidates)
    if console:
        print("\n" + "=" * 60)
        print(text)
        print("=" * 60)
        return False

    delivered = await send_telegram_message(text)
    if delivered:
        mark_listings_sent(candidates)
        mark_daily_report_sent(report_day)
    return delivered


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
            delivered = await send_telegram_message(format_report(top))
            if delivered:
                mark_listings_sent(top)
        return

    run_hours = _run_hours(SCHEDULE)
    seen_ids = set()
    print(
        f"Сбор: {', '.join(f'{h}:00' for h in run_hours)}; "
        f"один Telegram-отчёт в {run_hours[-1]:02d}:00; "
        f"тишина с {SCHEDULE['quiet_start_hour']}:00 до {SCHEDULE['first_run_hour']}:00"
    )

    while True:
        now = now_moscow()
        delay = _seconds_until_next_run(now, run_hours, SCHEDULE["first_run_hour"])
        print(f"\n⏳ Следующий запуск через ~{int(delay // 60)} мин...")
        await asyncio.sleep(delay)

        now = now_moscow()
        if now.hour == SCHEDULE["first_run_hour"]:
            seen_ids.clear()
            collection_hours = SCHEDULE["morning_lookback_hours"]
            archive_lookback_hours = SCHEDULE["archive_lookback_hours"]
            print(f"🌅 Утренний прогон: архив за {archive_lookback_hours} часов")
        else:
            collection_hours = SCHEDULE["regular_lookback_hours"]
            archive_lookback_hours = None

        try:
            top = await run_parser(
                collection_hours=collection_hours,
                seen_ids=seen_ids,
                archive_lookback_hours=archive_lookback_hours,
            )
            report_day = now.date().isoformat()
            save_daily_report_candidates(report_day, top)
            if now.hour == run_hours[-1]:
                delivered = await send_daily_report(report_day, console=args.console)
                if not delivered and not args.console:
                    print("Ежедневный Telegram-отчёт не отправлен; проверяю Telegram-настройки и доступность.")
            else:
                print(
                    f"В дневную сводку добавлено подтверждённых предложений: {len(top)}; "
                    f"отчёт будет отправлен в {run_hours[-1]:02d}:00."
                )
        except KeyboardInterrupt:
            print("\nОстановка парсера.")
            sys.exit(0)
        except Exception as e:
            print(f"\nОшибка в цикле: {e}")
            print("Следующая попытка по расписанию...")


if __name__ == "__main__":
    asyncio.run(main())
