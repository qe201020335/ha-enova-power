"""Tests for async_setup_entry's one-time statistics-repair log branch."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.enova_power.const import CONF_STATS_VERSION, DOMAIN
from custom_components.enova_power.coordinator import EnovaPowerCoordinator
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
        patch.object(
            EnovaPowerCoordinator,
            "async_config_entry_first_refresh",
            AsyncMock(return_value=None),
        ),
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
