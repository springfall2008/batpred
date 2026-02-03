# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2025 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Axle Energy Virtual Power Plant (VPP) API library
# -----------------------------------------------------------------------------

# API Documentation: https://vpp.axle.energy/landing/home-assistant

from datetime import datetime, timedelta
import asyncio
import aiohttp
from component_base import ComponentBase
from utils import str2time, minutes_to_time, TIME_FORMAT


class AxleAPI(ComponentBase):
    """Axle Energy VPP client."""

    def initialize(self, api_key, pence_per_kwh, automatic):
        """Initialize the AxleAPI component"""
        self.api_key = api_key
        self.pence_per_kwh = pence_per_kwh
        self.automatic = automatic
        self.failures_total = 0
        self.event_history = []  # List of past events
        self.current_event = {  # Current event
            "start_time": None,
            "end_time": None,
            "import_export": None,
            "pence_per_kwh": None,
        }
        self.updated_at: None  # Last updated moved out to separate attribute to not pollute triggering on change of current_event

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

                    self.log(f"Axle API: Loaded {len(self.event_history)} past events from sensor history")
        except Exception as e:
            self.log(f"Warn: Axle API: Failed to load event history: {e}")

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
            self.log(f"Axle API: Cleaned up {removed} events older than 7 days")

    def add_event_to_history(self, event_data):
        """
        Add an event to history as soon as it starts (becomes active).
        Once an event starts, it won't change and should be recorded.
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

        # Only add if event has started (not future events)
        # Once event starts, add it to history even if still active
        if start_time > self.now_utc:
            return

        # Check if event already exists in history (by start_time string)
        for existing_event in self.event_history:
            if existing_event.get("start_time") == start_time_str:
                # Update existing event in case any details changed
                existing_event.update(event_data)
                return

        # Add new event to history
        self.event_history.append(event_data.copy())
        self.log(f"Axle API: Added event to history - {event_data.get('import_export')} from {start_time_str} to {end_time_str}")

    async def _request_with_retry(self, url, headers, max_retries=3):
        """
        Perform HTTP GET request with retry logic, check status code, and decode JSON

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
                                return await response.json()
                            except Exception as e:
                                self.log(f"Warn: Axle API: Failed to parse JSON response: {e}")
                                return None
                        else:
                            self.log(f"Warn: Axle API: Failed to fetch data, status code {response.status}")
                            return None
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if attempt < max_retries - 1:
                    sleep_time = 2**attempt  # Exponential backoff: 1s, 2s, 4s
                    self.log(f"Warn: Axle API: Request attempt {attempt + 1} failed: {e}. Retrying in {sleep_time}s...")
                    await asyncio.sleep(sleep_time)
                else:
                    self.log(f"Warn: Axle API: Request failed after {max_retries} attempts: {e}")
                    return None
        return None

    async def fetch_axle_event(self):
        """
        Fetch the latest VPP event from Axle Energy API
        """
        self.log("Axle API: Fetching latest VPP event data")

        url = "https://api.axle.energy/vpp/home-assistant/event"
        headers = {"Authorization": f"Bearer {self.api_key}"}

        data = await self._request_with_retry(url, headers)
        if data is None:
            self.log("Axle API: Warn: No event data in response")
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
                    self.log(f"Warn: Axle API: Failed to parse start_time: {e}")
                    start_time = None

            if end_time:
                try:
                    end_time = str2time(end_time)
                except Exception as e:
                    self.log(f"Warn: Axle API: Failed to parse end_time: {e}")
                    end_time = None

            if updated_at:
                try:
                    updated_at = str2time(updated_at)
                except Exception as e:
                    self.log(f"Warn: Axle API: Failed to parse updated_at: {e}")
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
                        self.call_notify("Predbat: Scheduled Axle VPP event {}-{}, {} p/kWh".format(start_time.strftime("%a %d/%m %H:%M"), end_time.strftime("%H:%M"), self.pence_per_kwh))

            self.cleanup_event_history()
            self.publish_axle_event()
            self.log(f"Axle API: Successfully fetched event data - {import_export} event from {start_time} to {end_time}" if start_time else "Axle API: No scheduled event")
            self.update_success_timestamp()

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
            },
        )

    async def automatic_config(self):
        """
        Automatic configuration for Axle participation
        """
        self.log("Axle API: Automatic configuration of entities")
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
                self.log(f"Warn: Axle API: Exception during fetch: {e}")
                self.failures_total += 1

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

            base.log("Axle API: Fetched {} total events from sensor {}".format(len(axle_events_deduplicated), entity_id))

    return axle_events_deduplicated


def load_axle_slot(base, axle_sessions, export, rate_replicate={}):
    """
    Load Axle VPP session slot
    """

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
                        rate_replicate[minute] = "saving"

                        # Track Axle override in rate store
                        if base.rate_store:
                            today = datetime.now()
                            base.rate_store.update_auto_override(today, minute, None, base.rate_export[minute], "Axle")
                    else:
                        base.rate_import[minute] = base.rate_import.get(minute, 0) + pence_per_kwh
                        base.load_scaling_dynamic[minute] = base.load_scaling_saving
                        rate_replicate[minute] = "saving"

                        # Track Axle override in rate store
                        if base.rate_store:
                            today = datetime.now()
                            base.rate_store.update_auto_override(today, minute, base.rate_import[minute], None, "Axle")


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
