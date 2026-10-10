from config import FILTERS, PREP_COSTS, SELLING_COSTS
from market_data import estimate_market_price
from market_context import evaluate_market
from quality_filters import rejection_reasons


def analyze_car(ad, comparables=None):
    brand = ad.get("brand", "")
    model = ad.get("model", "")
    year = ad.get("year", 0)
    price = ad.get("price", 0)
    if price <= 0 or price > FILTERS["max_price"]:
        estimate = None
    elif comparables is not None:
        estimate = evaluate_market(ad, comparables)
    else:
        estimate = estimate_market_price(
            ad,
            history_days=FILTERS["market_history_days"],
            min_samples=FILTERS["market_min_samples"],
        )

    result = {
        "brand": brand,
        "model": model,
        "generation": ad.get("generation", ""),
        "engine": ad.get("engine", ""),
        "engine_volume": ad.get("engine_volume", ""),
        "transmission": ad.get("transmission", ""),
        "body": ad.get("body", ""),
        "year": year,
        "price": price,
        "mileage": ad.get("mileage", 0),
        "url": ad.get("url", ""),
        "site": ad.get("site", ""),
        "source_id": ad.get("source_id", ""),
        "market_price": estimate["market_price"] if estimate else None,
        "market_samples": estimate["sample_count"] if estimate else 0,
        "market_sites": estimate.get("market_sites", []) if estimate else [],
        "market_min_samples": FILTERS["market_min_samples"],
        "market_year_range": estimate["year_range"] if estimate else "",
        "market_mileage_matched": estimate["mileage_matched"] if estimate else False,
        "alternatives": estimate.get("alternatives", []) if estimate else [],
        "comparables": estimate.get("comparables", []) if estimate else [],
        "discount_pct": estimate["discount_pct"] if estimate else None,
        "discount_amount": 0,
        "total_costs": 0,
        "profit": 0,
        "red_flags": [],
        "is_below_market": False,
        "priority": "⚪ НЕДОСТАТОЧНО АНАЛОГОВ",
        "recommendation": "Недостаточно сопоставимых объявлений для оценки рынка.",
    }

    if not estimate or estimate.get("market_price") is None or price <= 0:
        return result

    market_price = estimate["market_price"]
    discount_amount = market_price - price
    prep_total = sum(PREP_COSTS.values())
    selling_total = sum(SELLING_COSTS.values())
    profit = discount_amount - prep_total - selling_total
    red_flags = rejection_reasons(ad, require_private_seller=(ad.get("site") != "drom"))
    is_below_market = estimate["discount_pct"] >= FILTERS["min_market_discount_pct"]

    result.update(
        {
            "discount_amount": discount_amount,
            "total_costs": price + prep_total + selling_total,
            "profit": profit,
            "red_flags": red_flags,
            "is_below_market": is_below_market,
        }
    )

    if red_flags:
        result["priority"] = "❌ РИСК ПО ОПИСАНИЮ"
        result["recommendation"] = "Проверьте объявление: " + ", ".join(red_flags)
    elif is_below_market:
        result["priority"] = "🔥 НИЖЕ РЫНКА"
        result["recommendation"] = "Цена ниже медианы качественных сопоставимых объявлений на установленный порог."
    else:
        result["priority"] = "❌ НЕ НИЖЕ РЫНКА"
        result["recommendation"] = "Скидка меньше минимального порога."

    return result
