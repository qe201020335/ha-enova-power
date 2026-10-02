"""Service actions for Enova Power."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_CONFIG_ENTRY_ID
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import ATTR_MONTHS, DOMAIN, MAX_BACKFILL_MONTHS, SERVICE_BACKFILL

BACKFILL_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Optional(ATTR_MONTHS): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=MAX_BACKFILL_MONTHS)
        ),
    }
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
        entry.runtime_data.async_request_backfill(months=call.data.get(ATTR_MONTHS))

    hass.services.async_register(DOMAIN, SERVICE_BACKFILL, backfill, schema=BACKFILL_SCHEMA)
