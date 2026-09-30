"""Tests for the Ontario OEB TOU/ULO schedule (pure logic)."""

from __future__ import annotations

from datetime import date, datetime

from custom_components.enova_power.const import (
    PERIOD_MID_PEAK,
    PERIOD_OFF_PEAK,
    PERIOD_ON_PEAK,
    PERIOD_TIERED,
    PERIOD_ULO_OVERNIGHT,
    PLAN_TIERED,
    PLAN_TOU,
    PLAN_ULO,
    TIME_ZONE,
)
from custom_components.enova_power.schedule import current_period, ontario_tou_holidays


def _dt(year: int, month: int, day: int, hour: int) -> datetime:
    return datetime(year, month, day, hour, tzinfo=TIME_ZONE)


# 2026-07-15 is a summer Wednesday; 2026-01-14 a winter Wednesday;
# 2026-07-18 a Saturday; 2026-12-25 (Fri) is Christmas.

async def test_holidays_include_fixed_and_computed() -> None:
    hols = ontario_tou_holidays(2026)
    assert date(2026, 12, 25) in hols  # Christmas
    assert date(2026, 2, 16) in hols  # Family Day (3rd Mon Feb)
    assert date(2026, 4, 3) in hols  # Good Friday (Easter 2026-04-05 - 2)


async def test_holidays_include_civic_holiday() -> None:
    # First Monday of August; summer on-peak would otherwise apply 11-17.
    assert date(2026, 8, 3) in ontario_tou_holidays(2026)
    assert current_period(_dt(2026, 8, 3, 13), PLAN_TOU) == PERIOD_OFF_PEAK


async def test_holidays_match_oeb_2026_schedule() -> None:
    # OEB's published 2026 list: Boxing Day (Sat Dec 26) observed Mon Dec 28.
    expected = {
        date(2026, 1, 1),
        date(2026, 2, 16),
        date(2026, 4, 3),
        date(2026, 5, 18),
        date(2026, 7, 1),
        date(2026, 8, 3),
        date(2026, 9, 7),
        date(2026, 10, 12),
        date(2026, 12, 25),
        date(2026, 12, 28),
    }
    weekdays = {d for d in ontario_tou_holidays(2026) if d.weekday() < 5}
    assert weekdays == expected
    assert current_period(_dt(2026, 12, 28, 8), PLAN_TOU) == PERIOD_OFF_PEAK
    assert current_period(_dt(2026, 12, 28, 17), PLAN_ULO) == PERIOD_OFF_PEAK


async def test_weekend_holiday_moves_past_a_following_holiday() -> None:
    # 2022: Christmas Sun, Boxing Day Mon -> Christmas observed Tue Dec 27.
    hols = ontario_tou_holidays(2022)
    assert {date(2022, 12, 26), date(2022, 12, 27)} <= hols
    # 2021: Christmas Sat, Boxing Day Sun -> observed Mon 27 and Tue 28.
    hols = ontario_tou_holidays(2021)
    assert {date(2021, 12, 27), date(2021, 12, 28)} <= hols
    assert date(2021, 12, 29) not in hols


async def test_weekend_new_year_and_canada_day_move_to_monday() -> None:
    assert date(2022, 1, 3) in ontario_tou_holidays(2022)  # Jan 1 was a Saturday
    assert date(2023, 7, 3) in ontario_tou_holidays(2023)  # Jul 1 was a Saturday


async def test_tou_summer_weekday_periods() -> None:
    assert current_period(_dt(2026, 7, 15, 13), PLAN_TOU) == PERIOD_ON_PEAK  # 11-17
    assert current_period(_dt(2026, 7, 15, 8), PLAN_TOU) == PERIOD_MID_PEAK  # 7-11
    assert current_period(_dt(2026, 7, 15, 22), PLAN_TOU) == PERIOD_OFF_PEAK  # >=19


async def test_tou_winter_weekday_on_peak_shifts() -> None:
    assert current_period(_dt(2026, 1, 14, 8), PLAN_TOU) == PERIOD_ON_PEAK  # 7-11
    assert current_period(_dt(2026, 1, 14, 13), PLAN_TOU) == PERIOD_MID_PEAK  # 11-17


async def test_tou_weekend_and_holiday_off_peak() -> None:
    assert current_period(_dt(2026, 7, 18, 13), PLAN_TOU) == PERIOD_OFF_PEAK  # Saturday
    assert current_period(_dt(2026, 12, 25, 13), PLAN_TOU) == PERIOD_OFF_PEAK  # Christmas


async def test_ulo_overnight_applies_every_day() -> None:
    assert current_period(_dt(2026, 7, 15, 2), PLAN_ULO) == PERIOD_ULO_OVERNIGHT
    assert current_period(_dt(2026, 7, 18, 23), PLAN_ULO) == PERIOD_ULO_OVERNIGHT  # Sat 23:00


async def test_ulo_weekday_on_and_mid_peak() -> None:
    assert current_period(_dt(2026, 7, 15, 17), PLAN_ULO) == PERIOD_ON_PEAK  # 16-21
    assert current_period(_dt(2026, 7, 15, 9), PLAN_ULO) == PERIOD_MID_PEAK  # 7-16
    assert current_period(_dt(2026, 7, 18, 13), PLAN_ULO) == PERIOD_OFF_PEAK  # weekend daytime


async def test_tiered_is_constant() -> None:
    assert current_period(_dt(2026, 7, 15, 13), PLAN_TIERED) == PERIOD_TIERED
