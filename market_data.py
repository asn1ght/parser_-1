import os
import re
import sqlite3
import unicodedata
import json
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median, quantiles

from config import FILTERS, now_moscow


DEFAULT_DB_PATH = Path(__file__).resolve().parent / ".market.sqlite3"
DB_PATH = Path(os.getenv("MARKET_DB_PATH", str(DEFAULT_DB_PATH)))
TRIM_TOKENS = {"at", "mt", "cvt", "dsg", "акпп", "мкпп", "автомат", "механика"}
_MODEL_NOISE_WORDS = {"рестайлинг", "restyling", "рест", "рестайл", "facelift", "fl"}
_GENERATION_MARKERS = {"i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x"}


def _connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS market_observations (
            source_id TEXT PRIMARY KEY,
            brand_key TEXT NOT NULL,
            model_key TEXT NOT NULL,
            year INTEGER NOT NULL,
            price INTEGER NOT NULL,
            mileage INTEGER NOT NULL,
            price_quality TEXT NOT NULL DEFAULT 'unverified',
            observed_at TEXT NOT NULL
        )
        """
    )
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(market_observations)")}
    if "price_quality" not in columns:
        connection.execute(
            "ALTER TABLE market_observations "
            "ADD COLUMN price_quality TEXT NOT NULL DEFAULT 'unverified'"
        )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS market_lookup "
        "ON market_observations (brand_key, model_key, year, observed_at)"
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS listing_quality_checks (
            source_id TEXT PRIMARY KEY,
            accepted INTEGER NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            checked_at TEXT NOT NULL
        )
        """
    )
    quality_columns = {row["name"] for row in connection.execute("PRAGMA table_info(listing_quality_checks)")}
    if "description" not in quality_columns:
        connection.execute(
            "ALTER TABLE listing_quality_checks "
            "ADD COLUMN description TEXT NOT NULL DEFAULT ''"
        )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS listing_catalog (
            site TEXT NOT NULL,
            source_id TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            sent_at TEXT,
            PRIMARY KEY (site, source_id)
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS listing_catalog_seen ON listing_catalog (last_seen_at)"
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_report_candidates (
            report_day TEXT NOT NULL,
            site TEXT NOT NULL,
            source_id TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (report_day, site, source_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_report_deliveries (
            report_day TEXT PRIMARY KEY,
            sent_at TEXT NOT NULL
        )
        """
    )
    _migrate_model_keys(connection)
    return connection


def _migrate_model_keys(connection):
    version = connection.execute("PRAGMA user_version").fetchone()[0] or 0
    if version >= 2:
        return
    rows = connection.execute("SELECT source_id, model_key FROM market_observations").fetchall()
    for row in rows:
        new_key = _model_key(row["model_key"])
        if new_key and new_key != row["model_key"]:
            connection.execute(
                "UPDATE market_observations SET model_key = ? WHERE source_id = ?",
                (new_key, row["source_id"]),
            )
    connection.execute("PRAGMA user_version = 2")


@contextmanager
def _connection():
    connection = _connect()
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _normalize_key(value):
    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[^a-zа-яё0-9]+", " ", value).strip()


def _model_key(value):
    model = unicodedata.normalize("NFKC", str(value or "")).casefold()
    model = re.sub(r"\b\d+[.,]\d+\b.*$", "", model).strip()
    model = _normalize_key(model)
    words = model.split()
    while words and words[-1] in TRIM_TOKENS:
        words.pop()
    words = [word for word in words if word not in _MODEL_NOISE_WORDS]
    while len(words) > 1 and words[-1] in _GENERATION_MARKERS:
        words.pop()
    return " ".join(words)


def exclude_price_outliers(rows, *, min_samples=5):
    rows = list(rows)
    if len(rows) < 5:
        return rows
    prices = [int(row["price"]) for row in rows]
    lower_quartile, _, upper_quartile = quantiles(prices, n=4, method="inclusive")
    spread = upper_quartile - lower_quartile
    if spread <= 0:
        return rows
    lower_bound = lower_quartile - 1.5 * spread
    upper_bound = upper_quartile + 1.5 * spread
    return [
        row for row in rows
        if lower_bound <= int(row["price"]) <= upper_bound
    ]


def save_market_observations(ads, observed_at=None, retention_days=30, price_quality="verified"):
    """Store priced Avito listings for later comparable-market estimates."""
    timestamp = (observed_at or now_moscow()).isoformat(timespec="seconds")
    rows = []
    catalog_rows = []
    for ad in ads:
        site = str(ad.get("site") or "").strip().casefold()
        source_id = str(ad.get("source_id") or ad.get("url") or "").strip()
        if site and source_id:
            catalog_rows.append(
                (site, source_id, json.dumps(ad, ensure_ascii=False), timestamp, timestamp)
            )

        brand_key = _normalize_key(ad.get("brand"))
        model_key = _model_key(ad.get("model"))
        year = int(ad.get("year") or 0)
        price = int(ad.get("price") or 0)
        if not (source_id and brand_key and model_key and year and price > 0):
            continue
        rows.append(
            (
                source_id,
                brand_key,
                model_key,
                year,
                price,
                int(ad.get("mileage") or 0),
                price_quality,
                timestamp,
            )
        )

    if not rows and not catalog_rows:
        return 0

    cutoff = ((observed_at or now_moscow()) - timedelta(days=retention_days)).isoformat(timespec="seconds")
    with _connection() as connection:
        if rows:
            connection.executemany(
            """
            INSERT INTO market_observations
                (source_id, brand_key, model_key, year, price, mileage, price_quality, observed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id) DO UPDATE SET
                brand_key = excluded.brand_key,
                model_key = excluded.model_key,
                year = excluded.year,
                price = excluded.price,
                mileage = excluded.mileage,
                price_quality = excluded.price_quality,
                observed_at = excluded.observed_at
            """,
            rows,
            )
        if catalog_rows:
            connection.executemany(
                """
                INSERT INTO listing_catalog
                    (site, source_id, payload_json, first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(site, source_id) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    last_seen_at = excluded.last_seen_at
                """,
                catalog_rows,
            )
        connection.execute("DELETE FROM market_observations WHERE observed_at < ?", (cutoff,))
        connection.execute("DELETE FROM listing_quality_checks WHERE checked_at < ?", (cutoff,))
    return len(rows)


def load_listing_catalog(*, history_days=30, max_price=None, max_mileage=None, now=None):
    cutoff = ((now or now_moscow()) - timedelta(days=history_days)).isoformat(timespec="seconds")
    with _connection() as connection:
        rows = connection.execute(
            """SELECT payload_json FROM listing_catalog
               WHERE last_seen_at >= ? AND sent_at IS NULL
               ORDER BY last_seen_at DESC""",
            (cutoff,),
        ).fetchall()

    ads = []
    for row in rows:
        try:
            ad = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            continue
        price = int(ad.get("price") or 0)
        mileage = int(ad.get("mileage") or 0)
        if price <= 0 or (max_price is not None and price > max_price):
            continue
        if max_mileage is not None and mileage > max_mileage:
            continue
        ads.append(ad)
    return ads


def filter_previously_sent_ads(ads):
    if not ads:
        return []
    with _connection() as connection:
        rows = connection.execute(
            "SELECT site, source_id FROM listing_catalog WHERE sent_at IS NOT NULL"
        ).fetchall()
    sent = {(row["site"], row["source_id"]) for row in rows}
    return [
        ad for ad in ads
        if (
            str(ad.get("site") or "").strip().casefold(),
            str(ad.get("source_id") or ad.get("url") or "").strip(),
        ) not in sent
    ]


def save_daily_report_candidates(report_day, ads):
    timestamp = str(report_day)
    rows = []
    for ad in ads:
        site = str(ad.get("site") or "").strip().casefold()
        source_id = str(ad.get("source_id") or ad.get("url") or "").strip()
        price = int(ad.get("price") or 0)
        if not (
            site
            and source_id
            and not ad.get("red_flags")
            and 0 < price <= FILTERS["max_price"]
        ):
            continue
        rows.append((timestamp, site, source_id, json.dumps(ad, ensure_ascii=False)))

    if not rows:
        return 0
    saved = 0
    with _connection() as connection:
        for day, site, source_id, payload in rows:
            already_sent = connection.execute(
                "SELECT 1 FROM listing_catalog WHERE site = ? AND source_id = ? AND sent_at IS NOT NULL",
                (site, source_id),
            ).fetchone()
            if already_sent:
                continue
            connection.execute(
                """INSERT INTO daily_report_candidates (report_day, site, source_id, payload_json)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(report_day, site, source_id) DO UPDATE SET
                       payload_json = excluded.payload_json""",
                (day, site, source_id, payload),
            )
            saved += 1
    return saved


def load_daily_report_candidates(report_day):
    with _connection() as connection:
        rows = connection.execute(
            """SELECT payload_json FROM daily_report_candidates
               WHERE report_day = ? ORDER BY site, source_id""",
            (str(report_day),),
        ).fetchall()
    candidates = []
    for row in rows:
        try:
            candidates.append(json.loads(row["payload_json"]))
        except (TypeError, ValueError):
            continue
    return candidates


def daily_report_was_sent(report_day):
    with _connection() as connection:
        return connection.execute(
            "SELECT 1 FROM daily_report_deliveries WHERE report_day = ?",
            (str(report_day),),
        ).fetchone() is not None


def mark_daily_report_sent(report_day, *, sent_at=None):
    timestamp = (sent_at or now_moscow()).isoformat(timespec="seconds")
    with _connection() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO daily_report_deliveries (report_day, sent_at) VALUES (?, ?)",
            (str(report_day), timestamp),
        )


def mark_listings_sent(ads, *, sent_at=None):
    timestamp = (sent_at or now_moscow()).isoformat(timespec="seconds")
    rows = [
        (timestamp, str(ad.get("site") or "").strip().casefold(),
         str(ad.get("source_id") or ad.get("url") or "").strip())
        for ad in ads
        if ad.get("site") and (ad.get("source_id") or ad.get("url"))
    ]
    if not rows:
        return
    with _connection() as connection:
        connection.executemany(
            "UPDATE listing_catalog SET sent_at = ? WHERE site = ? AND source_id = ?",
            rows,
        )


def get_quality_check(source_id, *, now=None, history_days=30):
    cutoff = ((now or now_moscow()) - timedelta(days=history_days)).isoformat(timespec="seconds")
    with _connection() as connection:
        row = connection.execute(
            "SELECT accepted, description FROM listing_quality_checks "
            "WHERE source_id = ? AND checked_at >= ?",
            (str(source_id), cutoff),
        ).fetchone()
    return None if row is None else {
        "accepted": bool(row["accepted"]),
        "description": row["description"],
    }


def save_quality_check(source_id, accepted, description="", checked_at=None):
    if not source_id:
        return
    timestamp = (checked_at or now_moscow()).isoformat(timespec="seconds")
    with _connection() as connection:
        connection.execute(
            """
            INSERT INTO listing_quality_checks (source_id, accepted, description, checked_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(source_id) DO UPDATE SET
                accepted = excluded.accepted,
                description = excluded.description,
                checked_at = excluded.checked_at
            """,
            (str(source_id), int(bool(accepted)), str(description or ""), timestamp),
        )


def estimate_market_price(ad, *, history_days=30, min_samples=3, now=None):
    """Estimate asking-market median from accumulated same-model observations."""
    brand_key = _normalize_key(ad.get("brand"))
    model_key = _model_key(ad.get("model"))
    year = int(ad.get("year") or 0)
    price = int(ad.get("price") or 0)
    source_id = str(ad.get("source_id") or ad.get("url") or "").strip()
    if not (brand_key and model_key and year):
        return None

    cutoff = ((now or now_moscow()) - timedelta(days=history_days)).isoformat(timespec="seconds")

    def _fetch_rows(year_span):
        with _connection() as connection:
            return connection.execute(
                """
                SELECT o.source_id, o.year, o.price, o.mileage
                FROM market_observations AS o
                JOIN listing_quality_checks AS q
                    ON q.source_id = o.source_id
                   AND q.accepted = 1
                   AND q.checked_at >= ?
                WHERE o.brand_key = ? AND o.model_key = ?
                  AND o.year BETWEEN ? AND ?
                  AND o.observed_at >= ?
                  AND o.price > 0
                  AND o.price_quality = 'verified'
                  AND o.source_id != ?
                """,
                (cutoff, brand_key, model_key, year - year_span, year + year_span, cutoff, source_id),
            ).fetchall()

    rows = list(_fetch_rows(1))
    if len(rows) < min_samples:
        rows = list(_fetch_rows(2))

    mileage = int(ad.get("mileage") or 0)
    if mileage > 0:
        tolerance = max(50000, int(mileage * 0.5))
        mileage_matched = [
            row for row in rows
            if row["mileage"] > 0 and abs(row["mileage"] - mileage) <= tolerance
        ]
        comparable_rows = mileage_matched if len(mileage_matched) >= min_samples else rows
    else:
        comparable_rows = rows

    comparable_rows = exclude_price_outliers(comparable_rows, min_samples=min_samples)
    if len(comparable_rows) < min_samples:
        return None

    market_price = int(median(row["price"] for row in comparable_rows))
    discount_pct = round((market_price - price) / market_price * 100, 1) if market_price else 0
    used_years = [int(row["year"]) for row in comparable_rows]
    return {
        "market_price": market_price,
        "sample_count": len(comparable_rows),
        "year_range": f"{min(used_years)}-{max(used_years)}",
        "mileage_matched": True,
        "discount_pct": discount_pct,
        "market_sites": [],
    }