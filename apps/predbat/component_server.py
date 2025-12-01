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
Component Server for Remote Component Execution

WARNING: This server uses pickle for serialization. Only use in trusted networks
(e.g. Kubernetes cluster) as pickle can execute arbitrary code. Do not expose
this server to untrusted networks or the internet.
"""

import asyncio
import pickle
import logging
import importlib
import traceback
from datetime import datetime, timezone
import aiohttp
import aiohttp.web
import os
class BaseMock:
    """
    Mock base object that forwards all calls back to the client's Predbat instance.
    
    This class mimics the PredBat base object interface but implements it by making
    HTTP callbacks to the client for all operations.
    """
    
    def __init__(self, callback_url, component_name, client_id):
        """
        Initialize BaseMock with callback URL.
        
        Args:
            callback_url: URL of the client's callback server
        """
        self.callback_url = callback_url
        self.session = aiohttp.ClientSession()
        self.fatal_error = False
        self.component_name = component_name
        self.client_id = client_id

    async def initialize(self):
        # Cache immutable attributes on initialization
        self.local_tz = await self._remote_call("get_local_attr", "local_tz")
        self.prefix = await self._remote_call("get_local_attr", "prefix")
        self.args = await self._remote_call("get_local_attr", "args")
        self.currency_symbols = await self._remote_call("get_local_attr", "currency_symbols")
        self.num_cars = await self._remote_call("get_local_attr", "num_cars")
        self.plan_interval_minutes = await self._remote_call("get_local_attr", "plan_interval_minutes")

        # Config root
        self.config_root = self.component_name + "_" + self.client_id
        os.makedirs(self.config_root, exist_ok=True)
    
    async def _remote_call(self, method, *args, **kwargs):
        """
        Make a remote call to the client's base object.
        
        Args:
            method: Method name to call
            *args: Positional arguments
            **kwargs: Keyword arguments
            
        Returns:
            Result from the remote call
        """
        try:
            request_data = pickle.dumps({
                "method": method,
                "args": args,
                "kwargs": kwargs
            }, protocol=4)
            
            # Get or create session for current event loop
            try:
                loop = asyncio.get_running_loop()
                if not hasattr(self, '_loop_sessions'):
                    self._loop_sessions = {}
                if loop not in self._loop_sessions:
                    self._loop_sessions[loop] = aiohttp.ClientSession()
                session = self._loop_sessions[loop]
            except RuntimeError:
                # No running loop, use default session
                session = self.session
            
            async with session.post(
                f"{self.callback_url}/base/call",
                data=request_data,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                response_data = await resp.read()
                result = pickle.loads(response_data)
                
                # Check if error was returned
                if isinstance(result, dict) and "error" in result:
                    raise RuntimeError(f"Remote call failed: {result['error']}")
                
                return result
        except Exception as e:
            logging.error(f"BaseMock._remote_call({method}) failed: {e}")
            raise
    
    # Base API methods - all forward to client
    def log(self, msg):
        logging.info(f"{self.component_name} ({self.client_id}): {msg}")
    
    async def async_get_arg(self, arg, default=None, indirect=True, combine=False, attribute=None, index=None, domain=None, can_override=True, required_unit=None):
        """Get configuration argument from client (async)."""
        return await self._remote_call(
            "get_arg", arg, default=default, indirect=indirect, combine=combine,
            attribute=attribute, index=index, domain=domain, can_override=can_override, required_unit=required_unit
        )


    # Async versions
    async def async_get_arg(self, arg, default=None, indirect=True, combine=False, attribute=None, index=None, domain=None, can_override=True, required_unit=None):
        return await self._remote_call(
            "get_arg", arg, default=default, indirect=indirect, combine=combine,
            attribute=attribute, index=index, domain=domain, can_override=can_override, required_unit=required_unit
        )
    async def async_set_arg(self, arg, value):
        return await self._remote_call("set_arg", arg, value)

    async def async_get_state_wrapper(self, entity_id=None, default=None, attribute=None, refresh=False, required_unit=None):
        return await self._remote_call("get_state_wrapper", entity_id, default=default, attribute=attribute, refresh=refresh, required_unit=required_unit)

    async def async_set_state_wrapper(self, entity_id, state, attributes={}, required_unit=None):
        return await self._remote_call("set_state_wrapper", entity_id, state, attributes=attributes, required_unit=required_unit)

    async def async_get_history_wrapper(self, entity_id, days=30, required=True, tracked=True):
        return await self._remote_call("get_history_wrapper", entity_id, days=days, required=required, tracked=tracked)

    async def async_get_ha_config(self, name, default):
        return await self._remote_call("get_ha_config", name, default)

    async def async_dashboard_item(self, entity, state, attributes, app=None):
        return await self._remote_call("dashboard_item", entity, state, attributes, app=app)

    @property
    async def async_now_utc(self):
        return await self._remote_call("get_local_attr", "now_utc")

    @property
    async def async_midnight_utc(self):
        return await self._remote_call("get_local_attr", "midnight_utc")

    @property
    async def async_minutes_now(self):
        return await self._remote_call("get_local_attr", "minutes_now")

    @property
    async def async_arg_errors(self):
        return await self._remote_call("get_local_attr", "arg_errors")

    # Sync wrappers (for non-async contexts only)
    def _run_async(self, coro):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop, safe to use asyncio.run
            return asyncio.run(coro)
        else:
            # Already in an event loop - must schedule and wait
            # Use a new thread to run asyncio.run to avoid blocking the event loop
            import threading
            import queue
            
            result_queue = queue.Queue()
            exception_queue = queue.Queue()
            
            def run_in_thread():
                try:
                    # Create a new event loop in this thread
                    new_loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(new_loop)
                    try:
                        result = new_loop.run_until_complete(coro)
                        result_queue.put(result)
                    finally:
                        new_loop.close()
                except Exception as e:
                    exception_queue.put(e)
            
            thread = threading.Thread(target=run_in_thread, daemon=True)
            thread.start()
            thread.join(timeout=2*60)  # 2 minute timeout
            
            if not exception_queue.empty():
                raise exception_queue.get()
            
            if not result_queue.empty():
                return result_queue.get()
            
            raise TimeoutError("Async operation timed out after 30 seconds")

    def get_arg(self, *args, **kwargs):
        return self._run_async(self.async_get_arg(*args, **kwargs))

    def set_arg(self, *args, **kwargs):
        return self._run_async(self.async_set_arg(*args, **kwargs))

    def get_state_wrapper(self, *args, **kwargs):
        return self._run_async(self.async_get_state_wrapper(*args, **kwargs))

    def set_state_wrapper(self, *args, **kwargs):
        return self._run_async(self.async_set_state_wrapper(*args, **kwargs))

    def get_history_wrapper(self, *args, **kwargs):
        return self._run_async(self.async_get_history_wrapper(*args, **kwargs))

    def get_ha_config(self, *args, **kwargs):
        return self._run_async(self.async_get_ha_config(*args, **kwargs))

    def dashboard_item(self, *args, **kwargs):
        return self._run_async(self.async_dashboard_item(*args, **kwargs))

    @property
    def now_utc(self):
        return self._run_async(self.async_now_utc)

    @property
    def midnight_utc(self):
        return self._run_async(self.async_midnight_utc)

    @property
    def minutes_now(self):
        return self._run_async(self.async_minutes_now)

    @property
    def arg_errors(self):
        return self._run_async(self.async_arg_errors)
    
    async def cleanup(self):
        """Cleanup session"""
        await self.session.close()
        # Clean up per-loop sessions
        if hasattr(self, '_loop_sessions'):
            for session in self._loop_sessions.values():
                await session.close()
            self._loop_sessions.clear()


class ComponentServer:
    """
    HTTP server that hosts remote components and forwards their base API calls
    back to the client Predbat instance.
    """
    
    def __init__(self, timeout, component_loader):
        """
        Initialize the component server.
        
        Args:
            timeout: Timeout in seconds for component inactivity
            component_classes: Dict mapping class names to class objects
            component_loader: Optional function(class_name) -> class for lazy loading
        """
        self.timeout = timeout
        self.component_loader = component_loader
        self.components = {}  # Key: client_id_component_class, Value: metadata dict
        self.shutdown_flag = False
        self.active_calls = 0
        self.active_calls_lock = asyncio.Lock()
        self.logger = logging.getLogger("ComponentServer")
        self.app = None
        self.timeout_task = None
    
    async def handle_component_start(self, request):
        """
        Handle component start request.
        
        Expected pickled request: {
            "client_id": str,
            "component_class": str,
            "callback_url": str,
            "init_kwargs": dict
        }
        """
        if self.shutdown_flag:
            return aiohttp.web.Response(
                body=pickle.dumps({"error": "Server is shutting down"}, protocol=4)
            )
        
        try:
            # Unpickle request
            data = await request.read()
            req = pickle.loads(data)
            
            client_id = req["client_id"]
            component_class = req["component_class"]
            callback_url = req["callback_url"]
            init_kwargs = req["init_kwargs"]
            component_args = init_kwargs.get("component_args", {})
            instance_key = f"{client_id}_{component_class}"

            if instance_key in self.components:
                self.logger.info(f"Component {instance_key} already started")
                return aiohttp.web.Response(
                    body=pickle.dumps({"success": True}, protocol=4)
                )
            
            self.logger.info(f"Starting component {component_class} for client {client_id} class {component_class} with callback {callback_url} args {init_kwargs}")
            
            # Validate callback URL is reachable
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        f"{callback_url}/health",
                        timeout=aiohttp.ClientTimeout(total=5)
                    ) as resp:
                        if resp.status != 200:
                            raise Exception(f"Health check returned {resp.status}")
            except Exception as e:
                error_msg = f"Callback URL {callback_url} not reachable: {e}"
                error_msg += traceback.format_exc()
                self.logger.error(error_msg)
                return aiohttp.web.Response(
                    body=pickle.dumps({"error": error_msg}, protocol=4)
                )
            
            # Import and instantiate component
            try:
                # Get component class from registry or lazy loader
                cls = self.component_loader(component_class)                
                if not cls:
                    raise ValueError(f"Component class {component_class} not registered")
                self.logger.info(f"Loaded component class {component_class}")
                
                # Create BaseMock
                base_mock = BaseMock(callback_url, component_class, client_id)
                self.logger.info(f"Initializing BaseMock for component {component_class}")
                await base_mock.initialize()
                self.logger.info(f"BaseMock initialized for component {component_class}")
                
                # Instantiate component
                component = cls(base_mock, **component_args)
                self.logger.info(f"Instantiated component {component_class}")
                
                # Store component metadata
                self.components[instance_key] = {
                    "component": component,
                    "base_mock": base_mock,
                    "callback_url": callback_url,
                    "last_ping": datetime.now(timezone.utc),
                    "task": asyncio.create_task(component.start())
                }
                
                self.logger.info(f"Component {instance_key} started successfully")
                component.api_started = True
                
                return aiohttp.web.Response(
                    body=pickle.dumps({"success": True}, protocol=4)
                )
                
            except Exception as e:
                error_msg = f"Failed to start component: {e}"
                self.logger.error(error_msg)
                self.logger.error(traceback.format_exc())
                return aiohttp.web.Response(
                    body=pickle.dumps({"error": error_msg}, protocol=4)
                )
                
        except Exception as e:
            error_msg = f"Failed to process start request: {e}"
            self.logger.error(error_msg)
            self.logger.error(traceback.format_exc())
            return aiohttp.web.Response(
                body=pickle.dumps({"error": error_msg}, protocol=4)
            )
    
    async def handle_component_call(self, request):
        """
        Handle component method call.
        
        Expected pickled request: {
            "client_id": str,
            "component_class": str,
            "method": str,
            "args": tuple,
            "kwargs": dict
        }
        """
        if self.shutdown_flag:
            return aiohttp.web.Response(
                body=pickle.dumps({"error": "Server is shutting down"}, protocol=4)
            )
        
        # Increment active calls counter
        async with self.active_calls_lock:
            self.active_calls += 1
        
        try:
            # Unpickle request
            data = await request.read()
            req = pickle.loads(data)
            
            client_id = req["client_id"]
            component_class = req["component_class"]
            method = req["method"]
            args = req.get("args", ())
            kwargs = req.get("kwargs", {})
            
            instance_key = f"{client_id}_{component_class}"

            self.logger.info(f"ComponentServer: Handling call to {method} for component {instance_key} class {component_class} args {args} kwargs {kwargs}")
            
            # Check if component exists
            if instance_key not in self.components:
                return aiohttp.web.Response(
                    body=pickle.dumps({"error": "Component not found"}, protocol=4)
                )
            
            component_meta = self.components[instance_key]
            component = component_meta["component"]
            
            # Invoke method
            try:
                method_fn = getattr(component, method)
                
                # Check if method is a coroutine function
                if asyncio.iscoroutinefunction(method_fn):
                    result = await method_fn(*args, **kwargs)
                else:
                    result = method_fn(*args, **kwargs)
                
                return aiohttp.web.Response(
                    body=pickle.dumps(result, protocol=4)
                )
                
            except Exception as e:
                error_msg = str(e)
                self.logger.error(f"Component method {method} failed: {error_msg}")
                self.logger.error(traceback.format_exc())
                return aiohttp.web.Response(
                    body=pickle.dumps({"error": error_msg}, protocol=4)
                )
                
        except Exception as e:
            error_msg = f"Failed to process call request: {e}"
            self.logger.error(error_msg)
            self.logger.error(traceback.format_exc())
            return aiohttp.web.Response(
                body=pickle.dumps({"error": error_msg}, protocol=4)
            )
        finally:
            # Decrement active calls counter
            async with self.active_calls_lock:
                self.active_calls -= 1
    
    async def handle_component_ping(self, request):
        """
        Handle component ping (health check).
        
        Expected pickled request: {
            "client_id": str,
            "component_class": str
        }
        """
        try:
            data = await request.read()
            req = pickle.loads(data)
            
            client_id = req["client_id"]
            component_class = req["component_class"]
            instance_key = f"{client_id}_{component_class}"
            
            if instance_key in self.components:
                # Update last ping time
                self.components[instance_key]["last_ping"] = datetime.now(timezone.utc)
                
                # Check if component is alive
                component = self.components[instance_key]["component"]
                alive = component.is_alive()
                
                return aiohttp.web.Response(
                    body=pickle.dumps({"alive": alive}, protocol=4)
                )
            else:
                return aiohttp.web.Response(
                    body=pickle.dumps({"error": "Component not found"}, protocol=4)
                )
                
        except Exception as e:
            error_msg = f"Failed to process ping request: {e}"
            self.logger.error(error_msg)
            return aiohttp.web.Response(
                body=pickle.dumps({"error": error_msg}, protocol=4)
            )
    
    async def handle_component_stop(self, request):
        """
        Handle component stop request.
        
        Expected pickled request: {
            "client_id": str,
            "component_class": str
        }
        """
        try:
            data = await request.read()
            req = pickle.loads(data)
            
            client_id = req["client_id"]
            component_class = req["component_class"]
            instance_key = f"{client_id}_{component_class}"
            
            if instance_key in self.components:
                await self._stop_component(instance_key)
                
                return aiohttp.web.Response(
                    body=pickle.dumps({"success": True}, protocol=4)
                )
            else:
                return aiohttp.web.Response(
                    body=pickle.dumps({"error": "Component not found"}, protocol=4)
                )
                
        except Exception as e:
            error_msg = f"Failed to process stop request: {e}"
            self.logger.error(error_msg)
            return aiohttp.web.Response(
                body=pickle.dumps({"error": error_msg}, protocol=4)
            )
    
    async def _stop_component(self, instance_key):
        """Stop a component and clean up."""
        if instance_key not in self.components:
            return
        
        self.logger.info(f"Stopping component {instance_key}")
        
        component_meta = self.components[instance_key]
        component = component_meta["component"]
        task = component_meta["task"]
        base_mock = component_meta["base_mock"]
        
        # Stop component
        try:
            await component.stop()
        except Exception as e:
            self.logger.error(f"Error stopping component: {e}")
        
        # Cancel task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        
        # Cleanup base mock
        try:
            await base_mock.cleanup()
        except Exception as e:
            self.logger.error(f"Error cleaning up base mock: {e}")
        
        # Remove from dict
        del self.components[instance_key]
        
        self.logger.info(f"Component {instance_key} stopped")
    
    async def _timeout_checker(self):
        """Background task to check for timed out components."""
        self.logger.info("Timeout checker started")
        
        while not self.shutdown_flag:
            try:
                await asyncio.sleep(60)  # Check every 60 seconds
                
                now = datetime.now(timezone.utc)
                timed_out = []
                
                for instance_key, meta in self.components.items():
                    last_ping = meta["last_ping"]
                    elapsed = (now - last_ping).total_seconds()
                    
                    if elapsed > self.timeout:
                        timed_out.append(instance_key)
                        self.logger.warning(f"Component {instance_key} timed out (no ping for {elapsed}s)")
                
                # Stop timed out components
                for instance_key in timed_out:
                    await self._stop_component(instance_key)
                    
            except Exception as e:
                self.logger.error(f"Error in timeout checker: {e}")
                self.logger.error(traceback.format_exc())
        
        self.logger.info("Timeout checker stopped")
    
    async def shutdown(self):
        """Graceful shutdown of the server."""
        self.logger.info("Shutdown initiated")
        
        # Set shutdown flag to reject new calls
        self.shutdown_flag = True
        
        # Wait for active calls to complete (max 30 seconds)
        wait_time = 0
        while wait_time < 30:
            async with self.active_calls_lock:
                if self.active_calls == 0:
                    break
            
            await asyncio.sleep(1)
            wait_time += 1
        
        if self.active_calls > 0:
            self.logger.warning(f"Forcing shutdown with {self.active_calls} active calls")
        
        # Stop all components
        instance_keys = list(self.components.keys())
        for instance_key in instance_keys:
            await self._stop_component(instance_key)
        
        # Cancel timeout checker
        if self.timeout_task and not self.timeout_task.done():
            self.timeout_task.cancel()
            try:
                await self.timeout_task
            except asyncio.CancelledError:
                pass
        
        self.logger.info("Shutdown complete")
    
    async def run(self, host, port):
        """
        Run the component server.
        
        Args:
            host: Host to bind to
            port: Port to bind to
        """
        # Create web application
        self.app = aiohttp.web.Application()
        
        # Add routes
        self.app.router.add_post('/component/start', self.handle_component_start)
        self.app.router.add_post('/component/call', self.handle_component_call)
        self.app.router.add_post('/component/ping', self.handle_component_ping)
        self.app.router.add_post('/component/stop', self.handle_component_stop)
        
        # Start timeout checker
        self.timeout_task = asyncio.create_task(self._timeout_checker())
        
        self.logger.info(f"Component server starting on {host}:{port}")
        
        # Run web server
        await aiohttp.web._run_app(self.app, host=host, port=port)
