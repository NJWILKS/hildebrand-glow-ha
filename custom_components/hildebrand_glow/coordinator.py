"""Data update coordinator for Hildebrand Glow integration."""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    DailyReading,
    GlowmarktApiClient,
    GlowmarktApiError,
    GlowmarktAuthError,
    UK_TZ,
)
from .const import (
    CLASSIFIER_ELECTRICITY_CONSUMPTION,
    CLASSIFIER_ELECTRICITY_COST,
    CLASSIFIER_GAS_CONSUMPTION,
    CLASSIFIER_GAS_COST,
    DEFAULT_CONSUMPTION_INTERVAL,
    DEFAULT_COST_INTERVAL,
    DOMAIN,
    HISTORY_START_DELAY_SECONDS,
    MIN_POLL_INTERVAL,
)
from .cost_ingestion import async_sync_cost_ingestion
from .costing import CostBreakdown, get_latest_cost_breakdown
from .identity import sensor_unique_id, site_identity

_LOGGER = logging.getLogger(__name__)
CUMULATIVE_CLASSIFIERS = (
    CLASSIFIER_ELECTRICITY_CONSUMPTION,
    CLASSIFIER_GAS_CONSUMPTION,
)
API_COST_CLASSIFIERS = (
    CLASSIFIER_ELECTRICITY_COST,
    CLASSIFIER_GAS_COST,
)
COMMODITY_CLASSIFIERS = {
    "electricity": (
        CLASSIFIER_ELECTRICITY_CONSUMPTION,
        CLASSIFIER_ELECTRICITY_COST,
    ),
    "gas": (
        CLASSIFIER_GAS_CONSUMPTION,
        CLASSIFIER_GAS_COST,
    ),
}
CUMULATIVE_STORAGE_VERSION = 1
CUMULATIVE_BACKFILL_SCHEMA_VERSION = 3


class GlowmarktDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Manage Glowmarkt rolling data, cumulative values and history backfill."""

    def __init__(
        self,
        hass: HomeAssistant,
        api_client: GlowmarktApiClient,
        tariff_config: dict[str, float],
        virtual_entity_id: str | None = None,
        entry_id: str = "default",
        consumption_interval_minutes: int = DEFAULT_CONSUMPTION_INTERVAL,
        cost_interval_minutes: int = DEFAULT_COST_INTERVAL,
    ) -> None:
        self._consumption_interval_minutes = max(
            MIN_POLL_INTERVAL,
            int(consumption_interval_minutes),
        )
        self._cost_interval_minutes = max(
            MIN_POLL_INTERVAL,
            int(cost_interval_minutes),
        )
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=self._consumption_interval_minutes),
        )
        self.api_client = api_client
        self.tariff_config = tariff_config
        self._virtual_entity_id = virtual_entity_id
        self._entry_id = entry_id
        self._site_id = site_identity(virtual_entity_id, entry_id)
        self._resources: dict[str, dict[str, Any]] = {}
        self._last_readings: dict[str, DailyReading] = {}
        self._cost_breakdowns: dict[str, CostBreakdown] = {}
        self._last_cost_refresh_at: datetime | None = None
        self._store = Store(
            hass,
            CUMULATIVE_STORAGE_VERSION,
            f"{DOMAIN}_{self._site_id}_cumulative",
        )
        self._cumulative: dict[str, Any] | None = None
        self._cumulative_lock = asyncio.Lock()
        self._cost_history_lock = asyncio.Lock()
        self._backfill_started = False
        self._history_start_scheduled = False
        self._entities_ready = False
        self._history_task: asyncio.Task[Any] | None = None
        self._delayed_history_task: asyncio.Task[Any] | None = None

    def _resource_id_for(self, classifier: str) -> str | None:
        resource = self._resources.get(classifier)
        if resource is None:
            return None
        return resource.get("resource_id")

    def _entity_id_for(self, classifier: str) -> str | None:
        registry = er.async_get(self.hass)
        unique_id = sensor_unique_id(
            self._site_id,
            classifier,
            self._resource_id_for(classifier),
        )
        return registry.async_get_entity_id("sensor", DOMAIN, unique_id)

    @staticmethod
    def _metadata_for(entity_id: str) -> StatisticMetaData:
        return StatisticMetaData(
            has_sum=True,
            mean_type=StatisticMeanType.NONE,
            name=None,
            source="recorder",
            statistic_id=entity_id,
            unit_class=None,
            unit_of_measurement="kWh",
        )

    def _import_hourly_statistics(
        self,
        entity_id: str,
        reading: DailyReading,
        baseline: float,
    ) -> float:
        """Import a closed day's shape using cumulative TOTAL_INCREASING state."""
        hourly: dict[datetime, float] = {}
        for timestamp, value in reading.intervals:
            hour_start = timestamp.replace(minute=0, second=0, microsecond=0)
            hourly[hour_start] = hourly.get(hour_start, 0.0) + value

        running = baseline
        stats: list[StatisticData] = []
        for hour_start in sorted(hourly):
            running += hourly[hour_start]
            cumulative = round(running, 3)
            stats.append(
                StatisticData(
                    start=hour_start,
                    state=cumulative,
                    sum=cumulative,
                )
            )
        if stats:
            async_import_statistics(
                self.hass,
                self._metadata_for(entity_id),
                stats,
            )
        return round(running, 3)

    async def _clear_statistics(self, statistic_ids: list[str]) -> None:
        """Clear legacy consumption statistics before a schema rebuild."""
        if not statistic_ids:
            return
        done = asyncio.Event()

        def _done() -> None:
            self.hass.loop.call_soon_threadsafe(done.set)

        get_instance(self.hass).async_clear_statistics(statistic_ids, on_done=_done)
        await done.wait()

    async def _load_cumulative(self) -> dict[str, Any]:
        if self._cumulative is None:
            self._cumulative = await self._store.async_load() or {}
        return self._cumulative

    @staticmethod
    def _day_start(day: date) -> datetime:
        return datetime.combine(day, time.min, tzinfo=UK_TZ)

    @classmethod
    def _expected_intervals(cls, day: date) -> int:
        start = cls._day_start(day)
        end = cls._day_start(day + timedelta(days=1))
        seconds = (
            end.astimezone(timezone.utc) - start.astimezone(timezone.utc)
        ).total_seconds()
        return int(seconds // (30 * 60))

    @classmethod
    def _reading_is_complete(cls, reading: DailyReading) -> bool:
        day = date.fromisoformat(reading.day)
        timestamps = {
            timestamp
            for timestamp, _value in reading.intervals
            if timestamp.astimezone(UK_TZ).date() == day
        }
        return len(timestamps) == cls._expected_intervals(day)

    async def _current_day_reading(
        self,
        classifier: str,
        now_uk: datetime,
    ) -> DailyReading | None:
        """Return only published, closed PT30M intervals for the current UK day."""
        resource_id = self._resource_id_for(classifier)
        if resource_id is None:
            return None
        today_start = now_uk.replace(hour=0, minute=0, second=0, microsecond=0)
        reading = await self.api_client._fetch_day_reading(  # noqa: SLF001
            resource_id,
            today_start,
            now_uk,
        )
        if reading is None:
            return None
        intervals = [
            (timestamp, value)
            for timestamp, value in reading.intervals
            if timestamp.astimezone(UK_TZ) + timedelta(minutes=30) <= now_uk
        ]
        if not intervals:
            return None
        return DailyReading(
            day=today_start.date().isoformat(),
            value=round(sum(value for _timestamp, value in intervals), 3),
            intervals=intervals,
        )

    async def _accumulate(self, classifier: str, reading: DailyReading) -> float:
        """Compatibility helper for adding one closed day exactly once."""
        async with self._cumulative_lock:
            cumulative = await self._load_cumulative()
            entry = cumulative.get(
                classifier,
                {
                    "completed_day": None,
                    "completed_cumulative": 0.0,
                    "cumulative": 0.0,
                },
            )
            previous_day = entry.get("completed_day") or entry.get("day")
            if previous_day is not None and reading.day <= previous_day:
                return float(
                    entry.get("completed_cumulative", entry.get("cumulative", 0.0))
                )

            baseline = float(
                entry.get("completed_cumulative", entry.get("cumulative", 0.0))
            )
            entity_id = self._entity_id_for(classifier)
            if entity_id is None or not reading.intervals:
                new_total = round(baseline + reading.value, 3)
            else:
                new_total = self._import_hourly_statistics(
                    entity_id,
                    reading,
                    baseline,
                )

            cumulative[classifier] = {
                "day": reading.day,
                "completed_day": reading.day,
                "completed_cumulative": new_total,
                "cumulative": new_total,
            }
            await self._store.async_save(cumulative)
            return new_total

    async def _refresh_consumption(
        self,
        now_uk: datetime,
    ) -> dict[str, float | None]:
        """Reconcile closed days, then calculate today's rolling cumulative state."""
        result: dict[str, float | None] = {
            classifier: None for classifier in CUMULATIVE_CLASSIFIERS
        }
        async with self._cumulative_lock:
            cumulative = await self._load_cumulative()
            if (
                not cumulative.get("_backfilled")
                or int(cumulative.get("_backfilled_version", 0) or 0)
                < CUMULATIVE_BACKFILL_SCHEMA_VERSION
            ):
                return result

            changed = False
            today = now_uk.date()
            today_iso = today.isoformat()
            yesterday = today - timedelta(days=1)

            for classifier in CUMULATIVE_CLASSIFIERS:
                if classifier not in self._resources:
                    continue
                entry = cumulative.get(
                    classifier,
                    {
                        "completed_day": None,
                        "completed_cumulative": 0.0,
                        "cumulative": 0.0,
                    },
                )
                completed_day_raw = entry.get("completed_day") or entry.get("day")
                if completed_day_raw is None:
                    continue
                completed_day = date.fromisoformat(str(completed_day_raw))
                baseline = float(
                    entry.get(
                        "completed_cumulative",
                        entry.get("cumulative", 0.0),
                    )
                )
                stored_live_day = entry.get("live_day")
                stored_live_intervals = int(entry.get("live_intervals", 0) or 0)
                stored_live_total = float(entry.get("cumulative", baseline))
                entity_id = self._entity_id_for(classifier)
                resource_id = self._resource_id_for(classifier)
                if entity_id is None or resource_id is None:
                    continue

                gap = completed_day + timedelta(days=1)
                gap_blocked = False
                reconciled_days = 0
                while gap <= yesterday:
                    day_start = self._day_start(gap)
                    day_end = self._day_start(gap + timedelta(days=1))
                    reading = await self.api_client._fetch_day_reading(  # noqa: SLF001
                        resource_id,
                        day_start,
                        day_end,
                    )
                    if reading is None or not self._reading_is_complete(reading):
                        _LOGGER.debug(
                            "Waiting for complete %s PT30M day %s before rolling on",
                            classifier,
                            gap,
                        )
                        gap_blocked = True
                        break
                    baseline = self._import_hourly_statistics(
                        entity_id,
                        reading,
                        baseline,
                    )
                    completed_day = gap
                    reconciled_days += 1
                    gap += timedelta(days=1)
                    changed = True

                if reconciled_days:
                    _LOGGER.info(
                        "Reconciled Glowmarkt consumption: %s=%s day(s)",
                        classifier,
                        reconciled_days,
                    )

                live_total = baseline
                live_day: str | None = None
                live_intervals = 0
                live_reading: DailyReading | None = None

                if gap_blocked:
                    # Do not let a late/incomplete closed day roll the TOTAL_INCREASING
                    # sensor backwards. Hold the last good state until reconciliation.
                    live_total = stored_live_total
                    live_day = (
                        str(stored_live_day) if stored_live_day is not None else None
                    )
                    live_intervals = stored_live_intervals
                elif completed_day == yesterday:
                    candidate = await self._current_day_reading(classifier, now_uk)
                    candidate_count = len(candidate.intervals) if candidate else 0

                    if (
                        stored_live_day == today_iso
                        and stored_live_intervals > candidate_count
                    ):
                        # Bright can transiently return a shorter partial-day window.
                        # Replaying it would look like a meter reset to Recorder.
                        live_total = stored_live_total
                        live_day = today_iso
                        live_intervals = stored_live_intervals
                        _LOGGER.warning(
                            "Glowmarkt returned fewer current-day %s intervals "
                            "(%s < %s); preserving the last good cumulative state",
                            classifier,
                            candidate_count,
                            stored_live_intervals,
                        )
                    elif candidate is not None:
                        live_reading = candidate
                        live_total = round(baseline + candidate.value, 3)
                        live_day = candidate.day
                        live_intervals = candidate_count
                        self._last_readings[classifier] = candidate
                    elif stored_live_day == today_iso and stored_live_intervals:
                        live_total = stored_live_total
                        live_day = today_iso
                        live_intervals = stored_live_intervals
                    else:
                        cached = self._last_readings.get(classifier)
                        if cached is not None and cached.day != today_iso:
                            self._last_readings.pop(classifier, None)

                cumulative[classifier] = {
                    "day": completed_day.isoformat(),
                    "completed_day": completed_day.isoformat(),
                    "completed_cumulative": round(baseline, 3),
                    "cumulative": round(live_total, 3),
                    "live_day": live_day,
                    "live_intervals": live_intervals,
                }
                result[classifier] = round(live_total, 3)
                changed = True

            if changed:
                await self._store.async_save(cumulative)
        return result

    def start_history_backfill(self) -> None:
        """Start backfill only after sensor entities exist; later polls can retry."""
        if self._backfill_started or not self._entities_ready:
            return
        if not any(self._entity_id_for(c) for c in CUMULATIVE_CLASSIFIERS):
            return
        self._backfill_started = True
        self._history_task = self.hass.async_create_task(
            self._async_backfill_history(),
            name=f"{DOMAIN} history backfill",
        )

    async def _async_delayed_history_start(self, delay: int) -> None:
        try:
            await asyncio.sleep(delay)
            self.start_history_backfill()
        finally:
            self._history_start_scheduled = False

    def schedule_history_backfill(
        self,
        delay: int = HISTORY_START_DELAY_SECONDS,
    ) -> None:
        """Mark entities ready and stagger history work after setup."""
        self._entities_ready = True
        if self._history_start_scheduled or self._backfill_started:
            return
        self._history_start_scheduled = True
        self._delayed_history_task = self.hass.async_create_task(
            self._async_delayed_history_start(delay),
            name=f"{DOMAIN} delayed history start",
        )

    async def _async_backfill_history(self) -> None:
        """Import closed historical consumption; Recorder owns the open day."""
        completed = False
        try:
            async with self._cumulative_lock:
                cumulative = await self._load_cumulative()
                previous_version = int(cumulative.get("_backfilled_version", 0) or 0)
                if (
                    cumulative.get("_backfilled")
                    and previous_version >= CUMULATIVE_BACKFILL_SCHEMA_VERSION
                ):
                    completed = True
                    return

            _LOGGER.info("Starting Glowmarkt completed consumption history backfill")
            history = await self.api_client.get_available_readings(
                set(CUMULATIVE_CLASSIFIERS)
            )

            entity_ids = [
                entity_id
                for classifier in CUMULATIVE_CLASSIFIERS
                if (entity_id := self._entity_id_for(classifier)) is not None
            ]
            expected_entities = sum(
                1 for classifier in CUMULATIVE_CLASSIFIERS if classifier in self._resources
            )
            if len(entity_ids) < expected_entities:
                _LOGGER.debug("Consumption entities not registered; backfill will retry")
                return

            # v2.3 changes current-day ownership. Clear the old statistic series once
            # so no v2.1/v2.2 synthetic/live rows can survive the migration.
            await self._clear_statistics(entity_ids)

            async with self._cumulative_lock:
                cumulative = await self._load_cumulative()
                for classifier in CUMULATIVE_CLASSIFIERS:
                    if classifier not in self._resources:
                        continue
                    entity_id = self._entity_id_for(classifier)
                    if entity_id is None:
                        return

                    baseline = 0.0
                    last_day: str | None = None
                    readings = history.get(classifier, [])
                    imported_days = 0
                    for reading in readings:
                        if not self._reading_is_complete(reading):
                            _LOGGER.warning(
                                "Stopping %s history at incomplete PT30M day %s",
                                classifier,
                                reading.day,
                            )
                            break
                        baseline = self._import_hourly_statistics(
                            entity_id,
                            reading,
                            baseline,
                        )
                        last_day = reading.day
                        imported_days += 1

                    if last_day is not None:
                        cumulative[classifier] = {
                            "day": last_day,
                            "completed_day": last_day,
                            "completed_cumulative": round(baseline, 3),
                            "cumulative": round(baseline, 3),
                            "live_day": None,
                            "live_intervals": 0,
                        }
                    _LOGGER.info(
                        "Backfilled Glowmarkt completed consumption: %s=%s day(s)",
                        classifier,
                        imported_days,
                    )

                cumulative["_backfilled"] = True
                cumulative["_backfilled_version"] = CUMULATIVE_BACKFILL_SCHEMA_VERSION
                await self._store.async_save(cumulative)
                completed = True
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("Glowmarkt history backfill failed")
        finally:
            if not completed:
                self._backfill_started = False

        if completed:
            await self.async_request_refresh()

    def _cost_refresh_due(self, now: datetime) -> bool:
        if self._last_cost_refresh_at is None:
            return True
        return (
            now - self._last_cost_refresh_at
            >= timedelta(minutes=self._cost_interval_minutes)
        )

    async def _refresh_cost_breakdowns(self, now: datetime) -> None:
        refreshed_any = False
        for commodity, (_consumption_classifier, cost_classifier) in (
            COMMODITY_CLASSIFIERS.items()
        ):
            resource_id = self._resource_id_for(cost_classifier)
            if resource_id is None:
                continue
            try:
                breakdown = await get_latest_cost_breakdown(
                    self.api_client,
                    resource_id,
                    now_uk=now.astimezone(UK_TZ),
                )
            except GlowmarktApiError as err:
                _LOGGER.warning(
                    "Glowmarkt API-cost refresh failed for %s; preserving last value: %s",
                    commodity,
                    err,
                )
                continue
            if breakdown is None:
                continue

            self._cost_breakdowns[commodity] = breakdown
            self._last_readings[cost_classifier] = DailyReading(
                day=breakdown.day,
                value=breakdown.total_pence,
                intervals=list(breakdown.usage_intervals),
            )
            refreshed_any = True

        if refreshed_any:
            self._last_cost_refresh_at = now
            if self._entities_ready:
                try:
                    await async_sync_cost_ingestion(
                        self.hass,
                        self,
                        self._site_id,
                    )
                except Exception:
                    _LOGGER.exception("Glowmarkt rolling cost ingestion failed")

    def _configured_daily_cost(self, commodity: str, consumption: float) -> float:
        return round(
            consumption * self.tariff_config.get(f"{commodity}_rate", 0)
            + self.tariff_config.get(f"{commodity}_standing_charge", 0),
            2,
        )

    def _compose_costs(
        self,
        values: dict[str, float | None],
    ) -> tuple[dict[str, float | None], dict[str, dict[str, Any]]]:
        costs: dict[str, float | None] = {}
        diagnostics: dict[str, dict[str, Any]] = {}
        standing_values: list[float] = []
        standing_unknown = False

        for commodity, (consumption_classifier, cost_classifier) in (
            COMMODITY_CLASSIFIERS.items()
        ):
            has_commodity = (
                consumption_classifier in self._resources
                or cost_classifier in self._resources
            )
            if not has_commodity:
                continue

            breakdown = self._cost_breakdowns.get(commodity)
            consumption = values.get(consumption_classifier)
            if breakdown is not None:
                costs[commodity] = round(breakdown.total_pence / 100.0, 2)
                standing = breakdown.standing_charge_pence
                costs[f"{commodity}_standing_charge"] = (
                    round(standing / 100.0, 2) if standing is not None else None
                )
                if standing is None:
                    standing_unknown = True
                else:
                    standing_values.append(standing / 100.0)
                diagnostics[commodity] = {
                    "source": "glow_api",
                    "api_day": breakdown.day,
                    "complete_day": breakdown.complete_day,
                    "usage_cost_gbp": (
                        round(breakdown.usage_pence / 100.0, 2)
                        if breakdown.usage_pence is not None
                        else None
                    ),
                    "standing_charge_status": breakdown.standing_charge_status,
                    "pt30m_intervals": len(breakdown.usage_intervals),
                }
                continue

            if consumption is not None:
                configured_standing = self.tariff_config.get(
                    f"{commodity}_standing_charge",
                    0,
                )
                costs[commodity] = self._configured_daily_cost(
                    commodity,
                    consumption,
                )
                costs[f"{commodity}_standing_charge"] = round(
                    configured_standing,
                    2,
                )
                standing_values.append(float(configured_standing))
                diagnostics[commodity] = {
                    "source": "configured_fallback",
                    "api_day": None,
                    "complete_day": True,
                    "usage_cost_gbp": None,
                    "standing_charge_status": "configured_fallback",
                    "pt30m_intervals": None,
                }
            else:
                costs[commodity] = None
                costs[f"{commodity}_standing_charge"] = None
                standing_unknown = True
                diagnostics[commodity] = {
                    "source": "unavailable",
                    "api_day": None,
                    "complete_day": None,
                    "usage_cost_gbp": None,
                    "standing_charge_status": "unknown",
                    "pt30m_intervals": None,
                }

        available_totals = [
            float(costs[commodity])
            for commodity in COMMODITY_CLASSIFIERS
            if costs.get(commodity) is not None
        ]
        costs["total"] = round(sum(available_totals), 2) if available_totals else None
        costs["standing_charges_total"] = (
            None if standing_unknown else round(sum(standing_values), 2)
        )
        return costs, diagnostics

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            if not self._resources:
                self._resources = await self.api_client.discover_resources(
                    self._virtual_entity_id
                )

            now = datetime.now(timezone.utc)
            cumulative_readings = await self._refresh_consumption(now.astimezone(UK_TZ))

            if self._cost_refresh_due(now):
                await self._refresh_cost_breakdowns(now)

            merged = {
                classifier: self._last_readings.get(classifier)
                for classifier in self._resources
            }
            values = {
                classifier: reading.value if reading is not None else None
                for classifier, reading in merged.items()
            }

            costs, cost_diagnostics = self._compose_costs(values)
            data: dict[str, Any] = {
                "readings": values,
                "cumulative_readings": cumulative_readings,
                "resources": self._resources,
                "costs": costs,
                "cost_diagnostics": cost_diagnostics,
            }

            self.start_history_backfill()
            return data
        except GlowmarktAuthError as err:
            raise UpdateFailed(f"Authentication error: {err}") from err
        except GlowmarktApiError as err:
            raise UpdateFailed(f"API error: {err}") from err

    async def async_shutdown(self) -> None:
        """Cancel coordinator-owned history tasks before unload/reset."""
        tasks = [
            task
            for task in (self._history_task, self._delayed_history_task)
            if task is not None and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    @property
    def resources(self) -> dict[str, dict[str, Any]]:
        return self._resources

    @property
    def cost_breakdowns(self) -> dict[str, CostBreakdown]:
        return self._cost_breakdowns

    @property
    def cost_history_lock(self) -> asyncio.Lock:
        return self._cost_history_lock

    @property
    def consumption_interval_minutes(self) -> int:
        return self._consumption_interval_minutes

    @property
    def cost_interval_minutes(self) -> int:
        return self._cost_interval_minutes

    def update_settings(
        self,
        tariff_config: dict[str, float],
        consumption_interval_minutes: int,
        cost_interval_minutes: int,
    ) -> None:
        """Apply options without requiring a config-entry recreation."""
        self.tariff_config = tariff_config
        self._consumption_interval_minutes = max(
            MIN_POLL_INTERVAL,
            int(consumption_interval_minutes),
        )
        self._cost_interval_minutes = max(
            MIN_POLL_INTERVAL,
            int(cost_interval_minutes),
        )
        self.update_interval = timedelta(minutes=self._consumption_interval_minutes)

    def update_tariff_config(self, tariff_config: dict[str, float]) -> None:
        """Backward-compatible helper used by older callers/tests."""
        self.tariff_config = tariff_config

    def clear_daily_cache(self) -> None:
        self._last_readings.clear()
        self._cost_breakdowns.clear()
