import asyncio
import re
from datetime import datetime, timedelta

import requests

from config import AVITO_API_LOGIN, AVITO_API_TOKEN, FILTERS
from market_data import save_market_observations, save_quality_check
from parsers.rest_app import ensure_real_price_access
from quality_filters import is_acceptable_private_car


API_URL = "https://rest-app.net/api-drom-ru/ads"


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


def _normalize_ad(raw_ad):
    seller = str(raw_ad.get("seller") or "")
    description = str(raw_ad.get("info") or raw_ad.get("description") or "")
    return {
        "brand": str(raw_ad.get("marka") or "").strip(),
        "model": str(raw_ad.get("model") or raw_ad.get("model_2") or "").strip(),
        "year": _number(raw_ad.get("year")),
        "price": _number(raw_ad.get("price")),
        "mileage": _number(raw_ad.get("run")),
        "description": description,
        "info": description,
        "seller": seller,
        "seller_name": seller,
        "seller_type": str(raw_ad.get("seller_type") or ""),
        "title": " ".join(filter(None, (str(raw_ad.get("marka") or ""), str(raw_ad.get("model") or "")))),
        "url": str(raw_ad.get("url") or ""),
        "source_id": str(raw_ad.get("Id") or raw_ad.get("url") or ""),
        "site": "drom",
    }


async def parse_drom():
    """Fetch Drom car ads through the documented REST-App API."""
    if not AVITO_API_LOGIN or not AVITO_API_TOKEN:
        raise RuntimeError("REST-App credentials are missing; set login and token_avito in .env")

    await asyncio.to_thread(ensure_real_price_access)

    params = {
        "login": AVITO_API_LOGIN,
        "token": AVITO_API_TOKEN,
        "region_id": FILTERS["drom_api_region_id"],
        "price2": FILTERS["market_max_price"],
        "limit": 1000,
        "offset": 0,
        "format": "json",
    }
    now = datetime.now()
    lookback_hours = FILTERS["drom_lookback_hours"]
    if lookback_hours > 0:
        params["date1"] = (now - timedelta(hours=lookback_hours)).strftime("%Y-%m-%d %H:%M:%S")
        params["date2"] = now.strftime("%Y-%m-%d %H:%M:%S")
    else:
        params["last_m"] = min(30, max(1, FILTERS["drom_lookback_minutes"]))
    try:
        response = await asyncio.to_thread(requests.get, API_URL, params=params, timeout=30)
    except requests.RequestException as exc:
        raise RuntimeError(f"Drom REST-App request failed ({type(exc).__name__})") from None

    if response.status_code >= 400:
        message = _extract_api_message(response)
        raise RuntimeError(f"Drom REST-App returned HTTP {response.status_code}: {message}")
    try:
        payload = response.json()
    except ValueError:
        raise RuntimeError("Drom REST-App returned invalid JSON") from None
    if payload.get("status") != "ok":
        message = str(payload.get("message") or payload.get("error") or "unknown API error")
        for secret in (AVITO_API_LOGIN, AVITO_API_TOKEN):
            message = message.replace(secret, "<redacted>")
        raise RuntimeError(f"Drom REST-App error: {message}")

    data = payload.get("data", [])
    if not isinstance(data, list):
        raise RuntimeError("Drom REST-App returned an unexpected data format")

    accepted = []
    for raw_ad in data:
        if not isinstance(raw_ad, dict):
            continue
        ad = _normalize_ad(raw_ad)
        if not (ad["source_id"] and ad["brand"] and ad["model"] and ad["year"] and ad["price"]):
            continue
        if not ad["description"].strip() or not is_acceptable_private_car(ad):
            continue
        save_quality_check(ad["source_id"], True, ad["description"])
        accepted.append(ad)

    save_market_observations(
        accepted,
        retention_days=FILTERS["market_history_days"],
        price_quality="verified",
    )
    return [
        ad for ad in accepted
        if ad["price"] <= FILTERS["max_price"]
        and (ad["mileage"] == 0 or ad["mileage"] <= FILTERS["max_mileage"])
    ]