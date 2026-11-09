# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""
A fake Teslemetry cloud with a simple Powerwall in it, for the Teslemetry hold harness.

It stands in for aiohttp.ClientSession, so it sees every request teslemetry.py makes at the HTTP boundary,
answers the reads from the simulated battery and applies the writes to it. Anything it does not model raises
AssertionError, so a request can never silently succeed or reach the real api.teslemetry.com.

The battery model is deliberately small - one Powerwall, minute steps - but each rule is one the field has
shown, and is the behaviour a control change has to be judged against:

- A backup reserve above the SoC is met from the grid, at a slow fill rate, whatever the grid-charging flag
  says (TESLA in config.py: reserve_above_soc_charges_from_grid; GH#6046 site evidence: ~1.4 kW to 100%
  with grid charging off).
- Since firmware 25.18.4 the backup reserve honours only 0-80 or exactly 100; a write in 81-99 is snapped
  (down to 80 by default) and recorded, so a test can see Predbat sent one.
- In autonomous mode (Time-Based Control) the cheapest band of the pushed tariff charges from the grid at the
  full rate when grid charging is allowed, and the dearest sell band exports down to the reserve when the
  export rule is battery_ok.
- Otherwise the battery covers the house down to the reserve and solar surplus charges it.
"""

import json
from dataclasses import dataclass

from teslemetry import TESLEMETRY_DEFAULT_URL

HARNESS_SITE_ID = 20001
SITE_PATH = "/api/1/energy_sites/{}/".format(HARNESS_SITE_ID)

# Backup reserve values a Powerwall still honours above 80 (firmware 25.18.4 and later)
RESERVE_HONOURED_MAX = 80
RESERVE_FULL = 100

# Fill rate when the reserve is above the SoC: slow, as reserve-driven charging is (GH#4892, GH#6046)
DEFAULT_RESERVE_FILL_W = 1400
# A Powerwall's battery rating
DEFAULT_RATE_W = 5000


@dataclass
class CommandRecord:
    """One write teslemetry made: when (simulated time), which endpoint, and the body."""

    sim_time: object
    endpoint: str
    body: dict


@dataclass
class TraceRow:
    """The battery's state over one simulated minute."""

    sim_time: object
    soc_percent: float
    reserve: int
    mode: str
    grid_charging: bool
    export_rule: str
    battery_w: float
    grid_w: float
    load_w: float
    pv_w: float


class FakeResponse:
    """The parts of an aiohttp response teslemetry reads."""

    def __init__(self, status, body):
        """status is the HTTP status, body the JSON-able response."""
        self.status = status
        self.body = body

    async def __aenter__(self):
        """Entered by `async with session.request(...) as resp`."""
        return self

    async def __aexit__(self, *args):
        """Nothing to release."""
        return False

    async def json(self):
        """The parsed body."""
        return self.body

    async def text(self):
        """The body as text, for error paths."""
        return json.dumps(self.body)


class FakeTeslemetry:
    """A Teslemetry account with one Powerwall site, driven minute by minute on a simulated clock."""

    def __init__(self, clock, capacity_kwh=13.5, soc_percent=50.0, reserve=20, mode="self_consumption", rate_w=DEFAULT_RATE_W, reserve_fill_w=DEFAULT_RESERVE_FILL_W, load_w=None, pv_w=None, snap_band_to=RESERVE_HONOURED_MAX):
        """clock returns the simulated time; load_w/pv_w are functions of it (constant 400 W load and no solar by default)."""
        self.clock = clock
        self.capacity_kwh = capacity_kwh
        self.soc_kwh = capacity_kwh * soc_percent / 100.0
        self.reserve = reserve
        self.mode = mode
        self.grid_charging = True
        self.export_rule = "pv_only"
        self.tariff = None
        self.rate_w = rate_w
        self.reserve_fill_w = reserve_fill_w
        self.load_w = load_w or (lambda now: 400.0)
        self.pv_w = pv_w or (lambda now: 0.0)
        self.snap_band_to = snap_band_to
        self.battery_w = 0.0
        self.grid_w = 0.0
        self.last_load_w = 0.0
        self.last_pv_w = 0.0
        self.import_kwh = 0.0
        self.export_kwh = 0.0
        self.load_kwh = 0.0
        self.pv_kwh = 0.0
        self.commands = []
        self.band_writes = []
        self.trace = []

    @property
    def soc_percent(self):
        """The state of charge in percent."""
        return self.soc_kwh / self.capacity_kwh * 100.0

    def session_factory(self):
        """A class to patch over aiohttp.ClientSession, bound to this fake."""
        fake = self

        class FakeSession:
            """aiohttp.ClientSession stand-in: each request is answered by the fake."""

            def __init__(self, *args, **kwargs):
                """Accepts and ignores the timeout."""

            async def __aenter__(self):
                """Entered by `async with aiohttp.ClientSession(...) as session`."""
                return self

            async def __aexit__(self, *args):
                """Nothing to close."""
                return False

            def request(self, method, url, headers=None, json=None):
                """Answer one request."""
                return fake.handle(method, url, json)

        return FakeSession

    def handle(self, method, url, body):
        """Route one request to its reader or writer; anything not modelled fails the test."""
        assert url.startswith(TESLEMETRY_DEFAULT_URL), "FakeTeslemetry: request to an unexpected host {}".format(url)
        path = url[len(TESLEMETRY_DEFAULT_URL) :]
        if method == "GET" and path == "/api/1/products":
            return FakeResponse(200, {"response": [{"energy_site_id": HARNESS_SITE_ID, "resource_type": "battery", "site_name": "Harness"}]})
        assert path.startswith(SITE_PATH), "FakeTeslemetry: not modelled path {} {}".format(method, path)
        endpoint = path[len(SITE_PATH) :]
        if method == "GET":
            if endpoint == "site_info":
                return FakeResponse(200, {"response": self.site_info()})
            if endpoint == "live_status":
                return FakeResponse(200, {"response": self.live_status()})
            if endpoint.startswith("calendar_history"):
                return FakeResponse(200, {"response": {"time_series": [self.energy_today()]}})
        if method == "POST":
            self.commands.append(CommandRecord(self.clock(), endpoint, body))
            self.apply_command(endpoint, body)
            return FakeResponse(200, {"response": {"code": 201, "message": "Updated"}})
        raise AssertionError("FakeTeslemetry: not modelled request {} {}".format(method, path))

    def apply_command(self, endpoint, body):
        """Change the device as the command asks."""
        if endpoint == "operation":
            self.mode = body["default_real_mode"]
        elif endpoint == "backup":
            percent = int(body["backup_reserve_percent"])
            if RESERVE_HONOURED_MAX < percent < RESERVE_FULL:
                self.band_writes.append((self.clock(), percent))
                percent = self.snap_band_to
            self.reserve = percent
        elif endpoint == "grid_import_export":
            if "disallow_charge_from_grid_with_solar_installed" in body:
                self.grid_charging = not body["disallow_charge_from_grid_with_solar_installed"]
            if "customer_preferred_export_rule" in body:
                self.export_rule = body["customer_preferred_export_rule"]
        elif endpoint == "time_of_use_settings":
            self.tariff = body["tou_settings"]["tariff_content_v2"]
        else:
            raise AssertionError("FakeTeslemetry: not modelled command {}".format(endpoint))

    def site_info(self):
        """A Powerwall 2 site: 13.5 kWh, 5 kW, battery export permitted."""
        return {
            "id": "harness",
            "site_name": "Harness",
            "nameplate_energy": int(self.capacity_kwh * 1000),
            "nameplate_power": self.rate_w,
            "battery_count": 1,
            "default_real_mode": self.mode,
            "backup_reserve_percent": self.reserve,
            "version": "25.18.4",
            "components": {"battery_type": "ac_powerwall", "net_meter_mode": "battery_ok", "gateways": []},
        }

    def live_status(self):
        """Live flows in Tesla's convention: battery positive discharging, grid positive importing."""
        return {
            "percentage_charged": round(self.soc_percent, 2),
            "total_pack_energy": int(self.capacity_kwh * 1000),
            "energy_left": int(self.soc_kwh * 1000),
            "battery_power": int(self.battery_w),
            "grid_power": int(self.grid_w),
            "load_power": int(self.last_load_w),
            "solar_power": int(self.last_pv_w),
        }

    def energy_today(self):
        """One calendar_history entry carrying today's totals in Wh."""
        return {
            "solar_energy_exported": self.pv_kwh * 1000,
            "grid_energy_imported": self.import_kwh * 1000,
            "grid_energy_exported_from_battery": self.export_kwh * 1000,
            "consumer_energy_imported_from_grid": self.load_kwh * 1000,
        }

    def tariff_prices(self, now):
        """(buy, sell, cheapest buy, dearest sell, dearest buy, cheapest sell) in the pushed tariff at now, or None before any tariff.

        A band only counts as cheap (or peak) when the tariff has more than one price on that side, so a flat
        tariff never reads as a charge or export signal.

        The band is looked up by time of day alone, which fits the signal tariff: it writes the same layout to
        every day of the week.
        """
        if not self.tariff:
            return None
        minute = now.hour * 60 + now.minute

        def price_at(side):
            """The price of the band covering minute on one side of the tariff."""
            rates = side["energy_charges"]["AllYear"]["rates"]
            for tier, block in side["seasons"]["AllYear"]["tou_periods"].items():
                for period in block["periods"]:
                    start = period["fromHour"] * 60 + period["fromMinute"]
                    end = period["toHour"] * 60 + period["toMinute"] or 1440
                    if start <= minute < end:
                        return rates.get(tier, 0.0), rates
            return 0.0, rates

        buy, buy_rates = price_at(self.tariff)
        sell, sell_rates = price_at(self.tariff["sell_tariff"])
        return buy, sell, min(buy_rates.values()), max(sell_rates.values()), max(buy_rates.values()), min(sell_rates.values())

    def advance(self, minutes=1):
        """Run the battery on for whole minutes at the current settings, keeping a trace row per minute."""
        for _ in range(minutes):
            now = self.clock()
            load_w = float(self.load_w(now))
            pv_w = float(self.pv_w(now))
            net_w = load_w - pv_w
            soc = self.soc_percent
            prices = self.tariff_prices(now) if self.mode == "autonomous" else None
            cheap = prices is not None and prices[0] == prices[2] and prices[2] < prices[4]
            peak = prices is not None and prices[1] == prices[3] and prices[3] > prices[5]
            if soc < self.reserve:
                # Meets the reserve from the grid whatever the grid-charging flag says, solar helping
                battery_w = -max(self.reserve_fill_w, -net_w if net_w < 0 else 0)
            elif cheap and self.grid_charging and soc < 100:
                battery_w = -self.rate_w
            elif peak and self.export_rule == "battery_ok" and soc > self.reserve:
                battery_w = self.rate_w
            elif net_w > 0 and soc > self.reserve:
                battery_w = min(net_w, self.rate_w)
            elif net_w < 0 and soc < 100:
                battery_w = max(net_w, -self.rate_w)
            else:
                battery_w = 0.0
            # Energy over the minute, kept between the reserve (discharging) and full (charging)
            delta_kwh = -battery_w / 60.0 / 1000.0
            if delta_kwh < 0:
                floor_kwh = min(self.soc_kwh, self.capacity_kwh * self.reserve / 100.0)
                delta_kwh = max(delta_kwh, floor_kwh - self.soc_kwh)
            else:
                delta_kwh = min(delta_kwh, self.capacity_kwh - self.soc_kwh)
            battery_w = -delta_kwh * 60.0 * 1000.0
            self.soc_kwh += delta_kwh
            grid_w = net_w - battery_w
            self.battery_w, self.grid_w, self.last_load_w, self.last_pv_w = battery_w, grid_w, load_w, pv_w
            self.import_kwh += max(grid_w, 0) / 60.0 / 1000.0
            self.export_kwh += max(-grid_w, 0) / 60.0 / 1000.0
            self.load_kwh += load_w / 60.0 / 1000.0
            self.pv_kwh += pv_w / 60.0 / 1000.0
            self.trace.append(TraceRow(now, self.soc_percent, self.reserve, self.mode, self.grid_charging, self.export_rule, battery_w, grid_w, load_w, pv_w))
