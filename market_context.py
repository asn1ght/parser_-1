import asyncio
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median

import requests

from config import AVITO_API_LOGIN, AVITO_API_TOKEN, FILTERS


CACHE_FILE = Path(os.getenv("MARKET_CACHE_PATH", str(Path(__file__).resolve().parent / ".market_context_cache.json")))
CACHE_TTL_HOURS = float(os.getenv("MARKET_CONTEXT_TTL_HOURS", "24"))
_QUOTA_EXHAUSTED = False
_SEARCHES_THIS_RUN = 0


class QuotaExhausted(RuntimeError):
    pass


AVITO_URL = "https://rest-app.net/api/ads"
AUTORU_URL = "https://rest-app.net/api-auto-ru/ads"
DROM_URL = "https://rest-app.net/api-drom-ru/ads"


def begin_market_context_run():
    global _SEARCHES_THIS_RUN
    _SEARCHES_THIS_RUN = 0


def _number(value):
    digits = re.sub(r"\D", "", str(value or ""))
    return int(digits) if digits else 0


def _norm_key(value):
    return re.sub(r"[^a-zа-яё0-9]+", " ", str(value or "").casefold()).strip()


def _model_key(value):
    model = str(value or "").casefold()
    model = re.sub(r"\b\d+[.,]\d+\b.*$", "", model)
    words = _norm_key(model).split()
    while words and words[-1] in {"at", "mt", "cvt", "dsg", "акпп", "мкпп", "автомат", "механика"}:
        words.pop()
    return " ".join(words)


def _to_comparable(raw, site):
    return {
        "brand": str(raw.get("marka") or "").strip(),
        "model": str(raw.get("model") or raw.get("model_2") or "").strip(),
        "year": _number(raw.get("year")),
        "price": _number(raw.get("price")),
        "mileage": _number(raw.get("run")),
        "condition": str(raw.get("condition") or "").strip(),
        "description": str(raw.get("info") or raw.get("description") or "").strip(),
        "url": str(raw.get("url") or ""),
        "source_id": str(raw.get("Id") or raw.get("avito_id") or raw.get("url") or ""),
        "site": site,
    }


def _matches(item, brand_key, model_key, year):
    if not item["brand"] or not item["model"] or not item["year"]:
        return False
    if _norm_key(item["brand"]) != brand_key or _model_key(item["model"]) != model_key:
        return False
    return year - 1 <= item["year"] <= year + 1


def _base_auth():
    return {"login": AVITO_API_LOGIN, "token": AVITO_API_TOKEN, "format": "json"}


def _window(hours):
    now = datetime.now()
    return {
        "date1": (now - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S"),
        "date2": now.strftime("%Y-%m-%d %H:%M:%S"),
    }


def _fetch(url, params):
    global _QUOTA_EXHAUSTED
    response = requests.get(url, params={**_base_auth(), **params}, timeout=40)
    if response.status_code >= 400:
        raise RuntimeError(f"market-context HTTP {response.status_code}: {response.text[:200]}")
    payload = response.json()
    if payload.get("status") != "ok":
        message = str(payload.get("message") or "unknown")
        if "daily limit" in message.casefold() or "лимит" in message.casefold():
            _QUOTA_EXHAUSTED = True
            raise QuotaExhausted(f"market-context daily limit exhausted: {message}")
        raise RuntimeError(f"market-context error: {message}")
    return payload.get("data", [])


def _fetch_sync(site, brand, model, year, history_hours, exclude_id):
    brand_key = _norm_key(brand)
    model_key = _model_key(model)
    history_hours = max(history_hours, 2)

    if site == "avito":
        rows = _fetch(
            AVITO_URL,
            {
                "category_id": 9,
                "region_id": FILTERS["avito_region_id"],
                "q": f"{brand} {model}".strip(),
                "limit": min(FILTERS["market_context_limit"], 100),
                "offset": 0,
                **_window(history_hours),
            },
        )
        comparables = [
            item for raw in rows if isinstance(raw, dict)
            for item in (_to_comparable(raw, "avito"),)
            if _matches(item, brand_key, model_key, year)
        ]
    elif site == "autoru":
        rows = _fetch(
            AUTORU_URL,
            {**{"region_id": FILTERS["autoru_api_region_id"], "limit": min(FILTERS["market_context_limit"], 100), "offset": 0}, **_window(history_hours)},
        )
        comparables = [
            item for raw in rows if isinstance(raw, dict)
            for item in (_to_comparable(raw, "autoru"),)
            if _matches(item, brand_key, model_key, year)
        ]
    elif site == "drom":
        rows = _fetch(
            DROM_URL,
            {**{"region_id": FILTERS["drom_api_region_id"], "limit": min(FILTERS["market_context_limit"], 100), "offset": 0}, **_window(history_hours)},
        )
        comparables = [
            item for raw in rows if isinstance(raw, dict)
            for item in (_to_comparable(raw, "drom"),)
            if _matches(item, brand_key, model_key, year)
        ]
    else:
        comparables = []

    return [
        item for item in comparables
        if item["price"] > 0 and item["source_id"] != exclude_id
    ]


_CACHE = {}


def _cache_key(ad):
    return json.dumps(
        [
            ad.get("site"),
            _norm_key(ad.get("brand")),
            _model_key(ad.get("model")),
            int(ad.get("year") or 0),
        ],
        ensure_ascii=False,
    )


def _load_disk_cache():
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_disk_cache(cache):
    try:
        CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


async def fetch_market_context(ad):
    global _QUOTA_EXHAUSTED, _SEARCHES_THIS_RUN
    if _QUOTA_EXHAUSTED:
        return []

    key = _cache_key(ad)
    exclude_id = str(ad.get("source_id") or ad.get("url") or "")
    ad_url = ad.get("url")

    if key not in _CACHE:
        disk = _load_disk_cache()
        entry = disk.get(key)
        if entry and "fetched_at" in entry and "items" in entry:
            try:
                age = (datetime.now() - datetime.fromisoformat(entry["fetched_at"])).total_seconds()
            except (ValueError, TypeError):
                age = CACHE_TTL_HOURS * 3600 + 1
            if age < CACHE_TTL_HOURS * 3600:
                _CACHE[key] = entry["items"]
            else:
                disk.pop(key, None)
                _save_disk_cache(disk)

        if key not in _CACHE:
            if _SEARCHES_THIS_RUN >= FILTERS["market_context_max_searches_per_run"]:
                return []
            history_hours = int(FILTERS["market_history_days"]) * 24
            _SEARCHES_THIS_RUN += 1
            try:
                items = await asyncio.to_thread(
                    _fetch_sync,
                    ad.get("site"),
                    ad.get("brand"),
                    ad.get("model"),
                    int(ad.get("year") or 0),
                    history_hours,
                    exclude_id,
                )
            except QuotaExhausted:
                _QUOTA_EXHAUSTED = True
                return []
            _CACHE[key] = items
            disk[key] = {"fetched_at": datetime.now().isoformat(), "items": items}
            _save_disk_cache(disk)

    items = _CACHE[key]
    return [
        item for item in items
        if item["source_id"] != exclude_id and item.get("url") != ad_url
    ]


def evaluate_market(ad, comparables, min_samples=None):
    """Return median-based market estimate and cheaper alternatives."""
    min_samples = FILTERS["market_min_samples"] if min_samples is None else min_samples
    price = int(ad.get("price") or 0)
    year_range = f"{int(ad.get('year') or 0) - 1}-{int(ad.get('year') or 0) + 1}"

    sorted_comparables = sorted(comparables, key=lambda item: item["price"])
    alternatives = [item for item in sorted_comparables if item["price"] < price]

    if len(comparables) < min_samples:
        return {
            "market_price": None,
            "sample_count": len(comparables),
            "year_range": year_range,
            "mileage_matched": False,
            "discount_pct": None,
            "alternatives": alternatives,
            "comparables": sorted_comparables,
        }

    market_price = int(median(item["price"] for item in comparables))
    discount_pct = round((market_price - price) / market_price * 100, 1) if market_price else 0
    return {
        "market_price": market_price,
        "sample_count": len(comparables),
        "year_range": year_range,
        "mileage_matched": True,
        "discount_pct": discount_pct,
        "alternatives": alternatives,
        "comparables": sorted_comparables,
    }
