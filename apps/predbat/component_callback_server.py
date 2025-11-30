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
Component Callback Server

This server runs within Predbat and provides a callback endpoint for remote
components to access the base Predbat object's methods.
"""

import asyncio
import pickle
import inspect
import traceback
import aiohttp.web
import time


class ComponentCallbackServer:
    """
    HTTP server that handles callbacks from remote components.
    
    This server provides endpoints for remote components to call back and access
    the main Predbat base object's methods (get_arg, get_state_wrapper, etc.)
    """
    
    def __init__(self, base, port):
        """
        Initialize the callback server.
        
        Args:
            base: The main Predbat base object
            port: Port to bind to
        """
        self.base = base
        self.port = port
        self.started = False
        self.runner = None
        self.site = None
        self.log = self.base.log
        self.stop_api = False
    
    async def start(self):
        """
        Start the callback server.
        """
        print('Here')
        self.log("CallBackServer: Initializing ComponentCallbackServer...")
        try:
            # Create web application
            app = aiohttp.web.Application()

            self.log("Starting ComponentCallbackServer...")
            
            # Add routes
            app.router.add_post('/base/call', self._handle_base_call)
            app.router.add_get('/health', self._handle_health)
            
            self.log("CallBackServer: Setting up ComponentCallbackServer runner...")
            # Create runner
            self.runner = aiohttp.web.AppRunner(app)
            await self.runner.setup()

            self.log(f"CallBackServer: Creating ComponentCallbackServer site port {self.port}...")
            
            # Create site and start
            self.site = aiohttp.web.TCPSite(self.runner, '0.0.0.0', self.port)
            
            try:
                await self.site.start()
            except OSError as e:
                error_msg = f"Failed to bind callback server to port {self.port}: {e}"
                self.log(error_msg)
                raise
            
            self.started = True
            self.log(f"CallBackServer: started on port {self.port}")
            
        except Exception as e:
            self.log(f"CallBackServer: Error: Failed to start callback server: {e}")
            self.log(traceback.format_exc())
            raise

        while not self.stop_api:
            await asyncio.sleep(1)

        if self.runner:
            self.log("CallBackServer: Stopping callback server")
            await self.runner.cleanup()
            self.started = False

    
    async def _handle_base_call(self, request):
        """
        Handle base API call from remote component.
        
        Expected pickled request: {
            "method": str,
            "args": tuple,
            "kwargs": dict
        }
        """
        try:
            # Read and unpickle request
            data = await request.read()
            req = pickle.loads(data)
            
            method = req["method"]
            args = req.get("args", ())
            kwargs = req.get("kwargs", {})
            
            # Invoke method on base object
            try:
                method_fn = getattr(self.base, method)
                result = method_fn(*args, **kwargs)
                
                # Check if result is a coroutine and await it
                if inspect.iscoroutine(result):
                    result = await result
                
                # Return pickled result
                return aiohttp.web.Response(
                    body=pickle.dumps(result, protocol=4)
                )
                
            except Exception as e:
                error_msg = str(e)
                self.log(f"CallBackServer: Error: Base call {method} failed: {error_msg}")
                self.log(traceback.format_exc())
                
                # Return error
                return aiohttp.web.Response(
                    body=pickle.dumps({"error": error_msg}, protocol=4)
                )
                
        except Exception as e:
            error_msg = f"Failed to process base call: {e}"
            self.log(f"CallBackServer: Error: {error_msg}")
            self.log(traceback.format_exc())
            
            return aiohttp.web.Response(
                body=pickle.dumps({"error": error_msg}, protocol=4)
            )
    
    async def _handle_health(self, request):
        """Handle health check request."""
        return aiohttp.web.json_response({"status": "ok"})
    
    def wait_started(self, timeout=5*60):
        """
        Wait for the server to start.
        
        Args:
            timeout: Maximum time to wait in seconds
            
        Raises:
            TimeoutError: If server doesn't start within timeout
        """
        elapsed = 0
        while not self.started and elapsed < timeout:
            time.sleep(1)
            elapsed += 1
        
        if not self.started:
            raise TimeoutError("Callback server failed to start within timeout")
    
    async def stop(self):
        """Stop the callback server."""
        self.stop_api = True
        await asyncio.sleep(0.1)  # Give time for any ongoing requests to finish
