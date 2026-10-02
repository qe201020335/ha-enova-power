"""Tests for async_setup_entry: the statistics-repair log branch, the first
refresh running after setup, and what reloads the entry."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.enova_power.const import (
    CONF_PLAN,
    CONF_STATS_VERSION,
    DOMAIN,
    PLAN_TIERED,
    PLAN_TOU,
)
from custom_components.enova_power.coordinator import EnovaPowerCoordinator, MeterData
from custom_components.enova_power.statistics import STATS_VERSION

REPAIR_MESSAGE = "Statistics format changed"


async def _setup(hass: HomeAssistant, stats_version: int | None) -> MockConfigEntry:
    """Set up a config entry with a mocked client and coordinator refresh.

    No real login, download, or recorder access happens: the client is a mock
    and the coordinator's first refresh (which would otherwise fetch from the
    portal and import statistics) is stubbed out entirely.
    """
    data = {CONF_USERNAME: "user@example.com", CONF_PASSWORD: "secret"}
    if stats_version is not None:
        data[CONF_STATS_VERSION] = stats_version
    entry = MockConfigEntry(domain=DOMAIN, data=data)
    entry.add_to_hass(hass)

    with (
        patch("custom_components.enova_power.AsyncEnovaClient") as mock_cls,
        patch.object(EnovaPowerCoordinator, "async_refresh", AsyncMock(return_value=None)),
        patch("custom_components.enova_power.statistics.get_instance") as mock_get_instance,
    ):
        client = mock_cls.return_value
        client.login = AsyncMock()
        client.meter_id = "111111"
        client.meter_ids = ["111111"]

        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    # Never awaited nor fired-and-forgot: the repair no longer clears anything.
    mock_get_instance.return_value.async_clear_statistics.assert_not_called()
    assert entry.state is ConfigEntryState.LOADED
    return entry


async def test_DW_repair_log_emitted_when_stats_version_stale(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="custom_components.enova_power")

    await _setup(hass, stats_version=4)

    assert REPAIR_MESSAGE in caplog.text
    assert f"v4 -> v{STATS_VERSION}" in caplog.text


async def test_DW_no_repair_log_when_already_current(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="custom_components.enova_power")

    await _setup(hass, stats_version=STATS_VERSION)

    assert REPAIR_MESSAGE not in caplog.text


# --- first refresh runs after setup ----------------------------------------- #

METER = "111111"
CREDENTIALS = {CONF_USERNAME: "user@example.com", CONF_PASSWORD: "secret"}
CYCLE_UNIQUE_ID = f"{METER}_cycle_to_date_consumption"


def _meter_data() -> MeterData:
    return MeterData(
        latest=None,
        plan=PLAN_TOU,
        cycle_energy=1.5,
        cycle_cost=None,
        last_bill=None,
        threshold=None,
        lifetime_energy=10.0,
    )


async def _setup_with_update(
    hass: HomeAssistant, update_data: Callable[..., Awaitable[dict[str, MeterData]]]
) -> tuple[MockConfigEntry, AsyncMock]:
    """Set up a current-version entry whose refreshes run ``update_data``.
    Returns the entry and the mock client."""
    entry = MockConfigEntry(
        domain=DOMAIN, data={**CREDENTIALS, CONF_STATS_VERSION: STATS_VERSION}
    )
    entry.add_to_hass(hass)
    with (
        patch("custom_components.enova_power.AsyncEnovaClient") as mock_cls,
        patch.object(EnovaPowerCoordinator, "_async_update_data", update_data),
    ):
        client = mock_cls.return_value
        client.login = AsyncMock()
        client.meter_id = METER
        client.meter_ids = [METER]
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry, client


def _cycle_state(hass: HomeAssistant) -> str:
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, CYCLE_UNIQUE_ID)
    assert entity_id is not None
    return hass.states.get(entity_id).state


async def test_setup_does_not_wait_for_the_first_refresh(hass: HomeAssistant) -> None:
    # The first refresh (a full backfill on a new entry) must not hold the
    # config flow open: setup finishes and the sensors wait for the data.
    release = asyncio.Event()

    async def update_data(self: EnovaPowerCoordinator) -> dict[str, MeterData]:
        await release.wait()
        return {METER: _meter_data()}

    entry, _ = await _setup_with_update(hass, update_data)
    assert entry.state is ConfigEntryState.LOADED
    assert _cycle_state(hass) == STATE_UNAVAILABLE

    release.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert _cycle_state(hass) == "1.5"


async def test_failed_first_refresh_keeps_entry_loaded_without_relogin(
    hass: HomeAssistant,
) -> None:
    # A failed first refresh used to fail setup, and every setup retry logged
    # in again. Now the entry stays loaded and the coordinator retries later.
    async def update_data(self: EnovaPowerCoordinator) -> dict[str, MeterData]:
        raise UpdateFailed("portal down")

    entry, client = await _setup_with_update(hass, update_data)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.state is ConfigEntryState.LOADED
    assert _cycle_state(hass) == STATE_UNAVAILABLE
    client.login.assert_awaited_once()


async def test_options_change_reloads_but_data_stamp_does_not(hass: HomeAssistant) -> None:
    entry = await _setup(hass, stats_version=STATS_VERSION)
    with (
        patch.object(hass.config_entries, "async_schedule_reload") as reload,
        patch.object(hass.config_entries, "async_reload") as direct_reload,
    ):
        # The coordinator stamping CONF_STATS_VERSION (a data update) must not
        # reload the entry: that would log in and refresh all over again.
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_STATS_VERSION: STATS_VERSION + 1}
        )
        await hass.async_block_till_done()
        reload.assert_not_called()
        direct_reload.assert_not_called()

        result = await hass.config_entries.options.async_init(entry.entry_id)
        await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_PLAN: PLAN_TIERED}
        )
        await hass.async_block_till_done()
        reload.assert_called_once_with(entry.entry_id)
