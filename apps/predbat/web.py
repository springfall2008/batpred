# This code creates a web server and serves up the Predbat web pages

from aiohttp import web
import asyncio
import os
import re
from datetime import datetime, timedelta

from config import CONFIG_ITEMS
from utils import calc_percent_limit
from config import TIME_FORMAT, TIME_FORMAT_SECONDS


class WebInterface:
    def __init__(self, base) -> None:
        self.abort = False
        self.base = base
        self.log = base.log
        self.default_page = "./dash"
        self.pv_power_hist = {}
        self.pv_forecast_hist = {}

    def history_attribute(self, history, state_key="state", last_updated_key="last_updated", scale=1.0):
        results = {}
        if history:
            history = history[0]

        if not history:
            return results

        # Process history
        for item in history:
            # Ignore data without correct keys
            if state_key not in item:
                continue
            if last_updated_key not in item:
                continue

            # Unavailable or bad values
            if item[state_key] == "unavailable" or item[state_key] == "unknown":
                continue

            # Get the numerical key and the timestamp and ignore if in error
            try:
                state = float(item[state_key]) * scale
                last_updated_time = item[last_updated_key]
            except (ValueError, TypeError):
                continue

            results[last_updated_time] = state
        return results

    def history_update(self):
        """
        Update the history data
        """
        self.log("Web interface history update")
        self.pv_power_hist = self.history_attribute(self.base.get_history_wrapper(self.base.prefix + ".pv_power", 7))
        self.pv_forecast_hist = self.history_attribute(self.base.get_history_wrapper("sensor." + self.base.prefix + "_pv_forecast_h0", 7))

    async def start(self):
        # Start the web server on port 5052
        app = web.Application()
        app.router.add_get("/", self.html_index)
        app.router.add_get("/plan", self.html_plan)
        app.router.add_get("/log", self.html_log)
        app.router.add_get("/menu", self.html_menu)
        app.router.add_get("/apps", self.html_apps)
        app.router.add_get("/charts", self.html_charts)
        app.router.add_get("/config", self.html_config)
        app.router.add_post("/config", self.html_config_post)
        app.router.add_get("/dash", self.html_dash)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", 5052)
        await site.start()
        print("Web interface started")
        while not self.abort:
            await asyncio.sleep(1)
        print("Web interface cleanup")
        await runner.cleanup()
        print("Web interface stopped")

    async def stop(self):
        print("Web interface stop called")
        self.abort = True
        await asyncio.sleep(1)

    def get_attributes_html(self, entity):
        text = ""
        attributes = self.base.dashboard_values.get(entity, {}).get("attributes", {})
        if not attributes:
            return ""
        text += "<table>"
        for key in attributes:
            if key in ["icon", "device_class", "state_class", "unit_of_measurement", "friendly_name"]:
                continue
            value = attributes[key]
            if len(str(value)) > 30:
                value = "(large data)"
            text += "<tr><td>{}</td><td>{}</td></tr>".format(key, value)
        text += "</table>"
        return text

    def icon2html(self, icon):
        if icon:
            icon = '<span class="mdi mdi-{}"></span>'.format(icon.replace("mdi:", ""))
        return icon

    def get_status_html(self, level, status):
        text = ""
        if not self.base.dashboard_index:
            text += "<h2>Loading please wait...</h2>"
            return text

        text += "<h2>Status</h2>\n"
        text += "<table>\n"
        text += "<tr><td>Status</td><td>{}</td></tr>\n".format(status)
        text += "<tr><td>SOC</td><td>{}%</td></tr>\n".format(level)
        text += "</table>\n"
        text += "<br>\n"

        text += "<h2>Entities</h2>\n"
        text += "<table>\n"
        text += "<tr><th></th><th>Name</th><th>Entity</th><th>State</th><th>Attributes</th></tr>\n"

        for entity in self.base.dashboard_index:
            state = self.base.dashboard_values.get(entity, {}).get("state", None)
            attributes = self.base.dashboard_values.get(entity, {}).get("attributes", {})
            unit_of_measurement = attributes.get("unit_of_measurement", "")
            icon = self.icon2html(attributes.get("icon", ""))
            if unit_of_measurement is None:
                unit_of_measurement = ""
            friendly_name = attributes.get("friendly_name", "")
            if not state:
                state = "None"
            text += "<tr><td> {} </td><td> {} </td><td>{}</td><td>{} {}</td><td>{}</td></tr>\n".format(icon, friendly_name, entity, state, unit_of_measurement, self.get_attributes_html(entity))
        text += "</table>\n"

        return text

    def get_header(self, title, refresh=0):
        """
        Return the HTML header for a page
        """
        text = "<!doctype html><html><head><title>Predbat Web Interface</title>"

        text += """
<link href="https://cdn.jsdelivr.net/npm/@mdi/font@7.4.47/css/materialdesignicons.min.css" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/apexcharts"></script>
<style>
        body, html {
            margin: 0;
            padding: 0;
            height: 100%;
        }
       .iframe-container {
            display: flex;
            flex-direction: column;
            height: 100vh;
        }
        /* Style for the top menu iframe */
        .menu-frame {
            height: 80px; /* Adjust the height of the menu bar */
            width: 100%;
            border: none;
            overflow: hidden;
        }
        /* Style for the full height iframe */
        .main-frame {
            flex-grow: 1;
            width: 100%;
            border: none;
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
        table {
            border-collapse: collapse;
            padding: 1px;
            border: 1px solid blue;
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
        .default {
            background-color: #fffffff;
        }
        .modified {
            background-color: #ffcccc;
        }
        """
        text += "</style>"

        if refresh:
            text += '<meta http-equiv="refresh" content="{}" >'.format(refresh)
        text += "</head>\n"
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

    def render_chart(self, series_data, yaxis_name, chart_name, now_str):
        """
        Render a chart
        """
        midnight_str = (self.base.midnight_utc + timedelta(days=1)).strftime(TIME_FORMAT)
        text = ""
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
    height: height
  },
  span: {
    start: 'minute', offset: '-12h'
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
        text += "var chart = new ApexCharts(document.querySelector('#chart'), options);\n"
        text += "chart.render();\n"
        text += "</script>\n"
        return text

    async def html_plan(self, request):
        """
        Return the Predbat plan as an HTML page
        """
        self.default_page = "./plan"
        html_plan = self.base.html_plan
        text = self.get_header("Predbat Plan", refresh=60)
        text += "<body>{}</body></html>\n".format(html_plan)
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
        if "errors" in args:
            errors = True
            self.default_page = "./log?errors"
        if "warnings" in args:
            warnings = True
            self.default_page = "./log?warnings"

        loglines = logdata.split("\n")
        text = self.get_header("Predbat Log", refresh=10)
        text += "<body bgcolor=#ffffff>"

        if errors:
            text += "<h2>Logfile (errors)</h2>\n"
        elif warnings:
            text += "<h2>Logfile (Warnings)</h2>\n"
        else:
            text += "<h2>Logfile (All)</h2>\n"

        text += '- <a href="./log">All</a> '
        text += '<a href="./log?warnings">Warnings</a> '
        text += '<a href="./log?errors">Errors</a><br>\n'

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

            for item in CONFIG_ITEMS:
                if item.get("entity") == pitem:
                    old_value = item.get("value", "")
                    step = item.get("step", 1)
                    itemtype = item.get("type", "")
                    if old_value is None:
                        old_value = item.get("default", "")
                    if itemtype == "input_number" and step == 1:
                        old_value = int(old_value)
                    if old_value != new_value:
                        self.log("set {} from {} to {}".format(pitem, old_value, new_value))
                        service_data = {}
                        service_data["domain"] = itemtype
                        if itemtype == "switch":
                            service_data["service"] = "turn_on" if new_value else "turn_off"
                            service_data["service_data"] = {"entity_id": pitem}
                        elif itemtype == "input_number":
                            service_data["service"] = "set_value"
                            service_data["service_data"] = {"entity_id": pitem, "value": new_value}
                        elif itemtype == "select":
                            service_data["service"] = "select_option"
                            service_data["service_data"] = {"entity_id": pitem, "option": new_value}
                        else:
                            continue
                        self.log("Call service {}".format(service_data))
                        await self.base.trigger_callback(service_data)
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
                text += "<tr><td>{}</td><td>: {}</td></tr>\n".format(key, self.render_type(arg, value[key]))
            text += "</table>"
        elif isinstance(value, str):
            pat = re.match(r"^[a-zA-Z]+\.\S+", value)
            if "{" in value:
                text = self.base.resolve_arg(arg, value, indirect=False, quiet=True)
                if text is None:
                    text = '<span style="background-color:#FFAAAA"> {} </p>'.format(value)
                else:
                    text = self.render_type(arg, text)
            elif pat:
                entity_id = value
                if "$" in entity_id:
                    entity_id, attribute = entity_id.split("$")
                    state = self.base.get_state_wrapper(entity_id=entity_id, attribute=attribute, default=None)
                else:
                    state = self.base.get_state_wrapper(entity_id=entity_id, default=None)

                if state:
                    text = "{} = {}".format(value, state)
                else:
                    text = '<span style="background-color:#FFAAAA"> {} ? </p>'.format(value)
            else:
                text = str(value)
        else:
            text = str(value)
        return text

    async def html_dash(self, request):
        """
        Render apps.yaml as an HTML page
        """
        self.default_page = "./dash"
        text = self.get_header("Predbat Dashboard")
        text += "<body>\n"
        soc_perc = calc_percent_limit(self.base.soc_kw, self.base.soc_max)
        text += self.get_status_html(soc_perc, self.base.current_status)
        text += "</body></html>\n"
        return web.Response(content_type="text/html", text=text)

    def prune_today(self, data, prune=True, group=15):
        """
        Remove data from before today
        """
        results = {}
        last_time = None
        for key in data:
            # Convert key in format '2024-09-07T15:40:09.799567+00:00' into a datetime
            if "." in key:
                timekey = datetime.strptime(key, TIME_FORMAT_SECONDS)
            else:
                timekey = datetime.strptime(key, TIME_FORMAT)
            if last_time and (timekey - last_time).seconds < group * 60:
                continue
            if not prune or (timekey > self.base.midnight_utc):
                results[key] = data[key]
                last_time = timekey
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
        best_discharge_limit_kw = self.get_entity_results(self.base.prefix + ".best_discharge_limit_kw")
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
                {"name": "Best Discharge Limit", "data": best_discharge_limit_kw, "opacity": "1.0", "stroke_width": "3", "stroke_curve": "stepline", "color": "#15eb1c"},
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
            series_data = [
                {"name": "Import", "data": rates, "opacity": "1.0", "stroke_width": "3", "stroke_curve": "stepline"},
                {"name": "Export", "data": rates_export, "opacity": "0.2", "stroke_width": "2", "stroke_curve": "stepline", "chart_type": "area"},
                {"name": "Gas", "data": rates_gas, "opacity": "0.2", "stroke_width": "2", "stroke_curve": "stepline", "chart_type": "area"},
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
            pv_forecast = self.prune_today(self.pv_forecast_hist, prune=chart == "PV")
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
        text = self.get_header("Predbat Config")
        text += "<body>\n"
        text += "<h2>{} Chart</h2>\n".format(chart)
        text += '- <a href="./charts?chart=Battery">Battery</a> '
        text += '<a href="./charts?chart=Power">Power</a> '
        text += '<a href="./charts?chart=Cost">Cost</a> '
        text += '<a href="./charts?chart=Rates">Rates</a> '
        text += '<a href="./charts?chart=InDay">InDay</a> '
        text += '<a href="./charts?chart=PV">PV</a> '
        text += '<a href="./charts?chart=PV7">PV7</a> '

        text += '<div id="chart"></div>'
        text += self.get_chart(chart=chart)
        text += "</body></html>\n"
        return web.Response(content_type="text/html", text=text)

    async def html_apps(self, request):
        """
        Render apps.yaml as an HTML page
        """
        self.default_page = "./apps"
        text = self.get_header("Predbat Config")
        text += "<body>\n"
        text += "<table>\n"
        text += "<tr><th>Name</th><th>Value</th><td>\n"

        args = self.base.args
        for arg in args:
            value = args[arg]
            if "api_key" in arg:
                value = '<span title = "{}"> (hidden)</span>'.format(value)
            text += "<tr><td>{}</td><td>{}</td></tr>\n".format(arg, self.render_type(arg, value))
        args = self.base.unmatched_args
        for arg in args:
            value = args[arg]
            text += '<tr><td>{}</td><td><span style="background-color:#FFAAAA">{}</span></td></tr>\n'.format(arg, self.render_type(arg, value))

        text += "</table>"
        text += "</body></html>\n"
        return web.Response(content_type="text/html", text=text)

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

        for item in CONFIG_ITEMS:
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

                text += "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td>".format(icon, friendly, entity, itemtype)
                if value == default:
                    text += '<td class="default">{} {}</td><td>{} {}</td>\n'.format(value, unit, default, unit)
                else:
                    text += '<td class="modified">{} {}</td><td>{} {}</td>\n'.format(value, unit, default, unit)

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

    async def html_menu(self, request):
        """
        Return the Predbat Menu page as an HTML page
        """
        text = self.get_header("Predbat Menu")
        text += "<body>\n"
        text += "<table><tr>\n"
        text += '<td><h2>Predbat</h2></td><td><img src="https://github-production-user-asset-6210df.s3.amazonaws.com/48591903/249456079-e98a0720-d2cf-4b71-94ab-97fe09b3cee1.png" width="50" height="50"></td>\n'
        text += '<td><a href="./dash" target="main_frame">Dash</a></td>\n'
        text += '<td><a href="./plan" target="main_frame">Plan</a></td>\n'
        text += '<td><a href="./charts" target="main_frame">Charts</a></td>\n'
        text += '<td><a href="./config" target="main_frame">Config</a></td>\n'
        text += '<td><a href="./apps" target="main_frame">apps.yaml</a></td>\n'
        text += '<td><a href="./log?warnings" target="main_frame">Log</a></td>\n'
        text += '<td><a href="https://springfall2008.github.io/batpred/" target="main_frame">Docs</a></td>\n'
        text += "</table></body></html>\n"
        return web.Response(content_type="text/html", text=text)

    async def html_index(self, request):
        """
        Return the Predbat index page as an HTML page
        """
        text = self.get_header("Predbat Index")
        text += "<body>\n"
        text += '<div class="iframe-container">\n'
        text += '<iframe src="./menu" title="Menu frame" class="menu-frame" name="menu_frame"></iframe>\n'
        text += '<iframe src="{}" title="Main frame" class="main-frame" name="main_frame"></iframe>\n'.format(self.default_page)
        text += "</div>\n"
        text += "</body></html>\n"
        return web.Response(content_type="text/html", text=text)
