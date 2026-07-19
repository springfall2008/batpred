# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# DEYE Cloud API Library
# -----------------------------------------------------------------------------

"""DEYE Cloud API integration for Predbat.

Registers each DEYE battery inverter as a ``DeyeCloud`` Predbat inverter,
publishing monitoring sensors and Fox-style schedule control entities. Predbat
drives those entities through the generic Inverter class; this module derives
the DEYE work mode internally and applies a combined ``strategy_dynamic_control``
payload. Supports HA add-on (self-managed token) and Predbat.com SaaS (injected
token) auth.
"""

from component_base import ComponentBase
from oauth_mixin import OAuthMixin
from deye_const import DEYE_BASE_URLS


class DeyeAPI(ComponentBase, OAuthMixin):
    """DEYE Cloud API component."""

    def initialize(self, **kwargs):
        """Satisfy the ComponentBase abstract lifecycle hook by delegating to initialise()."""
        self.initialise()

    def initialise(self):
        """Initialise the DEYE component from configured args."""
        self.log("Info: DeyeAPI initialising")
        self.data_center = self.get_arg("data_center", "eu")
        self.company_id = self.get_arg("company_id", "")
        self.automatic = self.get_arg("automatic", False)
        self.automatic_ignore_pv = self.get_arg("automatic_ignore_pv", False)
        sn = self.get_arg("inverter_sn", [])
        self.inverter_sn_filter = sn if isinstance(sn, list) else [sn]
        self.device_list = []
        self.device_values = {}
        self.device_battery_config = {}
        self.local_schedule = {}
        self.pending_orders = {}
        self.applied_payload = {}
        self.cached_values = {}
        auth_method = self.get_arg("auth_method", "app_credentials")
        self._init_oauth(
            auth_method=auth_method,
            key=self.get_arg("app_secret", self.get_arg("token_hash", "")),
            token_expires_at=self.get_arg("token_expires_at", None),
            provider_name="deye",
        )

    @property
    def base_url(self):
        """Return the OpenAPI base URL for the configured data centre."""
        return DEYE_BASE_URLS.get(self.data_center, DEYE_BASE_URLS["eu"])
