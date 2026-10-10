import asyncio
import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median

import requests

from config import AVITO_API_LOGIN, AVITO_API_TOKEN, FILTERS, SCRAPING, now_moscow
from market_data import exclude_price_outliers
from quality_filters import is_acceptable_private_car, rejection_reasons


CACHE_FILE = Path(os.getenv("MARKET_CACHE_PATH", str(Path(__file__).resolve().parent / ".market_context_cache.json")))
CACHE_TTL_HOURS = float(os.getenv("MARKET_CONTEXT_TTL_HOURS", "24"))
_QUOTA_EXHAUSTED = False
_SEARCHES_THIS_RUN = 0
_SEARCH_STATE = {}
_SEARCHED_KEYS_THIS_RUN = set()
_LAST_CONTEXT_REQUEST_AT = None


class QuotaExhausted(RuntimeError):
    pass


AVITO_URL = "https://rest-app.net/api/ads"
AUTORU_URL = "https://rest-app.net/api-auto-ru/ads"
DROM_URL = "https://rest-app.net/api-drom-ru/ads"


def begin_market_context_run():
    global _SEARCHES_THIS_RUN
    _SEARCHES_THIS_RUN = 0
    _SEARCHED_KEYS_THIS_RUN.clear()


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


def _param_value(raw, *terms):
    params = raw.get("params", [])
    if not isinstance(params, list):
        return ""
    for item in params:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").casefold().replace("ё", "е")
        if any(term in name for term in terms):
            return str(item.get("value") or "").strip()
    return ""


def _to_comparable(raw, site):
    seller = str(raw.get("seller") or raw.get("name") or "").strip()
    price_conditions = [
        str(item.get("value") or "")
        for item in raw.get("params", [])
        if isinstance(item, dict)
        and any(
            word in str(item.get("name") or "").casefold()
            for word in ("кредит", "трейд", "услов", "оплат", "цена")
        )
    ]
    return {
        "brand": str(raw.get("marka") or "").strip(),
        "model": str(raw.get("model") or raw.get("model_2") or "").strip(),
        "generation": str(raw.get("generation") or raw.get("model_2") or _param_value(raw, "поколение")).strip(),
        "year": _number(raw.get("year")),
        "price": _number(raw.get("price")),
        "mileage": _number(raw.get("run")),
        "engine": str(raw.get("engine") or _param_value(raw, "тип двигателя", "двигатель")).strip(),
        "engine_volume": str(raw.get("engine_volume") or raw.get("enginevol") or _param_value(raw, "объем двигателя")).strip(),
        "transmission": str(raw.get("transmission") or _param_value(raw, "коробка передач", "трансмиссия")).strip(),
        "body": str(raw.get("body") or _param_value(raw, "тип кузова", "кузов")).strip(),
        "condition": str(raw.get("condition") or _param_value(raw, "состояние")).strip(),
        "description": str(raw.get("info") or raw.get("description") or "").strip(),
        "price_conditions": " ".join(price_conditions),
        "seller": seller,
        "seller_name": seller,
        "seller_type": str(raw.get("seller_type") or "").strip(),
        "url": str(raw.get("url") or ""),
        "source_id": str(raw.get("Id") or raw.get("avito_id") or raw.get("url") or ""),
        "site": site,
    }


def _matches(
    item,
    brand_key,
    model_key,
    year,
    candidate=None,
    year_tolerance=1,
    *,
    check_mileage=True,
):
    if not item["brand"] or not item["model"] or not item["year"]:
        return False
    if _norm_key(item["brand"]) != brand_key or _model_key(item["model"]) != model_key:
        return False
    if not year - year_tolerance <= item["year"] <= year + year_tolerance:
        return False
    if candidate:
        for key in ("generation", "engine", "engine_volume", "transmission", "body"):
            candidate_value = _norm_key(candidate.get(key))
            item_value = _norm_key(item.get(key))
            if candidate_value and item_value and candidate_value != item_value:
                return False
        if check_mileage:
            candidate_mileage = _number(candidate.get("mileage"))
            item_mileage = _number(item.get("mileage"))
            if candidate_mileage <= 0 or item_mileage <= 0:
                return False
            mileage_tolerance_pct = float(item.get("mileage_tolerance_pct", 35))
            if abs(item_mileage - candidate_mileage) > max(
                50000,
                int(candidate_mileage * mileage_tolerance_pct / 100),
            ):
                return False
    return True


def _condition_is_comparable(item):
    condition = " ".join(
        str(item.get(key) or "") for key in ("condition", "description", "info")
    ).casefold().replace("ё", "е")
    return not re.search(
        r"\b(?:не на ходу|под восстановление|восстановлению не подлежит|тотал\w*|"
        r"требует ремонта|сработали подушки|серьезн\w* дтп)\b",
        condition,
    )


def _base_auth():
    return {"login": AVITO_API_LOGIN, "token": AVITO_API_TOKEN, "format": "json"}


def _window(start_hours, end_hours=0):
    now = now_moscow()
    return {
        "date1": (now - timedelta(hours=start_hours)).strftime("%Y-%m-%d %H:%M:%S"),
        "date2": (now - timedelta(hours=end_hours)).strftime("%Y-%m-%d %H:%M:%S"),
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


def _fetch_sync(
    site,
    brand,
    model,
    year,
    history_hours,
    exclude_id,
    candidate=None,
    *,
    end_hours=0,
    year_tolerance=1,
    mileage_tolerance_pct=35,
    offset=0,
    sort="desc",
):
    brand_key = _norm_key(brand)
    model_key = _model_key(model)
    history_hours = max(history_hours, 2)
    limit = min(max(1, FILTERS["market_context_limit"]), 100)
    params = {
        "limit": limit,
        "offset": offset,
        "sort": sort,
        "price2": FILTERS["market_max_price"],
        **_window(history_hours, end_hours),
    }

    if site == "avito":
        url = AVITO_URL
        params.update(
            {
                "category_id": 9,
                "region_id": FILTERS["avito_region_id"],
                "q": f"{brand} {model}".strip(),
            }
        )
    elif site == "autoru":
        url = AUTORU_URL
        params["region_id"] = FILTERS["autoru_api_region_id"]
    elif site == "drom":
        url = DROM_URL
        params["region_id"] = FILTERS["drom_api_region_id"]
    else:
        return [], 0, limit

    rows = _fetch(url, params)
    comparables = []
    vehicle_match_count = 0
    excluded_self_or_price_count = 0
    quality_match_count = 0
    quality_rejections = {}
    condition_rejection_count = 0
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        item = _to_comparable(raw, site)
        if not _matches(
            item,
            brand_key,
            model_key,
            year,
            candidate,
            year_tolerance,
            check_mileage=False,
        ):
            continue
        vehicle_match_count += 1
        if item["price"] <= 0 or item["source_id"] == exclude_id:
            excluded_self_or_price_count += 1
            continue
        reasons = rejection_reasons(item)
        if reasons:
            for reason in reasons:
                quality_rejections[reason] = quality_rejections.get(reason, 0) + 1
            continue
        quality_match_count += 1
        if not _condition_is_comparable(item):
            condition_rejection_count += 1
            continue
        item["year_tolerance"] = year_tolerance
        item["mileage_tolerance_pct"] = mileage_tolerance_pct
        comparables.append(item)
    print(
        f"[Рынок {site}] окно {history_hours // 24}–{end_hours // 24} дн., "
        f"offset={offset}, допуск по году ±{year_tolerance}: API {len(rows)}, "
        f"совместимы авто/год/комплектация {vehicle_match_count}, "
        f"исключены цена=0/сам кандидат {excluded_self_or_price_count}, "
        f"прошли продавца/цену {quality_match_count}, "
        f"отказы {quality_rejections}, не прошли состояние {condition_rejection_count}, "
        f"осталось {len(comparables)}.",
        flush=True,
    )
    return comparables, len(rows), limit


_CACHE = {}


def _cache_key(ad):
    return json.dumps(
        [
            "quality-v5",
            ad.get("site"),
            _norm_key(ad.get("brand")),
            _model_key(ad.get("model")),
            int(ad.get("year") or 0),
            _norm_key(ad.get("generation")),
            _norm_key(ad.get("engine")),
            _norm_key(ad.get("engine_volume")),
            _norm_key(ad.get("transmission")),
            _norm_key(ad.get("body")),
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


def _has_sufficient_market_sample(ad, items):
    return evaluate_market(ad, items).get("market_price") is not None


def _context_ranges(ad):
    initial_days = min(30, max(1, int(FILTERS["market_history_days"])))
    ranges = [(initial_days, 0, 1, 35)]
    if initial_days < 60:
        ranges.append((60, initial_days, 1, 45))
    if initial_days < 90:
        ranges.append((90, 60, 2 if ad.get("generation") else 1, 50))
    return ranges


async def fetch_market_context(ad):
    global _QUOTA_EXHAUSTED, _SEARCHES_THIS_RUN, _LAST_CONTEXT_REQUEST_AT
    if _QUOTA_EXHAUSTED:
        print("[Рынок] REST-App сообщил об исчерпании дневной квоты; онлайн-аналогов нет.", flush=True)
        return []

    key = _cache_key(ad)
    exclude_id = str(ad.get("source_id") or ad.get("url") or "")
    ad_url = ad.get("url")
    disk = _load_disk_cache()
    state = _SEARCH_STATE.get(key)
    if state:
        try:
            age = (now_moscow() - datetime.fromisoformat(state["fetched_at"])).total_seconds()
        except (KeyError, ValueError, TypeError):
            age = CACHE_TTL_HOURS * 3600 + 1
        if age >= CACHE_TTL_HOURS * 3600:
            _CACHE.pop(key, None)
            _SEARCH_STATE.pop(key, None)
            state = None

    if key not in _CACHE:
        entry = disk.get(key)
        if entry and "fetched_at" in entry and "items" in entry:
            try:
                age = (now_moscow() - datetime.fromisoformat(entry["fetched_at"])).total_seconds()
            except (ValueError, TypeError):
                age = CACHE_TTL_HOURS * 3600 + 1
            if age < CACHE_TTL_HOURS * 3600:
                _CACHE[key] = entry["items"]
                state = {
                    "range_index": int(entry.get("range_index", 0)),
                    "offset": int(entry.get("offset", 0)),
                    "complete": bool(entry.get("complete", False)),
                    "fetched_at": entry["fetched_at"],
                }
                _SEARCH_STATE[key] = state
            else:
                disk.pop(key, None)
        if key not in _CACHE:
            _CACHE[key] = []
            state = {
                "range_index": 0,
                "offset": 0,
                "complete": False,
                "fetched_at": now_moscow().isoformat(),
            }
            _SEARCH_STATE[key] = state

    items = _CACHE[key]
    estimate = evaluate_market(ad, items)
    state = _SEARCH_STATE[key]
    if (
        estimate["market_price"] is None
        and not state["complete"]
        and key not in _SEARCHED_KEYS_THIS_RUN
    ):
        if _SEARCHES_THIS_RUN >= FILTERS["market_context_max_searches_per_run"]:
            print(
                f"[Рынок {ad.get('brand')} {ad.get('model')} {ad.get('year')}] "
                f"лимит context-запросов {FILTERS['market_context_max_searches_per_run']} достигнут; "
                "поиск продолжится в следующем проходе.",
                flush=True,
            )
        else:
            ranges = _context_ranges(ad)
            range_index = int(state["range_index"])
            offset = int(state["offset"])
            if range_index >= len(ranges):
                state["complete"] = True
            else:
                end_days, start_days, year_tolerance, mileage_tolerance_pct = ranges[range_index]
                if _LAST_CONTEXT_REQUEST_AT is not None:
                    delay = SCRAPING["delay_min"] - (time.monotonic() - _LAST_CONTEXT_REQUEST_AT)
                    if delay > 0:
                        await asyncio.sleep(delay)
                _SEARCHES_THIS_RUN += 1
                _SEARCHED_KEYS_THIS_RUN.add(key)
                _LAST_CONTEXT_REQUEST_AT = time.monotonic()
                try:
                    page_items, returned_count, limit = await asyncio.to_thread(
                        _fetch_sync,
                        ad.get("site"),
                        ad.get("brand"),
                        ad.get("model"),
                        int(ad.get("year") or 0),
                        end_days * 24,
                        exclude_id,
                        ad,
                        end_hours=start_days * 24,
                        year_tolerance=year_tolerance,
                        mileage_tolerance_pct=mileage_tolerance_pct,
                        offset=offset,
                    )
                except QuotaExhausted:
                    _QUOTA_EXHAUSTED = True
                    return []
                found = {
                    str(item.get("source_id") or item.get("url") or ""): item
                    for item in items
                    if isinstance(item, dict) and (item.get("source_id") or item.get("url"))
                }
                for item in page_items:
                    key_id = str(item.get("source_id") or item.get("url") or "")
                    if key_id:
                        found[key_id] = item
                items = list(found.values())
                _CACHE[key] = items
                if returned_count < limit or offset >= limit:
                    state["range_index"] = range_index + 1
                    state["offset"] = 0
                else:
                    state["offset"] = offset + limit
                state["complete"] = state["range_index"] >= len(ranges)
                state["fetched_at"] = now_moscow().isoformat()
                _SEARCH_STATE[key] = state
                disk[key] = {
                    "fetched_at": state["fetched_at"],
                    "items": items,
                    "range_index": state["range_index"],
                    "offset": state["offset"],
                    "complete": state["complete"],
                }
                _save_disk_cache(disk)

    items = _CACHE[key]
    estimate = evaluate_market(ad, items)
    print(
        f"[Рынок {ad.get('brand')} {ad.get('model')} {ad.get('year')}] "
        f"подходящих аналогов {estimate['sample_count']}/{FILTERS['market_min_samples']}; "
        f"курсор {state['range_index']}/{len(_context_ranges(ad))}, offset={state['offset']}; "
        f"оценка {'готова' if estimate['market_price'] is not None else 'не подтверждена'}.",
        flush=True,
    )
    return [
        item for item in items
        if item["source_id"] != exclude_id and item.get("url") != ad_url
    ]


def evaluate_market(ad, comparables, min_samples=None):
    """Return median-based market estimate and cheaper alternatives."""
    min_samples = FILTERS["market_min_samples"] if min_samples is None else min_samples
    price = int(ad.get("price") or 0)
    year = int(ad.get("year") or 0)
    year_range = f"{year - 1}-{year + 1}"
    brand_key = _norm_key(ad.get("brand"))
    model_key = _model_key(ad.get("model"))
    sorted_comparables = sorted(
        (
            item for item in comparables
            if item.get("price", 0) > 0
            and _matches(
                item,
                brand_key,
                model_key,
                year,
                ad,
                int(item.get("year_tolerance", 1)),
            )
            and is_acceptable_private_car(item)
            and _condition_is_comparable(item)
        ),
        key=lambda item: item["price"],
    )
    year_range = _comparable_year_range(sorted_comparables, year_range)
    alternatives = [item for item in sorted_comparables if item["price"] < price]

    mileage = _number(ad.get("mileage"))
    if mileage <= 0:
        mileage_comparables = []
    else:
        mileage_comparables = [
            item for item in sorted_comparables
            if _number(item.get("mileage")) > 0
            and abs(_number(item.get("mileage")) - mileage)
            <= max(50000, int(mileage * float(item.get("mileage_tolerance_pct", 35)) / 100))
        ]
    if len(mileage_comparables) < min_samples:
        return {
            "market_price": None,
            "sample_count": len(mileage_comparables),
            "year_range": year_range,
            "mileage_matched": False,
            "discount_pct": None,
            "alternatives": alternatives,
            "comparables": mileage_comparables,
        }

    sorted_comparables = mileage_comparables
    sorted_comparables = exclude_price_outliers(
        sorted_comparables,
        min_samples=min_samples,
    )
    year_range = _comparable_year_range(sorted_comparables, year_range)
    if len(sorted_comparables) < min_samples:
        return {
            "market_price": None,
            "sample_count": len(sorted_comparables),
            "year_range": year_range,
            "mileage_matched": False,
            "discount_pct": None,
            "alternatives": alternatives,
            "comparables": sorted_comparables,
        }
    alternatives = [item for item in sorted_comparables if item["price"] < price]
    market_price = int(median(item["price"] for item in sorted_comparables))
    discount_pct = round((market_price - price) / market_price * 100, 1) if market_price else 0
    return {
        "market_price": market_price,
        "sample_count": len(sorted_comparables),
        "year_range": year_range,
        "mileage_matched": True,
        "discount_pct": discount_pct,
        "alternatives": alternatives,
        "comparables": sorted_comparables,
        "market_sites": sorted({item.get("site", "").casefold() for item in sorted_comparables if item.get("site")}),
    }


def _comparable_year_range(comparables, fallback):
    years = [int(item.get("year") or 0) for item in comparables if int(item.get("year") or 0) > 0]
    return f"{min(years)}-{max(years)}" if years else fallback
