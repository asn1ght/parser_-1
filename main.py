import asyncio
import argparse
import time
import sys

from config import SCRAPING
from analyzer import analyze_car
from market_context import fetch_market_context
from notifier import format_report, send_telegram_message
from parsers.avito import parse_avito
from parsers.autoru import parse_autoru
from parsers.drom import parse_drom
from quality_filters import is_acceptable_private_car


async def run_parser():
    """Запускает парсинг всех площадок и возвращает топ-объявлений с анализом."""
    print("🚀 Запуск парсера...")
    all_ads = []

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

    return top


async def main():
    parser = argparse.ArgumentParser(description="Парсер авто для перекупа")
    parser.add_argument("--console", action="store_true", help="Вывод в консоль вместо Telegram")
    parser.add_argument("--loop", action="store_true", help="Циклический режим")
    parser.add_argument("--interval", type=int, default=None, help="Интервал в часах (для --loop)")
    parser.add_argument("--interval-minutes", type=int, default=5, help="Интервал в минутах (для --loop; по умолчанию 5)")
    args = parser.parse_args()

    if args.loop:
        while True:
            try:
                top = await run_parser()
                if args.console:
                    print("\n" + "=" * 60)
                    print(format_report(top))
                    print("=" * 60)
                else:
                    await send_telegram_message(format_report(top))

                if args.interval is not None:
                    interval_seconds = args.interval * 3600
                    interval_label = f"{args.interval} часа(ов)"
                else:
                    interval_seconds = args.interval_minutes * 60
                    interval_label = f"{args.interval_minutes} минута(ы)"
                print(f"\n⏳ Следующий запуск через {interval_label}...")
                await asyncio.sleep(interval_seconds)
            except KeyboardInterrupt:
                print("\nОстановка парсера.")
                sys.exit(0)
            except Exception as e:
                print(f"\nОшибка в цикле: {e}")
                print(f"Повтор через 5 минут...")
                await asyncio.sleep(300)
    else:
        top = await run_parser()
        if args.console:
            print("\n" + "=" * 60)
            print(format_report(top))
            print("=" * 60)
        else:
            await send_telegram_message(format_report(top))


if __name__ == "__main__":
    asyncio.run(main())
