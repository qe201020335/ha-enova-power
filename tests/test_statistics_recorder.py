"""End-to-end statistics import tests against a real recorder.

Reproduces the production sequence reported broken after v0.5.8: a format
rebuild (fresh import of complete history), followed by incremental cycles as
new days publish — asserting the incrementally-arriving days land as hourly
rows with continuous sums, not daily lumps.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock

import pytest
from enovapower import BillingPeriod

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.enova_power.const import (
    CONF_STATS_VERSION,
    PLAN_TIERED,
    PLAN_TOU,
    TIME_ZONE,
)
from custom_components.enova_power.statistics import (
    STATS_VERSION,
    async_import_meter,
    async_scan_series,
    bucket_cost_statistic_id,
    bucket_statistic_id,
    consumption_statistic_id,
    days_covered,
    download_window,
    expected_statistic_ids,
    tiered_rates,
)

from .test_coordinator import _coordinator
from .test_statistics import TIER_RATES, TOU_RATES, _reading

METER = "111"


@pytest.fixture
def mock_recorder_before_hass(async_test_recorder):
    """Prepare the recorder database before the hass fixture starts.

    The plugin's ``hass`` fixture depends on this hook; recorder tests must
    override it (chaining ``async_test_recorder`` → ``recorder_db_url``) or
    ``recorder_db_url`` asserts that hass was created first.
    """


def _full_day(day: date, kwh: float = 1.0):
    return _reading(day, **{f"h{i:02d}": kwh for i in range(1, 25)})


async def _import(
    hass: HomeAssistant,
    readings: list,
    from_date: date,
    today: date,
    *,
    plan: str = PLAN_TOU,
    rates: list = TOU_RATES,
    tiered=None,
    periods: list[BillingPeriod] | None = None,
    rebuild: bool = False,
) -> None:
    """Import ``readings`` as the coordinator would for a download of ``from_date..today``."""
    await async_import_meter(
        hass, METER, readings, plan, rates, tiered, periods or [], "CAD",
        window=download_window(from_date, today),
        covered_days=days_covered(readings),
        rebuild=rebuild,
    )
    await async_wait_recording_done(hass)


async def _assert_no_drops(hass: HomeAssistant, statistic_ids: list[str]) -> None:
    for statistic_id in statistic_ids:
        sums = [r["sum"] for r in await _hourly_rows(hass, statistic_id)]
        drops = [(a, b) for a, b in zip(sums, sums[1:], strict=False) if b < a - 1e-9]
        assert not drops, f"{statistic_id} has sum drops: {drops}"


async def _hourly_rows(hass: HomeAssistant, statistic_id: str) -> list[dict]:
    start = datetime(2026, 6, 30, 0, tzinfo=timezone.utc)
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        start,
        None,
        {statistic_id},
        "hour",
        None,
        {"sum"},
    )
    return stats.get(statistic_id, [])


async def test_incremental_day_lands_hourly_after_rebuild(
    recorder_mock, hass: HomeAssistant
) -> None:
    day1, day2 = date(2026, 7, 1), date(2026, 7, 2)

    # Cycle 1 — the format rebuild: fresh import of complete history.
    await _import(hass, [_full_day(day1)], day1, day1, rebuild=True)
    assert len(await _hourly_rows(hass, consumption_statistic_id(METER))) == 24

    # Cycle 2 — incremental: the window still contains day 1; day 2 is new.
    await _import(hass, [_full_day(day1), _full_day(day2)], day1, day2)

    rows = await _hourly_rows(hass, consumption_statistic_id(METER))
    # Both days must be hourly — 48 rows with a continuous +1 kWh/h sum, no
    # daily lumps and no double-imported day-1 rows.
    assert len(rows) == 48
    assert [r["sum"] for r in rows] == [float(i) for i in range(1, 49)]

    # The TOU bucket series must also have gained hourly rows for the new day.
    bucket_rows = await _hourly_rows(hass, bucket_statistic_id(METER, "tou_off_peak"))
    day2_start = datetime(2026, 7, 2, 4, tzinfo=timezone.utc).timestamp()  # 00:00 EDT
    assert sum(1 for r in bucket_rows if r["start"] >= day2_start) == 12  # off-peak hours


async def test_preliminary_day_revision_heals(recorder_mock, hass: HomeAssistant) -> None:
    """The reported v0.5.8 bug: a day first publishes as a preliminary row with
    the whole total in the midnight slot and explicit zeros elsewhere, then is
    revised to real hourly values a day later. The revision must overwrite the
    preliminary rows — not be silently discarded, leaving the day's entire
    energy lumped in the 12am-1am bucket forever."""
    day1, day2 = date(2026, 7, 1), date(2026, 7, 2)

    # Cycle 1: day 1 complete; day 2 first sighting — 24 kWh in h01, zeros after.
    preliminary = _reading(
        day2, **{**{f"h{i:02d}": 0.0 for i in range(1, 25)}, "h01": 24.0}
    )
    await _import(hass, [_full_day(day1), preliminary], day1, day2)

    # Cycle 2 (next day): the portal has revised day 2 into real hourly values.
    await _import(hass, [_full_day(day1), _full_day(day2)], day1, day2)

    rows = await _hourly_rows(hass, consumption_statistic_id(METER))
    assert len(rows) == 48
    # Continuous +1 kWh per hour across both days: the 24-kWh midnight lump is
    # gone and day 2's energy sits in its real hours.
    assert [r["sum"] for r in rows] == [float(i) for i in range(1, 49)]

    # The off-peak bucket healed too (the lump was classified off-peak).
    # July 1 is Canada Day — off-peak all 24 hours; July 2 (Thu) has 12.
    bucket_rows = await _hourly_rows(hass, bucket_statistic_id(METER, "tou_off_peak"))
    assert bucket_rows[-1]["sum"] == 36.0
    # Day 2's midnight row carries one real hour, not the 24-kWh lump.
    day2_midnight = datetime(2026, 7, 2, 4, tzinfo=timezone.utc).timestamp()
    (midnight_row,) = [r for r in bucket_rows if r["start"] == day2_midnight]
    assert midnight_row["sum"] == 25.0  # 24 holiday hours + 1


async def test_DW_1_1_absent_day_rebased_on_revised_window(
    recorder_mock, hass: HomeAssistant
) -> None:
    """Days 1–5 stored; the next download of the same window revises days 1–4
    up and omits day 5 entirely. Day 5's rows are on an uncovered day, so they
    keep their +1 kWh/h increments, re-based on the new day-4 end — and no
    series is left with a sum drop."""
    days = [date(2026, 7, d) for d in range(1, 6)]
    await _import(hass, [_full_day(d) for d in days], days[0], days[-1])
    rows = await _hourly_rows(hass, consumption_statistic_id(METER))
    assert [r["sum"] for r in rows] == [float(i) for i in range(1, 121)]

    await _import(hass, [_full_day(d, 2.0) for d in days[:4]], days[0], days[-1])

    rows = await _hourly_rows(hass, consumption_statistic_id(METER))
    assert len(rows) == 120
    # Days 1–4 rewritten at 2 kWh/h (ends at 192); day 5 keeps +1/h on top.
    assert [r["sum"] for r in rows[:96]] == [float(2 * i) for i in range(1, 97)]
    assert [r["sum"] for r in rows[96:]] == [192.0 + i for i in range(1, 25)]

    # A bucket series: day 5 (Sun, off-peak all day) keeps its 24 rows at +1/h
    # after the revised days 1–4 (Jul 1 holiday + Jul 4–5 weekend are all
    # off-peak; Jul 2–3 have 12 off-peak hours each → 72 h × 2 kWh = 144).
    bucket_rows = await _hourly_rows(hass, bucket_statistic_id(METER, "tou_off_peak"))
    day5_start = datetime(2026, 7, 5, 4, tzinfo=timezone.utc).timestamp()
    day5 = [r["sum"] for r in bucket_rows if r["start"] >= day5_start]
    assert day5 == [144.0 + i for i in range(1, 25)]

    await _assert_no_drops(hass, expected_statistic_ids(METER, PLAN_TOU, TOU_RATES, None))


async def test_DW_1_2_omitted_tier2_hour_becomes_zero_increment(
    recorder_mock, hass: HomeAssistant
) -> None:
    """A tier2 row lands at the hour the cycle crosses 600 kWh. When the next
    import revises the day below the threshold, tier2 has no points at all;
    its stored row is on a covered in-window day, so it must become a
    zero-increment row rather than a stale plateau."""
    periods = [BillingPeriod(date(2026, 6, 30), date(2026, 7, 31), 31, 0.0, 0.0)]
    tiered = tiered_rates(TIER_RATES)
    day1, day2 = date(2026, 7, 1), date(2026, 7, 2)
    crossing = datetime(2026, 7, 2, 16, tzinfo=timezone.utc).timestamp()  # h13 EDT

    await _import(
        hass, [_reading(day1, h13=500.0), _reading(day2, h13=200.0)], day1, day2,
        plan=PLAN_TIERED, rates=TIER_RATES, tiered=tiered, periods=periods,
    )
    tier2 = await _hourly_rows(hass, bucket_statistic_id(METER, "tier2"))
    assert [(r["start"], r["sum"]) for r in tier2] == [(crossing, 100.0)]

    # Revision: day 2 drops to 50 kWh → 550 for the cycle, under the threshold.
    await _import(
        hass, [_reading(day1, h13=500.0), _reading(day2, h13=50.0)], day1, day2,
        plan=PLAN_TIERED, rates=TIER_RATES, tiered=tiered, periods=periods,
    )

    tier2 = await _hourly_rows(hass, bucket_statistic_id(METER, "tier2"))
    assert [(r["start"], r["sum"]) for r in tier2] == [(crossing, 0.0)]
    tier1 = await _hourly_rows(hass, bucket_statistic_id(METER, "tier1"))
    assert [r["sum"] for r in tier1] == [500.0, 550.0]
    # The paired cost series follows the same shape.
    cost_tier2 = await _hourly_rows(hass, bucket_cost_statistic_id(METER, "tier2"))
    assert [r["sum"] for r in cost_tier2] == [0.0]
    await _assert_no_drops(
        hass, expected_statistic_ids(METER, PLAN_TIERED, TIER_RATES, tiered)
    )


# --- integrity check + heal ----------------------------------------------------- #

JUL1, JUL2, JUL10, JUL15 = (date(2026, 7, d) for d in (1, 2, 10, 15))
JUL10_MIDNIGHT = datetime(2026, 7, 10, 4, tzinfo=timezone.utc)  # 00:00 EDT
TOU_IDS = expected_statistic_ids(METER, PLAN_TOU, TOU_RATES, None)
# 30 kWh/h crosses the 600 kWh tier threshold on day one, so tier2 has rows
# like a real meter's (an empty expected series would trigger the
# missing-series full refetch instead of exercising the heal).
KWH = 30.0


def _chain(hours: int, base: float = 0.0) -> list[float]:
    return [base + KWH * i for i in range(1, hours + 1)]


async def _plant_july_pattern(hass: HomeAssistant) -> None:
    """Reproduce the July incident: Jul 1–2 stored as one chain, Jul 10
    imported on a zero base (its sum restarts), Jul 15 appended after it.
    Every series with a Jul 10 row has its sum fall there."""
    await _import(hass, [_full_day(JUL1, KWH), _full_day(JUL2, KWH)], JUL1, JUL2)
    await _import(hass, [_full_day(JUL10, KWH)], JUL10, JUL10, rebuild=True)
    await _import(hass, [_full_day(JUL15, KWH)], JUL15, JUL15)
    rows = await _hourly_rows(hass, consumption_statistic_id(METER))
    assert [r["sum"] for r in rows] == _chain(48) + _chain(48)


async def test_DW_2_2_planted_july_pattern_reported_broken(
    recorder_mock, hass: HomeAssistant
) -> None:
    await _plant_july_pattern(hass)

    readings = [_full_day(d, KWH) for d in (JUL1, JUL2, JUL10, JUL15)]
    result = await async_import_meter(
        hass, METER, readings, PLAN_TOU, TOU_RATES, None, [], "CAD",
        window=download_window(JUL1, JUL15), covered_days=days_covered(readings),
    )
    await async_wait_recording_done(hass)

    # Every series with a Jul 10 row reports exactly one drop, at its first
    # hour that day. ULO off-peak only has rows on weekends/holidays (Canada
    # Day) — nothing on Jul 10, so nothing to fall.
    assert set(result.broken) == set(TOU_IDS) - {bucket_statistic_id(METER, "ulo_off_peak")}
    for drops in result.broken.values():
        assert len(drops) == 1
        assert drops[0].astimezone(TIME_ZONE).date() == JUL10
    assert result.broken[consumption_statistic_id(METER)] == [JUL10_MIDNIGHT]
    assert result.broken[bucket_statistic_id(METER, "tou_off_peak")] == [JUL10_MIDNIGHT]
    assert result.broken[bucket_cost_statistic_id(METER, "tou_off_peak")] == [JUL10_MIDNIGHT]
    assert result.broken[bucket_statistic_id(METER, "tou_on_peak")] == [
        datetime(2026, 7, 10, 15, tzinfo=timezone.utc)  # 11:00 EDT, first on-peak hour
    ]
    # The rewrite over the window put the chain back together.
    assert result.total == 96 * KWH
    rows = await _hourly_rows(hass, consumption_statistic_id(METER))
    assert [r["sum"] for r in rows] == _chain(96)
    await _assert_no_drops(hass, TOU_IDS)


async def test_scan_series_against_recorder(recorder_mock, hass: HomeAssistant) -> None:
    await _plant_july_pattern(hass)
    scans = await async_scan_series(hass, [*TOU_IDS, "enova_power:energy_never_111"])
    consumption = scans[consumption_statistic_id(METER)]
    assert consumption.first_start == datetime(2026, 7, 1, 4, tzinfo=timezone.utc)
    assert consumption.drops == [JUL10_MIDNIGHT]
    assert scans["enova_power:energy_never_111"].first_start is None
    assert scans["enova_power:energy_never_111"].drops == []


async def test_DW_2_4_startup_scan_heals_break_outside_window(
    recorder_mock, hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    await _plant_july_pattern(hass)
    # A cycle closed Jul 12, so a normal window starts Jul 13 — after the break.
    period = BillingPeriod(date(2026, 6, 30), date(2026, 7, 12), 12, 0.0, 0.0)
    coord = _coordinator(hass, detected=PLAN_TOU, data={CONF_STATS_VERSION: STATS_VERSION})
    coord.rates = TOU_RATES
    coord.client.billing_periods = AsyncMock(return_value=[period])
    published = {d: _full_day(d, KWH) for d in (JUL1, JUL2, JUL10, JUL15)}

    async def download_usage(from_date: date, to_date: date, *, meter_id: str) -> list:
        return [r for d, r in published.items() if from_date <= d <= to_date]

    coord.client.download_usage = AsyncMock(side_effect=download_usage)
    today = date(2026, 7, 20)

    # First refresh: the scan finds the break, caches the oldest date, and the
    # same cycle re-imports everything from it.
    await coord._update_meter(METER, today, None)
    await async_wait_recording_done(hass)

    assert coord._oldest == {METER: JUL1}
    assert coord.client.download_usage.call_args.args[:2] == (JUL1, today)
    assert "re-importing its full history now" in caplog.text
    assert coord._last_heal == {METER: today}
    assert coord._heal_pending == set()
    assert (consumption_statistic_id(METER), JUL10_MIDNIGHT) in coord._pre_heal[METER]
    rows = await _hourly_rows(hass, consumption_statistic_id(METER))
    assert [r["sum"] for r in rows] == _chain(96)
    await _assert_no_drops(hass, TOU_IDS)

    # The next cycle is back on the normal window (Jul 13 →, overlapping the
    # stored Jul 15 rows so the check runs), finds the chain clean: no error.
    await coord._update_meter(METER, today, None)
    await async_wait_recording_done(hass)
    assert coord.client.download_usage.call_args.args[:2] == (date(2026, 7, 13), today)
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert coord._pre_heal == {}
    assert coord._unhealable == set()
    assert coord._heal_pending == set()
