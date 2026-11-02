"""The Yahoo finance component.

https://github.com/iprak/yahoofinance
"""

from __future__ import annotations

import asyncio
import contextlib
from copy import deepcopy
from datetime import timedelta
from typing import Any

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.const import CONF_SCAN_INTERVAL, SERVICE_RELOAD, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.reload import async_integration_yaml_config
from homeassistant.helpers.typing import ConfigType

from custom_components.yahoofinance.const import (
    CONF_DECIMAL_PLACES,
    CONF_INCLUDE_DIVIDEND_VALUES,
    CONF_INCLUDE_FIFTY_DAY_VALUES,
    CONF_INCLUDE_FIFTY_TWO_WEEK_VALUES,
    CONF_INCLUDE_POST_VALUES,
    CONF_INCLUDE_PRE_VALUES,
    CONF_INCLUDE_TWO_HUNDRED_DAY_VALUES,
    CONF_NO_UNIT,
    CONF_SHOW_CURRENCY_SYMBOL_AS_UNIT,
    CONF_SHOW_OFF_MARKET_VALUES,
    CONF_SHOW_TRENDING_ICON,
    CONF_SYMBOLS,
    CONF_TARGET_CURRENCY,
    DEFAULT_CONF_DECIMAL_PLACES,
    DEFAULT_CONF_INCLUDE_DIVIDEND_VALUES,
    DEFAULT_CONF_INCLUDE_FIFTY_DAY_VALUES,
    DEFAULT_CONF_INCLUDE_FIFTY_TWO_WEEK_VALUES,
    DEFAULT_CONF_INCLUDE_POST_VALUES,
    DEFAULT_CONF_INCLUDE_PRE_VALUES,
    DEFAULT_CONF_INCLUDE_TWO_HUNDRED_DAY_VALUES,
    DEFAULT_CONF_NO_UNIT,
    DEFAULT_CONF_SHOW_CURRENCY_SYMBOL_AS_UNIT,
    DEFAULT_CONF_SHOW_OFF_MARKET_VALUES,
    DEFAULT_CONF_SHOW_TRENDING_ICON,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    HASS_DATA_CONFIG,
    HASS_DATA_COORDINATORS,
    LOGGER,
    MANUAL_SCAN_INTERVAL,
    MAX_LINE_SIZE,
    MINIMUM_SCAN_INTERVAL,
    SERVICE_REFRESH,
)
from custom_components.yahoofinance.coordinator import (
    CrumbCoordinator,
    YahooSymbolUpdateCoordinator,
)
from custom_components.yahoofinance.dataclasses import SymbolDefinition

BASIC_SYMBOL_SCHEMA = vol.All(cv.string, vol.Upper)


def minimum_scan_interval(value: timedelta) -> timedelta:
    """Validate scan_interval is the minimum value."""
    if value < MINIMUM_SCAN_INTERVAL:
        raise vol.Invalid(
            f"Scan interval should be at least {MINIMUM_SCAN_INTERVAL.total_seconds()} seconds"
        )
    return value


MANUAL_SCAN_INTERVAL_SCHEMA = vol.All(vol.Lower, MANUAL_SCAN_INTERVAL)
CUSTOM_SCAN_INTERVAL_SCHEMA = vol.All(cv.time_period, minimum_scan_interval)
SCAN_INTERVAL_SCHEMA = vol.Any(MANUAL_SCAN_INTERVAL_SCHEMA, CUSTOM_SCAN_INTERVAL_SCHEMA)

COMPLEX_SYMBOL_SCHEMA = vol.All(
    dict,
    vol.Schema(
        {
            vol.Required("symbol"): BASIC_SYMBOL_SCHEMA,
            vol.Optional(CONF_TARGET_CURRENCY): BASIC_SYMBOL_SCHEMA,
            vol.Optional(CONF_SCAN_INTERVAL): SCAN_INTERVAL_SCHEMA,
            vol.Optional(CONF_NO_UNIT, default=DEFAULT_CONF_NO_UNIT): cv.boolean,
        }
    ),
)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required(CONF_SYMBOLS): vol.All(
                    cv.ensure_list,
                    [vol.Any(BASIC_SYMBOL_SCHEMA, COMPLEX_SYMBOL_SCHEMA)],
                ),
                vol.Optional(
                    CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL
                ): SCAN_INTERVAL_SCHEMA,
                vol.Optional(CONF_TARGET_CURRENCY): vol.All(cv.string, vol.Upper),
                vol.Optional(
                    CONF_SHOW_TRENDING_ICON, default=DEFAULT_CONF_SHOW_TRENDING_ICON
                ): cv.boolean,
                vol.Optional(
                    CONF_SHOW_CURRENCY_SYMBOL_AS_UNIT,
                    default=DEFAULT_CONF_SHOW_CURRENCY_SYMBOL_AS_UNIT,
                ): cv.boolean,
                vol.Optional(CONF_DECIMAL_PLACES, default=DEFAULT_CONF_DECIMAL_PLACES): vol.Coerce(
                    int
                ),
                vol.Optional(
                    CONF_INCLUDE_FIFTY_DAY_VALUES,
                    default=DEFAULT_CONF_INCLUDE_FIFTY_DAY_VALUES,
                ): cv.boolean,
                vol.Optional(
                    CONF_INCLUDE_POST_VALUES, default=DEFAULT_CONF_INCLUDE_POST_VALUES
                ): cv.boolean,
                vol.Optional(
                    CONF_INCLUDE_PRE_VALUES, default=DEFAULT_CONF_INCLUDE_PRE_VALUES
                ): cv.boolean,
                vol.Optional(
                    CONF_INCLUDE_TWO_HUNDRED_DAY_VALUES,
                    default=DEFAULT_CONF_INCLUDE_TWO_HUNDRED_DAY_VALUES,
                ): cv.boolean,
                vol.Optional(
                    CONF_INCLUDE_FIFTY_TWO_WEEK_VALUES,
                    default=DEFAULT_CONF_INCLUDE_FIFTY_TWO_WEEK_VALUES,
                ): cv.boolean,
                vol.Optional(
                    CONF_INCLUDE_DIVIDEND_VALUES,
                    default=DEFAULT_CONF_INCLUDE_DIVIDEND_VALUES,
                ): cv.boolean,
                vol.Optional(
                    CONF_SHOW_OFF_MARKET_VALUES,
                    default=DEFAULT_CONF_SHOW_OFF_MARKET_VALUES,
                ): cv.boolean,
            }
        )
    },
    # The complete HA configuration is passed down to`async_setup`, allow the extra keys.
    extra=vol.ALLOW_EXTRA,
)


def normalize_input_symbols(defined_symbols: list) -> list[SymbolDefinition]:
    """Normalize input and remove duplicates."""
    symbols = set()
    symbol_definitions: list[SymbolDefinition] = []

    for value in defined_symbols:
        if isinstance(value, str):
            if value not in symbols:
                symbols.add(value)
                symbol_definitions.append(SymbolDefinition(value))
        else:
            symbol = value["symbol"]
            if symbol not in symbols:
                symbols.add(symbol)
                symbol_definitions.append(
                    SymbolDefinition(
                        symbol,
                        target_currency=value.get(CONF_TARGET_CURRENCY),
                        scan_interval=value.get(CONF_SCAN_INTERVAL),
                        no_unit=value.get(CONF_NO_UNIT),
                    )
                )

    return symbol_definitions


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the component."""
    hass.data.setdefault(DOMAIN, {HASS_DATA_COORDINATORS: {}})
    if DOMAIN in config:
        data = _serialize_config(config[DOMAIN])
        entries = hass.config_entries.async_entries(DOMAIN)
        if entries:
            hass.config_entries.async_update_entry(entries[0], data=data)
        else:
            hass.async_create_task(
                hass.config_entries.flow.async_init(
                    DOMAIN, context={"source": SOURCE_IMPORT}, data=data
                )
            )

    async def handle_refresh_symbols(_call: ServiceCall) -> None:
        """Refresh symbol data."""
        LOGGER.info("Processing refresh_symbols")

        # Always use latest coordinators to handle config reload
        coordinators: dict[timedelta, YahooSymbolUpdateCoordinator] = hass.data[DOMAIN][
            HASS_DATA_COORDINATORS
        ]
        if coordinators:
            for coordinator in coordinators.values():
                await coordinator.async_refresh()

    async def _async_reload_service_handler(service: ServiceCall) -> None:
        """Handle reload service call."""
        reload_config = None
        with contextlib.suppress(HomeAssistantError):
            reload_config = await async_integration_yaml_config(hass, DOMAIN)
        if reload_config is None:
            return

        if DOMAIN not in reload_config:
            for entry in hass.config_entries.async_entries(DOMAIN):
                await hass.config_entries.async_remove(entry.entry_id)
            return
        data = _serialize_config(CONFIG_SCHEMA(reload_config)[DOMAIN])
        entries = hass.config_entries.async_entries(DOMAIN)
        if entries:
            entry = entries[0]
            hass.config_entries.async_update_entry(entry, data=data)
            await hass.config_entries.async_reload(entry.entry_id)
        else:
            await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": SOURCE_IMPORT}, data=data
            )

    hass.services.async_register(DOMAIN, SERVICE_REFRESH, handle_refresh_symbols)
    hass.services.async_register(DOMAIN, SERVICE_RELOAD, _async_reload_service_handler)
    return True


def _serialize_config(value: Any) -> Any:
    """Convert YAML settings for the config-entry store.

    Args:
        value: A validated YAML value, collection, or scan interval.

    Returns:
        The same settings with timedelta values represented as seconds.
    """
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, dict):
        return {key: _serialize_config(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_serialize_config(item) for item in value]
    return value


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Load YAML-managed sensors through their config entry.

    Args:
        hass: The Home Assistant instance.
        entry: The imported settings for the single integration instance.

    Returns:
        True after the sensor platform has loaded.

    Raises:
        ConfigEntryNotReady: If Yahoo cannot provide a session crumb.
    """
    domain_config = CONFIG_SCHEMA({DOMAIN: deepcopy(dict(entry.data))})[DOMAIN]
    defined_symbols = domain_config.get(CONF_SYMBOLS, [])

    symbol_definitions: list[SymbolDefinition]
    symbol_definitions = normalize_input_symbols(defined_symbols)
    domain_config[CONF_SYMBOLS] = symbol_definitions

    global_scan_interval = domain_config.get(CONF_SCAN_INTERVAL)

    # Populate parsed value into domain_config
    domain_config[CONF_SCAN_INTERVAL] = global_scan_interval

    # Group symbols by scan_interval
    symbols_by_scan_interval: dict[str | timedelta | None, list[str]] = {}
    for symbol in symbol_definitions:
        # Use integration level scan_interval if none defined
        if symbol.scan_interval is None:
            symbol.scan_interval = global_scan_interval

        if symbol.scan_interval in symbols_by_scan_interval:
            symbols_by_scan_interval[symbol.scan_interval].append(symbol.symbol)
        else:
            symbols_by_scan_interval[symbol.scan_interval] = [symbol.symbol]

    LOGGER.info("Total %d unique scan intervals", len(symbols_by_scan_interval))

    _remove_stale_symbols(hass, entry, {symbol.symbol for symbol in symbol_definitions})

    websession = async_create_clientsession(
        hass, max_field_size=MAX_LINE_SIZE, max_line_size=MAX_LINE_SIZE
    )
    coordinators = {}
    try:
        crumb_coordinator = CrumbCoordinator(hass, websession)
        if await crumb_coordinator.try_get_crumb_cookies() is None:
            raise ConfigEntryNotReady("Unable to get Yahoo Finance crumb")

        for interval, symbols in symbols_by_scan_interval.items():
            coordinator = YahooSymbolUpdateCoordinator(
                symbols, hass, interval, crumb_coordinator, websession
            )
            coordinators[interval] = coordinator
            await coordinator.async_refresh()

        hass.data[DOMAIN] = {
            HASS_DATA_CONFIG: domain_config,
            HASS_DATA_COORDINATORS: coordinators,
            "session": websession,
        }
        await hass.config_entries.async_forward_entry_setups(entry, [Platform.SENSOR])
        for coordinator in coordinators.values():
            if not coordinator.last_update_success:
                hass.async_create_task(coordinator.async_request_refresh())
    except (Exception, asyncio.CancelledError):
        for coordinator in coordinators.values():
            await coordinator.async_shutdown()
        await websession.close()
        raise
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Stop sensors and release their polling and HTTP resources.

    Args:
        hass: The Home Assistant instance.
        entry: The integration entry to unload.

    Returns:
        True if the sensor platform was unloaded; otherwise False.
    """
    if not await hass.config_entries.async_unload_platforms(entry, [Platform.SENSOR]):
        return False
    runtime = hass.data[DOMAIN]
    for coordinator in runtime.get(HASS_DATA_COORDINATORS, {}).values():
        await coordinator.async_shutdown()
    if session := runtime.get("session"):
        await session.close()
    hass.data[DOMAIN] = {HASS_DATA_COORDINATORS: {}}
    return True


def convert_to_float(value) -> float | None:
    """Convert specified value to float."""
    try:
        return float(value)
    except:  # noqa: E722 pylint: disable=bare-except
        return None


def _remove_stale_symbols(hass: HomeAssistant, entry: ConfigEntry, symbols: set[str]) -> None:
    """Remove deleted tickers without replacing retained registry settings.

    Args:
        hass: The Home Assistant instance.
        entry: The config entry that owns the ticker records.
        symbols: The ticker identifiers retained in the new YAML settings.
    """
    registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if entity.platform == DOMAIN and entity.unique_id not in symbols:
            registry.async_remove(entity.entity_id)
    devices = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(devices, entry.entry_id):
        if not device.identifiers.intersection({(DOMAIN, symbol) for symbol in symbols}):
            devices.async_update_device(device.id, remove_config_entry_id=entry.entry_id)
