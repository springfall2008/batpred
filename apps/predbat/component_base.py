# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init


"""Abstract base class for all PredBat components.

Provides standardised lifecycle management (initialise, start, stop),
health monitoring with exponential backoff on startup failures, error
counting, and delegation to the main PredBat instance for HA operations.
All component types (HAInterface, WebInterface, SolarAPI, etc.) inherit
from this class.
"""

from abc import ABC, abstractmethod
from datetime import datetime, timezone

from utils import minutes_since_midnight
import asyncio
import time
import traceback

# Components typically come up in milliseconds, so the previous 1s poll spent nearly all of a
# start-up wait asleep after the component was already live. Polling ten times a second makes
# start-up feel immediate for no meaningful cost - one cheap flag check per tick. The wait is
# bounded by a monotonic deadline rather than by counting ticks, so `timeout` stays honest in
# seconds however often the flag is checked.
API_START_POLL_SECONDS = 0.1


class ComponentBase(ABC):
    """
    Base class for all Predbat components.

    This class defines a standard interface that all components should implement,
    providing consistent lifecycle management, health monitoring, and event handling.

    Components can inherit from this class to gain:
    - Standardized startup/shutdown interface
    - Health check and monitoring capabilities
    - Event handling framework
    - Common logging infrastructure

    Attributes:
        base: Reference to the main Predbat base object
        log: Logging function from the base object
        api_started: Flag indicating whether the component has successfully started
        api_stop: Flag to signal the component to stop
        last_success_timestamp: Timestamp of the last successful operation
        component_name: Registry key this component is filed under (e.g. "givtcp"), set by
            Components.initialize(); falls back to the class name for a component built outside
            the registry (the standalone CLI harnesses), so report_discovery() always has a name.
    """

    # Declared on the class, not only assigned in __init__, so they exist even on a component built
    # without it - the test harnesses construct components with Cls.__new__(Cls) to exercise one
    # method in isolation. refresh_discovery() must not be able to raise on such an instance: an
    # AttributeError escaping run() is exactly the degradation an observer is forbidden to cause.
    component_name = None
    _discovery_report = None

    def __init__(self, base, **kwargs):
        """
        Initialise the component base.

        Args:
            base: The main Predbat base object providing system-wide services
        """
        self.base = base
        self.log = base.log
        self.api_started = False
        self.api_stop = False
        self.last_success_timestamp = None
        self.local_tz = base.local_tz
        self.prefix = base.prefix
        self.args = base.args
        self.count_errors = 0
        self.run_timeout = 60 * 60  # Default run time in seconds, can be overridden by subclasses
        # Overridden with the registry key by Components.initialize() once this component is
        # constructed through the registry; a component built directly (the standalone CLI
        # harnesses) keeps this class-name fallback instead.
        self.component_name = self.__class__.__name__
        self.initialize(**kwargs)

    @abstractmethod
    def initialize(self, **kwargs):
        """
        Additional initialisation for subclasses.

        Subclasses can override this method to perform any additional setup
        required during initialisation.
        """
        pass

    def dashboard_item(self, entity, state, attributes, app=None):
        """
        Create a dashboard item representation.
        """
        return self.base.dashboard_item(entity, state, attributes, app=app)

    def get_ha_config(self, name, default):
        """
        Retrieve a Home Assistant configuration value from the base system.
        """
        return self.base.get_ha_config(name, default)

    def set_arg(self, arg, value):
        """
        Set a configuration argument in the base system.
        """
        return self.base.set_arg(arg, value)

    def set_arg_auto(self, arg, value, overwrite=True):
        """
        Like set_arg(), but for auto-discovery code (typically automatic_config()) binding an
        apps.yaml key to an auto-discovered entity/value.

        With overwrite=True (the default) auto-discovery wins and replaces whatever the user set,
        which is what every caller did before this option existed. Silently discarding an explicit
        apps.yaml entry left no way to notice (issue #4494 follow-up discussion, PR #4500), so a
        one-time note per key is logged when that happens.

        With overwrite=False the user's own apps.yaml entry wins and is left exactly as written;
        auto-discovery still fills the key in when the user set nothing. Callers use this for keys
        whose recorder HISTORY Predbat reads rather than just their current state - repointing one
        of those at a sensor Predbat has only just created throws that history away, which for the
        daily energy totals the load model is built from means planning against no history at all
        until the days build back up.

        Either way the decision is per key, and neither message repeats for the same key.
        """
        raw_args = getattr(self.base, "args_from_apps_yaml", None) or {}
        raw_value = raw_args.get(arg)
        user_configured = raw_value is not None and raw_value != value
        warned = getattr(self.base, "apps_yaml_override_warned", None)
        if user_configured and warned is not None and arg not in warned:
            warned.add(arg)
            if overwrite:
                self.log(f"Note: apps.yaml sets '{arg}: {raw_value}' but auto-discovery is using '{value}' instead - auto-discovery wins for this setting; remove the apps.yaml entry to avoid this message")
            else:
                self.log(f"Info: apps.yaml sets '{arg}: {raw_value}' - keeping your apps.yaml setting rather than auto-discovering '{value}'")
        if user_configured and not overwrite:
            # Deliberately not calling set_arg() at all - the user's own value is already in self.args
            return None
        return self.set_arg(arg, value)

    def get_arg(self, arg, default=None, indirect=True, combine=False, attribute=None, index=None, domain=None, can_override=True, required_unit=None):
        """
        Retrieve a configuration argument from the base system.
        """
        return self.base.get_arg(arg, default=default, indirect=indirect, combine=combine, attribute=attribute, index=index, domain=domain, can_override=can_override, required_unit=required_unit)

    def update_success_timestamp(self):
        """Update the last success timestamp to the current time"""
        self.last_success_timestamp = datetime.now(timezone.utc)

    def report_discovery(self, report):
        """Report what this component discovered to the discovery catalogue.

        Silently does nothing when there is no coordinator - the standalone CLI harnesses run a
        component against a MockBase with no registry at all.
        """
        components = getattr(self.base, "components", None)
        coordinator = getattr(components, "coordinator", None) if components else None
        if coordinator:
            coordinator.report(self.component_name or type(self).__name__, report)

    def refresh_discovery(self):
        """Rebuild this component's discovery report and file it if it has moved on.

        Call unconditionally once per run() cycle. A component opts in by defining
        build_discovery(), returning the report dict, or None when it has not discovered enough to
        describe yet (no serial, no account) - a component with no build_discovery() is a no-op.

        This is the whole reporting loop, so that a reporter only has to write the part that is
        actually its own. Three rules the reporters are otherwise each expected to remember are
        structural here instead:

        - The report is rebuilt and compared IN FULL on every call, never keyed on a hand-maintained
          snapshot of whatever build_discovery() happens to read. A key has to be kept in step with
          the build by hand, and when it drifts the symptom is a report frozen in its first,
          incomplete state for the life of the process - the endpoint that had not yet decoded its
          serial, the entity Home Assistant had not published yet. Comparing the built report cannot
          drift, and needs no completeness check either: a report that fills in later simply differs
          from the stored one and replaces it. Builds are dict work over data already in hand and
          run() is called about once a minute, so rebuilding to compare costs nothing measurable,
          and report() is still only reached when something actually moved.
        - The marker advances only after the report has been filed, and never on the failure path,
          so a transient failure is retried on the next cycle instead of being lost. This is why the
          call belongs OUTSIDE any one-shot "if first:" gate: "first" is a start()-local that flips
          to False forever the instant run() returns True, so a single failed cycle inside it can
          never be retried.
        - A failure is caught and logged here, never raised. An observer must not be able to degrade
          the health of the component it observes: an exception escaping run() withholds
          update_success_timestamp() and eventually pushes a healthy component toward unhealthy.
          Logged only, never non_fatal_error_occurred(): that sets base.had_errors, which makes
          update_pred() skip record_status() and suppress the run notification, so a purely
          observational side channel would be changing Predbat's user-visible status (see solis.py's
          own comment on the same trap).
        """
        build = getattr(self, "build_discovery", None)
        if build is None:
            return
        try:
            report = build()
            if report is None or report == self._discovery_report:
                return
            self.report_discovery(report)
            self._discovery_report = report
        except Exception as e:
            self.log("Warn: {}: failed to report discovery for the catalogue: {}".format(self.component_name or type(self).__name__, e))

    def discovery_entities(self, descriptors):
        """Keep only the entity descriptors Home Assistant has actually seen.

        `descriptors` maps a Predbat standard control name to its descriptor dict, each carrying at
        least an "entity_id". An entity spec describes what a component CAN publish, not what it HAS
        published on this install with this firmware - most reporters publish a good part of theirs
        conditionally - so the catalogue must never claim an entity exists that Home Assistant has
        never seen. Checking the state store is also what keeps this true across a restart
        mid-cycle, where a spec would still claim everything.
        """
        return {name: descriptor for name, descriptor in descriptors.items() if self.get_state_wrapper(descriptor["entity_id"]) is not None}

    @property
    def currency_symbols(self):
        """Get the currency symbols from the base system"""
        return self.base.currency_symbols

    @property
    def arg_errors(self):
        """Get the argument errors from the base system"""
        return self.base.arg_errors

    @property
    def now_utc(self):
        """Get the current time in UTC"""
        return self.base.now_utc

    @property
    def midnight_utc(self):
        """Get today's midnight time in UTC

        Derived from the base's now_utc rather than read from base.midnight_utc: calculate_yesterday()
        (output.py) rewinds the shared base.midnight_utc by a day for the duration of the savings
        calculation, and components run on their own threads, so a passthrough read can land on
        yesterday's midnight (GH#4804). now_utc is never faked by calculate_yesterday(), and always
        exists by the time a component does - initialize() calls update_time() before the components
        are constructed. The two agree outside the rewind: update_time() sets midnight_utc from now_utc.
        """
        return self.base.now_utc.replace(hour=0, minute=0, second=0, microsecond=0)

    @property
    def now_utc_exact(self):
        """Get the current time in the local timezone"""
        return datetime.now(self.local_tz)

    @property
    def minutes_now(self):
        """Get the current time in minutes since midnight

        Derived from the base's now_utc for the same reason as midnight_utc: calculate_yesterday()
        (output.py) fakes the shared base.minutes_now to 0 for the duration of the savings
        calculation, and a component reading it mid-rewind reads 0 - which, unlike a rewound date,
        looks like a perfectly legitimate "just after midnight" (GH#4804).

        This is the same calculation update_time() makes, through the same helper, so the value is
        identical to base.minutes_now outside that window.

        now_utc is snapshotted rather than read twice (once here, once through self.midnight_utc):
        update_time() runs on the main thread and can replace it between the two reads, which at a
        day boundary would subtract the new day's midnight from the old timestamp and return a
        negative minute.
        """
        now_utc = self.base.now_utc
        return minutes_since_midnight(now_utc, now_utc.replace(hour=0, minute=0, second=0, microsecond=0))

    @property
    def plan_interval_minutes(self):
        """Get the plan interval in minutes"""
        return self.base.plan_interval_minutes

    @property
    def num_cars(self):
        """Get the number of cars configured in the system"""
        return self.base.num_cars

    @property
    def config_root(self):
        """Get the configuration root directory"""
        return self.base.config_root

    @property
    def storage(self):
        """Get the storage component for save/load operations"""
        if hasattr(self, "base") and hasattr(self.base, "components") and self.base.components:
            return self.base.components.get_component("storage")
        return None

    def get_error_count(self):
        """Get the number of errors that have occurred in this component"""
        return self.count_errors

    def is_calculating(self):
        """Return whether the component is currently performing a long-running calculation."""
        return False

    async def start(self):
        """
        Start the component's main operation loop.

        This method should:
        - Initialise any required resources
        - Set api_started to True when ready
        - Run the main processing loop until api_stop is True
        - Clean up resources before exiting

        """
        seconds = 0
        first = True
        next_retry = 0  # When to next attempt self.run() during startup backoff
        backoff_interval = 60  # Start with 60 seconds between attempts
        max_backoff = 128 * 60  # Maximum 128 minutes between attempts
        while not self.api_stop and not self.fatal_error:
            try:
                # Check if it's time to run
                should_run = False
                if first:
                    # During startup, only run when we've reached the next retry time
                    if seconds >= next_retry:
                        should_run = True
                        backoff_interval = min(backoff_interval * 2, max_backoff)
                        next_retry = seconds + backoff_interval
                else:
                    # After startup, run every 60 seconds
                    if seconds % 60 == 0:
                        should_run = True

                if should_run:
                    task = asyncio.ensure_future(self.run(seconds, first))
                    try:
                        run_result = await asyncio.wait_for(asyncio.shield(task), timeout=self.run_timeout)
                    except asyncio.TimeoutError:
                        stack = task.get_stack()
                        tb_lines = ["Traceback of timed-out run():"] + ['  File "{}", line {}, in {}'.format(frame.f_code.co_filename, frame.f_lineno, frame.f_code.co_name) for frame in stack]
                        self.log("Error: {}: run() exceeded {}s timeout:\n{}".format(self.__class__.__name__, self.run_timeout, "\n".join(tb_lines)))
                        task.cancel()
                        try:
                            await task
                        except (asyncio.CancelledError, Exception):
                            pass
                        run_result = False
                    if run_result:
                        if not self.api_started:
                            self.api_started = True
                            self.log(f"{self.__class__.__name__}: Started")
                        # Clear first flag once started. This must happen even when a
                        # component sets api_started itself from a background task (e.g.
                        # the gateway's MQTT loop): otherwise first stays True forever and
                        # start() keeps re-running the first=True startup path on backoff,
                        # never reaching the steady-state housekeeping run().
                        first = False
                    else:
                        self.count_errors += 1
                        self.non_fatal_error_occurred()
                        self.log("Warn: " + f"{self.__class__.__name__}: run() returned False")
            except Exception as e:
                self.log(f"Error: {self.__class__.__name__}: {e}")
                self.log("Error: " + traceback.format_exc())
                self.non_fatal_error_occurred()
                self.count_errors += 1

            seconds += 5
            await asyncio.sleep(5)

        self.log(f"{self.__class__.__name__}: Finalizing...")
        await self.final()

        self.api_started = False
        self.log(f"{self.__class__.__name__}: Stopped")

    async def final(self):
        """
        Final cleanup before stopping.
        Subclasses can override this method to perform any necessary cleanup
        before the component stops.
        """
        pass

    async def stop(self):
        """
        Stop the component gracefully.

        This method:
        - Sets api_stop to True to signal the main loop to exit
        - Waits briefly to allow ongoing operations to complete
        - Releases any held resources as needed

        Subclasses may override this method if additional cleanup is required.
        """
        self.api_stop = True
        self.api_started = False
        await asyncio.sleep(0.1)  # Allow time for the main loop to exit

    def non_fatal_error_occurred(self):
        """
        Notify the base system that a non-fatal error has occurred.

        This method increments the non_fatal_error_count in the base object,
        which can be used for monitoring and logging purposes.
        """
        self.base.had_errors = True

    def fatal_error_occurred(self):
        """
        Notify the base system that a fatal error has occurred.

        This method sets the fatal_error flag in the base object,
        which can trigger system-wide error handling procedures.
        """
        self.base.fatal_error = True

    @property
    def fatal_error(self):
        """
        Check if a fatal error has occurred in the base system.

        Returns:
            bool: True if a fatal error has occurred, False otherwise
        """
        return self.base.fatal_error

    def get_history_wrapper(self, entity_id, days=30, required=True, tracked=True):
        return self.base.get_history_wrapper(entity_id, days=days, required=required, tracked=tracked)

    def get_state_wrapper(self, entity_id=None, default=None, attribute=None, refresh=False, required_unit=None, raw=False):
        return self.base.get_state_wrapper(entity_id, default=default, attribute=attribute, refresh=refresh, required_unit=required_unit, raw=raw)

    def set_state_wrapper(self, entity_id, state, attributes=None, required_unit=None):
        if attributes is None:
            attributes = {}
        return self.base.set_state_wrapper(entity_id, state, attributes=attributes, required_unit=required_unit)

    async def set_state_external(self, entity_id, state, attributes=None):
        """Change one of Predbat's OWN entities as if a user had, updating its CONFIG_ITEMS value.

        Distinct from set_state_wrapper, which only writes the entity state: components use this when
        auto-discovery has to change a Predbat setting (e.g. teslemetry turning inverter_hybrid off
        for an AC-coupled Powerwall), where writing the state alone would move the displayed entity
        without changing the value the planner reads.
        """
        if attributes is None:
            attributes = {}
        return await self.base.ha_interface.set_state_external(entity_id, state, attributes=attributes)

    def call_notify(self, message):
        return self.base.call_notify(message)

    def wait_api_started(self, timeout=10 * 60):
        """
        Wait for the component to start.

        Args:
            timeout: Maximum time to wait in seconds (default: 10*60)

        Returns:
            bool: True if component started successfully, False if timeout
        """
        self.log(f"{self.__class__.__name__}: Waiting for API to start")
        deadline = time.monotonic() + timeout
        while not self.api_started and time.monotonic() < deadline:
            time.sleep(API_START_POLL_SECONDS)
        if not self.api_started:
            self.log(f"Warn: {self.__class__.__name__}: Failed to start")
            return False
        return True

    def is_alive(self):
        """
        Check if the component is alive and functioning.

        Default implementation checks if the component has started.
        Subclasses can override to add additional health checks.

        Returns:
            bool: True if component is alive and healthy, False otherwise
        """
        return self.api_started

    def health_message(self):
        """
        Return a short reason this component is unhealthy, or None when it has nothing to add.

        Surfaced next to the component name in the final run status, so a user reading
        "component errors: Solis" is told what actually went wrong.

        Returns:
            str: A short reason, or None
        """
        return None

    def last_updated_time(self):
        """
        Get the timestamp of the last successful operation.

        Returns:
            datetime: Timestamp of last successful operation, or None if never succeeded
        """
        return self.last_success_timestamp

    async def select_event(self, entity_id, value):
        """
        Handle select entity state changes from Home Assistant.

        Args:
            entity_id: The entity ID that changed
            value: The new selected value

        Default implementation does nothing. Override in subclasses that handle select events.
        """
        pass

    async def number_event(self, entity_id, value):
        """
        Handle number entity value changes from Home Assistant.

        Args:
            entity_id: The entity ID that changed
            value: The new numeric value

        Default implementation does nothing. Override in subclasses that handle number events.
        """
        pass

    async def switch_event(self, entity_id, service):
        """
        Handle switch entity service calls from Home Assistant.

        Args:
            entity_id: The entity ID being controlled
            service: The service being called (e.g., 'turn_on', 'turn_off')

        Default implementation does nothing. Override in subclasses that handle switch events.
        """
        pass
