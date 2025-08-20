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

from utils import calc_percent_limit, str2time, dp0, dp2
from config import TIME_FORMAT, TIME_FORMAT_DAILY
from predbat import THIS_VERSION
import urllib.parse

DAY_OF_WEEK_MAP = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
ROOT_YAML_KEY = "pred_bat"


class WebInterface:
    def __init__(self, base) -> None:
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
        self.web_port = self.base.get_arg("web_port", 5052)
        self.default_log = "warnings"

        # Plugin registration system
        self.registered_endpoints = []

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
        app.router.add_post("/api/login", self.html_api_login)

        # Notify plugin system that web interface is ready
        if hasattr(self.base, 'plugin_system') and self.base.plugin_system:
          self.base.plugin_system.call_hooks('on_web_start')

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
        while not self.abort:
            await asyncio.sleep(1)
        await runner.cleanup()
        print("Web interface stopped")

    async def stop(self):
        print("Web interface stop called")
        self.abort = True
        await asyncio.sleep(1)

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
            text += "<tr><td colspan='2' bgcolor='#ff7777'>Predbat has errors</td></tr>\n"
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

    def get_header(self, title, refresh=0):
        """
        Return the HTML header for a page
        """
        calculating = self.base.get_arg("active", False)
        if self.base.update_pending:
            calculating = True

        text = '<!doctype html><html><head><meta charset="utf-8"><title>Predbat Web Interface</title>'

        text += """
    <link href="https://cdn.jsdelivr.net/npm/@mdi/font@7.4.47/css/materialdesignicons.min.css" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/apexcharts"></script>
    <style>
        body, html {
            margin: 0;
            padding: 0;
            height: 100%;
            border: 2px solid #ffffff;
        }
        body {
            font-family: Arial, sans-serif;
            text-align: left;
            margin: 5px;
            background-color: #ffffff;
            color: #333;
        }
        h1 {
            color: #4CAF50;
        }
        h2 {
            color: #4CAF50;
            display: inline
        }
        p {
            white-space: nowrap;
        }
        table {
            padding: 1px;
            border: 2px solid green;
            border-spacing: 2px;
            background-clip: padding-box;
        }
        th,
        td {
            text-align: left;
            padding: 5px;
            vertical-align: top;
        }
        th {
            background-color: #4CAF50;
            color: white;
        }
        .default, .cfg_default {
            background-color: #ffffff;
        }
        .modified, .cfg_modified {
            background-color: #ffcccc;
        }

        /* Apply dark mode to html element as well for earlier styling */
        html.dark-mode,
        body.dark-mode {
            background-color: #121212;
            color: #e0e0e0;
            border: 2px solid #121212;
        }
        body.dark-mode table {
            border-color: #333;
        }
        body.dark-mode th {
            background-color: #333;
            color: #e0e0e0;
        }
        body.dark-mode .default,
        body.dark-mode .cfg_default {
            background-color: #121212;
        }
        body.dark-mode .modified,
        body.dark-mode .cfg_modified {
            background-color: #662222;
        }
        /* Dark mode link styles */
        body.dark-mode a {
            color: #8cb4ff;
        }
        body.dark-mode a:visited {
            color: #c58cff;
        }
        body.dark-mode a:hover {
            color: #afd2ff;
        }
        /* Dark mode chart styles */
        body.dark-mode .apexcharts-legend-text {
            color: #ffffff !important;
        }
        body.dark-mode .apexcharts-title-text {
            fill: #ffffff !important;
        }
        body.dark-mode .apexcharts-xaxis-label,
        body.dark-mode .apexcharts-yaxis-label {
            fill: #e0e0e0 !important;
        }
        /* Dark mode tooltip styles */
        body.dark-mode .apexcharts-tooltip {
            background: #333 !important;
            color: #e0e0e0 !important;
            border: 1px solid #555 !important;
        }
        body.dark-mode .apexcharts-tooltip-title {
            background: #444 !important;
            color: #e0e0e0 !important;
            border-bottom: 1px solid #555 !important;
        }
        body.dark-mode .apexcharts-tooltip-series-group {
            border-bottom: 1px solid #555 !important;
        }
        body.dark-mode .apexcharts-tooltip-text {
            color: #e0e0e0 !important;
        }
        body.dark-mode .apexcharts-tooltip-text-y-value,
        body.dark-mode .apexcharts-tooltip-text-goals-value,
        body.dark-mode .apexcharts-tooltip-text-z-value {
            color: #e0e0e0 !important;
        }
        body.dark-mode .apexcharts-tooltip-marker {
            box-shadow: 0 0 0 1px #444 !important;
        }
        body.dark-mode .apexcharts-tooltip-text-label {
            color: #afd2ff !important;
        }
        body.dark-mode .apexcharts-xaxistooltip,
        body.dark-mode .apexcharts-yaxistooltip {
            background: #333 !important;
            color: #e0e0e0 !important;
            border: 1px solid #555 !important;
        }
        body.dark-mode .apexcharts-xaxistooltip-text,
        body.dark-mode .apexcharts-yaxistooltip-text {
            color: #e0e0e0 !important;
        }

        /* Dark mode styles for restart button */
        body.dark-mode button {
            background-color: #cc3333 !important;
            color: #e0e0e0 !important;
        }

        body.dark-mode button:hover {
            background-color: #aa2222 !important;
        }

        /* Dashboard form controls styling */
        .dashboard-select {
            border: 1px solid #ccc;
            padding: 2px 4px;
            border-radius: 3px;
            background-color: #fff;
            color: #333;
            font-size: 14px;
        }

        /* Toggle switch styles */
        .toggle-switch {
            position: relative;
            display: inline-block;
            width: 60px;
            height: 24px;
            background-color: #ccc;
            border-radius: 12px;
            cursor: pointer;
            transition: background-color 0.3s;
            border: none;
            outline: none;
            margin-right: 5px;
            vertical-align: middle;
        }

        .toggle-switch.active {
            background-color: #f44336;
        }

        .toggle-switch::before {
            content: '';
            position: absolute;
            top: 2px;
            left: 2px;
            width: 20px;
            height: 20px;
            background-color: white;
            border-radius: 50%;
            transition: transform 0.3s;
            box-shadow: 0 2px 4px rgba(0,0,0,0.2);
        }

        .toggle-switch.active::before {
            transform: translateX(36px);
        }

        .toggle-switch:hover {
            opacity: 0.8;
        }

        /* Dark mode form controls */
        body.dark-mode .dashboard-select {
            background-color: #444;
            color: #e0e0e0;
            border-color: #666;
        }

        body.dark-mode .dashboard-select:focus {
            border-color: #4CAF50;
        }

        /* Dark mode toggle switch styles */
        body.dark-mode .toggle-switch {
            background-color: #555 !important;
        }

        body.dark-mode .toggle-switch.active {
            background-color: #f44336 !important;
        }

        body.dark-mode .toggle-switch::before {
            background-color: #e0e0e0;
        }

        .menu-bar {
            background-color: #ffffff;
            overflow-x: auto; /* Enable horizontal scrolling */
            white-space: nowrap; /* Prevent menu items from wrapping */
            display: flex;
            align-items: center;
            border-bottom: 1px solid #ddd;
            -webkit-overflow-scrolling: touch; /* Smooth scrolling on iOS */
            scrollbar-width: thin; /* Firefox */
            scrollbar-color: #4CAF50 #f0f0f0; /* Firefox */
            position: fixed; /* Change from sticky to fixed */
            top: 0; /* Stick to the top */
            left: 0; /* Ensure it starts from the left edge */
            right: 0; /* Ensure it extends to the right edge */
            width: 100%; /* Make sure it spans the full width */
            z-index: 1000; /* Ensure it's above other content */
            box-shadow: 0 2px 5px rgba(0, 0, 0, 0.1); /* Add subtle shadow for visual separation */
        }

        /* Add padding to body to prevent content from hiding under fixed header */
        body {
            padding-top: 65px; /* Increased padding to account for the fixed menu height */
        }

        .battery-wrapper {
            display: flex;
            align-items: center;
            margin-left: 10px;
        }

        /* Flying bat animation */
        @keyframes flyAcross {
            0% {
                left: 10px;
                top: 30px;
                transform: translateY(0) scale(1.5);
            }
            25% {
                left: 25%;
                top: 65%;
                transform: translateY(0) scale(2.0) rotate(45deg);
            }
            50% {
                left: 50%;
                top: 30px;
                transform: translateY(0) scale(1.5) rotate(-45deg);
            }
            75% {
                left: 75%;
                top: 65%;
                transform: translateY(0) scale(2.0) rotate(45deg);
            }
            100% {
                left: 100%;
                top: 30px;
                transform: translateY(0) scale(1.5);
            }
        }

        .flying-bat {
            position: fixed;
            z-index: 9999;
            width: 60px;
            height: 60px;
            background-size: contain;
            background-repeat: no-repeat;
            background-position: center;
            pointer-events: none;
            animation: flyAcross 3s linear forwards;
        }

    </style>
    <script>
    // Check and apply the saved dark mode preference on page load
    window.onload = function() {
        applyDarkMode();
    };
    function applyDarkMode() {
        const darkModeEnabled = localStorage.getItem('darkMode') === 'true';
        if (darkModeEnabled) {
            document.body.classList.add('dark-mode');
            document.documentElement.classList.add('dark-mode');
        }
        else {
            document.body.classList.remove('dark-mode');
            document.documentElement.classList.remove('dark-mode');
        }

        // Update logo image source based on dark mode
        const logoImage = document.getElementById('logo-image');
        if (logoImage) {
            if (darkModeEnabled) {
                logoImage.src = logoImage.getAttribute('data-dark-src');
            } else {
                logoImage.src = logoImage.getAttribute('data-light-src');
            }
        }
    };

    function toggleDarkMode() {
        const isDarkMode = document.body.classList.toggle('dark-mode');
        localStorage.setItem('darkMode', isDarkMode);
        // Force reload to apply dark mode styles
        location.reload();
    }

    function flyBat() {
        // Remove any existing flying bats
        document.querySelectorAll('.flying-bat').forEach(bat => bat.remove());

        // Create a new bat element
        const bat = document.createElement('div');
        bat.className = 'flying-bat';

        // Get the appropriate bat image based on dark/light mode
        const isDarkMode = document.body.classList.contains('dark-mode');
        const batImage = isDarkMode
            ? 'https://raw.githubusercontent.com/springfall2008/batpred/refs/heads/main/docs/images/bat_logo_dark.png'
            : 'https://raw.githubusercontent.com/springfall2008/batpred/refs/heads/main/docs/images/bat_logo_light.png';

        bat.style.backgroundImage = `url('${batImage}')`;

        // Add to document
        document.body.appendChild(bat);

        // Remove after animation completes
        setTimeout(() => {
            bat.remove();
        }, 4100);  // Slightly longer than the animation duration
    }

    function restartPredbat() {
        if (confirm('Are you sure you want to restart Predbat?')) {
            fetch('./restart', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                }
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    alert('Restart initiated. Predbat will restart shortly.');
                    // Reload the page after a short delay to show the restart status
                    setTimeout(() => {
                        window.location.reload();
                    }, 2000);
                } else {
                    alert('Error initiating restart: ' + (data.message || 'Unknown error'));
                }
            })
            .catch(error => {
                console.error('Error:', error);
                alert('Error initiating restart: ' + error.message);
            });
        }
    }

    function toggleSwitch(element, fieldName) {
        // Toggle the active class
        element.classList.toggle('active');

        // Determine the new value
        const isActive = element.classList.contains('active');
        const newValue = isActive ? 'on' : 'off';

        // Find the associated form and hidden input
        const form = element.closest('form');
        if (form) {
            // Create or update the hidden input for the value
            let hiddenInput = form.querySelector('input[name="' + fieldName + '"]');
            if (!hiddenInput) {
                hiddenInput = document.createElement('input');
                hiddenInput.type = 'hidden';
                hiddenInput.name = fieldName;
                form.appendChild(hiddenInput);
            }
            hiddenInput.value = newValue;

            // If saveFilterValue function exists (config page), call it
            if (typeof saveFilterValue === 'function') {
                saveFilterValue();
            }

            // Submit the form
            form.submit();
        }
    }
    </script>
    """

        if refresh:
            text += '<meta http-equiv="refresh" content="{}" >'.format(refresh)
        text += "</head><body>"
        text += self.get_menu_html(calculating)
        return text

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
        text += """<body>
        <style>
        .dropdown {
            position: relative;
            display: inline-block;
        }

        .dropdown-content {
            display: none;
            position: absolute;
            background-color: #f9f9f9;
            min-width: 160px;
            box-shadow: 0px 8px 16px 0px rgba(0,0,0,0.2);
            z-index: 1;
            border-radius: 4px;
        }

        .dropdown-content a {
            color: black;
            padding: 12px 16px;
            text-decoration: none;
            display: block;
            cursor: pointer;
        }

        .dropdown-content a:hover {
            background-color: #f1f1f1;
        }

        .clickable-time-cell {
            cursor: pointer;
            position: relative;
            transition: background-color 0.2s;
        }

        .clickable-time-cell:hover {
            background-color: #f5f5f5 !important;
        }

        /* Dark mode styles */
        body.dark-mode .dropdown-content {
            background-color: #333;
            box-shadow: 0px 8px 16px 0px rgba(0,0,0,0.5);
        }

        body.dark-mode .dropdown-content a {
            color: #e0e0e0;
        }

        body.dark-mode .dropdown-content a:hover {
            background-color: #444;
        }

        body.dark-mode .clickable-time-cell:hover {
            background-color: #444 !important;
        }

        /* Override cell styling */
        .override-active {
            position: relative;
        }

        body.dark-mode .override-active {
            background-color: #93264c !important; /* Darker pink for dark mode */
        }

        /* Rate input field styles */
        .dropdown-content input[type="number"] {
            background-color: #fff;
            color: #333;
            border: 1px solid #ccc;
        }

        .dropdown-content input[type="number"]:focus {
            outline: none;
            border-color: #4CAF50;
        }

        .dropdown-content button {
            background-color: #4CAF50;
            color: white;
            border: none;
            cursor: pointer;
        }

        .dropdown-content button:hover {
            background-color: #45a049;
        }

        /* Dark mode styles for input and button */
        body.dark-mode .dropdown-content input[type="number"] {
            background-color: #444;
            color: #e0e0e0;
            border-color: #666;
        }

        body.dark-mode .dropdown-content input[type="number"]:focus {
            border-color: #4CAF50;
        }

        body.dark-mode .dropdown-content button {
            background-color: #4CAF50;
            color: white;
        }

        body.dark-mode .dropdown-content button:hover {
            background-color: #45a049;
        }
        </style>

        <script>
        // Close all dropdown menus
        function closeDropdowns() {
            var dropdowns = document.getElementsByClassName("dropdown-content");
            for (var i = 0; i < dropdowns.length; i++) {
                if (dropdowns[i].style.display === "block") {
                    dropdowns[i].style.display = "none";
                }
            }
        }

        // Toggle dropdown menu
        function toggleForceDropdown(id) {
            closeDropdowns();
            var dropdown = document.getElementById(id);
            if (dropdown.style.display === "block") {
                dropdown.style.display = "none";
            } else {
                dropdown.style.display = "block";
            }
        }

        // Handle rate override option function
        function handleRateOverride(time, rate, action, clear) {
            console.log("Rate override:", time, "Rate:", rate, "Action:", action);
            // Create a form data object to send the override parameters
            const formData = new FormData();
            formData.append('time', time);
            formData.append('rate', rate);
            formData.append('action', action);
            // Send the override request to the server
            fetch('./rate_override', {
                method: 'POST',
                body: formData
            })
            .then(response => {
                if (response.ok) {
                    return response.json();
                }
                throw new Error('Failed to set rate override');
            })
            .then(data => {
                if (data.success) {
                    // Show success message
                    const messageElement = document.createElement('div');
                    if (clear) {
                        messageElement.textContent = `Manual rate cleared for ${time}`;
                    } else {
                        messageElement.textContent = `Rate set to ${rate}p/kWh for ${time}`;
                    }
                    messageElement.style.position = 'fixed';
                    messageElement.style.top = '65px';
                    messageElement.style.right = '10px';
                    messageElement.style.padding = '10px';
                    messageElement.style.backgroundColor = '#4CAF50';
                    messageElement.style.color = 'white';
                    messageElement.style.borderRadius = '4px';
                    messageElement.style.zIndex = '1000';
                    document.body.appendChild(messageElement);

                    // Auto-remove message after 3 seconds
                    setTimeout(() => {
                        messageElement.style.opacity = '0';
                        messageElement.style.transition = 'opacity 0.5s';
                        setTimeout(() => messageElement.remove(), 500);
                    }, 3000);

                    // Reload the page to show the updated plan
                    setTimeout(() => location.reload(), 1000);
                } else {
                    alert('Error setting import override: ' + (data.message || 'Unknown error'));
                }
            })
            .catch(error => {
                console.error('Error:', error);
                alert('Error setting rate override: ' + (error.message || 'Unknown error'));
            });
            // Close dropdown after selection
            closeDropdowns();
        }

        // Handle option selection
        function handleTimeOverride(time, action) {
            console.log("Time override:", time, "Action:", action);

            // Create a form data object to send the override parameters
            const formData = new FormData();
            formData.append('time', time);
            formData.append('action', action);

            // Send the override request to the server
            fetch('./plan_override', {
                method: 'POST',
                body: formData
            })
            .then(response => {
                if (response.ok) {
                    return response.json();
                }
                throw new Error('Failed to set plan override');
            })
            .then(data => {
                if (data.success) {
                    // Show success message
                    const messageElement = document.createElement('div');
                    messageElement.textContent = `${action} override set for ${time}`;
                    messageElement.style.position = 'fixed';
                    messageElement.style.top = '65px';
                    messageElement.style.right = '10px';
                    messageElement.style.padding = '10px';
                    messageElement.style.backgroundColor = '#4CAF50';
                    messageElement.style.color = 'white';
                    messageElement.style.borderRadius = '4px';
                    messageElement.style.zIndex = '1000';
                    document.body.appendChild(messageElement);

                    // Auto-remove message after 3 seconds
                    setTimeout(() => {
                        messageElement.style.opacity = '0';
                        messageElement.style.transition = 'opacity 0.5s';
                        setTimeout(() => messageElement.remove(), 500);
                    }, 3000);

                    // Reload the page to show the updated plan
                    setTimeout(() => location.reload(), 1000);
                } else {
                    alert('Error setting override: ' + (data.message || 'Unknown error'));
                }
            })
            .catch(error => {
                console.error('Error:', error);
                alert('Error setting override: ' + error.message);
            });

            // Close dropdown after selection
            closeDropdowns();
        }

        // Close dropdowns when clicking outside
        document.addEventListener("click", function(event) {
            if (!event.target.matches('.clickable-time-cell') && !event.target.closest('.dropdown-content')) {
                closeDropdowns();
            }
        });
        </script>
        """

        # Process HTML table to add buttons to time cells
        html_plan = self.base.html_plan

        # Regular expression to find time cells in the table
        time_pattern = r"<td id=time.*?>((?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) \d{2}:\d{2})</td>"
        import_pattern = r"<td id=import data-minute=(\S+) data-rate=(\S+)(.*?)>(.*?)</td>"
        export_pattern = r"<td id=export data-minute=(\S+) data-rate=(\S+)(.*?)>(.*?)</td>"

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
            if minutes_from_midnight in manual_all_times:
                in_override = True
                cell_bg_color = "#FFC0CB"  # Pink background for cells with active overrides
                override_class = "override-active"

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
                default_rate = self.base.get_arg("manual_import_value") if is_import else self.base.get_arg("manual_export_value")
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

        # Process the HTML plan to add buttons to time cells
        processed_html = re.sub(time_pattern, add_button_to_time, html_plan)
        processed_html = re.sub(import_pattern, add_button_to_import, processed_html)
        processed_html = re.sub(export_pattern, add_button_to_export, processed_html)

        text += processed_html + "</body></html>\n"
        return web.Response(content_type="text/html", text=text)

    async def html_log(self, request):
        """
        Return the Predbat log as an HTML page
        """
        logfile = "predbat.log"
        logdata = ""
        self.default_page = "./log"
        if os.path.exists(logfile):
            with open(logfile, "r") as f:
                logdata = f.read()

        # Decode method get arguments
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

        loglines = logdata.split("\n")
        text = self.get_header("Predbat Log", refresh=10)
        text += """<body>
<style>
.log-menu {
    background-color: #ffffff;
    overflow: hidden;
    display: flex;
    align-items: center;
    margin-bottom: 6px;
    border-bottom: 1px solid #ddd;
    padding: 4px 0;
}

.log-menu a {
    color: #333;
    text-align: center;
    padding: 4px 12px;
    text-decoration: none;
    font-size: 14px;
    border-radius: 4px;
    margin: 0 2px;
}

.log-menu a:hover {
    background-color: #f0f0f0;
    color: #4CAF50;
}

.log-menu a.active {
    background-color: #4CAF50;
    color: white;
}

/* Dark mode log menu styles */
body.dark-mode .log-menu {
    background-color: #1e1e1e;
    border-bottom: 1px solid #333;
}

body.dark-mode .log-menu a {
    color: white;
}

body.dark-mode .log-menu a:hover {
    background-color: #2c652f;
    color: white;
}

body.dark-mode .log-menu a.active {
    background-color: #4CAF50;
    color: white;
}
</style>
"""

        if errors:
            active_all = ""
            active_warnings = ""
            active_errors = "active"
        elif warnings:
            active_all = ""
            active_warnings = "active"
            active_errors = ""
        else:
            active_all = "active"
            active_warnings = ""
            active_errors = ""

        text += '<div class="log-menu">'
        text += "<h3>Logfile</h3> "
        text += f'<a href="./log?all" class="{active_all}">All</a>'
        text += f'<a href="./log?warnings" class="{active_warnings}">Warnings</a>'
        text += f'<a href="./log?errors" class="{active_errors}">Errors</a>'
        text += '<a href="./debug_log">Download</a>'
        text += "</div>"

        text += "<table width=100%>\n"

        total_lines = len(loglines)
        count_lines = 0
        lineno = total_lines - 1
        while count_lines < 1024 and lineno >= 0:
            line = loglines[lineno]
            line_lower = line.lower()
            lineno -= 1

            start_line = line[0:27]
            rest_line = line[27:]

            if "error" in line_lower:
                text += "<tr><td>{}</td><td nowrap><font color=#ff3333>{}</font> {}</td></tr>\n".format(lineno, start_line, rest_line)
                count_lines += 1
                continue
            elif (not errors) and ("warn" in line_lower):
                text += "<tr><td>{}</td><td nowrap><font color=#ffA500>{}</font> {}</td></tr>\n".format(lineno, start_line, rest_line)
                count_lines += 1
                continue

            if line and (not errors) and (not warnings):
                text += "<tr><td>{}</td><td nowrap><font color=#33cc33>{}</font> {}</td></tr>\n".format(lineno, start_line, rest_line)
                count_lines += 1

        text += "</table>"
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
        text += """<body>
<style>
.charts-menu {
tabindex="0"  <!-- Make the menu focusable -->
    background-color: #ffffff;
    overflow-x: auto; /* Enable horizontal scrolling */
    white-space: nowrap; /* Prevent menu items from wrapping */
    display: flex;
    align-items: center;
    margin-bottom: 6px;
    border-bottom: 1px solid #ddd;
    padding: 4px 0;
    -webkit-overflow-scrolling: touch; /* Smooth scrolling on iOS */
    scrollbar-width: thin; /* Firefox */
    scrollbar-color: #4CAF50 #f0f0f0; /* Firefox */
}

.charts-menu h3 {
    margin: 0 10px;
    flex-shrink: 0; /* Prevent shrinking */
    white-space: nowrap; /* Prevent text wrapping */
}

.charts-menu a {
    color: #333;
    text-align: center;
    padding: 4px 12px;
    text-decoration: none;
    font-size: 14px;
    border-radius: 4px;
    margin: 0 2px;
    flex-shrink: 0; /* Prevent items from shrinking */
    white-space: nowrap;
    display: inline-block;
}

.charts-menu a:hover {
    background-color: #f0f0f0;
    color: #4CAF50;
}

.charts-menu a.active {
    background-color: #4CAF50;
    color: white;
}

/* Dark mode charts menu styles */
body.dark-mode .charts-menu {
    background-color: #1e1e1e;
    border-bottom: 1px solid #333;
    scrollbar-color: #4CAF50 #333; /* Firefox */
}

body.dark-mode .charts-menu h3 {
    color: #e0e0e0;
}

body.dark-mode .charts-menu a {
    color: white;
}

body.dark-mode .charts-menu a:hover {
    background-color: #2c652f;
    color: white;
}

body.dark-mode .charts-menu a.active {
    background-color: #4CAF50;
    color: white;
}
</style>
<script>
// Initialize the charts menu scrolling functionality
document.addEventListener("DOMContentLoaded", function() {
    // Scroll active item into view
    setTimeout(function() {
        const activeItem = document.querySelector('.charts-menu a.active');
        if (activeItem) {
            const menuBar = document.querySelector('.charts-menu');
            const activeItemLeft = activeItem.offsetLeft;
            const menuBarWidth = menuBar.clientWidth;
            menuBar.scrollLeft = activeItemLeft - menuBarWidth / 2 + activeItem.clientWidth / 2;
        }
    }, 100);
});
</script>
"""

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
        text += """
<style>
.edit-button {
    background-color: #4CAF50;
    color: white;
    border: none;
    padding: 4px 8px;
    text-align: center;
    text-decoration: none;
    display: inline-block;
    font-size: 12px;
    margin: 2px 2px;
    cursor: pointer;
    border-radius: 3px;
}

.edit-button:hover {
    background-color: #45a049;
}

.edit-input {
    width: 300px;
    padding: 4px;
    border: 1px solid #ddd;
    border-radius: 3px;
    font-size: 12px;
}

.save-button, .cancel-button {
    background-color: #2196F3;
    color: white;
    border: none;
    padding: 4px 8px;
    text-align: center;
    text-decoration: none;
    display: inline-block;
    font-size: 12px;
    margin: 2px 2px;
    cursor: pointer;
    border-radius: 3px;
}

.cancel-button {
    background-color: #f44336;
}

.save-button:hover {
    background-color: #0b7dda;
}

.cancel-button:hover {
    background-color: #da190b;
}

.message-container {
    padding: 10px;
    margin: 10px 0;
    border-radius: 4px;
    display: none;
}

.message-success {
    background-color: #d4edda;
    color: #155724;
    border: 1px solid #c3e6cb;
}

.message-error {
    background-color: #f8d7da;
    color: #721c24;
    border: 1px solid #f5c6cb;
}

/* Dark mode styles */
body.dark-mode .edit-button {
    background-color: #4CAF50;
    color: white;
}

body.dark-mode .edit-button:hover {
    background-color: #45a049;
}

body.dark-mode .edit-input {
    background-color: #2d2d2d;
    color: #e0e0e0;
    border: 1px solid #555;
}

body.dark-mode .save-button {
    background-color: #2196F3;
    color: white;
}

body.dark-mode .cancel-button {
    background-color: #f44336;
    color: white;
}

body.dark-mode .message-success {
    background-color: #1e3f20;
    color: #7bc97d;
    border: 1px solid #2d5a2f;
}

body.dark-mode .message-error {
    background-color: #3f1e1e;
    color: #f5c6cb;
    border: 1px solid #5a2d2d;
}

/* Toggle button styles */
.toggle-button {
    position: relative;
    display: inline-block;
    width: 60px;
    height: 24px;
    background-color: #ccc;
    border-radius: 12px;
    cursor: pointer;
    transition: background-color 0.3s;
    border: none;
    outline: none;
}

.toggle-button.active {
    background-color: #f44336;
}

.toggle-button::before {
    content: '';
    position: absolute;
    top: 2px;
    left: 2px;
    width: 20px;
    height: 20px;
    background-color: white;
    border-radius: 50%;
    transition: transform 0.3s;
    box-shadow: 0 2px 4px rgba(0,0,0,0.2);
}

.toggle-button.active::before {
    transform: translateX(36px);
}

.toggle-button:hover {
    opacity: 0.8;
}

/* Dark mode toggle styles */
body.dark-mode .toggle-button {
    background-color: #555 !important;
}

body.dark-mode .toggle-button.active {
    background-color: #f44336 !important;
}

body.dark-mode .toggle-button::before {
    background-color: #e0e0e0 !important;
}

/* Save controls styles */
.save-controls {
    background-color: #f8f9fa;
    border: 1px solid #dee2e6;
    border-radius: 8px;
    padding: 15px;
    margin: 10px 0;
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap;
    gap: 10px;
}

.save-status {
    display: flex;
    align-items: center;
    font-weight: bold;
    color: #495057;
}

.save-all-button {
    background-color: #28a745;
    color: white;
    border: none;
    padding: 10px 20px;
    border-radius: 5px;
    cursor: pointer;
    font-size: 14px;
    font-weight: bold;
    transition: background-color 0.3s;
}

.save-all-button:hover:not(:disabled) {
    background-color: #218838;
}

.save-all-button:disabled {
    background-color: #6c757d;
    cursor: not-allowed;
}

.discard-all-button {
    background-color: #dc3545;
    color: white;
    border: none;
    padding: 10px 20px;
    border-radius: 5px;
    cursor: pointer;
    font-size: 14px;
    font-weight: bold;
    transition: background-color 0.3s;
}

.discard-all-button:hover:not(:disabled) {
    background-color: #c82333;
}

.discard-all-button:disabled {
    background-color: #6c757d;
    cursor: not-allowed;
}

/* Highlight changed rows */
.row-changed {
    background-color: #fff3cd !important;
    border-left: 4px solid #ffc107 !important;
}

/* Dark mode save controls styles */
body.dark-mode .save-controls {
    background-color: #2d2d2d;
    border: 1px solid #404040;
}

body.dark-mode .save-status {
    color: #e0e0e0;
}

body.dark-mode .row-changed {
    background-color: #3d3d1d !important;
    border-left: 4px solid #ffc107 !important;
}

/* Confirmation dialog styles */
.confirmation-overlay {
    position: fixed;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    background-color: rgba(0, 0, 0, 0.5);
    display: flex;
    justify-content: center;
    align-items: center;
    z-index: 9999;
}

.confirmation-dialog {
    background-color: white;
    border-radius: 8px;
    padding: 25px;
    max-width: 600px;
    width: 95%;
    max-height: 90vh;
    overflow-y: auto;
    box-shadow: 0 4px 20px rgba(0, 0, 0, 0.3);
    word-wrap: break-word;
}

.confirmation-dialog h3 {
    margin-top: 0;
    color: #dc3545;
    font-size: 18px;
    word-wrap: break-word;
}

.confirmation-dialog p {
    margin: 15px 0;
    line-height: 1.6;
    word-wrap: break-word;
    overflow-wrap: break-word;
}

.confirmation-buttons {
    display: flex;
    gap: 10px;
    justify-content: flex-end;
    margin-top: 20px;
}

.confirm-button, .cancel-button-dialog {
    padding: 10px 20px;
    border: none;
    border-radius: 5px;
    cursor: pointer;
    font-size: 14px;
    font-weight: bold;
}

.confirm-button {
    background-color: #dc3545;
    color: white;
}

.confirm-button:hover {
    background-color: #c82333;
}

.cancel-button-dialog {
    background-color: #6c757d;
    color: white;
}

.cancel-button-dialog:hover {
    background-color: #545b62;
}

/* Dark mode confirmation dialog */
body.dark-mode .confirmation-dialog {
    background-color: #2d2d2d;
    color: #e0e0e0;
}

body.dark-mode .confirmation-dialog h3 {
    color: #ff6b6b;
}

/* Entity dropdown styles */
.entity-dropdown-container {
    position: relative;
    width: 100%;
    min-width: 400px;
}

.entity-search-input {
    width: 100%;
    min-width: 400px;
    padding: 8px;
    border: 1px solid #ddd;
    border-radius: 4px;
    font-size: 14px;
    box-sizing: border-box;
}

.entity-dropdown {
    position: absolute;
    top: 100%;
    left: 0;
    right: 0;
    min-width: 400px;
    background: white;
    border: 1px solid #ddd;
    border-top: none;
    max-height: 200px;
    overflow-y: auto;
    z-index: 1000;
    box-shadow: 0 2px 4px rgba(0,0,0,0.1);
}

.entity-option {
    padding: 8px;
    cursor: pointer;
    border-bottom: 1px solid #eee;
    display: flex;
    justify-content: space-between;
    align-items: center;
}

.entity-option:hover,
.entity-option.selected {
    background-color: #f0f0f0;
}

.entity-name {
    font-weight: bold;
    flex: 1;
    margin-right: 10px;
    word-break: break-all;
}

.entity-value {
    color: #666;
    font-size: 12px;
    flex-shrink: 0;
    max-width: 150px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

/* Dark mode entity dropdown styles */
body.dark-mode .entity-search-input {
    background-color: #444;
    color: #fff;
    border: 1px solid #666;
}

body.dark-mode .entity-dropdown {
    background: #333;
    border: 1px solid #666;
    color: #fff;
}

body.dark-mode .entity-option {
    border-bottom: 1px solid #555;
}

body.dark-mode .entity-option:hover,
body.dark-mode .entity-option.selected {
    background-color: #555;
}

body.dark-mode .entity-value {
    color: #ccc;
}
</style>
"""

        text += "<body>\n"
        text += (
            f"""
<script>
// Global object to track pending changes
let pendingChanges = {{}};

// All Home Assistant states for entity dropdown
const allStates = """
            + all_states_json
            + """;
function showMessage(message, type) {
    const container = document.getElementById('messageContainer');
    container.className = 'message-container message-' + type;
    container.textContent = message;
    container.style.display = 'block';

    // Auto-hide success messages after 5 seconds
    if (type === 'success') {
        setTimeout(() => {
            container.style.display = 'none';
        }, 5000);
    }
}

function updateChangeCounter() {
    const changeCount = Object.keys(pendingChanges).length;
    const changeCountElement = document.getElementById('changeCount');
    const saveButton = document.getElementById('saveAllButton');
    const discardButton = document.getElementById('discardAllButton');

    if (changeCount === 0) {
        changeCountElement.textContent = 'No unsaved changes';
        saveButton.disabled = true;
        discardButton.disabled = true;
    } else {
        changeCountElement.textContent = `${changeCount} unsaved change${changeCount > 1 ? 's' : ''}`;
        saveButton.disabled = false;
        discardButton.disabled = false;
    }
}

function markRowAsChanged(rowId) {
    const row = document.getElementById('row_' + rowId);
    row.classList.add('row-changed');
}

function unmarkRowAsChanged(rowId) {
    const row = document.getElementById('row_' + rowId);
    row.classList.remove('row-changed');
}

function toggleValue(rowId) {
    const row = document.getElementById('row_' + rowId);
    const argName = row.dataset.argName;
    const toggleButton = row.querySelector('.toggle-button');
    const currentValue = toggleButton.dataset.value === 'true';
    const newValue = !currentValue;

    // Track the change locally
    pendingChanges[argName] = {
        rowId: rowId,
        originalValue: row.dataset.originalValue,
        newValue: newValue.toString(),
        type: 'boolean'
    };

    // Update the toggle button state visually
    toggleButton.dataset.value = newValue.toString();
    if (newValue) {
        toggleButton.classList.add('active');
    } else {
        toggleButton.classList.remove('active');
    }

    // Update the value display
    const valueCell = document.getElementById('value_' + rowId);
    valueCell.innerHTML = newValue.toString();

    // Mark row as changed and update counter
    markRowAsChanged(rowId);
    updateChangeCounter();
}

function editValue(rowId) {
    const row = document.getElementById('row_' + rowId);
    const valueCell = document.getElementById('value_' + rowId);
    const argName = row.dataset.argName;
    const originalValue = row.dataset.originalValue;

    // Check if there's a pending change, use that value instead of original
    const currentValue = pendingChanges[argName] ? pendingChanges[argName].newValue : originalValue;

    // Check if this is an entity string (contains dots)
    if (currentValue && currentValue.match(/^[a-zA-Z]+\\.\\S+/)) {
        // Show entity dropdown
        showEntityDropdown(rowId, currentValue);
    } else {
        // Show regular text input for non-entity values
        valueCell.innerHTML = `
            <input type="text" class="edit-input" id="input_${rowId}" value="${currentValue}">
            <button class="save-button" onclick="saveValue(${rowId})">Apply</button>
            <button class="cancel-button" onclick="cancelEdit(${rowId})">Cancel</button>
        `;

        // Focus the input field
        document.getElementById('input_' + rowId).focus();
    }
}

function getDisplayValueEntity(entityId) {
    // Get the state of the entity from allStates
    if (typeIsEntity(entityId) && allStates[entityId])
    {
        const entityState = allStates[entityId];
        const state = entityState.state || '';
        const unit = entityState.unit_of_measurement || '';
        return `${entityId} = ${state} ${unit}`;
    }
    return entityId; // Fallback to just the entity ID if no state found
}

function cancelEdit(rowId) {
    const row = document.getElementById('row_' + rowId);
    const valueCell = document.getElementById('value_' + rowId);
    const argName = row.dataset.argName;

    // Check if there's a pending change for this row
    if (pendingChanges[argName]) {
        // Show the pending value - need to check if it's an entity
        const pendingValue = pendingChanges[argName].newValue;
        valueCell.innerHTML = getDisplayValueEntity(pendingValue);
    } else {
        // Show the original value - need to check if it's an entity
        const originalValue = row.dataset.originalValue;
        valueCell.innerHTML = getDisplayValueEntity(originalValue);
    }
}

function typeIsEntity(value) {
    if (value.match(/^[a-zA-Z]+\\.\\S+/) && !typeIsNumerical(value))
    {
        return true; // This looks like an entity ID (contains dots but is not a number)
    }
    return false; // Not an entity ID
}

function typeIsNumerical(value) {
    // Check if the string value can be number (integer or float)

    // Check if the value is a valid number
    if (!/^-?\\d*\\.?\\d+$/.test(value)) {
        return false; // Not a valid numerical format
    }

    try {
        if (value.includes('.')) {
            value = parseFloat(value);
            console.log("Parsed as float:", value);
        } else {
            value = parseInt(value);
            console.log("Parsed as integer:", value);
        }
    } catch (e) {
        return false; // Not a numerical value
    }
    return !isNaN(value); // Check if it's a valid number
}

function determineValueType(value) {
    let valueType = 'string';
    if (typeIsEntity(value)) {
        valueType = 'entity';
    } else if (typeIsNumerical(value)) {
        valueType = 'numerical';
    }
    return valueType;
}

function saveValue(rowId) {
    const row = document.getElementById('row_' + rowId);
    const input = document.getElementById('input_' + rowId);
    const argName = row.dataset.argName;
    const newValue = input.value.trim();
    const originalValue = row.dataset.originalValue;

    // Validate the input
    if (newValue === '') {
        showMessage('Value cannot be empty', 'error');
        return;
    }

    // Determine if this is an entity or numerical value
    let valueType = determineValueType(originalValue);
    if (valueType === 'numerical' && newValue !== originalValue) {
        if (!typeIsNumerical(newValue)) {
            showMessage('Invalid number format', 'error');
            return;
        }
    }

    // Track the change locally
    if (newValue !== originalValue) {
        pendingChanges[argName] = {
            rowId: rowId,
            originalValue: originalValue,
            newValue: newValue,
            type: valueType
        };
        markRowAsChanged(rowId);
    } else {
        // If value is same as original, remove from pending changes
        if (pendingChanges[argName]) {
            delete pendingChanges[argName];
            unmarkRowAsChanged(rowId);
        }
    }

    // Update the display value
    const valueCell = document.getElementById('value_' + rowId);
    valueCell.innerHTML = getDisplayValueEntity(newValue);

    updateChangeCounter();
}

// Entity dropdown functions for editing entity strings
function showEntityDropdown(rowId, currentValue) {
    const valueCell = document.getElementById('value_' + rowId);

    // Create the dropdown container
    valueCell.innerHTML = `
        <div class="entity-dropdown-container">
            <input type="text" class="entity-search-input" id="entity_search_${rowId}"
                   placeholder="Type to filter entities..." autocomplete="off">
            <div class="entity-dropdown" id="entity_dropdown_${rowId}"></div>
            <div style="margin-top: 5px;">
                <button class="save-button" onclick="saveEntityValue(${rowId})">Apply</button>
                <button class="cancel-button" onclick="cancelEdit(${rowId})">Cancel</button>
            </div>
        </div>
    `;

    // Set up search functionality first
    setupEntitySearch(rowId, currentValue);

    // If current value is an entity, populate with it as initial filter
    if (currentValue && typeIsEntity(currentValue)) {
        populateEntityDropdown(rowId, currentValue, currentValue);
    } else {
        populateEntityDropdown(rowId, currentValue);
    }

    // Focus the search input and select all text for easy replacement
    const searchInput = document.getElementById('entity_search_' + rowId);
    searchInput.focus();
    searchInput.select();
}

function populateEntityDropdown(rowId, currentValue, filterText = '') {
    const dropdown = document.getElementById('entity_dropdown_' + rowId);
    const searchInput = document.getElementById('entity_search_' + rowId);

    if (!dropdown) return;

    dropdown.innerHTML = '';

    // Get all entity IDs and filter them
    const entities = Object.keys(allStates);
    const filteredEntities = entities.filter(entityId =>
        entityId.toLowerCase().includes(filterText.toLowerCase())
    ).sort();

    // Limit results to prevent performance issues
    const maxResults = 100;
    const limitedEntities = filteredEntities.slice(0, maxResults);

    if (limitedEntities.length === 0) {
        dropdown.innerHTML = '<div class="entity-option">No entities found</div>';
        return;
    }

    limitedEntities.forEach(entityId => {
        const entityState = allStates[entityId];
        const entityValue = entityState && entityState.state ? entityState.state : 'unknown';
        const unit_of_measurement = entityState && entityState.unit_of_measurement ? entityState.unit_of_measurement : '';

        const option = document.createElement('div');
        option.className = 'entity-option';
        option.dataset.entityId = entityId;

        // Highlight current selection (only if search input is empty or matches exactly)
        if (entityId === currentValue && (!filterText || filterText === entityId)) {
            option.classList.add('selected');
        }

        option.innerHTML = `
            <div class="entity-name">${entityId}</div>
            <div class="entity-value">${entityValue}${unit_of_measurement}</div>
        `;

        option.addEventListener('click', () => {
            // Clear previous selections
            dropdown.querySelectorAll('.entity-option').forEach(opt => opt.classList.remove('selected'));
            // Select this option
            option.classList.add('selected');
            searchInput.value = entityId;
        });

        dropdown.appendChild(option);
    });

    if (filteredEntities.length > maxResults) {
        const moreOption = document.createElement('div');
        moreOption.className = 'entity-option';
        moreOption.style.fontStyle = 'italic';
        moreOption.innerHTML = `<div class="entity-name">... and ${filteredEntities.length - maxResults} more (refine search)</div>`;
        dropdown.appendChild(moreOption);
    }
}

function setupEntitySearch(rowId, currentValue) {
    const searchInput = document.getElementById('entity_search_' + rowId);

    if (!searchInput) return;

    // Set initial value if it's an entity
    if (currentValue && currentValue.includes('.')) {
        searchInput.value = currentValue;
    }

    // Handle search input
    searchInput.addEventListener('input', (e) => {
        const filterText = e.target.value;
        populateEntityDropdown(rowId, currentValue, filterText);
    });

    // Handle keyboard navigation
    searchInput.addEventListener('keydown', (e) => {
        const dropdown = document.getElementById('entity_dropdown_' + rowId);
        const options = dropdown.querySelectorAll('.entity-option[data-entity-id]');
        const selected = dropdown.querySelector('.entity-option.selected');

        let newSelection = null;

        if (e.key === 'ArrowDown') {
            e.preventDefault();
            if (selected) {
                newSelection = selected.nextElementSibling;
                if (!newSelection || !newSelection.dataset.entityId) {
                    newSelection = options[0];
                }
            } else {
                newSelection = options[0];
            }
        } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            if (selected) {
                newSelection = selected.previousElementSibling;
                if (!newSelection || !newSelection.dataset.entityId) {
                    newSelection = options[options.length - 1];
                }
            } else {
                newSelection = options[options.length - 1];
            }
        } else if (e.key === 'Enter') {
            e.preventDefault();
            if (selected && selected.dataset.entityId) {
                searchInput.value = selected.dataset.entityId;
            }
            saveEntityValue(rowId);
            return;
        } else if (e.key === 'Escape') {
            e.preventDefault();
            cancelEdit(rowId);
            return;
        }

        if (newSelection) {
            // Clear previous selections
            options.forEach(opt => opt.classList.remove('selected'));
            // Select new option
            newSelection.classList.add('selected');
            // Only update input value on Enter, not on arrow key navigation
            // searchInput.value = newSelection.dataset.entityId;
            // Scroll into view if needed
            newSelection.scrollIntoView({ block: 'nearest' });
        }
    });
}

function saveEntityValue(rowId) {
    const row = document.getElementById('row_' + rowId);
    const searchInput = document.getElementById('entity_search_' + rowId);
    const argName = row.dataset.argName;
    const newValue = searchInput.value.trim();
    const originalValue = row.dataset.originalValue;

    // Validate the input
    if (newValue === '') {
        showMessage('Entity value cannot be empty', 'error');
        return;
    }

    // Validate that it's a valid entity ID (contains at least one dot)
    if (!typeIsEntity(newValue)) {
        showMessage('Please select a valid entity ID', 'error');
        return;
    }

    // New value without text after dollar (if existing)
    const newValueBase = newValue.split('$')[0].trim();

    // Check if entity exists in allStates
    if (!allStates[newValueBase]) {
        if (!confirm(`Entity "${newValueBase}" was not found in Home Assistant. Do you want to use it anyway?`)) {
            return;
        }
    }

    // Track the change locally
    if (newValue !== originalValue) {
        pendingChanges[argName] = {
            rowId: rowId,
            originalValue: originalValue,
            newValue: newValue,
            type: 'entity'
        }
        markRowAsChanged(rowId);
    } else {
        // If value is same as original, remove from pending changes
        if (pendingChanges[argName]) {
            delete pendingChanges[argName];
            unmarkRowAsChanged(rowId);
        }
    }

    // Update the display value - show entity's current state value, not just the entity ID
    const valueCell = document.getElementById('value_' + rowId);

    // Get the entity's current state value for display
    valueCell.innerHTML = getDisplayValueEntity(newValue);
    updateChangeCounter();
}

// Nested entity dropdown functions for editing entity strings within lists/dictionaries
function showNestedEntityDropdown(rowId, currentValue) {
    const valueCell = document.getElementById('nested_value_' + rowId);

    // Create the dropdown container
    valueCell.innerHTML = `
        : <div class="entity-dropdown-container" style="min-width: 450px;">
            <input type="text" class="entity-search-input" id="nested_entity_search_${rowId}"
                   placeholder="Type to filter entities..." autocomplete="off" style="min-width: 400px;">
            <div class="entity-dropdown" id="nested_entity_dropdown_${rowId}" style="min-width: 400px;"></div>
            <div style="margin-top: 5px;">
                <button class="save-button" onclick="saveNestedEntityValue(${rowId})">Apply</button>
                <button class="cancel-button" onclick="cancelNestedEdit(${rowId})">Cancel</button>
            </div>
        </div>
    `;

    // Set up search functionality first
    setupNestedEntitySearch(rowId, currentValue);

    // If current value is an entity, populate with it as initial filter
    if (currentValue && currentValue.includes('.')) {
        populateNestedEntityDropdown(rowId, currentValue, currentValue);
    } else {
        populateNestedEntityDropdown(rowId, currentValue);
    }

    // Focus the search input and select all text for easy replacement
    const searchInput = document.getElementById('nested_entity_search_' + rowId);
    searchInput.focus();
    searchInput.select();
}

function populateNestedEntityDropdown(rowId, currentValue, filterText = '') {
    const dropdown = document.getElementById('nested_entity_dropdown_' + rowId);
    const searchInput = document.getElementById('nested_entity_search_' + rowId);

    if (!dropdown) return;

    dropdown.innerHTML = '';

    // Get all entity IDs and filter them
    const entities = Object.keys(allStates);
    const filteredEntities = entities.filter(entityId =>
        entityId.toLowerCase().includes(filterText.toLowerCase())
    ).sort();

    // Limit results to prevent performance issues
    const maxResults = 100;
    const limitedEntities = filteredEntities.slice(0, maxResults);

    if (limitedEntities.length === 0) {
        dropdown.innerHTML = '<div class="entity-option">No entities found</div>';
        return;
    }

    limitedEntities.forEach(entityId => {
        const entityState = allStates[entityId];
        const entityValue = entityState && entityState.state ? entityState.state : 'unknown';
        const unit_of_measurement = entityState && entityState.unit_of_measurement ? entityState.unit_of_measurement : '';

        const option = document.createElement('div');
        option.className = 'entity-option';
        option.dataset.entityId = entityId;

        // Highlight current selection (only if search input is empty or matches exactly)
        if (entityId === currentValue && (!filterText || filterText === entityId)) {
            option.classList.add('selected');
        }

        option.innerHTML = `
            <div class="entity-name">${entityId}</div>
            <div class="entity-value">${entityValue}${unit_of_measurement}</div>
        `;

        option.addEventListener('click', () => {
            // Clear previous selections
            dropdown.querySelectorAll('.entity-option').forEach(opt => opt.classList.remove('selected'));
            // Select this option
            option.classList.add('selected');
            searchInput.value = entityId;
        });

        dropdown.appendChild(option);
    });

    if (filteredEntities.length > maxResults) {
        const moreOption = document.createElement('div');
        moreOption.className = 'entity-option';
        moreOption.style.fontStyle = 'italic';
        moreOption.innerHTML = `<div class="entity-name">... and ${filteredEntities.length - maxResults} more (refine search)</div>`;
        dropdown.appendChild(moreOption);
    }
}

function setupNestedEntitySearch(rowId, currentValue) {
    const searchInput = document.getElementById('nested_entity_search_' + rowId);

    if (!searchInput) return;

    // Set initial value if it's an entity
    if (currentValue && currentValue.includes('.')) {
        searchInput.value = currentValue;
    }

    // Handle search input
    searchInput.addEventListener('input', (e) => {
        const filterText = e.target.value;
        populateNestedEntityDropdown(rowId, currentValue, filterText);
    });

    // Handle keyboard navigation
    searchInput.addEventListener('keydown', (e) => {
        const dropdown = document.getElementById('nested_entity_dropdown_' + rowId);
        const options = dropdown.querySelectorAll('.entity-option[data-entity-id]');
        const selected = dropdown.querySelector('.entity-option.selected');

        let newSelection = null;

        if (e.key === 'ArrowDown') {
            e.preventDefault();
            if (selected) {
                newSelection = selected.nextElementSibling;
                if (!newSelection || !newSelection.dataset.entityId) {
                    newSelection = options[0];
                }
            } else {
                newSelection = options[0];
            }
        } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            if (selected) {
                newSelection = selected.previousElementSibling;
                if (!newSelection || !newSelection.dataset.entityId) {
                    newSelection = options[options.length - 1];
                }
            } else {
                newSelection = options[options.length - 1];
            }
        } else if (e.key === 'Enter') {
            e.preventDefault();
            if (selected && selected.dataset.entityId) {
                searchInput.value = selected.dataset.entityId;
            }
            saveNestedEntityValue(rowId);
            return;
        } else if (e.key === 'Escape') {
            e.preventDefault();
            cancelNestedEdit(rowId);
            return;
        }

        if (newSelection) {
            // Clear previous selections
            options.forEach(opt => opt.classList.remove('selected'));
            // Select new option
            newSelection.classList.add('selected');
            // Scroll into view if needed
            newSelection.scrollIntoView({ block: 'nearest' });
        }
    });
}

function saveNestedEntityValue(rowId) {
    const row = document.getElementById('nested_row_' + rowId);
    const searchInput = document.getElementById('nested_entity_search_' + rowId);
    const nestedPath = row.dataset.nestedPath;
    const newValue = searchInput.value.trim();
    const originalValue = row.dataset.nestedOriginal;

    // Validate the input
    if (newValue === '') {
        showMessage('Entity value cannot be empty', 'error');
        return;
    }

    // Validate that it's a valid entity ID (contains at least one dot)
    if (!newValue.includes('.')) {
        showMessage('Please select a valid entity ID', 'error');
        return;
    }

    // New value without text after dollar (if existing)
    const newValueBase = newValue.split('$')[0].trim();

    // Check if entity exists in allStates
    if (!allStates[newValueBase]) {
        if (!confirm(`Entity "${newValueBase}" was not found in Home Assistant. Do you want to use it anyway?`)) {
            return;
        }
    }

    // Track the change locally
    if (newValue !== originalValue) {
        pendingChanges[nestedPath] = {
            rowId: rowId,
            originalValue: originalValue,
            newValue: newValue,
            type: 'entity',
            isNested: true,
            path: nestedPath
        };
        markNestedRowAsChanged(rowId);
    } else {
        // If value is same as original, remove from pending changes
        if (pendingChanges[nestedPath]) {
            delete pendingChanges[nestedPath];
            unmarkNestedRowAsChanged(rowId);
        }
    }

    // Update the display value - show entity's current state value, not just the entity ID
    const valueCell = document.getElementById('nested_value_' + rowId);

    // Get the entity's current state value for display
    valueCell.innerHTML = getDisplayValueEntity(newValue);

    updateChangeCounter();
}

function showConfirmationDialog() {
    const changeCount = Object.keys(pendingChanges).length;

    // Create the overlay
    const overlay = document.createElement('div');
    overlay.className = 'confirmation-overlay';
    overlay.innerHTML = `
        <div class="confirmation-dialog">
            <h3>⚠️ Confirm Save Changes</h3>
            <p>You are about to save <strong>${changeCount}</strong> change${changeCount > 1 ? 's' : ''} to apps.yaml.</p>
            <p><strong>Warning:</strong> This will restart Predbat to apply the changes.</p>
            <div class="confirmation-buttons">
                <button class="cancel-button-dialog" onclick="hideConfirmationDialog()">Cancel</button>
                <button class="confirm-button" onclick="confirmSaveChanges()">Save & Restart</button>
            </div>
        </div>
    `;

    document.body.appendChild(overlay);
}

function hideConfirmationDialog() {
    const overlay = document.querySelector('.confirmation-overlay');
    if (overlay) {
        overlay.remove();
    }
}

async function confirmSaveChanges() {
    hideConfirmationDialog();

    // Disable save buttons
    document.getElementById('saveAllButton').disabled = true;
    document.getElementById('discardAllButton').disabled = true;

    showMessage('Saving changes...', 'success');

    // Send all changes to the server
    try {
        const formData = new FormData();
        formData.append('changes', JSON.stringify(pendingChanges));

        const response = await fetch('./apps', {
            method: 'POST',
            body: formData
        });

        const result = await response.json();

        if (result.success) {
            showMessage(result.message + ' - Page will reload...', 'success');

            // Clear pending changes
            pendingChanges = {};
            updateChangeCounter();

            // Reload the page after a short delay
            setTimeout(() => {
                window.location.reload();
            }, 2000);
        } else {
            showMessage(result.message, 'error');
            // Re-enable buttons
            document.getElementById('saveAllButton').disabled = false;
            document.getElementById('discardAllButton').disabled = false;
        }
    } catch (error) {
        showMessage('Error saving changes: ' + error.message, 'error');
        // Re-enable buttons
        document.getElementById('saveAllButton').disabled = false;
        document.getElementById('discardAllButton').disabled = false;
    }
}

function saveAllChanges() {
    if (Object.keys(pendingChanges).length === 0) {
        showMessage('No changes to save', 'error');
        return;
    }

    showConfirmationDialog();
}

// Functions for handling nested dictionary values
function toggleNestedValue(rowId) {
    const row = document.getElementById('nested_row_' + rowId);
    const nestedPath = row.dataset.nestedPath;
    const toggleButton = row.querySelector('.toggle-button');
    const currentValue = toggleButton.dataset.value === 'true';
    const newValue = !currentValue;

    // Track the change locally
    pendingChanges[nestedPath] = {
        rowId: rowId,
        originalValue: row.dataset.nestedOriginal,
        newValue: newValue.toString(),
        type: 'boolean',
        isNested: true,
        path: nestedPath
    };

    // Update the toggle button state visually
    toggleButton.dataset.value = newValue.toString();
    if (newValue) {
        toggleButton.classList.add('active');
    } else {
        toggleButton.classList.remove('active');
    }

    // Update the value display
    const valueCell = document.getElementById('nested_value_' + rowId);
    valueCell.innerHTML = newValue.toString();

    // Mark row as changed and update counter
    markNestedRowAsChanged(rowId);
    updateChangeCounter();
}

function editNestedValue(rowId) {
    const row = document.getElementById('nested_row_' + rowId);
    const valueCell = document.getElementById('nested_value_' + rowId);
    const nestedPath = row.dataset.nestedPath;
    const originalValue = row.dataset.nestedOriginal;

    // Check if there's a pending change, use that value instead of original
    const currentValue = pendingChanges[nestedPath] ? pendingChanges[nestedPath].newValue : originalValue;

    // Check if this is an entity string (contains dots)
    if (currentValue && currentValue.match(/^[a-zA-Z]+\\.\\S+/)) {
        // Show entity dropdown for nested values
        showNestedEntityDropdown(rowId, currentValue);
    } else {
        // Replace the value cell content with an input field for non-entity values
        valueCell.innerHTML = `
            : <input type="text" class="edit-input" id="nested_input_${rowId}" value="${currentValue}">
            <button class="save-button" onclick="saveNestedValue(${rowId})">Apply</button>
            <button class="cancel-button" onclick="cancelNestedEdit(${rowId})">Cancel</button>
        `;

        // Focus the input field
        document.getElementById('nested_input_' + rowId).focus();
    }
}

function cancelNestedEdit(rowId) {
    const row = document.getElementById('nested_row_' + rowId);
    const valueCell = document.getElementById('nested_value_' + rowId);
    const nestedPath = row.dataset.nestedPath;

    // Check if there's a pending change for this nested value
    if (pendingChanges[nestedPath]) {
        // Show the pending value
        valueCell.innerHTML = getDisplayValueEntity(pendingChanges[nestedPath].newValue);
    } else {
        // Show the original value
        const originalValue = row.dataset.nestedOriginal;
        valueCell.innerHTML = getDisplayValueEntity(originalValue);
    }
}

function saveNestedValue(rowId) {
    const row = document.getElementById('nested_row_' + rowId);
    const input = document.getElementById('nested_input_' + rowId);
    const nestedPath = row.dataset.nestedPath;
    const newValue = input.value.trim();
    const originalValue = row.dataset.nestedOriginal;

    // Validate the input
    if (newValue === '') {
        showMessage('Value cannot be empty', 'error');
        return;
    }

    // Determine the value type and validate accordingly
    let valueType = determineValueType(originalValue);
    if (valueType === 'numerical' && newValue !== originalValue) {
        if (!typeIsNumerical(newValue)) {
            showMessage('Invalid number format', 'error');
            return;
        }
    }

    // Track the change locally
    if (newValue !== originalValue) {
        pendingChanges[nestedPath] = {
            rowId: rowId,
            originalValue: originalValue,
            newValue: newValue,
            type: valueType,
            isNested: true,
            path: nestedPath
        };
        markNestedRowAsChanged(rowId);
    } else {
        // If value is same as original, remove from pending changes
        if (pendingChanges[nestedPath]) {
            delete pendingChanges[nestedPath];
            unmarkNestedRowAsChanged(rowId);
        }
    }

    // Update the display value
    const valueCell = document.getElementById('nested_value_' + rowId);
    valueCell.innerHTML = getDisplayValueEntity(newValue);

    updateChangeCounter();
}

function markNestedRowAsChanged(rowId) {
    const row = document.getElementById('nested_row_' + rowId);
    row.classList.add('row-changed');
}

function unmarkNestedRowAsChanged(rowId) {
    const row = document.getElementById('nested_row_' + rowId);
    row.classList.remove('row-changed');
}

// Update the discardAllChanges function to handle nested values
function discardAllChanges() {
    // Reset all changed rows to their original values
    for (const pathOrArgName in pendingChanges) {
        const change = pendingChanges[pathOrArgName];

        if (change.isNested) {
            // Handle nested values
            const row = document.getElementById('nested_row_' + change.rowId);
            const valueCell = document.getElementById('nested_value_' + change.rowId);

            if (change.type === 'boolean') {
                // Reset toggle button
                const toggleButton = row.querySelector('.toggle-button');
                const originalValue = change.originalValue === 'True';
                toggleButton.dataset.value = change.originalValue.toLowerCase();
                if (originalValue) {
                    toggleButton.classList.add('active');
                } else {
                    toggleButton.classList.remove('active');
                }
                valueCell.innerHTML = getDisplayValueEntity(change.originalValue);
            } else {
                // Reset numerical value
                valueCell.innerHTML = getDisplayValueEntity(change.originalValue);
            }

            unmarkNestedRowAsChanged(change.rowId);
        } else {
            // Handle top-level values (existing logic)
            const row = document.getElementById('row_' + change.rowId);
            const valueCell = document.getElementById('value_' + change.rowId);

            if (change.type === 'boolean') {
                // Reset toggle button
                const toggleButton = row.querySelector('.toggle-button');
                const originalValue = change.originalValue === 'True';
                toggleButton.dataset.value = change.originalValue.toLowerCase();
                if (originalValue) {
                    toggleButton.classList.add('active');
                } else {
                    toggleButton.classList.remove('active');
                }
                valueCell.innerHTML = getDisplayValueEntity(change.originalValue);
            } else {
                // Reset numerical or entity value
                valueCell.innerHTML = getDisplayValueEntity(change.originalValue);
            }

            unmarkRowAsChanged(change.rowId);
        }
    }

    // Clear all pending changes
    pendingChanges = {};
    updateChangeCounter();
    showMessage('All changes discarded', 'success');
}
</script>
"""
        )

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
            if "_key" in arg:
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
        text += """
        <style>
        .filter-container {
            margin: 20px 0;
            display: flex;
            align-items: center;
        }
        .filter-input {
            padding: 8px 12px;
            border: 1px solid #ccc;
            border-radius: 4px;
            font-size: 16px;
            width: 300px;
            margin-left: 10px;
        }
        body.dark-mode .filter-input {
            background-color: #333;
            color: #e0e0e0;
            border-color: #555;
        }
        </style>
        <script>
        // Save and restore filter value between page loads
        function saveFilterValue() {
            localStorage.setItem('configFilterValue', document.getElementById('configFilter').value);
        }

        function restoreFilterValue() {
            const savedFilter = localStorage.getItem('configFilterValue');
            if (savedFilter) {
                document.getElementById('configFilter').value = savedFilter;
                filterConfig();
            }
        }

        function filterConfig() {
            const filterValue = document.getElementById('configFilter').value.toLowerCase();
            const rows = document.querySelectorAll('#configTable tr');

            // Save filter value for persistence
            saveFilterValue();

            // Skip header row
            for(let i = 1; i < rows.length; i++) {
                const row = rows[i];
                const nameCell = row.querySelector('td:nth-child(2)');
                const entityCell = row.querySelector('td:nth-child(3)');

                if (!nameCell || !entityCell) continue;

                const nameText = nameCell.textContent.toLowerCase();
                const entityText = entityCell.textContent.toLowerCase();

                if (nameText.includes(filterValue) || entityText.includes(filterValue)) {
                    row.style.display = '';
                } else {
                    row.style.display = 'none';
                }
            }
        }

        // Register event to restore filter value after page load
        document.addEventListener('DOMContentLoaded', restoreFilterValue);
        </script>

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
        text = self.get_header("Predbat Apps.yaml Editor", refresh=0)

        # Add CodeMirror scripts and styles to the header
        text = text.replace(
            "</head>",
            """
    <!-- CodeMirror Library -->
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.9/codemirror.min.css">
    <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.9/codemirror.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.9/mode/yaml/yaml.min.js"></script>

    <!-- CodeMirror Theme -->
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.9/theme/monokai.min.css">

    <!-- YAML Validation and Linting -->
    <script src="https://cdnjs.cloudflare.com/ajax/libs/js-yaml/4.1.0/js-yaml.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.9/addon/lint/lint.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.9/addon/lint/yaml-lint.js"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.9/addon/lint/lint.min.css">
    </head>""",
        )

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

        text += """
<style>
.editor-container {
    position: fixed;
    top: 70px; /* Account for fixed header */
    left: 0;
    right: 0;
    bottom: 0;
    display: flex;
    flex-direction: column;
    background-color: inherit;
    z-index: 10;
}

.editor-header {
    flex-shrink: 0;
    padding: 10px 15px;
    margin: 0;
    background-color: inherit;
}

.editor-form {
    flex: 1;
    display: flex;
    flex-direction: column;
    padding: 0 15px 15px 15px;
    min-height: 0; /* Important for flex children */
    overflow: hidden; /* Prevent form from overflowing */
}

.editor-textarea {
    flex: 1;
    font-family: 'Courier New', monospace;
    font-size: 14px;
    line-height: 1.4;
    padding: 10px;
    border: 2px solid #4CAF50;
    border-radius: 4px;
    resize: none;
    background-color: #ffffff;
    color: #333;
    white-space: pre;
    overflow-wrap: normal;
    overflow: auto;
    min-height: 0; /* Important for flex children */
    width: 100%;
    box-sizing: border-box;
}

.editor-controls {
    flex-shrink: 0;
    margin-top: 10px;
    display: flex;
    gap: 10px;
    align-items: center;
    flex-wrap: wrap;
    padding: 10px 0;
}

.save-button {
    background-color: #4CAF50;
    color: white;
    border: none;
    padding: 12px 24px;
    font-size: 16px;
    border-radius: 4px;
    cursor: pointer;
    font-weight: bold;
}

.save-button:hover {
    background-color: #45a049;
}

.save-button:disabled {
    background-color: #cccccc !important;
    color: #888888 !important;
    cursor: not-allowed !important;
    position: relative !important;
    border: 2px solid #bbbbbb !important;
}

.revert-button {
    background-color: #f44336;
    color: white;
    border: none;
    padding: 12px 24px;
    font-size: 16px;
    border-radius: 4px;
    cursor: pointer;
    font-weight: bold;
}

.revert-button:hover {
    background-color: #d32f2f;
}

.revert-button:disabled {
    background-color: #cccccc !important;
    color: #888888 !important;
    cursor: not-allowed !important;
    position: relative !important;
    border: 2px solid #bbbbbb !important;
}

.message {
    padding: 10px;
    border-radius: 4px;
    margin: 10px 0;
    display: none;
}

.success {
    background-color: #d4edda;
    color: #155724;
    border: 1px solid #c3e6cb;
}

.error {
    background-color: #f8d7da;
    color: #721c24;
    border: 1px solid #f5c6cb;
}

/* CodeMirror specific styles */
.CodeMirror {
    height: auto;
    flex: 1;
    font-family: 'Courier New', monospace;
    font-size: 14px;
    border: 2px solid #4CAF50;
    border-radius: 4px;
    background-color: #ffffff !important; /* Force white background */
}

/* Selection style */
.CodeMirror-selected {
    background-color: #b5d5ff !important;
}

/* Dark mode selection style */
body.dark-mode .CodeMirror-selected {
    background-color: #3a3d41 !important;
}

.CodeMirror-gutters {
    background-color: #f8f8f8;
    border-right: 1px solid #ddd;
}

.CodeMirror-linenumber {
    color: #999;
}

/* Dark mode styles */
body.dark-mode .editor-textarea {
    background-color: #2d2d2d;
    color: #f0f0f0;
    border-color: #4CAF50;
}

body.dark-mode .CodeMirror {
    border-color: #4CAF50;
    background-color: #2d2d2d !important; /* Force dark background */
    color: #f0f0f0 !important; /* Force light text */
}

body.dark-mode .CodeMirror-gutters {
    background-color: #2d2d2d;
    border-right: 1px solid #444;
}

body.dark-mode .CodeMirror-linenumber {
    color: #777;
}

/* Dark mode syntax highlighting adjustments */
body.dark-mode .cm-s-default .cm-string {
    color: #ce9178 !important;
}

body.dark-mode .cm-s-default .cm-number {
    color: #b5cea8 !important;
}

body.dark-mode .cm-s-default .cm-keyword {
    color: #569cd6 !important;
}

body.dark-mode .cm-s-default .cm-property {
    color: #9cdcfe !important;
}

body.dark-mode .cm-s-default .cm-atom {
    color: #d19a66 !important;
}

body.dark-mode .cm-s-default .cm-comment {
    color: #6a9955 !important;
}

body.dark-mode .cm-s-default .cm-meta {
    color: #dcdcaa !important;
}

body.dark-mode .cm-s-default .cm-tag {
    color: #569cd6 !important;
}

body.dark-mode .cm-s-default .cm-attribute {
    color: #9cdcfe !important;
}

body.dark-mode .cm-s-default .cm-variable {
    color: #9cdcfe !important;
}

body.dark-mode .cm-s-default .cm-variable-2 {
    color: #4ec9b0 !important;
}

body.dark-mode .cm-s-default .cm-def {
    color: #dcdcaa !important;
}

body.dark-mode .CodeMirror-cursor {
    border-left: 1px solid #f0f0f0 !important;
}

/* Lint markers for both light and dark mode */
.CodeMirror-lint-marker-error, .CodeMirror-lint-message-error {
    background-image: url('data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNCAyNCIgd2lkdGg9IjE2IiBoZWlnaHQ9IjE2IiBmaWxsPSJyZWQiPjxwYXRoIGQ9Ik0xMS45OTMgMi4wMDFhMTAgMTAgMCAwMC03LjA3MyAyLjkyOEExMCAxMCAwIDAwMS45OTIgMTIuMDAxYTEwIDEwIDAgMDAyLjkyOCA3LjA3MiAxMCAxMCAwIDAwNy4wNzMgMi45MjkgMTAgMTAgMCAwMDcuMDczLTIuOTMgMTAgMTAgMCAwMDIuOTI4LTcuMDcxIDEwIDEwIDAgMDAtMi45MjgtNy4wNzIgMTAgMTAgMCAwMC03LjA3My0yLjkyOHptMCA0bC4yMzIuMDAzYy41MjUuMDEzLjk5NC4zMzQgMS4yLjgyNWwuMDQuMTAzTDE2LjM0NSAxNWExLjUgMS41IDAgMDEtMi44NS45NDVsLS4wMzItLjFMMTIgMTIuNzYzIDguNTM3IDE1LjgybC0uMDk2LjA4YTEuNSAxLjUgMCAwMS0xLjgxLjEwNmwtLjEwMi0uMDgxYTEuNSAxLjUgMCAwMS0uMTA4LTEuODA2bC4wOC0uMTA0TDkuNCA3Ljk0NWwuMDgxLS4xMjVjLjIzMS0uMzE2LjYxNi0uNTE2IDEuMDM3LS4wMTZsLjA5Mi4wODMuMDc4LjA5My4wOTcuMTM2LjA0OC4wODUuMTYuMDMyeloiLz48L3N2Zz4=');
    background-position: center;
    background-repeat: no-repeat;
}

.CodeMirror-lint-tooltip {
    border: 1px solid #ccc;
    border-radius: 4px;
    background-color: white;
    z-index: 10000;
    max-width: 600px;
    overflow: hidden;
    white-space: pre-wrap;
    padding: 8px;
    box-shadow: 0 2px 10px rgba(0, 0, 0, 0.2);
    color: #333;
}

body.dark-mode .CodeMirror-lint-tooltip {
    background-color: #2d2d2d;
    color: #f0f0f0;
    border-color: #444;
}

.CodeMirror-lint-marker-error:hover {
    cursor: pointer;
}

.CodeMirror-lint-line-error {
    background-color: rgba(255, 0, 0, 0.1);
}

body.dark-mode .CodeMirror-lint-line-error {
    background-color: rgba(255, 0, 0, 0.2);
}

body.dark-mode .message.success {
    background-color: #1e3f20;
    color: #7bc97d;
    border-color: #2d5a2f;
}

body.dark-mode .message.error {
    background-color: #3f1e1e;
    color: #f5c6cb;
    border-color: #5a2d2d;
}

body.dark-mode .save-button:disabled {
    background-color: #444444 !important;
    color: #777777 !important;
    border: 2px solid #555555 !important;
}

body.dark-mode .revert-button {
    background-color: #c62828;
    color: white;
    border: none;
}

body.dark-mode .revert-button:hover {
    background-color: #b71c1c;
}

body.dark-mode .revert-button:disabled {
    background-color: #444444 !important;
    color: #777777 !important;
    border: 2px solid #555555 !important;
}
</style>

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

        text += """    </div>
</div>

<script>
let isSubmitting = false;
let editor; // CodeMirror instance

document.getElementById('editorForm').addEventListener('submit', function(e) {
    e.preventDefault(); // Always prevent default initially

    if (isSubmitting) {
        return;
    }

    // Get the current content and validate it
    const content = editor ? editor.getValue() : document.getElementById('appsContent').value;
    let hasYamlError = false;

    try {
        // Only validate if content exists and isn't empty
        if (content && content.trim()) {
            jsyaml.load(content);
        }
    } catch (e) {
        console.log('YAML validation error during form submit:', e.message);
        hasYamlError = true;
    }

    // Safety check - don't allow submission if there are YAML errors
    if (hasYamlError) {
        showMessage("Cannot save while there are YAML syntax errors. Please fix the errors first.", "error");
        return;
    }

    // Show confirmation popup
    const confirmed = confirm("Warning: Saving changes will restart Predbat. Are you sure you want to continue?");

    if (!confirmed) {
        return; // User cancelled the save
    }

    // Update the hidden textarea with CodeMirror content before submission
    if (editor) {
        document.getElementById('appsContent').value = editor.getValue();
    }

    isSubmitting = true;
    const saveButton = document.getElementById('saveButton');
    const saveStatus = document.getElementById('saveStatus');

    saveButton.disabled = true;
    saveButton.textContent = 'Saving...';
    saveStatus.textContent = 'Please wait...';

    // Clear stored content as we're saving now
    localStorage.removeItem('appsYamlContent');

    // Submit the form programmatically
    this.submit();
});

// Show messages
function showMessage(message, type = 'success') {
    const messageContainer = document.getElementById('messageContainer');
    const messageDiv = document.createElement('div');
    messageDiv.className = `message ${type}`;
    messageDiv.style.display = 'block';
    messageDiv.textContent = message;

    messageContainer.innerHTML = '';
    messageContainer.appendChild(messageDiv);

    // Auto-hide success messages after 5 seconds
    if (type === 'success') {
        setTimeout(() => {
            messageDiv.style.display = 'none';
        }, 5000);
    }
}

// Function to update button states based on content changes and validation
function updateButtonStates(saveButton, revertButton, content, hasError = false) {
    if (!saveButton && !revertButton) return;

    const isDarkMode = document.body.classList.contains('dark-mode');
    const hasChanged = content !== window.originalContent;

    // Update Save button
    if (saveButton) {
        // First set the disabled property, which is crucial for behavior
        const shouldDisableSave = hasError || !hasChanged;
        saveButton.disabled = shouldDisableSave;

        // Update tooltip
        if (hasError) {
            saveButton.title = 'Fix YAML errors before saving';
        } else {
            saveButton.title = hasChanged ? 'Save changes' : 'No changes to save';
        }

        // Apply styling - ensure disabled style gets applied correctly
        if (shouldDisableSave) {
            // Disabled styling
            saveButton.style.backgroundColor = isDarkMode ? '#444444' : '#cccccc';
            saveButton.style.color = isDarkMode ? '#777777' : '#888888';
            saveButton.style.border = `2px solid ${isDarkMode ? '#555555' : '#bbbbbb'}`;
        } else {
            // Enabled styling
            saveButton.style.backgroundColor = '#4CAF50';
            saveButton.style.color = 'white';
            saveButton.style.border = 'none';
        }
    }

    // Update Revert button - always enable if content changed, regardless of errors
    if (revertButton) {
        // First set the disabled property
        const shouldDisableRevert = !hasChanged;
        revertButton.disabled = shouldDisableRevert;
        revertButton.title = hasChanged ? 'Discard changes and reload from disk' : 'No changes to revert';

        // Apply styling - ensure disabled style gets applied correctly
        if (shouldDisableRevert) {
            // Disabled styling
            revertButton.style.backgroundColor = isDarkMode ? '#444444' : '#cccccc';
            revertButton.style.color = isDarkMode ? '#777777' : '#888888';
            revertButton.style.border = `2px solid ${isDarkMode ? '#555555' : '#bbbbbb'}`;
        } else {
            // Enabled styling
            revertButton.style.backgroundColor = '#4CAF50';
            revertButton.style.color = 'white';
            revertButton.style.border = 'none';
        }
    }
}

// Custom YAML linter using js-yaml
CodeMirror.registerHelper("lint", "yaml", function(text) {
    const found = [];
    if (!text.trim()) {
        return found; // Return empty array for empty text to avoid false errors
    }

    try {
        jsyaml.load(text);
    } catch (e) {
        // Convert js-yaml error to CodeMirror lint format
        const line = e.mark && e.mark.line ? e.mark.line : 0;
        const ch = e.mark && e.mark.column ? e.mark.column : 0;
        found.push({
            from: CodeMirror.Pos(line, ch),
            to: CodeMirror.Pos(line, ch + 1),
            message: e.message,
            severity: "error"
        });
    }

    // Return the array of found issues
    return found;
});

// Initialize CodeMirror and handle dark mode
function initializeCodeMirror() {
    const textarea = document.getElementById('appsContent');

    if (!textarea) return;

    const isDarkMode = document.body.classList.contains('dark-mode');

    // Check if we have unsaved content in localStorage
    const savedContent = localStorage.getItem('appsYamlContent');

    // Store the original content for change comparison
    window.originalContent = textarea.value;

    // If we have saved content and the textarea is empty or the saved content differs from current
    if (savedContent && (!textarea.value.trim() || savedContent !== textarea.value)) {
        // Always load the saved content automatically
        textarea.value = savedContent;
        console.log('Automatically restored content from localStorage');
    }

    // Create CodeMirror instance
    editor = CodeMirror.fromTextArea(textarea, {
        mode: 'yaml',
        theme: isDarkMode ? 'monokai' : 'default', // Use default theme for light mode (pure white background)
        lineNumbers: true,
        indentUnit: 2,
        smartIndent: true,
        tabSize: 2,
        indentWithTabs: false,
        lineWrapping: false,
        gutters: ['CodeMirror-linenumbers', 'CodeMirror-lint-markers'],
        lint: {
            getAnnotations: CodeMirror.helpers.lint.yaml,
            lintOnChange: true,
            delay: 300 // Reduced delay for faster feedback
        },
        autofocus: true,
        extraKeys: {
            'Tab': function(cm) {
                if (cm.somethingSelected()) {
                    cm.indentSelection('add');
                } else {
                    cm.replaceSelection('  ', 'end', '+input');
                }
            },
            'Ctrl-Space': 'autocomplete'
        }
    });

    // Manually validate YAML when editor is ready
    editor.on('change', function() {
        // Get the content and buttons
        const content = editor.getValue();
        const saveButton = document.getElementById('saveButton');
        const revertButton = document.getElementById('revertButton');
        let isValid = true;

        try {
            // Parse YAML to check for errors
            if (content.trim()) {
                jsyaml.load(content);
            }
        } catch (e) {
            isValid = false;
            console.log('YAML validation error in change handler:', e.message);
        }

        // Update button states based on YAML validation result
        updateButtonStates(saveButton, revertButton, content, !isValid);
        console.log('Button states updated by change handler, YAML valid:', isValid);

        // Save content to localStorage whenever it changes
        localStorage.setItem('appsYamlContent', content);
        console.log('Content saved to localStorage');
    });

    // Make CodeMirror fill the available space
    editor.setSize('100%', '100%');

    // Add custom CSS to make CodeMirror fill its container properly
    const cmElement = editor.getWrapperElement();
    cmElement.style.flex = '1';
    cmElement.style.minHeight = '0';
    cmElement.style.height = 'auto';

    // Set up the lint status display
    const lintStatusEl = document.getElementById('lintStatus');
    if (lintStatusEl) {
        // Update lint status when linting is done
        editor.on('lint', (errors) => {
            console.log('Lint event fired:', errors ? errors.length : 0, 'errors found');
            const saveButton = document.getElementById('saveButton');

            // Make a direct validation attempt as backup
            let isValid = true;
            try {
                const content = editor.getValue();
                if (content && content.trim()) {
                    jsyaml.load(content);
                }
            } catch (e) {
                console.log('Manual YAML validation error during lint event:', e.message);
                isValid = false;
            }

            const content = editor.getValue();
            const revertButton = document.getElementById('revertButton');
            const hasErrors = (errors && errors.length > 0) || !isValid;

            if (hasErrors) {
                lintStatusEl.innerHTML = `<div style="color: #d32f2f; padding: 5px; border-radius: 4px; background-color: ${isDarkMode ? '#3f1e1e' : '#fff0f0'}; border: 1px solid #d32f2f;">
                    <strong>⚠️ Found ${errors ? errors.length : 'syntax'} YAML ${errors && errors.length === 1 ? 'error' : 'errors'}</strong>
                    <p style="margin: 5px 0 0 0; font-size: 14px;">Hover over the red markers in the editor gutter to see details.</p>
                </div>`;

                // Update button states with error flag
                updateButtonStates(saveButton, revertButton, content, true);
                console.log('Button states updated by lint event (with errors)');
            } else {
                // Clear the lint status when syntax is valid
                lintStatusEl.innerHTML = '';

                // Update button states with no error flag
                updateButtonStates(saveButton, revertButton, content, false);
                console.log('Button states updated by lint event (no errors)');
            }
        });

        // Initial lint after a short delay to ensure editor is fully loaded
        setTimeout(() => {
            // Perform the lint
            editor.performLint();

            // Manually check and enable the button if there are no errors
            // This is a fallback in case the lint event doesn't fire correctly
            setTimeout(() => {
                const saveButton = document.getElementById('saveButton');
                const revertButton = document.getElementById('revertButton');
                try {
                    const content = editor.getValue();
                    let isValidYaml = true;

                    // Only validate if we have content
                    if (content && content.trim()) {
                        try {
                            jsyaml.load(content);
                        } catch (e) {
                            isValidYaml = false;
                            console.log('YAML validation error in initialization:', e.message);
                        }

                        // Update button states based on content validity
                        updateButtonStates(saveButton, revertButton, content, !isValidYaml);
                        console.log('Button states updated by initial validation');

                        // Also clear the lint status if it exists and YAML is valid
                        if (lintStatusEl && isValidYaml) {
                            lintStatusEl.innerHTML = '';
                        }
                    } else {
                        // Empty content is considered valid
                        updateButtonStates(saveButton, revertButton, content, false);
                        console.log('Button states updated for empty content');
                    }
                } catch (e) {
                    // Something went wrong, keep the save button disabled but enable revert if changed
                    console.log('Error during initialization button state update:', e.message);

                    const content = editor.getValue();
                    updateButtonStates(saveButton, revertButton, content, true);
                }
            }, 300);
        }, 800);
    }

    // Apply dark mode if needed
    if (isDarkMode) {
        // Make sure the CodeMirror editor has proper dark mode styling
        const cmElement = editor.getWrapperElement();
        cmElement.style.backgroundColor = '#2d2d2d';

        // Style the gutters
        const gutters = document.querySelectorAll('.CodeMirror-gutters');
        gutters.forEach(gutter => {
            gutter.style.backgroundColor = '#2d2d2d';
            gutter.style.borderRight = '1px solid #444';
        });

        // Force a refresh to ensure all styles are applied properly
        editor.refresh();
    }
}

// Handle page load
document.addEventListener('DOMContentLoaded', function() {
    const textarea = document.getElementById('appsContent');
    if (textarea && textarea.value.trim() === '') {
        textarea.placeholder = 'apps.yaml content could not be loaded';
    }

    // Initialize CodeMirror
    initializeCodeMirror();

    // Handle Revert button click
    document.getElementById('revertButton').addEventListener('click', function() {
        if (confirm('This will discard all your unsaved changes and reload the file from disk. Are you sure?')) {
            // Remove saved content from localStorage
            localStorage.removeItem('appsYamlContent');

            // Reload the page to get fresh content from disk
            window.location.reload();
        }
    });

    // Add a direct listener to ensure the button gets enabled
    // This is a final fallback in case other methods fail
    setTimeout(() => {
        const saveButton = document.getElementById('saveButton');
        const revertButton = document.getElementById('revertButton');

        if (editor) {
            // Force a final validation check
            try {
                const content = editor.getValue();
                const hasChanged = content !== window.originalContent;

                // Check YAML validity
                let isValid = true;
                if (content && content.trim()) {
                    try {
                        jsyaml.load(content);
                    } catch (e) {
                        isValid = false;
                        console.log('YAML validation error in DOMContentLoaded final check:', e.message);
                    }
                }

                // Update buttons states consistently
                updateButtonStates(saveButton, revertButton, content, !isValid);
                console.log('Button states updated by DOMContentLoaded final check, YAML valid:', isValid);

            } catch (e) {
                console.log('YAML validation error in DOMContentLoaded:', e.message);
                // We already know there's an error, but we won't disable the button here
                // as that should be handled by the lint event
            }

            // Manual override for debugging: add a global function to force-enable the button
            window.enableSaveButton = function() {
                const saveButton = document.getElementById('saveButton');
                const revertButton = document.getElementById('revertButton');
                if (saveButton) {
                    // Force enable the save button for debugging purposes by treating content as changed and valid
                    const content = editor ? editor.getValue() : '';
                    updateButtonStates(saveButton, revertButton, content, false);
                }
            };
        }
    }, 2000); // Wait longer for everything to initialize

    // Add a listener for dark mode toggle
    window.addEventListener('storage', function(e) {
        if (e.key === 'darkMode') {
            if (editor) {
                const isDarkMode = localStorage.getItem('darkMode') === 'true';
                editor.setOption('theme', isDarkMode ? 'monokai' : 'default');

                // Update the editor's wrapper element styling
                const cmElement = editor.getWrapperElement();

                if (isDarkMode) {
                    cmElement.style.backgroundColor = '#2d2d2d';

                    // Style the gutters
                    const gutters = document.querySelectorAll('.CodeMirror-gutters');
                    gutters.forEach(gutter => {
                        gutter.style.backgroundColor = '#2d2d2d';
                        gutter.style.borderRight = '1px solid #444';
                    });

                    // Re-style any lint tooltips that might be open
                    const tooltips = document.querySelectorAll('.CodeMirror-lint-tooltip');
                    tooltips.forEach(tooltip => {
                        tooltip.style.backgroundColor = '#2d2d2d';
                        tooltip.style.color = '#f0f0f0';
                        tooltip.style.borderColor = '#444';
                    });
                } else {
                    cmElement.style.backgroundColor = '#ffffff';

                    // Style the gutters
                    const gutters = document.querySelectorAll('.CodeMirror-gutters');
                    gutters.forEach(gutter => {
                        gutter.style.backgroundColor = '#f8f8f8';
                        gutter.style.borderRight = '1px solid #ddd';
                    });

                    // Re-style any lint tooltips that might be open
                    const tooltips = document.querySelectorAll('.CodeMirror-lint-tooltip');
                    tooltips.forEach(tooltip => {
                        tooltip.style.backgroundColor = '#ffffff';
                        tooltip.style.color = '#333';
                        tooltip.style.borderColor = '#ccc';
                    });
                }

                // Re-run the linter
                editor.performLint();

                // Force a refresh to ensure all styles are applied properly
                editor.refresh();
            }
        }
    });
});
</script>

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

    def get_menu_html(self, calculating=False):
        """
        Return the Predbat Menu page as HTML
        """
        text = ""
        # Check if there are configuration errors
        config_warning = ""
        if self.base.arg_errors:
            config_warning = '<span style="color: #ffcc00; margin-left: 5px;">&#9888;</span>'

        # Define status icon based on calculating state
        status_icon = ""
        if calculating:
            status_icon = '<span class="mdi mdi-sync mdi-spin calculating-icon" style="color: #4CAF50; font-size: 24px; margin-left: 10px; margin-right: 10px;" title="Calculation in progress..."></span>'
        else:
            status_icon = '<span class="mdi mdi-check-circle idle-icon" style="color: #4CAF50; font-size: 24px; margin-left: 10px; margin-right: 10px;" title="System idle"></span>'

        text += (
            """
<style>
.menu-bar {
    background-color: #ffffff;
    overflow-x: auto; /* Enable horizontal scrolling */
    white-space: nowrap; /* Prevent menu items from wrapping */
    display: flex;
    align-items: center;
    border-bottom: 1px solid #ddd;
    -webkit-overflow-scrolling: touch; /* Smooth scrolling on iOS */
    scrollbar-width: thin; /* Firefox */
    scrollbar-color: #4CAF50 #f0f0f0; /* Firefox */
    position: fixed; /* Change from sticky to fixed */
    top: 0; /* Stick to the top */
    left: 0; /* Ensure it starts from the left edge */
    right: 0; /* Ensure it extends to the right edge */
    width: 100%; /* Make sure it spans the full width */
    z-index: 1000; /* Ensure it's above other content */
    box-shadow: 0 2px 5px rgba(0, 0, 0, 0.1); /* Add subtle shadow for visual separation */
}

/* Add padding to body to prevent content from hiding under fixed header */
body {
    padding-top: 65px; /* Increased padding to account for the fixed menu height */
}

.menu-bar .logo {
    display: flex;
    align-items: center;
    padding: 0 16px;
    min-width: fit-content; /* Prevent logo from shrinking */
}

.menu-bar .logo img {
    height: 40px;
    margin-right: 10px;
}

.menu-bar .logo-text {
    font-size: 24px;
    font-weight: bold;
    color: #333;
    white-space: nowrap;
}

.menu-bar a {
    color: #333;
    text-align: center;
    padding: 14px 16px;
    text-decoration: none;
    font-size: 16px;
    display: flex;
    align-items: center;
    white-space: nowrap;
    flex-shrink: 0; /* Prevent items from shrinking */
}



.menu-bar a:hover {
    background-color: #f0f0f0;
    color: #4CAF50;
}

.menu-bar a.active {
    background-color: #4CAF50;
    color: white;
}

.dark-mode-toggle {
    margin-left: auto;
    padding: 14px 16px;
    flex-shrink: 0; /* Prevent from shrinking */
}

.dark-mode-toggle button {
    background-color: #f0f0f0;
    color: #333;
    border: 1px solid #ddd;
    padding: 8px 12px;
    border-radius: 4px;
    cursor: pointer;
    white-space: nowrap;
}

.dark-mode-toggle button:hover {
    background-color: #e0e0e0;
}

/* Dark mode menu styles */
body.dark-mode .menu-bar {
    background-color: #1e1e1e;
    border-bottom: 1px solid #333;
    scrollbar-color: #4CAF50 #333; /* Firefox */
}

body.dark-mode .menu-bar::-webkit-scrollbar-track {
    background: #333;
}

body.dark-mode .menu-bar .logo-text {
    color: white;
}

body.dark-mode .menu-bar a {
    color: white;
}

body.dark-mode .menu-bar a:hover {
    background-color: #2c652f;
    color: white;
}

body.dark-mode .menu-bar a.active {
    background-color: #4CAF50;
    color: white;
}

body.dark-mode .calculating-icon {
    color: #6CFF72 !important;
}

body.dark-mode .idle-icon {
    color: #6CFF72 !important;
}

body.dark-mode .dark-mode-toggle button {
    background-color: #444;
    color: #e0e0e0;
    border-color: #555;
}

body.dark-mode .dark-mode-toggle button:hover {
    background-color: #666;
}
</style>

<script>
// Add viewport meta tag if it doesn't exist
if (!document.querySelector('meta[name="viewport"]')) {
    const meta = document.createElement('meta');
    meta.name = 'viewport';
    meta.content = 'width=device-width, initial-scale=1, maximum-scale=1';
    document.head.appendChild(meta);
}

// Store the active menu item in session storage
function storeActiveMenuItem(path) {
    localStorage.setItem('activeMenuItem', path);
}

// Function to set the active menu item
function setActiveMenuItem() {
    // Get all menu links
    const menuLinks = document.querySelectorAll('.menu-bar a');

    // Get current page path from window location
    let currentPath = window.location.pathname;

    // Handle paths with trailing slash
    if (currentPath.endsWith('/')) {
        currentPath = currentPath.slice(0, -1);
    }

    // Default page from server if nothing else matches
    const defaultPage = '"""
            + self.default_page
            + """';

    // First try to get the active page from session storage (in case of resize or direct navigation)
    const storedActivePage = localStorage.getItem('activeMenuItem');

    let currentPage = currentPath;

    // If the current page is the root, check if we have a stored page
    if (currentPath === '' || currentPath === '/') {
        if (storedActivePage) {
            currentPage = storedActivePage;
        } else {
            currentPage = defaultPage;
        }
    } else {
        // Store the current page for future reference
        localStorage.setItem('activeMenuItem', currentPage);
    }

    let activeFound = false;

    // Remove active class from all links
    menuLinks.forEach(link => {
        link.classList.remove('active');

        // Check if this link's href matches the current page
        const linkPath = new URL(link.href).pathname;

        // Ensure we're comparing cleanly
        const cleanLinkPath = linkPath.endsWith('/') ? linkPath.slice(0, -1) : linkPath;
        const cleanCurrentPage = currentPage.endsWith('/') ? currentPage.slice(0, -1) : currentPage;

        // Match either the exact path or paths with a leading ./
        // (since server-side our paths often have ./ prefix)
        if (cleanCurrentPage === cleanLinkPath ||
            cleanLinkPath.endsWith(cleanCurrentPage) ||
            cleanCurrentPage.endsWith(cleanLinkPath)) {
            link.classList.add('active');
            activeFound = true;
        }
    });

    // If no active item was found, set default
    if (!activeFound && menuLinks.length > 0) {
        const defaultLink = menuLinks[0]; // Set first menu item as default
        defaultLink.classList.add('active');
        storeActiveMenuItem(new URL(defaultLink.href).pathname);
    }

    // Scroll active item into view
    const activeItem = document.querySelector('.menu-bar a.active');
    if (activeItem) {
        // Scroll with a slight offset to make it more visible
        const menuBar = document.querySelector('.menu-bar');
        const activeItemLeft = activeItem.offsetLeft;
        const menuBarWidth = menuBar.clientWidth;
        menuBar.scrollLeft = activeItemLeft - menuBarWidth / 2 + activeItem.clientWidth / 2;
    }
}

// Initialize menu on page load
document.addEventListener("DOMContentLoaded", function() {
    setActiveMenuItem();

    // For each menu item, add click handler to set it as active
    const menuLinks = document.querySelectorAll('.menu-bar a');
    menuLinks.forEach(link => {
        link.addEventListener('click', function(e) {
            // Don't override external links (like Docs)
            if (!this.href.includes(window.location.hostname)) {
                return;
            }

            // Remove active class from all links
            menuLinks.forEach(l => l.classList.remove('active'));

            // Add active class to clicked link
            this.classList.add('active');

            // Store the clicked menu item path
            storeActiveMenuItem(new URL(this.href).pathname);
        });
    });
});

// Additional window.onload handler for other functionality
const originalOnLoad = window.onload;
window.onload = function() {
    // Call the original onload function if it exists
    if (typeof originalOnLoad === 'function') {
        originalOnLoad();
    }
    applyDarkMode();
};

// Handle window resize without losing active menu item
window.addEventListener('resize', function() {
    // Don't reload the page, just make sure the active menu item is visible
    setTimeout(function() {
        const activeItem = document.querySelector('.menu-bar a.active');
        if (activeItem) {
            const menuBar = document.querySelector('.menu-bar');
            const activeItemLeft = activeItem.offsetLeft;
            const menuBarWidth = menuBar.clientWidth;
            menuBar.scrollLeft = activeItemLeft - menuBarWidth / 2 + activeItem.clientWidth / 2;
        }
    }, 100);
});
</script>

<div class="menu-bar">
    <div class="logo">
        <img id="logo-image"
             src="https://raw.githubusercontent.com/springfall2008/batpred/refs/heads/main/docs/images/bat_logo_light.png"
             data-light-src="https://raw.githubusercontent.com/springfall2008/batpred/refs/heads/main/docs/images/bat_logo_light.png"
             data-dark-src="https://raw.githubusercontent.com/springfall2008/batpred/refs/heads/main/docs/images/bat_logo_dark.png"
             alt="Predbat Logo"
             onclick="flyBat()"
             style="cursor: pointer;"
        >
        """
            + status_icon
            + """
        <div class="battery-wrapper">
            """
            + self.get_battery_status_icon()
            + """
        </div>
    </div>
    <a href='./dash'>Dash</a>
    <a href='./plan'>Plan</a>
    <a href='./entity'>Entities</a>
    <a href='./charts'>Charts</a>
    <a href='./config'>Config"""
            + config_warning
            + """</a>
    <a href='./apps'>Apps</a>
    <a href='./apps_editor'>Editor</a>
    <a href='./log'>Log</a>
    <a href='./compare'>Compare</a>
    <a href='https://springfall2008.github.io/batpred/'>Docs</a>
    <div class="dark-mode-toggle">
        """
            + THIS_VERSION
            + """
        <button onclick="toggleDarkMode()">Toggle Dark Mode</button>
    </div>
</div>
"""
        )
        return text

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
            self.log(f"Rate override requested: {action} at {time_str}")

            # Validate inputs
            if not time_str or not action:
                self.log("ERROR: Missing required parameters for rate override")
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
