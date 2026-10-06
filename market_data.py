import os
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median


DEFAULT_DB_PATH = Path(__file__).resolve().parent / ".market.sqlite3"
DB_PATH = Path(os.getenv("MARKET_DB_PATH", str(DEFAULT_DB_PATH)))
TRIM_TOKENS = {"at", "mt", "cvt", "dsg", "акпп", "мкпп", "автомат", "механика"}


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
    return connection


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
    return " ".join(words)


def save_market_observations(ads, observed_at=None, retention_days=30, price_quality="verified"):
    """Store priced Avito listings for later comparable-market estimates."""
    timestamp = (observed_at or datetime.now()).isoformat(timespec="seconds")
    rows = []
    for ad in ads:
        brand_key = _normalize_key(ad.get("brand"))
        model_key = _model_key(ad.get("model"))
        year = int(ad.get("year") or 0)
        price = int(ad.get("price") or 0)
        source_id = str(ad.get("source_id") or ad.get("url") or "").strip()
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

    if not rows:
        return 0

    cutoff = ((observed_at or datetime.now()) - timedelta(days=retention_days)).isoformat(timespec="seconds")
    with _connection() as connection:
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
        connection.execute("DELETE FROM market_observations WHERE observed_at < ?", (cutoff,))
        connection.execute("DELETE FROM listing_quality_checks WHERE checked_at < ?", (cutoff,))
    return len(rows)


def get_quality_check(source_id, *, now=None, history_days=30):
    cutoff = ((now or datetime.now()) - timedelta(days=history_days)).isoformat(timespec="seconds")
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
    timestamp = (checked_at or datetime.now()).isoformat(timespec="seconds")
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


def estimate_market_price(ad, *, history_days=30, min_samples=5, now=None):
    """Estimate asking-market median from recent same-model listings."""
    brand_key = _normalize_key(ad.get("brand"))
    model_key = _model_key(ad.get("model"))
    year = int(ad.get("year") or 0)
    price = int(ad.get("price") or 0)
    source_id = str(ad.get("source_id") or ad.get("url") or "").strip()
    if not (brand_key and model_key and year):
        return None

    cutoff = ((now or datetime.now()) - timedelta(days=history_days)).isoformat(timespec="seconds")
    with _connection() as connection:
        rows = connection.execute(
            """
                        SELECT observations.source_id, observations.year, observations.price, observations.mileage
                        FROM market_observations AS observations
                        JOIN listing_quality_checks AS quality
                            ON quality.source_id = observations.source_id
                         AND quality.accepted = 1
                         AND quality.checked_at >= ?
                        WHERE observations.brand_key = ? AND observations.model_key = ?
                            AND observations.year BETWEEN ? AND ?
                            AND observations.observed_at >= ?
                            AND observations.price > 0
                            AND observations.price_quality = 'verified'
                            AND observations.source_id != ?
            """,
                        (cutoff, brand_key, model_key, year - 1, year + 1, cutoff, source_id),
        ).fetchall()

    if not rows:
        return None

    mileage = int(ad.get("mileage") or 0)
    mileage_matches = []
    if mileage > 0:
        mileage_tolerance = max(50000, int(mileage * 0.35))
        mileage_matches = [
            row for row in rows
            if row["mileage"] > 0 and abs(row["mileage"] - mileage) <= mileage_tolerance
        ]

    comparable_rows = mileage_matches if len(mileage_matches) >= min_samples else rows
    if len(comparable_rows) < min_samples:
        return None

    market_price = int(median(row["price"] for row in comparable_rows))
    discount_pct = round((market_price - price) / market_price * 100, 1) if market_price else 0
    return {
        "market_price": market_price,
        "sample_count": len(comparable_rows),
        "year_range": f"{year - 1}-{year + 1}",
        "mileage_matched": comparable_rows is mileage_matches,
        "discount_pct": discount_pct,
    }