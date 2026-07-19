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

    def initialize(self, app_id="", app_secret="", username="", password="", data_center="eu", company_id="", auth_method="app_credentials", token_expires_at=None, token_hash="", inverter_sn=None, automatic=False, automatic_ignore_pv=False, **kwargs):
        """Initialise the DEYE component from its resolved config args.

        ComponentBase.__init__ calls initialize(**kwargs); the Components
        registry has already resolved each arg from its deye_* config key and
        passes it BY ARG NAME (e.g. data_center <- deye_data_center), exactly
        like fox/enphase/solax/teslemetry. Consume the kwargs directly — do NOT
        re-derive with get_arg("data_center"): that bare name is not in
        apps.yaml (the key is deye_data_center), so it would always return the
        default and silently pin every setting.
        """
        self.log("Info: DeyeAPI initialising")
        self.app_id = app_id
        self.app_secret = app_secret
        self.username = username
        self.password = password
        self.data_center = data_center or "eu"
        self.company_id = company_id
        self.token_hash = token_hash
        self.automatic = automatic
        self.automatic_ignore_pv = automatic_ignore_pv
        self.inverter_sn_filter = inverter_sn if isinstance(inverter_sn, list) else ([inverter_sn] if inverter_sn else [])
        self.device_list = []
        self.device_values = {}
        self.device_battery_config = {}
        self.local_schedule = {}
        self.pending_orders = {}
        self.applied_payload = {}
        self.cached_values = {}
        self._init_oauth(
            auth_method=auth_method,
            key=app_secret or token_hash,
            token_expires_at=token_expires_at,
            provider_name="deye",
        )

    @property
    def base_url(self):
        """Return the OpenAPI base URL for the configured data centre."""
        return DEYE_BASE_URLS.get(self.data_center, DEYE_BASE_URLS["eu"])
