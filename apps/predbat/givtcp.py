# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init


"""GivTCP component - publishes GivEnergy REST controls as HA entities.

Polls GivTCP's REST API in the background (one GivTCPRest client per configured
inverter) and publishes its status as plain HA entities - the same shape every
other vendor already uses. Predbat's Inverter class never talks REST directly;
automatic_config() points its normal entity-based apps.yaml args at the
entities this component creates.

Reuses givtcp_rest.py's GivTCPRest unchanged as the actual REST client -
this component's own job is purely the publish/subscribe/auto-config glue
(dashboard_item calls, select/number/switch event handlers, automatic_config),
matching the pattern already used by fox.py, ohme.py, solax.py etc.

This now covers the whole surface Inverter used to read or write over REST:
the live status/control entities, inverter_mode and pause_mode, the #4517
discharge-target model check, and the one-time battery/capacity discovery
(capacity, nominal capacity, temperature, inverter clock, max rates and
calibration state) that Inverter.__init__ used to read off the REST blob.

GivTCPRest itself is still the REST client underneath - it is this component
that owns it now rather than Inverter.
"""

import asyncio

from component_base import ComponentBase
from utils import dp4
from givtcp_rest import GivTCPRest, InverterRestState

# Every minute of the day, matching GivTCP's own slot resolution. This has to be the full 1440
# entries rather than a coarser step: adjust_charge_window()/adjust_force_export() write whatever
# minute the plan lands on, shifted again by inverter_clock_skew_*, so any coarser option list
# would simply not contain the value the entity is actually holding.
GIVTCP_TIME_OPTIONS = ["{:02d}:{:02d}:00".format(m // 60, m % 60) for m in range(0, 24 * 60)]

# GivTCP's own pause-mode vocabulary, as written to /setBatteryPauseMode and read back from
# Control.Battery_pause_mode. Deliberately not the GE Cloud spelling ("Pause Charge", "Not Paused",
# ...) - Inverter.adjust_pause_mode picks between the two by looking at the value it reads back, so
# publishing the native spelling is what keeps it on the GivTCP side of that branch.
GIVTCP_PAUSE_MODES = ["Disabled", "PauseCharge", "PauseDischarge", "PauseBoth"]

# GivTCP raw.invertor.model values confirmed not to support the Discharge_Target_SOC_1 register
# (#4517). "Ac" (AC Coupled) and "Hybrid_gen1" are confirmed live - the latter on two separate
# reporter inverters, still repeating the write every cycle post-fix until added here.
# "Hybrid_gen2" is inferred from the same GivEnergy firmware-archive generational split
# (github.com/DJBenson/giv-firmware), not independently confirmed on real Gen2 hardware yet.
#
# These inverters accept the write and report success, but it never persists, so the caller sees a
# permanent mismatch and rewrites every cycle. Not publishing the entity at all is what stops that:
# Inverter.adjust_force_export already leaves a target it cannot read alone.
DISCHARGE_TARGET_UNSUPPORTED_MODELS = ("Ac", "Hybrid_gen1", "Hybrid_gen2")

# Control.Mode values. Predbat only ever writes Eco or Timed Export, but the inverter reports the
# others, and an option list that omitted them could not represent the mode the inverter is in.
GIVTCP_INVERTER_MODES = ["Eco", "Eco (Paused)", "Timed Export", "Timed Charge", "Timed Demand"]

# Poll GivTCP's REST API this often (seconds). Matches the cadence other components use for a
# background refresh (e.g. Ohme's device/session poll).
GIVTCP_POLL_SECONDS = 60

# How often to re-probe endpoints that have never answered, so an inverter that was unreachable at
# startup (GivTCP still booting, a network blip) or one added since is picked up without a Predbat
# restart. Only endpoints not already being managed are probed, and only ever to add them.
GIVTCP_REDISCOVER_SECONDS = 3600

# control name -> (domain, GivTCPRest write method name, HA entity attributes)
GIVTCP_CONTROLS = {
    "charge_rate": ("number", "set_charge_rate", {"unit_of_measurement": "W", "device_class": "power", "icon": "mdi:battery-charging", "min": 0, "max": 20000, "step": 100}),
    "discharge_rate": ("number", "set_discharge_rate", {"unit_of_measurement": "W", "device_class": "power", "icon": "mdi:battery-arrow-down", "min": 0, "max": 20000, "step": 100}),
    "charge_limit": ("number", "set_charge_target", {"unit_of_measurement": "%", "device_class": "battery", "icon": "mdi:battery-charging-100", "min": 4, "max": 100, "step": 1}),
    "reserve": ("number", "set_reserve", {"unit_of_measurement": "%", "device_class": "battery", "icon": "mdi:battery-low", "min": 4, "max": 100, "step": 1}),
    "discharge_target_soc": ("number", "set_discharge_target", {"unit_of_measurement": "%", "device_class": "battery", "icon": "mdi:battery-arrow-down", "min": 4, "max": 100, "step": 1}),
    "charge_limit_enable": ("switch", "enable_charge_target", {"icon": "mdi:battery-charging-100"}),
    "scheduled_charge_enable": ("switch", "enable_charge_schedule", {"icon": "mdi:battery-charging"}),
    "scheduled_discharge_enable": ("switch", "enable_discharge_schedule", {"icon": "mdi:battery-arrow-down"}),
    "charge_start_time": ("select", "_set_charge_slot", {"icon": "mdi:clock-start", "options": GIVTCP_TIME_OPTIONS}),
    "charge_end_time": ("select", "_set_charge_slot", {"icon": "mdi:clock-end", "options": GIVTCP_TIME_OPTIONS}),
    "discharge_start_time": ("select", "_set_discharge_slot", {"icon": "mdi:clock-start", "options": GIVTCP_TIME_OPTIONS}),
    "discharge_end_time": ("select", "_set_discharge_slot", {"icon": "mdi:clock-end", "options": GIVTCP_TIME_OPTIONS}),
    "inverter_mode": ("select", "set_battery_mode", {"icon": "mdi:home-battery", "options": GIVTCP_INVERTER_MODES}),
    "pause_mode": ("select", "set_battery_pause_mode", {"icon": "mdi:pause-octagon", "options": GIVTCP_PAUSE_MODES}),
    "pause_start_time": ("select", "_set_pause_slot", {"icon": "mdi:clock-start", "options": GIVTCP_TIME_OPTIONS}),
    "pause_end_time": ("select", "_set_pause_slot", {"icon": "mdi:clock-end", "options": GIVTCP_TIME_OPTIONS}),
}

# sensor name -> HA entity attributes
GIVTCP_SENSORS = {
    "soc_kw": {"unit_of_measurement": "kWh", "device_class": "energy", "icon": "mdi:battery"},
    "soc_percent": {"unit_of_measurement": "%", "device_class": "battery", "icon": "mdi:battery"},
    "battery_power": {"unit_of_measurement": "W", "device_class": "power", "icon": "mdi:battery-charging"},
    "pv_power": {"unit_of_measurement": "W", "device_class": "power", "icon": "mdi:solar-power"},
    "grid_power": {"unit_of_measurement": "W", "device_class": "power", "icon": "mdi:transmission-tower"},
    "load_power": {"unit_of_measurement": "W", "device_class": "power", "icon": "mdi:home-lightning-bolt"},
    "battery_voltage": {"unit_of_measurement": "V", "device_class": "voltage", "icon": "mdi:sine-wave"},
    "soc_max": {"unit_of_measurement": "kWh", "device_class": "energy", "icon": "mdi:battery-high"},
    "battery_temperature": {"unit_of_measurement": "°C", "device_class": "temperature", "icon": "mdi:thermometer"},
    "inverter_time": {"icon": "mdi:clock-outline"},
    "battery_rate_max": {"unit_of_measurement": "W", "device_class": "power", "icon": "mdi:battery-arrow-up"},
    "inverter_limit": {"unit_of_measurement": "W", "device_class": "power", "icon": "mdi:transmission-tower"},
    "battery_calibration": {"icon": "mdi:battery-sync"},
    "battery_soh": {"icon": "mdi:battery-heart-variant"},
    "battery_dod": {"icon": "mdi:battery-arrow-down-outline"},
    "battery_dod_soh": {"icon": "mdi:battery-heart-outline"},
}

# Discovery values Inverter.__init__ used to read straight off the REST blob. Published as sensors
# and claimed below so that path becomes an ordinary entity read like every other inverter type.
GIVTCP_AUTO_CONFIG_DISCOVERY_KEYS = [
    "soc_max",
    "battery_temperature",
    "inverter_time",
    "inverter_limit",
    "battery_calibration",
]

# apps.yaml keys automatic_config() points at the published entities - keys not listed here
# (soc_max, battery_power_invert, ...) are left for the user/other discovery to configure.
#
# soc_kw (not soc_percent) is deliberate: Inverter.update_status() prefers soc_percent when both
# are set, but GivTCP only reports whole-percent SOC, which on a ~9.5kWh battery quantises SoC to
# ~0.1kWh steps. SOC_kWh is reported to 3 decimal places, so pointing soc_kw at it keeps the
# precision the old direct-REST path had. The soc_percent sensor is still published for display.
GIVTCP_AUTO_CONFIG_KEYS = [
    "charge_rate",
    "discharge_rate",
    "charge_limit",
    "reserve",
    "discharge_target_soc",
    "scheduled_charge_enable",
    "scheduled_discharge_enable",
    "charge_start_time",
    "charge_end_time",
    "discharge_start_time",
    "discharge_end_time",
    "inverter_mode",
    "soc_kw",
]

# Pause control is GivTCP v3 only - v2 has no /setBatteryPauseMode endpoint, and
# Inverter.adjust_pause_mode's REST path was gated on rest_v3 for the same reason. Auto-configured
# only when every discovered inverter reports v3, so a mixed fleet leaves these to the user rather
# than pointing half of them at entities that can never be written. Endpoints that never answered
# are excluded - they default to rest_v3 False and would otherwise veto a wholly-v3 live fleet.
GIVTCP_AUTO_CONFIG_PAUSE_KEYS = [
    "pause_mode",
    "pause_start_time",
    "pause_end_time",
]

# The Enable_Charge_Target register (reg 20) gates whether the inverter acts on Target_SOC at all,
# so Predbat has to set it alongside the limit or the battery charges to 100% regardless (#4141).
# Claimed only when every discovered inverter actually reports the register - enable_charge_target()
# verifies its write by reading that field back, so publishing a control for a register GivTCP never
# reports would burn the retry ladder on every charge limit change, and would displace the user's
# own apps.yaml charge_limit_enable that could have done the job.
GIVTCP_AUTO_CONFIG_CHARGE_ENABLE_KEYS = [
    "charge_limit_enable",
]

# battery_scaling points at the combined depth-of-discharge x state-of-health sensor, the same shape
# GE Cloud uses (battery_dod_soh). Inverter computes soc_max = nominal_capacity * battery_scaling, so
# with soc_max carrying the design capacity this yields the true usable size while leaving
# nominal_capacity as the nameplate figure that battery_scaling_auto measures degradation against.
# Claimed only when a design capacity was actually reported - see publish_data().
GIVTCP_AUTO_CONFIG_SCALING_KEYS = [
    "battery_scaling",
]

# Power/voltage keys are auto-configured separately: givtcp_rest_power_ignore opts out of them.
GIVTCP_AUTO_CONFIG_POWER_KEYS = [
    "battery_power",
    "pv_power",
    "grid_power",
    "load_power",
    "battery_voltage",
]


class GivTCPComponent(ComponentBase):
    """
    Publishes one or more GivTCP-connected inverters as HA entities.

    One instance handles every URL in the (scalar-or-list, per-inverter-indexed) `givtcp_rest`
    apps.yaml arg - the same key and shape Inverter itself used to read directly, so an existing
    user's apps.yaml needs no changes to pick this component up.
    """

    def initialize(self, rest_urls):
        rest_urls = rest_urls if isinstance(rest_urls, list) else [rest_urls]
        self.rest = []
        for n, url in enumerate(rest_urls):
            state = InverterRestState(id=n, rest_api=url)
            self.rest.append(GivTCPRest(self.base, state))
        self.automatic_config_done = False
        # givtcp_rest is the key that enables this component, but its length says nothing about how
        # many inverters exist - the shipped apps.yaml pairs num_inverters: 1 with a two-entry list,
        # the same over-provisioning every other per-inverter key in that template uses. discovered
        # holds the indices that actually answered, and is what drives automatic_config().
        self.discovered = []
        self.discovery_done = False
        # The discovered set automatic_config() was last run against, so a fleet that grows on a
        # later re-probe reconfigures rather than staying at its startup size.
        self.configured_for = []
        # Whether the most recent poll read every inverter being managed. run() withholds the
        # success timestamp while this is False, which is what eventually puts the component into
        # error - a failed read otherwise leaves stale entities republishing as though nothing
        # were wrong.
        self.poll_healthy = False
        # publish_data() runs every poll; the unsupported-model notice is per inverter and only
        # worth saying once rather than every 60 seconds for the life of the process
        self.discharge_target_warned = {}

    async def _run_blocking(self, func, *args):
        """Run one of GivTCPRest's blocking (requests + time.sleep) calls off the event loop."""
        return await asyncio.get_event_loop().run_in_executor(None, func, *args)

    async def run(self, seconds, first):
        # Captured before the poll block so the pass that performs initial discovery does not also
        # immediately re-probe. Only worth doing while something is still unaccounted for.
        rediscover = self.discovery_done and len(self.discovered) < len(self.rest) and (seconds % GIVTCP_REDISCOVER_SECONDS) == 0

        if first or (seconds % GIVTCP_POLL_SECONDS) == 0:
            # Probe every configured URL until discovery settles, then only the ones that answered:
            # read_data() retries with 20s then 40s sleeps, so re-probing a placeholder URL every
            # cycle would spend longer failing than the poll interval it runs on.
            poll = range(len(self.rest)) if not self.discovery_done else self.discovered
            answered = []
            for n in poll:
                rest = self.rest[n]
                data = await self._run_blocking(rest.read_data)
                if data:
                    rest.inverter.rest_data = data
                    # Mirrors Inverter.__init__'s own version detection - power_readings() only
                    # trusts GivTCP's own Battery_Voltage field once this is set.
                    version = data.get("Stats", {}).get("GivTCP_Version", "Unknown")
                    rest.inverter.rest_v3 = version.startswith("3")
                    answered.append(n)

            # Settle discovery on the first pass that finds anything. A pass where nothing answers
            # leaves it open so the next cycle re-probes every URL, rather than locking in an empty
            # fleet because GivTCP happened to be starting up.
            if not self.discovery_done and answered:
                self.discovered = answered
                self.discovery_done = True
                self.log("GivTCP: discovered {} inverter(s) from {} configured REST endpoint(s)".format(len(answered), len(self.rest)))
                missing = [self.rest[n].inverter.rest_api for n in range(len(self.rest)) if n not in answered]
                if missing:
                    self.log("Warn: GivTCP: no inverter answered at {} - those endpoints will not be managed. Restart Predbat once they are reachable if that is wrong.".format(", ".join(missing)))

            # An endpoint that has never been adopted is not a failure - the shipped apps.yaml
            # over-provisions givtcp_rest, so a placeholder entry is the normal case. Only the
            # inverters actually being managed count against the component's health.
            failed = [n for n in self.discovered if n not in answered]
            for n in failed:
                self.log("Warn: GivTCP: inverter {} at {} did not respond - its entities are now stale, and the component will go into error if this continues".format(n, self.rest[n].inverter.rest_api))
            self.poll_healthy = not failed

            await self.publish_data()

        # Report failure while nothing has ever been read, so ComponentBase.start() keeps retrying
        # with backoff instead of reporting a healthy component. Crucially this also holds back
        # automatic_config(): pointing Predbat's apps.yaml keys at entities that were never
        # published would override the user's own working config with unavailable entities, and
        # it only ever runs once.
        if not self.discovered:
            self.log("Warn: GivTCP: no data read from any configured REST endpoint yet")
            return False

        if rediscover:
            await self.rediscover()

        # Re-runs when the fleet has grown. Deliberately not when it shrinks: self.discovered is
        # append-only, because dropping an inverter that stopped answering would rebuild Predbat's
        # inverter list for a smaller fleet and leave a real battery uncontrolled at whatever
        # settings it last had. Leaving it in makes Inverter.__init__ fail visibly instead.
        if self.discovered != self.configured_for:
            await self.automatic_config()
            self.configured_for = list(self.discovered)
            self.automatic_config_done = True

        # Deliberately still True: a failed poll should not tear the component down and restart it
        # over a transient blip. Withholding the timestamp lets ComponentManager's staleness check
        # mark it not-alive once the failure persists.
        if self.poll_healthy:
            self.update_success_timestamp()
        return True

    async def rediscover(self):
        """Re-probe endpoints that have never answered, adopting any inverter that has appeared."""
        for n in range(len(self.rest)):
            if n in self.discovered:
                continue
            rest = self.rest[n]
            # A single cheap GET, unlike startup discovery: read_data()'s 20s/40s retry ladder is
            # there for a GivTCP that might merely be booting, and spending ~100s on a placeholder
            # URL that will never answer would outlast the poll interval this runs on.
            data = await self._run_blocking(rest.read_data, "readData", False)
            if not data:
                continue
            rest.inverter.rest_data = data
            version = data.get("Stats", {}).get("GivTCP_Version", "Unknown")
            rest.inverter.rest_v3 = version.startswith("3")
            # Appended, never inserted. The order of self.discovered is Predbat's inverter
            # numbering, so moving an existing entry would repoint a running inverter at different
            # physical hardware mid-flight.
            self.discovered.append(n)
            self.log("GivTCP: inverter at {} answered on re-probe - now managing {} inverter(s)".format(rest.inverter.rest_api, len(self.discovered)))

    def _entity_id(self, domain, n, control):
        return "{}.{}_givtcp_{}_{}".format(domain, self.prefix, n, control)

    async def publish_data(self):
        """Publish current status as HA entities for every configured inverter."""
        for n, rest in enumerate(self.rest):
            if not rest.inverter.rest_data:
                continue

            # The rate entities carry the inverter's real maximum rate as their "max" attribute, not
            # the generic ceiling in GIVTCP_CONTROLS. Inverter.__init__ derives battery_rate_max_raw
            # for a GE inverter from exactly this attribute, so publishing the generic value would
            # tell it the battery can take 20kW.
            max_battery_rate = rest.max_battery_rate()
            charge_rate_attributes = dict(GIVTCP_CONTROLS["charge_rate"][2])
            discharge_rate_attributes = dict(GIVTCP_CONTROLS["discharge_rate"][2])
            if max_battery_rate:
                charge_rate_attributes["max"] = max_battery_rate
                discharge_rate_attributes["max"] = max_battery_rate
            else:
                # Nothing reported: publish no max at all rather than the generic ceiling. Inverter
                # reads this attribute as ground truth, and an absent one falls back to its own
                # 2600W default (ha.py get_state returns the caller's default for a missing
                # attribute) - which is precisely what main did when REST reported no rate.
                # Leaving 20000 in place claims a 20kW battery instead.
                charge_rate_attributes.pop("max", None)
                discharge_rate_attributes.pop("max", None)

            self.dashboard_item(self._entity_id("number", n, "charge_rate"), state=rest.inverter.rest_data.get("Control", {}).get("Battery_Charge_Rate", 0), attributes=charge_rate_attributes, app="givtcp")
            self.dashboard_item(self._entity_id("number", n, "discharge_rate"), state=rest.inverter.rest_data.get("Control", {}).get("Battery_Discharge_Rate", 0), attributes=discharge_rate_attributes, app="givtcp")
            target_soc = rest.target_soc
            if target_soc is not None:
                self.dashboard_item(self._entity_id("number", n, "charge_limit"), state=target_soc, attributes=GIVTCP_CONTROLS["charge_limit"][2], app="givtcp")
            self.dashboard_item(self._entity_id("number", n, "reserve"), state=rest.inverter.rest_data.get("Control", {}).get("Battery_Power_Reserve", 0), attributes=GIVTCP_CONTROLS["reserve"][2], app="givtcp")
            # An unsupported model gets no entity at all - see DISCHARGE_TARGET_UNSUPPORTED_MODELS.
            # Publishing one would restart the every-cycle rewrite loop of #4517, because the write
            # reports success and then silently fails to persist.
            inverter_model = rest.inverter.rest_data.get("raw", {}).get("invertor", {}).get("model", "")
            if inverter_model in DISCHARGE_TARGET_UNSUPPORTED_MODELS:
                if not self.discharge_target_warned.get(n):
                    self.log("Info: GivTCP: inverter {} is {}, which has no working discharge target register - export target will not be written".format(n, inverter_model))
                    self.discharge_target_warned[n] = True
            else:
                discharge_target = rest.read_discharge_target()
                if discharge_target is not None:
                    self.dashboard_item(self._entity_id("number", n, "discharge_target_soc"), state=discharge_target, attributes=GIVTCP_CONTROLS["discharge_target_soc"][2], app="givtcp")

            # Only when GivTCP reports it: see GivTCPRest.charge_target_enabled
            charge_target_enabled = rest.charge_target_enabled
            if charge_target_enabled is not None:
                self.dashboard_item(self._entity_id("switch", n, "charge_limit_enable"), state="on" if charge_target_enabled else "off", attributes=GIVTCP_CONTROLS["charge_limit_enable"][2], app="givtcp")
            self.dashboard_item(self._entity_id("switch", n, "scheduled_charge_enable"), state="on" if rest.charge_enable_time else "off", attributes=GIVTCP_CONTROLS["scheduled_charge_enable"][2], app="givtcp")
            self.dashboard_item(self._entity_id("switch", n, "scheduled_discharge_enable"), state="on" if rest.discharge_enable_time else "off", attributes=GIVTCP_CONTROLS["scheduled_discharge_enable"][2], app="givtcp")

            control = rest.inverter.rest_data.get("Control", {})
            self.dashboard_item(self._entity_id("select", n, "inverter_mode"), state=control.get("Mode", "Eco"), attributes=GIVTCP_CONTROLS["inverter_mode"][2], app="givtcp")

            # v3 only - see GIVTCP_AUTO_CONFIG_PAUSE_KEYS
            if rest.inverter.rest_v3:
                self.dashboard_item(self._entity_id("select", n, "pause_mode"), state=control.get("Battery_pause_mode", "Disabled"), attributes=GIVTCP_CONTROLS["pause_mode"][2], app="givtcp")

            timeslots = rest.inverter.rest_data.get("Timeslots", {})
            if rest.inverter.rest_v3:
                self.dashboard_item(self._entity_id("select", n, "pause_start_time"), state=timeslots.get("Battery_pause_start_time_slot", "00:00:00"), attributes=GIVTCP_CONTROLS["pause_start_time"][2], app="givtcp")
                self.dashboard_item(self._entity_id("select", n, "pause_end_time"), state=timeslots.get("Battery_pause_end_time_slot", "00:00:00"), attributes=GIVTCP_CONTROLS["pause_end_time"][2], app="givtcp")

            self.dashboard_item(self._entity_id("select", n, "charge_start_time"), state=timeslots.get("Charge_start_time_slot_1", "00:00:00"), attributes=GIVTCP_CONTROLS["charge_start_time"][2], app="givtcp")
            self.dashboard_item(self._entity_id("select", n, "charge_end_time"), state=timeslots.get("Charge_end_time_slot_1", "00:00:00"), attributes=GIVTCP_CONTROLS["charge_end_time"][2], app="givtcp")
            self.dashboard_item(self._entity_id("select", n, "discharge_start_time"), state=timeslots.get("Discharge_start_time_slot_1", "00:00:00"), attributes=GIVTCP_CONTROLS["discharge_start_time"][2], app="givtcp")
            self.dashboard_item(self._entity_id("select", n, "discharge_end_time"), state=timeslots.get("Discharge_end_time_slot_1", "00:00:00"), attributes=GIVTCP_CONTROLS["discharge_end_time"][2], app="givtcp")

            soc_kwh = rest.soc_kwh
            if soc_kwh is not None:
                self.dashboard_item(self._entity_id("sensor", n, "soc_kw"), state=soc_kwh, attributes=GIVTCP_SENSORS["soc_kw"], app="givtcp")
            soc_percent = rest.inverter.rest_data.get("Power", {}).get("Power", {}).get("SOC", None)
            if soc_percent is not None:
                self.dashboard_item(self._entity_id("sensor", n, "soc_percent"), state=soc_percent, attributes=GIVTCP_SENSORS["soc_percent"], app="givtcp")

            # Discovery values - see GIVTCP_AUTO_CONFIG_DISCOVERY_KEYS. Each is only published when
            # GivTCP actually reports it, so a missing one falls back to the user's own apps.yaml
            # value rather than being published as a zero that would look authoritative.
            #
            # soc_max carries the design capacity - Battery_Capacity_kWh already is that, the same
            # figure raw.invertor.battery_nominal_capacity gives in Ah - and battery health is
            # expressed separately through battery_scaling, matching GE Cloud (battery_size +
            # battery_dod_soh). Inverter computes soc_max = nominal_capacity * battery_scaling and
            # measures degradation against nominal_capacity, so the design figure has to stay here
            # and the derate has to live in the scaling.
            design_capacity = rest.battery_capacity_kwh() or rest.nominal_capacity()

            # Real state of health, from the per-module Battery_Capacity vs Battery_Design_Capacity
            # the BMS reports. Predbat has never used these for GivTCP: battery_scaling stayed at
            # the user's manual value and degradation was left for battery_scaling_auto to infer
            # from history.
            soh = rest.battery_soh()

            if soh is not None and self.get_arg("battery_capacity_nominal", default=False):
                # main's expert switch meant "size the battery from the nameplate". Suppressing the
                # health derate is what that means once health is a separate factor.
                self.log("Info: GivTCP: inverter {} battery_capacity_nominal is set - using the full design capacity and ignoring the reported state of health {}".format(n, soh))
                soh = 1.0

            soc_max = design_capacity
            if soc_max:
                self.dashboard_item(self._entity_id("sensor", n, "soc_max"), state=soc_max, attributes=GIVTCP_SENSORS["soc_max"], app="givtcp")

            if soh is not None:
                # GivTCP reports no depth of discharge anywhere in a full /readData dump, so it
                # defaults to 1.0 and can be supplied per inverter with givtcp_battery_dod. Inverter
                # applies one scaling factor, so the combined product is what battery_scaling points at.
                dod = float(self.get_arg("givtcp_battery_dod", default=1.0, index=n))
                self.dashboard_item(self._entity_id("sensor", n, "battery_soh"), state=soh, attributes=GIVTCP_SENSORS["battery_soh"], app="givtcp")
                self.dashboard_item(self._entity_id("sensor", n, "battery_dod"), state=dod, attributes=GIVTCP_SENSORS["battery_dod"], app="givtcp")
                self.dashboard_item(self._entity_id("sensor", n, "battery_dod_soh"), state=dp4(soh * dod), attributes=GIVTCP_SENSORS["battery_dod_soh"], app="givtcp")

            battery_temperature = rest.battery_temperature()
            if battery_temperature is not None:
                self.dashboard_item(self._entity_id("sensor", n, "battery_temperature"), state=battery_temperature, attributes=GIVTCP_SENSORS["battery_temperature"], app="givtcp")

            inverter_time = rest.inverter_time()
            if inverter_time:
                self.dashboard_item(self._entity_id("sensor", n, "inverter_time"), state=inverter_time, attributes=GIVTCP_SENSORS["inverter_time"], app="givtcp")

            if max_battery_rate:
                self.dashboard_item(self._entity_id("sensor", n, "battery_rate_max"), state=max_battery_rate, attributes=GIVTCP_SENSORS["battery_rate_max"], app="givtcp")

            max_inverter_rate = rest.max_inverter_rate()
            if max_inverter_rate:
                self.dashboard_item(self._entity_id("sensor", n, "inverter_limit"), state=max_inverter_rate, attributes=GIVTCP_SENSORS["inverter_limit"], app="givtcp")

            # Always published, unlike the values above: "not calibrating" is a real answer that
            # Predbat needs, and an absent entity would be indistinguishable from one
            self.dashboard_item(self._entity_id("sensor", n, "battery_calibration"), state="on" if rest.in_calibration() else "off", attributes=GIVTCP_SENSORS["battery_calibration"], app="givtcp")

            power = rest.power_readings()
            if power:
                self.dashboard_item(self._entity_id("sensor", n, "battery_power"), state=power["battery_power"], attributes=GIVTCP_SENSORS["battery_power"], app="givtcp")
                self.dashboard_item(self._entity_id("sensor", n, "pv_power"), state=power["pv_power"], attributes=GIVTCP_SENSORS["pv_power"], app="givtcp")
                self.dashboard_item(self._entity_id("sensor", n, "grid_power"), state=power["grid_power"], attributes=GIVTCP_SENSORS["grid_power"], app="givtcp")
                self.dashboard_item(self._entity_id("sensor", n, "load_power"), state=power["load_power"], attributes=GIVTCP_SENSORS["load_power"], app="givtcp")
                self.dashboard_item(self._entity_id("sensor", n, "battery_voltage"), state=power["battery_voltage"], attributes=GIVTCP_SENSORS["battery_voltage"], app="givtcp")

    async def automatic_config(self):
        """Point Predbat's standard entity-based apps.yaml keys at the entities this component publishes."""
        # Driven by the endpoints that answered discovery, not by the length of the configured
        # givtcp_rest list - counting the list would have Predbat build an Inverter against a URL
        # with nothing behind it and then plan and execute against a phantom battery.
        discovered = self.discovered
        if not discovered:
            self.log("Warn: GivTCP automatic_config: no inverters discovered, skipping configuration")
            return

        n_inverters = len(discovered)
        self.log("GivTCP: configuring Predbat for {} discovered inverter(s)".format(n_inverters))
        self.set_arg_auto("inverter_type", ["GE" for _ in range(n_inverters)])
        self.set_arg_auto("num_inverters", n_inverters)

        keys = list(GIVTCP_AUTO_CONFIG_KEYS)

        # givtcp_rest_power_ignore is the documented opt-out for users whose GivTCP power readings
        # are wrong for their setup - typically multi-inverter systems using their own combined
        # power sensors (see docs/apps-yaml.md). Claiming those keys here would silently override
        # exactly the config they set it to protect. It is a per-inverter arg, but these keys are
        # written as one whole list, so any inverter opting out leaves all of them to the user.
        if any(self.get_arg("givtcp_rest_power_ignore", default=False, index=n) for n in discovered):
            self.log("Info: GivTCP: givtcp_rest_power_ignore is set - leaving power/voltage entities to your apps.yaml config")
        else:
            keys += GIVTCP_AUTO_CONFIG_POWER_KEYS

        keys += GIVTCP_AUTO_CONFIG_DISCOVERY_KEYS

        if all(self.rest[n].battery_soh() is not None for n in discovered):
            keys += GIVTCP_AUTO_CONFIG_SCALING_KEYS
        else:
            self.log("Info: GivTCP: no design capacity reported by every inverter - leaving battery_scaling to your apps.yaml config")

        if all(self.rest[n].charge_target_enabled is not None for n in discovered):
            keys += GIVTCP_AUTO_CONFIG_CHARGE_ENABLE_KEYS
        else:
            self.log("Info: GivTCP: Enable_Charge_Target is not reported by every inverter - leaving charge_limit_enable to your apps.yaml config")

        if all(self.rest[n].inverter.rest_v3 for n in discovered):
            keys += GIVTCP_AUTO_CONFIG_PAUSE_KEYS
        else:
            self.log("Info: GivTCP: pause control needs GivTCP v3 on every inverter - leaving pause_mode/pause_start_time/pause_end_time to your apps.yaml config")

        for key in keys:
            domain, _, _ = GIVTCP_CONTROLS.get(key, (None, None, None))
            domain = domain or "sensor"
            if key == "battery_scaling":
                self.set_arg_auto(key, [self._entity_id("sensor", n, "battery_dod_soh") for n in discovered])
                continue
            # Indexed by REST endpoint, not by position: _parse_entity feeds self.rest[n] on every
            # write, so renumbering would route the surviving inverter's writes at a dead client.
            self.set_arg_auto(key, [self._entity_id(domain, n, key) for n in discovered])

    def _parse_entity(self, entity_id):
        """entity_id -> (inverter index, control name), or (None, None) if it doesn't match."""
        prefix = "givtcp_"
        idx = entity_id.find(prefix)
        if idx < 0:
            return None, None
        rest = entity_id[idx + len(prefix) :]
        parts = rest.split("_", 1)
        if len(parts) != 2:
            return None, None
        try:
            n = int(parts[0])
        except ValueError:
            return None, None
        return n, parts[1]

    async def _set_charge_slot(self, entity_id, value):
        n, control = self._parse_entity(entity_id)
        rest = self.rest[n]
        timeslots = rest.inverter.rest_data.get("Timeslots", {}) if rest.inverter.rest_data else {}
        start = value if control == "charge_start_time" else timeslots.get("Charge_start_time_slot_1", "00:00:00")
        end = value if control == "charge_end_time" else timeslots.get("Charge_end_time_slot_1", "00:00:00")
        await self._run_blocking(rest.set_charge_slot1, start, end)

    async def _set_discharge_slot(self, entity_id, value):
        n, control = self._parse_entity(entity_id)
        rest = self.rest[n]
        timeslots = rest.inverter.rest_data.get("Timeslots", {}) if rest.inverter.rest_data else {}
        start = value if control == "discharge_start_time" else timeslots.get("Discharge_start_time_slot_1", "00:00:00")
        end = value if control == "discharge_end_time" else timeslots.get("Discharge_end_time_slot_1", "00:00:00")
        await self._run_blocking(rest.set_discharge_slot1, start, end)

    async def _set_pause_slot(self, entity_id, value):
        n, control = self._parse_entity(entity_id)
        rest = self.rest[n]
        timeslots = rest.inverter.rest_data.get("Timeslots", {}) if rest.inverter.rest_data else {}
        start = value if control == "pause_start_time" else timeslots.get("Battery_pause_start_time_slot", "00:00:00")
        end = value if control == "pause_end_time" else timeslots.get("Battery_pause_end_time_slot", "00:00:00")
        await self._run_blocking(rest.set_pause_slot, start, end)

    async def _handle_write(self, entity_id, value, is_switch=False, is_number=False):
        """
        Apply one entity write to the inverter, then immediately republish so the entity reflects it.

        This must complete inline, before returning to the caller - it cannot be deferred to the
        next run() tick. Inverter.write_and_poll_value/option() call the HA service and then poll
        the entity back (every inv_write_and_poll_sleep, up to INVERTER_MAX_RETRY times) to decide
        whether the write landed, and the service call itself does not update the entity - only the
        publish_data() below does. Deferring the write to run() (a 60s cadence) would mean every
        rate/window/reserve write is judged failed long before it is even attempted. This is why
        fox.py handles its write events inline too.
        """
        try:
            n, control = self._parse_entity(entity_id)
            if n is None or n >= len(self.rest) or control not in GIVTCP_CONTROLS:
                self.log("Warn: GivTCP: write event for unknown entity {}".format(entity_id))
                return
            rest = self.rest[n]
            _, method_name, _ = GIVTCP_CONTROLS[control]

            if method_name in ("_set_charge_slot", "_set_discharge_slot", "_set_pause_slot"):
                await getattr(self, method_name)(entity_id, value)
            elif is_switch:
                await self._run_blocking(getattr(rest, method_name), value == "on")
            elif is_number:
                await self._run_blocking(getattr(rest, method_name), value)
            else:
                # Plain select (inverter_mode, pause_mode) - the chosen option is the value the
                # write method takes, unlike the slot selects which need both ends of the window
                await self._run_blocking(getattr(rest, method_name), value)
            await self.publish_data()
        except Exception as e:
            # A failed write must not propagate into the shared HA event dispatch, which would stop
            # other components seeing this event. Inverter's own write-and-poll retry notices the
            # entity never changed and reports the failure through its usual path.
            self.log("Warn: GivTCP: write event error for {}: {}".format(entity_id, e))
            self.non_fatal_error_occurred()

    async def select_event(self, entity_id, value):
        await self._handle_write(entity_id, value)

    async def number_event(self, entity_id, value):
        await self._handle_write(entity_id, value, is_number=True)

    async def switch_event(self, entity_id, service):
        value = "on" if service == "turn_on" else "off" if service == "turn_off" else None
        if value is None:
            return
        await self._handle_write(entity_id, value, is_switch=True)
