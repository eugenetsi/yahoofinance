"""Import existing Yahoo Finance YAML settings into a config entry."""

from typing import Any

from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult

from custom_components.yahoofinance.const import DOMAIN


class YahooFinanceConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """A single YAML-managed integration instance."""

    VERSION = 1

    async def async_step_import(self, import_data: dict[str, Any]) -> ConfigFlowResult:
        """Create a config entry from validated YAML settings.

        Args:
            import_data: Validated settings with serializable scan intervals.

        Returns:
            A creation result for the imported integration entry.
        """
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title="Yahoo Finance", data=import_data)

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Direct manual setup requests to configuration.yaml.

        Args:
            user_input: Unused because ticker settings remain in YAML.

        Returns:
            An abort result with instructions to configure YAML.
        """
        return self.async_abort(reason="yaml_only")
