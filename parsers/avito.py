import asyncio
import re
from datetime import datetime, timedelta

import requests

from config import AVITO_API_LOGIN, AVITO_API_TOKEN, FILTERS, SCRAPING, now_moscow
from market_data import save_market_observations, save_quality_check
from parsers.rest_app import ensure_real_price_access
from quality_filters import is_acceptable_private_car


API_URL = "https://rest-app.net/api/ads"


def _number(value):
    digits = re.sub(r"\D", "", str(value or ""))
    return int(digits) if digits else 0


def _extract_api_message(response):
    try:
        payload = response.json()
    except ValueError:
        return response.text[:200]
    message = str(payload.get("message") or payload.get("error") or response.text[:200])
    for secret in (AVITO_API_LOGIN, AVITO_API_TOKEN):
        message = message.replace(secret, "<redacted>")
    return message


def _get_mileage(ad):
    title = str(ad.get("title", "")).casefold().replace("\xa0", " ")
    match = re.search(
        r"(\d[\d ]*(?:[-–—]\s*\d[\d ]*)?)\s*(тыс\.?|тысяч(?:а|и)?|км)\b",
        title,
    )
    if match:
        mileage_values = [_number(value) for value in re.findall(r"\d[\d ]*", match.group(1))]
        if mileage_values:
            mileage = max(mileage_values)
            return mileage * 1000 if match.group(2).startswith("тыс") and mileage < 1000 else mileage

    params = ad.get("params", [])
    if isinstance(params, list):
        for item in params:
            if not isinstance(item, dict) or "пробег" not in str(item.get("name", "")).casefold():
                continue
            value = str(item.get("value", "")).casefold().replace("\xa0", " ")
            mileages = [_number(number) for number in re.findall(r"\d[\d ]*", value)]
            if mileages:
                mileage = max(mileages)
                if "тыс" in value and mileage < 1000:
                    mileage *= 1000
                return mileage
    return 0


def _param_value(ad, *terms):
    params = ad.get("params", [])
    if not isinstance(params, list):
        return ""
    for item in params:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").casefold().replace("ё", "е")
        if any(term in name for term in terms):
            return str(item.get("value") or "").strip()
    return ""


def _normalize_ad(ad):
    title = str(ad.get("title", ""))
    title_parts = [part.strip() for part in title.split(",")]
    vehicle_name = title_parts[0] if title_parts else ""
    name_parts = vehicle_name.split()

    brand = str(ad.get("marka") or (name_parts[0] if name_parts else ""))
    model = str(ad.get("model") or " ".join(name_parts[1:]))
    year = _number(ad.get("year"))
    if not year:
        year_match = re.search(r"\b(?:19|20)\d{2}\b", title)
        year = int(year_match.group()) if year_match else 0

    seller_params = [
        str(item.get("value") or "")
        for item in ad.get("params", [])
        if isinstance(item, dict)
        and any(word in str(item.get("name", "")).casefold() for word in ("кто прода", "тип продав"))
    ]
    price_conditions = [
        str(item.get("value") or "")
        for item in ad.get("params", [])
        if isinstance(item, dict)
        and any(
            word in str(item.get("name", "")).casefold()
            for word in ("кредит", "трейд", "услов", "оплат", "цена")
        )
    ]
    return {
        "brand": brand,
        "model": model,
        "generation": str(ad.get("model_2") or _param_value(ad, "поколение")),
        "year": year,
        "price": _number(ad.get("price")),
        "mileage": _get_mileage(ad),
        "engine": _param_value(ad, "тип двигателя", "двигатель"),
        "engine_volume": _param_value(ad, "объем двигателя"),
        "transmission": _param_value(ad, "коробка передач", "трансмиссия"),
        "body": _param_value(ad, "тип кузова", "кузов"),
        "condition": _param_value(ad, "состояние"),
        "price_conditions": " ".join(price_conditions),
        "description": str(ad.get("description") or ad.get("body") or ""),
        "seller": str(ad.get("name") or ""),
        "seller_name": str(ad.get("name") or ""),
        "seller_type": " ".join(seller_params),
        "url": str(ad.get("url") or ""),
        "source_id": str(ad.get("avito_id") or ad.get("Id") or ad.get("url") or ""),
        "site": "avito",
    }


async def parse_avito():
    """Fetch recent Saint Petersburg car ads from the REST-App Avito API."""
    if not AVITO_API_LOGIN or not AVITO_API_TOKEN:
        raise RuntimeError("Avito REST-App credentials are missing; set login and token_avito in .env")

    await asyncio.to_thread(ensure_real_price_access)

    now = now_moscow()
    params = {
        "login": AVITO_API_LOGIN,
        "token": AVITO_API_TOKEN,
        "category_id": 9,
        "region_id": FILTERS["avito_region_id"],
        "limit": SCRAPING["listing_api_limit"],
        "offset": 0,
        "format": "json",
    }
    lookback_hours = FILTERS["avito_lookback_hours"]
    if lookback_hours > 0:
        params["date1"] = (now - timedelta(hours=lookback_hours)).strftime("%Y-%m-%d %H:%M:%S")
        params["date2"] = now.strftime("%Y-%m-%d %H:%M:%S")
    else:
        params["last_m"] = min(30, max(1, FILTERS["avito_lookback_minutes"]))

    try:
        response = await asyncio.to_thread(requests.get, API_URL, params=params, timeout=30)
    except requests.RequestException as exc:
        raise RuntimeError(f"Avito REST-App request failed ({type(exc).__name__})") from None

    if response.status_code >= 400:
        message = _extract_api_message(response)
        raise RuntimeError(f"Avito REST-App returned HTTP {response.status_code}: {message}")

    try:
        payload = response.json()
    except ValueError:
        raise RuntimeError("Avito REST-App returned invalid JSON") from None

    if payload.get("status") != "ok":
        message = str(payload.get("message") or payload.get("error") or "unknown API error")
        for secret in (AVITO_API_LOGIN, AVITO_API_TOKEN):
            message = message.replace(secret, "<redacted>")
        raise RuntimeError(f"Avito REST-App error: {message}")

    data = payload.get("data", [])
    if not isinstance(data, list):
        raise RuntimeError("Avito REST-App returned an unexpected data format")
    if data and not any(isinstance(item, dict) and _number(item.get("price")) > 0 for item in data):
        raise RuntimeError(
            "Avito REST-App returned listings without usable prices; "
            "market evaluation requires an account plan with real prices"
        )

    observations = []
    rejected_count = 0
    for raw_ad in data:
        if not isinstance(raw_ad, dict):
            continue
        ad = _normalize_ad(raw_ad)
        description = ad["description"].strip()
        if description and is_acceptable_private_car(ad):
            save_quality_check(ad["source_id"], True, description)
            observations.append(ad)
        else:
            rejected_count += 1

    save_market_observations(
        observations,
        retention_days=FILTERS["market_history_days"],
    )

    results = []
    for ad in observations:
        if ad["price"] <= 0 or ad["price"] > FILTERS["max_price"]:
            continue
        if ad["mileage"] > FILTERS["max_mileage"]:
            continue
        results.append(ad)

    print(
        f"[Avito API] Получено: {len(data)}; частных с описанием: {len(observations)}; "
        f"отклонено фильтром: {rejected_count}; прошло цену/пробег: {len(results)}"
    )
    return results