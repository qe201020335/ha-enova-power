"""Tests for the backfill service action."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from homeassistant.const import ATTR_CONFIG_ENTRY_ID, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.enova_power.const import ATTR_MONTHS, DOMAIN, SERVICE_BACKFILL
from custom_components.enova_power.coordinator import EnovaPowerCoordinator


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    """A loaded entry with a mocked client and no real refresh."""
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_USERNAME: "user@example.com", CONF_PASSWORD: "secret"}
    )
    entry.add_to_hass(hass)
    with (
        patch("custom_components.enova_power.AsyncEnovaClient") as mock_cls,
        patch.object(
            EnovaPowerCoordinator, "async_config_entry_first_refresh", AsyncMock()
        ),
    ):
        client = mock_cls.return_value
        client.login = AsyncMock()
        client.meter_id = "111111"
        client.meter_ids = ["111111"]
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


@pytest.mark.parametrize(("months", "expected"), [(24, 24), (None, None)])
async def test_backfill_requests_it_on_the_entry(
    hass: HomeAssistant, months: int | None, expected: int | None
) -> None:
    entry = await _setup(hass)
    data = {ATTR_CONFIG_ENTRY_ID: entry.entry_id}
    if months is not None:
        data[ATTR_MONTHS] = months
    with patch.object(EnovaPowerCoordinator, "async_request_backfill") as request:
        await hass.services.async_call(DOMAIN, SERVICE_BACKFILL, data, blocking=True)
    request.assert_called_once_with(months=expected)


async def test_backfill_unknown_entry(hass: HomeAssistant) -> None:
    await _setup(hass)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, SERVICE_BACKFILL, {ATTR_CONFIG_ENTRY_ID: "nope"}, blocking=True
        )


async def test_backfill_unloaded_entry(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, SERVICE_BACKFILL, {ATTR_CONFIG_ENTRY_ID: entry.entry_id}, blocking=True
        )


async def test_backfill_months_out_of_range(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_BACKFILL,
            {ATTR_CONFIG_ENTRY_ID: entry.entry_id, ATTR_MONTHS: 61},
            blocking=True,
        )
