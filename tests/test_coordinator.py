"""Tests for the coordinator's download-window selection and heal state machine."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from enovapower import BillingPeriod, EnovaError

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.enova_power.const import (
    BACKFILL_MONTHS,
    CONF_PLAN,
    CONF_STATS_VERSION,
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
    assert fetch_from_date(None, today) == today - timedelta(days=BACKFILL_MONTHS * 31)


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
        await coord._update_meter("111", today, None)
        return client.download_usage.call_args.args[:2]

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
    assert kwargs["rebuild"] is False


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
) -> tuple[EnovaPowerCoordinator, AsyncMock, AsyncMock]:
    """A coordinator with every recorder seam stubbed; statistics are stored
    through yesterday and the startup scan returns ``scan`` (clean by default).
    Returns it with the ``async_import_meter`` and ``async_scan_series`` mocks."""
    coord = _coordinator(hass, detected=PLAN_TOU, data={CONF_STATS_VERSION: stats_version})
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
    assert import_meter.call_args.kwargs["rebuild"] is False


async def test_rebuild_cycle_skips_the_startup_scan(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The queued clear may not have run yet, so stored rows can't be checked.
    coord, import_meter, scan_series = _healing_coordinator(hass, monkeypatch, stats_version=1)
    from_date = await _cycle(coord, import_meter, TODAY, {})
    scan_series.assert_not_awaited()
    assert from_date == cycle_start_containing([], fetch_from_date(None, TODAY))
    assert import_meter.call_args.kwargs["rebuild"] is True
