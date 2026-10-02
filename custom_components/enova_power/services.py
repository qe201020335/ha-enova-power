"""Service actions for Enova Power."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_CONFIG_ENTRY_ID
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import (
    ATTR_END_DATE,
    ATTR_MONTHS,
    ATTR_START_DATE,
    DOMAIN,
    MAX_BACKFILL_MONTHS,
    SERVICE_BACKFILL,
)


def _valid_range(data: dict[str, Any]) -> dict[str, Any]:
    """An end date needs a start date, and must not come before it."""
    start, end = data.get(ATTR_START_DATE), data.get(ATTR_END_DATE)
    if end is not None and start is None:
        raise vol.Invalid("end_date requires start_date")
    if start is not None and end is not None and end < start:
        raise vol.Invalid("end_date is before start_date")
    return data


# Either the last ``months`` or a ``start_date``..``end_date`` (default: today) range.
BACKFILL_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
            vol.Exclusive(ATTR_MONTHS, "range"): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=MAX_BACKFILL_MONTHS)
            ),
            vol.Exclusive(ATTR_START_DATE, "range"): cv.date,
            vol.Optional(ATTR_END_DATE): cv.date,
        }
    ),
    _valid_range,
)


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the integration's service actions."""

    async def backfill(call: ServiceCall) -> None:
        """Re-import an entry's history; runs in the background."""
        entry = hass.config_entries.async_get_entry(call.data[ATTR_CONFIG_ENTRY_ID])
        if entry is None or entry.domain != DOMAIN:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="entry_not_found"
            )
        if entry.state is not ConfigEntryState.LOADED:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="entry_not_loaded"
            )
        entry.runtime_data.async_request_backfill(
            months=call.data.get(ATTR_MONTHS),
            start=call.data.get(ATTR_START_DATE),
            end=call.data.get(ATTR_END_DATE),
        )

    hass.services.async_register(DOMAIN, SERVICE_BACKFILL, backfill, schema=BACKFILL_SCHEMA)
