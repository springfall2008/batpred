# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2025 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from abc import ABC, abstractmethod
from datetime import datetime, timezone
import asyncio
import time
import traceback


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
    """

    def __init__(self, base, **kwargs):
        """
        Initialize the component base.

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
        self.initialize(**kwargs)
        self.count_errors = 0

    @abstractmethod
    def initialize(self, **kwargs):
        """
        Additional initialization for subclasses.

        Subclasses can override this method to perform any additional setup
        required during initialization.
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

    def get_arg(self, arg, default=None, indirect=True, combine=False, attribute=None, index=None, domain=None, can_override=True, required_unit=None):
        """
        Retrieve a configuration argument from the base system.
        """
        return self.base.get_arg(arg, default=default, indirect=indirect, combine=combine, attribute=attribute, index=index, domain=domain, can_override=can_override, required_unit=required_unit)

    def update_success_timestamp(self):
        """Update the last success timestamp to the current time"""
        self.last_success_timestamp = datetime.now(timezone.utc)

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
        """Get today's midnight time in UTC"""
        return self.base.midnight_utc

    @property
    def now_utc_exact(self):
        """Get the current time in the local timezone"""
        return datetime.now(self.local_tz)

    @property
    def minutes_now(self):
        """Get the current time in minutes since midnight"""
        return self.base.minutes_now

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

    async def start(self):
        """
        Start the component's main operation loop.

        This method should:
        - Initialize any required resources
        - Set api_started to True when ready
        - Run the main processing loop until api_stop is True
        - Clean up resources before exiting

        """
        seconds = 0
        first = True
        while not self.api_stop and not self.fatal_error:
            try:
                if first or seconds % 60 == 0:
                    if await self.run(seconds, first):
                        if not self.api_started:
                            self.api_started = True
                            self.log(f"{self.__class__.__name__}: Started")
                    else:
                        self.count_errors += 1
                first = False
            except Exception as e:
                self.log(f"Error: {self.__class__.__name__}: {e}")
                self.log("Error: " + traceback.format_exc())

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

    def get_state_wrapper(self, entity_id=None, default=None, attribute=None, refresh=False, required_unit=None):
        return self.base.get_state_wrapper(entity_id, default=default, attribute=attribute, refresh=refresh, required_unit=required_unit)

    def set_state_wrapper(self, entity_id, state, attributes={}, required_unit=None):
        return self.base.set_state_wrapper(entity_id, state, attributes=attributes, required_unit=required_unit)

    def wait_api_started(self, timeout=5 * 60):
        """
        Wait for the component to start.

        Args:
            timeout: Maximum time to wait in seconds (default: 5*60)

        Returns:
            bool: True if component started successfully, False if timeout
        """
        self.log(f"{self.__class__.__name__}: Waiting for API to start")
        count = 0
        while not self.api_started and count < timeout:
            time.sleep(1)
            count += 1
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
