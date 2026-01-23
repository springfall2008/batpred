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
import os.path
import sys
import re
from datetime import datetime, timedelta
import json
import shutil
import html as html_module
import urllib.parse
import traceback
import threading
import io
from io import StringIO
from ruamel.yaml import YAML
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

from web_helper import (
    get_header_html,
    get_plan_css,
    get_editor_js,
    get_editor_css,
    get_log_css,
    get_charts_css,
    get_apps_css,
    get_html_config_css,
    get_apps_js,
    get_components_css,
    get_entity_modal_css,
    get_component_edit_modal_css,
    get_entity_modal_js,
    get_component_edit_modal_js,
    get_logfile_js,
    get_entity_toggle_js,
    get_entity_control_css,
    get_entity_css,
    get_entity_js,
    get_restart_button_js,
    get_browse_css,
    get_entity_detailed_row_js,
    get_internals_css,
    get_internals_js,
    get_dashboard_css,
    get_dashboard_collapsible_js,
)

from utils import calc_percent_limit, str2time, dp0, dp2, format_time_ago, get_override_time_from_string, history_attribute, prune_today
from const import TIME_FORMAT, TIME_FORMAT_DAILY, TIME_FORMAT_HA
from predbat import THIS_VERSION
from component_base import ComponentBase
from config import APPS_SCHEMA

ROOT_YAML_KEY = "pred_bat"


class WebInterface(ComponentBase):
    def initialize(self, web_port):
        self.default_page = "./dash"
        self.web_port = web_port
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
        app.router.add_post("/entity", self.html_entity_post)
        app.router.add_post("/config", self.html_config_post)
        app.router.add_get("/dash", self.html_dash)
        app.router.add_post("/dash", self.html_dash_post)
        app.router.add_get("/components", self.html_components)
        app.router.add_get("/component_entities", self.html_component_entities)
        app.router.add_post("/component_restart", self.html_component_restart)
        app.router.add_get("/component_config", self.html_component_config)
        app.router.add_post("/component_config_save", self.html_component_config_save)
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
        app.router.add_get("/api/entities", self.html_api_get_entities)
        app.router.add_post("/api/login", self.html_api_login)
        app.router.add_get("/browse", self.html_browse)
        app.router.add_get("/download", self.html_download_file)
        app.router.add_get("/internals", self.html_internals)
        app.router.add_get("/api/internals", self.html_api_internals)
        app.router.add_get("/api/internals/download", self.html_api_internals_download)

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
        count = 0
        while not self.api_stop:
            await asyncio.sleep(1)
            if count % 60 == 0:
                self.update_success_timestamp()
            count += 1
        await runner.cleanup()

        self.api_started = False
        print("Web interface stopped")

    def get_attributes_html(self, entity, from_db=False):
        """
        Return the attributes of an entity as HTML
        """
        text = ""
        attributes = {}
        if from_db:
            history = self.get_history_wrapper(entity, 1, required=False)
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
            full_value = str(value)[:16384]  # Limit to 16k
            full_value = full_value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#39;")
            if len(str(value)) > 128:
                display_value = str(full_value)[:128] + " ... "
                # Escape HTML entities for tooltip
                text += '<tr><td>{}</td><td title="{}">{}</td></tr>'.format(key, full_value, display_value)
            else:
                # Also escape HTML entities for short values
                text += "<tr><td>{}</td><td>{}</td></tr>".format(key, full_value)
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

        debug_enable, ignore = self.get_ha_config("debug_enable", None)
        read_only, ignore = self.get_ha_config("set_read_only", None)
        mode, ignore = self.get_ha_config("mode", None)

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

        last_updated = self.get_state_wrapper("predbat.status", attribute="last_updated", default=None)
        if status and (("Warn:" in status) or ("Error:" in status)):
            text += "<tr><td>Status</td><td bgcolor=#ff7777>{}</td></tr>\n".format(status)
        elif not is_running:
            text += "<tr><td colspan='2' bgcolor='#ff7777'>{} (unhealthy)</td></tr>\n".format(status)
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

        text += "<tr><td>SoC</td><td>{}</td></tr>\n".format(self.get_battery_status_icon())

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
        if self.arg_errors:
            count_errors = len(self.arg_errors)
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
        text_plan = self.get_state_wrapper(entity_id=self.prefix + ".plan_html", attribute="text", default="No plan available")
        text += "<tr><td>{}</td></tr>\n".format(text_plan)
        text += "</table>\n"

        # Form the app list
        app_list = ["predbat"]
        for entity_id in self.base.dashboard_index_app.keys():
            app = self.base.dashboard_index_app[entity_id]
            if app not in app_list:
                app_list.append(app)

        # Add expand/collapse all button
        text += '<div style="margin: 20px 0;">\n'
        text += '<button id="expandAllBtn" class="expand-all-button" onclick="toggleAllSections()">Expand All</button>\n'
        text += "</div>\n"

        # Display per app
        for app in app_list:
            section_id = f"section-{app}"

            # Build entity list first to get count
            if app == "predbat":
                entity_list = self.base.dashboard_index
            else:
                entity_list = []
                for entity_id in self.base.dashboard_index_app.keys():
                    if self.base.dashboard_index_app[entity_id] == app:
                        entity_list.append(entity_id)

            entity_count = len(entity_list)
            entity_word = "entity" if entity_count == 1 else "entities"

            text += f'<div class="dashboard-section">\n'
            text += f'<h2 class="dashboard-section-header" onclick="toggleDashboardSection(\'{section_id}\')">\n'
            text += f'<span class="expand-icon" id="icon-{section_id}">+</span> {app[0].upper() + app[1:]} Entities ({entity_count} {entity_word})\n'
            text += "</h2>\n"
            text += f'<div id="{section_id}" class="dashboard-section-content collapsed">\n'
            text += "<table>\n"
            text += "<tr><th></th><th>Name</th><th>Entity</th><th>State</th><th>Attributes</th></tr>\n"

            for entity in entity_list:
                text += self.html_get_entity_text(entity)
            text += "</table>\n"
            text += "</div>\n"
            text += "</div>\n"

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
            state = self.get_state_wrapper(entity_id=entity)
            unit_of_measurement = self.get_state_wrapper(entity_id=entity, attribute="unit_of_measurement")
            friendly_name = self.get_state_wrapper(entity_id=entity, attribute="friendly_name")
            text += '<tr><td> {} </td><td> <a href="./entity?entity_id={}"> {} </a></td><td>{}</td><td>{} {}</td><td>{}</td></tr>\n'.format("", entity, friendly_name, entity, state, unit_of_measurement, self.get_attributes_html(entity, from_db=True))
        return text

    def get_entity_list_data(self):
        """
        Generate entity list data for the entity selector
        """
        # Get app list
        app_list = ["predbat"]
        for entity_id in self.base.dashboard_index_app.keys():
            app = self.base.dashboard_index_app[entity_id]
            if app not in app_list:
                app_list.append(app)

        entity_data_list = []

        for app in app_list:
            if app == "predbat":
                entity_list = self.base.dashboard_index if hasattr(self.base, "dashboard_index") and self.base.dashboard_index else []
            else:
                entity_list = []
                if hasattr(self.base, "dashboard_index_app") and self.base.dashboard_index_app:
                    for entity_id in self.base.dashboard_index_app.keys():
                        if self.base.dashboard_index_app[entity_id] == app:
                            entity_list.append(entity_id)

            for entity_id in entity_list:
                if hasattr(self.base, "dashboard_values") and self.base.dashboard_values:
                    attributes = self.base.dashboard_values.get(entity_id, {}).get("attributes", {})
                    entity_friendly_name = attributes.get("friendly_name", entity_id)
                    unit = attributes.get("unit_of_measurement", "")
                    if unit:
                        entity_friendly_name = f"{entity_friendly_name} ({unit})"
                else:
                    entity_friendly_name = entity_id

                entity_data_list.append({"id": entity_id, "name": entity_friendly_name, "group": f"{app[0].upper() + app[1:]} Entities"})

        # Add config settings
        if hasattr(self.base, "CONFIG_ITEMS") and self.base.CONFIG_ITEMS:
            for item in self.base.CONFIG_ITEMS:
                if self.base.user_config_item_enabled(item):
                    entity_id = item.get("entity", "")
                    entity_friendly_name = item.get("friendly_name", "")
                    unit = item.get("unit", "")
                    if unit:
                        entity_friendly_name = f"{entity_friendly_name} ({unit})"
                    if entity_id:
                        entity_data_list.append({"id": entity_id, "name": entity_friendly_name, "group": "Config Settings"})

        return entity_data_list

    async def html_api_get_entities(self, request):
        """
        API endpoint to get entity list as JSON
        """
        try:
            entity_list = self.get_entity_list_data()
            return web.json_response(entity_list)
        except Exception as e:
            self.base.log(f"Error in html_api_get_entities: {e}")
            return web.json_response({"error": str(e)}, status=500)

    def is_data_numerical(self, history, attribute=None):
        """
        Check if history data is numerical (supports both state and attribute checking)
        Returns True if at least 10% of values are numeric or boolean
        """
        count_nums = 0
        count_total = 0

        if history and len(history) >= 1:
            for item in history[0]:
                if attribute:
                    # Check attribute value
                    attr_value = item.get("attributes", {}).get(attribute, None)
                    if attr_value is None:
                        continue
                    value = str(attr_value)
                else:
                    # Check state value
                    value = item.get("state", None)
                    if value is None:
                        continue
                    value = str(value)

                if value.lower() in ["on", "off", "true", "false"]:
                    count_nums += 1
                else:
                    try:
                        float(value)
                        count_nums += 1
                    except (ValueError, TypeError):
                        pass
                count_total += 1

        if count_total > 0 and (count_nums / count_total) >= 0.1:
            return True
        elif count_total == 0:
            return True
        return False

    async def get_history_with_now(self, entity_id, days, attribute=None):
        """
        Get history for an entity including the current state
        """
        history = self.get_history_wrapper(entity_id, days, required=False, tracked=False)
        current_value = self.get_state_wrapper(entity_id=entity_id, attribute=attribute)
        if current_value is not None:
            if not history:
                history = [[]]
                if attribute:
                    history[0].append({"attributes": {attribute: current_value}, "last_updated": (self.now_utc - timedelta(days=days)).strftime(TIME_FORMAT_HA)})
                else:
                    history[0].append({"state": current_value, "last_updated": (self.now_utc - timedelta(days=days)).strftime(TIME_FORMAT_HA)})
            if attribute:
                history[0].append({"attributes": {attribute: current_value}, "last_updated": self.now_utc.strftime(TIME_FORMAT_HA)})
            else:
                history[0].append({"state": current_value, "last_updated": self.now_utc.strftime(TIME_FORMAT_HA)})

        return history

    def get_entity_attributes(self, entity_id):
        """
        get_entity_attributes returns a list of attribute names for the given entity_id
        """
        state_info = self.get_state_wrapper(entity_id=entity_id, raw=True)
        if state_info and ("attributes" in state_info) and isinstance(state_info["attributes"], dict):
            attr_list = sorted(list(state_info["attributes"].keys()))
            for attr in ["friendly_name", "icon", "unit_of_measurement", "device_class", "state_class"]:
                try:
                    attr_list.remove(attr)
                except ValueError:
                    pass
            return attr_list
        return []

    async def html_entity(self, request):
        """
        Return the Predbat entity as an HTML page
        """
        # Support multiple entity_id parameters
        entity_ids = request.query.getall("entity_id", [])
        if not entity_ids:
            # Fallback to single entity_id for backward compatibility
            single_entity = request.query.get("entity_id", "")
            entity_ids = [single_entity] if single_entity else []

        # Get attribute selections (parallel to entity_ids)
        entity_attributes = request.query.getall("entity_attribute", [])

        # Build entity_selections list pairing entity IDs with attributes
        # Each entity can have multiple attributes (comma-separated)
        entity_selections = []
        for i, entity_id in enumerate(entity_ids):
            if entity_id:
                attr_string = entity_attributes[i] if i < len(entity_attributes) else ""
                # Parse comma-separated attributes
                if attr_string:
                    # Keep empty strings (they represent state)
                    attrs = [a.strip() if a.strip() else None for a in attr_string.split(",")]
                else:
                    attrs = [None]  # Default to state

                # Create one selection per attribute
                for attr in attrs:
                    entity_selections.append({"entity_id": entity_id, "attribute": attr})

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

        # Collect available attributes for all selected entities
        entity_attributes_map = {}
        entity_data_fetch = {}
        for selection in entity_selections:
            entity_id = selection["entity_id"]
            attribute = selection["attribute"]
            history = await self.get_history_with_now(entity_id, days, attribute=None)
            entity_data_fetch[entity_id] = history
            available_attrs = self.get_entity_attributes(entity_id)
            entity_attributes_map[entity_id] = available_attrs

        # Build selected entities data structure with attributes grouped by entity
        entity_attr_groups = {}
        for selection in entity_selections:
            entity_id = selection["entity_id"]
            attr = selection["attribute"] or ""
            if entity_id not in entity_attr_groups:
                entity_attr_groups[entity_id] = []
            if attr not in entity_attr_groups[entity_id]:
                entity_attr_groups[entity_id].append(attr)

        # Convert to array format for JavaScript
        selected_entities_data = [{"entity": entity_id, "attributes": attrs} for entity_id, attrs in entity_attr_groups.items()]
        selected_entities_json = json.dumps(selected_entities_data)
        entity_attributes_json = json.dumps(entity_attributes_map)

        # Add entity multi-select dropdown with checkboxes
        text += """<div style="margin-bottom: 20px;">
            <form id="entitySelectForm" method="get" action="./entity">
                <label for="entitySearchInput" style="margin-right: 10px; font-weight: bold;">Select Entities: </label>
                <div class="entity-search-container" style="position: relative; max-width: 800px;">
                    <input type="text" id="entitySearchInput" name="entity_search"
                           placeholder="Type to search entities... (click to show all)"
                           style="width: 100%; padding: 8px 30px 8px 8px; border-radius: 4px; border: 1px solid #ddd; box-sizing: border-box;"
                           autocomplete="off" />
                    <button type="button" id="clearEntitySearch"
                            style="position: absolute; right: 5px; top: 50%; transform: translateY(-50%); background: none; border: none; font-size: 16px; color: #999; cursor: pointer; padding: 2px 5px;"
                            title="Clear search">×</button>
                    <div id="entityDropdown" class="entity-dropdown"
                         style="position: absolute; top: 100%; left: 0; right: 0; background: white; border: 1px solid #ddd; border-top: none; max-height: 400px; overflow-y: auto; z-index: 1000; display: none; box-shadow: 0 2px 4px rgba(0,0,0,0.1);">
                    </div>
                </div>
                <div id="selectedEntitiesDisplay" style="margin-top: 10px; padding: 10px; border: 1px solid #ddd; border-radius: 4px; min-height: 40px; background-color: var(--background-secondary, #f9f9f9);"></div>
                <input type="hidden" name="days" value="{}" />
                <button type="submit" style="margin-top: 10px; padding: 8px 16px; background-color: #4CAF50; color: white; border: none; border-radius: 4px; cursor: pointer; font-weight: bold;">Update Chart</button>
            </form>
        </div>""".format(
            days
        )

        # Add days selector
        text += """<div style="margin-bottom: 20px;">
            <form id="daysSelectForm" style="display: flex; align-items: center;" method="get" action="./entity">
                <label for="daysSelect" style="margin-right: 10px; font-weight: bold;">History Days: </label>
                <select id="daysSelect" name="days" style="padding: 8px; border-radius: 4px; border: 1px solid #ddd;" onchange="document.getElementById('daysSelectForm').submit();">
        """

        # Add days options
        for option in [1, 2, 3, 4, 5, 7, 10, 14, 21, 30, 60, 90]:
            selected = "selected" if option == days else ""
            text += f'<option value="{option}" {selected}>{option} days</option>'

        text += """
                </select>
        """

        # Add hidden inputs for all selected entities with attributes
        for entity_id, attrs in entity_attr_groups.items():
            attr_string = ",".join(attrs)
            text += f'<input type="hidden" name="entity_id" value="{entity_id}" />'
            text += f'<input type="hidden" name="entity_attribute" value="{attr_string}" />'

        text += """
            </form>
        </div>"""
        # CSS
        text += get_entity_css()

        # Add JavaScript and CSS for entity list and search (with attribute selection support)
        text += get_entity_js(selected_entities_json, entity_attributes_json)

        if entity_selections:
            # Group entities by unit of measurement
            entity_groups = {}
            for selection in entity_selections:
                entity_id = selection["entity_id"]
                attribute = selection["attribute"]

                attributes = self.base.dashboard_values.get(entity_id, {}).get("attributes", {})
                unit = attributes.get("unit_of_measurement", "") or "(no unit)"

                if unit not in entity_groups:
                    entity_groups[unit] = []

                entity_groups[unit].append({"id": entity_id, "friendly_name": attributes.get("friendly_name", entity_id), "unit": unit, "attribute": attribute, "available_attrs": entity_attributes_map.get(entity_id, [])})

            # Display entity details table for first selected entity
            if len(entity_selections) == 1:
                entity = entity_selections[0]["entity_id"]
                config_text = self.html_config_item_text(entity)
                if not config_text:
                    text += "<table>\n"
                    text += "<tr><th></th><th>Name</th><th>Entity</th><th>State</th><th>Attributes</th></tr>\n"
                    text += self.html_get_entity_text(entity)
                    text += "</table>\n"
                else:
                    text += config_text

                control_html = self.get_entity_control_html(entity, days)
                if control_html:
                    text += control_html
            else:
                # Show summary of selected entities
                text += "<h2>Selected Entities ({})</h2>\n".format(len(entity_selections))
                text += "<table>\n"
                text += "<tr><th>Name</th><th>Entity</th><th>Attribute</th><th>Current State</th><th>Unit</th></tr>\n"
                for selection in entity_selections:
                    entity_id = selection["entity_id"]
                    attribute = selection["attribute"]
                    if entity_id:
                        text += self.html_get_entity_text(entity_id)
                text += "</table>\n"

            # Create separate charts for each unit group
            now_str = self.now_utc.strftime(TIME_FORMAT)

            for unit, entities in entity_groups.items():
                text += "<h2>History Chart - {}</h2>\n".format(unit if unit != "(no unit)" else "(no unit)")
                chart_id = "chart_{}".format(unit.replace("/", "_").replace(" ", "_").replace("(", "").replace(")", ""))
                text += '<div id="{}"></div>'.format(chart_id)
                is_numerical = False

                # First, collect all entity data
                entity_data = []
                for entity_info in entities:
                    entity_id = entity_info["id"]
                    friendly_name = entity_info["friendly_name"]
                    attribute = entity_info.get("attribute")

                    # Fetch history with attribute if specified
                    history = entity_data_fetch[entity_id]

                    # Check if data is numerical (supports both state and attribute)
                    is_numerical = self.is_data_numerical(history, attribute=attribute)

                    # Extract chart data using history_attribute
                    if attribute:
                        # Chart attribute data
                        history_chart = history_attribute(history, state_key=attribute, attributes=True, is_numerical=is_numerical)
                        display_name = f"{friendly_name} ({attribute})"
                    else:
                        # Chart state data (default)
                        history_chart = history_attribute(history, is_numerical=is_numerical)
                        display_name = friendly_name

                    if history_chart:
                        entity_data.append({"name": display_name, "entity_id": entity_id, "data": history_chart})

                # Prepare data for the appropriate chart type
                if is_numerical:
                    series_data = [{"name": item["name"], "data": item["data"], "chart_type": "line", "stroke_width": "2", "stroke_curve": "stepline"} for item in entity_data]
                    chart_unit = unit if unit != "(no unit)" else ""
                    chart_title = "{} entities".format(len(entities)) if len(entities) > 1 else entities[0]["friendly_name"]
                    text += self.render_chart(series_data, chart_unit, chart_title, now_str, tagname=chart_id)
                else:
                    # Render timeline chart for non-numerical data
                    text += self.render_timeline_chart(entity_data, chart_id, days)

            # History table showing all selected entities
            if entity_selections:
                text += "<h2>History</h2>\n"
                text += """
                <style>
                .history-row { cursor: pointer; }
                .history-row:hover { background-color: var(--hover-color, #f5f5f5); }
                .detail-row { display: none; background-color: var(--detail-bg, #fafafa); }
                .detail-row td { padding-left: 30px; font-size: 0.9em; color: var(--text-secondary, #666); }
                .expanded { background-color: var(--expanded-bg, #e8f4f8) !important; }
                </style>
                """
                text += "<table>\n"

                # Build header row
                text += "<tr><th>Time</th>"
                for selection in entity_selections:
                    entity_id = selection["entity_id"]
                    attribute = selection["attribute"]
                    attributes = self.base.dashboard_values.get(entity_id, {}).get("attributes", {})
                    friendly_name = attributes.get("friendly_name", entity_id)
                    unit = attributes.get("unit_of_measurement", "")
                    unit_display = f" ({unit})" if unit else ""
                    attr_display = f" - {attribute}" if attribute else ""
                    text += f"<th>{friendly_name}{attr_display}{unit_display}</th>"
                text += "</tr>\n"

                # Collect history data for all entities (both 30-min summary and 5-min detail)
                entity_histories_30min = []
                entity_histories_5min = []
                all_timestamps_30min = set()

                for selection in entity_selections:
                    entity_id = selection["entity_id"]
                    attribute = selection["attribute"]
                    history = entity_data_fetch[entity_id]
                    entity_data_30min = {}
                    entity_data_5min = {}

                    if history and len(history) >= 1:
                        history = history[0]
                        if history:
                            history.reverse()
                            for item in history:
                                if "last_updated" not in item:
                                    continue
                                last_updated_time = item["last_updated"]
                                last_updated_stamp = str2time(last_updated_time)

                                # Get state or attribute value
                                if attribute:
                                    state = item.get("attributes", {}).get(attribute, None)
                                else:
                                    state = item.get("state", None)

                                if state is None:
                                    state = "None"

                                # Store 5-minute interval data
                                minutes = last_updated_stamp.hour * 60 + last_updated_stamp.minute
                                rounded_minutes_5 = (minutes // 5) * 5
                                rounded_stamp_5 = last_updated_stamp.replace(minute=rounded_minutes_5 % 60, hour=rounded_minutes_5 // 60, second=0, microsecond=0)
                                entity_data_5min[rounded_stamp_5] = state

                                # Round to 30-minute intervals for summary
                                rounded_minutes_30 = (minutes // 30) * 30
                                rounded_stamp_30 = last_updated_stamp.replace(minute=rounded_minutes_30 % 60, hour=rounded_minutes_30 // 60, second=0, microsecond=0)
                                entity_data_30min[rounded_stamp_30] = state
                                all_timestamps_30min.add(rounded_stamp_30)

                    entity_histories_30min.append(entity_data_30min)
                    entity_histories_5min.append(entity_data_5min)

                # Sort timestamps in reverse chronological order
                sorted_timestamps_30min = sorted(all_timestamps_30min, reverse=True)

                # Build table rows with expandable detail
                row_index = 0
                for timestamp_30 in sorted_timestamps_30min:
                    # Main 30-minute row (clickable)
                    text += f'<tr class="history-row" onclick="toggleDetailRow({row_index})" id="row_{row_index}">'
                    text += f"<td>▶ {timestamp_30.strftime(TIME_FORMAT)}</td>"
                    for entity_data in entity_histories_30min:
                        state = entity_data.get(timestamp_30, "-")
                        text += f"<td>{state}</td>"
                    text += "</tr>\n"

                    # Detail rows (5-minute intervals within this 30-minute period)
                    # Calculate 5-minute timestamps for this 30-minute period
                    detail_timestamps = []
                    for offset in range(0, 30, 5):
                        detail_time = timestamp_30 + timedelta(minutes=offset)
                        detail_timestamps.append(detail_time)

                    for detail_time in detail_timestamps:
                        text += f'<tr class="detail-row" id="detail_{row_index}">'
                        text += f"<td>  {detail_time.strftime(TIME_FORMAT)}</td>"
                        for entity_data_5min in entity_histories_5min:
                            state = entity_data_5min.get(detail_time, "-")
                            text += f"<td>{state}</td>"
                        text += "</tr>\n"

                    row_index += 1

                text += "</table><br>\n"

                # Add JavaScript for toggling detail rows
                text += get_entity_detailed_row_js()
        else:
            text += "<h2>Select one or more entities</h2>\n"

        # Return web response
        text += "</body></html>\n"
        return web.Response(content_type="text/html", text=text)

    async def html_entity_post(self, request):
        """
        Handle POST request for entity value updates
        """
        try:
            postdata = await request.post()
            entity_id = postdata.get("entity_id", "")
            new_value = postdata.get("value", "")
            days = postdata.get("days", "7")
            attributes = self.base.dashboard_values.get(entity_id, {}).get("attributes", {})

            if entity_id:
                # Determine entity type from entity_id domain
                domain = entity_id.split(".")[0] if "." in entity_id else ""

                # Convert value based on entity type
                if domain in ["switch", "input_boolean"]:
                    # For toggle switches, check if value is 'on' or 'off'
                    new_value = new_value.lower() == "on"
                elif domain in ["number", "input_number"]:
                    try:
                        new_value = float(new_value) if new_value else 0.0
                    except ValueError:
                        new_value = 0.0
                elif domain in ["select", "input_select"]:
                    # Keep as string for select entities
                    pass

                # Set the entity state
                await self.base.ha_interface.set_state_external(entity_id, new_value, attributes=attributes)
                self.log(f"Entity {entity_id} updated to {new_value} via web interface")

        except Exception as e:
            self.log(f"Error updating entity value: {e}")

        # Redirect back to entity page with same parameters
        redirect_url = f"./entity?entity_id={entity_id}&days={days}"
        raise web.HTTPFound(redirect_url)

    def is_entity_writable(self, entity_id):
        """
        Determine if an entity is writable and return its control type
        Returns: (is_writable, control_type, options)
        """
        if not entity_id or "." not in entity_id:
            return False, None, None

        domain = entity_id.split(".")[0]

        # Check if it's a writable entity type
        if domain in ["switch", "input_boolean"]:
            return True, "toggle", None
        elif domain in ["number", "input_number"]:
            return True, "number", None
        elif domain in ["select", "input_select"]:
            options = self.get_state_wrapper(entity_id=entity_id, attribute="options", default=[])
            return True, "select", options

        return False, None, None

    def get_entity_control_html(self, entity_id, days):
        """
        Generate HTML control for writable entities
        """
        current_value = self.get_state_wrapper(entity_id=entity_id)

        is_writable, control_type, options = self.is_entity_writable(entity_id)

        if not is_writable:
            return ""

        html = '<div class="entity-edit-container" style="margin-top: 15px; padding: 15px; border: 1px solid var(--border-color, #ddd); border-radius: 5px; background-color: var(--background-secondary, #f9f9f9);">'

        # Add CSS for dark mode support
        html += get_entity_control_css()

        html += f'<form method="post" action="./entity" style="display: flex; align-items: center; gap: 10px;">'
        html += f'<input type="hidden" name="entity_id" value="{entity_id}">'
        html += f'<input type="hidden" name="days" value="{days}">'

        if control_type == "toggle":
            is_active = str(current_value).lower() in ["true", "on", "1"]
            toggle_class = "toggle-switch active" if is_active else "toggle-switch"

            html += f'<div style="display: flex; align-items: center; gap: 10px;">'
            html += f'<button class="{toggle_class}" type="button" onclick="toggleEntitySwitch(this, \'{entity_id}\', {days})"></button>'
            html += f"<span>Enable/Disable</span>"
            html += f"</div>"

            # Add JavaScript for toggle functionality
            html += get_entity_toggle_js()

        elif control_type == "number":
            min_val = self.get_state_wrapper(entity_id=entity_id, attribute="min", default=None)
            max_val = self.get_state_wrapper(entity_id=entity_id, attribute="max", default=None)
            step = self.get_state_wrapper(entity_id=entity_id, attribute="step", default=1)
            unit = self.get_state_wrapper(entity_id=entity_id, attribute="unit_of_measurement", default="")

            unit_display = f" {unit}" if unit else ""

            html += f'<input type="number" name="value" value="{current_value}" '
            if min_val is not None and max_val is not None and step is not None:
                html += f'min="{min_val}" max="{max_val}" step="{step}" '
            html += f'style="padding: 8px; border-radius: 4px; border: 1px solid var(--border-color, #ccc); width: 120px; background-color: var(--input-background, #fff); color: var(--text-color, #333);" '
            if unit_display:
                html += f'<span style="margin-left: 5px; color: var(--text-secondary, #666);">{unit}</span>'
            html += '<button type="submit" style="padding: 8px 16px; background-color: #4CAF50; color: white; border: none; border-radius: 4px; cursor: pointer; font-weight: bold;">Update</button>'

        elif control_type == "select" and options:
            html += f'<select name="value" style="padding: 8px; border-radius: 4px; border: 1px solid var(--border-color, #ccc); min-width: 150px; background-color: var(--input-background, #fff); color: var(--text-color, #333);">'
            for option in options:
                selected = "selected" if str(option) == str(current_value) else ""
                html += f'<option value="{option}" {selected}>{option}</option>'
            html += "</select>"
            html += '<button type="submit" style="padding: 8px 16px; background-color: #4CAF50; color: white; border: none; border-radius: 4px; cursor: pointer; font-weight: bold;">Update</button>'

        html += "</form>"
        html += "</div>"

        return html

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
        calculating = self.get_arg("active", False)
        if self.base.update_pending:
            calculating = True
        self.update_success_timestamp()
        return get_header_html(title, calculating, self.default_page, self.arg_errors, THIS_VERSION, self.get_battery_status_icon(), refresh, codemirror=codemirror)

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

    def render_chart(self, series_data, yaxis_name, chart_name, now_str, tagname="chart", daily_chart=True, extra_yaxis=None):
        """
        Render a chart
        """
        midnight_str = (self.midnight_utc + timedelta(days=1)).strftime(TIME_FORMAT)
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
        series_units = []
        for series in series_data:
            name = series.get("name")
            results = series.get("data")
            opacity_value = series.get("opacity", "1.0")
            stroke_width_value = series.get("stroke_width", "1")
            stroke_curve_value = series.get("stroke_curve", "smooth")
            chart_type = series.get("chart_type", "line")
            color = series.get("color", "")
            unit_name = series.get("unit", yaxis_name) or ""

            if results:
                if not first:
                    text += ","
                first = False
                text += self.get_chart_series(name, results, chart_type, color)
                opacity.append(opacity_value)
                stroke_width.append(stroke_width_value)
                stroke_curve.append("'{}'".format(stroke_curve_value))
                series_units.append(unit_name)

        units_array = ",".join("'{}'".format(unit) for unit in series_units)

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
        text += "    },\n"
        text += "    y: {\n"
        text += "      formatter: function(value, { seriesIndex }) {\n"
        text += "        if (value === null || typeof value === 'undefined') {\n"
        text += "          return value;\n"
        text += "        }\n"
        text += "        var units = [{}];\n".format(units_array)
        text += "        var unit = units[seriesIndex] !== undefined ? units[seriesIndex] : '{}';\n".format(yaxis_name)
        text += "        var rounded = (Math.round((value + Number.EPSILON) * 100) / 100).toFixed(2);\n"
        text += "        return unit ? rounded + ' ' + unit : rounded;\n"
        text += "      }\n"
        text += "    }\n"
        text += "  },"
        if extra_yaxis:
            axis_list = extra_yaxis if isinstance(extra_yaxis, list) else [extra_yaxis]
            primary_series_names = [series.get("name") for series in series_data if series.get("data") and series.get("unit", yaxis_name) == yaxis_name]

            primary_parts = [
                "      title: {{ text: '{}' }}".format(yaxis_name),
                "      decimalsInFloat: 2",
            ]
            if primary_series_names:
                names = ",".join("'{}'".format(name) for name in primary_series_names)
                primary_parts.append("      seriesName: [{}]".format(names))

            text += "  yaxis: [\n"
            text += "    {\n" + ",\n".join(primary_parts) + "\n    }"

            for axis in axis_list:
                title = axis.get("title", "")
                decimals = axis.get("decimals", 2)
                series_name = axis.get("series_name")
                series_names = axis.get("series_names")
                opposite = axis.get("opposite", False)
                labels_formatter = axis.get("labels_formatter")

                axis_parts = []
                if series_names:
                    names = ",".join("'{}'".format(name) for name in series_names)
                    axis_parts.append("      seriesName: [{}]".format(names))
                elif series_name:
                    axis_parts.append("      seriesName: '{}'".format(series_name))
                axis_parts.append("      title: {{ text: '{}' }}".format(title))
                axis_parts.append("      decimalsInFloat: {}".format(decimals))
                if opposite:
                    axis_parts.append("      opposite: true")
                if labels_formatter:
                    axis_parts.append("      labels: {{ formatter: function(val) {{ {} }} }}".format(labels_formatter))

                text += ",\n    {\n" + ",\n".join(axis_parts) + "\n    }"

            text += "\n  ],\n"
        else:
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
        text += "        var units = [{}];\n".format(units_array)
        text += "        var unit = units[opts.seriesIndex] !== undefined ? units[opts.seriesIndex] : '{}';\n".format(yaxis_name)
        text += "        return [opts.w.globals.series[opts.seriesIndex].at(-1), ' ', unit, ' <br> ', seriesName]\n"
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

    def render_timeline_chart(self, timeline_data, tagname, days):
        """
        Render a timeline chart for non-numerical data (like on/off states)
        Shows horizontal bars with different states as colored segments
        """
        # Build the initial template
        text = """
<script>
window.onresize = function() {{ location.reload(); }};
var width = window.innerWidth;
var height = window.innerHeight;

// Use full width minus small margins
width = Math.max(800, width - 100);

// Calculate height based on number of series (compact timeline view)
var seriesCount = {0};
var baseHeight = Math.max(200, seriesCount * 50 + 100);
height = Math.min(400, baseHeight);

var options = {{
  chart: {{
    type: 'rangeBar',
    width: width,
    height: height,
    animations: {{
      enabled: false
    }},
    toolbar: {{
      show: true
    }}
  }},
  plotOptions: {{
    bar: {{
      horizontal: true,
      barHeight: '70%',
      rangeBarGroupRows: false
    }}
  }},
  series: [
"""

        # Process each entity's timeline data
        first_series = True
        all_states = set()

        for entity_timeline in timeline_data:
            entity_name = entity_timeline["name"]
            history_chart = entity_timeline["data"]  # Dict with timestamp keys and state values

            # Convert history data to timeline ranges
            ranges = []
            current_state = None
            start_time = None

            # Sort by timestamp - history_chart is a dict
            sorted_items = sorted(history_chart.items(), key=lambda x: x[0])

            # Downsample if needed - keep max 288 data points
            max_points = 288
            if len(sorted_items) > max_points:
                # Calculate step size to keep approximately max_points
                step = len(sorted_items) // max_points
                if step < 1:
                    step = 1
                # Keep every Nth item, but always keep first and last
                downsampled = [sorted_items[0]]  # Always keep first
                # Add items at regular intervals
                for i in range(step, len(sorted_items) - 1, step):
                    downsampled.append(sorted_items[i])
                # Always keep last if it's not already included
                if len(sorted_items) > 1 and sorted_items[-1] not in downsampled:
                    downsampled.append(sorted_items[-1])
                sorted_items = downsampled

            last_timestamp_ms = None
            for timestamp_str, state in sorted_items:
                state = str(state)
                all_states.add(state)

                # Convert timestamp string to milliseconds for ApexCharts
                try:
                    timestamp_dt = str2time(timestamp_str)
                    timestamp_ms = int(timestamp_dt.timestamp() * 1000)
                    last_timestamp_ms = timestamp_ms  # Track the last valid timestamp
                except (ValueError, TypeError):
                    continue

                if current_state is None:
                    # First point
                    current_state = state
                    start_time = timestamp_ms
                elif current_state != state:
                    # State changed, save previous range and start new one
                    if start_time is not None:
                        ranges.append({"x": entity_name, "y": [start_time, timestamp_ms], "fillColor": self.get_state_color(current_state), "label": current_state})
                    current_state = state
                    start_time = timestamp_ms
                # else: state is the same, just extend the current range (don't create duplicate)

            # Add final range
            if start_time is not None and last_timestamp_ms is not None:
                ranges.append({"x": entity_name, "y": [start_time, last_timestamp_ms], "fillColor": self.get_state_color(current_state), "label": current_state})

            # Add series for this entity
            if not first_series:
                text += ","
            first_series = False

            text += "\n    {{\n"
            text += f"      name: '{entity_name}',\n"
            text += "      data: [\n"

            first_range = True
            for range_data in ranges:
                if not first_range:
                    text += ","
                first_range = False
                text += "        {{\n"
                text += f"          x: '{range_data['x']}',\n"
                text += f"          y: [{range_data['y'][0]}, {range_data['y'][1]}],\n"
                text += f"          fillColor: '{range_data['fillColor']}',\n"
                text += f"          label: '{range_data['label']}'\n"
                text += "        }}\n"

            text += "      ]\n"
            text += "    }}\n"

        text += """
  ],
  xaxis: {{
    type: 'datetime',
    labels: {{
      datetimeUTC: false
    }}
  }},
  yaxis: {{
    show: true,
    labels: {{
      style: {{
        fontSize: '14px'
      }}
    }}
  }},
  tooltip: {{
    custom: function({{ seriesIndex, dataPointIndex, w }}) {{
      var data = w.config.series[seriesIndex].data[dataPointIndex];
      var start = new Date(data.y[0]);
      var end = new Date(data.y[1]);
      var duration = (data.y[1] - data.y[0]) / (1000 * 60); // minutes

      return '<div style="padding: 10px;">' +
        '<strong>' + data.x + '</strong><br/>' +
        'State: <strong>' + data.label + '</strong><br/>' +
        'From: ' + start.toLocaleString() + '<br/>' +
        'To: ' + end.toLocaleString() + '<br/>' +
        'Duration: ' + duration.toFixed(0) + ' minutes' +
        '</div>';
    }}
  }},
  legend: {{
    show: false
  }},
  dataLabels: {{
    enabled: true,
    formatter: function(val, opts) {{
      var label = opts.w.config.series[opts.seriesIndex].data[opts.dataPointIndex].label;
      return label;
    }},
    style: {{
      colors: ['#fff'],
      fontSize: '14px',
      fontWeight: 'bold'
    }}
  }}
}};

var chart = new ApexCharts(document.querySelector('#{1}'), options);
chart.render();
</script>
"""

        # Now format the entire string at once
        return text.format(len(timeline_data), tagname)

    def get_state_color(self, state):
        """
        Get a color for a given state value
        """
        state_lower = str(state).lower()

        # Common state colors
        color_map = {
            "on": "#4CAF50",  # Green
            "off": "#9E9E9E",  # Gray
            "true": "#4CAF50",  # Green
            "false": "#9E9E9E",  # Gray
            "open": "#FF9800",  # Orange
            "closed": "#2196F3",  # Blue
            "active": "#4CAF50",  # Green
            "inactive": "#9E9E9E",  # Gray
            "home": "#4CAF50",  # Green
            "away": "#FF9800",  # Orange
            "charging": "#FFC107",  # Amber
            "discharging": "#03A9F4",  # Light Blue
            "idle": "#9E9E9E",  # Gray
        }

        if state_lower in color_map:
            return color_map[state_lower]

        # Generate a color based on hash of the state string
        hash_val = sum(ord(c) for c in str(state))
        hue = (hash_val * 137) % 360  # Use golden angle for better distribution
        return "hsl({}, 65%, 50%)".format(hue)

    async def html_api_get_log(self, request):
        """
        JSON API to get log data with filtering and search
        """

        def highlight_search_term(text, search_term):
            """
            Highlight search term in text with HTML markup, case-insensitive
            """
            if not search_term or not text:
                return text

            # Create case-insensitive pattern for the original text
            pattern = re.compile(re.escape(search_term), re.IGNORECASE)

            # Split text around matches, then escape and highlight each part
            parts = pattern.split(text)
            matches = pattern.findall(text)

            result_parts = []

            for i, part in enumerate(parts):
                # Escape the regular text part
                result_parts.append(html_module.escape(part))

                # Add highlighted match if there is one
                if i < len(matches):
                    escaped_match = html_module.escape(matches[i])
                    highlighted_match = f'<mark class="search-highlight" style="background-color: #ffff00; font-weight: bold;">{escaped_match}</mark>'
                    result_parts.append(highlighted_match)

            return "".join(result_parts)

        try:
            logfile = "predbat.log"
            logfile_1 = "predbat.1.log"
            logdata = ""

            if os.path.exists(logfile):
                with open(logfile, "r") as f:
                    logdata = f.read()
            if os.path.exists(logfile_1):
                with open(logfile_1, "r") as f:
                    logdata = f.read() + "\n" + logdata

            # Get query parameters
            args = request.query
            filter_type = args.get("filter", "warnings")  # all, warnings, errors
            since_line = int(args.get("since", 0))  # Line number to start from
            max_lines = int(args.get("max_lines", 1024))  # Maximum lines to return
            search_term = args.get("search", "").lower().strip()  # Search term

            loglines = logdata.split("\n")
            total_lines = len(loglines)

            # Process log lines with filtering and search
            result_lines = []
            count_lines = 0
            lineno = total_lines - 1
            matched_lines = 0  # Count of lines that match search criteria before limiting
            total_search_matches = 0  # Total matches in entire file (used when search is active)

            while lineno >= 0:
                line = loglines[lineno]
                line_lower = line.lower()

                # Skip empty lines
                if not line.strip():
                    lineno -= 1
                    continue

                # Apply log level filtering first
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

                # Apply search filter if search term is provided
                if include_line and search_term:
                    include_line = search_term in line_lower
                    if include_line:
                        total_search_matches += 1

                # Check if we've reached the max lines limit
                if count_lines >= max_lines:
                    include_line = False

                if include_line and (since_line == 0 or lineno > since_line):
                    matched_lines += 1
                    start_line = line[0:27] if len(line) >= 27 else line
                    rest_line = line[27:] if len(line) >= 27 else ""

                    # Apply highlighting if search term is present
                    if search_term:
                        highlighted_timestamp = highlight_search_term(start_line, search_term)
                        highlighted_message = highlight_search_term(rest_line, search_term)
                        highlighted_full_line = highlight_search_term(line, search_term)
                    else:
                        # Escape HTML characters even when no search highlighting
                        highlighted_timestamp = html_module.escape(start_line)
                        highlighted_message = html_module.escape(rest_line)
                        highlighted_full_line = html_module.escape(line)

                    result_lines.append(
                        {
                            "line_number": lineno,
                            "timestamp": highlighted_timestamp,
                            "message": highlighted_message,
                            "type": line_type,
                            "full_line": highlighted_full_line,
                            "raw_timestamp": start_line,  # Keep raw versions for any client-side processing
                            "raw_message": rest_line,
                            "raw_full_line": line,
                        }
                    )
                    count_lines += 1

                lineno -= 1

            # Reverse to get reverse chronological order (newest first)
            result_lines.reverse()

            response_data = {"status": "success", "total_lines": total_lines, "returned_lines": len(result_lines), "lines": result_lines, "filter": filter_type, "search_term": search_term}

            # If we have a search term, add information about total matches
            if search_term:
                response_data["search_matches"] = total_search_matches

            return web.json_response(response_data)

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
            self.set_state_wrapper(entity_id, state, attributes=attributes)
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
        state_data = self.get_state_wrapper()
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

        # Get the view parameter from the request
        args = request.query
        view = args.get("view", "plan")  # Default to 'plan' view

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

        # Add view switcher buttons
        text += '<div style="margin-bottom: 15px; display: flex; gap: 8px;">'

        # Determine active button styling
        plan_active = "background-color: #4CAF50; color: white;" if view == "plan" else "background-color: #f0f0f0; color: black;"
        yesterday_active = "background-color: #4CAF50; color: white;" if view == "yesterday" else "background-color: #f0f0f0; color: black;"
        baseline_active = "background-color: #4CAF50; color: white;" if view == "baseline" else "background-color: #f0f0f0; color: black;"

        text += f'<a href="./plan?view=plan" style="padding: 6px 12px; text-decoration: none; border-radius: 3px; font-size: 14px; border: 1px solid #ddd; {plan_active}">Plan</a>'
        text += f'<a href="./plan?view=yesterday" style="padding: 6px 12px; text-decoration: none; border-radius: 3px; font-size: 14px; border: 1px solid #ddd; {yesterday_active}">History</a>'
        text += f'<a href="./plan?view=baseline" style="padding: 6px 12px; text-decoration: none; border-radius: 3px; font-size: 14px; border: 1px solid #ddd; {baseline_active}">Yesterday Without Predbat</a>'
        text += "</div>"

        # Select the appropriate HTML plan based on the view
        if view == "yesterday":
            html_plan = self.get_state_wrapper(entity_id=self.prefix + ".cost_yesterday", attribute="html", default="<p>No yesterday plan available</p>")
            # Don't process buttons for yesterday view - just display the plan
            text += html_plan + "</body></html>\n"
            return web.Response(content_type="text/html", text=text)
        elif view == "baseline":
            html_plan = self.get_state_wrapper(entity_id=self.prefix + ".savings_yesterday_predbat", attribute="html", default="<p>No baseline plan available</p>")
            # Don't process buttons for baseline view - just display the plan
            text += html_plan + "</body></html>\n"
            return web.Response(content_type="text/html", text=text)
        else:
            # Default to plan view with editing capabilities
            html_plan = self.get_state_wrapper(entity_id=self.prefix + ".plan_html", attribute="html", default="<p>No plan available</p>")

        # Process HTML table to add buttons to time cells (only for plan view)
        # Regular expression to find time cells in the table
        time_pattern = r"<td id=time.*?>((?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) \d{2}:\d{2})</td>"
        import_pattern = r"<td id=import data-minute=(\S+) data-rate=(\S+)(.*?)>(.*?)</td>"
        export_pattern = r"<td id=export data-minute=(\S+) data-rate=(\S+)(.*?)>(.*?)</td>"
        load_pattern = r"<td id=load data-minute=(\S+) (.*?)>(.*?)</td>"
        soc_pattern = r"<td id=soc data-minute=(\S+)(.*?)>(.*?)</td>"

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
        manual_soc_keep = self.base.manual_rates("manual_soc")

        # Function to replace time cells with cells containing dropdowns
        def add_button_to_time(match):
            nonlocal dropdown_counter
            time_text = match.group(1)
            dropdown_id = f"dropdown_{dropdown_counter}"
            dropdown_counter += 1

            now_utc = self.now_utc
            time_stamp = get_override_time_from_string(now_utc, time_text, self.plan_interval_minutes)
            if time_stamp is None:
                return match.group(0)

            minutes_from_midnight = (time_stamp - self.midnight_utc).total_seconds() / 60
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
                cell_bg_color = "#C0C0C0"  # Matches freeze charging grey
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
            import_minute_to_time = self.midnight_utc + timedelta(minutes=int(import_minute))
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
                default_rate = self.get_arg("manual_import_value", 0.0) if is_import else self.get_arg("manual_export_value", 0.0)
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
            load_minute_to_time = self.midnight_utc + timedelta(minutes=int(load_minute))
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
                default_adjust = self.get_arg("manual_load_value", 0.0)
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

        def add_button_to_soc(match):
            """
            Add SOC button to limit cells
            """
            nonlocal dropdown_counter
            dropdown_id = f"dropdown_{dropdown_counter}"
            input_id = f"soc_input_{dropdown_counter}"
            dropdown_counter += 1

            soc_minute = match.group(1)
            soc_tag = match.group(2).strip()
            soc_text = match.group(3).strip()
            soc_minute_to_time = self.midnight_utc + timedelta(minutes=int(soc_minute))
            soc_minute_str = soc_minute_to_time.strftime("%a %H:%M")
            soc_target = manual_soc_keep.get(int(soc_minute), 0)

            button_html = f"""<td {soc_tag} class="clickable-time-cell {'override-active' if soc_target > 0 else ''}" onclick="toggleForceDropdown('{dropdown_id}')">
                {soc_text}
                <div class="dropdown">
                    <div id="{dropdown_id}" class="dropdown-content">
            """
            if soc_target > 0:
                action = "Clear SOC"
                button_html += f"""<a onclick="handleSocOverride('{soc_minute_str}', '{soc_target}', '{action}', true)">{action}</a>"""
            else:
                # Add input field for custom SOC entry
                default_soc = self.get_arg("manual_soc_value", 100)
                action = "Set SOC"
                button_html += f"""
                    <div style="padding: 12px 16px;">
                        <label style="display: block; margin-bottom: 5px; color: inherit;">{action} {soc_minute_str} Target (%):</label>
                        <input type="number" id="{input_id}" step="1" min="0" max="100" value="{default_soc}"
                               style="width: 80px; padding: 4px; margin-bottom: 8px; border-radius: 3px;">
                        <br>
                        <button onclick="handleSocOverride('{soc_minute_str}', document.getElementById('{input_id}').value, '{action}', false)"
                                style="padding: 6px 12px; border-radius: 3px; font-size: 12px;">
                            Set SOC Target
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
        processed_html = re.sub(soc_pattern, add_button_to_soc, processed_html)

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
        text += '<input type="text" id="logSearchInput" class="log-search-input" placeholder="Search entire log file..." oninput="filterLogEntries()" />'
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

            self.log("Web interface setting {} to {}".format(pitem, new_value))
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
            pat = re.match(r"^[a-zA-Z_]+\.\S+", value)
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
                    state = self.get_state_wrapper(entity_id=entity_id, attribute=attribute, default=None)
                else:
                    state = self.get_state_wrapper(entity_id=entity_id, default=None)
                    unit_of_measurement = self.get_state_wrapper(entity_id=entity_id, attribute="unit_of_measurement", default="")

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
        html_plan = self.get_state_wrapper(entity_id=self.prefix + ".plan_html", attribute="html", default="<p>No plan available</p>")
        if not html_plan:
            html_plan = None
        return await self.html_file("predbat_plan.html", html_plan)

    async def html_dash(self, request):
        """
        Render apps.yaml as an HTML page
        """

        self.default_page = "./dash"
        text = self.get_header("Predbat Dashboard", refresh=60)
        text += get_dashboard_css()
        text += get_dashboard_collapsible_js()
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
                    entity_id = f"select.{self.prefix}_{key}"
                    await self.base.ha_interface.set_state_external(entity_id, value)
                elif key in ["debug_enable", "set_read_only"]:
                    # Update switches - convert to boolean
                    entity_id = f"switch.{self.prefix}_{key}"
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
        now_str = self.now_utc.strftime(TIME_FORMAT)
        soc_kw_h0 = {}
        if self.base.soc_kwh_history:
            hist = self.base.soc_kwh_history
            for minute in range(0, self.minutes_now, self.plan_interval_minutes):
                minute_timestamp = self.midnight_utc + timedelta(minutes=minute)
                stamp = minute_timestamp.strftime(TIME_FORMAT)
                soc_kw_h0[stamp] = hist.get(self.minutes_now - minute, 0)
        soc_kw_h0[now_str] = self.base.soc_kw
        soc_kw = self.get_entity_results(self.prefix + ".soc_kw")
        soc_kw_best = self.get_entity_results(self.prefix + ".soc_kw_best")
        soc_kw_best10 = self.get_entity_results(self.prefix + ".soc_kw_best10")
        soc_kw_base10 = self.get_entity_results(self.prefix + ".soc_kw_base10")
        charge_limit_kw = self.get_entity_results(self.prefix + ".charge_limit_kw")
        best_charge_limit_kw = self.get_entity_results(self.prefix + ".best_charge_limit_kw")
        best_export_limit_kw = self.get_entity_results(self.prefix + ".best_export_limit_kw")
        battery_power_best = self.get_entity_results(self.prefix + ".battery_power_best")
        pv_power_best = self.get_entity_results(self.prefix + ".pv_power_best")
        grid_power_best = self.get_entity_results(self.prefix + ".grid_power_best")
        load_power_best = self.get_entity_results(self.prefix + ".load_power_best")
        iboost_best = self.get_entity_results(self.prefix + ".iboost_best")
        metric = self.get_entity_results(self.prefix + ".metric")
        best_metric = self.get_entity_results(self.prefix + ".best_metric")
        best10_metric = self.get_entity_results(self.prefix + ".best10_metric")
        cost_today = self.get_entity_results(self.prefix + ".cost_today")
        cost_today_export = self.get_entity_results(self.prefix + ".cost_today_export")
        cost_today_import = self.get_entity_results(self.prefix + ".cost_today_import")
        base10_metric = self.get_entity_results(self.prefix + ".base10_metric")
        rates = self.get_entity_results(self.prefix + ".rates")
        rates_export = self.get_entity_results(self.prefix + ".rates_export")
        rates_gas = self.get_entity_results(self.prefix + ".rates_gas")
        record = self.get_entity_results(self.prefix + ".record")

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
            text += self.render_chart(series_data, "kWh", "Battery SoC Prediction", now_str)
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
            text += self.render_chart(series_data, self.currency_symbols[1], "Home Cost Prediction", now_str)
        elif chart == "Rates":
            cost_today_hist = history_attribute(self.get_history_wrapper(self.prefix + ".ppkwh_today", 2, required=False))
            cost_hour_hist = history_attribute(self.get_history_wrapper(self.prefix + ".ppkwh_hour", 2, required=False))

            cost_pkwh_today = prune_today(cost_today_hist, self.now_utc, self.midnight_utc, prune=False, prune_future=False)
            cost_pkwh_hour = prune_today(cost_hour_hist, self.now_utc, self.midnight_utc, prune=False, prune_future=False)
            series_data = [
                {"name": "Import", "data": rates, "opacity": "1.0", "stroke_width": "3", "stroke_curve": "stepline"},
                {"name": "Export", "data": rates_export, "opacity": "0.2", "stroke_width": "2", "stroke_curve": "stepline", "chart_type": "area"},
                {"name": "Gas", "data": rates_gas, "opacity": "0.2", "stroke_width": "2", "stroke_curve": "stepline", "chart_type": "area"},
                {"name": "Hourly p/kWh", "data": cost_pkwh_hour, "opacity": "1.0", "stroke_width": "2", "stroke_curve": "stepline"},
                {"name": "Today p/kWh", "data": cost_pkwh_today, "opacity": "1.0", "stroke_width": "2", "stroke_curve": "stepline"},
            ]
            text += self.render_chart(series_data, self.currency_symbols[1], "Energy Rates", now_str)
        elif chart == "InDay":
            load_energy_actual = self.get_entity_results(self.prefix + ".load_energy_actual")
            load_energy_predicted = self.get_entity_results(self.prefix + ".load_energy_predicted")
            load_energy_adjusted = self.get_entity_results(self.prefix + ".load_energy_adjusted")
            inday_adjust_hist = history_attribute(self.get_history_wrapper(self.prefix + ".load_inday_adjustment", 2, required=False))
            adjustment_factor = prune_today(inday_adjust_hist, self.now_utc, self.midnight_utc, prune=True)

            series_data = [
                {"name": "Actual", "data": load_energy_actual, "opacity": "1.0", "stroke_width": "2", "stroke_curve": "smooth", "unit": "kWh"},
                {"name": "Predicted", "data": load_energy_predicted, "opacity": "1.0", "stroke_width": "2", "stroke_curve": "smooth", "unit": "kWh"},
                {"name": "Adjusted", "data": load_energy_adjusted, "opacity": "1.0", "stroke_width": "2", "stroke_curve": "smooth", "unit": "kWh"},
                {"name": "Adjustment Factor", "data": adjustment_factor, "opacity": "1.0", "stroke_width": "2", "stroke_curve": "smooth", "unit": "%"},
            ]
            secondary_axis = [
                {
                    "title": "%",
                    "series_name": "Adjustment Factor",
                    "decimals": 0,
                    "opposite": True,
                    "labels_formatter": "return val.toFixed(0) + '%';",
                }
            ]
            text += self.render_chart(series_data, "kWh", "In Day Adjustment", now_str, extra_yaxis=secondary_axis)
        elif chart == "PV" or chart == "PV7":
            pv_power_hist = history_attribute(self.get_history_wrapper(self.prefix + ".pv_power", 7, required=False))
            pv_power = prune_today(pv_power_hist, self.now_utc, self.midnight_utc, prune=chart == "PV")
            pv_forecast_hist = history_attribute(self.get_history_wrapper("sensor." + self.prefix + "_pv_forecast_h0", 7, required=False))
            pv_forecast_histCL = history_attribute(self.get_history_wrapper("sensor." + self.prefix + "_pv_forecast_h0", 7, required=False), attributes=True, state_key="nowCL")

            pv_forecast = prune_today(pv_forecast_hist, self.now_utc, self.midnight_utc, prune=chart == "PV", intermediate=True)
            pv_forecastCL = prune_today(pv_forecast_histCL, self.now_utc, self.midnight_utc, prune=chart == "PV", intermediate=True)
            pv_today_forecast = prune_today(self.get_entity_detailedForecast("sensor." + self.prefix + "_pv_today", "pv_estimate"), self.now_utc, self.midnight_utc, prune=False, intermediate=True)
            pv_today_forecast10 = prune_today(self.get_entity_detailedForecast("sensor." + self.prefix + "_pv_today", "pv_estimate10"), self.now_utc, self.midnight_utc, prune=False, intermediate=True)
            pv_today_forecast90 = prune_today(self.get_entity_detailedForecast("sensor." + self.prefix + "_pv_today", "pv_estimate90"), self.now_utc, self.midnight_utc, prune=False, intermediate=True)
            pv_today_forecastCL = prune_today(self.get_entity_detailedForecast("sensor." + self.prefix + "_pv_today", "pv_estimateCL"), self.now_utc, self.midnight_utc, prune=False, intermediate=True)
            pv_today_forecast.update(prune_today(self.get_entity_detailedForecast("sensor." + self.prefix + "_pv_tomorrow", "pv_estimate"), self.now_utc, self.midnight_utc, prune=False, intermediate=True))
            pv_today_forecast10.update(prune_today(self.get_entity_detailedForecast("sensor." + self.prefix + "_pv_tomorrow", "pv_estimate10"), self.now_utc, self.midnight_utc, prune=False, intermediate=True))
            pv_today_forecast90.update(prune_today(self.get_entity_detailedForecast("sensor." + self.prefix + "_pv_tomorrow", "pv_estimate90"), self.now_utc, self.midnight_utc, prune=False, intermediate=True))
            pv_today_forecastCL.update(prune_today(self.get_entity_detailedForecast("sensor." + self.prefix + "_pv_tomorrow", "pv_estimateCL"), self.now_utc, self.midnight_utc, prune=False, intermediate=True))

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
        all_states_data = self.get_state_wrapper()
        all_states = {}
        for entity in all_states_data:
            all_states[entity] = {"state": all_states_data[entity].get("state", ""), "unit_of_measurement": all_states_data[entity].get("attributes", {}).get("unit_of_measurement", "")}

        # Ensure all_states is valid and serializable
        try:
            all_states_json = json.dumps(all_states)
        except (TypeError, ValueError) as e:
            self.log(f"Error serializing all_states for web interface: {e}")
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

        args = self.args
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
                        self._update_nested_yaml_value(self.args, path_or_arg, converted_value)
                        updated_args.append(f"{path_or_arg}={converted_value}")
                    except (KeyError, TypeError) as e:
                        return web.json_response({"success": False, "message": f"Path {path_or_arg} not found or invalid: {str(e)}"})
                else:
                    # Handle top-level arguments
                    if path_or_arg in data[ROOT_YAML_KEY]:
                        data[ROOT_YAML_KEY][path_or_arg] = converted_value
                        self.args[path_or_arg] = converted_value  # Update the base args as well
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

                if itemtype in ["input_number", "number"] and item.get("step", 1) == 1:
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
                elif itemtype in ["input_number", "number"]:
                    input_number_with_save = input_number.replace('onchange="javascript: this.form.submit();"', 'onchange="saveFilterValue(); this.form.submit();"')
                    text += '<td><form style="display: inline;" method="post" action="./config">'
                    text += "{}\n".format(input_number_with_save.format(useid, useid, value, item.get("min", 0), item.get("max", 100), item.get("step", 1)))
                    text += f"</form></td>\n"
                elif itemtype == "select":
                    options = item.get("options", [])
                    if value not in options:
                        options.append(value)
                    text += f'<td><form style="display: inline;" method="post" action="./config">'
                    text += '<select name="{}" id="{}" onchange="saveFilterValue(); this.form.submit();">'.format(useid, useid)
                    for option in options:
                        selected = option == value
                        option_label = option if option else "None"
                        text += '<option value="{}" label="{}" {}>{}</option>'.format(option, option_label, "selected" if selected else "", option)
                    text += "</select>\n"
                    text += f"</form></td>\n"
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
                service_data["service_data"] = {"entity_id": "switch.{}_compare_active".format(self.prefix)}
                await self.base.trigger_callback(service_data)

        return await self.html_compare(request)

    def to_pounds(self, cost):
        """
        Convert cost to pounds
        """
        res = ""
        if cost:
            # Convert cost into pounds in format
            res = self.currency_symbols[0] + "{:.2f}".format(cost / 100.0)
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
        active = self.get_arg("compare_active", False)

        if not active:
            text += '<button type="submit" form="compareform" value="run">Compare now</button>\n'
        else:
            text += '<button type="submit" form="compareform" value="run" disabled>Running..</button>\n'

        text += '<input type="hidden" name="run" value="run">\n'
        text += "</form>"

        text += "<table class='comparison-table'>\n"
        text += "<tr><th>ID</th><th>Name</th><th>Date</th><th>True cost</th><th>Cost</th><th>Cost 10%</th><th>Export</th><th>Import</th><th>Final SoC</th>"
        if self.base.iboost_enable:
            text += "<th>Iboost</th>"
        if self.base.carbon_enable:
            text += "<th>Carbon</th>"
        text += "<th>Result</th>\n"

        """
        Update the history data
        """
        cost_yesterday_hist = history_attribute(self.get_history_wrapper(self.prefix + ".cost_yesterday", 28, required=False), daily=True, offset_days=-1, pounds=True)
        if self.base.num_cars > 0:
            cost_yesterday_car_hist = history_attribute(self.get_history_wrapper(self.prefix + ".cost_yesterday_car", 28, required=False), daily=True, offset_days=-1, pounds=True)
            cost_yesterday_no_car = self.subtract_daily(cost_yesterday_hist, cost_yesterday_car_hist)
        else:
            cost_yesterday_no_car = cost_yesterday_hist

        compare_list = self.get_arg("compare_list", [])
        compare_hist = {}
        for item in compare_list:
            id = item.get("id", None)
            if id and self.base.comparison:
                compare_hist[id] = {}
                result = self.base.comparison.get_comparison(id)
                if result:
                    compare_hist[id]["cost"] = history_attribute(self.get_history_wrapper(result["entity_id"], 28), daily=True, pounds=True)
                    compare_hist[id]["metric"] = history_attribute(self.get_history_wrapper(result["entity_id"], 28), state_key="metric", attributes=True, daily=True, pounds=True)

        compare_list = self.get_arg("compare_list", [])

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
            if stamp and (id in compare_hist):
                if "metric" not in compare_hist[id]:
                    compare_hist[id]["metric"] = {}
                    compare_hist[id]["cost"] = {}
                compare_hist[id]["metric"][stamp] = dp2(metric / 100)
                compare_hist[id]["cost"][stamp] = dp2(cost / 100.0)

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
            series_data.append({"name": name, "data": compare_hist.get(id, {}).get("metric", {}), "chart_type": "bar"})
        series_data.append({"name": "Actual", "data": cost_yesterday_hist, "chart_type": "line", "stroke_width": "2"})
        if self.base.num_cars > 0:
            series_data.append({"name": "Actual (no car)", "data": cost_yesterday_no_car, "chart_type": "line", "stroke_width": "2"})

        now_str = self.now_utc.strftime(TIME_FORMAT)

        if compare_hist:
            text += self.render_chart(series_data, self.currency_symbols[0], "Tariff Comparison - True cost", now_str, daily_chart=False)
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
            success_message = f"Apps.yaml saved successfully. Backup created at {backup_path}."
            encoded_message = urllib.parse.quote(success_message)
            raise web.HTTPFound(f"./apps_editor?success={encoded_message}")

        except web.HTTPFound:
            raise  # Re-raise HTTP redirects
        except Exception as e:
            error_msg = f"Failed to save apps.yaml: {str(e)}"
            self.log(f"ERROR: {error_msg}")
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

            now_utc = self.now_utc
            override_time = get_override_time_from_string(now_utc, time_str, self.plan_interval_minutes)

            minutes_from_now = (override_time - now_utc).total_seconds() / 60
            if minutes_from_now >= 48 * 60:
                return web.json_response({"success": False, "message": "Override time must be within 48 hours from now."}, status=400)

            selection_option = "{}={}".format(override_time.strftime("%a %H:%M"), rate)
            clear_option = "[{}={}]".format(override_time.strftime("%a %H:%M"), rate)
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
            elif action == "Set SOC":
                item = self.base.config_index.get("manual_soc_value", {})
                await self.base.ha_interface.set_state_external(item.get("entity", None), rate)
                await self.base.async_manual_select("manual_soc", selection_option)
            elif action == "Clear SOC":
                await self.base.async_manual_select("manual_soc", clear_option)
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

            now_utc = self.now_utc
            override_time = get_override_time_from_string(now_utc, time_str, self.plan_interval_minutes)
            if not override_time:
                return web.json_response({"success": False, "message": "Invalid time format"}, status=400)

            minutes_from_now = (override_time - now_utc).total_seconds() / 60
            if minutes_from_now >= 48 * 60:
                return web.json_response({"success": False, "message": "Override time must be within 48 hours from now."}, status=400)

            selection_option = "{}".format(override_time.strftime("%a %H:%M"))
            clear_option = "[{}]".format(override_time.strftime("%a %H:%M"))
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
            all_entities = self.get_state_wrapper()
            if all_entities:
                for entity_id in all_entities.keys():
                    if event_filter in entity_id:
                        count += 1
        except Exception as e:
            self.log(f"Error counting entities for filter '{event_filter}': {e}")
        return count

    def get_entities_matching_filter(self, event_filter):
        """
        Get a list of entities that match the given event filter pattern.
        Returns a list of dicts with entity_id, state, unit_of_measurement, and friendly_name.
        Sorted by entity_id.
        """
        entities = []
        try:
            # Get all entities from Home Assistant
            all_entities = self.get_state_wrapper()
            if all_entities:
                for entity_id in all_entities.keys():
                    if event_filter in entity_id:
                        entity_data = all_entities[entity_id]
                        state = entity_data.get("state", "")
                        attributes = entity_data.get("attributes", {})
                        unit_of_measurement = attributes.get("unit_of_measurement", "")
                        friendly_name = attributes.get("friendly_name", entity_id)

                        entities.append({"entity_id": entity_id, "state": str(state), "unit_of_measurement": str(unit_of_measurement) if unit_of_measurement else "", "friendly_name": friendly_name})

                # Sort by entity_id
                entities.sort(key=lambda x: x["entity_id"])
        except Exception as e:
            self.log(f"Error getting entities for filter '{event_filter}': {e}")

        return entities

    async def html_component_entities(self, request):
        """
        API endpoint to return entities matching a filter as JSON
        """
        try:
            args = request.query
            event_filter = args.get("filter", "")

            if not event_filter:
                return web.json_response({"entities": []}, status=200)

            entities = self.get_entities_matching_filter(event_filter)
            return web.json_response({"entities": entities}, status=200)

        except Exception as e:
            self.log(f"Error in html_component_entities: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def html_components(self, request):
        """
        Return the Components view as an HTML page showing status of all components
        """
        self.default_page = "./components"
        text = self.get_header("Predbat Components", refresh=60)
        text += "<body>\n"
        text += get_components_css()
        text += get_entity_modal_css()
        text += get_component_edit_modal_css()
        text += get_entity_modal_js()
        text += get_component_edit_modal_js()

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
            can_restart = self.base.components.can_restart(component_name)
            is_active = component_name in active_components

            # Get last updated time
            last_updated_time = self.base.components.last_updated_time(component_name)
            time_ago_text = format_time_ago(last_updated_time)

            # Create component card
            card_class = "active" if is_active else "inactive"
            if is_active and not is_alive:
                card_class += " error"
            text += f'<div class="component-card {card_class}">\n'
            text += f'<div class="component-header">\n'
            text += f'<h3>{component_info.get("name", component_name)}</h3>\n'

            # Status indicator
            if is_active and is_alive:
                text += '<span class="status-indicator status-healthy">●</span><span class="status-text">Active</span>\n'
            elif is_active and not is_alive:
                text += '<span class="status-indicator status-error">●</span><span class="status-text">Error</span>\n'
            else:
                text += '<span class="status-indicator status-inactive">●</span><span class="status-text">Disabled</span>\n'

            # Add restart button for active components
            if is_active and can_restart:
                text += f'<button class="restart-button" onclick="restartComponent(\'{component_name}\')" title="Restart this component">Restart</button>\n'

            # Add edit button for all components
            text += f'<button class="edit-button" onclick="showComponentEditModal(\'{component_name}\')" title="Edit component configuration">&#9998;</button>\n'

            text += f"</div>\n"

            # Component details
            text += f'<div class="component-details">\n'

            # Add last updated time
            text += f'<p><strong>Last Updated:</strong> <span class="last-updated-time">{time_ago_text}</span></p>\n'

            # Add error count
            error_count = self.base.components.get_error_count(component_name)
            if error_count is not None:
                error_class = "error-count-high" if error_count > 0 else "error-count-none"
                text += f'<p><strong>Error Count:</strong> <span class="{error_class}">{error_count}</span></p>\n'

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
                    current_value = self.get_arg(config_key, default, indirect=False)

                    if isinstance(current_value, (dict, list)):
                        current_value = "{...}"

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

                # Make entity count clickable only if count > 0
                if entity_count > 0:
                    onclick_attr = f"onclick=\"showEntityModal('{event_filter}')\""
                    style_attr = 'style="cursor: pointer; text-decoration: underline;"'
                    text += f'<p><strong>Entities:</strong> <span class="{count_class}" {style_attr} {onclick_attr}> num_entities: {entity_count}</span></p>\n'
                else:
                    text += f'<p><strong>Entities:</strong> <span class="{count_class}">num_entities: {entity_count}</span></p>\n'

            text += f"</div>\n"
            text += f"</div>\n"

        text += "</div>\n"

        # Add entity modal container
        text += """
<!-- Entity Modal -->
<div id="entityModal" class="entity-modal">
    <div class="entity-modal-content">
        <span class="entity-modal-close" onclick="closeEntityModal()">&times;</span>
        <h2 id="entityModalTitle">Entities</h2>
        <input type="text" id="entitySearchInput" class="entity-search-input" placeholder="Search entities..." oninput="filterEntityTable()">
        <div class="entity-list-table-container">
            <table class="entity-list-table" id="entityTable">
                <thead>
                    <tr>
                        <th>Entity ID</th>
                        <th>Friendly Name</th>
                        <th>State</th>
                    </tr>
                </thead>
                <tbody id="entityTableBody">
                </tbody>
            </table>
        </div>
        <div id="entityEmptyState" class="entity-empty-state" style="display: none;">No entities found</div>
    </div>
</div>

<!-- Component Edit Modal -->
<div id="componentEditModal" class="entity-modal">
    <div class="entity-modal-content component-edit-modal-content">
        <span class="entity-modal-close" onclick="closeComponentEditModal()">&times;</span>
        <h2 id="componentEditModalTitle">Edit Component Configuration</h2>
        <div id="componentEditForm" class="component-edit-form">
            <!-- Form will be populated dynamically -->
        </div>
        <div id="componentEditError" class="component-edit-error"></div>
        <div class="component-edit-buttons">
            <button id="componentEditSaveBtn" class="save-button" onclick="saveComponentConfig()">Save</button>
            <button class="cancel-button" onclick="closeComponentEditModal()">Cancel</button>
        </div>
    </div>
</div>
"""

        text += get_restart_button_js()

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

    async def html_component_restart(self, request):
        """
        Handle component restart request
        """
        try:
            # Parse form data
            data = await request.post()
            component_name = data.get("component")

            if not component_name:
                return web.json_response({"success": False, "message": "Missing component name"}, status=400)

            # Validate that the component exists and is active
            all_components = self.base.components.get_all()
            active_components = self.base.components.get_active()

            if component_name not in all_components:
                return web.json_response({"success": False, "message": f"Component '{component_name}' not found"}, status=400)

            if component_name not in active_components:
                return web.json_response({"success": False, "message": f"Component '{component_name}' is not active"}, status=400)

            self.log(f"Component restart requested from web interface: {component_name}")

            # Restart the specific component
            await self.base.components.restart(only=component_name)

            return web.json_response({"success": True, "message": f"Component '{component_name}' restarted successfully"})

        except Exception as e:
            self.log(f"ERROR: Failed to restart component: {str(e)}")
            return web.json_response({"success": False, "message": str(e)}, status=500)

    async def html_component_config(self, request):
        """
        Get component configuration for editing
        """
        try:
            from components import COMPONENT_LIST

            args = request.query
            component_name = args.get("component_name")

            if not component_name:
                return web.json_response({"success": False, "message": "Missing component_name parameter"}, status=400)

            if component_name not in COMPONENT_LIST:
                return web.json_response({"success": False, "message": f"Component '{component_name}' not found"}, status=404)

            component_info = COMPONENT_LIST[component_name]
            args_info = component_info.get("args", {})
            required_or = component_info.get("required_or", [])
            display_name = component_info.get("name", component_name)

            result_args = []

            for arg_name, arg_info in args_info.items():
                config_key = arg_info.get("config", "")
                if not config_key:
                    continue

                required = arg_info.get("required", False)
                default = arg_info.get("default", None)

                # Get current value from self.args
                current_value = self.args.get(config_key, None)

                # Determine type from APPS_SCHEMA
                field_type = "text"
                if config_key in APPS_SCHEMA:
                    schema_entry = APPS_SCHEMA[config_key]
                    if isinstance(schema_entry, dict):
                        schema_type_str = schema_entry.get("type", "text")
                        if "boolean" in schema_type_str:
                            field_type = "boolean"
                        elif "integer" in schema_type_str or "int" in schema_type_str:
                            field_type = "integer"
                        elif "float" in schema_type_str:
                            field_type = "float"
                        elif "string_list" in schema_type_str:
                            field_type = "string_list"
                        elif "dict_list" in schema_type_str or "dict" == schema_type_str:
                            field_type = "dict"
                        else:
                            field_type = "text"
                else:
                    # Infer from default value
                    if isinstance(default, bool):
                        field_type = "boolean"
                    elif isinstance(default, int):
                        field_type = "integer"
                    elif isinstance(default, float):
                        field_type = "float"
                    elif isinstance(default, list):
                        field_type = "string_list"
                    elif isinstance(default, dict):
                        field_type = "dict"

                # Check if in required_or list
                is_required_or = config_key in required_or or arg_name in required_or

                result_args.append({"config_key": config_key, "required": required, "required_or": is_required_or, "current_value": current_value, "default": default, "type": field_type})

            return web.json_response({"success": True, "component_name": component_name, "display_name": display_name, "args": result_args})

        except Exception as e:
            self.log(f"ERROR: Failed to get component config: {str(e)}")
            traceback.print_exc()
            return web.json_response({"success": False, "message": str(e)}, status=500)

    async def html_component_config_save(self, request):
        """
        Save component configuration using ruamel.yaml
        """
        try:
            json_data = await request.json()
            component_name = json_data.get("component_name")
            changes = json_data.get("changes", {})
            deletions = json_data.get("deletions", [])

            if not component_name:
                return web.json_response({"success": False, "message": "Missing component_name"}, status=400)

            self.log(f"Component config save requested: {component_name}, changes={list(changes.keys())}, deletions={deletions}")

            # Read and parse apps.yaml
            apps_yaml_path = "apps.yaml"
            yaml = YAML()
            yaml.preserve_quotes = True
            yaml.default_flow_style = False

            try:
                with open(apps_yaml_path, "r") as f:
                    data = yaml.load(f)
            except Exception as e:
                return web.json_response({"success": False, "message": f"Error reading apps.yaml: {str(e)}"}, status=500)

            if ROOT_YAML_KEY not in data:
                return web.json_response({"success": False, "message": f"{ROOT_YAML_KEY} section not found in apps.yaml"}, status=500)

            # Process changes
            for config_key, new_value in changes.items():
                # Look up type in APPS_SCHEMA
                field_type = "text"
                if config_key in APPS_SCHEMA:
                    schema_entry = APPS_SCHEMA[config_key]
                    if isinstance(schema_entry, dict):
                        schema_type_str = schema_entry.get("type", "text")
                        if "boolean" in schema_type_str:
                            field_type = "boolean"
                        elif "integer" in schema_type_str or "int" in schema_type_str:
                            field_type = "integer"
                        elif "float" in schema_type_str:
                            field_type = "float"
                        elif "string_list" in schema_type_str:
                            field_type = "string_list"
                        elif "dict_list" in schema_type_str or "dict" == schema_type_str:
                            field_type = "dict"

                # Convert value based on type
                try:
                    if field_type == "boolean":
                        if isinstance(new_value, bool):
                            converted_value = new_value
                        else:
                            converted_value = str(new_value).lower() == "true"
                    elif field_type == "integer":
                        converted_value = int(new_value)
                    elif field_type == "float":
                        converted_value = float(new_value)
                    elif field_type == "string_list":
                        # Already should be a list from JSON
                        converted_value = new_value if isinstance(new_value, list) else [new_value]
                    elif field_type == "dict":
                        # Parse JSON/YAML string from frontend
                        if isinstance(new_value, dict):
                            converted_value = new_value
                        elif isinstance(new_value, list):
                            converted_value = new_value
                        else:
                            # Try JSON first, then fall back to YAML
                            import json

                            try:
                                converted_value = json.loads(str(new_value))
                            except json.JSONDecodeError:
                                # Fall back to YAML parser
                                yaml_parser = YAML()
                                stream = StringIO(str(new_value))
                                converted_value = yaml_parser.load(stream)
                    else:
                        # String type - always quote strings for safety
                        converted_value = DoubleQuotedScalarString(str(new_value))

                    data[ROOT_YAML_KEY][config_key] = converted_value

                    # Mask password values in log
                    log_value = converted_value
                    if any(sensitive in config_key.lower() for sensitive in ["password", "key", "secret", "token"]):
                        log_value = "***"
                    self.log(f"Setting {config_key} = {log_value}")

                except ValueError as e:
                    return web.json_response({"success": False, "message": f"Invalid {field_type} value for {config_key}: {str(e)}"}, status=400)

            # Process deletions
            for config_key in deletions:
                if config_key in data[ROOT_YAML_KEY]:
                    del data[ROOT_YAML_KEY][config_key]
                    self.log(f"Deleted {config_key}")

            # Write back to file
            try:
                with open(apps_yaml_path, "w") as f:
                    yaml.dump(data, f)

                self.log(f"Component {component_name} config updated successfully")

                return web.json_response({"success": True, "message": "Configuration saved. Predbat will restart automatically."})

            except Exception as e:
                return web.json_response({"success": False, "message": f"Error writing to apps.yaml: {str(e)}"}, status=500)

        except Exception as e:
            self.log(f"ERROR: Failed to save component config: {str(e)}")
            traceback.print_exc()
            return web.json_response({"success": False, "message": str(e)}, status=500)

    async def html_browse(self, request):
        """
        Return the Browse page as an HTML page with file browsing functionality
        """
        self.default_page = "./browse"

        # Get query parameters for navigation and file viewing
        args = request.query
        current_path = args.get("path", ".")
        view_file = args.get("file", None)

        # Security check - prevent directory traversal attacks
        # Normalize the path and ensure it's within the current working directory
        base_dir = os.getcwd()
        safe_path = os.path.abspath(os.path.join(base_dir, current_path))

        # Ensure the path is within the base directory
        if not safe_path.startswith(base_dir):
            safe_path = base_dir
            current_path = "."

        text = self.get_header("Predbat File Browser", refresh=0)
        text += "<body>\n"
        text += get_browse_css()

        # Breadcrumb navigation
        text += '<div class="breadcrumb-container">\n'
        text += "<h2>File Browser</h2>\n"
        text += '<div class="breadcrumb">\n'

        # Build breadcrumb trail
        path_parts = []
        if current_path != ".":
            path_parts = current_path.split("/")
            path_parts = [part for part in path_parts if part]  # Remove empty parts

        # Root directory link
        text += '<a href="./browse?path=." class="breadcrumb-item">Root</a>'

        # Build each level of the path
        accumulated_path = "."
        for part in path_parts:
            accumulated_path = os.path.join(accumulated_path, part).replace("\\", "/")
            text += f' / <a href="./browse?path={accumulated_path}" class="breadcrumb-item">{part}</a>'

        text += "</div>\n"
        text += "</div>\n"

        if view_file:
            # File viewing mode
            file_path = os.path.join(safe_path, view_file) if current_path != "." else view_file
            try:
                # Security check for file path
                file_abs_path = os.path.abspath(file_path)
                if not file_abs_path.startswith(base_dir):
                    raise PermissionError("Access denied")

                if os.path.isfile(file_abs_path):
                    # Read file content
                    with open(file_abs_path, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()

                    # Escape HTML content
                    content = html_module.escape(content)

                    # Determine file type for syntax highlighting
                    file_ext = os.path.splitext(view_file)[1].lower()

                    text += f'<div class="file-viewer">\n'
                    text += f'<div class="file-header">\n'
                    text += f"<h3>Viewing: {view_file}</h3>\n"
                    text += f'<div class="file-actions">\n'
                    text += f'<a href="./download?path={current_path}&file={view_file}" class="download-button" download><span class="mdi mdi-download"></span> Download</a>\n'
                    text += f'<a href="./browse?path={current_path}" class="back-button">← Back to Directory</a>\n'
                    text += f"</div>\n"
                    text += f"</div>\n"

                    # File content with basic syntax highlighting
                    syntax_class = "code-content"
                    if file_ext in [".yaml", ".yml"]:
                        syntax_class += " yaml"
                    elif file_ext in [".py"]:
                        syntax_class += " python"
                    elif file_ext in [".json"]:
                        syntax_class += " json"
                    elif file_ext in [".log"]:
                        syntax_class += " log"

                    text += f'<pre class="{syntax_class}">{content}</pre>\n'
                    text += f"</div>\n"
                else:
                    text += f'<div class="error">File not found: {view_file}</div>\n'
            except PermissionError:
                text += f'<div class="error">Permission denied: {view_file}</div>\n'
            except UnicodeDecodeError:
                text += f'<div class="error">Cannot display binary file: {view_file}</div>\n'
            except Exception as e:
                text += f'<div class="error">Error reading file: {str(e)}</div>\n'
        else:
            # Directory listing mode
            try:
                items = []

                # Add parent directory link if not at root
                if current_path != ".":
                    parent_path = os.path.dirname(current_path) if current_path != "." else "."
                    if parent_path == "":
                        parent_path = "."
                    items.append({"name": "..", "type": "parent", "path": parent_path, "size": "", "modified": ""})

                # List directory contents
                for item in os.listdir(safe_path):
                    item_path = os.path.join(safe_path, item)

                    # Skip hidden files and directories
                    if item.startswith(".") and item not in [".."]:
                        continue

                    try:
                        stat_info = os.stat(item_path)
                        size = ""
                        if os.path.isfile(item_path):
                            # Format file size
                            size_bytes = stat_info.st_size
                            if size_bytes < 1024:
                                size = f"{size_bytes} B"
                            elif size_bytes < 1024 * 1024:
                                size = f"{size_bytes / 1024:.1f} KB"
                            else:
                                size = f"{size_bytes / (1024 * 1024):.1f} MB"

                        # Format modification time
                        modified = datetime.fromtimestamp(stat_info.st_mtime).strftime("%Y-%m-%d %H:%M")

                        items.append({"name": item, "type": "directory" if os.path.isdir(item_path) else "file", "path": os.path.join(current_path, item).replace("\\", "/"), "size": size, "modified": modified})
                    except (OSError, PermissionError):
                        # Skip items we can't access
                        continue

                # Sort items: directories first, then files, both alphabetically
                items.sort(key=lambda x: (x["type"] != "parent", x["type"] != "directory", x["name"].lower()))

                # Display file list
                text += '<div class="file-list">\n'
                text += '<table class="files-table">\n'
                text += "<thead>\n"
                text += "<tr><th>Name</th><th>Size</th><th>Modified</th></tr>\n"
                text += "</thead>\n"
                text += "<tbody>\n"

                for item in items:
                    icon = ""
                    link_target = ""
                    css_class = ""

                    if item["type"] == "parent":
                        icon = '<span class="mdi mdi-arrow-up-bold"></span>'
                        link_target = f"./browse?path={item['path']}"
                        css_class = "parent-dir"
                    elif item["type"] == "directory":
                        icon = '<span class="mdi mdi-folder"></span>'
                        link_target = f"./browse?path={item['path']}"
                        css_class = "directory"
                    else:
                        # File icons based on extension
                        file_ext = os.path.splitext(item["name"])[1].lower()
                        if file_ext in [".yaml", ".yml"]:
                            icon = '<span class="mdi mdi-code-braces"></span>'
                        elif file_ext in [".py"]:
                            icon = '<span class="mdi mdi-language-python"></span>'
                        elif file_ext in [".json"]:
                            icon = '<span class="mdi mdi-code-json"></span>'
                        elif file_ext in [".log"]:
                            icon = '<span class="mdi mdi-text-box-outline"></span>'
                        elif file_ext in [".txt", ".md"]:
                            icon = '<span class="mdi mdi-file-document-outline"></span>'
                        else:
                            icon = '<span class="mdi mdi-file-outline"></span>'

                        link_target = f"./browse?path={current_path}&file={item['name']}"
                        css_class = "file"

                    text += f'<tr class="{css_class}">\n'
                    text += f'<td><a href="{link_target}">{icon} {item["name"]}</a></td>\n'
                    text += f'<td>{item["size"]}</td>\n'
                    text += f'<td>{item["modified"]}</td>\n'
                    text += f"</tr>\n"

                text += "</tbody>\n"
                text += "</table>\n"
                text += "</div>\n"

            except PermissionError:
                text += '<div class="error">Permission denied accessing directory</div>\n'
            except Exception as e:
                text += f'<div class="error">Error listing directory: {str(e)}</div>\n'

        text += "</body></html>\n"
        return web.Response(content_type="text/html", text=text)

    async def html_download_file(self, request):
        """
        Download a file from the filesystem
        """
        args = request.query
        current_path = args.get("path", ".")
        download_file = args.get("file", None)

        if not download_file:
            return web.Response(text="File not specified", status=400)

        # Security check - prevent directory traversal attacks
        base_dir = os.getcwd()
        safe_path = os.path.abspath(os.path.join(base_dir, current_path))

        # Ensure the path is within the base directory
        if not safe_path.startswith(base_dir):
            return web.Response(text="Access denied", status=403)

        file_path = os.path.join(safe_path, download_file)

        try:
            # Security check for file path
            file_abs_path = os.path.abspath(file_path)
            if not file_abs_path.startswith(base_dir):
                return web.Response(text="Access denied", status=403)

            if os.path.isfile(file_abs_path):
                # Read file content
                with open(file_abs_path, "rb") as f:
                    content = f.read()

                # Set appropriate content type based on file extension
                file_ext = os.path.splitext(download_file)[1].lower()
                content_type = "application/octet-stream"

                if file_ext in [".yaml", ".yml"]:
                    content_type = "text/yaml"
                elif file_ext in [".py"]:
                    content_type = "text/x-python"
                elif file_ext in [".json"]:
                    content_type = "application/json"
                elif file_ext in [".log", ".txt"]:
                    content_type = "text/plain"
                elif file_ext in [".html"]:
                    content_type = "text/html"
                elif file_ext in [".md"]:
                    content_type = "text/markdown"

                # Create response with download headers
                response = web.Response(body=content, content_type=content_type)
                response.headers["Content-Disposition"] = f'attachment; filename="{download_file}"'
                return response
            else:
                return web.Response(text="File not found", status=404)
        except PermissionError:
            return web.Response(text="Permission denied", status=403)
        except Exception as e:
            self.log(f"Error downloading file: {str(e)}")
            return web.Response(text=f"Error downloading file: {str(e)}", status=500)

    async def html_internals(self, request):
        """
        Return the Internals page showing the class hierarchy and object inspection
        """
        self.default_page = "./internals"

        text = self.get_header("Predbat Internals", refresh=0)
        text += "<body>\n"
        text += get_internals_css()
        text += get_internals_js()

        text += '<div class="internals-container">\n'
        text += '<div class="breadcrumb-container">\n'
        text += "<h2>Predbat Internals Browser</h2>\n"
        text += '<div class="breadcrumb">Browse the internal object hierarchy starting from predbat</div>\n'
        text += "</div>\n"

        # Add thread stack frames section
        text += '<div class="threads-section">\n'
        text += "<h3>Thread Stack Frames</h3>\n"
        text += '<div class="threads-container">\n'
        text += self._get_thread_stacks_html()
        text += "</div>\n"
        text += "</div>\n"

        # Object tree in a panel
        text += '<div class="tree-section">\n'
        text += "<h3>Object Hierarchy</h3>\n"
        text += '<div class="tree-container">\n'
        text += '<div class="tree-view">\n'

        # Create a single root node for 'predbat' pointing to self.base
        text += '<div class="tree-item" title="predbat" onclick="toggleNode(this, \'predbat\')">\n'
        text += '<span class="expand-icon expandable">+</span>'
        text += '<button class="refresh-icon" title="Refresh this node" onclick="event.stopPropagation(); refreshNode(this, \'predbat\')">🔄</button>'
        text += '<a href="./api/internals/download?path=predbat" class="download-icon" title="Download as YAML" onclick="event.stopPropagation()">⬇</a>'
        text += '<span class="key">predbat</span>'
        text += f'<span class="type">&lt;{type(self.base).__name__}&gt;</span>'
        text += "</div>\n"
        text += '<div class="tree-children"></div>\n'

        text += "</div>\n"  # tree-view
        text += "</div>\n"  # tree-container
        text += "</div>\n"  # tree-section
        text += "</div>\n"  # internals-container

        text += "</body></html>\n"
        return web.Response(content_type="text/html", text=text)

    def _get_thread_stacks_html(self):
        """
        Get HTML representation of all thread stack frames
        """
        text = ""

        try:
            # Get all thread frames
            frames = sys._current_frames()
            threads = {thread.ident: thread for thread in threading.enumerate()}

            # Sort threads by name for consistent display
            thread_list = []
            for thread_id, frame in frames.items():
                thread = threads.get(thread_id)
                thread_name = thread.name if thread else f"Thread-{thread_id}"
                thread_list.append((thread_name, thread_id, frame, thread))

            thread_list.sort(key=lambda x: x[0])

            if not thread_list:
                text += '<div class="thread-item">No threads found</div>'
                return text

            for thread_name, thread_id, frame, thread in thread_list:
                # Thread header
                text += f'<div class="thread-item">'
                text += f'<div class="thread-header" onclick="toggleThreadStack(this)">'
                text += f'<span class="expand-icon expandable">+</span>'
                text += f'<span class="thread-name">{thread_name}</span>'
                text += f'<span class="thread-id">ID: {thread_id}</span>'
                if thread:
                    alive_status = "alive" if thread.is_alive() else "dead"
                    daemon_status = "daemon" if thread.daemon else "normal"
                    text += f'<span class="thread-status">{alive_status}, {daemon_status}</span>'
                text += "</div>"

                # Stack trace (collapsed by default)
                text += f'<div class="thread-stack collapsed">'

                # Extract all stack frames
                stack = traceback.extract_stack(frame)

                text += '<div class="stack-frames">'
                for i, frame_info in enumerate(stack):
                    frame_num = i
                    text += f'<div class="stack-frame">'
                    text += f'<span class="frame-number">#{frame_num}</span>'
                    text += f'<span class="frame-file">{frame_info.filename}:{frame_info.lineno}</span>'
                    text += f'<span class="frame-function">in {frame_info.name}()</span>'
                    if frame_info.line:
                        text += f'<div class="frame-code">{html_module.escape(frame_info.line.strip())}</div>'
                    text += "</div>"
                text += "</div>"

                # Check if this thread has an asyncio event loop with tasks
                asyncio_tasks = self._get_thread_asyncio_tasks(frame)
                if asyncio_tasks:
                    text += '<div class="asyncio-tasks">'
                    text += "<h4>Asyncio Tasks in this thread:</h4>"
                    for task_info in asyncio_tasks:
                        text += '<div class="asyncio-task">'
                        text += f'<div class="task-header">'
                        text += f'<span class="task-name">{task_info["name"]}</span>'
                        text += f'<span class="task-state">{task_info["state"]}</span>'
                        text += "</div>"
                        if task_info.get("stack"):
                            text += '<div class="task-stack">'
                            for frame_info in task_info["stack"]:
                                text += f'<div class="stack-frame">'
                                text += f'<span class="frame-file">{frame_info["file"]}:{frame_info["line"]}</span>'
                                text += f'<span class="frame-function">in {frame_info["name"]}()</span>'
                                if frame_info.get("code"):
                                    text += f'<div class="frame-code">{html_module.escape(frame_info["code"])}</div>'
                                text += "</div>"
                            text += "</div>"
                        text += "</div>"
                    text += "</div>"

                text += "</div>"  # thread-stack
                text += "</div>"  # thread-item

        except Exception as e:
            self.log(f"Error getting thread stacks: {e}")
            text += f'<div class="error">Error retrieving thread information: {html_module.escape(str(e))}</div>'

        return text

    def _get_thread_asyncio_tasks(self, frame):
        """
        Extract asyncio tasks from a thread's frame if it's running an event loop
        """
        import asyncio

        tasks_info = []

        try:
            # Try to find the event loop in this thread's frame locals
            current_frame = frame
            event_loop = None

            # Walk up the stack to find the event loop
            while current_frame is not None:
                if "self" in current_frame.f_locals:
                    obj = current_frame.f_locals["self"]
                    if isinstance(obj, asyncio.AbstractEventLoop):
                        event_loop = obj
                        break
                current_frame = current_frame.f_back

            if not event_loop:
                return tasks_info

            # Get all tasks for this event loop
            all_tasks = asyncio.all_tasks(event_loop)

            for task in all_tasks:
                task_name = task.get_name()

                # Get task state
                if task.done():
                    if task.cancelled():
                        state = "cancelled"
                    else:
                        state = "done"
                else:
                    state = "running"

                task_info = {"name": task_name, "state": state, "stack": []}

                # Get the coroutine stack
                try:
                    coro = task.get_coro()
                    if coro:
                        # Get the stack frames for this coroutine
                        stack = []
                        cr_frame = getattr(coro, "cr_frame", None)
                        if cr_frame:
                            # Extract stack from coroutine frame
                            frames_list = []
                            current = cr_frame
                            while current:
                                frames_list.append(current)
                                current = current.f_back

                            # Reverse to show from oldest to newest
                            frames_list.reverse()

                            for fr in frames_list:
                                code = fr.f_code
                                line_no = fr.f_lineno

                                # Try to get the actual line of code
                                try:
                                    import linecache

                                    line_code = linecache.getline(code.co_filename, line_no).strip()
                                except:
                                    line_code = ""

                                stack.append({"file": code.co_filename, "line": line_no, "name": code.co_name, "code": line_code})

                            task_info["stack"] = stack
                except Exception as e:
                    # If we can't get the coroutine stack, just skip it
                    pass

                tasks_info.append(task_info)

        except Exception as e:
            self.log(f"Error extracting asyncio tasks: {e}")

        return tasks_info

    async def html_api_internals(self, request):
        """
        API endpoint to get object members for a given path
        """
        args = request.query
        path = args.get("path", "")

        try:
            # Navigate to the requested object
            obj = self.base
            if path and path != "predbat":
                parts = path.split("::")
                # Skip 'predbat' if it's the first part
                if parts[0] == "predbat":
                    parts = parts[1:]
                for part in parts:
                    # Check dict first before hasattr to avoid accessing dict methods
                    if isinstance(obj, dict):
                        if part in obj:
                            obj = obj[part]
                        else:
                            return web.Response(content_type="application/json", text=json.dumps({"success": False, "error": f"Key not found: {part}"}))
                    elif isinstance(obj, (list, tuple)):
                        try:
                            # Strip brackets if present (e.g., "[0]" -> "0")
                            index_str = part.strip("[]")
                            index = int(index_str)
                            obj = obj[index]
                        except (ValueError, IndexError):
                            return web.Response(content_type="application/json", text=json.dumps({"success": False, "error": f"Invalid index: {part}"}))
                    elif hasattr(obj, part):
                        obj = getattr(obj, part)
                    else:
                        return web.Response(content_type="application/json", text=json.dumps({"success": False, "error": f"Path not found: {path}"}))

            # Get members of the object
            members = self._get_object_members(obj, path)

            return web.Response(content_type="application/json", text=json.dumps({"success": True, "members": members}))
        except Exception as e:
            self.log(f"Error in internals API: {str(e)}")
            return web.Response(content_type="application/json", text=json.dumps({"success": False, "error": str(e)}))

    async def html_api_internals_download(self, request):
        """
        API endpoint to download object as YAML
        """
        args = request.query
        path = args.get("path", "")

        try:
            # Navigate to the requested object
            obj = self.base
            if path and path != "predbat":
                parts = path.split("::")
                # Skip 'predbat' if it's the first part
                if parts[0] == "predbat":
                    parts = parts[1:]
                for part in parts:
                    # Check dict first before hasattr to avoid accessing dict methods
                    if isinstance(obj, dict):
                        if part in obj:
                            obj = obj[part]
                        else:
                            return web.Response(content_type="text/plain", text=f"Error: Key not found: {part}")
                    elif isinstance(obj, (list, tuple)):
                        try:
                            # Strip brackets if present (e.g., "[0]" -> "0")
                            index_str = part.strip("[]")
                            index = int(index_str)
                            obj = obj[index]
                        except (ValueError, IndexError):
                            return web.Response(content_type="text/plain", text=f"Error: Invalid index: {part}")
                    elif hasattr(obj, part):
                        obj = getattr(obj, part)
                    else:
                        return web.Response(content_type="text/plain", text=f"Error: Attribute not found: {part}")

            # Convert object to YAML-serializable format
            try:
                yaml_data = self._object_to_yaml_dict(obj, visited=set())
            except Exception as e:
                self.log(f"Error converting object to YAML dict: {e}")
                return web.Response(content_type="text/plain", text=f"Error: Failed to convert object to YAML-serializable format: {str(e)}")

            # Convert to YAML
            try:
                yaml = YAML()
                yaml.default_flow_style = False
                yaml.preserve_quotes = True

                stream = io.StringIO()
                yaml.dump(yaml_data, stream)
                yaml_content = stream.getvalue()
            except Exception as e:
                self.log(f"Error dumping YAML: {e}")
                return web.Response(content_type="text/plain", text=f"Error: Failed to serialize to YAML format: {str(e)}\n\nThis object may contain types that are not YAML-serializable.")

            # Generate filename from path
            if path:
                filename = path.replace("::", "_") + ".yaml"
            else:
                filename = "predbat_root.yaml"

            # Return as downloadable file
            return web.Response(content_type="application/x-yaml", headers={"Content-Disposition": f'attachment; filename="{filename}"'}, text=yaml_content)

        except Exception as e:
            self.log(f"Error downloading internals as YAML: {e}")
            return web.Response(content_type="text/plain", text=f"Error: {str(e)}")

    def _object_to_yaml_dict(self, obj, max_depth=10, current_depth=0, visited=None):
        """
        Convert an object to a YAML-serializable dictionary
        Handles nested objects, lists, dicts, etc.
        Detects circular references to prevent infinite loops
        """
        if visited is None:
            visited = set()

        if current_depth >= max_depth:
            return f"<max depth {max_depth} reached>"

        # Handle None
        if obj is None:
            return None

        # Handle primitives (no circular reference check needed)
        if isinstance(obj, (str, int, float, bool)):
            return obj

        # Check for circular references for complex objects
        obj_id = id(obj)
        if obj_id in visited:
            return f"<circular reference to {type(obj).__name__}>"

        # Add to visited set
        visited.add(obj_id)

        # Handle lists and tuples
        if isinstance(obj, (list, tuple)):
            result = []
            for i, item in enumerate(obj[:100]):  # Limit to 100 items
                try:
                    result.append(self._object_to_yaml_dict(item, max_depth, current_depth + 1, visited))
                except Exception as e:
                    result.append(f"<error at index {i}: {type(e).__name__}>")
            # Remove from visited after processing to allow same object in different branches
            visited.discard(obj_id)
            return result

        # Handle dictionaries
        if isinstance(obj, dict):
            result = {}
            for key, value in list(obj.items())[:100]:  # Limit to 100 items
                try:
                    # Ensure key is YAML-serializable
                    yaml_key = str(key) if not isinstance(key, (str, int, float, bool)) else key
                    result[yaml_key] = self._object_to_yaml_dict(value, max_depth, current_depth + 1, visited)
                except Exception as e:
                    try:
                        yaml_key = str(key)
                    except:
                        yaml_key = f"<unprintable_key_{hash(key)}>"
                    result[yaml_key] = f"<error: {type(e).__name__}>"
            # Remove from visited after processing to allow same object in different branches
            visited.discard(obj_id)
            return result

        # Handle objects with __dict__
        if hasattr(obj, "__dict__"):
            result = {}
            try:
                obj_dict = obj.__dict__
                for key, value in list(obj_dict.items())[:100]:  # Limit to 100 items
                    if not key.startswith("_"):
                        try:
                            result[key] = self._object_to_yaml_dict(value, max_depth, current_depth + 1, visited)
                        except Exception as e:
                            result[key] = f"<error: {type(e).__name__}>"
            except Exception as e:
                visited.discard(obj_id)
                return f"<{type(obj).__name__} object - error accessing __dict__: {type(e).__name__}>"
            # Remove from visited after processing to allow same object in different branches
            visited.discard(obj_id)
            return result if result else f"<{type(obj).__name__} object>"

        # Fallback: try to convert to string
        try:
            str_value = str(obj)
            if len(str_value) > 200:
                return str_value[:200] + "..."
            return str_value
        except:
            return f"<{type(obj).__name__}>"

    def _get_object_members(self, obj, path):
        """
        Get members of an object for display in the internals browser
        Returns a list of dictionaries with key, type, value, and expandable flag
        """
        members = []

        try:
            # Handle dictionaries
            if isinstance(obj, dict):
                for key in sorted(obj.keys())[:100]:  # Limit to first 100 items
                    try:
                        value = obj[key]
                        member_info = self._analyze_value(str(key), value, path)
                        members.append(member_info)
                    except Exception as e:
                        members.append({"key": str(key), "type": "error", "value": f"Error: {str(e)}", "expandable": False})

                if len(obj) > 100:
                    members.append({"key": "...", "type": "info", "value": f"({len(obj) - 100} more items)", "expandable": False})

            # Handle lists/tuples
            elif isinstance(obj, (list, tuple)):
                for i, value in enumerate(obj[:100]):  # Limit to first 100 items
                    try:
                        member_info = self._analyze_value(f"[{i}]", value, path)
                        members.append(member_info)
                    except Exception as e:
                        members.append({"key": f"[{i}]", "type": "error", "value": f"Error: {str(e)}", "expandable": False})

                if len(obj) > 100:
                    members.append({"key": "...", "type": "info", "value": f"({len(obj) - 100} more items)", "expandable": False})

            # Handle objects with attributes
            else:
                # Get all attributes
                attrs = []
                for attr in dir(obj):
                    # Skip private attributes and methods
                    if attr.startswith("_"):
                        continue
                    attrs.append(attr)

                # Sort and limit
                for attr in sorted(attrs)[:200]:  # Limit to 200 attributes
                    try:
                        value = getattr(obj, attr)
                        # Skip methods for now (could make them expandable later)
                        if callable(value):
                            continue
                        member_info = self._analyze_value(attr, value, path)
                        members.append(member_info)
                    except Exception as e:
                        members.append({"key": attr, "type": "error", "value": f"Error: {str(e)}", "expandable": False})

        except Exception as e:
            self.log(f"Error getting object members: {str(e)}")
            members.append({"key": "error", "type": "error", "value": str(e), "expandable": False})

        return members

    def _analyze_value(self, key, value, path):
        """
        Analyze a value and return information about it
        """
        value_type = type(value).__name__
        expandable = False
        display_value = None
        type_size = None

        # Check if expandable
        if isinstance(value, dict):
            expandable = len(value) > 0
            display_value = f"{{{len(value)} items}}"
            type_size = len(value)
        elif isinstance(value, (list, tuple)):
            expandable = len(value) > 0
            display_value = f"[{len(value)} items]"
            type_size = len(value)
        elif hasattr(value, "__dict__") and not isinstance(value, (str, int, float, bool, type(None))):
            # Object with attributes
            expandable = True
            display_value = f"<{value_type} object>"
        elif isinstance(value, (str, int, float, bool, type(None))):
            # Scalar types
            expandable = False
            if isinstance(value, str):
                # Truncate long strings
                if len(value) > 100:
                    display_value = value[:100] + "..."
                else:
                    display_value = value
            else:
                display_value = value
        else:
            # Other types
            try:
                str_value = str(value)
                if len(str_value) > 100:
                    display_value = str_value[:100] + "..."
                else:
                    display_value = str_value
            except:
                display_value = f"<{value_type}>"

        # Build the full path for this item using :: as separator
        full_path = f"{path}::{key}" if path else key

        result = {"key": key, "type": value_type, "value": display_value, "expandable": expandable, "path": full_path}

        # Add size for collections
        if type_size is not None:
            result["size"] = type_size

        return result
