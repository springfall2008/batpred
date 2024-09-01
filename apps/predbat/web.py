# This code creates a web server and serves up the Predbat web pages

from aiohttp import web
import asyncio
import os
import re

from config import CONFIG_ITEMS
from utils import calc_percent_limit


class WebInterface:
    def __init__(self, base) -> None:
        self.abort = False
        self.base = base
        self.log = base.log

    async def start(self):
        # Start the web server on port 5052
        app = web.Application()
        app.router.add_get("/", self.html_index)
        app.router.add_get("/plan", self.html_plan)
        app.router.add_get("/log", self.html_log)
        app.router.add_get("/menu", self.html_menu)
        app.router.add_get("/apps", self.html_apps)
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
        text += "<tr><th>Icon</th><th>Name</th><th>Entity</th><th>State</th><th>Attributes</th></tr>\n"

        for entity in self.base.dashboard_index:
            state = self.base.dashboard_values.get(entity, {}).get("state", None)
            attributes = self.base.dashboard_values.get(entity, {}).get("attributes", {})
            unit_of_measurement = attributes.get("unit_of_measurement", "")
            icon = attributes.get("icon", "")
            if icon:
                icon = '<span class="mdi mdi-{}"></span>'.format(icon.replace("mdi:", ""))
            if unit_of_measurement is None:
                unit_of_measurement = ""
            friendly_name = attributes.get("friendly_name", "")
            if not state:
                state = "None"
            text += "<tr><td> {} </td><td> {} </td><td>{}</td><td>{} {}</td><td>{}</td></tr>\n".format(
                icon, friendly_name, entity, state, unit_of_measurement, self.get_attributes_html(entity)
            )
        text += "</table>\n"

        return text

    def get_header(self, title, refresh=0):
        """
        Return the HTML header for a page
        """
        text = "<!doctype html><html><head><title>Predbat Web Interface</title>"

        text += """
<link href="https://cdn.jsdelivr.net/npm/@mdi/font@7.4.47/css/materialdesignicons.min.css" rel="stylesheet">
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

    async def html_plan(self, request):
        """
        Return the Predbat plan as an HTML page
        """
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
        if os.path.exists(logfile):
            with open(logfile, "r") as f:
                logdata = f.read()

        # Decode method get arguments
        args = request.query
        errors = False
        warnings = False
        if "errors" in args:
            errors = True
        if "warnings" in args:
            warnings = True

        loglines = logdata.split("\n")
        text = self.get_header("Predbat Log", refresh=10)
        text += "<body bgcolor=#ffffff>"

        if errors:
            text += "<h2>Logfile (errors)</h2>\n"
        elif warnings:
            text += "<h2>Logfile (Warnings)</h2>\n"
        else:
            text += "<h2>Logfile (All)</h2>\n"

        text += '- <a href="/log">All</a> '
        text += '<a href="/log?warnings">Warnings</a> '
        text += '<a href="/log?errors">Errors</a><br>\n'

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
        self.log("Post data: {}".format(postdata))
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
        raise web.HTTPFound("/config")

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
        text = self.get_header("Predbat Dashboard")
        text += "<body>\n"
        soc_perc = calc_percent_limit(self.base.soc_kw, self.base.soc_max)
        text += self.get_status_html(soc_perc, self.base.previous_status)
        text += "</body></html>\n"
        return web.Response(content_type="text/html", text=text)

    async def html_apps(self, request):
        """
        Render apps.yaml as an HTML page
        """
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

        text = self.get_header("Predbat Config", refresh=60)
        text += "<body>\n"
        text += '<form class="form-inline" action="/config" method="post" enctype="multipart/form-data" id="configform">\n'
        text += "<table>\n"
        text += "<tr><th>Name</th><th>Entity</th><th>Type</th><th>Current</th><th>Default</th><th>Select</th></tr>\n"

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

                if itemtype == "input_number" and item.get("step", 1) == 1:
                    value = int(value)

                text += "<tr><td>{}</td><td>{}</td><td>{}</td>".format(friendly, entity, itemtype)
                if value == default:
                    text += '<td class="default">{}</td><td>{}</td>\n'.format(value, default)
                else:
                    text += '<td class="modified">{}</td><td>{}</td>\n'.format(value, default)

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
        text += '<td><a href="/dash" target="main_frame">Dash</a></td>\n'
        text += '<td><a href="/plan" target="main_frame">Plan</a></td>\n'
        text += '<td><a href="/config" target="main_frame">Config</a></td>\n'
        text += '<td><a href="/apps" target="main_frame">apps.yaml</a></td>\n'
        text += '<td><a href="/log?warnings" target="main_frame">Log</a></td>\n'
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
        text += '<iframe src="/menu" title="Menu frame" class="menu-frame" name="menu_frame"></iframe>\n'
        text += '<iframe src="/dash" title="Main frame" class="main-frame" name="main_frame"></iframe>\n'
        text += "</div>\n"
        text += "</body></html>\n"
        return web.Response(content_type="text/html", text=text)
