# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Axle Energy Virtual Power Plant (VPP) API library
# -----------------------------------------------------------------------------

# API Documentation: https://vpp.axle.energy/landing/home-assistant


"""Axle Energy Virtual Power Plant (VPP) event management.

Integrates with the Axle Energy API to receive and process VPP events
(import/export commands) with event history tracking and binary sensor
publishing for demand response participation.

Supports two modes:
- BYOK (Bring Your Own Key): User's own API key with /vpp/home-assistant/event endpoint.
- Managed VPP: Partner credentials with /entities/site/{site_id}/price-curve endpoint.
"""

from datetime import datetime, timedelta, timezone
import asyncio
import aiohttp
from component_base import ComponentBase
from predbat_metrics import record_api_call
from utils import str2time, minutes_to_time, TIME_FORMAT


class AxleAPI(ComponentBase):
    """Axle Energy VPP client with managed mode support."""

    def initialize(self, api_key, pence_per_kwh, automatic, managed_mode=False, site_id=None, partner_username=None, partner_password=None, api_base_url="https://api.axle.energy"):
        """Initialise the AxleAPI component.

        Args:
            api_key: BYOK API key (used in BYOK mode only)
            pence_per_kwh: VPP compensation rate in pence per kWh
            automatic: Whether to auto-configure entity mappings
            managed_mode: If True, use partner API price curve instead of BYOK event endpoint
            site_id: Axle site ID (required for managed mode)
            partner_username: Partner API username (required for managed mode)
            partner_password: Partner API password (required for managed mode)
            api_base_url: Axle API base URL
        """
        # Validate api_key type (common YAML misconfiguration: list instead of string)
        if api_key is not None and (not isinstance(api_key, str) or not api_key):
            self.log("Error: AxleAPI: axle_api_key is missing or invalid, you must set it to a string (not a list or number). Axle Energy integration will not function correctly.")
            api_key = None
        self.api_key = api_key
        self.pence_per_kwh = pence_per_kwh
        self.automatic = automatic
        self.failures_total = 0
        self.history_loaded = False
        self.event_history = []  # List of past events
        self.current_event = {  # Current event
            "start_time": None,
            "end_time": None,
            "import_export": None,
            "pence_per_kwh": None,
        }
        self.updated_at = None  # Last updated moved out to separate attribute to not pollute triggering on change of current_event

        # Managed mode config
        self.managed_mode = managed_mode
        self.site_id = site_id
        self.partner_username = partner_username
        self.partner_password = partner_password
        self.api_base_url = api_base_url.rstrip("/") if api_base_url else "https://api.axle.energy"
        self.partner_token = None
        self.partner_token_expiry = None

        if self.managed_mode:
            missing = []
            if not self.site_id:
                missing.append("site_id")
            if not self.partner_username:
                missing.append("partner_username")
            if not self.partner_password:
                missing.append("partner_password")
            if missing:
                self.log(f"Error: AxleAPI: Managed mode missing required params: {', '.join(missing)}")
                self.managed_mode = False  # Fall back to disabled
        elif not self.api_key:
            self.log("Warn: AxleAPI: BYOK mode requires api_key — Axle integration disabled")

    def load_event_history(self):
        """
        Load event history from the sensor on startup.
        Only keep past events (not future ones).
        """

        try:
            sensor_id = "binary_sensor." + self.prefix + "_axle_event"
            state = self.get_state_wrapper(sensor_id)

            if state:
                attributes = self.get_state_wrapper(sensor_id, attribute="event_history")
                if attributes and isinstance(attributes, list):
                    now = self.now_utc
                    # Only keep past events (end_time in the past)
                    for event in attributes:
                        if isinstance(event, dict):
                            end_time = event.get("end_time")
                            if end_time:
                                # Parse if string
                                if isinstance(end_time, str):
                                    try:
                                        end_time = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
                                    except Exception:
                                        continue
                                # Only keep if event has ended
                                if end_time < now:
                                    self.event_history.append(event)

                    self.log(f"AxleAPI: Loaded {len(self.event_history)} past events from sensor history")
        except Exception as e:
            self.log(f"Warn: AxleAPI: Failed to load event history: {e}")

        self.history_loaded = True

    def cleanup_event_history(self):
        """
        Remove events older than 7 days from history
        """
        if not self.event_history:
            return

        cutoff_time = self.now_utc - timedelta(days=7)
        original_count = len(self.event_history)

        # Keep only events that ended within the last 7 days
        cleaned_history = []
        for event in self.event_history:
            end_time_str = event.get("end_time")
            if end_time_str:
                try:
                    end_time = str2time(end_time_str)
                    if end_time >= cutoff_time:
                        cleaned_history.append(event)
                except (ValueError, TypeError):
                    # Skip events with invalid timestamps
                    pass

        self.event_history = cleaned_history
        removed = original_count - len(self.event_history)
        if removed > 0:
            self.log(f"AxleAPI: Cleaned up {removed} events older than 7 days")

    def add_event_to_history(self, event_data, allow_future=False):
        """Add an event to history.

        Args:
            event_data: Event dict with start_time, end_time, import_export, pence_per_kwh.
            allow_future: If True, accept future events (used by managed mode price curves).
                          BYOK mode only adds events once they become active.
        """
        start_time_str = event_data.get("start_time")
        end_time_str = event_data.get("end_time")

        if not start_time_str or not end_time_str:
            return

        # Parse string timestamps to datetime objects for comparison
        try:
            start_time = str2time(start_time_str)
        except (ValueError, TypeError):
            self.log(f"Warn: Unable to parse start_time for history: {start_time_str}")
            return

        # Only add if event has started (not future events), unless allow_future
        if not allow_future and start_time > self.now_utc:
            return

        # Check if event already exists in history (by start_time and import_export direction)
        # Managed mode creates both export and import per timeslot, so dedup must check direction
        for existing_event in self.event_history:
            if existing_event.get("start_time") == start_time_str and existing_event.get("import_export") == event_data.get("import_export"):
                # Update existing event in case any details changed
                existing_event.update(event_data)
                return

        # Add new event to history
        self.event_history.append(event_data.copy())
        self.log(f"AxleAPI: Added event to history - {event_data.get('import_export')} from {start_time_str} to {end_time_str}")

    async def _request_with_retry(self, url, headers, max_retries=3):
        """
        Perform HTTP GET request with retry logic, check status code, and decode JSON

        Retries on connection errors, timeouts, and 5xx server errors.
        Returns None immediately on 4xx client errors (no retry).

        Args:
            url: URL to request
            headers: Request headers
            max_retries: Maximum number of retry attempts (default: 3)

        Returns:
            Decoded JSON data if successful (status 200), None if failed or non-200 status
        """
        timeout = aiohttp.ClientTimeout(total=30)

        for attempt in range(max_retries):
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(url, headers=headers) as response:
                        if response.status == 200:
                            try:
                                data = await response.json()
                                record_api_call("axle")
                                return data
                            except Exception as e:
                                self.log(f"Warn: AxleAPI: Failed to parse JSON response: {e}")
                                record_api_call("axle", False, "decode_error")
                                return None
                        elif response.status >= 500:
                            # Server error — retry with backoff
                            record_api_call("axle", False, "server_error")
                            if attempt < max_retries - 1:
                                sleep_time = 2**attempt
                                self.log(f"Warn: AxleAPI: Server error {response.status}, retrying in {sleep_time}s...")
                                await asyncio.sleep(sleep_time)
                            else:
                                self.log(f"Warn: AxleAPI: Server error {response.status} after {max_retries} attempts")
                                return None
                        else:
                            # Client error (4xx) — no retry
                            self.log(f"Warn: AxleAPI: Failed to fetch data, status code {response.status}")
                            record_api_call("axle", False, "client_error")
                            return None
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if attempt < max_retries - 1:
                    sleep_time = 2**attempt  # Exponential backoff: 1s, 2s, 4s
                    self.log(f"Warn: AxleAPI: Request attempt {attempt + 1} failed: {e}. Retrying in {sleep_time}s...")
                    await asyncio.sleep(sleep_time)
                else:
                    self.log(f"Warn: AxleAPI: Request failed after {max_retries} attempts: {e}")
                    record_api_call("axle", False, "connection_error")
                    return None
        return None

    async def _get_partner_token(self):
        """Authenticate with Axle partner API and cache the token."""
        if self.partner_token and self.partner_token_expiry and self.partner_token_expiry > datetime.now(timezone.utc):
            return self.partner_token

        url = f"{self.api_base_url}/auth/token-form"
        timeout = aiohttp.ClientTimeout(total=30)

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    url,
                    data={
                        "grant_type": "password",
                        "username": self.partner_username,
                        "password": self.partner_password,
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        token = data.get("access_token")
                        if not token:
                            self.log("Warn: AxleAPI: Auth response missing access_token")
                            record_api_call("axle", False, "auth_failed")
                            return None
                        self.partner_token = token
                        # Cache for 50 minutes (tokens typically last 60 min)
                        self.partner_token_expiry = datetime.now(timezone.utc) + timedelta(minutes=50)
                        self.log("AxleAPI: Partner token obtained successfully")
                        record_api_call("axle")
                        return self.partner_token
                    else:
                        self.log(f"Warn: AxleAPI: Partner auth failed (status {response.status})")
                        record_api_call("axle", False, "auth_failed")
                        return None
        except Exception as e:
            self.log(f"Warn: AxleAPI: Partner auth exception: {e}")
            record_api_call("axle", False, "auth_error")
            return None

    async def fetch_axle_event(self):
        """Fetch the latest VPP event from Axle Energy API.

        In BYOK mode: GET /vpp/home-assistant/event with user's API key.
        In managed mode: GET /entities/site/{site_id}/price-curve with partner token.
        """
        if self.managed_mode:
            await self._fetch_managed_price_curve()
        else:
            await self._fetch_byok_event()

    async def _fetch_byok_event(self):
        """Original BYOK event fetch logic."""
        if not self.api_key:
            self.log("Error: AxleAPI: Cannot fetch event - axle_api_key is not set or invalid. Please check your apps.yaml configuration.")
            self.failures_total += 1
            return

        self.log("AxleAPI: Fetching latest VPP event data")

        url = f"{self.api_base_url}/vpp/home-assistant/event"
        headers = {"Authorization": f"Bearer {self.api_key}"}

        data = await self._request_with_retry(url, headers)
        if data is None:
            self.log("AxleAPI: Warn: No event data in response")
            self.failures_total += 1
        elif isinstance(data, dict):
            # Convert ISO 8601 strings to timezone-aware datetime objects
            start_time = data.get("start_time")
            end_time = data.get("end_time")
            import_export = data.get("import_export")
            updated_at = data.get("updated_at")

            # Parse datetime strings
            if start_time:
                try:
                    start_time = str2time(start_time)
                except Exception as e:
                    self.log(f"Warn: AxleAPI: Failed to parse start_time: {e}")
                    start_time = None

            if end_time:
                try:
                    end_time = str2time(end_time)
                except Exception as e:
                    self.log(f"Warn: AxleAPI: Failed to parse end_time: {e}")
                    end_time = None

            if updated_at:
                try:
                    updated_at = str2time(updated_at)
                except Exception as e:
                    self.log(f"Warn: AxleAPI: Failed to parse updated_at: {e}")
                    updated_at = None

            self.current_event = {
                "start_time": start_time.strftime(TIME_FORMAT) if start_time else None,
                "end_time": end_time.strftime(TIME_FORMAT) if end_time else None,
                "import_export": import_export,
                "pence_per_kwh": self.pence_per_kwh,
            }
            self.updated_at = updated_at.strftime(TIME_FORMAT) if updated_at else None

            # Get the sensor entity_id from configuration
            sensor_id = "binary_sensor." + self.prefix + "_axle_event"
            current_start_time = None  # start and end of any current Axle event
            current_end_time = None
            if sensor_id:
                # Fetch current event(s)
                event_current = self.get_state_wrapper(entity_id=sensor_id, attribute="event_current")
                if event_current and isinstance(event_current, list):
                    current_start_time = event_current[0]["start_time"]
                    current_end_time = event_current[0]["end_time"]

            if start_time and end_time:
                # An Axle event is planned

                # Add to history once event has started (active or past events)
                self.add_event_to_history(self.current_event)

                # send alert if this is a new Axle event
                if (start_time.strftime(TIME_FORMAT) != current_start_time) or (end_time.strftime(TIME_FORMAT) != current_end_time):
                    if self.get_arg("set_event_notify"):
                        local_start = start_time.astimezone(self.local_tz)
                        local_end = end_time.astimezone(self.local_tz)
                        self.call_notify("Predbat: Scheduled Axle VPP event {}-{}, {} p/kWh".format(local_start.strftime("%a %d/%m %H:%M"), local_end.strftime("%H:%M"), self.pence_per_kwh))

            self.cleanup_event_history()
            self.publish_axle_event()
            self.log(f"AxleAPI: Successfully fetched event data - {import_export} event from {start_time} to {end_time}" if start_time else "AxleAPI: No scheduled event")
            self.update_success_timestamp()

    async def _fetch_managed_price_curve(self):
        """Fetch price curve from Axle partner API and convert to sessions."""
        if not self.site_id:
            self.log("Warn: AxleAPI: Managed mode requires site_id")
            return

        token = await self._get_partner_token()
        if not token:
            self.log("Warn: AxleAPI: Could not obtain partner token")
            self.failures_total += 1
            return

        self.log(f"AxleAPI: Fetching price curve for site {self.site_id} (managed)")
        url = f"{self.api_base_url}/entities/site/{self.site_id}/price-curve"
        headers = {"Authorization": f"Bearer {token}"}

        data = await self._request_with_retry(url, headers)
        if data is None:
            # Could be an expired/revoked token — invalidate and retry once
            self.partner_token = None
            self.partner_token_expiry = None
            token = await self._get_partner_token()
            if token:
                headers = {"Authorization": f"Bearer {token}"}
                data = await self._request_with_retry(url, headers)

        if data is None:
            self.log("Warn: AxleAPI: No price curve data after retry")
            self.failures_total += 1
            return

        self._process_price_curve(data)
        self.cleanup_event_history()
        self.publish_axle_event()
        self.update_success_timestamp()
        self.log("AxleAPI: Price curve processed successfully (managed mode)")

    def _process_price_curve(self, data):
        """Convert Axle price curve to session format for load_axle_slot().

        The price curve provides half-hourly wholesale market prices (GBP/MWh).
        These are overlaid onto existing tariff rates:
        - Export: wholesale price added to rate_export (high price = more export)
        - Import: wholesale price added to rate_import (high price = less import,
          negative price = cheap import encourages charging)
        - Null prices are skipped (no modification, normal tariff applies)

        Note: load_axle_slot subtracts import pence_per_kwh, so we negate it here
        to achieve addition of the wholesale price to the import rate.
        """
        prices = data.get("half_hourly_traded_prices", [])
        session_count = 0

        for slot in prices:
            price_gbp_mwh = slot.get("price_gbp_per_mwh")
            if price_gbp_mwh is None:
                continue

            start_str = slot.get("start_timestamp")
            if not start_str:
                continue

            try:
                start_dt = str2time(start_str)
            except (ValueError, TypeError):
                self.log(f"Warn: AxleAPI: Failed to parse price curve timestamp: {start_str}")
                continue

            end_dt = start_dt + timedelta(minutes=30)
            pence_per_kwh = price_gbp_mwh / 10  # GBP/MWh -> p/kWh

            start_formatted = start_dt.strftime(TIME_FORMAT)
            end_formatted = end_dt.strftime(TIME_FORMAT)

            # Create export session: adds wholesale price as export bonus
            # load_axle_slot does: rate_export + pence_per_kwh
            export_session = {
                "start_time": start_formatted,
                "end_time": end_formatted,
                "import_export": "export",
                "pence_per_kwh": pence_per_kwh,
            }
            self.add_event_to_history(export_session, allow_future=True)

            # Create import session: adds wholesale price to import cost
            # load_axle_slot does: rate_import - pence_per_kwh, so we negate
            # to get rate_import + wholesale_price (high price = expensive import,
            # negative price = cheap import)
            import_session = {
                "start_time": start_formatted,
                "end_time": end_formatted,
                "import_export": "import",
                "pence_per_kwh": -pence_per_kwh,
            }
            self.add_event_to_history(import_session, allow_future=True)
            session_count += 1

        self.log(f"AxleAPI: Processed {session_count} price curve slots into sessions")

        # Update current event to the nearest future/active slot for sensor state
        now = self.now_utc
        self.current_event = {
            "start_time": None,
            "end_time": None,
            "import_export": None,
            "pence_per_kwh": None,
        }
        self.updated_at = now.strftime(TIME_FORMAT)

        # Find the active or nearest future session for display
        for event in self.event_history:
            try:
                start = str2time(event["start_time"])
                end = str2time(event["end_time"])
                if start <= now < end and event.get("import_export") == "export":
                    self.current_event = event.copy()
                    break
            except (ValueError, TypeError, KeyError):
                continue

    def publish_axle_event(self):
        """
        Publish the latest Axle VPP event to the system as a binary sensor
        State is "on" if event is currently active, "off" otherwise
        Event history is stored in attributes
        """
        start_time_str = self.current_event.get("start_time")
        end_time_str = self.current_event.get("end_time")

        # Determine if event is currently active
        state = "off"
        if start_time_str and end_time_str:
            try:
                start_time = str2time(start_time_str)
                end_time = str2time(end_time_str)
                now = self.now_utc
                if now >= start_time and now < end_time:
                    state = "on"
            except (ValueError, TypeError):
                self.log("Warn: Unable to parse event times for state determination")

        # Create event_current as a list containing the current event (if valid)
        event_current = []
        if start_time_str and end_time_str:
            event_current = [self.current_event.copy()]

        # Publish binary sensor with current event and history
        self.dashboard_item(
            "binary_sensor." + self.prefix + "_axle_event",
            state=state,
            app="axle",
            attributes={
                "friendly_name": "Axle Event",
                "icon": "mdi:transmission-tower",
                "event_current": event_current,
                "event_history": self.event_history,
                "updated_at": self.updated_at,
                "managed_mode": self.managed_mode,
            },
        )

    async def automatic_config(self):
        """
        Automatic configuration for Axle participation
        """
        self.log("AxleAPI: Automatic configuration of entities")
        self.set_arg("axle_session", "binary_sensor." + self.prefix + "_axle_event")

    async def run(self, seconds, first):
        # Load history from sensor on first run
        if first:
            self.load_event_history()
            if self.automatic:
                await self.automatic_config()

        """
        Main run loop - poll API every 10 minutes (600 seconds)
        """
        if first or (seconds % (10 * 60) == 0):  # Every 10 minutes
            try:
                await self.fetch_axle_event()
            except Exception as e:
                self.log(f"Warn: AxleAPI: Exception during fetch: {e}")
                self.failures_total += 1
        elif (seconds % 60) == 0:  # Every minute, update state to reflect if event is active or not
            self.publish_axle_event()

        return True


def fetch_axle_sessions(base):
    """
    Fetch Axle events (current and past) from the sensor
    Returns a list of all events (current + history)
    """
    axle_events = []
    axle_events_deduplicated = []

    # Get the sensor entity_id from configuration
    entity_id = base.get_arg("axle_session", indirect=False)
    if entity_id:
        # Fetch current event(s)
        event_current = base.get_state_wrapper(entity_id=entity_id, attribute="event_current")
        if event_current and isinstance(event_current, list):
            axle_events.extend(event_current)

        # Fetch event history
        event_history = base.get_state_wrapper(entity_id=entity_id, attribute="event_history")
        if event_history and isinstance(event_history, list):
            axle_events.extend(event_history)

        if axle_events:
            # deduplicate events, which occurs when a current event starts and it's immediately written to the history
            for i in range(len(axle_events)):
                if axle_events[i] not in axle_events[i + 1 :]:
                    axle_events_deduplicated.append(axle_events[i])

            base.log("AxleAPI: Fetched {} total events from sensor {}".format(len(axle_events_deduplicated), entity_id))

    return axle_events_deduplicated


def load_axle_slot(base, axle_sessions, export, rate_replicate=None):
    """
    Load Axle VPP session slot
    """
    if rate_replicate is None:
        rate_replicate = {}

    for axle_session in axle_sessions:
        start_time = axle_session.get("start_time")
        end_time = axle_session.get("end_time")
        import_export = axle_session.get("import_export")
        pence_per_kwh = axle_session.get("pence_per_kwh")
        start_minutes = None
        end_minutes = None

        if start_time and end_time and import_export and pence_per_kwh is not None:
            try:
                start_time = str2time(start_time)
                end_time = str2time(end_time)
            except (ValueError, TypeError):
                start_time = None
                end_time = None
                base.log("Warn: Unable to decode Axle VPP session start/end time")
        if start_time and end_time:
            start_minutes = minutes_to_time(start_time, base.midnight_utc)
            end_minutes = min(minutes_to_time(end_time, base.midnight_utc), base.forecast_minutes + base.minutes_now)

        if start_minutes is not None and end_minutes is not None and start_minutes < (base.forecast_minutes + base.minutes_now):
            if (export and import_export == "export") or (not export and import_export == "import"):
                base.log("Setting Axle VPP session in range {} - {} export {} pence_per_kwh {}".format(base.time_abs_str(start_minutes), base.time_abs_str(end_minutes), export, pence_per_kwh))
                for minute in range(start_minutes, end_minutes):
                    if export:
                        base.rate_export[minute] = base.rate_export.get(minute, 0) + pence_per_kwh
                        base.load_scaling_dynamic[minute] = base.load_scaling_saving
                        rate_replicate[minute] = "saving"
                    else:
                        base.rate_import[minute] = base.rate_import.get(minute, 0) - pence_per_kwh
                        base.load_scaling_dynamic[minute] = base.load_scaling_free
                        rate_replicate[minute] = "saving"


def fetch_axle_active(base):
    """
    Check if an Axle VPP event is currently active

    Args:
        base: PredBat instance with access to get_arg and get_state_wrapper

    Returns:
        bool: True if an Axle event is currently active (sensor state is "on"), False otherwise
    """
    entity_id = base.get_arg("axle_session", indirect=False)
    if not entity_id:
        return False

    state = base.get_state_wrapper(entity_id=entity_id, default="off")
    return str(state).lower() == "on"
