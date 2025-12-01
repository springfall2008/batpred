# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2025 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt: off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""
Component Client for Remote Component Execution

WARNING: This client uses pickle for serialization. Only use in trusted networks
(e.g. Kubernetes cluster) as pickle can execute arbitrary code.
"""

import asyncio
import pickle
import time
import uuid
import traceback
from datetime import datetime, timezone
import aiohttp


class ComponentClient:
    """
    Client proxy that forwards component operations to a remote ComponentServer.

    This class inherits from ComponentBase and acts as a transparent proxy for
    remote components, handling all communication with the component server.
    """
    def __init__(self, base, server_url, remote_class, **component_kwargs):
        """
        Initialize the component client.

        Args:
            base: The main Predbat base object
            server_url: URL of the component server
            remote_class: Name of the remote component class
            **component_kwargs: Initialization kwargs to pass to remote component
        """
        # Store parameters before calling parent init
        self.server_url = server_url
        self.remote_class = remote_class
        self.component_kwargs = component_kwargs
        self.client_id = None
        self.session = None
        self.last_ping_time = 0
        self.restart_lock = asyncio.Lock()
        self.component_started = False

        # Initialize base attributes (mimic ComponentBase)
        self.base = base
        self.log = base.log
        self.api_started = False
        self.api_stop = False
        self.last_success_timestamp = None
        self.local_tz = base.local_tz
        self.prefix = base.prefix
        self.args = base.args
        self.currenty_symbols = base.currency_symbols
        self.count_errors = 0

    def wait_api_started(self, timeout=5*60):
        """
        Wait for the component's API to be started (self.api_started == True).
        Returns True if started, False if timeout.
        """
        start = time.time()
        self.log(f"ComponentClient: Waiting for API to start (timeout {timeout}s)...")
        while not self.api_started:
            if time.time() - start > timeout:
                return False
            time.sleep(1)
        return True

    async def start(self):
        """
        Start the component client.

        This method:
        1. Gets or creates a client ID
        2. Creates HTTP session
        3. Starts the remote component
        4. Runs the main loop
        """
        try:
            # Get or create client ID
            client_id_entity = f"{self.prefix}.client_id"
            self.client_id = self.base.get_state_wrapper(client_id_entity, default=None)

            if not self.client_id:
                # Generate new client ID
                self.client_id = str(uuid.uuid4())
                self.log(f"ComponentClient: Generated new client ID: {self.client_id}")

                # Save client ID
                self.base.set_state_wrapper(
                    client_id_entity,
                    self.client_id,
                    attributes={
                        "created": datetime.now(timezone.utc).isoformat(),
                        "server_url": self.server_url
                    }
                )
            else:
                self.log(f"ComponentClient: Using existing client ID: {self.client_id}")

            # Get callback URL from config
            callback_url = self.base.get_arg("component_client_callback_url", "http://localhost:5054")

            # Create HTTP session
            self.session = aiohttp.ClientSession()

            # Start remote component
            await self._start_remote_component(callback_url)

            # Run main loop (same as ComponentBase.start())
            seconds = 0
            first = True
            while not self.api_stop and not self.fatal_error:
                try:
                    if first or seconds % 60 == 0:
                        if await self.run(seconds, first):
                            if not self.api_started:
                                self.api_started = True
                                self.log(f"ComponentClient ({self.remote_class}): Started")
                        else:
                            self.count_errors += 1
                    first = False
                except Exception as e:
                    self.log(f"Error: ComponentClient ({self.remote_class}): {e}")
                    self.log("Error: " + traceback.format_exc())

                seconds += 5
                await asyncio.sleep(5)

            self.log(f"ComponentClient ({self.remote_class}): Finalizing...")
            await self.final()

            self.api_started = False
            self.log(f"ComponentClient ({self.remote_class}): Stopped")

        except Exception as e:
            self.log(f"Error: ComponentClient.start() failed: {e}")
            self.log("Error: " + traceback.format_exc())
            self.api_started = False

    async def _start_remote_component(self, callback_url):
        """
        Start the component on the remote server.

        Args:
            callback_url: URL for server to call back to
        """
        try:
            self.log(f"ComponentClient: Starting remote component {self.remote_class} on {self.server_url} with callback {callback_url} args {self.component_kwargs}")

            # Prepare request
            request_data = pickle.dumps({
                "client_id": self.client_id,
                "component_class": self.remote_class,
                "callback_url": callback_url,
                "init_kwargs": self.component_kwargs
            }, protocol=4)

            # Send start request
            timeout = aiohttp.ClientTimeout(total=30)
            async with self.session.post(
                f"{self.server_url}/component/start",
                data=request_data,
                timeout=timeout
            ) as resp:
                response_data = await resp.read()
                result = pickle.loads(response_data)

                # Check for error
                if isinstance(result, dict) and "error" in result:
                    error_msg = f"Failed to start remote component: {result['error']}"
                    self.log(f"Error: {error_msg}")
                    raise RuntimeError(error_msg)

                self.component_started = True
                self.log(f"ComponentClient: Remote component {self.remote_class} started successfully")

        except Exception as e:
            self.log(f"Error: Failed to start remote component: {e}")
            self.log("Error: " + traceback.format_exc())
            raise

    async def _call_remote_with_retry(self, method, *args, **kwargs):
        """
        Call a remote method with retry logic.

        Args:
            method: Method name to call
            *args: Positional arguments
            **kwargs: Keyword arguments

        Returns:
            Result from remote call, or False on failure
        """
        start_time = time.time()
        backoff = 1
        timeout_config = self.base.get_arg("component_server_timeout", 1800)

        while True:
            try:
                # Prepare request
                request_data = pickle.dumps({
                    "client_id": self.client_id,
                    "component_class": self.remote_class,
                    "method": method,
                    "args": args,
                    "kwargs": kwargs
                }, protocol=4)

                # Send request
                timeout = aiohttp.ClientTimeout(total=30)
                async with self.session.post(
                    f"{self.server_url}/component/call",
                    data=request_data,
                    timeout=timeout
                ) as resp:
                    response_data = await resp.read()
                    result = pickle.loads(response_data)

                    # Check for "Component not found" error - trigger restart
                    if isinstance(result, dict) and "error" in result:
                        if "Component not found" in result["error"]:
                            self.log(f"Warn: Remote component not found, restarting...")

                            # Use lock to prevent duplicate restarts
                            async with self.restart_lock:
                                # Double-check component is still missing
                                if not self.component_started:
                                    callback_url = self.base.get_arg("component_client_callback_url", "http://localhost:5054")
                                    await self._start_remote_component(callback_url)

                            # Retry the call
                            continue
                        else:
                            # Other error
                            self.log(f"Error: Remote call {method} failed: {result['error']}")
                            return False

                    # Success
                    return result

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                # Check if we've exceeded total timeout
                elapsed = time.time() - start_time
                if elapsed > timeout_config:
                    self.log(f"Error: Remote call {method} timed out after {elapsed}s")
                    return False

                # Log and retry with backoff
                self.log(f"Warn: Remote call {method} failed ({e}), retrying in {backoff}s...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)  # Exponential backoff, max 30s

            except Exception as e:
                self.log(f"Error: Remote call {method} failed: {e}")
                self.log("Error: " + traceback.format_exc())
                return False

    async def run(self, seconds, first):
        """
        Run method called by the main loop.

        Args:
            seconds: Seconds since start
            first: True if first run

        Returns:
            True on success, False on failure
        """
        # Send ping if needed
        poll_interval = self.base.get_arg("component_server_poll_interval", 300)
        if time.time() - self.last_ping_time > poll_interval:
            return await self._send_ping()
        return True

    async def _send_ping(self):
        """Send health check ping to server."""
        try:
            request_data = pickle.dumps({
                "client_id": self.client_id,
                "component_class": self.remote_class
            }, protocol=4)

            timeout = aiohttp.ClientTimeout(total=10)
            async with self.session.post(
                f"{self.server_url}/component/ping",
                data=request_data,
                timeout=timeout
            ) as resp:
                response_data = await resp.read()
                result = pickle.loads(response_data)

                self.last_ping_time = time.time()

                # Log if component is not alive
                if isinstance(result, dict) and not result.get("alive", True):
                    self.log(f"Warn: Remote component reports not alive")
                    return False
                return True

        except Exception as e:
            self.log(f"Warn: Failed to ping server: {e}")
            return False

    async def final(self):
        """
        Final cleanup before stopping.
        """
        try:
            # Send stop request to server
            request_data = pickle.dumps({
                "client_id": self.client_id,
                "component_class": self.remote_class
            }, protocol=4)

            timeout = aiohttp.ClientTimeout(total=10)
            async with self.session.post(
                f"{self.server_url}/component/stop",
                data=request_data,
                timeout=timeout
            ) as resp:
                await resp.read()

        except Exception as e:
            self.log(f"Warn: Failed to stop remote component: {e}")

        # Close session
        if self.session:
            await self.session.close()

    async def stop(self):
        """
        Stop the component gracefully.
        """
        self.api_stop = True
        self.api_started = False
        await asyncio.sleep(0.1)

    @property
    def fatal_error(self):
        """Check if a fatal error has occurred."""
        return self.base.fatal_error

    def is_alive(self):
        """Check if component is alive."""
        return self.api_started

    def last_updated_time(self):
        """Get last updated time."""
        return self.last_success_timestamp

    def update_success_timestamp(self):
        """Update last success timestamp."""
        self.last_success_timestamp = datetime.now(timezone.utc)

    # Event handlers - forward to remote component

    async def select_event(self, entity_id, value):
        """Handle select entity event."""
        await self._call_remote_with_retry("select_event", entity_id, value)

    async def number_event(self, entity_id, value):
        """Handle number entity event."""
        await self._call_remote_with_retry("number_event", entity_id, value)

    async def switch_event(self, entity_id, service):
        """Handle switch entity event."""
        await self._call_remote_with_retry("switch_event", entity_id, service)
