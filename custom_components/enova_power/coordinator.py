"""Data update coordinator for Enova Power.

Each cycle fetches usage per meter and imports it into Home Assistant long-term
statistics. The download window is derived from the recorder (no in-memory flag):
with no prior statistics it backfills the entry's configured months of history
(``CONF_BACKFILL_MONTHS``) once, and
thereafter fetches incrementally but always covers the current billing cycle so
cycle-to-date totals are correct. Plans are resolved per meter (a subscriber can
be on different plans per meter); an account-wide options value overrides
detection. The coordinator's ``data`` maps each meter id to its ``MeterData``.
"""

from __future__ import annotations

import random
from asyncio import sleep
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from enovapower import (
    AsyncEnovaClient,
    BillingPeriod,
    EnovaAuthError,
    EnovaError,
    EnovaNetworkError,
    TariffRate,
    UsageReading,
)
from enovapower.async_client import MAX_RANGE_DAYS

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CHUNK_DELAY_SECONDS,
    CONF_BACKFILL_MONTHS,
    CONF_INITIAL_BACKFILL,
    CONF_PLAN,
    CONF_STATS_VERSION,
    CURRENCY,
    DEFAULT_BACKFILL_MONTHS,
    DEFAULT_PLAN,
    DOMAIN,
    LOGGER,
    PLAN_TIERED,
    RECENT_DAYS,
    TIME_ZONE,
    UPDATE_INTERVAL,
)
from .statistics import (
    STATS_VERSION,
    TieredRates,
    async_import_meter,
    async_last_statistic_start,
    async_missing_series,
    async_scan_series,
    consumption_statistic_id,
    cost_total,
    days_covered,
    download_window,
    expected_statistic_ids,
    season_threshold,
    tiered_rates,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry


@dataclass
class MeterData:
    """Per-meter state exposed to sensors."""

    latest: UsageReading | None
    plan: str  # this meter's active plan
    cycle_energy: float  # kWh consumed this billing cycle, to the latest day
    cycle_cost: float | None  # estimated energy cost this cycle to date (CAD)
    last_bill: BillingPeriod | None  # most recent closed cycle (actual $)
    threshold: float | None  # current tier-1 kWh cap (Tiered only)
    lifetime_energy: float | None  # kWh since first import (the LTS cumulative sum)


def fetch_from_date(
    last_start: datetime | None,
    today: date,
    backfill_months: int = DEFAULT_BACKFILL_MONTHS,
) -> date:
    """Choose the download start date.

    No prior statistics → full historical backfill window of ``backfill_months``.
    Otherwise an incremental window: from just before the last stored point (to
    fill any gap after downtime and catch late revisions), but never shorter
    than the recent window.
    """
    if last_start is None:
        return today - timedelta(days=backfill_months * 31)
    return min(last_start.date() - timedelta(days=1), today - timedelta(days=RECENT_DAYS))


def current_cycle_start(periods: list[BillingPeriod], today: date) -> date:
    """First day of the current (open) billing cycle.

    The last closed cycle's read date is the day before the current cycle begins;
    with no billing data, fall back to the calendar month.
    """
    if periods:
        return max(p.end_date for p in periods) + timedelta(days=1)
    return today.replace(day=1)


def cycle_start_containing(periods: list[BillingPeriod], d: date) -> date:
    """First day of the tier-split group containing ``d`` (see ``_cycle_key``).

    Inside a known billing cycle (``start_date < d <= end_date`` — ``start_date``
    is the previous read date, exclusive) that's the cycle's first day. Days
    outside every known cycle are split by calendar month, so reach back to the
    first of ``d``'s month — but never into a closed cycle: a window that
    starts partway through a cycle would re-split it from a zero cumulative and
    rewrite its real tier rows. Downloading from here keeps the tier split
    stable across imports.
    """
    for p in periods:
        if p.start_date < d <= p.end_date:
            return p.start_date + timedelta(days=1)
    month_start = d.replace(day=1)
    closed_before = [p.end_date for p in periods if p.end_date < d]
    if closed_before and max(closed_before) >= month_start:
        return max(closed_before) + timedelta(days=1)
    return month_start


def cycle_end_containing(periods: list[BillingPeriod], d: date) -> date:
    """Last day of the tier-split group containing ``d`` (the mirror of
    ``cycle_start_containing``).

    Inside a known billing cycle that's the cycle's read date. Days outside
    every known cycle are split by calendar month, so reach forward to the end
    of ``d``'s month — but never into the next known cycle, which starts the
    day after its ``start_date`` (the previous read date). A backfill range
    ending here re-imports whole groups, keeping the tier split stable.
    """
    for p in periods:
        if p.start_date < d <= p.end_date:
            return p.end_date
    month_end = (d.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    opened_after = [p.start_date for p in periods if p.start_date >= d]
    if opened_after and min(opened_after) <= month_end:
        return min(opened_after)
    return month_end


def _drop_set(broken: dict[str, list[datetime]]) -> set[tuple[str, datetime]]:
    """Flatten ``{statistic_id: [drop hours]}`` into ``(statistic_id, hour)`` pairs."""
    return {(statistic_id, start) for statistic_id, starts in broken.items() for start in starts}


class EnovaPowerCoordinator(DataUpdateCoordinator[dict[str, "MeterData"]]):
    """Coordinate Enova Power downloads and statistics imports (per meter)."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: AsyncEnovaClient,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            LOGGER,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
            config_entry=entry,
        )
        self.client = client
        # History depth for a full backfill (chosen at setup).
        self._backfill_months: int = entry.data.get(
            CONF_BACKFILL_MONTHS, DEFAULT_BACKFILL_MONTHS
        )
        # Local date range requested by the backfill action, run by the next refresh.
        self._backfill_request: tuple[date, date] | None = None
        # Account-wide scraped tariff rates (all plans), refreshed each cycle and
        # read by the rate-card and current-rate sensors. Empty until first fetch.
        self.rates: list[TariffRate] = []
        # One-time repair pending: the entry's series were last imported under
        # an older STATS_VERSION, so every meter's first cycle is forced through
        # a full re-import from its oldest stored date (see
        # ``_full_reimport_from`` and heal rule 3) regardless of what the
        # integrity check finds — no clear, in place. Stamped complete (and the
        # flag dropped) only after a fully successful cycle, so any failure
        # retries the whole repair idempotently.
        self._rebuild = entry.data.get(CONF_STATS_VERSION, 1) < STATS_VERSION
        # Statistics integrity (see ``_download_from``): a cumulative sum that
        # falls means stored history is broken; the fix is a full re-import
        # from the meter's oldest stored date, at most once per meter per day.
        self._scanned: set[str] = set()  # meters whose startup scan has run
        self._oldest: dict[str, date] = {}  # meter → oldest stored local date
        self._heal_pending: set[str] = set()  # meters awaiting a full re-import
        self._last_heal: dict[str, date] = {}  # meter → day of its last heal
        # meter → the (statistic id, hour) drops present when it was last healed
        self._pre_heal: dict[str, set[tuple[str, datetime]]] = {}
        # drops that survived a heal: logged as ERROR once, never healed again
        self._unhealable: set[tuple[str, datetime]] = set()

    @callback
    def async_request_backfill(
        self,
        *,
        months: int | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> None:
        """Re-import every meter's usage for ``start..end`` (``end`` defaults
        to today), or else for the last ``months`` (default: the entry's
        backfill depth), and start that refresh now in the background.

        The range is imported in place alongside the normal window (see
        ``_update_meter``): nothing is cleared, and history outside it is kept.
        One attempt only; if it fails, the action has to be run again.
        """
        today = date.today()
        if start is None:
            start = today - timedelta(days=(months or self._backfill_months) * 31)
        self._backfill_request = (start, end or today)
        LOGGER.info("Backfill of %s to %s requested", *self._backfill_request)
        self.config_entry.async_create_background_task(
            self.hass, self.async_request_refresh(), f"{DOMAIN} backfill"
        )

    def plan_override(self) -> str | None:
        """Account-wide plan override (options, or a legacy configured value)."""
        entry = self.config_entry
        return entry.options.get(CONF_PLAN) or entry.data.get(CONF_PLAN)

    async def _meter_plan(self, meter_id: str) -> str:
        """Resolve a meter's plan: override, else portal detection, else default."""
        override = self.plan_override()
        if override:
            return override
        try:
            return await self.client.get_current_plan(meter_id) or DEFAULT_PLAN
        except EnovaError as err:
            LOGGER.warning("Could not detect plan for meter %s: %s", meter_id, err)
            return DEFAULT_PLAN

    async def _async_update_data(self) -> dict[str, MeterData]:
        """Fetch + import each meter; return per-meter state for the sensors."""
        today = date.today()
        data: dict[str, MeterData] = {}
        # A requested backfill gets this one cycle; a request arriving while
        # it runs waits for the next.
        backfill, self._backfill_request = self._backfill_request, None
        # A new entry applies its chosen depth once, only reaching further back
        # than history already stored; retried until a cycle succeeds.
        initial = backfill is None and self.config_entry.data.get(
            CONF_INITIAL_BACKFILL, False
        )
        if initial:
            backfill = (fetch_from_date(None, today, self._backfill_months), today)
        try:
            self.rates = await self._fetch_rates(today)
            tiered = tiered_rates(self.rates)
            for meter_id in self.client.meter_ids:
                data[meter_id] = await self._update_meter(
                    meter_id, today, tiered, backfill, extend_only=initial
                )
        except EnovaAuthError as err:  # also covers EnovaSessionExpiredError
            raise ConfigEntryAuthFailed(str(err)) from err
        except EnovaError as err:  # also covers EnovaNetworkError + parse/form errors
            if backfill is not None and not initial:
                LOGGER.warning(
                    "Backfill of %s to %s failed (%s); run the backfill action again",
                    *backfill,
                    err,
                )
            raise UpdateFailed(str(err)) from err
        if backfill is not None and not initial:
            LOGGER.info("Backfill of %s to %s complete", *backfill)
        # Data-only updates: options changes are what reload the entry
        # (OptionsFlowWithReload), so recording these doesn't trigger one.
        updates: dict[str, int | bool] = {}
        if initial:
            updates[CONF_INITIAL_BACKFILL] = False
        if self._rebuild:
            # Every meter repaired; record it so the next setup doesn't repair again.
            self._rebuild = False
            updates[CONF_STATS_VERSION] = STATS_VERSION
            LOGGER.info("Statistics repair complete (v%s)", STATS_VERSION)
        if updates:
            entry = self.config_entry
            self.hass.config_entries.async_update_entry(entry, data={**entry.data, **updates})
        return data

    async def _update_meter(
        self,
        meter_id: str,
        today: date,
        tiered: TieredRates | None,
        backfill: tuple[date, date] | None = None,
        *,
        extend_only: bool = False,
    ) -> MeterData:
        """Fetch, import, and summarize a single meter.

        ``backfill`` is a requested local date range (see
        ``async_request_backfill``), widened to whole billing cycles. Whatever
        of it lies before this cycle's normal window is downloaded too, and
        both are imported as one window from the backfill's start: stored days
        between them weren't downloaded, so they keep their values and are
        only re-based. With ``extend_only`` (a new entry's initial backfill)
        it runs only when the meter's stored history starts later than the
        range: with nothing stored the normal backfill already covers it, and
        history already reaching that far is not re-imported.
        """
        plan = await self._meter_plan(meter_id)
        try:
            periods = await self.client.billing_periods(meter_id)
        except EnovaError as err:
            LOGGER.warning("Could not fetch billing cycles for %s: %s", meter_id, err)
            periods = []
        ids = expected_statistic_ids(meter_id, plan, self.rates, tiered)

        from_date, healing = await self._download_from(meter_id, ids, periods, today)
        readings = await self._download_usage(meter_id, from_date, today)
        window_start = from_date
        if backfill is not None and extend_only:
            oldest = self._oldest.get(meter_id)
            if oldest is None or oldest <= backfill[0]:
                backfill = None
        if backfill is not None:
            start = cycle_start_containing(periods, backfill[0])
            end = min(
                cycle_end_containing(periods, backfill[1]), from_date - timedelta(days=1)
            )
            if start <= end:
                LOGGER.info("Meter %s: backfilling %s to %s", meter_id, start, end)
                older = await self._download_backfill(meter_id, start, end)
                if older:
                    readings = older + readings
                    window_start = start
        result = await async_import_meter(
            self.hass,
            meter_id,
            readings,
            plan,
            self.rates,
            tiered,
            periods,
            CURRENCY,
            window=download_window(window_start, today),
            covered_days=days_covered(readings),
        )
        if healing:
            self._finish_heal(meter_id, result.broken, today)
        else:
            self._record_drops(meter_id, result.broken, today)

        cycle_start = current_cycle_start(periods, today)
        cycle = [r for r in readings if r.date >= cycle_start]
        return MeterData(
            latest=max(readings, key=lambda r: r.date) if readings else None,
            plan=plan,
            cycle_energy=sum(r.total for r in cycle),
            cycle_cost=cost_total(cycle, plan, self.rates, tiered, periods) if cycle else None,
            last_bill=max(periods, key=lambda p: p.end_date) if periods else None,
            threshold=season_threshold(today) if plan == PLAN_TIERED else None,
            lifetime_energy=result.total,
        )

    async def _download_backfill(
        self, meter_id: str, start: date, end: date
    ) -> list[UsageReading]:
        """Download a backfill range; a range with no usage at all (e.g. before
        move-in) is logged and skipped rather than failing the whole cycle."""
        try:
            return await self._download_usage(meter_id, start, end)
        except (EnovaAuthError, EnovaNetworkError):
            raise
        except EnovaError as err:
            LOGGER.warning(
                "Meter %s: the portal has no usage data for %s to %s (%s)",
                meter_id,
                start,
                end,
                err,
            )
            return []

    async def _download_usage(
        self, meter_id: str, from_date: date, to_date: date
    ) -> list[UsageReading]:
        """Download ``[from_date, to_date]`` in portal-sized chunks, newest first.

        The portal answers a range with no usage (before the subscriber moved
        in, past what it keeps, or a gap in the middle) with an empty export,
        which the library rejects as a malformed CSV. Skip such chunks and keep
        the rest instead of failing the cycle, which would fail setup and lose
        the history around the gap. Only every chunk failing fails the cycle,
        and auth or network errors always do. Chunks are spaced by a random
        pause (``CHUNK_DELAY_SECONDS``) so a deep backfill doesn't hammer the
        portal.

        The portal can also silently return less than asked: a range ending
        today comes back as only its last 30 days. So each next chunk ends
        the day before the earliest reading received, not before the chunk's
        own start, and the days it left out are asked for again.
        """
        readings: list[UsageReading] = []
        empty: list[tuple[date, date]] = []  # skipped ranges, newest first
        error: EnovaError | None = None
        fetched = False
        chunk_end = to_date
        while chunk_end >= from_date:
            if chunk_end != to_date:
                await sleep(random.uniform(*CHUNK_DELAY_SECONDS))
            chunk_start = max(from_date, chunk_end - timedelta(days=MAX_RANGE_DAYS - 1))
            try:
                chunk = await self.client.download_usage(
                    chunk_start, chunk_end, meter_id=meter_id
                )
            except (EnovaAuthError, EnovaNetworkError):
                raise
            except EnovaError as err:
                error = error or err
                # Merge with the adjacent newer gap: one entry per gap.
                if empty and empty[-1][0] == chunk_end + timedelta(days=1):
                    empty[-1] = (chunk_start, empty[-1][1])
                else:
                    empty.append((chunk_start, chunk_end))
            else:
                fetched = True
                chunk = [r for r in chunk if chunk_start <= r.date <= chunk_end]
                readings = chunk + readings
                first = min((r.date for r in chunk), default=None)
                if first is not None and chunk_start < first <= chunk_end:
                    chunk_end = first - timedelta(days=1)  # ask for the rest
                    continue
            chunk_end = chunk_start - timedelta(days=1)
        if error is not None:
            if not fetched:
                raise error
            LOGGER.warning(
                "Meter %s: the portal has no usage data for %s (%s); imported the rest",
                meter_id,
                ", ".join(f"{start} to {end}" for start, end in reversed(empty)),
                error,
            )
        return readings

    async def _download_from(
        self,
        meter_id: str,
        ids: list[str],
        periods: list[BillingPeriod],
        today: date,
    ) -> tuple[date, bool]:
        """This cycle's download start date, and whether it is a heal cycle.

        The start is the earliest of every trigger — the recent/current-cycle
        window, a full backfill when a series is missing, and the meter's
        oldest stored date when it is being healed — then snapped back to the
        start of the billing cycle it falls in, so the tier split is computed
        on whole cycles (when a bill posts, this reaches back over the newly
        closed cycle once, rewriting its month-keyed split as cycle-keyed).

        A heal is due when one is pending and the meter was not already
        healed today; drops found by the startup scan are healed right away.
        A pending ``STATS_VERSION`` repair (``self._rebuild``) forces every
        meter's first cycle to heal too, regardless of what the check finds —
        the startup scan still runs first so ``_full_reimport_from`` has an
        oldest date to work from (see heal rule 3).
        """
        last_start = await self._incremental_start(meter_id, ids)
        if meter_id not in self._scanned:
            await self._startup_scan(meter_id, ids)
        healing = self._rebuild or (
            meter_id in self._heal_pending and self._last_heal.get(meter_id) != today
        )

        from_date = min(
            fetch_from_date(last_start, today, self._backfill_months),
            current_cycle_start(periods, today),
        )
        if healing:
            heal_from = await self._full_reimport_from(meter_id)
            from_date = min(
                from_date, heal_from or fetch_from_date(None, today, self._backfill_months)
            )
            LOGGER.info(
                "Meter %s: re-importing its full history from %s to heal its statistics",
                meter_id,
                from_date,
            )
        return cycle_start_containing(periods, from_date), healing

    async def _incremental_start(self, meter_id: str, ids: list[str]) -> datetime | None:
        """The newest stored consumption hour, or None to backfill full history.

        None when nothing is stored yet, or when a series added by an upgrade
        has no rows: imports never reach behind a series' first point on
        their own, so it can only get its history from a full refetch now.
        """
        last_start = await async_last_statistic_start(
            self.hass, consumption_statistic_id(meter_id)
        )
        if last_start is None:
            LOGGER.debug("No prior statistics for %s; backfilling", meter_id)
            return None
        missing = await async_missing_series(self.hass, ids)
        if missing:
            LOGGER.info(
                "Meter %s gained %d statistics series; refetching full "
                "history once to backfill them",
                meter_id,
                len(missing),
            )
            return None
        return last_start

    async def _startup_scan(self, meter_id: str, ids: list[str]) -> None:
        """Once per meter: check every series' full history and cache the
        meter's oldest stored date; any drop found is healed this same cycle."""
        scan = await async_scan_series(self.hass, ids)
        self._scanned.add(meter_id)
        firsts = [s.first_start for s in scan.values() if s.first_start is not None]
        if firsts:
            self._oldest[meter_id] = min(firsts).astimezone(TIME_ZONE).date()
        drops = _drop_set({statistic_id: s.drops for statistic_id, s in scan.items()})
        if drops:
            self._schedule_heal(meter_id, drops, "now")

    async def _full_reimport_from(self, meter_id: str) -> date | None:
        """Where a full re-import (a heal, or the format rebuild) starts.

        The meter's oldest stored local date, so every series is rewritten
        from its first row with no anchor; None when the startup scan found
        no rows — the caller then uses the normal backfill window. Any cycle
        importing from here is a heal cycle (see ``_finish_heal``).
        """
        return self._oldest.get(meter_id)

    def _finish_heal(
        self, meter_id: str, broken: dict[str, list[datetime]], today: date
    ) -> None:
        """Book-keep a completed heal cycle.

        Its own check saw the pre-heal data (the read that preceded the
        rewrite), so that drop set is kept as the pre-heal snapshot for the
        next cycle's verdict rather than acted on: no warning, no error, no
        re-schedule.
        """
        self._heal_pending.discard(meter_id)
        self._last_heal[meter_id] = today
        self._pre_heal[meter_id] = _drop_set(broken)

    def _record_drops(
        self, meter_id: str, broken: dict[str, list[datetime]], today: date
    ) -> None:
        """Act on a normal cycle's check result.

        A drop that was present before the last heal and still is cannot be
        fixed from portal data: log it as an error once and never heal it
        again (until restart). Any other drop schedules a heal.
        """
        reported = _drop_set(broken)
        persistent = (reported & self._pre_heal.pop(meter_id, set())) - self._unhealable
        for statistic_id, start in sorted(persistent):
            LOGGER.error(
                "%s still has a sum drop at %s after a full re-import from the "
                "portal; it will not be re-imported for this again until restart",
                statistic_id,
                start,
            )
        self._unhealable |= persistent
        new = reported - self._unhealable
        if not new:
            return
        # At most one heal per day: a drop found after today's heal waits.
        when = (
            "tomorrow (already re-imported once today)"
            if self._last_heal.get(meter_id) == today
            else "on the next update"
        )
        self._schedule_heal(meter_id, new, when)

    def _schedule_heal(self, meter_id: str, drops: set[tuple[str, datetime]], when: str) -> None:
        """Queue a full re-import of ``meter_id`` and warn once (``when`` says
        when it will run); a no-op while one is already pending."""
        if meter_id in self._heal_pending:
            return
        self._heal_pending.add(meter_id)
        statistic_id, start = min(drops, key=lambda drop: drop[1])
        LOGGER.warning(
            "Meter %s: %d statistics sum drop(s) detected (first: %s at %s); "
            "re-importing its full history %s",
            meter_id,
            len(drops),
            statistic_id,
            start,
            when,
        )

    async def _fetch_rates(self, today: date) -> list[TariffRate]:
        """Current tariff rates for all plans (best effort; empty on failure)."""
        try:
            return await self.client.download_tariff(today - timedelta(days=30), today)
        except EnovaError as err:
            LOGGER.warning("Could not fetch tariff prices: %s", err)
            return []
