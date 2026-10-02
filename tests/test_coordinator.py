"""Tests for the coordinator's download-window selection and heal state machine."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from enovapower import BillingPeriod, EnovaAuthError, EnovaError, EnovaNetworkError

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.enova_power.const import (
    CHUNK_DELAY_SECONDS,
    CONF_BACKFILL_MONTHS,
    CONF_PLAN,
    CONF_STATS_VERSION,
    DEFAULT_BACKFILL_MONTHS,
    DEFAULT_PLAN,
    DOMAIN,
    PLAN_TOU,
    RECENT_DAYS,
)
import custom_components.enova_power.coordinator as coordinator_module
from custom_components.enova_power.coordinator import (
    EnovaPowerCoordinator,
    cycle_start_containing,
    fetch_from_date,
)
from custom_components.enova_power.statistics import (
    STATS_VERSION,
    ImportResult,
    SeriesScan,
    consumption_statistic_id,
    download_window,
)

from .test_statistics import _reading

# Two consecutive billing cycles: May 20 – Jun 19 and Jun 20 – Jul 20
# (start_date is the previous read date, exclusive).
P1 = BillingPeriod(date(2026, 5, 19), date(2026, 6, 19), 31, 0.0, 0.0)
P2 = BillingPeriod(date(2026, 6, 19), date(2026, 7, 20), 31, 0.0, 0.0)


async def test_backfills_when_no_statistics() -> None:
    today = date(2026, 6, 1)
    assert fetch_from_date(None, today) == today - timedelta(days=DEFAULT_BACKFILL_MONTHS * 31)


async def test_backfill_depth_is_configurable() -> None:
    today = date(2026, 6, 1)
    assert fetch_from_date(None, today, 16) == today - timedelta(days=16 * 31)
    # An incremental window ignores the backfill depth.
    last_start = datetime(2026, 5, 31, 5, tzinfo=timezone.utc)
    assert fetch_from_date(last_start, today, 16) == fetch_from_date(last_start, today)


async def test_incremental_uses_recent_window_when_current() -> None:
    today = date(2026, 6, 10)
    last_start = datetime(2026, 6, 9, 5, tzinfo=timezone.utc)  # yesterday
    # recent window is earlier than (last_start - 1 day), so it wins
    assert fetch_from_date(last_start, today) == today - timedelta(days=RECENT_DAYS)


async def test_incremental_covers_long_gap_after_downtime() -> None:
    today = date(2026, 6, 30)
    last_start = datetime(2026, 6, 1, 5, tzinfo=timezone.utc)  # ~29 days ago
    # gap is older than the recent window, so fetch from just before the gap
    assert fetch_from_date(last_start, today) == date(2026, 5, 31)


def _coordinator(hass, *, detected=None, options=None, data=None):
    entry = MockConfigEntry(domain=DOMAIN, data=data or {}, options=options or {})
    entry.add_to_hass(hass)
    client = MagicMock()
    client.get_current_plan = AsyncMock(return_value=detected)
    return EnovaPowerCoordinator(hass, entry, client)


async def test_plan_override_from_options(hass: HomeAssistant) -> None:
    assert _coordinator(hass, options={CONF_PLAN: "tiered"}).plan_override() == "tiered"


async def test_plan_override_from_legacy_data(hass: HomeAssistant) -> None:
    assert _coordinator(hass, data={CONF_PLAN: "ulo"}).plan_override() == "ulo"


async def test_plan_override_none(hass: HomeAssistant) -> None:
    assert _coordinator(hass).plan_override() is None


async def test_meter_plan_uses_detection(hass: HomeAssistant) -> None:
    assert await _coordinator(hass, detected="ulo")._meter_plan("111") == "ulo"


async def test_meter_plan_override_wins(hass: HomeAssistant) -> None:
    coord = _coordinator(hass, detected="ulo", options={CONF_PLAN: "tiered"})
    assert await coord._meter_plan("111") == "tiered"


async def test_meter_plan_defaults_when_undetected(hass: HomeAssistant) -> None:
    assert await _coordinator(hass, detected=None)._meter_plan("111") == DEFAULT_PLAN


# --- billing-cycle snap ------------------------------------------------------ #


async def test_DW_1_5_cycle_start_containing_boundaries() -> None:
    # d == start_date belongs to the previous cycle (start_date is exclusive)...
    assert cycle_start_containing([P1, P2], date(2026, 6, 19)) == date(2026, 5, 20)
    # ...or, with no previous cycle known, to its calendar month.
    assert cycle_start_containing([P2], date(2026, 6, 19)) == date(2026, 6, 1)
    # start_date + 1 and end_date are both inside this cycle.
    assert cycle_start_containing([P1, P2], date(2026, 6, 20)) == date(2026, 6, 20)
    assert cycle_start_containing([P1, P2], date(2026, 7, 20)) == date(2026, 6, 20)


async def test_DW_1_5_cycle_start_no_periods_month_start() -> None:
    assert cycle_start_containing([], date(2026, 7, 15)) == date(2026, 7, 1)
    # Before every known cycle → month start too.
    assert cycle_start_containing([P1, P2], date(2026, 3, 15)) == date(2026, 3, 1)


async def test_cycle_start_after_last_cycle_never_reaches_into_it() -> None:
    # The open cycle (Jul 21 →) is month-keyed, but the month start (Jul 1)
    # lies inside the closed cycle: a window from there would re-split the
    # closed cycle from a partial download. Stop at the open cycle's start.
    assert cycle_start_containing([P1, P2], date(2026, 7, 21)) == date(2026, 7, 21)
    assert cycle_start_containing([P1, P2], date(2026, 7, 31)) == date(2026, 7, 21)
    # A month boundary inside the open cycle snaps to that month.
    assert cycle_start_containing([P1, P2], date(2026, 8, 10)) == date(2026, 8, 1)


async def test_DW_1_5_update_meter_downloads_whole_cycles_and_reaches_back_once(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Entry already at the current stats version: a normal (non-rebuild) cycle.
    coord = _coordinator(hass, detected=PLAN_TOU, data={CONF_STATS_VERSION: STATS_VERSION})
    client = coord.client
    client.billing_periods = AsyncMock(return_value=[P1])
    client.download_usage = AsyncMock(return_value=[])
    last_start = AsyncMock()
    import_meter = AsyncMock(return_value=ImportResult(None, {}))
    monkeypatch.setattr(coordinator_module, "async_last_statistic_start", last_start)
    monkeypatch.setattr(coordinator_module, "async_missing_series", AsyncMock(return_value=[]))
    monkeypatch.setattr(coordinator_module, "async_scan_series", AsyncMock(return_value={}))
    monkeypatch.setattr(coordinator_module, "async_import_meter", import_meter)

    async def cycle(today: date, periods: list[BillingPeriod]) -> tuple[date, date]:
        # Statistics are stored through yesterday; the portal lists ``periods``.
        last_start.return_value = datetime.combine(
            today - timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc
        )
        client.billing_periods.return_value = periods
        client.download_usage.reset_mock()
        await coord._update_meter("111", today, None)
        # The cycle's main request (a follow-up may ask for days it left out).
        return client.download_usage.call_args_list[0].args[:2]

    # Steady state inside the open cycle (Jun 20 →): the whole open cycle,
    # not just the recent days and not back to the month start.
    assert await cycle(date(2026, 7, 15), [P1]) == (date(2026, 6, 20), date(2026, 7, 15))

    # A bill posts (P2 closes Jul 20): the recent window starts inside the
    # newly closed cycle, so the download reaches back over the whole cycle
    # and its month-keyed tier split is rewritten as cycle-keyed.
    assert await cycle(date(2026, 7, 23), [P1, P2]) == (date(2026, 6, 20), date(2026, 7, 23))

    # Once the recent window clears the closed cycle, only the open one.
    client.download_usage.return_value = [_reading(date(2026, 7, 26), h01=1.0)]
    assert await cycle(date(2026, 7, 27), [P1, P2]) == (date(2026, 7, 21), date(2026, 7, 27))

    # The import is told the same window and which days the download covered.
    kwargs = import_meter.call_args.kwargs
    assert kwargs["window"] == download_window(date(2026, 7, 21), date(2026, 7, 27))
    assert kwargs["covered_days"] == {date(2026, 7, 26)}


async def test_incremental_start_fresh_meter_skips_missing_series_check(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No prior statistics at all → backfill directly; a series-added-by-upgrade
    # check would be meaningless (everything is "missing" on a fresh meter).
    coord = _coordinator(hass, detected=PLAN_TOU, data={CONF_STATS_VERSION: STATS_VERSION})
    monkeypatch.setattr(
        coordinator_module, "async_last_statistic_start", AsyncMock(return_value=None)
    )
    missing = AsyncMock(return_value=[])
    monkeypatch.setattr(coordinator_module, "async_missing_series", missing)

    assert await coord._incremental_start("111", ["enova_power:x"]) is None
    missing.assert_not_awaited()


# --- chunked download ---------------------------------------------------------- #

MOVE_IN = date(2025, 6, 15)  # the meter's first day of portal history
EMPTY_EXPORT = EnovaError("Unrecognized CSV header: expected at least 25 columns, got 0")


def _portal(coord: EnovaPowerCoordinator, history_start: date = MOVE_IN) -> AsyncMock:
    """Stub ``download_usage`` like the portal: one reading per day from
    ``history_start`` on, and an empty export for a range wholly before it."""

    async def download_usage(start: date, end: date, *, meter_id: str) -> list:
        if end < history_start:
            raise EMPTY_EXPORT
        first = max(start, history_start)
        return [_reading(first + timedelta(days=i), h01=1.0) for i in range((end - first).days + 1)]

    coord.client.download_usage = AsyncMock(side_effect=download_usage)
    return coord.client.download_usage


async def test_download_usage_chunks_newest_first_within_portal_limit(
    hass: HomeAssistant,
) -> None:
    coord = _coordinator(hass)
    download = _portal(coord, history_start=date(2020, 1, 1))
    from_date, today = date(2025, 3, 1), date(2026, 7, 15)

    readings = await coord._download_usage(METER, from_date, today)

    chunks = [c.args[:2] for c in download.call_args_list]
    assert chunks[0][1] == today
    assert chunks[-1][0] == from_date
    for (start, end), (_, older_end) in zip(chunks, chunks[1:]):
        assert older_end == start - timedelta(days=1)  # contiguous, no overlap
    assert all((end - start).days < 90 for start, end in chunks)
    # One reading per day, oldest first, nothing duplicated.
    assert [r.date for r in readings] == [
        from_date + timedelta(days=i) for i in range((today - from_date).days + 1)
    ]


async def test_download_usage_pauses_randomly_between_chunks(
    hass: HomeAssistant, no_chunk_delay: AsyncMock
) -> None:
    coord = _coordinator(hass)
    download = _portal(coord, history_start=date(2020, 1, 1))

    await coord._download_usage(METER, date(2025, 3, 1), TODAY)

    # A pause between every two requests, none before the first.
    assert no_chunk_delay.await_count == download.await_count - 1
    low, high = CHUNK_DELAY_SECONDS
    assert all(low <= c.args[0] <= high for c in no_chunk_delay.await_args_list)


async def test_download_usage_single_chunk_does_not_pause(
    hass: HomeAssistant, no_chunk_delay: AsyncMock
) -> None:
    coord = _coordinator(hass)
    _portal(coord)
    await coord._download_usage(METER, TODAY - timedelta(days=30), TODAY)
    no_chunk_delay.assert_not_awaited()


async def test_download_usage_skips_ranges_before_history_starts(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    # A backfill reaching back before move-in: the portal returns an empty
    # export for those chunks. Keep the history it does have instead of failing.
    coord = _coordinator(hass)
    download = _portal(coord)
    from_date = date(2024, 9, 1)

    readings = await coord._download_usage(METER, from_date, TODAY)

    assert readings[0].date == MOVE_IN
    assert readings[-1].date == TODAY
    assert download.call_args.args[0] == from_date  # every chunk was asked for
    # The empty chunks are reported once, merged into one range.
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    # Includes the days before move-in in the chunk that holds it.
    assert f"Meter {METER}: the portal has no usage data for 2024-09-01 to 2025-06-14" in (
        warnings[0]
    )


async def test_download_usage_keeps_history_on_both_sides_of_a_gap(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    coord = _coordinator(hass)
    download = _portal(coord, history_start=date(2020, 1, 1))
    portal = download.side_effect
    gap = (date(2026, 1, 17), date(2026, 4, 16))  # exactly the second chunk

    async def with_gap(start: date, end: date, *, meter_id: str) -> list:
        if (start, end) == gap:
            raise EMPTY_EXPORT
        return await portal(start, end, meter_id=meter_id)

    download.side_effect = with_gap
    readings = await coord._download_usage(METER, date(2025, 3, 1), TODAY)

    days = {r.date for r in readings}
    assert date(2025, 3, 1) in days  # older than the gap: still imported
    assert TODAY in days
    assert not any(gap[0] <= d <= gap[1] for d in days)
    assert "no usage data for 2026-01-17 to 2026-04-16" in caplog.text


async def test_download_usage_asks_again_for_days_the_portal_left_out(
    hass: HomeAssistant,
) -> None:
    # The real portal answers a range ending today with only its last 30 days.
    coord = _coordinator(hass)
    download = _portal(coord, history_start=date(2020, 1, 1))
    portal = download.side_effect

    async def last_30_days_when_ending_today(start: date, end: date, *, meter_id: str) -> list:
        if end == TODAY:
            start = max(start, TODAY - timedelta(days=30))
        return await portal(start, end, meter_id=meter_id)

    download.side_effect = last_30_days_when_ending_today
    from_date = date(2026, 1, 1)
    readings = await coord._download_usage(METER, from_date, TODAY)

    assert [r.date for r in readings] == [
        from_date + timedelta(days=i) for i in range((TODAY - from_date).days + 1)
    ]
    # The follow-up starts right before the first day the portal returned.
    assert download.call_args_list[1].args[1] == TODAY - timedelta(days=31)


async def test_download_usage_ignores_days_outside_the_requested_range(
    hass: HomeAssistant,
) -> None:
    coord = _coordinator(hass)
    coord.client.download_usage = AsyncMock(
        return_value=[_reading(TODAY - timedelta(days=200), h01=1.0), _reading(TODAY, h01=1.0)]
    )
    readings = await coord._download_usage(METER, TODAY - timedelta(days=10), TODAY)
    assert [r.date for r in readings] == [TODAY]


async def test_download_usage_fails_when_every_chunk_is_empty(
    hass: HomeAssistant,
) -> None:
    # No data anywhere in the window is a real problem, not a history start.
    coord = _coordinator(hass)
    _portal(coord, history_start=TODAY + timedelta(days=1))
    with pytest.raises(EnovaError):
        await coord._download_usage(METER, date(2025, 3, 1), TODAY)


@pytest.mark.parametrize(
    "error", [EnovaAuthError("expired"), EnovaNetworkError("down")], ids=["auth", "network"]
)
async def test_download_usage_older_chunk_auth_or_network_error_propagates(
    hass: HomeAssistant, error: EnovaError
) -> None:
    coord = _coordinator(hass)
    download = _portal(coord)
    newest = download.side_effect

    async def fail_older(start: date, end: date, *, meter_id: str) -> list:
        if end < TODAY:
            raise error
        return await newest(start, end, meter_id=meter_id)

    download.side_effect = fail_older
    with pytest.raises(type(error)):
        await coord._download_usage(METER, date(2025, 3, 1), TODAY)


# --- integrity check + heal state machine ------------------------------------- #

METER = "111"
CONS = consumption_statistic_id(METER)
# Oldest stored hour: local midnight Jul 1 2025 (EDT) — a month start, so the
# billing-cycle snap leaves a heal's from-date exactly there.
FIRST_START = datetime(2025, 7, 1, 4, tzinfo=timezone.utc)
OLDEST = date(2025, 7, 1)
TODAY = date(2026, 7, 15)
NORMAL_FROM = date(2026, 7, 1)  # no billing periods → the calendar month
T1 = datetime(2026, 7, 10, 4, tzinfo=timezone.utc)
T2 = datetime(2026, 7, 11, 4, tzinfo=timezone.utc)


def _healing_coordinator(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    *,
    scan: dict[str, SeriesScan] | None = None,
    stats_version: int = STATS_VERSION,
    data: dict | None = None,
) -> tuple[EnovaPowerCoordinator, AsyncMock, AsyncMock]:
    """A coordinator with every recorder seam stubbed; statistics are stored
    through yesterday and the startup scan returns ``scan`` (clean by default).
    Returns it with the ``async_import_meter`` and ``async_scan_series`` mocks."""
    coord = _coordinator(
        hass, detected=PLAN_TOU, data={CONF_STATS_VERSION: stats_version, **(data or {})}
    )
    coord.client.meter_ids = [METER]
    coord.client.billing_periods = AsyncMock(return_value=[])
    coord.client.download_usage = AsyncMock(return_value=[])
    coord.client.download_tariff = AsyncMock(return_value=[])
    yesterday = datetime(2026, 7, 14, 4, tzinfo=timezone.utc)
    monkeypatch.setattr(
        coordinator_module, "async_last_statistic_start", AsyncMock(return_value=yesterday)
    )
    monkeypatch.setattr(coordinator_module, "async_missing_series", AsyncMock(return_value=[]))
    if scan is None:
        scan = {CONS: SeriesScan(FIRST_START, [])}
    scan_series = AsyncMock(return_value=scan)
    monkeypatch.setattr(coordinator_module, "async_scan_series", scan_series)
    import_meter = AsyncMock(return_value=ImportResult(None, {}))
    monkeypatch.setattr(coordinator_module, "async_import_meter", import_meter)
    return coord, import_meter, scan_series


async def _cycle(
    coord: EnovaPowerCoordinator,
    import_meter: AsyncMock,
    today: date,
    broken: dict[str, list[datetime]],
) -> date:
    """Run one update cycle whose import reports ``broken``; return the download start."""
    import_meter.return_value = ImportResult(None, broken)
    await coord._update_meter(METER, today, None)
    return coord.client.download_usage.call_args.args[0]


def _errors(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]


def _freeze_today(monkeypatch: pytest.MonkeyPatch, today: date) -> None:
    """Pin ``date.today()`` as the coordinator module sees it, so a full
    ``_async_update_data()`` cycle is deterministic (``_update_meter`` takes
    ``today`` as a parameter and doesn't need this)."""

    class _FixedDate(date):
        @classmethod
        def today(cls) -> date:
            return today

    monkeypatch.setattr(coordinator_module, "date", _FixedDate)


async def test_DW_2_3_reported_drop_heals_next_cycle_from_oldest_date(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    coord, import_meter, _ = _healing_coordinator(hass, monkeypatch)

    # Cycle 1: the startup scan is clean, but the import's own check reports a
    # drop → warning, heal queued for the next cycle.
    assert await _cycle(coord, import_meter, TODAY, {CONS: [T1]}) == NORMAL_FROM
    assert coord._oldest == {METER: OLDEST}
    assert coord._heal_pending == {METER}
    assert (
        f"Meter {METER}: 1 statistics sum drop(s) detected (first: {CONS} at "
        "2026-07-10 04:00:00+00:00); re-importing its full history on the next update"
    ) in caplog.text

    # Cycle 2: the heal — full history from the oldest stored date. Its own
    # check still sees the pre-heal data: no error, nothing re-queued.
    assert await _cycle(coord, import_meter, TODAY, {CONS: [T1]}) == OLDEST
    assert _errors(caplog) == []
    assert coord._heal_pending == set()
    assert coord._last_heal == {METER: TODAY}
    assert coord._pre_heal == {METER: {(CONS, T1)}}

    # Cycle 3: healed — a clean report on the normal window, nothing remembered.
    assert await _cycle(coord, import_meter, TODAY, {}) == NORMAL_FROM
    assert _errors(caplog) == []
    assert coord._pre_heal == {}
    assert coord._unhealable == set()


async def test_DW_2_3_persistent_drop_errors_once_and_is_never_rehealed(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    coord, import_meter, _ = _healing_coordinator(hass, monkeypatch)
    await _cycle(coord, import_meter, TODAY, {CONS: [T1]})  # detect
    assert await _cycle(coord, import_meter, TODAY, {CONS: [T1]}) == OLDEST  # heal

    # Still there after the heal → one ERROR naming the series and hour.
    assert await _cycle(coord, import_meter, TODAY, {CONS: [T1]}) == NORMAL_FROM
    assert _errors(caplog) == [
        f"{CONS} still has a sum drop at 2026-07-10 04:00:00+00:00 after a full "
        "re-import from the portal; it will not be re-imported for this again until restart"
    ]
    assert coord._unhealable == {(CONS, T1)}
    assert coord._heal_pending == set()

    # Reported again today and tomorrow: no new error, no heal.
    for day in (TODAY, TODAY + timedelta(days=1)):
        assert await _cycle(coord, import_meter, day, {CONS: [T1]}) == NORMAL_FROM
    assert len(_errors(caplog)) == 1
    assert coord._heal_pending == set()
    assert coord._last_heal == {METER: TODAY}


async def test_DW_2_3_new_drop_same_day_defers_to_tomorrow(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    coord, import_meter, _ = _healing_coordinator(hass, monkeypatch)
    await _cycle(coord, import_meter, TODAY, {CONS: [T1]})  # detect
    assert await _cycle(coord, import_meter, TODAY, {CONS: [T1]}) == OLDEST  # heal

    # A different drop the same day: queued for tomorrow, no error.
    assert await _cycle(coord, import_meter, TODAY, {CONS: [T2]}) == NORMAL_FROM
    assert _errors(caplog) == []
    assert coord._heal_pending == {METER}
    assert "re-importing its full history tomorrow (already re-imported once today)" in caplog.text

    # Later cycles today stay on the normal window and don't warn again.
    warnings = len([r for r in caplog.records if r.levelno == logging.WARNING])
    assert await _cycle(coord, import_meter, TODAY, {CONS: [T2]}) == NORMAL_FROM
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == warnings

    # Tomorrow's first cycle heals.
    tomorrow = TODAY + timedelta(days=1)
    assert await _cycle(coord, import_meter, tomorrow, {CONS: [T2]}) == OLDEST
    assert coord._last_heal == {METER: tomorrow}
    assert coord._heal_pending == set()
    assert _errors(caplog) == []


async def test_startup_scan_heals_in_the_first_refresh_and_runs_once(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    scan = {CONS: SeriesScan(FIRST_START, [T1]), "enova_power:energy_tier2_111": SeriesScan(None, [])}
    coord, import_meter, scan_series = _healing_coordinator(hass, monkeypatch, scan=scan)

    # First refresh: the scan's drop is healed right away, from the oldest date.
    assert await _cycle(coord, import_meter, TODAY, {CONS: [T1]}) == OLDEST
    assert "re-importing its full history now" in caplog.text
    assert coord._oldest == {METER: OLDEST}
    assert coord._last_heal == {METER: TODAY}
    assert coord._pre_heal == {METER: {(CONS, T1)}}

    # The next cycle is normal and does not scan again.
    assert await _cycle(coord, import_meter, TODAY, {}) == NORMAL_FROM
    assert scan_series.await_count == 1
    assert _errors(caplog) == []


async def test_heal_stays_pending_when_the_download_fails(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    coord, import_meter, _ = _healing_coordinator(hass, monkeypatch)
    await _cycle(coord, import_meter, TODAY, {CONS: [T1]})

    coord.client.download_usage.side_effect = EnovaError("portal down")
    with pytest.raises(UpdateFailed):
        await coord._async_update_data()
    assert coord._heal_pending == {METER}
    assert coord._last_heal == {}

    # The retry heals.
    coord.client.download_usage.side_effect = None
    assert await _cycle(coord, import_meter, TODAY, {CONS: [T1]}) == OLDEST


async def test_heal_without_oldest_date_uses_backfill_window(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The scan found no rows for any series → the oldest date is unknown.
    coord, import_meter, _ = _healing_coordinator(hass, monkeypatch, scan={CONS: SeriesScan(None, [])})
    await _cycle(coord, import_meter, TODAY, {CONS: [T1]})
    assert coord._oldest == {}
    assert await coord._full_reimport_from(METER) is None

    backfill = cycle_start_containing([], fetch_from_date(None, TODAY))
    assert backfill == date(2025, 7, 1)
    assert await _cycle(coord, import_meter, TODAY, {CONS: [T1]}) == backfill


async def test_first_import_uses_configured_backfill_months(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Nothing stored yet: the download reaches back the entry's chosen depth.
    coord, import_meter, _ = _healing_coordinator(
        hass, monkeypatch, data={CONF_BACKFILL_MONTHS: 16}
    )
    monkeypatch.setattr(
        coordinator_module, "async_last_statistic_start", AsyncMock(return_value=None)
    )
    expected = cycle_start_containing([], TODAY - timedelta(days=16 * 31))
    assert await _cycle(coord, import_meter, TODAY, {}) == expected
    assert expected < cycle_start_containing([], fetch_from_date(None, TODAY))


async def test_heal_window_never_shrinks_the_normal_window(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An oldest stored date *later* than this cycle's normal start (a series
    # trimmed by the recorder's purge, say) must not pull the window forward.
    scan = {CONS: SeriesScan(datetime(2026, 7, 5, 4, tzinfo=timezone.utc), [])}
    coord, import_meter, _ = _healing_coordinator(hass, monkeypatch, scan=scan)
    await _cycle(coord, import_meter, TODAY, {CONS: [T1]})
    assert await _cycle(coord, import_meter, TODAY, {CONS: [T1]}) == NORMAL_FROM


async def test_missing_series_refetches_full_history(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A series added by an upgrade has no rows → one full backfill, no heal.
    coord, import_meter, _ = _healing_coordinator(hass, monkeypatch)
    monkeypatch.setattr(
        coordinator_module, "async_missing_series", AsyncMock(return_value=["enova_power:new"])
    )
    assert await _cycle(coord, import_meter, TODAY, {}) == date(2025, 7, 1)
    assert f"Meter {METER} gained 1 statistics series; refetching full history" in caplog.text
    assert coord._heal_pending == set()


async def test_rebuild_cycle_routes_through_full_reimport(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A pending STATS_VERSION repair forces the meter's first cycle to heal
    # from its oldest stored date, via the same scan + full-reimport machinery
    # as a detected drop — no clear, no separate rebuild path.
    coord, import_meter, scan_series = _healing_coordinator(hass, monkeypatch, stats_version=1)
    from_date = await _cycle(coord, import_meter, TODAY, {})
    scan_series.assert_awaited_once()
    assert from_date == OLDEST
    assert coord._last_heal == {METER: TODAY}
    assert coord._heal_pending == set()


async def test_DW_3_2_rebuild_stamps_version_on_success(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    coord, import_meter, scan_series = _healing_coordinator(hass, monkeypatch, stats_version=4)
    _freeze_today(monkeypatch, TODAY)
    await coord._async_update_data()
    assert coord.config_entry.data[CONF_STATS_VERSION] == STATS_VERSION
    assert coord.client.download_usage.call_args.args[0] == OLDEST
    scan_series.assert_awaited_once()


async def test_DW_3_2_rebuild_not_stamped_when_cycle_fails(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    coord, _import_meter, _scan_series = _healing_coordinator(
        hass, monkeypatch, stats_version=4
    )
    _freeze_today(monkeypatch, TODAY)
    coord.client.download_usage.side_effect = EnovaError("portal down")
    with pytest.raises(UpdateFailed):
        await coord._async_update_data()
    assert coord.config_entry.data.get(CONF_STATS_VERSION, 1) == 4


async def test_DW_3_3_rebuild_reaches_past_backfill_window_for_old_history(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Stored history predates the 12-month backfill window (the user's real
    # case: rows back to 2025-07-01, older than DEFAULT_BACKFILL_MONTHS would reach on
    # its own). The repair must still start from the oldest stored date, not
    # get clamped to the backfill window.
    old_first_start = datetime(2024, 1, 1, 5, tzinfo=timezone.utc)
    scan = {CONS: SeriesScan(old_first_start, [])}
    coord, import_meter, _ = _healing_coordinator(
        hass, monkeypatch, scan=scan, stats_version=1
    )
    assert await _cycle(coord, import_meter, TODAY, {}) == old_first_start.date()
    backfill_limit = TODAY - timedelta(days=DEFAULT_BACKFILL_MONTHS * 31)
    assert old_first_start.date() < backfill_limit


async def test_DW_3_3_fresh_install_backfills_and_stamps_version(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No stored rows anywhere: the scan finds nothing, so the repair falls
    # back to a normal backfill — and still stamps the version on success.
    coord, import_meter, scan_series = _healing_coordinator(
        hass, monkeypatch, scan={CONS: SeriesScan(None, [])}, stats_version=4
    )
    monkeypatch.setattr(
        coordinator_module, "async_last_statistic_start", AsyncMock(return_value=None)
    )
    _freeze_today(monkeypatch, TODAY)
    await coord._async_update_data()
    scan_series.assert_awaited_once()
    backfill = cycle_start_containing([], fetch_from_date(None, TODAY))
    assert coord.client.download_usage.call_args.args[0] == backfill
    assert coord.config_entry.data[CONF_STATS_VERSION] == STATS_VERSION
