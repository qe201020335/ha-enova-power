"""Tests for the statistics importer's pure logic (no recorder needed)."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from enovapower import BillingPeriod, TariffRate, UsageReading

from custom_components.enova_power.const import (
    PERIOD_MID_PEAK,
    PERIOD_OFF_PEAK,
    PERIOD_ON_PEAK,
    PERIOD_ULO_OVERNIGHT,
    PLAN_TIERED,
    PLAN_TOU,
    PLAN_ULO,
    TIME_ZONE,
)
import custom_components.enova_power.statistics as statistics_module
from custom_components.enova_power.statistics import (
    ImportResult,
    SeriesResult,
    SeriesScan,
    TieredRates,
    _async_import_series,
    _cycle_key,
    _merge_statistics,
    _missing_series,
    _period_hourly,
    _scan_series,
    _tier_hourly,
    bucket_cost_points,
    bucket_cost_statistic_id,
    bucket_points,
    bucket_statistic_id,
    consumption_statistic_id,
    cost_if_statistic_id,
    cost_points,
    cost_statistic_id,
    cost_total,
    days_covered,
    download_window,
    expected_statistic_ids,
    find_sum_drops,
    plan_prices,
    rebuild_statistic_ids,
    season_threshold,
    tiered_rates,
)


def _reading(day: date, **hours: float) -> UsageReading:
    hourly: dict[str, float | None] = {f"h{i:02d}": None for i in range(1, 25)}
    hourly.update(hours)
    return UsageReading(date=day, hourly=hourly)


def _tou_rate(name: str, price: float, plan: str = "Time-of-Use") -> TariffRate:
    return TariffRate(
        start_date=date(2026, 5, 1),
        end_date=date(2026, 10, 31),
        plan=plan,
        name=name,
        price=price,
    )


TOU_RATES = [
    _tou_rate("TOU Off-peak", 9.8),
    _tou_rate("TOU Mid-peak", 15.7),
    _tou_rate("TOU On-peak", 20.3),
]
TIER_RATES = [
    TariffRate(date(2026, 5, 1), date(2026, 10, 31), "Tiered", "Tier 1", 10.0),
    TariffRate(date(2026, 5, 1), date(2026, 10, 31), "Tiered", "Tier 2", 20.0),
]

# Unit-test download window: local Jan 1 2026 = 05:00 UTC through 04:00 UTC
# on Jan 2 (EST). Rows at 04:00 UTC Jan 1 are before it; 05:00 UTC Jan 2 after.
JAN1 = date(2026, 1, 1)
WINDOW = download_window(JAN1, JAN1)


def _utc(hour: int, day: int = 1) -> datetime:
    return datetime(2026, 1, day, hour, tzinfo=timezone.utc)


def _covered(*days: date):
    """A ``covered`` predicate for the merge: local date of the hour in ``days``."""
    return lambda start: start.astimezone(TIME_ZONE).date() in days


# --- IDs -------------------------------------------------------------------- #


async def test_statistic_ids() -> None:
    assert consumption_statistic_id("111") == "enova_power:energy_consumption_111"
    assert bucket_statistic_id("111", "tou_on_peak") == "enova_power:energy_tou_on_peak_111"
    assert bucket_statistic_id("111", "tier1") == "enova_power:energy_tier1_111"
    assert bucket_cost_statistic_id("111", "tou_on_peak") == "enova_power:cost_tou_on_peak_111"
    assert bucket_cost_statistic_id("111", "tier1") == "enova_power:cost_tier1_111"
    assert cost_statistic_id("111") == "enova_power:energy_cost_111"
    assert cost_if_statistic_id("111", PLAN_ULO) == "enova_power:cost_if_ulo_111"


# --- rates ------------------------------------------------------------------ #


async def test_plan_prices_tou() -> None:
    assert plan_prices(TOU_RATES, PLAN_TOU) == {
        PERIOD_OFF_PEAK: 9.8,
        PERIOD_MID_PEAK: 15.7,
        PERIOD_ON_PEAK: 20.3,
    }


async def test_tiered_rates() -> None:
    assert tiered_rates(TIER_RATES) == TieredRates(tier1=10.0, tier2=20.0)
    assert tiered_rates(TIER_RATES[:1]) is None  # missing Tier 2


async def test_season_threshold() -> None:
    assert season_threshold(date(2026, 7, 1)) == 600.0  # summer
    assert season_threshold(date(2026, 1, 1)) == 1000.0  # winter


# --- classification (local Ontario clock) ------------------------------------ #


async def test_period_hourly_tou_summer_weekday() -> None:
    # 2026-06-01 is a Monday. h01=00:00 local (off), h09=08:00 (mid), h13=12:00 (on).
    r = _reading(date(2026, 6, 1), h01=1.0, h09=3.0, h13=2.0)
    hourly = _period_hourly([r], PLAN_TOU)
    # Points are hourly, timestamped at the interval start; June is EDT (UTC-4).
    assert hourly[PERIOD_OFF_PEAK] == [(datetime(2026, 6, 1, 4, tzinfo=timezone.utc), 1.0)]
    assert hourly[PERIOD_MID_PEAK] == [(datetime(2026, 6, 1, 12, tzinfo=timezone.utc), 3.0)]
    assert hourly[PERIOD_ON_PEAK] == [(datetime(2026, 6, 1, 16, tzinfo=timezone.utc), 2.0)]


async def test_period_hourly_ulo_overnight() -> None:
    # h02 = 01:00 local → ULO overnight (23:00-07:00); h18 = 17:00 → ULO on-peak (16-21).
    r = _reading(date(2026, 6, 1), h02=4.0, h18=5.0)
    hourly = _period_hourly([r], PLAN_ULO)
    assert hourly[PERIOD_ULO_OVERNIGHT] == [(datetime(2026, 6, 1, 5, tzinfo=timezone.utc), 4.0)]
    assert hourly[PERIOD_ON_PEAK] == [(datetime(2026, 6, 1, 21, tzinfo=timezone.utc), 5.0)]


async def test_period_hourly_winter_matches_est() -> None:
    # In winter EST = local, so h18 (17:00) lands at 22:00 UTC — the winter
    # on-peak window the user verified in HA.
    r = _reading(date(2026, 1, 15), h18=2.0)  # Thursday, winter on-peak 17-19
    hourly = _period_hourly([r], PLAN_TOU)
    assert hourly[PERIOD_ON_PEAK] == [(datetime(2026, 1, 15, 22, tzinfo=timezone.utc), 2.0)]


async def test_period_hourly_keeps_hours_separate() -> None:
    # Two off-peak hours on the same day stay two points — no day aggregation.
    r = _reading(date(2026, 6, 1), h01=1.0, h02=2.0)
    hourly = _period_hourly([r], PLAN_TOU)
    assert [kwh for _, kwh in hourly[PERIOD_OFF_PEAK]] == [1.0, 2.0]


async def test_bucket_points_has_all_keys() -> None:
    r = _reading(date(2026, 6, 1), h13=2.0)
    buckets = bucket_points([r], [])
    assert {"tou_on_peak", "ulo_on_peak", "tier1", "tier2"} <= set(buckets)
    assert buckets["tou_on_peak"][0][1] == 2.0


# --- billing cycle grouping ------------------------------------------------- #


async def test_cycle_key_uses_billing_period() -> None:
    periods = [BillingPeriod(date(2026, 5, 19), date(2026, 6, 19), 31, 0.0, 0.0)]
    assert _cycle_key(date(2026, 6, 1), periods) == ("cycle", date(2026, 6, 19))
    # Outside any cycle → calendar-month fallback.
    assert _cycle_key(date(2026, 8, 1), periods) == ("month", 2026, 8)


async def test_tier_hourly_crosses_threshold_in_cycle() -> None:
    periods = [BillingPeriod(date(2026, 5, 31), date(2026, 6, 30), 30, 0.0, 0.0)]
    readings = [
        _reading(date(2026, 6, 1), h13=500.0),  # under 600
        _reading(date(2026, 6, 2), h13=200.0),  # crosses 600 → 100 t1 + 100 t2
    ]
    tiers = _tier_hourly(readings, periods)
    assert [k for _, k in tiers["tier1"]] == pytest.approx([500.0, 100.0])
    # Zero-contribution hours are omitted: tier2 only starts at the crossing.
    assert tiers["tier2"] == [(datetime(2026, 6, 2, 16, tzinfo=timezone.utc), 100.0)]


async def test_tier_hourly_splits_within_a_day() -> None:
    # The crossing lands in the exact hour it happens, not smeared over the day.
    periods = [BillingPeriod(date(2026, 5, 31), date(2026, 6, 30), 30, 0.0, 0.0)]
    readings = [_reading(date(2026, 6, 1), h01=590.0, h02=20.0, h03=5.0)]
    tiers = _tier_hourly(readings, periods)
    assert [k for _, k in tiers["tier1"]] == pytest.approx([590.0, 10.0])
    assert [k for _, k in tiers["tier2"]] == pytest.approx([10.0, 5.0])
    assert tiers["tier2"][0][0] == datetime(2026, 6, 1, 5, tzinfo=timezone.utc)  # h02, EDT


# --- cost ------------------------------------------------------------------- #


async def test_cost_points_tou_hourly() -> None:
    r = _reading(date(2026, 6, 1), h01=10.0, h13=5.0)  # off 10, on 5
    points = cost_points([r], PLAN_TOU, TOU_RATES, None, [])
    # One point per hour: 10 × 9.8¢ at h01, 5 × 20.3¢ at h13 (June = EDT).
    assert points == [
        (datetime(2026, 6, 1, 4, tzinfo=timezone.utc), pytest.approx(0.98)),
        (datetime(2026, 6, 1, 16, tzinfo=timezone.utc), pytest.approx(1.015)),
    ]


async def test_cost_total_tiered() -> None:
    periods = [BillingPeriod(date(2026, 5, 31), date(2026, 6, 30), 30, 0.0, 0.0)]
    readings = [
        _reading(date(2026, 6, 1), h13=500.0),
        _reading(date(2026, 6, 2), h13=200.0),
    ]
    tiered = TieredRates(tier1=10.0, tier2=20.0)
    # day1 500@10=$50; day2 100@10 + 100@20 = $30 → $80.
    assert cost_total(readings, PLAN_TIERED, [], tiered, periods) == pytest.approx(80.0)


async def test_cost_points_empty_without_rates() -> None:
    r = _reading(date(2026, 6, 1), h13=5.0)
    assert cost_points([r], PLAN_TOU, [], None, []) == []


# --- per-bucket cost ---------------------------------------------------------- #


async def test_bucket_cost_points_tou() -> None:
    r = _reading(date(2026, 6, 1), h01=10.0, h13=5.0)  # off 10, on 5
    buckets = bucket_cost_points([r], TOU_RATES, None, [])
    assert buckets["tou_off_peak"][0][1] == pytest.approx(0.98)  # 10 × 9.8¢
    assert buckets["tou_on_peak"][0][1] == pytest.approx(1.015)  # 5 × 20.3¢
    # No ULO rates and no tiered rates → those buckets are omitted entirely.
    assert not any(key.startswith("ulo_") for key in buckets)
    assert "tier1" not in buckets


async def test_bucket_cost_points_tiers() -> None:
    periods = [BillingPeriod(date(2026, 5, 31), date(2026, 6, 30), 30, 0.0, 0.0)]
    readings = [
        _reading(date(2026, 6, 1), h13=500.0),
        _reading(date(2026, 6, 2), h13=200.0),  # crosses 600 → 100 t1 + 100 t2
    ]
    buckets = bucket_cost_points(readings, [], TieredRates(tier1=10.0, tier2=20.0), periods)
    assert [c for _, c in buckets["tier1"]] == pytest.approx([50.0, 10.0])
    assert [c for _, c in buckets["tier2"]] == pytest.approx([20.0])


async def test_bucket_costs_sum_to_scheme_cost() -> None:
    # A scheme's bucket costs must always sum to its cost series.
    r = _reading(date(2026, 6, 1), h01=10.0, h09=3.0, h13=5.0)
    buckets = bucket_cost_points([r], TOU_RATES, None, [])
    bucket_total = sum(cost for points in buckets.values() for _, cost in points)
    scheme_total = sum(cost for _, cost in cost_points([r], PLAN_TOU, TOU_RATES, None, []))
    assert bucket_total == pytest.approx(scheme_total)


# --- upgrade detection (expected vs stored series) ---------------------------- #


async def test_expected_ids_without_rates() -> None:
    ids = expected_statistic_ids("111", PLAN_TOU, [], None)
    # Consumption + the 9 kWh buckets always; no cost ids without rates.
    assert len(ids) == 10
    assert consumption_statistic_id("111") in ids
    assert not any(":cost" in statistic_id for statistic_id in ids)


async def test_expected_ids_with_tou_rates() -> None:
    ids = expected_statistic_ids("111", PLAN_TOU, TOU_RATES, None)
    assert bucket_cost_statistic_id("111", "tou_on_peak") in ids
    assert cost_statistic_id("111") in ids  # active plan (TOU) is priced
    assert cost_if_statistic_id("111", PLAN_TOU) in ids
    # ULO rates and tiered rates unavailable → their cost ids are not expected.
    assert bucket_cost_statistic_id("111", "ulo_overnight") not in ids
    assert bucket_cost_statistic_id("111", "tier1") not in ids
    assert cost_if_statistic_id("111", PLAN_TIERED) not in ids


async def test_expected_ids_tiered_plan() -> None:
    ids = expected_statistic_ids("111", PLAN_TIERED, TIER_RATES, tiered_rates(TIER_RATES))
    assert bucket_cost_statistic_id("111", "tier1") in ids
    assert bucket_cost_statistic_id("111", "tier2") in ids
    assert cost_statistic_id("111") in ids
    assert cost_if_statistic_id("111", PLAN_TIERED) in ids
    # TOU prices missing → the active-cost id must not depend on them, but
    # TOU bucket costs and cost_if_tou are not expected.
    assert bucket_cost_statistic_id("111", "tou_on_peak") not in ids


async def test_missing_series(monkeypatch: pytest.MonkeyPatch) -> None:
    stored = {"enova_power:energy_consumption_111"}
    monkeypatch.setattr(
        statistics_module,
        "get_last_statistics",
        lambda hass, n, statistic_id, convert, types: (
            {statistic_id: [{"sum": 1.0}]} if statistic_id in stored else {}
        ),
    )
    ids = ["enova_power:energy_consumption_111", "enova_power:cost_tou_on_peak_111"]
    assert _missing_series(None, ids) == ["enova_power:cost_tou_on_peak_111"]


# --- statistics-format rebuild ------------------------------------------------ #


async def test_rebuild_ids_cover_every_series() -> None:
    ids = rebuild_statistic_ids("111")
    # consumption + 9 kWh buckets + 9 cost buckets + energy_cost + 3 cost_if.
    assert len(ids) == 23
    # v3 moves timestamps, so consumption rebuilds too.
    assert consumption_statistic_id("111") in ids
    assert bucket_statistic_id("111", "tou_on_peak") in ids
    assert bucket_cost_statistic_id("111", "tier2") in ids
    assert cost_statistic_id("111") in ids
    assert cost_if_statistic_id("111", PLAN_ULO) in ids
    # Rate-gating must not apply here: clear everything that may exist.
    assert set(ids) >= set(expected_statistic_ids("111", PLAN_TOU, [], None))


async def test_start_rebuild_queues_clear_for_all_meters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleared: list[str] = []

    class FakeRecorder:
        # Mirrors Recorder.async_clear_statistics: queues onto the recorder
        # thread. The rebuild must never wait on it — the recorder holds its
        # queue until HA has started, so a wait deadlocks bootstrap.
        def async_clear_statistics(self, ids):
            cleared.extend(ids)

    monkeypatch.setattr(statistics_module, "get_instance", lambda hass: FakeRecorder())

    statistics_module.async_start_rebuild(None, ["111", "222"])

    assert len(cleared) == 46
    assert consumption_statistic_id("111") in cleared
    assert bucket_statistic_id("111", "tier1") in cleared
    assert bucket_cost_statistic_id("222", "ulo_overnight") in cleared


async def test_import_series_fresh_ignores_stored_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A stale row exists (the queued clear hasn't executed yet); fresh=True
    # must neither filter against it nor resume from its sum.
    base = datetime(2026, 1, 1, 5, tzinfo=timezone.utc)
    written = _patch_import(monkeypatch, {"start": base.replace(hour=7), "sum": 500.0})
    points = [(base, 1.0), (base.replace(hour=6), 2.0)]

    result = await _async_import_series(
        None, "enova_power:x", "x", points, "kWh",
        window=WINDOW, covered_days={JAN1}, fresh=True,
    )

    assert result == SeriesResult(3.0, [])  # nothing read → nothing checked
    assert [s["sum"] for s in written] == [1.0, 3.0]


async def test_import_meter_rebuild_reimports_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale_row = {"start": datetime(2026, 6, 2, 0, tzinfo=timezone.utc), "sum": 500.0}
    written: dict[str, list[float]] = {}

    async def fake_last_row(hass, statistic_id):
        return stale_row

    monkeypatch.setattr(statistics_module, "_async_last_row", fake_last_row)
    monkeypatch.setattr(
        statistics_module,
        "async_add_external_statistics",
        lambda hass, metadata, stats: written.__setitem__(
            metadata["statistic_id"], [s["sum"] for s in stats]
        ),
    )

    readings = [_reading(date(2026, 6, 1), h01=10.0, h13=5.0)]
    result = await statistics_module.async_import_meter(
        None, "111", readings, PLAN_TOU, TOU_RATES, None, [], "CAD",
        window=download_window(date(2026, 6, 1), date(2026, 6, 1)),
        covered_days=days_covered(readings),
        rebuild=True,
    )

    # Every series — consumption included (its timestamps moved in v3) —
    # ignores the stale row (no stored-rows read either): full points, sums
    # restarting from zero.
    assert result == ImportResult(15.0, {})
    assert written[consumption_statistic_id("111")] == [10.0, 15.0]
    assert written[bucket_statistic_id("111", "tou_off_peak")] == [10.0]
    assert written[bucket_cost_statistic_id("111", "tou_on_peak")] == pytest.approx([1.015])


# --- window + covered days ----------------------------------------------------- #


async def test_download_window_is_local_midnight_to_last_hour() -> None:
    # Jan (EST, UTC-5): 00:00 local Jan 1 → 05:00 UTC; 23:00 local Jan 2 → 04:00 UTC Jan 3.
    assert download_window(date(2026, 1, 1), date(2026, 1, 2)) == (
        datetime(2026, 1, 1, 5, tzinfo=timezone.utc),
        datetime(2026, 1, 3, 4, tzinfo=timezone.utc),
    )
    # July (EDT, UTC-4).
    assert download_window(date(2026, 7, 1), date(2026, 7, 1)) == (
        datetime(2026, 7, 1, 4, tzinfo=timezone.utc),
        datetime(2026, 7, 2, 3, tzinfo=timezone.utc),
    )


async def test_days_covered_needs_a_published_hour() -> None:
    readings = [
        _reading(date(2026, 6, 1), h13=0.0),  # a real zero counts as published
        _reading(date(2026, 6, 2)),  # all None → the portal published nothing
    ]
    assert days_covered(readings) == {date(2026, 6, 1)}


async def test_stored_rows_normalizes_and_skips_unusable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list = []

    def fake_during_period(hass, start, end, ids, period, units, types):
        asked.append((start, end, ids, period, types))
        return {
            "enova_power:a": [
                {"start": _utc(6).timestamp(), "sum": 2.0},  # float seconds (recorder)
                {"start": _utc(5), "sum": 1.0},  # aware datetime
                {"start": _utc(7).replace(tzinfo=None), "sum": 3.0},  # naive → UTC
                {"start": None, "sum": 9.0},  # no start → skipped
                {"start": _utc(8).timestamp(), "sum": None},  # no sum → skipped
            ],
        }

    monkeypatch.setattr(statistics_module, "statistics_during_period", fake_during_period)
    rows = statistics_module._stored_rows(None, {"enova_power:a", "enova_power:b"}, _utc(5))
    # Ascending, UTC-aware, unusable rows dropped; ids with no rows are absent.
    assert rows == {"enova_power:a": [(_utc(5), 1.0), (_utc(6), 2.0), (_utc(7), 3.0)]}
    assert asked == [(_utc(5), None, {"enova_power:a", "enova_power:b"}, "hour", {"sum"})]


# --- merge (pure) -------------------------------------------------------------- #


async def test_DW_1_3_merge_zeroes_omitted_hour_on_covered_day() -> None:
    # Stored 06:00 is in the window on a covered day but the new points skip
    # it → zero increment; the other stored hours are replaced by the points.
    stored = [(_utc(5), 10.0), (_utc(6), 20.0), (_utc(7), 30.0)]
    points = [(_utc(5), 1.0), (_utc(7), 1.0)]
    stats = _merge_statistics(points, 0.0, stored, WINDOW, _covered(JAN1))
    assert [(s["start"], s["sum"]) for s in stats] == [
        (_utc(5), 1.0), (_utc(6), 1.0), (_utc(7), 2.0),
    ]


async def test_DW_1_3_merge_preserves_uncovered_day() -> None:
    # Two-day window; the download only covered Jan 1, so Jan 2's stored rows
    # keep their stored increments (+5, +3) on the new base.
    window = download_window(JAN1, date(2026, 1, 2))
    stored = [(_utc(5), 10.0), (_utc(5, day=2), 15.0), (_utc(6, day=2), 18.0)]
    stats = _merge_statistics([(_utc(5), 1.0)], 0.0, stored, window, _covered(JAN1))
    assert [s["sum"] for s in stats] == [1.0, 6.0, 9.0]


async def test_DW_1_3_merge_rebases_tail() -> None:
    # Rows after the window keep their increments, shifted onto the new chain.
    stored = [(_utc(5), 10.0), (_utc(5, day=2), 15.0), (_utc(6, day=2), 18.0)]
    stats = _merge_statistics([(_utc(5), 4.0)], 100.0, stored, WINDOW, _covered(JAN1))
    assert [s["sum"] for s in stats] == [104.0, 109.0, 112.0]


async def test_DW_1_3_merge_anchor_none_bases_on_zero() -> None:
    # No row before the window: the first stored increment is its whole sum.
    window = download_window(JAN1, date(2026, 1, 2))
    stored = [(_utc(5, day=2), 100.0), (_utc(6, day=2), 130.0)]
    stats = _merge_statistics([(_utc(5), 2.0)], 0.0, stored, window, _covered(JAN1))
    assert [s["sum"] for s in stats] == [2.0, 102.0, 132.0]


async def test_DW_1_3_merge_sums_duplicate_timestamps() -> None:
    points = [(_utc(5), 1.0), (_utc(5), 2.0), (_utc(6), 3.0)]
    stats = _merge_statistics(points, 0.0, [(_utc(5), 50.0)], WINDOW, _covered(JAN1))
    assert [(s["start"], s["sum"]) for s in stats] == [(_utc(5), 3.0), (_utc(6), 6.0)]


async def test_DW_1_3_merge_without_stored_rows() -> None:
    # Plain cumulative sum from the base (the pure-append path).
    stats = _merge_statistics([(_utc(5), 1.0), (_utc(6), 2.0)], 10.0, [], WINDOW, _covered())
    assert [s["sum"] for s in stats] == [11.0, 13.0]
    assert stats[0]["state"] == 11.0


async def test_DW_1_3_merge_no_points_zeroes_covered_rows() -> None:
    # The download covered the day but this series has nothing there any more
    # (a revised tier split) → every stored in-window hour becomes a zero row.
    stored = [(_utc(5), 10.0), (_utc(6), 20.0)]
    stats = _merge_statistics([], 5.0, stored, WINDOW, _covered(JAN1))
    assert [s["sum"] for s in stats] == [5.0, 5.0]


async def test_DW_1_3_merge_no_points_nothing_to_zero_is_noop() -> None:
    # Nothing new and no covered day → the chain is unchanged; nothing to write.
    stored = [(_utc(5), 10.0), (_utc(5, day=2), 15.0)]
    assert _merge_statistics([], 0.0, stored, WINDOW, _covered()) == []
    assert _merge_statistics([], 0.0, [], WINDOW, _covered(JAN1)) == []


async def test_merge_ignores_stored_rows_before_window() -> None:
    # History before the window is never touched — even if handed in.
    stored = [(_utc(4), 3.0), (_utc(5), 10.0)]
    stats = _merge_statistics([], 3.0, stored, WINDOW, _covered(JAN1))
    assert [(s["start"], s["sum"]) for s in stats] == [(_utc(5), 3.0)]


async def test_merge_window_end_is_inclusive() -> None:
    # 04:00 UTC Jan 2 is the window's last hour (covered → zeroed); 05:00 is
    # the first tail hour (increment preserved).
    stored = [(_utc(4, day=2), 10.0), (_utc(5, day=2), 12.0)]
    stats = _merge_statistics([(_utc(5), 1.0)], 0.0, stored, WINDOW, _covered(JAN1))
    assert [s["sum"] for s in stats] == [1.0, 1.0, 3.0]


async def test_merge_preserves_a_stored_drop_faithfully() -> None:
    # A negative stored increment (a broken chain) is carried as-is: Phase 1
    # rewrites consistently, it does not silently repair (that's Phase 2).
    stored = [(_utc(5, day=2), 10.0), (_utc(6, day=2), 4.0)]
    stats = _merge_statistics([(_utc(5), 1.0)], 0.0, stored, WINDOW, _covered(JAN1))
    assert [s["sum"] for s in stats] == [1.0, 11.0, 5.0]


# --- import series return value (lifetime total) ----------------------------- #


def _patch_import(
    monkeypatch: pytest.MonkeyPatch,
    row: dict | None,
    anchor: dict | None = None,
    stored: list[tuple[datetime, float]] | None = None,
) -> list:
    """Stub the recorder read/write; return the list capturing written stats.

    ``row`` is the newest stored row (pure-append path), ``anchor`` the row
    before the window and ``stored`` the rows from the window on (overlap path).
    """
    written: list = []

    async def fake_last_row(hass, statistic_id):
        return row

    async def fake_row_before(hass, statistic_id, before):
        return anchor

    async def fake_stored_rows(hass, statistic_ids, start):
        return {statistic_id: list(stored or []) for statistic_id in statistic_ids}

    monkeypatch.setattr(statistics_module, "_async_last_row", fake_last_row)
    monkeypatch.setattr(statistics_module, "_async_row_before", fake_row_before)
    monkeypatch.setattr(statistics_module, "_async_stored_rows", fake_stored_rows)
    monkeypatch.setattr(
        statistics_module,
        "async_add_external_statistics",
        lambda hass, metadata, stats: written.extend(stats),
    )
    return written


async def _import_result(points, **kwargs) -> SeriesResult:
    kwargs.setdefault("window", WINDOW)
    kwargs.setdefault("covered_days", {JAN1})
    return await _async_import_series(None, "enova_power:x", "x", points, "kWh", **kwargs)


async def _import(points, **kwargs) -> float | None:
    """The cumulative total ``_async_import_series`` reports."""
    return (await _import_result(points, **kwargs)).total


async def test_import_series_returns_cumulative_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    written = _patch_import(monkeypatch, None)
    total = await _import([(_utc(5), 1.0), (_utc(6), 2.0)])
    assert total == 3.0
    assert [s["sum"] for s in written] == [1.0, 3.0]


async def test_import_series_resumes_from_stored_sum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Newest stored row is before the window → pure append on its sum.
    _patch_import(monkeypatch, {"start": _utc(4), "sum": 10.0})
    assert await _import([(_utc(6), 2.0)]) == 12.0


async def test_import_series_append_path_reads_no_anchor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_import(monkeypatch, {"start": _utc(4), "sum": 10.0})

    async def unexpected(hass, statistic_id, before):
        raise AssertionError("anchor read on the pure-append path")

    monkeypatch.setattr(statistics_module, "_async_row_before", unexpected)
    assert await _import([(_utc(6), 2.0)]) == 12.0


async def test_import_series_total_survives_no_new_points(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Nothing published at all this window → the stored sum, no writes.
    written = _patch_import(monkeypatch, {"start": _utc(4), "sum": 10.0})
    assert await _import([], covered_days=set()) == 10.0
    assert written == []


async def test_import_series_no_points_uncovered_stored_rows_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Empty download with in-window rows on days it didn't cover: no write,
    # and the total is the sum the series already ends on.
    anchor = {"start": _utc(4), "sum": 10.0}
    stored = [(_utc(5), 12.0), (_utc(6), 15.0)]
    written = _patch_import(monkeypatch, None, anchor=anchor, stored=stored)
    assert await _import([], covered_days=set()) == 15.0
    assert written == []


async def test_import_series_no_points_zeroes_covered_stored_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # tier2 after a revision that drops below the threshold: no points, but
    # the day was covered → its stored rows become zero-increment rows.
    anchor = {"start": _utc(4), "sum": 10.0}
    written = _patch_import(monkeypatch, None, anchor=anchor, stored=[(_utc(5), 12.0)])
    assert await _import([]) == 10.0
    assert [(s["start"], s["sum"]) for s in written] == [(_utc(5), 10.0)]


async def test_import_series_rewrites_overlapping_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The portal revises already-imported hours (preliminary → real values).
    # An overlapping window must be rewritten from the anchor row before it,
    # not silently skipped.
    anchor = {"start": _utc(4), "sum": 100.0}  # last row before window
    stale = [(_utc(5), 200.0), (_utc(6), 300.0), (_utc(7), 500.0)]  # preliminary sums
    written = _patch_import(monkeypatch, None, anchor=anchor, stored=stale)

    points = [(_utc(5), 2.0), (_utc(6), 3.0), (_utc(7), 4.0)]
    total = await _import(points)

    # Sums re-derived from the anchor, replacing the stale rows in place.
    assert [s["sum"] for s in written] == [102.0, 105.0, 109.0]
    assert total == 109.0


async def test_import_series_overlap_without_anchor_starts_from_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Overlap at the very beginning of a series (no row before the window).
    written = _patch_import(monkeypatch, None, anchor=None, stored=[(_utc(5), 24.0)])
    total = await _import([(_utc(5), 1.0), (_utc(6), 2.0)])
    assert [s["sum"] for s in written] == [1.0, 3.0]
    assert total == 3.0


async def test_import_series_reads_stored_rows_itself_when_not_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[tuple[set[str], datetime]] = []
    _patch_import(monkeypatch, None, stored=[])

    async def fake_stored_rows(hass, statistic_ids, start):
        asked.append((statistic_ids, start))
        return {}

    monkeypatch.setattr(statistics_module, "_async_stored_rows", fake_stored_rows)
    await _import([(_utc(5), 1.0)])
    assert asked == [({"enova_power:x"}, WINDOW[0])]


async def test_import_series_drops_points_outside_window(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    written = _patch_import(monkeypatch, None)
    total = await _import([(_utc(4), 9.0), (_utc(5), 1.0), (_utc(5, day=2), 9.0)])
    assert [(s["start"], s["sum"]) for s in written] == [(_utc(5), 1.0)]
    assert total == 1.0
    assert "Dropping 2 points outside the download window" in caplog.text


async def test_import_series_none_when_series_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_import(monkeypatch, None)
    assert await _import([]) is None


async def test_import_series_metadata_uses_mean_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # has_mean is deprecated (removal per HA 2026.11 warning); the metadata
    # must carry mean_type instead.
    from homeassistant.components.recorder.models import StatisticMeanType

    captured: dict = {}
    _patch_import(monkeypatch, None)
    monkeypatch.setattr(
        statistics_module,
        "async_add_external_statistics",
        lambda hass, metadata, stats: captured.update(metadata),
    )

    await _import([(_utc(5), 1.0)])

    assert captured["mean_type"] == StatisticMeanType.NONE
    assert "has_mean" not in captured
    assert captured["has_sum"] is True
    # unit_class must be stated explicitly (required from HA 2026.11).
    assert captured["unit_class"] == "energy"


async def test_import_meter_writes_bucket_costs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    written: dict[str, str] = {}  # statistic_id → unit
    _patch_import(monkeypatch, None)
    monkeypatch.setattr(
        statistics_module,
        "async_add_external_statistics",
        lambda hass, metadata, stats: written.__setitem__(
            metadata["statistic_id"], metadata["unit_of_measurement"]
        ),
    )

    rates = TOU_RATES + TIER_RATES
    tiered = tiered_rates(TIER_RATES)
    readings = [_reading(date(2026, 6, 1), h01=10.0, h13=5.0)]
    result = await statistics_module.async_import_meter(
        None, "111", readings, PLAN_TOU, rates, tiered, [], "CAD",
        window=download_window(date(2026, 6, 1), date(2026, 6, 1)),
        covered_days=days_covered(readings),
    )

    assert result == ImportResult(15.0, {})
    assert written[consumption_statistic_id("111")] == "kWh"
    assert written[bucket_cost_statistic_id("111", "tou_off_peak")] == "CAD"
    assert written[bucket_cost_statistic_id("111", "tou_on_peak")] == "CAD"
    assert written[bucket_cost_statistic_id("111", "tier1")] == "CAD"
    assert cost_statistic_id("111") in written
    assert cost_if_statistic_id("111", PLAN_TIERED) in written
    # No ULO rates → no ULO cost series (and none expected, so no refetch loop).
    assert cost_if_statistic_id("111", PLAN_ULO) not in written
    assert bucket_cost_statistic_id("111", "ulo_overnight") not in written
    # Everything written must be expected, or upgrade detection would never settle.
    expected = set(expected_statistic_ids("111", PLAN_TOU, rates, tiered))
    assert set(written) <= expected


async def _imported_ids(monkeypatch: pytest.MonkeyPatch, plan, rates, tiered) -> set[str]:
    """Run ``async_import_meter`` and return the statistic ids it imported."""
    imported: set[str] = set()

    async def fake_import_series(hass, statistic_id, name, points, unit, **kwargs):
        imported.add(statistic_id)
        return SeriesResult(None, [])

    async def fake_stored_rows(hass, statistic_ids, start):
        return {}

    monkeypatch.setattr(statistics_module, "_async_import_series", fake_import_series)
    monkeypatch.setattr(statistics_module, "_async_stored_rows", fake_stored_rows)
    readings = [_reading(date(2026, 6, 1), h01=10.0, h13=5.0)]
    await statistics_module.async_import_meter(
        None, "111", readings, plan, rates, tiered, [], "CAD",
        window=download_window(date(2026, 6, 1), date(2026, 6, 1)),
        covered_days=days_covered(readings),
    )
    return imported


async def test_DW_1_4_unpriced_cost_series_not_imported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No rates at all: not a single cost series is touched — importing one
    # with no points would zero its stored rows on the covered day.
    imported = await _imported_ids(monkeypatch, PLAN_TOU, [], None)
    assert imported == set(expected_statistic_ids("111", PLAN_TOU, [], None))
    assert not any(":cost" in statistic_id for statistic_id in imported)

    # TOU rates only, Tiered plan active: TOU cost series import; the unpriced
    # active-plan cost, cost_if_ulo/tiered and ULO/tier bucket costs do not.
    imported = await _imported_ids(monkeypatch, PLAN_TIERED, TOU_RATES, None)
    assert cost_if_statistic_id("111", PLAN_TOU) in imported
    assert bucket_cost_statistic_id("111", "tou_on_peak") in imported
    assert cost_statistic_id("111") not in imported
    assert cost_if_statistic_id("111", PLAN_ULO) not in imported
    assert cost_if_statistic_id("111", PLAN_TIERED) not in imported
    assert bucket_cost_statistic_id("111", "ulo_overnight") not in imported
    assert bucket_cost_statistic_id("111", "tier2") not in imported
    assert imported == set(expected_statistic_ids("111", PLAN_TIERED, TOU_RATES, None))


async def test_import_meter_batches_one_stored_rows_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads: list[tuple[set[str], datetime]] = []
    written = _patch_import(monkeypatch, None)

    async def fake_stored_rows(hass, statistic_ids, start):
        reads.append((statistic_ids, start))
        return {}

    monkeypatch.setattr(statistics_module, "_async_stored_rows", fake_stored_rows)
    readings = [_reading(date(2026, 6, 1), h01=10.0)]
    window = download_window(date(2026, 6, 1), date(2026, 6, 1))
    await statistics_module.async_import_meter(
        None, "111", readings, PLAN_TOU, TOU_RATES, None, [], "CAD",
        window=window, covered_days={date(2026, 6, 1)},
    )
    # One read for the whole meter, from the window start, covering every id.
    assert len(reads) == 1
    assert reads[0] == (set(expected_statistic_ids("111", PLAN_TOU, TOU_RATES, None)), window[0])
    assert written


# --- integrity check (pure) ---------------------------------------------------- #


async def test_DW_2_1_find_sum_drops_clean_and_single_drop() -> None:
    # A flat hour (equal sums) is fine; only a fall counts.
    assert find_sum_drops([(_utc(5), 1.0), (_utc(6), 1.0), (_utc(7), 2.5)]) == []
    # The July pattern: a day re-imported on a stale base falls below its
    # predecessor — reported at the hour the sum falls, once.
    broken = [(_utc(5), 100.0), (_utc(6), 101.0), (_utc(7), 1.0), (_utc(8), 2.0)]
    assert find_sum_drops(broken) == [_utc(7)]


async def test_DW_2_1_find_sum_drops_ignores_negative_zero_and_tolerance() -> None:
    assert find_sum_drops([(_utc(5), 0.0), (_utc(6), -0.0)]) == []
    # Exactly at the tolerance is float noise; just above it is a drop.
    assert find_sum_drops([(_utc(5), 1.0), (_utc(6), 1.0 - 1e-6)]) == []
    assert find_sum_drops([(_utc(5), 1.0), (_utc(6), 1.0 - 2e-6)]) == [_utc(6)]
    assert find_sum_drops([(_utc(5), 1.0), (_utc(6), 0.5)], tolerance=1.0) == []


async def test_DW_2_1_find_sum_drops_empty_and_single_row() -> None:
    assert find_sum_drops([]) == []
    assert find_sum_drops([(_utc(5), 7.0)]) == []


async def test_DW_2_1_find_sum_drops_first_row_vs_anchor() -> None:
    # Without the anchor the chain starts at the first stored row (nothing to
    # compare); with it, a first row below the anchor's sum is the drop.
    anchor = (_utc(4), 50.0)
    stored = [(_utc(5), 1.0), (_utc(6), 2.0)]
    assert find_sum_drops(stored) == []
    assert find_sum_drops([anchor, *stored]) == [_utc(5)]


async def test_find_sum_drops_reports_every_drop() -> None:
    rows = [(_utc(5), 10.0), (_utc(6), 4.0), (_utc(7), 9.0), (_utc(8), 3.0)]
    assert find_sum_drops(rows) == [_utc(6), _utc(8)]


async def test_scan_series_full_history_per_series(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list = []

    def fake_during_period(hass, start, end, ids, period, units, types):
        asked.append((start, end, ids))
        return {
            "enova_power:a": [
                {"start": _utc(6).timestamp(), "sum": 5.0},
                {"start": _utc(5).timestamp(), "sum": 1.0},
                {"start": _utc(7).timestamp(), "sum": 2.0},  # falls below 5.0
            ],
        }

    monkeypatch.setattr(statistics_module, "statistics_during_period", fake_during_period)
    scans = _scan_series(None, ["enova_power:a", "enova_power:b"])
    # Sorted before checking; a series with no rows scans clean with no first hour.
    assert scans == {
        "enova_power:a": SeriesScan(first_start=_utc(5), drops=[_utc(7)]),
        "enova_power:b": SeriesScan(first_start=None, drops=[]),
    }
    # One batched full-history read for every requested id.
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    assert asked == [(epoch, None, {"enova_power:a", "enova_power:b"})]


# --- integrity check inside the import ----------------------------------------- #


async def test_import_series_reports_drop_from_anchor_and_heals_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The July pattern inside the window: stored rows re-based on zero after an
    # anchor at 100. The pre-write read reports the drop; the rewrite itself
    # puts the chain back on the anchor.
    anchor = {"start": _utc(4), "sum": 100.0}
    stale = [(_utc(5), 1.0), (_utc(6), 2.0)]
    written = _patch_import(monkeypatch, None, anchor=anchor, stored=stale)

    result = await _import_result([(_utc(5), 2.0), (_utc(6), 3.0)])

    assert result == SeriesResult(105.0, [_utc(5)])
    assert [s["sum"] for s in written] == [102.0, 105.0]


async def test_import_series_reports_drop_even_when_nothing_is_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No points and uncovered rows → no write, but the check still ran.
    anchor = {"start": _utc(4), "sum": 10.0}
    stored = [(_utc(5), 12.0), (_utc(6), 3.0)]
    written = _patch_import(monkeypatch, None, anchor=anchor, stored=stored)
    assert await _import_result([], covered_days=set()) == SeriesResult(3.0, [_utc(6)])
    assert written == []


async def test_import_series_overlap_without_anchor_checks_stored_rows_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_import(monkeypatch, None, anchor=None, stored=[(_utc(5), 24.0), (_utc(6), 25.0)])
    assert (await _import_result([(_utc(5), 1.0)])).drops == []
    _patch_import(monkeypatch, None, anchor=None, stored=[(_utc(5), 24.0), (_utc(6), 2.0)])
    assert (await _import_result([(_utc(5), 1.0)])).drops == [_utc(6)]


async def test_import_series_anchor_without_usable_start_is_not_compared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The anchor's sum still bases the chain, but with no start it can't be a
    # row in the check.
    _patch_import(monkeypatch, None, anchor={"start": None, "sum": 100.0}, stored=[(_utc(5), 1.0)])
    assert await _import_result([(_utc(5), 2.0)]) == SeriesResult(102.0, [])


async def test_import_series_append_path_reports_no_drops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Nothing overlapping was read, so there is nothing to check.
    _patch_import(monkeypatch, {"start": _utc(4), "sum": 10.0})
    assert await _import_result([(_utc(6), 2.0)]) == SeriesResult(12.0, [])


async def test_import_meter_maps_broken_series_to_drop_times(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jun1 = datetime(2026, 6, 1, 4, tzinfo=timezone.utc)  # 00:00 EDT
    consumption = consumption_statistic_id("111")
    _patch_import(monkeypatch, None, anchor={"start": jun1.replace(hour=3), "sum": 100.0})

    async def fake_stored_rows(hass, statistic_ids, start):
        # Only consumption has stored rows in the window — re-based on zero.
        return {consumption: [(jun1, 10.0)]}

    monkeypatch.setattr(statistics_module, "_async_stored_rows", fake_stored_rows)
    readings = [_reading(date(2026, 6, 1), h01=10.0, h13=5.0)]
    result = await statistics_module.async_import_meter(
        None, "111", readings, PLAN_TOU, TOU_RATES, None, [], "CAD",
        window=download_window(date(2026, 6, 1), date(2026, 6, 1)),
        covered_days=days_covered(readings),
    )
    # Broken series only, keyed by id; the total is still the healed chain's end.
    assert result == ImportResult(115.0, {consumption: [jun1]})
