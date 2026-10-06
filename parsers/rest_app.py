from functools import lru_cache

import requests

from config import AVITO_API_LOGIN, AVITO_API_TOKEN


API_INFO_URL = "https://rest-app.net/api/info"


@lru_cache(maxsize=1)
def ensure_real_price_access():
    if not AVITO_API_LOGIN or not AVITO_API_TOKEN:
        raise RuntimeError("REST-App credentials are missing; set login and token_avito in .env")

    try:
        response = requests.get(
            API_INFO_URL,
            params={"login": AVITO_API_LOGIN, "token": AVITO_API_TOKEN, "format": "json"},
            timeout=20,
        )
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise RuntimeError(f"REST-App account check failed ({type(exc).__name__})") from None

    data = payload.get("data")
    if isinstance(data, list):
        data = data[0] if data else {}
    if payload.get("status") != "ok" or not isinstance(data, dict):
        raise RuntimeError("REST-App could not confirm account price access")
    if str(data.get("demo", "0")) == "1":
        raise RuntimeError(
            "REST-App account is in demo mode; prices are random or masked, "
            "so market evaluation requires real-price access"
        )