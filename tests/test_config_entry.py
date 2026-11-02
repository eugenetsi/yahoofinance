"""Exercise YAML import, real entity/device registration, and entry lifecycle."""

from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_IMPORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from custom_components.yahoofinance.const import (
    CONF_SYMBOLS,
    DOMAIN,
    HASS_DATA_CONFIG,
    HASS_DATA_COORDINATORS,
    MANUAL_SCAN_INTERVAL,
)
from tests import TEST_SYMBOL


@pytest.fixture
def yahoo_responses(mock_json: dict[str, Any]) -> Iterator[None]:
    """Use real coordinators and platforms without contacting Yahoo."""
    with (
        patch(
            "custom_components.yahoofinance.CrumbCoordinator.try_get_crumb_cookies",
            AsyncMock(return_value="test-crumb"),
        ),
        patch(
            "custom_components.yahoofinance.YahooSymbolUpdateCoordinator.get_json",
            AsyncMock(return_value=mock_json),
        ),
    ):
        yield


async def test_yaml_import_registers_device_and_preserves_entity(
    hass: HomeAssistant, enable_custom_integrations: None, yahoo_responses: None
) -> None:
    """Import YAML without replacing customized entities or duplicating devices."""
    registry = er.async_get(hass)
    old = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        TEST_SYMBOL,
        suggested_object_id="my_stock",
    )
    registry.async_update_entity(old.entity_id, name="My stock", icon="mdi:bank")

    assert await async_setup_component(
        hass,
        DOMAIN,
        {
            DOMAIN: {
                CONF_SYMBOLS: [TEST_SYMBOL],
                "scan_interval": "manual",
            }
        },
    )
    await hass.async_block_till_done()

    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.source == SOURCE_IMPORT
    entity = registry.async_get(old.entity_id)
    assert entity is not None
    assert entity.device_id is not None
    assert entity.config_entry_id == entry.entry_id
    assert entity.name == "My stock"
    assert entity.icon == "mdi:bank"
    assert hass.states.get(old.entity_id) is not None
    device = dr.async_get(hass).async_get(entity.device_id)
    assert device is not None
    assert device.identifiers == {(DOMAIN, TEST_SYMBOL)}
    assert device.configuration_url == f"https://finance.yahoo.com/quote/{TEST_SYMBOL}"
    assert entry.entry_id in device.config_entries
    assert len(dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)) == 1
    assert next(iter(hass.data[DOMAIN][HASS_DATA_COORDINATORS].values())).update_interval is None

    duplicate = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_IMPORT}, data=dict(entry.data)
    )
    assert duplicate["reason"] == "already_configured"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1

    old_coordinator = next(iter(hass.data[DOMAIN][HASS_DATA_COORDINATORS].values()))
    old_session = hass.data[DOMAIN]["session"]
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert old_session.closed
    assert old_coordinator._shutdown_requested
    restored = registry.async_get(old.entity_id)
    assert restored is not None
    assert restored.device_id == device.id
    assert len(dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)) == 1
    assert await hass.config_entries.async_unload(entry.entry_id)
    state = hass.states.get(old.entity_id)
    assert state is not None
    assert state.state == "unavailable"


async def test_yaml_reload_changes_symbols_and_retains_settings(
    hass: HomeAssistant, enable_custom_integrations: None, yahoo_responses: None
) -> None:
    """Reload YAML options and remove only deleted ticker records."""
    config = {DOMAIN: {CONF_SYMBOLS: [TEST_SYMBOL], "scan_interval": 120}}
    assert await async_setup_component(hass, DOMAIN, config)
    await hass.async_block_till_done()
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.data["scan_interval"] == 120
    assert hass.data[DOMAIN][HASS_DATA_CONFIG]["scan_interval"] == timedelta(seconds=120)
    previous = next(iter(hass.data[DOMAIN][HASS_DATA_COORDINATORS].values()))
    with patch(
        "custom_components.yahoofinance.async_integration_yaml_config",
        AsyncMock(
            return_value={
                DOMAIN: {
                    CONF_SYMBOLS: [{"symbol": "NEW", "scan_interval": MANUAL_SCAN_INTERVAL}],
                    "scan_interval": 300,
                    "decimal_places": 4,
                }
            }
        ),
    ):
        await hass.services.async_call(DOMAIN, "reload", {}, blocking=True)
        await hass.async_block_till_done()
    assert previous._shutdown_requested
    assert hass.data[DOMAIN][HASS_DATA_CONFIG]["decimal_places"] == 4
    assert next(iter(hass.data[DOMAIN][HASS_DATA_COORDINATORS].values())).update_interval is None
    registry = er.async_get(hass)
    assert registry.async_get_entity_id("sensor", DOMAIN, TEST_SYMBOL) is None
    new_id = registry.async_get_entity_id("sensor", DOMAIN, "NEW")
    assert new_id is not None
    devices = dr.async_get(hass)
    assert devices.async_get_device(identifiers={(DOMAIN, TEST_SYMBOL)}) is None
    new_entity = registry.async_get(new_id)
    assert new_entity is not None
    assert new_entity.device_id is not None
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_crumb_failure_is_retried_without_leaking_session(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    """Use config-entry retries when Yahoo cannot provide a session crumb."""
    with patch(
        "custom_components.yahoofinance.CrumbCoordinator.try_get_crumb_cookies",
        AsyncMock(return_value=None),
    ):
        assert await async_setup_component(hass, DOMAIN, {DOMAIN: {CONF_SYMBOLS: [TEST_SYMBOL]}})
        await hass.async_block_till_done()
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.state.value == "setup_retry"
    await hass.config_entries.async_unload(entry.entry_id)


async def test_reload_after_removing_yaml_removes_entry(
    hass: HomeAssistant, enable_custom_integrations: None, yahoo_responses: None
) -> None:
    """Remove the imported entry when the YAML section is removed."""
    assert await async_setup_component(hass, DOMAIN, {DOMAIN: {CONF_SYMBOLS: [TEST_SYMBOL]}})
    await hass.async_block_till_done()
    coordinator = next(iter(hass.data[DOMAIN][HASS_DATA_COORDINATORS].values()))
    with patch("homeassistant.config.async_hass_config_yaml", AsyncMock(return_value={})):
        await hass.services.async_call(DOMAIN, "reload", {}, blocking=True)
        await hass.async_block_till_done()
    assert not hass.config_entries.async_entries(DOMAIN)
    assert coordinator._shutdown_requested
    assert dr.async_get(hass).async_get_device(identifiers={(DOMAIN, TEST_SYMBOL)}) is None


async def test_failed_yaml_reload_retains_running_entry(
    hass: HomeAssistant, enable_custom_integrations: None, yahoo_responses: None
) -> None:
    """Keep the active configuration when the replacement YAML is invalid."""
    assert await async_setup_component(hass, DOMAIN, {DOMAIN: {CONF_SYMBOLS: [TEST_SYMBOL]}})
    await hass.async_block_till_done()
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    coordinator = next(iter(hass.data[DOMAIN][HASS_DATA_COORDINATORS].values()))
    with patch(
        "homeassistant.config.async_hass_config_yaml",
        AsyncMock(return_value={DOMAIN: {CONF_SYMBOLS: [TEST_SYMBOL], "scan_interval": "invalid"}}),
    ):
        await hass.services.async_call(DOMAIN, "reload", {}, blocking=True)
        await hass.async_block_till_done()
    assert hass.config_entries.async_entries(DOMAIN)[0] is entry
    assert next(iter(hass.data[DOMAIN][HASS_DATA_COORDINATORS].values())) is coordinator
    assert not coordinator._shutdown_requested
    assert await hass.config_entries.async_unload(entry.entry_id)
