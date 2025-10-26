"""
Model Context Protocol (MCP) Server for Predbat

This module provides an MCP server that integrates with the Predbat web interface
to expose battery prediction data and plan information via the MCP protocol.
"""

import asyncio
import json
import logging
import sys
from typing import Any, Dict, List, Optional, Sequence
from datetime import datetime, timezone
from utils import calc_percent_limit, get_override_time_from_string
import re
from aiohttp import web
import asyncio
import time


class PredbatMCPServer:
    """
    Model Context Protocol (MCP) Server for Predbat
    """

    def __init__(self, enable, mcp_secret, base):
        """Initialize the MCP server component"""
        self.enable = enable
        self.mcp_secret = mcp_secret
        self.base = base
        self.mcp_server = None
        self.log = base.log
        self.api_started = False
        self.abort = False
        self.last_success_timestamp = None

    async def start(self):
        """Start the MCP server if enabled"""
        if self.enable:
            self.mcp_server = create_mcp_server(self.base, self.log)
            await self.mcp_server.start()

            # Now create Web UI on port 8199
            app = web.Application()
            app.router.add_get("/mcp", self.html_mcp_get)
            app.router.add_post("/mcp", self.html_mcp_post)

            runner = web.AppRunner(app)
            await runner.setup()

            site = web.TCPSite(runner, "0.0.0.0", 8199)
            await site.start()

            print("MCP interface started")
            self.api_started = True
            while not self.abort:
                self.last_success_timestamp = datetime.now(timezone.utc)
                await asyncio.sleep(2)
            await runner.cleanup()

            if self.mcp_server:
                self.mcp_server.stop()

            self.api_started = False
            print("MCP interface stopped")

    async def stop(self):
        print("MCP interface stop called")
        self.abort = True
        await asyncio.sleep(1)

    async def stop(self):
        print("Web interface stop called")
        self.abort = True
        await asyncio.sleep(1)

    def wait_api_started(self):
        """
        Wait for the API to start
        """
        self.log("MCP: Waiting for API to start")
        count = 0
        while not self.api_started and count < 240:
            time.sleep(1)
            count += 1
        if not self.api_started:
            self.log("Warn: MCP: Failed to start")
            return False
        return True

    def is_alive(self):
        return self.api_started

    def last_updated_time(self):
        """
        Get the last successful update time
        """
        return self.last_success_timestamp

    async def html_mcp_get(self, request):
        """
        Handle GET requests to MCP endpoint - returns server info and available tools
        """
        if not self.mcp_server:
            return web.json_response({"success": False, "error": "MCP server is not available."}, status=503)

        # Check if mcp_secret is in the header as auth bearer
        mcp_secret = request.headers.get("Authorization")
        if mcp_secret != f"Bearer {self.mcp_secret}":
            return web.json_response({"success": False, "error": "Unauthorized: Invalid MCP secret."}, status=401)

        try:
            result = await self.mcp_server.handle_mcp_request(request, self.mcp_server)
            return web.json_response(result)
        except Exception as e:
            self.log(f"Error in MCP GET endpoint: {e}")
            return web.json_response({"success": False, "error": f"Server error: {str(e)}"}, status=500)

    async def html_mcp_post(self, request):
        """
        Handle POST requests to MCP endpoint - executes tools via JSON-RPC 2.0
        """
        if not self.mcp_server:
            return web.json_response({"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": "MCP server is not available."}}, status=503)

        # Check if mcp_secret is in the header as auth bearer
        mcp_secret = request.headers.get("Authorization")
        if mcp_secret != f"Bearer {self.mcp_secret}":
            return web.json_response({"success": False, "error": "Unauthorized: Invalid MCP secret."}, status=401)

        try:
            result = await self.mcp_server.handle_mcp_request(request, self.mcp_server)
            return web.json_response(result)
        except Exception as e:
            self.log(f"Error in MCP POST endpoint: {e}")
            return web.json_response({"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": f"Server error: {str(e)}"}}, status=500)


class MCPServerWrapper:
    """
    Wrapper class for the MCP Server to provide HTTP-based MCP functionality for web interface
    """

    def __init__(self, base, log_func=None):
        """Initialize the MCP server wrapper"""
        self.base = base
        self.log = log_func or print
        self.is_running = False

        if log_func:
            log_func("Creating HTTP MCP Server with Predbat integration")

    async def start(self):
        """Start the MCP server (web interface compatibility)"""
        self.is_running = True
        if self.log:
            self.log("HTTP MCP Server started (web interface integration)")

    async def stop(self):
        """Stop the MCP server (web interface compatibility)"""
        self.is_running = False
        if self.log:
            self.log("HTTP MCP Server stopped")

    async def handle_request(self, request):
        """
        Handle HTTP MCP requests

        Args:
            request: HTTP request object

        Returns:
            JSON response following MCP protocol
        """
        return await handle_mcp_request(request, self)

    async def _execute_get_plan(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Execute the get_plan tool"""
        try:
            # Check if we have plan data available
            if not hasattr(self.base, "raw_plan") or not self.base.raw_plan:
                return {"success": False, "error": "No plan data available", "data": None}

            # Return the complete plan data
            return {"success": True, "error": None, "data": self.base.raw_plan, "timestamp": datetime.now().isoformat(), "description": "Current Predbat battery plan including forecasts, costs, and operational states"}

        except Exception as e:
            return {"success": False, "error": f"Error retrieving plan data: {str(e)}", "data": None}

    async def _execute_get_entities(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """
        Get current Predbat entities
        """
        try:
            filter = arguments.get("filter", None)
            entities = self.base.dashboard_values
            returned_entities = []
            for entity in entities:
                entity_id = entity.get("entity_id", "")
                if filter:
                    if not re.search(filter, entity_id):
                        continue
                value = {
                    "entity_id": entity.get("entity_id"),
                    "state": entity.get("state"),
                    "friendly_name": entity.get("friendly_name"),
                }
                if "unit_of_measurement" in entity:
                    value["unit_of_measurement"] = entity.get("unit_of_measurement")
                if "device_class" in entity:
                    value["device_class"] = entity.get("device_class")
                if "state_class" in entity:
                    value["state_class"] = entity.get("state_class")
                if "icon" in entity:
                    value["icon"] = entity.get("icon")
                returned_entities.append(value)
            return {"success": True, "error": None, "data": returned_entities, "timestamp": datetime.now().isoformat(), "description": "The current Predbat entities and their states"}

        except Exception as e:
            return {"success": False, "error": f"Error retrieving entities data: {str(e)}", "data": None}

    async def _execute_set_plan_override(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute a plan override request
        """
        try:
            action = arguments.get("action", None)
            time_str = arguments.get("time", None)

            if not action or not time_str:
                return {"success": False, "error": "Missing required parameters", "data": None}

            action = action.lower()
            action = action.replace(" ", "_")

            now_utc = self.base.now_utc
            override_time = get_override_time_from_string(now_utc, time_str)
            if not override_time:
                return {"success": False, "error": "Invalid time format. Use 'Day HH:MM' format e.g. Sat 14:30", "data": None}

            minutes_from_now = (override_time - now_utc).total_seconds() / 60
            if minutes_from_now >= 17 * 60:
                return {"success": False, "error": "Override time must be within 17 hours from now.", "data": None}

            selection_option = "{}".format(override_time.strftime("%H:%M:%S"))
            clear_option = "[{}]".format(override_time.strftime("%H:%M:%S"))
            if action == "clear":
                await self.base.async_manual_select("manual_demand", selection_option)
                await self.base.async_manual_select("manual_demand", clear_option)
            else:
                if action == "demand":
                    await self.base.async_manual_select("manual_demand", selection_option)
                elif action == "charge":
                    await self.base.async_manual_select("manual_charge", selection_option)
                elif action == "export":
                    await self.base.async_manual_select("manual_export", selection_option)
                elif action == "freeze_charge":
                    await self.base.async_manual_select("manual_freeze_charge", selection_option)
                elif action == "freeze_export":
                    await self.base.async_manual_select("manual_freeze_export", selection_option)
                else:
                    return {"success": False, "error": "Unknown action {}".format(action), "data": None}

            # Refresh plan
            self.base.update_pending = True
            self.base.plan_valid = False
        except Exception as e:
            return {"success": False, "error": f"Error applying plan override: {str(e)}", "data": None}

    async def _execute_get_config(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """
        Get full HA configuration for Predbat
        """
        try:
            entity_id_filter = arguments.get("filter", None)
            config_return = []
            for item in self.base.CONFIG_ITEMS:
                if entity_id_filter:
                    entity_id = item.get("entity", None)
                    if entity_id and re.search(entity_id_filter, entity_id):
                        config_return.append(item)
                else:
                    config_return.append(item)
            return {"success": True, "error": None, "data": config_return, "timestamp": datetime.now().isoformat(), "description": "The contents of the Predbat configuration settings"}

        except Exception as e:
            return {"success": False, "error": f"Error retrieving apps.yaml data: {str(e)}", "data": None}

    async def _execute_get_apps(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Execute the get_apps tool"""
        try:
            configuration = self.base.args
            return_configuration = {}
            config_id_filter = arguments.get("filter", None)
            for key, value in configuration.items():
                if config_id_filter:
                    if re.search(config_id_filter, key):
                        return_configuration[key] = value
                else:
                    return_configuration[key] = value

            return {"success": True, "error": None, "data": return_configuration, "timestamp": datetime.now().isoformat(), "description": "The contents of the Predbat apps.yaml configuration"}

        except Exception as e:
            return {"success": False, "error": f"Error retrieving Predbat apps.yaml data: {str(e)}", "data": None}

    async def _execute_get_status(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Execute the get_status tool"""

        try:
            debug_enable, _ = self.base.get_ha_config("debug_enable", None)
            read_only, _ = self.base.get_ha_config("set_read_only", None)
            predbat_mode, _ = self.base.get_ha_config("mode", None)
            num_cars, _ = self.base.get_ha_config("num_cars", None)
            last_updated = self.base.get_state_wrapper("predbat.status", attribute="last_updated", default=None)
            soc_percent = calc_percent_limit(self.base.soc_kw, self.base.soc_max)
            grid_power = self.base.grid_power
            battery_power = self.base.battery_power
            pv_power = self.base.pv_power
            load_power = self.base.load_power
            status_data = {
                "is_running": self.base.is_running(),
                "status": self.base.get_state_wrapper("predbat.status"),
                "current_soc": self.base.soc_kw,
                "soc_max": self.base.soc_max,
                "soc_percent": soc_percent,
                "reserve": self.base.reserve,
                "mode": predbat_mode,
                "num_cars": num_cars,
                "carbon_enable": self.base.carbon_enable,
                "iboost_enable": self.base.iboost_enable,
                "forecast_minutes": self.base.forecast_minutes,
                "debug_enable": debug_enable,
                "read_only": read_only,
                "last_updated": last_updated,
                "grid_power": grid_power,
                "battery_power": battery_power,
                "pv_power": pv_power,
                "load_power": load_power,
            }

            return {"success": True, "error": None, "data": status_data, "timestamp": datetime.now().isoformat()}

        except Exception as e:
            return {"success": False, "error": f"Error retrieving status: {str(e)}", "data": None}

    async def _execute_set_config(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Execute the set_config tool"""
        try:
            entity_id = arguments.get("entity_id")
            value = arguments.get("value")

            if not entity_id or value is None:
                return {"success": False, "error": "Both 'entity_id' and 'value' must be provided", "data": None}

            # Update the configuration setting
            await self.base.ha_interface.set_state_external(entity_id, value)

            return {"success": True, "error": None, "data": {"entity_id": entity_id, "new_value": value}, "timestamp": datetime.now().isoformat(), "description": f"Configuration setting '{entity_id}' updated successfully"}

        except Exception as e:
            return {"success": False, "error": f"Error setting configuration: {str(e)}", "data": None}

    async def handle_mcp_request(self, request, mcp_server):
        """
        Handle HTTP requests implementing the Model Context Protocol over HTTP

        Args:
            request: Web request object
            mcp_server: MCP server instance

        Returns:
            JSON response following MCP/JSON-RPC 2.0 protocol
        """
        try:
            if request.method == "POST":
                # Handle MCP JSON-RPC requests
                try:
                    request_data = await request.json()
                except Exception:
                    return {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}

                # Extract JSON-RPC fields
                jsonrpc = request_data.get("jsonrpc", "2.0")
                request_id = request_data.get("id")
                method = request_data.get("method")
                params = request_data.get("params", {})

                # Route to appropriate handler
                if method == "initialize":
                    result = await self._handle_initialize(params)
                elif method == "tools/list":
                    result = await self._handle_tools_list(params)
                elif method == "tools/call":
                    result = await self._handle_tools_call(params)
                elif method == "notifications/initialized":
                    # This is a notification - return empty success response
                    # Some clients expect a response even for notifications over HTTP
                    return {"jsonrpc": jsonrpc, "id": request_id, "result": {}}
                else:
                    # Method not found
                    return {"jsonrpc": jsonrpc, "id": request_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}

                # Build success response
                return {"jsonrpc": jsonrpc, "id": request_id, "result": result}

            else:
                # For non-POST requests, return error
                return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": f"Invalid Request: {request.method} not supported, use POST"}}

        except Exception as e:
            return {"jsonrpc": "2.0", "id": request_id if "request_id" in locals() else None, "error": {"code": -32603, "message": f"Internal error: {str(e)}"}}

    async def _handle_initialize(self, params):
        """Handle MCP initialize request"""
        tools = await self._handle_tools_list(params)
        return {"protocolVersion": "2024-11-05", "capabilities": {"tools": tools["tools"]}, "serverInfo": {"name": "Predbat MCP Server", "version": "1.0.1"}}

    async def _handle_tools_list(self, params):
        """Handle MCP tools/list request"""
        return {
            "tools": [
                {"name": "get_plan", "description": "Get the current Predbat battery plan data including forecast, costs, and state information", "inputSchema": {"type": "object", "properties": {}, "required": []}},
                {"name": "get_status", "description": "Get the current Predbat system status and configuration", "inputSchema": {"type": "object", "properties": {}, "required": []}},
                {
                    "name": "get_apps",
                    "description": "Get predbat apps.yaml static configuration data",
                    "inputSchema": {"type": "object", "properties": {"filter": {"type": "string", "description": "The configuration item name to filter on, as a Python regex (optional)"}}, "required": []},
                },
                {
                    "name": "get_config",
                    "description": "Get the current Predbat live configuration settings",
                    "inputSchema": {"type": "object", "properties": {"filter": {"type": "string", "description": "The entity ID name to filter on, as a Python regex (optional)"}}, "required": []},
                },
                {
                    "name": "get_entities",
                    "description": "Get the current Predbat entities",
                    "inputSchema": {"type": "object", "properties": {"filter": {"type": "string", "description": "The configuration item name to filter on, as a Python regex (optional)"}}, "required": []},
                },
                {
                    "name": "set_config",
                    "description": "Set Predbat configuration setting",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"entity_id": {"type": "string", "description": "The entity ID of the configuration setting to update"}, "value": {"type": "string", "description": "The new value for the configuration setting"}},
                        "required": ["entity_id", "value"],
                    },
                },
                {
                    "name": "set_plan_override",
                    "description": "Override the current Predbat plan for a specific 30 minute period with a manual action",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "action": {"type": "string", "description": "The action to perform: demand, charge, export, freeze_charge, freeze_export, clear"},
                            "time": {"type": "string", "description": 'The time at which to perform the action, in "Day HH:MM" format (24-hour), covers one 30-minute period'},
                        },
                        "required": ["action", "time"],
                    },
                },
            ]
        }

    async def _handle_tools_call(self, params):
        """Handle MCP tools/call request"""
        tool_name = params.get("name")
        arguments = params.get("arguments", {})

        try:
            if tool_name == "get_plan":
                result = await self._execute_get_plan(arguments)
            elif tool_name == "get_status":
                result = await self._execute_get_status(arguments)
            elif tool_name == "get_apps":
                result = await self._execute_get_apps(arguments)
            elif tool_name == "get_config":
                result = await self._execute_get_config(arguments)
            elif tool_name == "get_entities":
                result = await self._execute_get_entities(arguments)
            elif tool_name == "set_config":
                result = await self._execute_set_config(arguments)
            elif tool_name == "set_plan_override":
                result = await self._execute_set_plan_override(arguments)
            else:
                return {"content": [{"type": "text", "text": json.dumps({"success": False, "error": f"Unknown tool: {tool_name}"})}], "isError": True}

            # Return result in MCP format
            return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]}

        except Exception as e:
            return {"content": [{"type": "text", "text": json.dumps({"success": False, "error": f"Tool execution failed: {str(e)}"})}], "isError": True}


def create_mcp_server(base, log_func=None):
    """
    Factory function to create an HTTP MCP server instance

    Args:
        base: Predbat base instance
        log_func: Optional logging function

    Returns:
        Configured HTTP MCP Server wrapper instance for web interface compatibility
    """
    return MCPServerWrapper(base, log_func)


async def main():
    """
    Main entry point for testing HTTP MCP server
    This is used when running the MCP server directly for testing
    """

    class MockBase:
        """Mock base for testing"""

        def __init__(self):
            self.raw_plan = {"test": "data", "forecast": [{"time": "10:00", "soc": 50, "cost": 0.15}, {"time": "11:00", "soc": 60, "cost": 0.12}], "totals": {"cost_today": 5.50, "profit_today": 2.30}}
            self.soc_kw = 50
            self.soc_max = 100
            self.reserve = 10
            self.mode = "Automatic"
            self.num_cars = 1
            self.carbon_enable = True
            self.iboost_enable = False
            self.forecast_minutes = 1440

        def is_running(self):
            return True

    base = MockBase()

    # Test the HTTP MCP server wrapper
    wrapper = create_mcp_server(base, print)
    await wrapper.start()

    print("HTTP MCP Server wrapper created and started successfully")

    await wrapper.stop()


if __name__ == "__main__":
    asyncio.run(main())
