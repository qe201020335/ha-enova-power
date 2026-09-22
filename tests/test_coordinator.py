"""Tests for the coordinator's download-window selection (pure logic)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from enovapower import BillingPeriod

from homeassistant.core import HomeAssistant
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
from custom_components.enova_power.statistics import STATS_VERSION, download_window

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
    import_meter = AsyncMock(return_value=None)
    monkeypatch.setattr(coordinator_module, "async_last_statistic_start", last_start)
    monkeypatch.setattr(coordinator_module, "async_missing_series", AsyncMock(return_value=[]))
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
