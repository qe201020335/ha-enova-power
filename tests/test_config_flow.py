"""Tests for the Enova Power config flow."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from enovapower import EnovaAuthError, EnovaNetworkError

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData

from custom_components.enova_power.const import (
    CONF_BACKFILL_MONTHS,
    DEFAULT_BACKFILL_MONTHS,
    DOMAIN,
)

USER_INPUT = {CONF_USERNAME: "user@example.com", CONF_PASSWORD: "secret"}


async def test_user_flow_success(hass: HomeAssistant, mock_client) -> None:
    """A valid login creates an entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == USER_INPUT[CONF_USERNAME]
    assert result["result"].unique_id == "1234567890"
    assert result["data"][CONF_BACKFILL_MONTHS] == DEFAULT_BACKFILL_MONTHS
    mock_client.login.assert_awaited_once()


async def test_user_flow_custom_backfill_months(hass: HomeAssistant, mock_client) -> None:
    """The chosen history depth is stored on the entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_BACKFILL_MONTHS: 16}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_BACKFILL_MONTHS] == 16


async def test_user_flow_rejects_out_of_range_backfill(
    hass: HomeAssistant, mock_client
) -> None:
    """A backfill depth outside 1..MAX is refused by the form schema."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with pytest.raises(InvalidData):
        await hass.config_entries.flow.async_configure(
            result["flow_id"], {**USER_INPUT, CONF_BACKFILL_MONTHS: 0}
        )
    mock_client.login.assert_not_awaited()


async def test_user_flow_invalid_auth(hass: HomeAssistant, mock_client) -> None:
    """A bad login shows invalid_auth and lets the user retry."""
    mock_client.login.side_effect = EnovaAuthError("bad")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_user_flow_cannot_connect(hass: HomeAssistant, mock_client) -> None:
    """A network failure shows cannot_connect."""
    mock_client.login.side_effect = EnovaNetworkError("down")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_no_meter(hass: HomeAssistant, mock_client) -> None:
    """A login that finds no meter (e.g. the portal's lockout page) is refused."""
    mock_client.meter_id = None
    mock_client.meter_ids = []
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "no_meter"}
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_flow_detaches_its_session_instead_of_closing(
    hass: HomeAssistant, mock_client
) -> None:
    """The flow's private session is detached, never closed (HA forbids close)."""
    session = MagicMock()
    with patch(
        "custom_components.enova_power.config_flow.async_create_clientsession",
        return_value=session,
    ) as create:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    create.assert_called_once_with(hass, auto_cleanup=False)
    session.detach.assert_called_once()
    session.close.assert_not_called()
