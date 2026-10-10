import asyncio
from datetime import datetime, timedelta

from config import FILTERS, SCHEDULE, SCRAPING
from market_data import (
    DailyBudgetExceeded,
    ensure_daily_scan,
    ensure_scan_window,
    finish_daily_scan,
    get_daily_scan_summary,
    list_scan_windows,
    record_scan_page,
    is_rest_app_budget_blocked,
    is_rest_app_source_budget_blocked,
    stop_pending_scan_windows,
    stop_scan_window,
)
from parsers.avito import parse_avito
from parsers.autoru import parse_autoru
from parsers.drom import parse_drom
from quality_filters import normalize_vin


DEFAULT_ADAPTERS = {
    "avito": parse_avito,
    "autoru": parse_autoru,
    "drom": parse_drom,
}


def build_date_windows(range_start, range_end, window_days=1):
    if window_days <= 0:
        raise ValueError("window_days must be positive")
    if range_end <= range_start:
        raise ValueError("range_end must be after range_start")
    windows = []
    start = range_start
    step = timedelta(days=window_days)
    while start < range_end:
        end = min(start + step, range_end)
        windows.append((start, end))
        start = end
    return windows


def _parse_checkpoint_datetime(value):
    parsed = datetime.fromisoformat(value)
    return parsed


async def scan_daily_market(
    report_day,
    *,
    now,
    deadline,
    range_start=None,
    range_end=None,
    adapters=None,
    sleep=asyncio.sleep,
    clock=None,
    request_delay=None,
):
    adapters = adapters or DEFAULT_ADAPTERS
    clock = clock or (lambda: datetime.now(now.tzinfo))
    request_delay = SCHEDULE["request_delay_seconds"] if request_delay is None else request_delay
    range_start = range_start or (now - timedelta(days=SCHEDULE["scan_lookback_days"]))
    range_end = range_end or deadline
    existing = ensure_daily_scan(report_day, range_start, range_end, now=now)
    range_start = _parse_checkpoint_datetime(existing["range_start"])
    range_end = _parse_checkpoint_datetime(existing["range_end"])
    windows = list(reversed(build_date_windows(range_start, range_end, SCHEDULE["scan_window_days"])))
    for source in adapters:
        for window_start, window_end in windows:
            ensure_scan_window(report_day, source, window_start, window_end)

    page_limit = min(1000, max(1, SCRAPING["listing_api_limit"]))
    stop_reason = ""
    weights = FILTERS["rest_app_source_weights"]
    sources = sorted(adapters, key=lambda source: (-weights.get(source, 0), source))

    for source in sources:
        adapter = adapters[source]
        source_blocked = False
        for checkpoint in list_scan_windows(report_day, source):
            if checkpoint["status"] == "complete":
                continue
            window_start = _parse_checkpoint_datetime(checkpoint["window_start"])
            window_end = _parse_checkpoint_datetime(checkpoint["window_end"])

            while True:
                current_time = clock()
                if current_time >= deadline:
                    stop_reason = "report_deadline"
                    stop_scan_window(report_day, source, window_start, window_end, stop_reason)
                    stop_pending_scan_windows(report_day, source, stop_reason)
                    for remaining_source in adapters:
                        stop_pending_scan_windows(report_day, remaining_source, stop_reason)
                    finish_daily_scan(report_day, "partial", stop_reason)
                    return get_daily_scan_summary(report_day, now=current_time)
                if window_end > current_time and window_end - current_time > timedelta(
                    minutes=SCHEDULE["final_window_minutes"]
                ):
                    break

                checkpoint = next(
                    item for item in list_scan_windows(report_day, source)
                    if item["window_start"] == window_start.isoformat(timespec="seconds")
                    and item["window_end"] == window_end.isoformat(timespec="seconds")
                )
                if checkpoint["status"] == "complete":
                    break
                if checkpoint["page_count"] >= SCHEDULE["max_pages_per_window"]:
                    stop_reason = "max_pages_per_window"
                    stop_scan_window(report_day, source, window_start, window_end, stop_reason)
                    stop_pending_scan_windows(report_day, source, stop_reason)
                    break

                offset = int(checkpoint["next_offset"])
                budget_source = (
                    f"{source}_current"
                    if window_start.date() == range_end.date()
                    else source
                )
                if is_rest_app_budget_blocked():
                    stop_reason = "daily_budget_or_vendor_quota_exhausted"
                    stop_scan_window(report_day, source, window_start, window_end, stop_reason)
                    for remaining_source in adapters:
                        stop_pending_scan_windows(report_day, remaining_source, stop_reason)
                    finish_daily_scan(report_day, "partial", stop_reason)
                    return get_daily_scan_summary(report_day, now=current_time)
                if is_rest_app_source_budget_blocked(budget_source):
                    stop_reason = f"source_budget_cap:{budget_source}"
                    stop_scan_window(report_day, source, window_start, window_end, stop_reason)
                    source_blocked = True
                    break
                try:
                    page = await adapter(
                        date_start=window_start,
                        date_end=window_end,
                        offset=offset,
                        limit=page_limit,
                        return_page=True,
                        budget_source=budget_source,
                    )
                except DailyBudgetExceeded as exc:
                    stop_reason = "source_budget_cap"
                    stop_scan_window(report_day, source, window_start, window_end, f"{stop_reason}: {exc}")
                    source_blocked = True
                    break
                except Exception as exc:
                    stop_reason = f"source_error: {type(exc).__name__}: {exc}"
                    await sleep(request_delay)
                    stop_scan_window(report_day, source, window_start, window_end, stop_reason)
                    source_blocked = True
                    break

                result = record_scan_page(
                    report_day,
                    source,
                    window_start,
                    window_end,
                    offset=offset,
                    limit=page_limit,
                    returned_count=page["returned_count"],
                    ads=page["ads"],
                )
                await sleep(request_delay)
                if result["complete"]:
                    break
            if source_blocked:
                break

    summary = get_daily_scan_summary(report_day, now=clock())
    finish_daily_scan(report_day, "complete" if summary["complete"] else "partial", stop_reason)
    return get_daily_scan_summary(report_day, now=clock())
