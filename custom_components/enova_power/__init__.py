"""The Enova Power integration.

Unofficial — not affiliated with, endorsed by, or supported by Enova Power Corp.
This is a thin Home Assistant wrapper over the generic ``enovapower`` library;
all portal/API logic lives there.
"""

from __future__ import annotations

from enovapower import AsyncEnovaClient, EnovaAuthError, EnovaNetworkError

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .const import CONF_STATS_VERSION, DOMAIN, LOGGER
from .coordinator import EnovaPowerCoordinator
from .statistics import STATS_VERSION

PLATFORMS: list[Platform] = [Platform.SENSOR]

# Typed config entry: entry.runtime_data is the coordinator.
EnovaPowerConfigEntry = ConfigEntry[EnovaPowerCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: EnovaPowerConfigEntry) -> bool:
    """Set up Enova Power from a config entry."""
    # Dedicated session with an isolated cookie jar — NOT async_get_clientsession.
    # The portal serves a non-login page (no CSRF token) when the shared jar
    # already holds an authenticated session cookie from the config flow's login,
    # which made the setup login fail with "Invalid credentials". HA closes the
    # session itself (auto_cleanup): on unload, and on any failed setup — never
    # close it here, that trips the "integration closes the session" warning.
    session = async_create_clientsession(hass)
    client = AsyncEnovaClient(session=session)

    try:
        await client.login(entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD])
    except EnovaAuthError as err:
        raise ConfigEntryAuthFailed("Invalid Enova Power credentials") from err
    except EnovaNetworkError as err:
        raise ConfigEntryNotReady(f"Cannot reach Enova Power: {err}") from err

    LOGGER.debug("Logged in; %d meter(s) found", len(client.meter_ids))

    # Every statistic/entity is keyed on the meter id; never set up on None.
    if not client.meter_id:
        raise ConfigEntryNotReady("No Enova Power meter found for this account yet")

    # One-time statistics repair: the coordinator's first refresh re-imports
    # every meter's series in place from its oldest stored date and stamps
    # CONF_STATS_VERSION once that cycle succeeds (see
    # EnovaPowerCoordinator._rebuild / statistics.STATS_VERSION). Nothing is
    # cleared here or anywhere else in the repair — setup never awaits the
    # recorder queue (it isn't drained until HA has fully started; waiting on
    # it here would deadlock bootstrap), and the repair no longer needs a
    # clear to begin with, since the in-place merge rewrites any window.
    if entry.data.get(CONF_STATS_VERSION, 1) < STATS_VERSION:
        LOGGER.info(
            "Statistics format changed (v%s -> v%s); repairing every series "
            "in place from its oldest stored date on the first refresh",
            entry.data.get(CONF_STATS_VERSION, 1),
            STATS_VERSION,
        )

    coordinator = EnovaPowerCoordinator(hass, entry, client)
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # The first refresh runs after setup instead of gating it: on a new entry
    # it is the full history backfill, which would hold the config flow's
    # dialog open for its whole duration. Sensors stay unavailable until it
    # lands. A failure here doesn't fail setup either (HA would retry setup
    # with a fresh login each time, which trips the portal's login lockout);
    # the coordinator retries on its normal interval with this session, and
    # an auth failure still starts re-authentication.
    entry.async_create_background_task(
        hass, coordinator.async_refresh(), f"{DOMAIN} first refresh"
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EnovaPowerConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
