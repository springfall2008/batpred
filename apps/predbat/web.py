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

from utils import calc_percent_limit, str2time, dp0, dp2
from config import TIME_FORMAT, TIME_FORMAT_SECONDS
from predbat import THIS_VERSION

TIME_FORMAT_DAILY = "%Y-%m-%d"


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

    def history_attribute(self, history, state_key="state", last_updated_key="last_updated", scale=1.0, attributes=False, print=False, daily=False, offset_days=0, first=True, pounds=False):
        results = {}
        last_updated_time = None
        last_day_stamp = None
        if history and len(history) >= 1:
            history = history[0]

        if not history:
            return results

        # Process history
        for item in history:
            if last_updated_key not in item:
                continue

            if print:
                self.log("Item {}".format(item))

            if attributes:
                if state_key not in item["attributes"]:
                    continue
                state = item["attributes"][state_key]
            else:
                # Ignore data without correct keys
                if state_key not in item:
                    continue

                # Unavailable or bad values
                if item[state_key] == "unavailable" or item[state_key] == "unknown":
                    continue

                state = item[state_key]

            # Get the numerical key and the timestamp and ignore if in error
            try:
                state = float(state) * scale
                if pounds:
                    state = dp2(state / 100)
            except (ValueError, TypeError):
                if isinstance(state, str):
                    if state.lower() in ["on", "true", "yes"]:
                        state = 1
                    elif state.lower() in ["off", "false", "no"]:
                        state = 0
                    else:
                        continue
                else:
                    continue

            try:
                last_updated_time = item[last_updated_key]
                last_updated_stamp = str2time(last_updated_time)
            except (ValueError, TypeError):
                continue

            day_stamp = last_updated_stamp.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
            if offset_days:
                day_stamp += timedelta(days=offset_days)

            if first and daily and day_stamp == last_day_stamp:
                continue
            last_day_stamp = day_stamp

            # Add the state to the result
            if daily:
                # Convert day stamp from UTC into localtime
                results[day_stamp.strftime(TIME_FORMAT_DAILY)] = state
            else:
                results[last_updated_time] = state

        return results

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
        self.pv_power_hist = self.history_attribute(self.base.get_history_wrapper(self.base.prefix + ".pv_power", 7, required=False))
        self.pv_forecast_hist = self.history_attribute(self.base.get_history_wrapper("sensor." + self.base.prefix + "_pv_forecast_h0", 7, required=False))
        self.cost_today_hist = self.history_attribute(self.base.get_history_wrapper(self.base.prefix + ".ppkwh_today", 2, required=False))
        self.cost_hour_hist = self.history_attribute(self.base.get_history_wrapper(self.base.prefix + ".ppkwh_hour", 2, required=False))
        self.cost_yesterday_hist = self.history_attribute(self.base.get_history_wrapper(self.base.prefix + ".cost_yesterday", 28, required=False), daily=True, offset_days=-1, pounds=True)

        if self.base.num_cars > 0:
            self.cost_yesterday_car_hist = self.history_attribute(self.base.get_history_wrapper(self.base.prefix + ".cost_yesterday_car", 28, required=False), daily=True, offset_days=-1, pounds=True)
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
                    self.compare_hist[id]["cost"] = self.history_attribute(self.base.get_history_wrapper(result["entity_id"], 28), daily=True, pounds=True)
                    self.compare_hist[id]["metric"] = self.history_attribute(self.base.get_history_wrapper(result["entity_id"], 28), state_key="metric", attributes=True, daily=True, pounds=True)

    async def start(self):
        # Start the web server
        app = web.Application()
        app.router.add_get("/", self.html_default)
        app.router.add_get("/plan", self.html_plan)
        app.router.add_get("/log", self.html_log)
        app.router.add_get("/apps", self.html_apps)
        app.router.add_get("/charts", self.html_charts)
        app.router.add_get("/config", self.html_config)
        app.router.add_get("/entity", self.html_entity)
        app.router.add_post("/config", self.html_config_post)
        app.router.add_get("/dash", self.html_dash)
        app.router.add_get("/debug_yaml", self.html_debug_yaml)
        app.router.add_get("/debug_log", self.html_debug_log)
        app.router.add_get("/debug_apps", self.html_debug_apps)
        app.router.add_get("/debug_plan", self.html_debug_plan)
        app.router.add_get("/compare", self.html_compare)
        app.router.add_post("/compare", self.html_compare_post)
        app.router.add_get("/api/state", self.html_api_get_state)
        app.router.add_post("/api/state", self.html_api_post_state)
        app.router.add_post("/api/service", self.html_api_post_service)
        app.router.add_post("/api/login", self.html_api_login)

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
        """.format(
            dp0(load_power)
        )
        # Draw arrows and labels
        if pv_generating:
            html += """
                <!-- PV to House Arrow -->
                <line x1="200" y1="100" x2="250" y2="150" stroke="#2196F3" stroke-width="2" marker-end="url(#pv-arrow)" />
                <text x="250" y="120" text-anchor="middle" fill="#2196F3">{} W</text>
            """.format(
                dp0(pv_power)
            )
        else:
            # Make the PV to House line dashed if not generating
            html += """
                <!-- PV to House Arrow (dashed) -->
                <line x1="200" y1="100" x2="250" y2="150" stroke="#2196F3" stroke-width="2" stroke-dasharray="5,5" marker-end="url(#pv-arrow)" />
                <text x="250" y="120" text-anchor="middle" fill="#2196F3">{} W</text>
            """.format(
                dp0(pv_power)
            )
        if battery_charging:
            html += """
                <!-- Battery to House Arrow -->
                <line x1="200" y1="300" x2="250" y2="250" stroke="#FF9800" stroke-width="2" marker-end="url(#battery-arrow)" />
                <text x="260" y="280" text-anchor="middle" fill="#FF9800">{} W</text>
            """.format(
                dp0(battery_power)
            )
        else:
            html += """
                <!-- House to Battery Arrow -->
                <line x1="265" y1="235" x2="215" y2="275" stroke="#FF9800" stroke-width="2" marker-end="url(#battery-arrow)" />
                <text x="260" y="280" text-anchor="middle" fill="#FF9800">{} W</text>
            """.format(
                dp0(battery_power)
            )

        if grid_importing:
            html += """
                <!-- Grid to House Arrow -->
                <line x1="410" y1="290" x2="355" y2="240" stroke="#4CAF50" stroke-width="2" marker-end="url(#grid-arrow)" />
                <text x="350" y="280" text-anchor="middle" fill="#4CAF50">{} W</text>
            """.format(
                dp0(grid_power)
            )
        else:
            html += """
                <!-- House to Grid Arrow -->
                <line x1="340" y1="230" x2="390" y2="270" stroke="#4CAF50" stroke-width="2" marker-end="url(#grid-arrow)" />
                <text x="340" y="280" text-anchor="middle" fill="#4CAF50">{} W</text>
            """.format(
                dp0(grid_power)
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
        """

        return html

    def get_status_html(self, status, debug_enable, read_only, mode, version):
        text = ""
        if not self.base.dashboard_index:
            text += "<h2>Loading please wait...</h2>"
            return text

        text += "<h2>Status</h2>\n"
        text += "<table>\n"
        if status and (("Warn:" in status) or ("Error:" in status)):
            text += "<tr><td>Status</td><td bgcolor=#ff7777>{}</td></tr>\n".format(status)
        else:
            text += "<tr><td>Status</td><td>{}</td></tr>\n".format(status)
        text += "<tr><td>Version</td><td>{}</td></tr>\n".format(version)
        text += "<tr><td>Mode</td><td>{}</td></tr>\n".format(mode)
        text += "<tr><td>SOC</td><td>{}</td></tr>\n".format(self.get_battery_status_icon())
        # text += "<tr><td>Battery Power</td><td>{}</td></tr>\n".format(self.get_battery_power_icon())
        # text += "<tr><td>PV Power</td><td>{}</td></tr>\n".format(self.get_pv_power_icon())
        # text += "<tr><td>Load Power</td><td>{} W</td></tr>\n".format(dp0(self.base.load_power))
        # text += "<tr><td>Grid Power</td><td>{}</td></tr>\n".format(self.get_grid_power_icon())
        text += "<tr><td>Debug Enable</td><td>{}</td></tr>\n".format(debug_enable)
        text += "<tr><td>Set Read Only</td><td>{}</td></tr>\n".format(read_only)
        if self.base.arg_errors:
            count_errors = len(self.base.arg_errors)
            text += "<tr><td>Config</td><td bgcolor=#ff7777>apps.yaml has {} errors</td></tr>\n".format(count_errors)
        else:
            text += "<tr><td>Config</td><td>OK</td></tr>\n"
        text += "</table>\n"

        # Add power flow diagram
        text += "<h2>Power Flow</h2>\n"
        text += self.get_power_flow_diagram()

        text += "<table>\n"
        text += "<h2>Debug</h2>\n"
        text += "<tr><td>Download</td><td><a href='./debug_apps'>apps.yaml</a></td></tr>\n"
        text += "<tr><td>Create</td><td><a href='./debug_yaml'>predbat_debug.yaml</a></td></tr>\n"
        text += "<tr><td>Download</td><td><a href='./debug_log'>predbat.log</a></td></tr>\n"
        text += "<tr><td>Download</td><td><a href='./debug_plan'>predbat_plan.html</a></td></tr>\n"
        text += "</table>\n"
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
            history_chart = self.history_attribute(history)
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
    </script>
    """

        if refresh:
            text += '<meta http-equiv="refresh" content="{}" >'.format(refresh)
        text += "</head><body>"
        text += self.get_menu_html()
        return text

    def get_entity_detailedForecast(self, entity, subitem="pv_estimate"):
        results = {}
        detailedForecast = self.base.dashboard_values.get(entity, {}).get("attributes", {}).get("detailedForecast", {})
        if detailedForecast:
            for item in detailedForecast:
                sub = item.get(subitem, None)
                start = item.get("period_start", None)
                if sub and start:
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
height = height - 50;

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
        text = self.get_header("Predbat Plan", refresh=60)
        text += "<body>{}</body></html>\n".format(self.base.html_plan)
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

    def render_type(self, arg, value):
        """
        Render a value based on its type
        """
        text = ""
        if isinstance(value, list):
            text += "<table>"
            for item in value:
                text += "<tr><td>- {}</td></tr>\n".format(self.render_type(arg, item))
            text += "</table>"
        elif isinstance(value, dict):
            text += "<table>"
            for key in value:
                text += "<tr><td>{}</td><td>: {}</td></tr>\n".format(key, self.render_type(key, value[key]))
            text += "</table>"
        elif isinstance(value, str):
            pat = re.match(r"^[a-zA-Z]+\.\S+", value)
            if "{" in value:
                text = self.base.resolve_arg(arg, value, indirect=False, quiet=True)
                if text is None:
                    text = '<span style="background-color:#FFAAAA"> {} </p>'.format(value)
                else:
                    text = self.render_type(arg, text)
            elif pat and (arg != "service"):
                entity_id = value
                if "$" in entity_id:
                    entity_id, attribute = entity_id.split("$")
                    state = self.base.get_state_wrapper(entity_id=entity_id, attribute=attribute, default=None)
                else:
                    state = self.base.get_state_wrapper(entity_id=entity_id, default=None)

                if state is not None:
                    text = '<a href="./entity?entity_id={}">{}</a> = {}'.format(entity_id, value, state)
                else:
                    text = '<span style="background-color:#FFAAAA"> {} ? </span>'.format(value)
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
        text += self.get_status_html(self.base.current_status, self.base.debug_enable, self.base.set_read_only, self.base.predbat_mode, THIS_VERSION)
        text += "</body></html>\n"
        return web.Response(content_type="text/html", text=text)

    def prune_today(self, data, prune=True, group=15, prune_future=False, intermediate=False):
        """
        Remove data from before today
        """
        results = {}
        last_time = None
        prev_value = None
        for key in data:
            # Convert key in format '2024-09-07T15:40:09.799567+00:00' into a datetime
            if "." in key:
                timekey = datetime.strptime(key, TIME_FORMAT_SECONDS)
            else:
                timekey = datetime.strptime(key, TIME_FORMAT)
            if last_time and (timekey - last_time).seconds < group * 60:
                continue
            if intermediate and last_time and ((timekey - last_time).seconds > group * 60):
                # Large gap, introduce intermediate data point
                seconds_gap = (timekey - last_time).seconds
                for i in range(1, seconds_gap // (group * 60)):
                    new_time = last_time + timedelta(seconds=i * group * 60)
                    results[new_time.strftime(TIME_FORMAT)] = prev_value
            if not prune or (timekey > self.base.midnight_utc):
                if prune_future and (timekey > self.base.now_utc):
                    continue
                results[key] = data[key]
                last_time = timekey
                prev_value = data[key]
        return results

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
            cost_pkwh_today = self.prune_today(self.cost_today_hist, prune=False, prune_future=False)
            cost_pkwh_hour = self.prune_today(self.cost_hour_hist, prune=False, prune_future=False)
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
            pv_power = self.prune_today(self.pv_power_hist, prune=chart == "PV")
            pv_forecast = self.prune_today(self.pv_forecast_hist, prune=chart == "PV", intermediate=True)
            pv_today_forecast = self.prune_today(self.get_entity_detailedForecast("sensor." + self.base.prefix + "_pv_today", "pv_estimate"), prune=False)
            pv_today_forecast10 = self.prune_today(self.get_entity_detailedForecast("sensor." + self.base.prefix + "_pv_today", "pv_estimate10"), prune=False)
            pv_today_forecast90 = self.prune_today(self.get_entity_detailedForecast("sensor." + self.base.prefix + "_pv_today", "pv_estimate90"), prune=False)
            series_data = [
                {"name": "PV Power", "data": pv_power, "opacity": "1.0", "stroke_width": "3", "stroke_curve": "smooth", "color": "#f5c43d"},
                {"name": "Forecast History", "data": pv_forecast, "opacity": "0.3", "stroke_width": "3", "stroke_curve": "smooth", "color": "#a8a8a7", "chart_type": "area"},
                {"name": "Forecast", "data": pv_today_forecast, "opacity": "0.3", "stroke_width": "2", "stroke_curve": "smooth", "chart_type": "area", "color": "#a8a8a7"},
                {"name": "Forecast 10%", "data": pv_today_forecast10, "opacity": "0.3", "stroke_width": "2", "stroke_curve": "smooth", "chart_type": "area", "color": "#6b6b6b"},
                {"name": "Forecast 90%", "data": pv_today_forecast90, "opacity": "0.3", "stroke_width": "2", "stroke_curve": "smooth", "chart_type": "area", "color": "#cccccc"},
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

    async def html_apps(self, request):
        """
        Render apps.yaml as an HTML page
        """
        self.default_page = "./apps"
        text = self.get_header("Predbat Apps.yaml", refresh=60 * 5)
        text += "<body>\n"
        warning = ""
        if self.base.arg_errors:
            warning = "&#9888;"
        text += "{}<a href='./debug_apps'>apps.yaml</a> - has {} errors<br>\n".format(warning, len(self.base.arg_errors))
        text += "<table>\n"
        text += "<tr><th>Name</th><th>Value</th><td>\n"

        args = self.base.args
        for arg in args:
            value = args[arg]
            if "api_key" in arg or "cloud_key" in arg:
                value = '<span title = "{}"> (hidden)</span>'.format(value)
            arg_errors = self.base.arg_errors.get(arg, "")
            if arg_errors:
                text += '<tr><td bgcolor=#FF7777><span title="{}">&#9888;{}</span></td><td>{}</td></tr>\n'.format(arg_errors, arg, self.render_type(arg, value))
            else:
                text += "<tr><td>{}</td><td>{}</td></tr>\n".format(arg, self.render_type(arg, value))
        args = self.base.unmatched_args
        for arg in args:
            value = args[arg]
            text += '<tr><td>{}</td><td><span style="background-color:#FFAAAA">{}</span></td></tr>\n'.format(arg, self.render_type(arg, value))

        text += "</table>"
        text += "</body></html>\n"
        return web.Response(content_type="text/html", text=text)

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
        text += '<form class="form-inline" action="./config" method="post" enctype="multipart/form-data" id="configform">\n'
        text += "<table>\n"
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
                    text += '<td><select name="{}" id="{}" onchange="javascript: this.form.submit();">'.format(useid, useid)
                    text += '<option value={} label="{}" {}>{}</option>'.format("off", "off", "selected" if not value else "", "off")
                    text += '<option value={} label="{}" {}>{}</option>'.format("on", "on", "selected" if value else "", "on")
                    text += "</select></td>\n"
                elif itemtype == "input_number":
                    text += "<td>{}</td>\n".format(input_number.format(useid, useid, value, item.get("min", 0), item.get("max", 100), item.get("step", 1)))
                elif itemtype == "select":
                    options = item.get("options", [])
                    if value not in options:
                        options.append(value)
                    text += '<td><select name="{}" id="{}" onchange="javascript: this.form.submit();">'.format(useid, useid)
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
        text += '<form class="form-inline" action="./compare" method="post" enctype="multipart/form-data" id="compareform">\n'
        active = self.base.get_arg("compare_active", False)

        if not active:
            text += '<button type="submit" form="compareform" value="run">Compare now</button>\n'
        else:
            text += '<button type="submit" form="compareform" value="run" disabled>Running..</button>\n'

        text += '<input type="hidden" name="run" value="run">\n'
        text += "</form>"

        text += "<table>\n"
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

    async def html_default(self, request):
        """
        Redirect to the default page
        """
        return web.HTTPFound(self.default_page)

    def get_menu_html(self):
        """
        Return the Predbat Menu page as HTML
        """
        text = ""
        # Check if there are configuration errors
        config_warning = ""
        if self.base.arg_errors:
            config_warning = '<span style="color: #ffcc00; margin-left: 5px;">&#9888;</span>'

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
    width:  100%; /* Make sure it spans the full width */
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
             src="https://github-production-user-asset-6210df.s3.amazonaws.com/48591903/249456079-e98a0720-d2cf-4b71-94ab-97fe09b3cee1.png"
             data-light-src="https://github-production-user-asset-6210df.s3.amazonaws.com/48591903/249456079-e98a0720-d2cf-4b71-94ab-97fe09b3cee1.png"
             data-dark-src="https://raw.githubusercontent.com/springfall2008/batpred/refs/heads/main/docs/images/bat_logo_dark.png"
             alt="Predbat Logo"
        >
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
