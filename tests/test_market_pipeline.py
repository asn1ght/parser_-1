import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import main
import market_data
import market_context
import notifier
from analyzer import analyze_car
from config import now_moscow
from market_context import _to_comparable, evaluate_market
from parsers.avito import _normalize_ad


class UnconfirmedOfferTests(unittest.IsolatedAsyncioTestCase):
    async def test_unvalued_listing_is_returned_with_unconfirmed_status(self):
        ad = {
            "brand": "Skoda",
            "model": "Octavia",
            "year": 2014,
            "price": 850000,
            "mileage": 150000,
            "description": "Я собственник, продаю свой автомобиль.",
            "url": "https://example.test/octavia",
            "source_id": "octavia-1",
            "site": "avito",
        }
        unvalued_analysis = {
            "brand": "Skoda",
            "model": "Octavia",
            "year": 2014,
            "price": 850000,
            "mileage": 150000,
            "url": ad["url"],
            "source_id": ad["source_id"],
            "site": "avito",
            "market_price": None,
            "market_samples": 0,
            "discount_pct": None,
            "discount_amount": 0,
            "profit": 0,
            "red_flags": [],
            "is_below_market": False,
        }

        with (
            patch.object(main, "parse_avito", new=AsyncMock(return_value=[ad])),
            patch.object(main, "parse_autoru", new=AsyncMock(return_value=[])),
            patch.object(main, "parse_drom", new=AsyncMock(return_value=[])),
            patch.object(main, "load_listing_catalog", return_value=[]),
            patch.object(main, "filter_previously_sent_ads", side_effect=lambda ads: ads),
            patch.object(main, "fetch_market_context", new=AsyncMock(return_value=[])),
            patch.object(main, "analyze_car", return_value=unvalued_analysis),
        ):
            result = await main.run_parser()

        self.assertEqual(len(result), 1)
        self.assertIsNone(result[0]["market_price"])
        self.assertFalse(result[0]["market_confirmed"])
        self.assertEqual(result[0]["priority"], "⚪ РЫНОК НЕ ПОДТВЕРЖДЁН")

    async def test_saved_listing_is_re_evaluated_when_sources_return_no_new_ads(self):
        candidate = {
            "brand": "Skoda",
            "model": "Octavia",
            "generation": "III",
            "engine": "Бензин",
            "engine_volume": "1.8",
            "transmission": "АКПП",
            "body": "Седан",
            "year": 2014,
            "price": 800000,
            "mileage": 150000,
            "description": "Я собственник, продаю свой автомобиль.",
            "url": "https://example.test/saved-octavia",
            "source_id": "saved-octavia-1",
            "site": "avito",
        }
        comparables = ComparableQualityTests.clean_comparables()
        with (
            patch.object(main, "parse_avito", new=AsyncMock(return_value=[])),
            patch.object(main, "parse_autoru", new=AsyncMock(return_value=[])),
            patch.object(main, "parse_drom", new=AsyncMock(return_value=[])),
            patch.object(main, "load_listing_catalog", return_value=[candidate]),
            patch.object(main, "filter_previously_sent_ads", side_effect=lambda ads: ads),
            patch.object(main, "fetch_market_context", new=AsyncMock(return_value=comparables)),
        ):
            result = await main.run_parser()

        self.assertEqual([item["source_id"] for item in result], [candidate["source_id"]])
        self.assertEqual(result[0]["market_samples"], 5)
        self.assertTrue(result[0]["is_below_market"])


class ComparableQualityTests(unittest.TestCase):
    @staticmethod
    def candidate(price=800000):
        return {
            "brand": "Skoda",
            "model": "Octavia",
            "generation": "III",
            "engine": "Бензин",
            "engine_volume": "1.8",
            "transmission": "АКПП",
            "body": "Седан",
            "year": 2014,
            "mileage": 150000,
            "price": price,
            "site": "avito",
            "source_id": "candidate-1",
            "description": "Я собственник, продаю свой автомобиль.",
        }

    @staticmethod
    def clean_comparables(count=5):
        return [
            {
                "brand": "Skoda",
                "model": "Octavia",
                "generation": "III",
                "year": 2014,
                "mileage": 145000 + index * 2500,
                "price": 1000000 + index * 100000,
                "engine": "Бензин",
                "engine_volume": "1.8",
                "transmission": "АКПП",
                "seller_type": "private",
                "description": "Я собственник, продаю свой автомобиль.",
            }
            for index in range(count)
        ]

    def test_dealer_and_total_loss_listings_do_not_change_market_median(self):
        candidate = self.candidate(price=1000000)
        clean_comparables = self.clean_comparables(count=3)
        comparables = clean_comparables + [
            {
                **clean_comparables[0],
                "price": 400000,
                "seller_type": "dealer",
                "description": "Автосалон, автомобиль после тотала.",
            },
            {
                **clean_comparables[1],
                "price": 500000,
                "description": "Я собственник, автомобиль не на ходу после тотала.",
            },
        ]

        result = evaluate_market(candidate, comparables, min_samples=3)

        self.assertEqual(result["sample_count"], 3)
        self.assertEqual(result["market_price"], 1100000)

    def test_generation_engine_and_mileage_mismatch_do_not_change_median(self):
        candidate = self.candidate()
        comparables = self.clean_comparables()
        comparables.extend(
            [
                {
                    **comparables[0],
                    "price": 100000,
                    "generation": "II",
                },
                {
                    **comparables[0],
                    "price": 200000,
                    "engine_volume": "1.4",
                },
                {
                    **comparables[0],
                    "price": 300000,
                    "mileage": 400000,
                },
            ]
        )

        result = evaluate_market(candidate, comparables)

        self.assertEqual(result["sample_count"], 5)
        self.assertEqual(result["market_price"], 1200000)

    def test_older_range_mileage_tolerance_is_used_only_when_tagged(self):
        candidate = self.candidate()
        comparables = [
            {
                **item,
                "mileage": 210000,
                "mileage_tolerance_pct": 45,
            }
            for item in self.clean_comparables(count=3)
        ]

        result = evaluate_market(candidate, comparables)
        strict_comparables = [
            {**item, "mileage_tolerance_pct": 35}
            for item in comparables
        ]
        strict_result = evaluate_market(candidate, strict_comparables)

        self.assertEqual(result["sample_count"], 3)
        self.assertIsNotNone(result["market_price"])
        self.assertIsNone(strict_result["market_price"])

    def test_expanded_year_tolerance_is_applied_and_reported(self):
        candidate = self.candidate()
        comparables = [
            {
                **item,
                "year": 2012,
                "year_tolerance": 2,
            }
            for item in self.clean_comparables()
        ]

        result = evaluate_market(candidate, comparables)

        self.assertEqual(result["sample_count"], 5)
        self.assertEqual(result["year_range"], "2012-2012")

    def test_insufficient_quality_comparables_are_not_an_estimate(self):
        result = evaluate_market(self.candidate(), self.clean_comparables(count=2))

        self.assertIsNone(result["market_price"])
        self.assertEqual(result["sample_count"], 2)

    def test_clear_price_outlier_is_excluded_from_median_and_sample_count(self):
        comparables = self.clean_comparables()
        comparables.append({**comparables[0], "source_id": "outlier", "price": 5000000})

        result = evaluate_market(self.candidate(), comparables)

        self.assertEqual(result["sample_count"], 5)
        self.assertEqual(result["market_price"], 1200000)

    def test_only_discounted_candidate_under_price_cap_is_below_market(self):
        candidate = self.candidate()
        result = analyze_car(candidate, self.clean_comparables())

        self.assertTrue(result["is_below_market"])
        self.assertEqual(result["market_price"], 1200000)
        self.assertEqual(result["market_samples"], 5)

    def test_three_quality_comparables_can_confirm_market_price(self):
        result = analyze_car(self.candidate(), self.clean_comparables(count=3))

        self.assertEqual(result["market_samples"], 3)
        self.assertIsNotNone(result["market_price"])
        self.assertTrue(result["is_below_market"])

    def test_market_price_or_higher_is_not_below_market(self):
        comparables = self.clean_comparables()
        market_price = 1200000
        for price in (market_price, market_price + 1):
            with self.subTest(price=price):
                result = analyze_car(self.candidate(price=price), comparables)
                self.assertFalse(result["is_below_market"])

    def test_candidate_over_one_million_is_not_confirmable(self):
        result = analyze_car(self.candidate(price=1000001), self.clean_comparables())

        self.assertFalse(result["is_below_market"])

    def test_credit_only_price_is_rejected_but_negated_credit_is_not(self):
        conditional = self.candidate()
        conditional["description"] = "Цена действует только при покупке в кредит. Я собственник."
        unconditional = self.candidate()
        unconditional["description"] = "Я собственник, кредит не нужен, продаю автомобиль."

        conditional_result = analyze_car(conditional, self.clean_comparables())
        unconditional_result = analyze_car(unconditional, self.clean_comparables())

        self.assertTrue(conditional_result["red_flags"])
        self.assertFalse(unconditional_result["red_flags"])

    def test_avito_params_preserve_generation_and_powertrain(self):
        raw = {
            "title": "Skoda Octavia, 2014",
            "marka": "Skoda",
            "model": "Octavia",
            "year": 2014,
            "price": 900000,
            "params": [
                {"name": "Поколение", "value": "III"},
                {"name": "Тип двигателя", "value": "Бензин"},
                {"name": "Объём двигателя, л", "value": "1.8"},
                {"name": "Коробка передач", "value": "АКПП"},
                {"name": "Состояние", "value": "Не битый"},
            ],
        }

        candidate = _normalize_ad(raw)
        comparable = _to_comparable(raw, "avito")

        for listing in (candidate, comparable):
            self.assertEqual(listing["generation"], "III")
            self.assertEqual(listing["engine"], "Бензин")
            self.assertEqual(listing["engine_volume"], "1.8")
            self.assertEqual(listing["transmission"], "АКПП")


class PersistentCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = market_data.DB_PATH
        market_data.DB_PATH = Path(self.temp_dir.name) / "test-market.sqlite3"

    def tearDown(self):
        market_data.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    @staticmethod
    def listing():
        return {
            "site": "avito",
            "source_id": "listing-1",
            "brand": "Skoda",
            "model": "Octavia",
            "year": 2014,
            "price": 800000,
            "mileage": 150000,
            "description": "Я собственник, продаю свой автомобиль.",
        }

    def test_saved_listing_can_be_loaded_for_re_evaluation_and_sent_one_is_skipped(self):
        listing = self.listing()
        market_data.save_market_observations([listing])

        self.assertEqual(market_data.load_listing_catalog(), [listing])

        market_data.mark_listings_sent([listing])

        self.assertEqual(market_data.load_listing_catalog(), [])
        self.assertEqual(market_data.filter_previously_sent_ads([listing]), [])

    def test_daily_report_deduplicates_listing_and_delivery(self):
        candidate = {
            **self.listing(),
            "market_price": 1200000,
            "market_samples": 5,
            "discount_pct": 33.3,
            "is_below_market": True,
            "red_flags": [],
        }
        report_day = "2026-10-10"

        market_data.save_daily_report_candidates(report_day, [candidate, candidate])
        market_data.save_daily_report_candidates(report_day, [candidate])

        self.assertEqual(market_data.load_daily_report_candidates(report_day), [candidate])
        self.assertFalse(market_data.daily_report_was_sent(report_day))
        market_data.mark_daily_report_sent(report_day)
        market_data.mark_daily_report_sent(report_day)
        self.assertTrue(market_data.daily_report_was_sent(report_day))

    def test_sent_listing_cannot_reenter_the_daily_report(self):
        candidate = {
            **self.listing(),
            "market_price": 1200000,
            "market_samples": 5,
            "discount_pct": 33.3,
            "is_below_market": True,
            "red_flags": [],
        }
        market_data.save_market_observations([candidate])
        market_data.mark_listings_sent([candidate])

        self.assertEqual(market_data.filter_previously_sent_ads([candidate]), [])
        self.assertEqual(market_data.save_daily_report_candidates("2026-10-10", [candidate]), 0)
        self.assertEqual(market_data.load_daily_report_candidates("2026-10-10"), [])

    def test_saved_market_listings_are_reused_only_for_matching_generation(self):
        comparables = [
            {
                **self.listing(),
                "source_id": f"comparable-{index}",
                "price": 1000000 + index * 100000,
                "generation": "III",
                "engine": "Бензин",
                "engine_volume": "1.8",
                "transmission": "АКПП",
            }
            for index in range(5)
        ]
        for comparable in comparables:
            market_data.save_quality_check(
                comparable["source_id"], True, comparable["description"]
            )
        market_data.save_market_observations(comparables)
        candidate = {
            **self.listing(),
            "source_id": "candidate-1",
            "generation": "III",
            "engine": "Бензин",
            "engine_volume": "1.8",
            "transmission": "АКПП",
        }

        estimate = market_data.estimate_market_price(candidate, min_samples=5)
        mismatched = market_data.estimate_market_price(
            {**candidate, "generation": "II"}, min_samples=5
        )

        self.assertEqual(estimate["market_price"], 1200000)
        self.assertEqual(estimate["sample_count"], 5)
        self.assertIsNone(mismatched)

    def test_sqlite_market_prefers_candidate_site_then_uses_other_sites_if_short(self):
        def stored(site, source_id, price, brand="Skoda", model="Octavia"):
            return {
                **self.listing(),
                "site": site,
                "source_id": source_id,
                "brand": brand,
                "model": model,
                "price": price,
                "generation": "III",
                "engine": "Бензин",
                "engine_volume": "1.8",
                "transmission": "АКПП",
            }

        same_site = [
            stored("avito", f"avito-{index}", 1000000 + index * 100000)
            for index in range(5)
        ]
        other_site = [stored("autoru", f"autoru-{index}", 500000) for index in range(5)]
        all_listings = same_site + other_site
        for item in all_listings:
            market_data.save_quality_check(item["source_id"], True, item["description"])
        market_data.save_market_observations(all_listings)
        candidate = {
            **self.listing(),
            "generation": "III",
            "engine": "Бензин",
            "engine_volume": "1.8",
            "transmission": "АКПП",
        }

        preferred = market_data.estimate_market_price(candidate, min_samples=5)
        short_same_site = [
            stored("avito", "one-avito", 1500000, "Ford", "Focus"),
            stored("avito", "two-avito", 1600000, "Ford", "Focus"),
            *[
                stored("autoru", f"fallback-{index}", 500000, "Ford", "Focus")
                for index in range(3)
            ],
        ]
        for item in short_same_site:
            market_data.save_quality_check(item["source_id"], True, item["description"])
        market_data.save_market_observations(short_same_site)
        fallback_candidate = {
            **candidate,
            "brand": "Ford",
            "model": "Focus",
            "source_id": "fallback-candidate",
        }
        fallback = market_data.estimate_market_price(fallback_candidate, min_samples=5)

        self.assertEqual(preferred["market_sites"], ["avito"])
        self.assertEqual(preferred["market_price"], 1200000)
        self.assertEqual(set(fallback["market_sites"]), {"avito", "autoru"})


class DailyReportTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_daily_report_is_sent_once(self):
        report_day = "2026-10-10"
        with (
            patch.object(main, "daily_report_was_sent", side_effect=(False, True)),
            patch.object(main, "load_daily_report_candidates", return_value=[]),
            patch.object(main, "send_telegram_message", new=AsyncMock(return_value=True)) as send,
            patch.object(main, "mark_daily_report_sent") as mark_report,
            patch.object(main, "mark_listings_sent") as mark_listings,
        ):
            delivered = await main.send_daily_report(report_day)
            repeated = await main.send_daily_report(report_day)

        self.assertTrue(delivered)
        self.assertFalse(repeated)
        send.assert_awaited_once()
        self.assertIn("нет объявлений", send.await_args.args[0])
        mark_report.assert_called_once_with(report_day)
        mark_listings.assert_called_once_with([])


class TelegramDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_sender_reports_success_without_real_telegram_call(self):
        with (
            patch.object(notifier, "TELEGRAM_TOKEN", "test-token"),
            patch.object(notifier, "TELEGRAM_CHAT_IDS", ["test-chat"]),
            patch.object(notifier, "Bot") as bot_class,
            patch.object(notifier.asyncio, "sleep", new=AsyncMock()),
        ):
            bot_class.return_value.send_message = AsyncMock()

            delivered = await notifier.send_telegram_message("test message")

        self.assertTrue(delivered)
        bot_class.return_value.send_message.assert_awaited_once()


class ConfirmedReportFormattingTests(unittest.TestCase):
    def test_report_contains_price_discount_and_comparability_only(self):
        analysis = {
            "brand": "Skoda",
            "model": "Octavia",
            "generation": "III",
            "engine": "Бензин",
            "engine_volume": "1.8",
            "transmission": "АКПП",
            "year": 2014,
            "mileage": 150000,
            "price": 800000,
            "market_price": 1200000,
            "market_samples": 5,
            "market_year_range": "2014-2014",
            "market_sites": ["avito"],
            "body": "Седан",
            "market_mileage_matched": True,
            "discount_pct": 33.3,
            "discount_amount": 400000,
            "profit": 100000,
            "red_flags": [],
            "priority": "🔥 НИЖЕ РЫНКА",
            "recommendation": "Подтверждено аналогами.",
            "site": "avito",
            "url": "https://example.test/skoda",
        }

        text = notifier.format_report([analysis])

        self.assertIn("Цена: 800 000 руб", text)
        self.assertIn("медиана 1 200 000 руб по 5 аналогам", text)
        self.assertIn("Ниже рынка: 33.3% (400 000 руб)", text)
        self.assertIn("поколение III", text)
        self.assertIn("кузов Седан", text)
        self.assertIn("двигатель Бензин 1.8", text)
        self.assertIn("КПП АКПП", text)
        self.assertIn("Источники аналогов: Avito", text)
        self.assertIn("https://example.test/skoda", text)
        self.assertNotIn("Оценка после расходов", text)
        self.assertNotIn("Дешевле в глобальном поиске", text)


class ScheduleTests(unittest.TestCase):
    def test_three_hour_schedule_runs_only_between_eight_and_twenty(self):
        schedule = {
            "first_run_hour": 8,
            "run_interval_hours": 3,
            "quiet_start_hour": 23,
        }

        self.assertEqual(main._run_hours(schedule), [8, 11, 14, 17, 20])

    def test_schedule_clock_is_moscow_utc_plus_three(self):
        expected = (datetime.now(timezone.utc) + timedelta(hours=3)).replace(tzinfo=None)

        self.assertLess(abs((now_moscow() - expected).total_seconds()), 2)


class MarketContextSearchTests(unittest.IsolatedAsyncioTestCase):
    def test_fetch_sync_applies_mileage_tier_for_each_documented_source(self):
        candidate = {
            "site": "avito",
            "source_id": "candidate",
            "brand": "Skoda",
            "model": "Octavia",
            "generation": "III",
            "engine": "Бензин",
            "engine_volume": "1.8",
            "transmission": "АКПП",
            "body": "Седан",
            "year": 2014,
            "mileage": 150000,
        }
        raw = {
            "Id": "comparable",
            "url": "https://example.test/comparable",
            "marka": "Skoda",
            "model": "Octavia",
            "model_2": "III",
            "year": 2014,
            "price": 1200000,
            "run": 210000,
            "engine": "Бензин",
            "enginevol": "1.8",
            "transmission": "АКПП",
            "body": "Седан",
            "seller": "Иван Иванов",
            "info": "Я собственник, продаю свой автомобиль.",
        }
        expected_urls = {
            "avito": market_context.AVITO_URL,
            "autoru": market_context.AUTORU_URL,
            "drom": market_context.DROM_URL,
        }

        for site, url in expected_urls.items():
            with self.subTest(site=site), patch.object(market_context, "_fetch", return_value=[raw]) as fetch:
                comparables, returned_count, _ = market_context._fetch_sync(
                    site,
                    "Skoda",
                    "Octavia",
                    2014,
                    30 * 24,
                    "candidate",
                    candidate,
                    year_tolerance=1,
                    mileage_tolerance_pct=45,
                )

            self.assertEqual(fetch.call_args.args[0], url)
            self.assertEqual(returned_count, 1)
            self.assertEqual(comparables[0]["mileage_tolerance_pct"], 45)
            self.assertEqual(comparables[0]["year_tolerance"], 1)

    def test_cache_key_separates_generation_and_powertrain(self):
        base = {
            "site": "avito",
            "brand": "Skoda",
            "model": "Octavia",
            "year": 2014,
            "generation": "III",
            "engine": "Бензин",
            "engine_volume": "1.8",
            "transmission": "АКПП",
            "body": "Седан",
        }

        self.assertNotEqual(
            market_context._cache_key(base),
            market_context._cache_key({**base, "generation": "II"}),
        )
        self.assertNotEqual(
            market_context._cache_key(base),
            market_context._cache_key({**base, "transmission": "МКПП"}),
        )

    async def test_search_expands_date_and_year_only_when_initial_sample_is_small(self):
        candidate = {
            "site": "avito",
            "source_id": "candidate",
            "brand": "Skoda",
            "model": "Octavia",
            "year": 2014,
            "mileage": 150000,
        }

        def comparable(index):
            return {
                "site": "avito",
                "source_id": f"comparable-{index}",
                "url": f"https://example.test/{index}",
                "brand": "Skoda",
                "model": "Octavia",
                "year": 2014,
                "mileage": 150000,
                "price": 1200000 + index * 10000,
                "seller_type": "private",
                "description": "Я собственник, продаю свой автомобиль.",
            }

        disk_cache = {}
        with (
            patch.dict(market_context._CACHE, {}, clear=True),
            patch.object(market_context, "_SEARCH_STATE", {}),
            patch.object(market_context, "_SEARCHED_KEYS_THIS_RUN", set()),
            patch.object(market_context, "_load_disk_cache", side_effect=lambda: dict(disk_cache)),
            patch.object(
                market_context,
                "_save_disk_cache",
                side_effect=lambda cache: disk_cache.update(cache),
            ),
            patch.object(market_context.asyncio, "sleep", new=AsyncMock()),
            patch.object(
                market_context,
                "_fetch_sync",
                side_effect=[([comparable(1), comparable(2)], 2, 100),
                             ([comparable(3), comparable(4), comparable(5)], 3, 100)],
            ) as fetch,
        ):
            market_context.begin_market_context_run()
            first_pass = await market_context.fetch_market_context(candidate)
            repeated_same_pass = await market_context.fetch_market_context(candidate)
            market_context._CACHE.clear()
            market_context._SEARCH_STATE.clear()
            market_context.begin_market_context_run()
            result = await market_context.fetch_market_context(candidate)

        self.assertEqual(len(first_pass), 2)
        self.assertEqual(len(repeated_same_pass), 2)
        self.assertEqual(len(result), 5)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(fetch.call_args_list[0].kwargs["year_tolerance"], 1)
        self.assertEqual(fetch.call_args_list[1].kwargs["year_tolerance"], 1)
        self.assertEqual(fetch.call_args_list[0].kwargs["mileage_tolerance_pct"], 35)
        self.assertEqual(fetch.call_args_list[1].kwargs["mileage_tolerance_pct"], 45)
        self.assertEqual(fetch.call_args_list[0].kwargs["end_hours"], 0)
        self.assertEqual(fetch.call_args_list[1].kwargs["end_hours"], 30 * 24)

    async def test_context_cap_is_distributed_across_six_models(self):
        candidates = [
            {
                "site": "autoru",
                "source_id": f"candidate-{index}",
                "brand": f"Brand{index}",
                "model": f"Model{index}",
                "year": 2015,
                "mileage": 100000,
                "price": 700000,
            }
            for index in range(7)
        ]
        with (
            patch.dict(market_context._CACHE, {}, clear=True),
            patch.object(market_context, "_SEARCH_STATE", {}),
            patch.object(market_context, "_SEARCHED_KEYS_THIS_RUN", set()),
            patch.object(market_context, "_load_disk_cache", return_value={}),
            patch.object(market_context, "_save_disk_cache"),
            patch.object(market_context.asyncio, "sleep", new=AsyncMock()),
            patch.object(market_context, "_fetch_sync", return_value=([], 0, 100)) as fetch,
        ):
            market_context.begin_market_context_run()
            for candidate in candidates:
                await market_context.fetch_market_context(candidate)

        self.assertEqual(fetch.call_count, 6)

    async def test_search_expands_to_ninety_days_with_generation_for_year_tolerance(self):
        candidate = {
            "site": "avito",
            "source_id": "candidate",
            "brand": "Skoda",
            "model": "Octavia",
            "generation": "III",
            "year": 2014,
            "mileage": 150000,
            "price": 800000,
        }

        def page(start, count):
            return [
                {
                    "site": "avito",
                    "source_id": f"page-{start}-{index}",
                    "url": f"https://example.test/page-{start}-{index}",
                    "brand": "Skoda",
                    "model": "Octavia",
                    "generation": "III",
                    "year": 2014,
                    "mileage": 150000,
                    "price": 1200000 + index * 10000,
                    "seller_type": "private",
                    "description": "Я собственник, продаю свой автомобиль.",
                }
                for index in range(count)
            ]

        with (
            patch.dict(market_context._CACHE, {}, clear=True),
            patch.object(market_context, "_SEARCH_STATE", {}),
            patch.object(market_context, "_SEARCHED_KEYS_THIS_RUN", set()),
            patch.object(market_context, "_load_disk_cache", return_value={}),
            patch.object(market_context, "_save_disk_cache"),
            patch.object(market_context.asyncio, "sleep", new=AsyncMock()),
            patch.object(
                market_context,
                "_fetch_sync",
                side_effect=[(page("30", 2), 2, 100),
                             (page("60", 0), 0, 100),
                             (page("90", 5), 5, 100)],
            ) as fetch,
        ):
            market_context.begin_market_context_run()
            first_pass = await market_context.fetch_market_context(candidate)
            market_context.begin_market_context_run()
            second_pass = await market_context.fetch_market_context(candidate)
            market_context.begin_market_context_run()
            result = await market_context.fetch_market_context(candidate)

        self.assertEqual(len(first_pass), 2)
        self.assertEqual(len(second_pass), 2)
        self.assertEqual(len(result), 7)
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(fetch.call_args_list[2].kwargs["end_hours"], 60 * 24)
        self.assertEqual(fetch.call_args_list[2].kwargs["year_tolerance"], 2)
        self.assertEqual(fetch.call_args_list[2].kwargs["mileage_tolerance_pct"], 50)


if __name__ == "__main__":
    unittest.main()
