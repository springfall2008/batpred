# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
#
# This code creates a web server and serves up the Predbat web pages

from aiohttp import web
import asyncio
import os
import re
from datetime import datetime, timedelta
import json
import shutil
import html as html_module
import time
from web_helper import get_header_html, get_plan_css, get_editor_js, get_editor_css, get_log_css, get_charts_css, get_apps_css, get_html_config_css, get_apps_js, get_components_css, get_logfile_js

from utils import calc_percent_limit, str2time, dp0, dp2
from config import TIME_FORMAT, TIME_FORMAT_DAILY
from predbat import THIS_VERSION
import urllib.parse

DAY_OF_WEEK_MAP = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
ROOT_YAML_KEY = "pred_bat"


class WebInterface:
    def __init__(self, web_port, base) -> None:
        self.abort = False
        self.base = base
        self.log = base.log
        self.default_page = "./dash"
        self.pv_power_hist = {}
        self.pv_forecast_hist = {}
        self.cost_today_hist = {}
        self.compare_hist = {}
        self.cost_yesterday_hist = {}
        self.cost_yesterday_car_hist = {}
        self.cost_yesterday_no_car = {}
        self.web_port = web_port
        self.default_log = "warnings"
        self.api_started = False

        # Plugin registration system
        self.registered_endpoints = []

    async def select_event(self, entity_id, value):
        pass

    async def number_event(self, entity_id, value):
        pass

    async def switch_event(self, entity_id, service):
        pass

    def register_endpoint(self, path, handler, method="GET"):
        """
        Register a new endpoint with the web interface

        Args:
            path (str): URL path for the endpoint (e.g., '/metrics')
            handler (callable): Async handler function
            method (str): HTTP method ('GET', 'POST', etc.)
        """
        self.registered_endpoints.append({"path": path, "handler": handler, "method": method.upper()})
        self.log(f"Registered endpoint: {method.upper()} {path}")

    def subtract_daily(self, hist1, hist2):
        """
        Subtract the values in hist2 from hist1
        """
        results = {}
        for key in hist1:
            if key in hist2:
                results[key] = hist1[key] - hist2[key]
            else:
                results[key] = hist1[key]
        return results

    def history_update(self):
        """
        Update the history data
        """
        self.log("Web interface history update")
        self.pv_power_hist = self.base.history_attribute(self.base.get_history_wrapper(self.base.prefix + ".pv_power", 7, required=False))
        self.pv_forecast_hist = self.base.history_attribute(self.base.get_history_wrapper("sensor." + self.base.prefix + "_pv_forecast_h0", 7, required=False))
        self.pv_forecast_histCL = self.base.history_attribute(self.base.get_history_wrapper("sensor." + self.base.prefix + "_pv_forecast_h0", 7, required=False), attributes=True, state_key="nowCL")
        self.cost_today_hist = self.base.history_attribute(self.base.get_history_wrapper(self.base.prefix + ".ppkwh_today", 2, required=False))
        self.cost_hour_hist = self.base.history_attribute(self.base.get_history_wrapper(self.base.prefix + ".ppkwh_hour", 2, required=False))
        self.cost_yesterday_hist = self.base.history_attribute(self.base.get_history_wrapper(self.base.prefix + ".cost_yesterday", 28, required=False), daily=True, offset_days=-1, pounds=True)

        if self.base.num_cars > 0:
            self.cost_yesterday_car_hist = self.base.history_attribute(self.base.get_history_wrapper(self.base.prefix + ".cost_yesterday_car", 28, required=False), daily=True, offset_days=-1, pounds=True)
            self.cost_yesterday_no_car = self.subtract_daily(self.cost_yesterday_hist, self.cost_yesterday_car_hist)
        else:
            self.cost_yesterday_no_car = self.cost_yesterday_hist

        compare_list = self.base.get_arg("compare_list", [])
        for item in compare_list:
            id = item.get("id", None)
            if id and self.base.comparison:
                self.compare_hist[id] = {}
                result = self.base.comparison.get_comparison(id)
                if result:
                    self.compare_hist[id]["cost"] = self.base.history_attribute(self.base.get_history_wrapper(result["entity_id"], 28), daily=True, pounds=True)
                    self.compare_hist[id]["metric"] = self.base.history_attribute(self.base.get_history_wrapper(result["entity_id"], 28), state_key="metric", attributes=True, daily=True, pounds=True)

    async def start(self):
        # Start the web server
        app = web.Application()
        app.router.add_get("/", self.html_default)
        app.router.add_get("/plan", self.html_plan)
        app.router.add_get("/log", self.html_log)
        app.router.add_get("/apps", self.html_apps)
        app.router.add_post("/apps", self.html_apps_post)
        app.router.add_get("/charts", self.html_charts)
        app.router.add_get("/config", self.html_config)
        app.router.add_get("/entity", self.html_entity)
        app.router.add_post("/config", self.html_config_post)
        app.router.add_get("/dash", self.html_dash)
        app.router.add_post("/dash", self.html_dash_post)
        app.router.add_get("/components", self.html_components)
        app.router.add_get("/debug_yaml", self.html_debug_yaml)
        app.router.add_get("/debug_log", self.html_debug_log)
        app.router.add_get("/debug_apps", self.html_debug_apps)
        app.router.add_get("/debug_plan", self.html_debug_plan)
        app.router.add_get("/compare", self.html_compare)
        app.router.add_post("/compare", self.html_compare_post)
        app.router.add_get("/apps_editor", self.html_apps_editor)
        app.router.add_post("/apps_editor", self.html_apps_editor_post)
        app.router.add_post("/plan_override", self.html_plan_override)
        app.router.add_post("/rate_override", self.html_rate_override)
        app.router.add_post("/restart", self.html_restart)
        app.router.add_get("/api/state", self.html_api_get_state)
        app.router.add_get("/api/ping", self.html_api_ping)
        app.router.add_post("/api/state", self.html_api_post_state)
        app.router.add_post("/api/service", self.html_api_post_service)
        app.router.add_get("/api/log", self.html_api_get_log)
        app.router.add_post("/api/login", self.html_api_login)

        # Notify plugin system that web interface is ready
        if hasattr(self.base, "plugin_system") and self.base.plugin_system:
            self.base.plugin_system.call_hooks("on_web_start")

        # Register any dynamically registered endpoints
        for endpoint in self.registered_endpoints:
            if endpoint["method"] == "GET":
                app.router.add_get(endpoint["path"], endpoint["handler"])
            elif endpoint["method"] == "POST":
                app.router.add_post(endpoint["path"], endpoint["handler"])
            # Add more methods as needed
            self.log(f"Added registered endpoint: {endpoint['method']} {endpoint['path']}")

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.web_port)
        await site.start()
        print("Web interface started")
        self.api_started = True
        while not self.abort:
            await asyncio.sleep(1)
        await runner.cleanup()
        self.api_started = False
        print("Web interface stopped")

    async def stop(self):
        print("Web interface stop called")
        self.abort = True
        await asyncio.sleep(1)

    def wait_api_started(self):
        """
        Wait for the API to start
        """
        self.log("Web: Waiting for API to start")
        count = 0
        while not self.api_started and count < 240:
            time.sleep(1)
            count += 1
        if not self.api_started:
            self.log("Warn: Web: Failed to start")
            return False
        return True

    def is_alive(self):
        return self.api_started

    def get_attributes_html(self, entity, from_db=False):
        """
        Return the attributes of an entity as HTML
        """
        text = ""
        attributes = {}
        if from_db:
            history = self.base.get_history_wrapper(entity, 1, required=False)
            if history and len(history) >= 1:
                history = history[0]
                if history:
                    attributes = history[0].get("attributes", {})
        else:
            attributes = self.base.dashboard_values.get(entity, {}).get("attributes", {})
        if not attributes:
            return ""
        text += "<table>"
        for key in attributes:
            if key in ["icon", "device_class", "state_class", "unit_of_measurement", "friendly_name"]:
                continue
            value = attributes[key]
            if len(str(value)) > 1024:
                value = "(large data)"
            text += "<tr><td>{}</td><td>{}</td></tr>".format(key, value)
        text += "</table>"
        return text

    def icon2html(self, icon):
        if icon:
            icon = '<span class="mdi mdi-{}"></span>'.format(icon.replace("mdi:", ""))
        return icon

    def get_power_flow_diagram(self):
        """
        Generate a graphical power flow diagram showing energy movement between grid, battery, PV, and house load

        Each component (grid, battery, PV, House) will be represented with a circle
        arrows will run between the PV and Load, the Battery and Load and the House and Grid
        The energy the house consumes is called the Load Power
        The energy the PV generates is called the PV Power
        The energy the battery charges or discharges is called the Battery Power
        The energy the grid imports or exports is called the Grid Power

        The house will be a circle in the middle the PV will be top left, the battery bottom left and the grid bottom right
        """
        # Get power values
        grid_power = self.base.grid_power
        battery_power = self.base.battery_power
        pv_power = self.base.pv_power
        load_power = self.base.load_power

        # Determine flow directions
        grid_importing = grid_power <= -10  # Grid is importing power (negative value)
        grid_exporting = grid_power >= 10  # Grid is exporting power (positive value)

        battery_charging = battery_power >= 10  # Battery is charging (positive value)
        battery_discharging = battery_power <= -10  # Battery is discharging (negative value)

        pv_generating = pv_power > 0  # PV is generating power
        html = ""

        html += """
        <div style="text-align: left; margin: 0px;">
            <svg width="600" height="400" viewBox="0 0 600 400" xmlns="http://www.w3.org/2000/svg">

                <!-- Grid Circle -->
                <circle cx="450" cy="300" r="50" fill="#4CAF50" />
                <text x="450" y="300" text-anchor="middle" dy=".3em" fill="#fff">Grid</text>

                <!-- Battery Circle -->
                <circle cx="150" cy="300" r="50" fill="#FF9800" />
                <text x="150" y="300" text-anchor="middle" dy=".3em" fill="#fff">Battery</text>

                <!-- PV Circle -->
                <circle cx="150" cy="100" r="50" fill="#2196F3" />
                <text x="150" y="100" text-anchor="middle" dy=".3em" fill="#fff">PV</text>

                <!-- House Circle -->
                <circle cx="300" cy="200" r="50" fill="#9C27B0" />
                <text x="300" y="190" text-anchor="middle" dy=".3em" fill="#fff">House</text>
                <text x="300" y="215" text-anchor="middle" dy=".3em" fill="#fff">{} W</text>

                <!-- Define animation paths -->
                <defs>
                    <!-- PV to House path -->
                    <path id="pv-house-path" d="M200,100 L250,150" stroke="transparent" fill="none" />
                    <!-- House to PV path -->
                    <path id="house-pv-path" d="M250,150 L200,100" stroke="transparent" fill="none" />
                    <!-- Battery to House path -->
                    <path id="battery-house-path" d="M200,300 L250,250" stroke="transparent" fill="none" />
                    <!-- House to Battery path -->
                    <path id="house-battery-path" d="M265,235 L215,275" stroke="transparent" fill="none" />
                    <!-- Grid to House path -->
                    <path id="grid-house-path" d="M410,290 L355,240" stroke="transparent" fill="none" />
                    <!-- House to Grid path -->
                    <path id="house-grid-path" d="M340,230 L390,270" stroke="transparent" fill="none" />
                </defs>
        """.format(
            dp0(load_power)
        )
        # Draw arrows and labels
        if pv_generating:
            # Calculate animation speed based on power flow - faster for higher power
            pv_speed = max(0.5, min(3.0, 2.0 - (abs(pv_power) / 3000)))

            html += """
                <!-- PV to House Arrow -->
                <line x1="200" y1="100" x2="250" y2="150" stroke="#2196F3" stroke-width="2" marker-end="url(#pv-arrow)" />
                <text x="250" y="120" text-anchor="middle" fill="#2196F3">{} W</text>

                <!-- Moving dots for PV to House -->
                <circle r="4" fill="#2196F3" opacity="0.8">
                    <animateMotion dur="{}s" repeatCount="indefinite" path="M200,100 L250,150" />
                </circle>
                <circle r="3" fill="#2196F3" opacity="0.6">
                    <animateMotion dur="{}s" repeatCount="indefinite" begin="0.5s" path="M200,100 L250,150" />
                </circle>
                <circle r="2" fill="#2196F3" opacity="0.4">
                    <animateMotion dur="{}s" repeatCount="indefinite" begin="1.0s" path="M200,100 L250,150" />
                </circle>
            """.format(
                dp0(pv_power), pv_speed, pv_speed, pv_speed
            )
        else:
            # Make the PV to House line dashed if not generating
            html += """
                <!-- PV to House Arrow (dashed) -->
                <line x1="200" y1="100" x2="250" y2="150" stroke="#2196F3" stroke-width="2" stroke-dasharray="5,5" marker-end="url(#pv-arrow)" />
                <text x="250" y="120" text-anchor="middle" fill="#2196F3">{} W</text>
                <!-- No moving dot when PV is not generating -->
            """.format(
                dp0(pv_power)
            )
        if battery_charging:
            # Calculate animation speed based on power flow - faster for higher power
            battery_speed = max(0.5, min(3.0, 2.0 - (abs(battery_power) / 3000)))

            html += """
                <!-- Battery to House Arrow -->
                <line x1="200" y1="300" x2="250" y2="250" stroke="#FF9800" stroke-width="2" marker-end="url(#battery-arrow)" />
                <text x="260" y="280" text-anchor="middle" fill="#FF9800">{} W</text>

                <!-- Moving dots for Battery to House -->
                <circle r="4" fill="#FF9800" opacity="0.8">
                    <animateMotion dur="{}s" repeatCount="indefinite" path="M200,300 L250,250" />
                </circle>
                <circle r="3" fill="#FF9800" opacity="0.6">
                    <animateMotion dur="{}s" repeatCount="indefinite" begin="0.5s" path="M200,300 L250,250" />
                </circle>
                <circle r="2" fill="#FF9800" opacity="0.4">
                    <animateMotion dur="{}s" repeatCount="indefinite" begin="1.0s" path="M200,300 L250,250" />
                </circle>
            """.format(
                dp0(battery_power), battery_speed, battery_speed, battery_speed
            )
        else:
            # Calculate animation speed based on power flow - faster for higher power
            battery_speed = max(0.5, min(3.0, 2.0 - (abs(battery_power) / 3000)))

            html += """
                <!-- House to Battery Arrow -->
                <line x1="265" y1="235" x2="215" y2="275" stroke="#FF9800" stroke-width="2" marker-end="url(#battery-arrow)" />
                <text x="260" y="280" text-anchor="middle" fill="#FF9800">{} W</text>

                <!-- Moving dots for House to Battery -->
                <circle r="4" fill="#FF9800" opacity="0.8">
                    <animateMotion dur="{}s" repeatCount="indefinite" path="M265,235 L215,275" />
                </circle>
                <circle r="3" fill="#FF9800" opacity="0.6">
                    <animateMotion dur="{}s" repeatCount="indefinite" begin="0.5s" path="M265,235 L215,275" />
                </circle>
                <circle r="2" fill="#FF9800" opacity="0.4">
                    <animateMotion dur="{}s" repeatCount="indefinite" begin="1.0s" path="M265,235 L215,275" />
                </circle>
            """.format(
                dp0(battery_power), battery_speed, battery_speed, battery_speed
            )

        if grid_importing:
            # Calculate animation speed based on power flow - faster for higher power
            grid_speed = max(0.5, min(3.0, 2.0 - (abs(grid_power) / 3000)))

            html += """
                <!-- Grid to House Arrow -->
                <line x1="410" y1="290" x2="355" y2="240" stroke="#4CAF50" stroke-width="2" marker-end="url(#grid-arrow)" />
                <text x="350" y="280" text-anchor="middle" fill="#4CAF50">{} W</text>

                <!-- Moving dots for Grid to House -->
                <circle r="4" fill="#4CAF50" opacity="0.8">
                    <animateMotion dur="{}s" repeatCount="indefinite" path="M410,290 L355,240" />
                </circle>
                <circle r="3" fill="#4CAF50" opacity="0.6">
                    <animateMotion dur="{}s" repeatCount="indefinite" begin="0.5s" path="M410,290 L355,240" />
                </circle>
                <circle r="2" fill="#4CAF50" opacity="0.4">
                    <animateMotion dur="{}s" repeatCount="indefinite" begin="1.0s" path="M410,290 L355,240" />
                </circle>
            """.format(
                dp0(grid_power), grid_speed, grid_speed, grid_speed
            )
        else:
            # Calculate animation speed based on power flow - faster for higher power
            grid_speed = max(0.5, min(3.0, 2.0 - (abs(grid_power) / 3000)))

            html += """
                <!-- House to Grid Arrow -->
                <line x1="340" y1="230" x2="390" y2="270" stroke="#4CAF50" stroke-width="2" marker-end="url(#grid-arrow)" />
                <text x="340" y="280" text-anchor="middle" fill="#4CAF50">{} W</text>

                <!-- Moving dots for House to Grid -->
                <circle r="4" fill="#4CAF50" opacity="0.8">
                    <animateMotion dur="{}s" repeatCount="indefinite" path="M340,230 L390,270" />
                </circle>
                <circle r="3" fill="#4CAF50" opacity="0.6">
                    <animateMotion dur="{}s" repeatCount="indefinite" begin="0.5s" path="M340,230 L390,270" />
                </circle>
                <circle r="2" fill="#4CAF50" opacity="0.4">
                    <animateMotion dur="{}s" repeatCount="indefinite" begin="1.0s" path="M340,230 L390,270" />
                </circle>
            """.format(
                dp0(grid_power), grid_speed, grid_speed, grid_speed
            )
        html += """
                <!-- Arrowhead Marker -->
                <defs>
                    <marker id="pv-arrow" markerWidth="10" markerHeight="7" refX="0" refY="3.5" orient="auto">
                    <polygon points="0 0, 10 3.5, 0 7" fill="#2196F3"/>
                    </marker>
                    <marker id="battery-arrow" markerWidth="10" markerHeight="7" refX="0" refY="3.5" orient="auto">
                    <polygon points="0 0, 10 3.5, 0 7" fill="#FF9800"/>
                    </marker>
                    <marker id="grid-arrow" markerWidth="10" markerHeight="7" refX="0" refY="3.5" orient="auto">
                    <polygon points="0 0, 10 3.5, 0 7" fill="#4CAF50"/>
                    </marker>
                </defs>
            </svg>
        </div>

        <script>
        // Ensure the animations work correctly in both light and dark modes
        function adjustFlowAnimations() {
            const isDarkMode = document.body.classList.contains('dark-mode');

            // Could add additional animation adjustments here if needed for dark mode
            // This function is called when the page loads and can be extended for other special effects
        }

        // Run when page loads
        adjustFlowAnimations();
        </script>
        """

        return html

    def get_status_html(self, status, version):
        text = ""
        if not self.base.dashboard_index:
            text += "<h2>Loading please wait...</h2>"
            return text

        debug_enable, ignore = self.base.get_ha_config("debug_enable", None)
        read_only, ignore = self.base.get_ha_config("set_read_only", None)
        mode, ignore = self.base.get_ha_config("mode", None)

        # Create a two-column layout for Status and Debug tables
        text += '<div style="display: flex; gap: 5px; margin-bottom: 20px; max-width: 800px;">\n'

        # Left column - Status table
        text += '<div style="flex: 1;">\n'
        text += "<h2>Status</h2>\n"
        text += "<table>\n"

        try:
            is_running = self.base.is_running()
        except Exception as e:
            self.log("Error checking if Predbat is running: {}".format(e))
            is_running = False

        last_updated = self.base.get_state_wrapper("predbat.status", attribute="last_updated", default=None)
        if status and (("Warn:" in status) or ("Error:" in status)):
            text += "<tr><td>Status</td><td bgcolor=#ff7777>{}</td></tr>\n".format(status)
        elif not is_running:
            text += "<tr><td colspan='2' bgcolor='#ff7777'>{} (with errors)</td></tr>\n".format(status)
        else:
            text += "<tr><td>Status</td><td>{}</td></tr>\n".format(status)
        text += "<tr><td>Last Updated</td><td>{}</td></tr>\n".format(last_updated)
        text += "<tr><td>Version</td><td>{}</td></tr>\n".format(version)

        # Editable Mode field
        text += "<tr><td>Mode</td><td>"
        text += f'<form style="display: inline;" method="post" action="./dash">'
        text += f'<select name="mode" class="dashboard-select" onchange="this.form.submit()">'
        for option in self.base.config_index.get("mode", {}).get("options", []):
            selected = "selected" if option == mode else ""
            text += f'<option value="{option}" {selected}>{option}</option>'
        text += "</select></form></td></tr>\n"

        text += "<tr><td>SOC</td><td>{}</td></tr>\n".format(self.get_battery_status_icon())

        # Editable Debug Enable field
        text += "<tr><td>Debug Enable</td><td>"
        text += f'<form style="display: inline;" method="post" action="./dash">'
        toggle_class = "toggle-switch active" if debug_enable else "toggle-switch"
        text += f'<button class="{toggle_class}" type="button" onclick="toggleSwitch(this, \'debug_enable\')"></button>'
        text += "</form></td></tr>\n"

        # Editable Set Read Only field
        text += "<tr><td>Set Read Only</td><td>"
        text += f'<form style="display: inline;" method="post" action="./dash">'
        toggle_class = "toggle-switch active" if read_only else "toggle-switch"
        text += f'<button class="{toggle_class}" type="button" onclick="toggleSwitch(this, \'set_read_only\')"></button>'
        text += "</form></td></tr>\n"
        if self.base.arg_errors:
            count_errors = len(self.base.arg_errors)
            text += "<tr><td>Config</td><td bgcolor=#ff7777>apps.yaml has {} errors</td></tr>\n".format(count_errors)
        else:
            text += "<tr><td>Config</td><td>OK</td></tr>\n"
        text += "</table>\n"
        text += "</div>\n"

        # Right column - Debug table
        text += '<div style="flex: 1;">\n'
        text += "<h2>Debug</h2>\n"
        text += "<table>\n"
        text += "<tr><td>Download</td><td><a href='./debug_apps'>apps.yaml</a></td></tr>\n"
        text += "<tr><td>Create</td><td><a href='./debug_yaml'>predbat_debug.yaml</a></td></tr>\n"
        text += "<tr><td>Download</td><td><a href='./debug_log'>predbat.log</a></td></tr>\n"
        text += "<tr><td>Download</td><td><a href='./debug_plan'>predbat_plan.html</a></td></tr>\n"
        text += "<tr><td>Restart</td><td><button onclick='restartPredbat()' style='background-color: #ff4444; color: white; border: none; padding: 8px 16px; border-radius: 4px; cursor: pointer; font-weight: bold;'>Restart Predbat</button></td></tr>\n"
        text += "</table>\n"
        text += "</div>\n"

        # Close the two-column layout
        text += "</div>\n"

        # Add power flow diagram
        text += "<h2>Power Flow</h2>\n"
        text += self.get_power_flow_diagram()

        # Text description of the plan
        text += "<h2>Plan textual description</h2>\n"
        text += "<table>\n"
        text += "<tr><td>{}</td></tr>\n".format(self.base.text_plan)
        text += "</table>\n"

        # Form the app list
        app_list = ["predbat"]
        for entity_id in self.base.dashboard_index_app.keys():
            app = self.base.dashboard_index_app[entity_id]
            if app not in app_list:
                app_list.append(app)

        # Display per app
        for app in app_list:
            text += "<h2>{} Entities</h2>\n".format(app[0].upper() + app[1:])
            text += "<table>\n"
            text += "<tr><th></th><th>Name</th><th>Entity</th><th>State</th><th>Attributes</th></tr>\n"
            if app == "predbat":
                entity_list = self.base.dashboard_index
            else:
                entity_list = []
                for entity_id in self.base.dashboard_index_app.keys():
                    if self.base.dashboard_index_app[entity_id] == app:
                        entity_list.append(entity_id)

            for entity in entity_list:
                text += self.html_get_entity_text(entity)
            text += "</table>\n"

        return text

    def html_get_entity_text(self, entity):
        text = ""
        if entity in self.base.dashboard_values:
            state = self.base.dashboard_values.get(entity, {}).get("state", None)
            attributes = self.base.dashboard_values.get(entity, {}).get("attributes", {})
            unit_of_measurement = attributes.get("unit_of_measurement", "")
            icon = self.icon2html(attributes.get("icon", ""))
            if unit_of_measurement is None:
                unit_of_measurement = ""
            friendly_name = attributes.get("friendly_name", "")
            if state is None:
                state = "None"
            text += '<tr><td> {} </td><td> <a href="./entity?entity_id={}"> {} </a></td><td>{}</td><td>{} {}</td><td>{}</td></tr>\n'.format(icon, entity, friendly_name, entity, state, unit_of_measurement, self.get_attributes_html(entity))
        else:
            state = self.base.get_state_wrapper(entity_id=entity)
            unit_of_measurement = self.base.get_state_wrapper(entity_id=entity, attribute="unit_of_measurement")
            friendly_name = self.base.get_state_wrapper(entity_id=entity, attribute="friendly_name")
            text += '<tr><td> {} </td><td> <a href="./entity?entity_id={}"> {} </a></td><td>{}</td><td>{} {}</td><td>{}</td></tr>\n'.format("", entity, friendly_name, entity, state, unit_of_measurement, self.get_attributes_html(entity, from_db=True))
        return text

    async def html_entity(self, request):
        """
        Return the Predbat entity as an HTML page
        """
        entity = request.query.get("entity_id", "")
        days = int(request.query.get("days", 7))  # Default to 7 days if not specified

        text = self.get_header("Predbat Entity", refresh=60)

        # Include a back button to return the previous page
        text += """<div style="margin-bottom: 15px;">
            <a href="{}" class="button" style="display: inline-block; padding: 8px 15px; background-color: #4CAF50; color: white; text-decoration: none; border-radius: 4px; font-weight: bold;">
                <span class="mdi mdi-arrow-left" style="margin-right: 5px;"></span>Back
            </a>
        </div>""".format(
            self.default_page
        )

        attributes = self.base.dashboard_values.get(entity, {}).get("attributes", {})
        unit_of_measurement = attributes.get("unit_of_measurement", "")
        friendly_name = attributes.get("friendly_name", "")

        # Add entity dropdown selector
        text += """<div style="margin-bottom: 20px;">
            <form id="entitySelectForm" style="display: flex; align-items: center;">
                <label for="entitySelect" style="margin-right: 10px; font-weight: bold;">Select Entity: </label>
                <select id="entitySelect" name="entity_id" style="padding: 8px; border-radius: 4px; border: 1px solid #ddd; flex-grow: 1; max-width: 500px;" onchange="document.getElementById('entitySelectForm').submit();">
        """

        # Add app list entries to dropdown
        app_list = ["predbat"]
        for entity_id in self.base.dashboard_index_app.keys():
            app = self.base.dashboard_index_app[entity_id]
            if app not in app_list:
                app_list.append(app)

        if not entity:
            text += f'<optgroup label="Not selected">\n'
            text += f'<option value="" selected></option>\n'
            text += "</optgroup>\n"
        elif entity not in self.base.dashboard_values:
            text += f'<optgroup label="Selected">\n'
            text += f'<option value="{entity}" selected>{entity}</option>\n'
            text += "</optgroup>\n"

        # Group entities by app in the dropdown
        for app in app_list:
            text += f'<optgroup label="{app[0].upper() + app[1:]} Entities">\n'

            if app == "predbat":
                entity_list = self.base.dashboard_index
            else:
                entity_list = []
                for entity_id in self.base.dashboard_index_app.keys():
                    if self.base.dashboard_index_app[entity_id] == app:
                        entity_list.append(entity_id)

            for entity_id in entity_list:
                entity_friendly_name = self.base.dashboard_values.get(entity_id, {}).get("attributes", {}).get("friendly_name", entity_id)
                selected = "selected" if entity_id == entity else ""
                text += f'<option value="{entity_id}" {selected}>{entity_friendly_name} ({entity_id})</option>\n'

            text += "</optgroup>\n"

        text += f'<optgroup label="Config Settings">\n'
        for item in self.base.CONFIG_ITEMS:
            if self.base.user_config_item_enabled(item):
                entity_id = item.get("entity", "")
                entity_friendly_name = item.get("friendly_name", "")
                if entity_id:
                    selected = "selected" if entity_id == entity else ""
                    text += f'<option value="{entity_id}" {selected}>{entity_friendly_name} ({entity_id})</option>\n'

        text += "</optgroup>\n"
        text += """
                </select>
                <input type="hidden" name="days" value="{}" />
            </form>
        </div>""".format(
            days
        )

        # Add days selector
        text += """<div style="margin-bottom: 20px;">
            <form id="daysSelectForm" style="display: flex; align-items: center;">
                <label for="daysSelect" style="margin-right: 10px; font-weight: bold;">History Days: </label>
                <select id="daysSelect" name="days" style="padding: 8px; border-radius: 4px; border: 1px solid #ddd;" onchange="document.getElementById('daysSelectForm').submit();">
        """

        # Add days options
        for option in [1, 2, 3, 4, 5, 7, 10, 14, 21, 30, 60, 90]:
            selected = "selected" if option == days else ""
            text += f'<option value="{option}" {selected}>{option} days</option>'

        text += """
                </select>
                <input type="hidden" name="entity_id" value="{}" />
            </form>
        </div>""".format(
            entity
        )

        if entity:
            config_text = self.html_config_item_text(entity)
            if not config_text:
                text += "<table>\n"
                text += "<tr><th></th><th>Name</th><th>Entity</th><th>State</th><th>Attributes</th></tr>\n"
                text += self.html_get_entity_text(entity)
                text += "</table>\n"
            else:
                text += config_text

            text += "<h2>History Chart</h2>\n"
            text += '<div id="chart"></div>'
            now_str = self.base.now_utc.strftime(TIME_FORMAT)
            history = self.base.get_history_wrapper(entity, days, required=False)
            history_chart = self.base.history_attribute(history)
            series_data = []
            series_data.append({"name": "entity_id", "data": history_chart, "chart_type": "line", "stroke_width": "3", "stroke_curve": "stepline"})
            text += self.render_chart(series_data, unit_of_measurement, friendly_name, now_str)

            # History table
            text += "<h2>History</h2>\n"
            text += "<table>\n"
            text += "<tr><th>Time</th><th>State</th></tr>\n"

            prev_stamp = None
            if history and len(history) >= 1:
                history = history[0]
                if history:
                    count = 0
                    history.reverse()
                    for item in history:
                        if "last_updated" not in item:
                            continue
                        last_updated_time = item["last_updated"]
                        last_updated_stamp = str2time(last_updated_time)
                        state = item.get("state", None)
                        if state is None:
                            state = "None"
                        # Only show in 30 minute intervals
                        if prev_stamp and ((prev_stamp - last_updated_stamp) < timedelta(minutes=30)):
                            continue
                        text += "<tr><td>{}</td><td>{}</td></tr>\n".format(last_updated_stamp.strftime(TIME_FORMAT), state)
                        prev_stamp = last_updated_stamp
                        count += 1
            text += "</table>\n"
        else:
            text += "<h2>Select an entity</h2>\n"

        # Return web response
        text += "</body></html>\n"
        return web.Response(content_type="text/html", text=text)

    def get_entity_detailedForecast(self, entity, subitem="pv_estimate"):
        results = {}
        detailedForecast = self.base.dashboard_values.get(entity, {}).get("attributes", {}).get("detailedForecast", {})
        if detailedForecast:
            for item in detailedForecast:
                sub = item.get(subitem, None)
                start = item.get("period_start", None)
                if (sub is not None) and (start is not None):
                    results[start] = sub
        return results

    def get_entity_results(self, entity):
        results = self.base.dashboard_values.get(entity, {}).get("attributes", {}).get("results", {})
        return results

    def get_header(self, title, refresh=0, codemirror=False):
        calculating = self.base.get_arg("active", False)
        if self.base.update_pending:
            calculating = True
        return get_header_html(title, calculating, self.default_page, self.base.arg_errors, THIS_VERSION, self.get_battery_status_icon(), refresh, codemirror=codemirror)

    def get_chart_series(self, name, results, chart_type, color):
        """
        Return HTML for a chart series
        """
        text = ""
        text += "   {\n"
        text += "    name: '{}',\n".format(name)
        text += "    type: '{}',\n".format(chart_type)
        if color:
            text += "    color: '{}',\n".format(color)
        text += "    data: [\n"
        first = True
        for key in results:
            if not first:
                text += ","
            first = False
            text += "{"
            text += "x: new Date('{}').getTime(),".format(key)
            text += "y: {}".format(results[key])
            text += "}"
        text += "  ]\n"
        text += "  }\n"
        return text

    def render_chart(self, series_data, yaxis_name, chart_name, now_str, tagname="chart", daily_chart=True):
        """
        Render a chart
        """
        midnight_str = (self.base.midnight_utc + timedelta(days=1)).strftime(TIME_FORMAT)
        text = ""
        if daily_chart:
            text += """
<script>
window.onresize = function(){ location.reload(); };
var width = window.innerWidth;
var height = window.innerHeight;
if (width < 600) {
    width = 600
}
width = width - 50;
height = height - 120;

if (height * 1.68 > width) {
   height = width / 1.68;
}
else {
   width = height * 1.68;
}

var options = {
  chart: {
    type: 'line',
    width: width,
    height: height,
    animations: {
        enabled: false
    }
  },
  span: {
    start: 'minute', offset: '-12h'
  },
"""
        else:
            text += """
<script>
window.onresize = function(){ location.reload(); };
var width = window.innerWidth;
var height = window.innerHeight;
width = width / 3 * 2;
height = height / 3 * 2;

if (height * 1.68 > width) {
   height = width / 1.68;
}
else {
   width = height * 1.68;
}

var options = {
  chart: {
    type: 'line',
    width: width,
    height: height
  },
  span: {
    start: 'day'
  },
"""

        text += "  series: [\n"
        first = True
        opacity = []
        stroke_width = []
        stroke_curve = []
        for series in series_data:
            name = series.get("name")
            results = series.get("data")
            opacity_value = series.get("opacity", "1.0")
            stroke_width_value = series.get("stroke_width", "1")
            stroke_curve_value = series.get("stroke_curve", "smooth")
            chart_type = series.get("chart_type", "line")
            color = series.get("color", "")

            if results:
                if not first:
                    text += ","
                first = False
                text += self.get_chart_series(name, results, chart_type, color)
                opacity.append(opacity_value)
                stroke_width.append(stroke_width_value)
                stroke_curve.append("'{}'".format(stroke_curve_value))

        text += "  ],\n"
        text += "  fill: {\n"
        text += "     opacity: [{}]\n".format(",".join(opacity))
        text += "  },\n"
        text += "  stroke: {\n"
        text += "     width: [{}],\n".format(",".join(stroke_width))
        text += "     curve: [{}],\n".format(",".join(stroke_curve))
        text += "  },\n"
        text += "  xaxis: {\n"
        text += "    type: 'datetime',\n"
        text += "    labels: {\n"
        text += "           datetimeUTC: false\n"
        text += "       }\n"
        text += "  },\n"
        text += "  tooltip: {\n"
        text += "    x: {\n"
        text += "      format: 'dd/MMM HH:mm'\n"
        text += "    }\n"
        text += "  },"
        text += "  yaxis: {\n"
        text += "    title: {{ text: '{}' }},\n".format(yaxis_name)
        text += "    decimalsInFloat: 2\n"
        text += "  },\n"
        text += "  title: {\n"
        text += "    text: '{}'\n".format(chart_name)
        text += "  },\n"
        text += "  legend: {\n"
        text += "    position: 'top',\n"
        text += "    formatter: function(seriesName, opts) {\n"
        text += "        return [opts.w.globals.series[opts.seriesIndex].at(-1),' {} <br> ', seriesName]\n".format(yaxis_name)
        text += "    }\n"
        text += "  },\n"
        text += "  annotations: {\n"
        text += "   xaxis: [\n"
        text += "    {\n"
        text += "       x: new Date('{}').getTime(),\n".format(now_str)
        text += "       borderColor: '#775DD0',\n"
        text += "       textAnchor: 'middle',\n"
        text += "       label: {\n"
        text += "          text: 'now'\n"
        text += "       }\n"
        text += "    },\n"
        text += "    {\n"
        text += "       x: new Date('{}').getTime(),\n".format(midnight_str)
        text += "       borderColor: '#000000',\n"
        text += "       textAnchor: 'middle',\n"
        text += "       label: {\n"
        text += "          text: 'midnight'\n"
        text += "       }\n"
        text += "    }\n"
        text += "   ]\n"
        text += "  }\n"
        text += "}\n"
        text += "var chart = new ApexCharts(document.querySelector('#{}'), options);\n".format(tagname)
        text += "chart.render();\n"
        text += "</script>\n"
        return text

    async def html_api_get_log(self, request):
        """
        JSON API to get log data with filtering
        """
        try:
            logfile = "predbat.log"
            logdata = ""

            if os.path.exists(logfile):
                with open(logfile, "r") as f:
                    logdata = f.read()

            # Get query parameters
            args = request.query
            filter_type = args.get("filter", "warnings")  # all, warnings, errors
            since_line = int(args.get("since", 0))  # Line number to start from
            max_lines = int(args.get("max_lines", 1024))  # Maximum lines to return

            loglines = logdata.split("\n")
            total_lines = len(loglines)

            # Process log lines with filtering
            result_lines = []
            count_lines = 0
            lineno = total_lines - 1

            while count_lines < max_lines and lineno >= 0:
                line = loglines[lineno]
                line_lower = line.lower()

                # Skip empty lines
                if not line.strip():
                    lineno -= 1
                    continue

                # Apply filtering
                include_line = False
                line_type = "info"

                if "error" in line_lower:
                    line_type = "error"
                    include_line = True
                elif "warn" in line_lower:
                    line_type = "warning"
                    include_line = filter_type in ["all", "warnings"]
                else:
                    line_type = "info"
                    include_line = filter_type == "all"

                if include_line and (since_line == 0 or lineno > since_line):
                    start_line = line[0:27] if len(line) >= 27 else line
                    rest_line = line[27:] if len(line) >= 27 else ""

                    result_lines.append({"line_number": lineno, "timestamp": start_line, "message": rest_line, "type": line_type, "full_line": line})
                    count_lines += 1

                lineno -= 1

            # Reverse to get reverse chronological order (newest first)
            result_lines.reverse()

            return web.json_response({"status": "success", "total_lines": total_lines, "returned_lines": len(result_lines), "lines": result_lines, "filter": filter_type})

        except Exception as e:
            self.log(f"Error in html_api_get_log: {e}")
            return web.json_response({"status": "error", "message": str(e)}, status=500)

    async def html_api_post_state(self, request):
        """
        JSON API
        """
        json_data = await request.json()
        entity_id = json.get("entity_id", None)
        state = json_data.get("state", None)
        attributes = json_data.get("attributes", {})
        if entity_id:
            self.base.set_state_wrapper(entity_id, state, attributes=attributes)
            return web.Response(content_type="application/json", text='{"result": "ok"}')
        else:
            return web.Response(content_type="application/json", text='{"result": "error"}')

    async def html_api_ping(self, request):
        """
        Check if Predbat is running
        return error 500 if not running
        """
        try:
            is_running = self.base.is_running()
        except Exception as e:
            self.log("Error checking if Predbat is running: {}".format(e))
            is_running = False

        if is_running:
            return web.Response(content_type="application/json", text='{"result": "ok"}')
        else:
            return web.Response(status=500, content_type="application/json", text='{"result": "error"}')

    async def html_api_get_state(self, request):
        """
        JSON API
        """
        args = request.query
        state_data = self.base.get_state_wrapper()
        if "entity_id" in args:
            text = json.dumps(state_data.get(args["entity_id"], {}))
            return web.Response(content_type="application/json", text=text)
        else:
            text = json.dumps(state_data)
            return web.Response(content_type="application/json", text=text)

    async def html_api_post_service(self, request):
        """
        JSON API
        """
        json_data = await request.json()
        service = json_data.get("service", None)
        service_data = json_data.get("data", {})
        if service:
            result = self.base.call_service_wrapper(service, **service_data)
            return web.Response(content_type="application/json", text=json.dumps(result))
        else:
            return web.Response(content_type="application/json", text='{"result": "error"}')

    async def html_plan(self, request):
        """
        Return the Predbat plan as an HTML page
        """
        self.default_page = "./plan"
        text = self.get_header("Predbat Plan", refresh=30)

        if not self.base.dashboard_index:
            text += "<body>"
            text += "<h2>Loading please wait...</h2>"
            text += "</body></html>\n"
            return web.Response(content_type="text/html", text=text)

        """ The table html_plan is already generated in the base class and is in the format
        <table><tr><tr><th><b>Time</b></th><th><b>Import p (w/loss)</b></th><th><b>Export p (w/loss)</b></th><th colspan=2><b>State</b></th><th><b>Limit %</b></th><th><b>PV kWh (10%)</b></th><th><b>Load kWh (10%)</b></th><th><b>Clip kWh</b></th><th><b>XLoad kWh</b></th><th><b>Car kWh</b></th><th><b>SoC %</b></th><th><b>Cost</b></th><th><b>Total</b></th><th><b>CO2 g/kWh</b></th><th><b>CO2 kg</b></th></tr><tr style="color:black"><td bgcolor=#FFFFFF>Sun 16:00</td><td style="padding: 4px;" bgcolor=#3AEE85><b>7.00 (7.52)</b> </td><td style="padding: 4px;" bgcolor=#FFFFAA>15.00 (13.97) </td><td colspan=2 style="padding: 4px;" bgcolor=#3AEE85>Chrg&nearr;</td><td bgcolor=#FFFFFF> 95 (95)</td><td bgcolor=#FFAAAA>0.83 (0.45)&#9728;</td><td bgcolor=#FFFF00>0.47 (0.57)</td><td bgcolor=#FFFFFF>&#9866;</td><td bgcolor=#FFFF00>0.42</td><td bgcolor=FFFF00>3.84</td><td bgcolor=#3AEE85>92&nearr;</td><td bgcolor=#F18261>+26 p  &nearr;</td><td bgcolor=#FFFFFF>&#163;1.06</td><td bgcolor=#90EE90>57 </td><td bgcolor=#FFAA00> 3.39 &nearr; </td></tr>
        <tr style="color:black"><td bgcolor=#FFFFFF>Sun 16:30</td><td style="padding: 4px;" bgcolor=#3AEE85><b>7.00 (7.52)</b> </td><td style="padding: 4px;" bgcolor=#FFFFAA>15.00 (13.97) </td><td colspan=2 style="padding: 4px;" rowspan=2 bgcolor=#EEEEEE>FrzChrg&rarr;</td><td rowspan=2 bgcolor=#FFFFFF> 95 (4)</td><td bgcolor=#FFAAAA>0.84 (0.46)&#9728;</td><td bgcolor=#F18261>0.54 (0.64)</td><td bgcolor=#FFFFFF>&#9866;</td><td bgcolor=#FFFF00>0.47</td><td bgcolor=FFFF00>3.84</td><td bgcolor=#3AEE85>95&rarr;</td><td bgcolor=#F18261>+24 p  &nearr;</td><td bgcolor=#FFFFFF>&#163;1.33</td><td bgcolor=#90EE90>60 </td><td bgcolor=#FFAA00> 3.6 &nearr; </td></tr>
        </table>
        """
        text += get_plan_css()

        # Process HTML table to add buttons to time cells
        html_plan = self.base.html_plan

        # Regular expression to find time cells in the table
        time_pattern = r"<td id=time.*?>((?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) \d{2}:\d{2})</td>"
        import_pattern = r"<td id=import data-minute=(\S+) data-rate=(\S+)(.*?)>(.*?)</td>"
        export_pattern = r"<td id=export data-minute=(\S+) data-rate=(\S+)(.*?)>(.*?)</td>"
        load_pattern = r"<td id=load data-minute=(\S+) (.*?)>(.*?)</td>"

        # Counter for creating unique IDs for dropdowns
        dropdown_counter = 0

        manual_charge_times = self.base.manual_times("manual_charge")
        manual_export_times = self.base.manual_times("manual_export")
        manual_freeze_charge_times = self.base.manual_times("manual_freeze_charge")
        manual_freeze_export_times = self.base.manual_times("manual_freeze_export")
        manual_demand_times = self.base.manual_times("manual_demand")
        manual_all_times = manual_charge_times + manual_export_times + manual_demand_times + manual_freeze_charge_times + manual_freeze_export_times
        manual_import_rates = self.base.manual_rates("manual_import_rates")
        manual_export_rates = self.base.manual_rates("manual_export_rates")
        manual_load_adjust = self.base.manual_rates("manual_load_adjust")

        # Function to replace time cells with cells containing dropdowns
        def add_button_to_time(match):
            nonlocal dropdown_counter
            time_text = match.group(1)
            dropdown_id = f"dropdown_{dropdown_counter}"
            dropdown_counter += 1

            time_stamp = self.get_override_time_from_string(time_text)
            minutes_from_midnight = (time_stamp - self.base.midnight_utc).total_seconds() / 60
            in_override = False
            cell_bg_color = "#FFFFFF"
            override_class = ""
            if minutes_from_midnight in manual_charge_times:
                in_override = True
                cell_bg_color = "#3AEE85"  # Matches auto charging green
                override_class = "override-charge"
            elif minutes_from_midnight in manual_export_times:
                in_override = True
                cell_bg_color = "#FFFF00"  # Matches export yellow
                override_class = "override-export"
            elif minutes_from_midnight in manual_demand_times:
                in_override = True
                cell_bg_color = "#F18261"  # Matches high-cost demand red
                override_class = "override-demand"
            elif minutes_from_midnight in manual_freeze_charge_times:
                in_override = True
                cell_bg_color = "#EEEEEE"  # Matches freeze charging grey
                override_class = "override-freeze-charge"
            elif minutes_from_midnight in manual_freeze_export_times:
                in_override = True
                cell_bg_color = "#AAAAAA"  # Matches freeze exporting dark grey
                override_class = "override-freeze-export"

            # Create clickable cell and dropdown HTML
            button_html = f"""<td bgcolor={cell_bg_color} onclick="toggleForceDropdown('{dropdown_id}')" class="clickable-time-cell {override_class}">
                {time_text}
                <div class="dropdown">
                    <div id="{dropdown_id}" class="dropdown-content">
            """

            if minutes_from_midnight in manual_all_times:
                button_html += f"""<a onclick="handleTimeOverride('{time_text}', 'Clear')">Clear</a>"""
            if minutes_from_midnight not in manual_demand_times:
                button_html += f"""<a onclick="handleTimeOverride('{time_text}', 'Manual Demand')">Manual Demand</a>"""
            if minutes_from_midnight not in manual_charge_times:
                button_html += f"""<a onclick="handleTimeOverride('{time_text}', 'Manual Charge')">Manual Charge</a>"""
            if minutes_from_midnight not in manual_export_times:
                button_html += f"""<a onclick="handleTimeOverride('{time_text}', 'Manual Export')">Manual Export</a>"""
            if minutes_from_midnight not in manual_freeze_charge_times:
                button_html += f"""<a onclick="handleTimeOverride('{time_text}', 'Manual Freeze Charge')">Manual Freeze Charge</a>"""
            if minutes_from_midnight not in manual_freeze_export_times:
                button_html += f"""<a onclick="handleTimeOverride('{time_text}', 'Manual Freeze Export')">Manual Freeze Export</a>"""
            button_html += f"""
                    </div>
                </div>
            </td>"""

            return button_html

        def add_button_to_export(match):
            return add_button_to_import(match, is_import=False)

        def add_button_to_import(match, is_import=True):
            """
            Add import rate button to import cells
            """
            nonlocal dropdown_counter
            dropdown_id = f"dropdown_{dropdown_counter}"
            input_id = f"rate_input_{dropdown_counter}"
            dropdown_counter += 1

            import_minute = match.group(1)
            import_rate = match.group(2).strip()
            import_tag = match.group(3).strip()
            import_rate_text = match.group(4).strip()
            import_minute_to_time = self.base.midnight_utc + timedelta(minutes=int(import_minute))
            import_minute_str = import_minute_to_time.strftime("%a %H:%M")
            override_active = False
            if is_import:
                if int(import_minute) in manual_import_rates:
                    override_active = True
                    import_rate = manual_import_rates[int(import_minute)]
            elif int(import_minute) in manual_export_rates:
                override_active = True
                import_rate = manual_export_rates[int(import_minute)]

            button_html = f"""<td {import_tag} class="clickable-time-cell {'override-active' if override_active else ''}" onclick="toggleForceDropdown('{dropdown_id}')">
                {import_rate_text}
                <div class="dropdown">
                    <div id="{dropdown_id}" class="dropdown-content">
            """
            if override_active:
                action = "Clear Import" if is_import else "Clear Export"
                button_html += f"""<a onclick="handleRateOverride('{import_minute_str}', '{import_rate}', '{action}', true)">{action}</a>"""
            else:
                # Add input field for custom rate entry
                default_rate = self.base.get_arg("manual_import_value", 0.0) if is_import else self.base.get_arg("manual_export_value", 0.0)
                action = "Set Import" if is_import else "Set Export"
                button_html += f"""
                    <div style="padding: 12px 16px;">
                        <label style="display: block; margin-bottom: 5px; color: inherit;">{action} {import_minute_str} Rate:</label>
                        <input type="number" id="{input_id}" step="0.1" value="{default_rate}"
                               style="width: 80px; padding: 4px; margin-bottom: 8px; border-radius: 3px;">
                        <br>
                        <button onclick="handleRateOverride('{import_minute_str}', document.getElementById('{input_id}').value, '{action}', false)"
                                style="padding: 6px 12px; border-radius: 3px; font-size: 12px;">
                            Set Rate
                        </button>
                    </div>
                """
            button_html += f"""
                    </div>
                </div>
            </td>"""

            return button_html

        def add_button_to_load(match):
            """
            Add load rate button to load cells
            """
            nonlocal dropdown_counter
            dropdown_id = f"dropdown_{dropdown_counter}"
            input_id = f"load_input_{dropdown_counter}"
            dropdown_counter += 1

            load_minute = match.group(1)
            load_tag = match.group(2).strip()
            load_text = match.group(3).strip()
            load_minute_to_time = self.base.midnight_utc + timedelta(minutes=int(load_minute))
            load_minute_str = load_minute_to_time.strftime("%a %H:%M")
            override_active = False
            if int(load_minute) in manual_load_adjust:
                override_active = True
                load_adjust = manual_load_adjust[int(load_minute)]

            button_html = f"""<td {load_tag} class="clickable-time-cell {'override-active' if override_active else ''}" onclick="toggleForceDropdown('{dropdown_id}')">
                {load_text}
                <div class="dropdown">
                    <div id="{dropdown_id}" class="dropdown-content">
            """
            if override_active:
                action = "Clear Load"
                button_html += f"""<a onclick="handleLoadOverride('{load_minute_str}', '{load_adjust}', '{action}', true)">{action}</a>"""
            else:
                # Add input field for custom rate entry
                default_adjust = self.base.get_arg("manual_load_value", 0.0)
                action = "Set Load"
                button_html += f"""
                    <div style="padding: 12px 16px;">
                        <label style="display: block; margin-bottom: 5px; color: inherit;">{action} {load_minute_str} Adjustment:</label>
                        <input type="number" id="{input_id}" step="0.1" value="{default_adjust}"
                               style="width: 80px; padding: 4px; margin-bottom: 8px; border-radius: 3px;">
                        <br>
                        <button onclick="handleLoadOverride('{load_minute_str}', document.getElementById('{input_id}').value, '{action}', false)"
                                style="padding: 6px 12px; border-radius: 3px; font-size: 12px;">
                            Set Load Adjustment
                        </button>
                    </div>
                """
            button_html += f"""
                    </div>
                </div>
            </td>"""

            return button_html

        # Process the HTML plan to add buttons to time cells
        processed_html = re.sub(time_pattern, add_button_to_time, html_plan)
        processed_html = re.sub(import_pattern, add_button_to_import, processed_html)
        processed_html = re.sub(export_pattern, add_button_to_export, processed_html)
        processed_html = re.sub(load_pattern, add_button_to_load, processed_html)

        text += processed_html + "</body></html>\n"
        return web.Response(content_type="text/html", text=text)

    async def html_log(self, request):
        """
        Return the Predbat log as an HTML page with dynamic updates
        """
        self.default_page = "./log"

        # Decode method get arguments to determine filter type
        args = request.query
        errors = False
        warnings = False

        if "errors" in args or (not args and self.default_log == "errors"):
            errors = True
            self.default_log = "errors"
        elif "warnings" in args or (not args and self.default_log == "warnings"):
            warnings = True
            self.default_log = "warnings"
        elif "all" in args or (not args and self.default_log == "all"):
            self.default_log = "all"
        else:
            self.default_log = "warnings"
            warnings = True

        # Remove refresh from header since we'll update dynamically
        text = self.get_header("Predbat Log", refresh=0)
        text += """<body>"""
        text += get_log_css()

        if errors:
            active_all = ""
            active_warnings = ""
            active_errors = "active"
            filter_type = "errors"
        elif warnings:
            active_all = ""
            active_warnings = "active"
            active_errors = ""
            filter_type = "warnings"
        else:
            active_all = "active"
            active_warnings = ""
            active_errors = ""
            filter_type = "all"

        text += '<div class="log-menu">'
        text += "<h3>Logfile</h3> "
        text += f'<a href="./log?all" class="{active_all}">All</a>'
        text += f'<a href="./log?warnings" class="{active_warnings}">Warnings</a>'
        text += f'<a href="./log?errors" class="{active_errors}">Errors</a>'
        text += '<a href="./debug_log">Download</a>'
        text += '<label class="auto-scroll-toggle"><input type="checkbox" id="autoScroll"> Auto-scroll to new</label>'
        text += '<button class="scroll-to-bottom" onclick="scrollToBottom()">Scroll to Bottom</button>'
        text += '<button id="pauseResumeBtn" onclick="toggleUpdates()" style="margin-left: 10px; padding: 4px 8px;">Pause</button>'
        text += "</div>"

        text += '<div class="log-search-container">'
        text += '<input type="text" id="logSearchInput" class="log-search-input" placeholder="Search log entries..." oninput="filterLogEntries()" />'
        text += '<button id="clearSearchBtn" class="clear-search-button" onclick="clearLogSearch()">Clear</button>'
        text += '<div id="searchStatus" class="search-status"></div>'
        text += "</div>"

        text += '<div id="logStatus" class="log-status">Loading log data...</div>'
        text += "<table width=100% id='logTable'>\n"
        text += "<tbody id='logTableBody'>\n"
        text += "<!-- Log entries will be loaded dynamically via JavaScript -->\n"
        text += "</tbody>\n"
        text += "</table>"

        # Add JavaScript for dynamic updates
        text += get_logfile_js(filter_type)

        text += "</body></html>\n"
        return web.Response(content_type="text/html", text=text)

    async def html_config_post(self, request):
        """
        Save the Predbat config from an HTML page
        """
        postdata = await request.post()

        # Process only the submitted form data
        for pitem in postdata:
            new_value = postdata[pitem]
            if pitem:
                pitem = pitem.replace("__", ".")
            if new_value == "None":
                new_value = ""
            if pitem.startswith("switch"):
                if new_value == "on":
                    new_value = True
                else:
                    new_value = False
            elif pitem.startswith("input_number"):
                new_value = float(new_value)

            await self.base.ha_interface.set_state_external(pitem, new_value)

        raise web.HTTPFound("./config")

    def render_type(self, arg, value, parent_path="", row_counter=None):
        """
        Render a value based on its type with support for nested editing.

        Parameters:
        - arg (str): The name of the argument or key being rendered.
        - value (any): The value to render, which can be a list, dictionary, or other data type.
        - parent_path (str): The hierarchical path to the current value, used for nested structures.
          For example, "config[0]" for the first item in a list under the "config" key.
        - row_counter (int, optional): A counter to track the number of rows rendered, useful for
          indexing or applying alternating styles in tables.

        Returns:
        - str: The rendered HTML representation of the value.
        """
        text = ""
        if isinstance(value, list):
            text += "<table>"
            for idx, item in enumerate(value):
                nested_path = f"{parent_path}[{idx}]" if parent_path else f"{arg}[{idx}]"

                # Check if this list item is editable
                can_edit = self.is_editable_value(item)
                actions_cell = ""

                if can_edit and row_counter is not None:
                    row_counter[0] += 1
                    nested_row_id = row_counter[0]

                    if isinstance(item, bool):
                        toggle_class = "toggle-button active" if item else "toggle-button"
                        actions_cell = f'<button class="{toggle_class}" onclick="toggleNestedValue({nested_row_id})" data-value="{str(item).lower()}" data-path="{nested_path}"></button>'
                    else:
                        actions_cell = f'<button class="edit-button" onclick="editNestedValue({nested_row_id})" data-path="{nested_path}">Edit</button>'

                    # Store the nested value info for later processing
                    if not hasattr(self, "_nested_values"):
                        self._nested_values = {}
                    self._nested_values[nested_row_id] = {"path": nested_path, "value": item}

                raw_value = self.resolve_value_raw(arg, item)

                if actions_cell:
                    text += f"<tr id='nested_row_{row_counter[0] if can_edit else 'static'}' data-nested-path='{nested_path}' data-nested-original='{html_module.escape(str(raw_value))}'><td>- </td><td id='nested_value_{row_counter[0] if can_edit else 'static'}'>{self.render_type(arg, item, nested_path, row_counter)}</td><td>{actions_cell}</td></tr>\n"
                else:
                    text += "<tr><td>- {}</td></tr>\n".format(self.render_type(arg, item, nested_path, row_counter))
            text += "</table>"
        elif isinstance(value, dict):
            text += "<table>"
            for key in value:
                nested_path = f"{parent_path}.{key}" if parent_path else f"{arg}.{key}"
                nested_value = value[key]

                # Check if this nested value is editable
                can_edit = self.is_editable_value(nested_value)
                actions_cell = ""

                if can_edit and row_counter is not None:
                    row_counter[0] += 1
                    nested_row_id = row_counter[0]

                    if isinstance(nested_value, bool):
                        toggle_class = "toggle-button active" if nested_value else "toggle-button"
                        actions_cell = f'<button class="{toggle_class}" onclick="toggleNestedValue({nested_row_id})" data-value="{str(nested_value).lower()}" data-path="{nested_path}"></button>'
                    else:
                        actions_cell = f'<button class="edit-button" onclick="editNestedValue({nested_row_id})" data-path="{nested_path}">Edit</button>'

                    # Store the nested value info for later processing
                    if not hasattr(self, "_nested_values"):
                        self._nested_values = {}
                    self._nested_values[nested_row_id] = {"path": nested_path, "value": nested_value}

                raw_value = self.resolve_value_raw(key, nested_value)

                if actions_cell:
                    text += f"<tr id='nested_row_{row_counter[0] if can_edit else 'static'}' data-nested-path='{nested_path}' data-nested-original='{html_module.escape(str(raw_value))}'><td><b>{key}: </b></td><td id='nested_value_{row_counter[0] if can_edit else 'static'}'>{self.render_type(key, nested_value, nested_path, row_counter)}</td><td>{actions_cell}</td></tr>\n"
                else:
                    text += "<tr><td><b>{}: </b></td><td colspan='2'>{}</td></tr>\n".format(key, self.render_type(key, nested_value, nested_path, row_counter))
            text += "</table>"
        elif isinstance(value, str):
            pat = re.match(r"^[a-zA-Z]+\.\S+", value)
            if "{" in value:
                text = self.base.resolve_arg(arg, value, indirect=False, quiet=True)
                if text is None:
                    text = '<span style="background-color:#FFAAAA"> {} </p>'.format(value)
                else:
                    text = self.render_type(arg, text, parent_path, row_counter)
            elif pat and (arg != "service"):
                entity_id = value
                unit_of_measurement = ""
                if "$" in entity_id:
                    entity_id, attribute = entity_id.split("$")
                    state = self.base.get_state_wrapper(entity_id=entity_id, attribute=attribute, default=None)
                else:
                    state = self.base.get_state_wrapper(entity_id=entity_id, default=None)
                    unit_of_measurement = self.base.get_state_wrapper(entity_id=entity_id, attribute="unit_of_measurement", default="")

                if state is not None:
                    text = '<a href="./entity?entity_id={}">{}</a> = {}{}'.format(entity_id, value, state, unit_of_measurement)
                else:
                    text = '<span style="background-color:#FFAAAA"> {} = ?{} </span>'.format(value, unit_of_measurement)
            else:
                text = str(value)
        else:
            text = str(value)
        return text

    async def html_file(self, filename, data):
        if data is None:
            return web.Response(content_type="text/html", text="{} not found".format(filename), status=404)
        else:
            return web.Response(content_type="application/octet-stream", body=data.encode("utf-8"), headers={"Content-Disposition": "attachment; filename={}".format(filename)})  # Convert text to binary if needed

    async def html_debug_yaml(self, request):
        """
        Return the Predbat debug yaml data
        """
        yaml_debug = self.base.create_debug_yaml(write_file=False)
        return await self.html_file("predbat_debug.yaml", yaml_debug)

    async def html_file_load(self, filename):
        """
        Load a file and serve it up
        """
        data = None
        if os.path.exists(filename):
            with open(filename, "r") as f:
                data = f.read()
        return await self.html_file(filename, data)

    async def html_debug_log(self, request):
        return await self.html_file_load("predbat.log")

    async def html_debug_apps(self, request):
        return await self.html_file_load("apps.yaml")

    async def html_debug_plan(self, request):
        html_plan = self.base.html_plan
        if not html_plan:
            html_plan = None
        return await self.html_file("predbat_plan.html", html_plan)

    async def html_dash(self, request):
        """
        Render apps.yaml as an HTML page
        """
        self.default_page = "./dash"
        text = self.get_header("Predbat Dashboard", refresh=60)
        text += "<body>\n"
        text += self.get_status_html(self.base.current_status, THIS_VERSION)
        text += "</body></html>\n"
        return web.Response(content_type="text/html", text=text)

    async def html_dash_post(self, request):
        """
        Handle POST request for dashboard status updates
        """
        try:
            # Parse form data
            data = await request.post()

            # Handle the different types of controls
            for key, value in data.items():
                if key == "mode":
                    # Update mode - it's a select type
                    entity_id = f"select.{self.base.prefix}_{key}"
                    await self.base.ha_interface.set_state_external(entity_id, value)
                elif key in ["debug_enable", "set_read_only"]:
                    # Update switches - convert to boolean
                    entity_id = f"switch.{self.base.prefix}_{key}"
                    bool_value = value == "on"
                    await self.base.ha_interface.set_state_external(entity_id, bool_value)

            # Log the update
            self.log(f"Dashboard status updated: {dict(data)}")

        except Exception as e:
            self.log(f"ERROR: Failed to update dashboard status: {str(e)}")

        # Redirect back to dashboard
        raise web.HTTPFound("./dash")

    def get_chart(self, chart):
        """
        Return the HTML for a chart
        """
        now_str = self.base.now_utc.strftime(TIME_FORMAT)
        soc_kw_h0 = {}
        if self.base.soc_kwh_history:
            hist = self.base.soc_kwh_history
            for minute in range(0, self.base.minutes_now, 30):
                minute_timestamp = self.base.midnight_utc + timedelta(minutes=minute)
                stamp = minute_timestamp.strftime(TIME_FORMAT)
                soc_kw_h0[stamp] = hist.get(self.base.minutes_now - minute, 0)
        soc_kw_h0[now_str] = self.base.soc_kw
        soc_kw = self.get_entity_results(self.base.prefix + ".soc_kw")
        soc_kw_best = self.get_entity_results(self.base.prefix + ".soc_kw_best")
        soc_kw_best10 = self.get_entity_results(self.base.prefix + ".soc_kw_best10")
        soc_kw_base10 = self.get_entity_results(self.base.prefix + ".soc_kw_base10")
        charge_limit_kw = self.get_entity_results(self.base.prefix + ".charge_limit_kw")
        best_charge_limit_kw = self.get_entity_results(self.base.prefix + ".best_charge_limit_kw")
        best_export_limit_kw = self.get_entity_results(self.base.prefix + ".best_export_limit_kw")
        battery_power_best = self.get_entity_results(self.base.prefix + ".battery_power_best")
        pv_power_best = self.get_entity_results(self.base.prefix + ".pv_power_best")
        grid_power_best = self.get_entity_results(self.base.prefix + ".grid_power_best")
        load_power_best = self.get_entity_results(self.base.prefix + ".load_power_best")
        iboost_best = self.get_entity_results(self.base.prefix + ".iboost_best")
        metric = self.get_entity_results(self.base.prefix + ".metric")
        best_metric = self.get_entity_results(self.base.prefix + ".best_metric")
        best10_metric = self.get_entity_results(self.base.prefix + ".best10_metric")
        cost_today = self.get_entity_results(self.base.prefix + ".cost_today")
        cost_today_export = self.get_entity_results(self.base.prefix + ".cost_today_export")
        cost_today_import = self.get_entity_results(self.base.prefix + ".cost_today_import")
        base10_metric = self.get_entity_results(self.base.prefix + ".base10_metric")
        rates = self.get_entity_results(self.base.prefix + ".rates")
        rates_export = self.get_entity_results(self.base.prefix + ".rates_export")
        rates_gas = self.get_entity_results(self.base.prefix + ".rates_gas")
        record = self.get_entity_results(self.base.prefix + ".record")

        text = ""

        if not soc_kw_best:
            text += "<br><h2>Loading...</h2>"
        elif chart == "Battery":
            series_data = [
                {"name": "Base", "data": soc_kw, "opacity": "1.0", "stroke_width": "2", "stroke_curve": "smooth", "color": "#3291a8"},
                {"name": "Base10", "data": soc_kw_base10, "opacity": "1.0", "stroke_width": "2", "stroke_curve": "smooth", "color": "#e8972c"},
                {"name": "Best", "data": soc_kw_best, "opacity": "1.0", "stroke_width": "4", "stroke_curve": "smooth", "color": "#eb2323"},
                {"name": "Best10", "data": soc_kw_best10, "opacity": "1.0", "stroke_width": "2", "stroke_curve": "smooth", "color": "#cd23eb"},
                {"name": "Actual", "data": soc_kw_h0, "opacity": "1.0", "stroke_width": "2", "stroke_curve": "smooth", "color": "#3291a8"},
                {"name": "Charge Limit Base", "data": charge_limit_kw, "opacity": "1.0", "stroke_width": "2", "stroke_curve": "stepline", "color": "#15eb8b"},
                {
                    "name": "Charge Limit Best",
                    "data": best_charge_limit_kw,
                    "opacity": "0.2",
                    "stroke_width": "4",
                    "stroke_curve": "stepline",
                    "chart_type": "area",
                    "color": "#e3e019",
                },
                {"name": "Best Export Limit", "data": best_export_limit_kw, "opacity": "1.0", "stroke_width": "3", "stroke_curve": "stepline", "color": "#15eb1c"},
                {"name": "Record", "data": record, "opacity": "0.5", "stroke_width": "4", "stroke_curve": "stepline", "color": "#000000", "chart_type": "area"},
            ]
            text += self.render_chart(series_data, "kWh", "Battery SOC Prediction", now_str)
        elif chart == "Power":
            series_data = [
                {"name": "battery", "data": battery_power_best, "opacity": "1.0", "stroke_width": "2", "stroke_curve": "smooth"},
                {"name": "pv", "data": pv_power_best, "opacity": "1.0", "stroke_width": "2", "stroke_curve": "smooth"},
                {"name": "grid", "data": grid_power_best, "opacity": "1.0", "stroke_width": "2", "stroke_curve": "smooth"},
                {"name": "load", "data": load_power_best, "opacity": "1.0", "stroke_width": "2", "stroke_curve": "smooth"},
                {"name": "iboost", "data": iboost_best, "opacity": "1.0", "stroke_width": "2", "stroke_curve": "smooth"},
            ]
            text += self.render_chart(series_data, "kW", "Best Power", now_str)
        elif chart == "Cost":
            series_data = [
                {"name": "Actual", "data": cost_today, "opacity": "0.2", "stroke_width": "2", "stroke_curve": "smooth", "chart_type": "area"},
                {"name": "Actual Import", "data": cost_today_import, "opacity": "1.0", "stroke_width": "2", "stroke_curve": "smooth"},
                {"name": "Actual Export", "data": cost_today_export, "opacity": "1.0", "stroke_width": "2", "stroke_curve": "smooth"},
                {"name": "Base", "data": metric, "opacity": "1.0", "stroke_width": "2", "stroke_curve": "smooth"},
                {"name": "Best", "data": best_metric, "opacity": "0.2", "stroke_width": "2", "stroke_curve": "smooth", "chart_type": "area"},
                {"name": "Base10", "data": base10_metric, "opacity": "1.0", "stroke_width": "2", "stroke_curve": "smooth"},
                {"name": "Best10", "data": best10_metric, "opacity": "1.0", "stroke_width": "2", "stroke_curve": "smooth"},
            ]
            text += self.render_chart(series_data, self.base.currency_symbols[1], "Home Cost Prediction", now_str)
        elif chart == "Rates":
            cost_pkwh_today = self.base.prune_today(self.cost_today_hist, prune=False, prune_future=False)
            cost_pkwh_hour = self.base.prune_today(self.cost_hour_hist, prune=False, prune_future=False)
            series_data = [
                {"name": "Import", "data": rates, "opacity": "1.0", "stroke_width": "3", "stroke_curve": "stepline"},
                {"name": "Export", "data": rates_export, "opacity": "0.2", "stroke_width": "2", "stroke_curve": "stepline", "chart_type": "area"},
                {"name": "Gas", "data": rates_gas, "opacity": "0.2", "stroke_width": "2", "stroke_curve": "stepline", "chart_type": "area"},
                {"name": "Hourly p/kWh", "data": cost_pkwh_hour, "opacity": "1.0", "stroke_width": "2", "stroke_curve": "stepline"},
                {"name": "Today p/kWh", "data": cost_pkwh_today, "opacity": "1.0", "stroke_width": "2", "stroke_curve": "stepline"},
            ]
            text += self.render_chart(series_data, self.base.currency_symbols[1], "Energy Rates", now_str)
        elif chart == "InDay":
            load_energy_actual = self.get_entity_results(self.base.prefix + ".load_energy_actual")
            load_energy_predicted = self.get_entity_results(self.base.prefix + ".load_energy_predicted")
            load_energy_adjusted = self.get_entity_results(self.base.prefix + ".load_energy_adjusted")

            series_data = [
                {"name": "Actual", "data": load_energy_actual, "opacity": "1.0", "stroke_width": "2", "stroke_curve": "smooth"},
                {"name": "Predicted", "data": load_energy_predicted, "opacity": "1.0", "stroke_width": "2", "stroke_curve": "smooth"},
                {"name": "Adjusted", "data": load_energy_adjusted, "opacity": "1.0", "stroke_width": "2", "stroke_curve": "smooth"},
            ]
            text += self.render_chart(series_data, "kWh", "In Day Adjustment", now_str)
        elif chart == "PV" or chart == "PV7":
            pv_power = self.base.prune_today(self.pv_power_hist, prune=chart == "PV")
            pv_forecast = self.base.prune_today(self.pv_forecast_hist, prune=chart == "PV", intermediate=True)
            pv_forecastCL = self.base.prune_today(self.pv_forecast_histCL, prune=chart == "PV", intermediate=True)
            pv_today_forecast = self.base.prune_today(self.get_entity_detailedForecast("sensor." + self.base.prefix + "_pv_today", "pv_estimate"), prune=False, intermediate=True)
            pv_today_forecast10 = self.base.prune_today(self.get_entity_detailedForecast("sensor." + self.base.prefix + "_pv_today", "pv_estimate10"), prune=False, intermediate=True)
            pv_today_forecast90 = self.base.prune_today(self.get_entity_detailedForecast("sensor." + self.base.prefix + "_pv_today", "pv_estimate90"), prune=False, intermediate=True)
            pv_today_forecastCL = self.base.prune_today(self.get_entity_detailedForecast("sensor." + self.base.prefix + "_pv_today", "pv_estimateCL"), prune=False, intermediate=True)
            pv_today_forecast.update(self.base.prune_today(self.get_entity_detailedForecast("sensor." + self.base.prefix + "_pv_tomorrow", "pv_estimate"), prune=False, intermediate=True))
            pv_today_forecast10.update(self.base.prune_today(self.get_entity_detailedForecast("sensor." + self.base.prefix + "_pv_tomorrow", "pv_estimate10"), prune=False, intermediate=True))
            pv_today_forecast90.update(self.base.prune_today(self.get_entity_detailedForecast("sensor." + self.base.prefix + "_pv_tomorrow", "pv_estimate90"), prune=False, intermediate=True))
            pv_today_forecastCL.update(self.base.prune_today(self.get_entity_detailedForecast("sensor." + self.base.prefix + "_pv_tomorrow", "pv_estimateCL"), prune=False, intermediate=True))
            series_data = [
                {"name": "PV Power", "data": pv_power, "opacity": "1.0", "stroke_width": "3", "stroke_curve": "smooth", "color": "#f5c43d"},
                {"name": "Forecast History", "data": pv_forecast, "opacity": "0.3", "stroke_width": "3", "stroke_curve": "smooth", "color": "#a8a8a7", "chart_type": "area"},
                {"name": "Forecast History CL", "data": pv_forecastCL, "opacity": "0.3", "stroke_width": "3", "stroke_curve": "smooth", "color": "#e90a0a", "chart_type": "area"},
                {"name": "Forecast", "data": pv_today_forecast, "opacity": "0.3", "stroke_width": "2", "stroke_curve": "smooth", "chart_type": "area", "color": "#a8a8a7"},
                {"name": "Forecast 10%", "data": pv_today_forecast10, "opacity": "0.3", "stroke_width": "2", "stroke_curve": "smooth", "chart_type": "area", "color": "#6b6b6b"},
                {"name": "Forecast 90%", "data": pv_today_forecast90, "opacity": "0.3", "stroke_width": "2", "stroke_curve": "smooth", "chart_type": "area", "color": "#cccccc"},
                {"name": "Forecast CL", "data": pv_today_forecastCL, "opacity": "0.3", "stroke_width": "2", "stroke_curve": "smooth", "chart_type": "area", "color": "#e90a0a"},
            ]
            text += self.render_chart(series_data, "kW", "Solar Forecast", now_str)
        else:
            text += "<br><h2>Unknown chart type</h2>"

        return text

    async def html_charts(self, request):
        """
        Render apps.yaml as an HTML page
        """
        args = request.query
        chart = args.get("chart", "Battery")
        self.default_page = "./charts?chart={}".format(chart)
        text = self.get_header("Predbat Charts", refresh=60 * 5)
        text += "<body>\n"
        text += get_charts_css()

        # Define which chart is active
        active_battery = ""
        active_power = ""
        active_cost = ""
        active_rates = ""
        active_inday = ""
        active_pv = ""
        active_pv7 = ""

        if chart == "Battery":
            active_battery = "active"
        elif chart == "Power":
            active_power = "active"
        elif chart == "Cost":
            active_cost = "active"
        elif chart == "Rates":
            active_rates = "active"
        elif chart == "InDay":
            active_inday = "active"
        elif chart == "PV":
            active_pv = "active"
        elif chart == "PV7":
            active_pv7 = "active"

        text += '<div class="charts-menu">'
        text += "<h3>Charts</h3> "
        text += f'<a href="./charts?chart=Battery" class="{active_battery}">Battery</a>'
        text += f'<a href="./charts?chart=Power" class="{active_power}">Power</a>'
        text += f'<a href="./charts?chart=Cost" class="{active_cost}">Cost</a>'
        text += f'<a href="./charts?chart=Rates" class="{active_rates}">Rates</a>'
        text += f'<a href="./charts?chart=InDay" class="{active_inday}">InDay</a>'
        text += f'<a href="./charts?chart=PV" class="{active_pv}">PV</a>'
        text += f'<a href="./charts?chart=PV7" class="{active_pv7}">PV7</a>'
        text += "</div>"

        text += '<div id="chart"></div>'
        text += self.get_chart(chart=chart)
        text += "</body></html>\n"
        return web.Response(content_type="text/html", text=text)

    def is_editable_value(self, value):
        """
        Check if a value is editable (numerical, boolean, entity strings, or list with editable items)
        """
        if isinstance(value, bool):
            return True
        if isinstance(value, (int, float)):
            return True
        if isinstance(value, str):
            return True
        if isinstance(value, list):
            # A list is editable if it contains any editable items
            return len(value) > 0 and any(self.is_editable_value(item) for item in value)
        return False

    def resolve_value_raw(self, arg, value):
        if isinstance(value, str) and "{" in value:
            text = self.base.resolve_arg(arg, value, indirect=False, quiet=True)
            if text is not None:
                value = text
            return value
        elif isinstance(value, list):
            return [self.resolve_value_raw(arg, item) for item in value]
        elif isinstance(value, dict):
            return {key: self.resolve_value_raw(arg, val) for key, val in value.items()}
        else:
            return value

    async def html_apps(self, request):
        """
        Render apps.yaml as an HTML page with edit functionality
        """
        self.default_page = "./apps"
        text = self.get_header("Predbat Apps.yaml", refresh=60 * 5)

        # all_states has all the HA entities and their states
        all_states_data = self.base.get_state_wrapper()
        all_states = {}
        for entity in all_states_data:
            all_states[entity] = {"state": all_states_data[entity].get("state", ""), "unit_of_measurement": all_states_data[entity].get("attributes", {}).get("unit_of_measurement", "")}

        # Ensure all_states is valid and serializable
        try:
            all_states_json = json.dumps(all_states)
        except (TypeError, ValueError) as e:
            self.base.log(f"Error serializing all_states for web interface: {e}")
            all_states_json = "{}"

        # Add CSS styles for edit functionality
        text += get_apps_css()
        text += "<body>\n"

        # JavaScript for edit functionality
        text += get_apps_js(all_states_json)

        # Add message container
        text += '<div id="messageContainer" class="message-container"></div>\n'

        # Add save controls at the top
        text += """
<div id="saveControls" class="save-controls">
    <div class="save-status">
        <span id="changeCount">No unsaved changes</span>
    </div>
    <button id="saveAllButton" class="save-all-button" onclick="saveAllChanges()" disabled>Save All Changes</button>
    <button id="discardAllButton" class="discard-all-button" onclick="discardAllChanges()" disabled>Discard Changes</button>
</div>
"""

        warning = ""
        if self.base.arg_errors:
            warning = "&#9888;"
        text += "{}<a href='./debug_apps'>apps.yaml</a> - has {} errors<br>\n".format(warning, len(self.base.arg_errors))
        text += "<table>\n"
        text += "<tr><th>Name</th><th>Value</th><th>Actions</th></tr>\n"

        args = self.base.args
        row_id = 0
        # Initialize nested values tracking and row counter
        self._nested_values = {}
        row_counter = [1000]  # Start nested rows at 1000 to avoid conflicts

        for arg in args:
            value = args[arg]
            raw_value = self.resolve_value_raw(arg, value)
            if ("_key" in arg) or ("_password" in arg):
                value = '<span title = "{}"> (hidden)</span>'.format(value)
            arg_errors = self.base.arg_errors.get(arg, "")

            # Determine if this value can be edited
            # Lists should not be editable at the top level - only their individual items
            can_edit = self.is_editable_value(value) and not arg_errors and not isinstance(value, list)

            if arg_errors:
                text += '<tr id="row_{}" data-arg-name="{}" data-original-value="{}"><td bgcolor=#FF7777><span title="{}">&#9888;{}</span></td><td>{}</td><td></td></tr>\n'.format(
                    row_id, arg, html_module.escape(str(raw_value)), arg_errors, arg, self.render_type(arg, value, "", row_counter)
                )
            else:
                actions_cell = ""
                if can_edit:
                    if isinstance(value, bool):
                        # For boolean values, show a toggle button
                        toggle_class = "toggle-button active" if value else "toggle-button"
                        actions_cell = f'<button class="{toggle_class}" onclick="toggleValue({row_id})" data-value="{str(value).lower()}"></button>'
                    else:
                        # For numerical values, show edit button
                        actions_cell = f'<button class="edit-button" onclick="editValue({row_id})">Edit</button>'

                text += '<tr id="row_{}" data-arg-name="{}" data-original-value="{}"><td>{}</td><td id="value_{}">{}</td><td>{}</td></tr>\n'.format(
                    row_id, arg, html_module.escape(str(raw_value)), arg, row_id, self.render_type(arg, value, "", row_counter), actions_cell
                )
            row_id += 1

        args = self.base.unmatched_args
        for arg in args:
            value = args[arg]
            raw_value = self.resolve_value_raw(arg, value)
            text += '<tr id="row_{}" data-arg-name="{}" data-original-value="{}"><td>{}</td><td><span style="background-color:#FFAAAA">{}</span></td><td></td></tr>\n'.format(
                row_id, arg, html_module.escape(str(raw_value)), arg, self.render_type(arg, value, "", row_counter)
            )
            row_id += 1

        text += "</table>"

        text += "</body></html>\n"
        return web.Response(content_type="text/html", text=text)

    def _update_nested_yaml_value(self, data, path, value):
        """
        Update a nested value in YAML data using a dot-notation path
        """
        pre_keys = path.split(".")
        keys = []
        # Split out set of square brackets into a different key
        for key in pre_keys:
            if "[" in key and "]" in key:
                # Handle keys with square brackets, e.g., "battery_charge_low[0]"
                base_key, index = key.split("[")
                index = index.rstrip("]")
                keys.append(base_key)
                keys.append(f"[{index}]")
            else:
                keys.append(key)

        current = data

        # Navigate to the parent of the target value
        for key in keys[:-1]:
            if key.startswith("[") and key.endswith("]"):
                # Handle numerical index in square brackets
                index = int(key[1:-1])
                if not isinstance(current, list) or index >= len(current):
                    raise KeyError(f"Index '{index}' out of range in path '{path}'")
                current = current[index]
            elif key in current:
                current = current[key]
            else:
                raise KeyError(f"Key '{key}' not found in path '{path}'")

        # Set the final value
        key = keys[-1]
        if key.startswith("[") and key.endswith("]"):
            # Handle numerical index in square brackets
            index = int(key[1:-1])
            if not isinstance(current, list) or index >= len(current):
                raise KeyError(f"Index '{index}' out of range in path '{path}'")
            current[index] = value
        elif key in current:
            current[key] = value
        else:
            # If final key is numerical try it as an integer
            if key.isdigit():
                key = int(key)
                if key not in current:
                    raise KeyError(f"Final key '{key}' not found in path '{path}'")
                else:
                    current[key] = value
            else:
                raise KeyError(f"Final key '{key}' not found in path '{path}'")

    async def html_apps_post(self, request):
        """
        Handle POST request for apps page - batch edit values
        """
        try:
            from ruamel.yaml import YAML

            postdata = await request.post()
            changes_json = postdata.get("changes", "")

            if not changes_json:
                return web.json_response({"success": False, "message": "No changes provided"})

            # Parse the changes JSON
            try:
                changes = json.loads(changes_json)
            except json.JSONDecodeError:
                return web.json_response({"success": False, "message": "Invalid changes format"})

            if not changes:
                return web.json_response({"success": False, "message": "No changes to save"})

            # Read and parse the apps.yaml file using ruamel.yaml
            apps_yaml_path = "apps.yaml"
            yaml = YAML()
            yaml.preserve_quotes = True

            try:
                with open(apps_yaml_path, "r") as f:
                    data = yaml.load(f)
            except Exception as e:
                return web.json_response({"success": False, "message": f"Error reading apps.yaml: {str(e)}"})

            # Navigate to the predbat section
            if ROOT_YAML_KEY not in data:
                return web.json_response({"success": False, "message": "pred_bat section not found in apps.yaml"})

            # Process each change
            updated_args = []
            for path_or_arg, change_info in changes.items():
                new_value = change_info["newValue"]
                change_type = change_info.get("type", "numerical")
                is_nested = change_info.get("isNested", False)

                # Convert the new value to appropriate type
                try:
                    if change_type == "boolean":
                        converted_value = new_value.lower() == "true"
                    elif change_type == "string" or change_type == "entity":
                        # Keep as string (includes entity IDs)
                        converted_value = new_value
                    else:
                        # Try to convert to int first, then float (numerical values)
                        if "." in new_value:
                            converted_value = float(new_value)
                        else:
                            converted_value = int(new_value)
                except ValueError:
                    return web.json_response({"success": False, "message": f"Invalid value format for {path_or_arg}: {new_value}"})

                # Update the value in the YAML data
                if is_nested:
                    # Handle nested paths like "battery_charge_low.normal"
                    try:
                        self._update_nested_yaml_value(data[ROOT_YAML_KEY], path_or_arg, converted_value)
                        self._update_nested_yaml_value(self.base.args, path_or_arg, converted_value)
                        updated_args.append(f"{path_or_arg}={converted_value}")
                    except (KeyError, TypeError) as e:
                        return web.json_response({"success": False, "message": f"Path {path_or_arg} not found or invalid: {str(e)}"})
                else:
                    # Handle top-level arguments
                    if path_or_arg in data[ROOT_YAML_KEY]:
                        data[ROOT_YAML_KEY][path_or_arg] = converted_value
                        self.base.args[path_or_arg] = converted_value  # Update the base args as well
                        updated_args.append(f"{path_or_arg}={converted_value}")
                    else:
                        return web.json_response({"success": False, "message": f"Argument {path_or_arg} not found in apps.yaml"})

            # Write back to the file, preserving comments and formatting
            try:
                with open(apps_yaml_path, "w") as f:
                    yaml.dump(data, f)

                change_count = len(updated_args)
                self.log(f"Batch updated {change_count} arguments in apps.yaml: {', '.join(updated_args)}")

                return web.json_response({"success": True, "message": f"Successfully saved {change_count} changes to apps.yaml. Predbat will restart to apply changes."})

            except Exception as e:
                return web.json_response({"success": False, "message": f"Error writing to apps.yaml: {str(e)}"})

        except ImportError:
            return web.json_response({"success": False, "message": "ruamel.yaml library not available, update Predbat add-on first."})
        except Exception as e:
            return web.json_response({"success": False, "message": f"Unexpected error: {str(e)}"})

    def html_config_item_text(self, entity):
        text = ""
        text += "<table>\n"
        text += "<tr><th></th><th>Name</th><th>Entity</th><th>Type</th><th>Current</th><th>Default</th></tr>\n"
        found = False
        for item in self.base.CONFIG_ITEMS:
            if self.base.user_config_item_enabled(item) and item.get("entity") == entity:
                value = item.get("value", "")
                if value is None:
                    value = item.get("default", "")
                friendly = item.get("friendly_name", "")
                itemtype = item.get("type", "")
                default = item.get("default", "")
                icon = self.icon2html(item.get("icon", ""))
                unit = item.get("unit", "")
                text += "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td>".format(icon, friendly, entity, itemtype)
                if value == default:
                    text += '<td class="cfg_default">{} {}</td><td>{} {}</td>\n'.format(value, unit, default, unit)
                else:
                    text += '<td class="cfg_modified">{} {}</td><td>{} {}</td>\n'.format(value, unit, default, unit)
                text += "</tr>\n"
                found = True
        text += "</table>\n"
        if found:
            return text
        else:
            return None

    async def html_config(self, request):
        """
        Return the Predbat config as an HTML page
        """

        self.default_page = "./config"
        text = self.get_header("Predbat Config", refresh=60)
        text += "<body>\n"
        text += get_html_config_css()

        text += """
        <div class="filter-container">
            <label for="configFilter"><strong>Filter settings:</strong></label>
            <input type="text" id="configFilter" class="filter-input" placeholder="Type to filter settings..." oninput="filterConfig()" />
            <button type="button" style="margin-left: 10px; padding: 8px 12px;" onclick="document.getElementById('configFilter').value=''; filterConfig();">Clear</button>
        </div>
        """
        text += '<form class="form-inline" action="./config" method="post" enctype="multipart/form-data" id="configform">\n'
        text += '<table id="configTable">\n'
        text += "<tr><th></th><th>Name</th><th>Entity</th><th>Type</th><th>Current</th><th>Default</th><th>Select</th></tr>\n"

        input_number = """
            <input type="number" id="{}" name="{}" value="{}" min="{}" max="{}" step="{}" onchange="javascript: this.form.submit();">
            """

        for item in self.base.CONFIG_ITEMS:
            if self.base.user_config_item_enabled(item):
                value = item.get("value", "")
                if value is None:
                    value = item.get("default", "")
                entity = item.get("entity")
                if not entity:
                    continue
                friendly = item.get("friendly_name", "")
                itemtype = item.get("type", "")
                default = item.get("default", "")
                useid = entity.replace(".", "__")
                unit = item.get("unit", "")
                icon = self.icon2html(item.get("icon", ""))

                if itemtype == "input_number" and item.get("step", 1) == 1:
                    value = int(value)

                text += '<tr><td>{}</td><td><a href="./entity?entity_id={}">{}</a></td><td>{}</td><td>{}</td>'.format(icon, entity, friendly, entity, itemtype)
                if value == default:
                    text += '<td class="cfg_default">{} {}</td><td>{} {}</td>\n'.format(value, unit, default, unit)
                else:
                    text += '<td class="cfg_modified">{} {}</td><td>{} {}</td>\n'.format(value, unit, default, unit)

                if itemtype == "switch":
                    toggle_class = "toggle-switch active" if value else "toggle-switch"
                    text += f'<td><form style="display: inline;" method="post" action="./config">'
                    text += f'<button class="{toggle_class}" type="button" onclick="toggleSwitch(this, \'{useid}\')"></button>'
                    text += f"</form></td>\n"
                elif itemtype == "input_number":
                    input_number_with_save = input_number.replace('onchange="javascript: this.form.submit();"', 'onchange="saveFilterValue(); this.form.submit();"')
                    text += "<td>{}</td>\n".format(input_number_with_save.format(useid, useid, value, item.get("min", 0), item.get("max", 100), item.get("step", 1)))
                elif itemtype == "select":
                    options = item.get("options", [])
                    if value not in options:
                        options.append(value)
                    text += '<td><select name="{}" id="{}" onchange="saveFilterValue(); this.form.submit();">'.format(useid, useid)
                    for option in options:
                        selected = option == value
                        option_label = option if option else "None"
                        text += '<option value="{}" label="{}" {}>{}</option>'.format(option, option_label, "selected" if selected else "", option)
                    text += "</select></td>\n"
                else:
                    text += "<td>{}</td>\n".format(value)

                text += "</tr>\n"

        text += "</table>"
        text += "</form>"
        text += "</body></html>\n"
        return web.Response(content_type="text/html", text=text)

    async def html_compare_post(self, request):
        """
        Handle post request for html compare
        """
        postdata = await request.post()
        for pitem in postdata:
            if pitem == "run":
                self.log("Starting compare from web page")
                service_data = {}
                service_data["domain"] = "switch"
                service_data["service"] = "turn_on"
                service_data["service_data"] = {"entity_id": "switch.{}_compare_active".format(self.base.prefix)}
                await self.base.trigger_callback(service_data)

        return await self.html_compare(request)

    def to_pounds(self, cost):
        """
        Convert cost to pounds
        """
        res = ""
        if cost:
            # Convert cost into pounds in format
            res = self.base.currency_symbols[0] + "{:.2f}".format(cost / 100.0)
            # Pad with leading spaces to align to 6 characters
            res = res.rjust(6)
            # Convert space to &nbsp;
            res = res.replace(" ", "&nbsp;")

        return res

    async def html_compare(self, request):
        """
        Return the Predbat compare page as an HTML page
        """
        self.default_page = "./compare"

        text = self.get_header("Predbat Compare", refresh=5 * 60)

        text += "<body>\n"

        text += "<style>\n"
        text += "body.dark-mode .comparison-table font { color: #000; }\n"
        text += "</style>\n"

        text += '<form class="form-inline" action="./compare" method="post" enctype="multipart/form-data" id="compareform">\n'
        active = self.base.get_arg("compare_active", False)

        if not active:
            text += '<button type="submit" form="compareform" value="run">Compare now</button>\n'
        else:
            text += '<button type="submit" form="compareform" value="run" disabled>Running..</button>\n'

        text += '<input type="hidden" name="run" value="run">\n'
        text += "</form>"

        text += "<table class='comparison-table'>\n"
        text += "<tr><th>ID</th><th>Name</th><th>Date</th><th>True cost</th><th>Cost</th><th>Cost 10%</th><th>Export</th><th>Import</th><th>Final SOC</th>"
        if self.base.iboost_enable:
            text += "<th>Iboost</th>"
        if self.base.carbon_enable:
            text += "<th>Carbon</th>"
        text += "<th>Result</th>\n"

        compare_list = self.base.get_arg("compare_list", [])

        for compare in compare_list:
            name = compare.get("name", "")
            id = compare.get("id", "")

            if self.base.comparison:
                result = self.base.comparison.get_comparison(id)
            else:
                result = {}

            cost = result.get("cost", "")
            cost10 = result.get("cost10", "")
            metric = result.get("metric", "")
            export = result.get("export_kwh", "")
            imported = result.get("import_kwh", "")
            soc = result.get("soc", "")
            final_iboost = result.get("final_iboost", "")
            final_carbon_g = result.get("final_carbon_g", "")
            date = result.get("date", "")
            best = result.get("best", False)
            existing_tariff = result.get("existing_tariff", False)

            try:
                date_timestamp = datetime.strptime(date, "%Y-%m-%d %H:%M:%S")
                stamp = date_timestamp.strftime(TIME_FORMAT_DAILY)
            except (ValueError, TypeError):
                stamp = None

            # Save current datapoint for today
            if stamp and (id in self.compare_hist):
                if "metric" not in self.compare_hist[id]:
                    self.compare_hist[id]["metric"] = {}
                    self.compare_hist[id]["cost"] = {}
                self.compare_hist[id]["metric"][stamp] = dp2(metric / 100)
                self.compare_hist[id]["cost"][stamp] = dp2(cost / 100.0)

            selected = '<font style="background-color:#FFaaaa;>"> Best </font>' if best else ""
            if existing_tariff:
                selected += '<font style="background-color:#aaFFaa;"> Existing </font>'

            metric_str = self.to_pounds(metric)
            cost_str = self.to_pounds(cost)
            cost10_str = self.to_pounds(cost10)

            text += "<tr><td><a href='#heading-{}'>{}</a></td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td>".format(id, id, name, date, metric_str, cost_str, cost10_str, export, imported, soc)
            if self.base.iboost_enable:
                text += "<td>{}</td>".format(final_iboost)
            if self.base.carbon_enable:
                text += "<td>{}</td>".format(final_carbon_g)
            text += "<td>{}</td></tr>\n".format(selected)

        text += "</table>\n"

        # Charts
        text += '<div id="chart"></div>'
        series_data = []
        for compare in compare_list:
            name = compare.get("name", "")
            id = compare.get("id", "")
            series_data.append({"name": name, "data": self.compare_hist.get(id, {}).get("metric", {}), "chart_type": "bar"})
        series_data.append({"name": "Actual", "data": self.cost_yesterday_hist, "chart_type": "line", "stroke_width": "2"})
        if self.base.car_charging_hold:
            series_data.append({"name": "Actual (no car)", "data": self.cost_yesterday_no_car, "chart_type": "line", "stroke_width": "2"})

        now_str = self.base.now_utc.strftime(TIME_FORMAT)

        if self.compare_hist:
            text += self.render_chart(series_data, self.base.currency_symbols[0], "Tariff Comparison - True cost", now_str, daily_chart=False)
        else:
            text += "<br><h2>Loading chart (please wait)...</h2><br>"

        # HTML Plans
        for compare in compare_list:
            name = compare.get("name", "")
            id = compare.get("id", "")
            if self.base.comparison:
                result = self.base.comparison.get_comparison(id)
            else:
                result = {}

            html = result.get("html", "")

            text += "<br>\n"
            text += "<h2 id='heading-{}'>{}</h2>\n".format(id, name)
            if html:
                text += html
            else:
                text += "<br><p>No data yet....</p>"

        text += "</body></html>\n"
        return web.Response(content_type="text/html", text=text)

    async def html_apps_editor(self, request):
        """
        Return the apps.yaml editor as an HTML page
        """
        self.default_page = "./apps_editor"
        text = self.get_header("Predbat Apps.yaml Editor", refresh=0, codemirror=True)

        # Get success/error messages from URL parameters
        args = request.query
        success_message = urllib.parse.unquote(args.get("success", ""))
        error_message = urllib.parse.unquote(args.get("error", ""))

        # Try to read the current apps.yaml file
        apps_yaml_content = ""
        file_error = ""

        try:
            # apps.yaml will always live in the current directory of the script
            apps_yaml_path = "apps.yaml"
            if apps_yaml_path:
                with open(apps_yaml_path, "r") as f:
                    apps_yaml_content = f.read()
                self.log(f"Successfully loaded apps.yaml from {apps_yaml_path}")
            else:
                file_error = f"Could not find apps.yaml file."

        except Exception as e:
            file_error = f"Error reading apps.yaml: {str(e)}"

        text += get_editor_css()
        text += """

<div class="editor-container">
    <div class="editor-header">
        <h2>Apps.yaml Editor</h2>
        <div id="lintStatus" style="margin-top: 8px;"></div>
"""

        text += """
    </div>

    <form id="editorForm" class="editor-form" method="post" action="./apps_editor">
        <!-- We use a regular textarea that CodeMirror will replace -->
        <textarea class="editor-textarea" name="apps_content" id="appsContent" placeholder="Loading apps.yaml content...">"""

        # Escape the content for HTML

        text += html_module.escape(apps_yaml_content)

        text += """</textarea>

        <div class="editor-controls">
            <button type="submit" class="save-button" id="saveButton" title="Click to save changes">Save</button>
            <button type="button" class="revert-button" id="revertButton" title="Discard changes and reload from disk">Revert</button>
            <span id="saveStatus"></span>
        </div>
    </form>

    <div id="messageContainer">"""

        # Show success message if present
        if success_message:
            text += f'<div class="message success" style="display: block;">{html_module.escape(success_message)}</div>'

        # Show error message if present (from URL or file reading)
        display_error = error_message or file_error
        if display_error:
            text += f'<div class="message error" style="display: block;">{html_module.escape(display_error)}</div>'

        text += """
    </div>
</div>"""
        text += get_editor_js()
        text += """
</div>"""

        return web.Response(content_type="text/html", text=text)

    async def html_apps_editor_post(self, request):
        """
        Handle POST request for apps.yaml editor - save the file
        """
        try:
            postdata = await request.post()
            apps_content = postdata.get("apps_content", "")

            # Remove dos line endings
            apps_content = apps_content.replace("\r\n", "\n").replace("\r", "\n")

            # Find the apps.yaml file path
            apps_yaml_path = "apps.yaml"
            # Create backup
            backup_path = "apps.yaml.backup"
            shutil.copy2(apps_yaml_path, backup_path)

            # Save the new content
            with open(apps_yaml_path, "w") as f:
                f.write(apps_content)

            self.log(f"Apps.yaml successfully saved to {apps_yaml_path}")
            if backup_path:
                self.log(f"Backup created at {backup_path}")

            # Redirect back to editor with success message
            import urllib.parse

            success_message = f"Apps.yaml saved successfully. Backup created at {backup_path}."
            encoded_message = urllib.parse.quote(success_message)
            raise web.HTTPFound(f"./apps_editor?success={encoded_message}")

        except web.HTTPFound:
            raise  # Re-raise HTTP redirects
        except Exception as e:
            error_msg = f"Failed to save apps.yaml: {str(e)}"
            self.log(f"ERROR: {error_msg}")
            import urllib.parse

            encoded_error = urllib.parse.quote(error_msg)
            raise web.HTTPFound(f"./apps_editor?error={encoded_error}")

    async def html_default(self, request):
        """
        Redirect to the default page
        """
        return web.HTTPFound(self.default_page)

    def get_grid_power_icon(self):
        """
        Returns a visual indicator showing if the grid is importing or exporting power
        """
        if not self.base.dashboard_index:
            return ""

        power = self.base.grid_power
        if power >= 10:
            icon_text = "transmission-tower-export"
        elif power <= -10:
            icon_text = "transmission-tower-import"
        else:
            icon_text = "transmission-tower-outline"

        text = '<span class="mdi mdi-{}"></span>'.format(icon_text)
        text += str(dp0(power)) + " W"

        return text

    def get_battery_power_icon(self):
        """
        Returns a visual indicator showing if the battery is charging or discharging
        """
        if not self.base.dashboard_index:
            return ""

        power = self.base.battery_power
        if power >= 10:
            icon_text = "battery-arrow-down"
        elif power <= -10:
            icon_text = "battery-arrow-up"
        else:
            icon_text = "battery-outline"

        text = '<span class="mdi mdi-{}"></span>'.format(icon_text)
        text += str(dp0(power)) + " W"

        return text

    def get_pv_power_icon(self):
        """
        Returns a visual indicator showing if the PV is producing power
        """
        if not self.base.dashboard_index:
            return ""

        power = self.base.pv_power
        if power > 0:
            icon_text = "solar-power"
        else:
            icon_text = "solar-power-outline"

        text = '<span class="mdi mdi-{}"></span>'.format(icon_text)
        text += str(dp0(power)) + " W"

        return text

    def get_battery_status_icon(self):
        """
        Returns a visual indicator showing if the battery is charging or exporting
        """
        if not self.base.dashboard_index:
            return '<span class="mdi mdi-battery-sync"></span>'

        percent = calc_percent_limit(self.base.soc_kw, self.base.soc_max)
        percent_rounded_to_nearest_10 = round(float(percent) / 10) * 10
        if self.base.isCharging:
            if percent_rounded_to_nearest_10 == 0:
                icon_text = "battery-charging-outline"
            else:
                icon_text = "battery-charging-{}".format(percent_rounded_to_nearest_10)
        else:
            if percent_rounded_to_nearest_10 == 0:
                icon_text = "battery-outline"
            elif percent_rounded_to_nearest_10 == 100:
                icon_text = "battery"
            else:
                icon_text = "battery-{}".format(percent_rounded_to_nearest_10)

        text = '<span class="mdi mdi-{}"></span>'.format(icon_text)
        text += str(dp2(percent)) + "%"

        if self.base.isExporting:
            text += '<span class="mdi mdi-transmission-tower-export"></span>'
        return text

    async def html_api_login(self, request):
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)

        token = data.get("token")
        redirect_url = data.get("redirect", "/")

        if not token:
            return web.json_response({"error": "Missing 'token'"}, status=400)

        # Extract domain from Host header
        host = request.headers.get("Host", "")
        domain = host.split(":")[0]  # Remove port if present

        if not domain:
            return web.json_response({"error": "Could not determine request domain"}, status=400)

        response = web.HTTPFound(location=redirect_url)
        response.set_cookie(name="access-token", value=token, path="/", domain=domain, secure=True, httponly=True, samesite="Lax", max_age=60 * 60 * 24 * 7)  # 7 days

        return response

    def get_override_time_from_string(self, time_str):
        """
        Convert a time string like "Sun 13:00" into a datetime object
        """
        now_utc = self.base.now_utc
        # Parse the time string into a datetime object
        # Format is Sun 13:00
        try:
            override_time = datetime.strptime(time_str, "%a %H:%M")
        except ValueError:
            override_time = now_utc

        # Convert day of week text to a number (0=Monday, 6=Sunday)
        day_of_week_text = time_str.split()[0].lower()
        day_of_week = DAY_OF_WEEK_MAP.get(day_of_week_text, 0)
        day_of_week_today = now_utc.weekday()

        override_time = now_utc.replace(hour=override_time.hour, minute=override_time.minute, second=0, microsecond=0)
        add_days = day_of_week - day_of_week_today
        if add_days < 0:
            add_days += 7
        override_time += timedelta(days=add_days)
        return override_time

    async def html_rate_override(self, request):
        """
        Handle POST request for rate overrides
        """
        try:
            # Parse form data
            data = await request.post()
            time_str = data.get("time")
            action = data.get("action")
            rate = data.get("rate")

            try:
                rate = float(rate)
            except (TypeError, ValueError):
                rate = 0.0

            # Log the override request
            self.log(f"Rate override requested: {action} at {time_str} value {rate}")

            # Validate inputs
            if not time_str or not action:
                self.log("ERROR: Missing required parameters for override")
                return web.json_response({"success": False, "message": "Missing required parameters"}, status=400)

            now_utc = self.base.now_utc
            override_time = self.get_override_time_from_string(time_str)

            minutes_from_now = (override_time - now_utc).total_seconds() / 60
            if minutes_from_now >= 17 * 60:
                return web.json_response({"success": False, "message": "Override time must be within 17 hours from now."}, status=400)

            selection_option = "{}={}".format(override_time.strftime("%H:%M:%S"), rate)
            clear_option = "[{}={}]".format(override_time.strftime("%H:%M:%S"), rate)
            if action == "Clear Import":
                await self.base.async_manual_select("manual_import_rates", clear_option)
            elif action == "Set Import":
                item = self.base.config_index.get("manual_import_value", {})
                await self.base.ha_interface.set_state_external(item.get("entity", None), rate)
                await self.base.async_manual_select("manual_import_rates", selection_option)
            elif action == "Clear Export":
                await self.base.async_manual_select("manual_export_rates", clear_option)
            elif action == "Set Export":
                item = self.base.config_index.get("manual_export_value", {})
                await self.base.ha_interface.set_state_external(item.get("entity", None), rate)
                await self.base.async_manual_select("manual_export_rates", selection_option)
            elif action == "Set Load":
                item = self.base.config_index.get("manual_load_value", {})
                await self.base.ha_interface.set_state_external(item.get("entity", None), rate)
                await self.base.async_manual_select("manual_load_adjust", selection_option)
            elif action == "Clear Load":
                await self.base.async_manual_select("manual_load_adjust", clear_option)
            else:
                self.log("ERROR: Unknown action for rate override")
                return web.json_response({"success": False, "message": "Unknown action"}, status=400)

            # Refresh plan
            self.base.update_pending = True
            self.base.plan_valid = False

            # Return html plan again
            self.log("Rate override processed successfully")
            return web.json_response({"success": True}, status=200)

        except Exception as e:
            self.log(f"ERROR: Failed to process rate override: {str(e)}")
            return web.json_response({"success": False, "message": str(e)}, status=500)

    async def html_plan_override(self, request):
        """
        Handle POST request for plan overrides
        """
        try:
            # Parse form data
            data = await request.post()
            time_str = data.get("time")
            action = data.get("action")

            # Log the override request
            self.log(f"Plan override requested: {action} at {time_str}")

            # Validate inputs
            if not time_str or not action:
                return web.json_response({"success": False, "message": "Missing required parameters"}, status=400)

            now_utc = self.base.now_utc
            override_time = self.get_override_time_from_string(time_str)

            minutes_from_now = (override_time - now_utc).total_seconds() / 60
            if minutes_from_now >= 17 * 60:
                return web.json_response({"success": False, "message": "Override time must be within 17 hours from now."}, status=400)

            selection_option = "{}".format(override_time.strftime("%H:%M:%S"))
            clear_option = "[{}]".format(override_time.strftime("%H:%M:%S"))
            if action == "Clear":
                await self.base.async_manual_select("manual_demand", selection_option)
                await self.base.async_manual_select("manual_demand", clear_option)
            else:
                if action == "Manual Demand":
                    await self.base.async_manual_select("manual_demand", selection_option)
                elif action == "Manual Charge":
                    await self.base.async_manual_select("manual_charge", selection_option)
                elif action == "Manual Export":
                    await self.base.async_manual_select("manual_export", selection_option)
                elif action == "Manual Freeze Charge":
                    await self.base.async_manual_select("manual_freeze_charge", selection_option)
                elif action == "Manual Freeze Export":
                    await self.base.async_manual_select("manual_freeze_export", selection_option)
                else:
                    return web.json_response({"success": False, "message": "Unknown action"}, status=400)

            # Refresh plan
            self.base.update_pending = True
            self.base.plan_valid = False

            # Return html plan again
            return web.json_response({"success": True}, status=200)

        except Exception as e:
            self.log(f"ERROR: Failed to process plan override: {str(e)}")
            return web.json_response({"success": False, "message": str(e)}, status=500)

    def count_entities_matching_filter(self, event_filter):
        """
        Count the number of entities that match the given event filter pattern
        """
        count = 0
        try:
            # Get all entities from Home Assistant
            all_entities = self.base.get_state_wrapper()
            if all_entities:
                for entity_id in all_entities.keys():
                    if event_filter in entity_id:
                        count += 1
        except Exception as e:
            self.log(f"Error counting entities for filter '{event_filter}': {e}")
        return count

    async def html_components(self, request):
        """
        Return the Components view as an HTML page showing status of all components
        """
        self.default_page = "./components"
        text = self.get_header("Predbat Components", refresh=60)
        text += "<body>\n"
        text += get_components_css()

        text += "<h2>Component Status</h2>\n"
        text += "<div class='components-grid'>\n"

        # Get all component information
        all_components = self.base.components.get_all()
        active_components = self.base.components.get_active()

        for component_name in all_components:
            from components import COMPONENT_LIST

            component_info = COMPONENT_LIST.get(component_name, {})
            component = self.base.components.get_component(component_name)
            is_alive = self.base.components.is_alive(component_name)
            is_active = component_name in active_components

            # Create component card
            text += f'<div class="component-card {"active" if is_active else "inactive"}">\n'
            text += f'<div class="component-header">\n'
            text += f'<h3>{component_info.get("name", component_name)}</h3>\n'

            # Status indicator
            if is_active and is_alive:
                text += '<span class="status-indicator status-healthy">●</span><span class="status-text">Active</span>\n'
            elif is_active and not is_alive:
                text += '<span class="status-indicator status-error">●</span><span class="status-text">Error</span>\n'
            else:
                text += '<span class="status-indicator status-inactive">●</span><span class="status-text">Disabled</span>\n'

            text += f"</div>\n"

            # Component details
            text += f'<div class="component-details">\n'

            # Show args and their current values
            args_info = component_info.get("args", {})
            if args_info:
                text += f'<div class="component-args">\n'
                text += f"<h4>Configuration:</h4>\n"
                text += f'<table class="args-table">\n'
                text += f"<tr><th>Setting</th><th>Required</th><th>Current Value</th></tr>\n"

                for arg_name, arg_info in args_info.items():
                    required = arg_info.get("required", False)
                    config_key = arg_info.get("config", "")
                    default = arg_info.get("default", "")

                    # Get current value
                    current_value = self.base.get_arg(config_key, default, indirect=False)

                    # Hide sensitive values
                    display_value = current_value
                    if any(sensitive in arg_name.lower() for sensitive in ["password", "key", "secret", "token"]):
                        if current_value:
                            display_value = "***configured***"
                        else:
                            display_value = "***not set***"
                    elif current_value is None:
                        display_value = "Not set"
                    elif current_value == "":
                        display_value = "Empty"

                    required_text = "Yes" if required else "No"
                    text += f'<tr class="{"required-arg" if required else "optional-arg"}">\n'
                    text += f"<td>{config_key}</td>\n"
                    text += f"<td>{required_text}</td>\n"
                    text += f"<td>{display_value}</td>\n"
                    text += f"</tr>\n"

                text += f"</table>\n"
                text += f"</div>\n"

            # Event filter info - count matching entities
            event_filter = component_info.get("event_filter", "")
            if event_filter:
                # Count entities that match the filter
                entity_count = self.count_entities_matching_filter(event_filter)
                count_class = "entity-count-zero" if entity_count == 0 else "entity-count-positive"
                text += f'<p><strong>Entities:</strong> <span class="{count_class}">num_entities: {entity_count}</span></p>\n'

            text += f"</div>\n"
            text += f"</div>\n"

        text += "</div>\n"
        text += "</body></html>\n"
        return web.Response(content_type="text/html", text=text)

    async def html_restart(self, request):
        """
        Handle restart request by setting fatal_error to trigger restart
        """
        try:
            self.log("Restart requested from web interface")
            self.base.fatal_error = True
            return web.json_response({"success": True, "message": "Restart initiated"})
        except Exception as e:
            self.log(f"ERROR: Failed to initiate restart: {str(e)}")
            return web.json_response({"success": False, "message": str(e)}, status=500)
