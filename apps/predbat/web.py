# This code creates a web server and serves up the Predbat web pages

from aiohttp import web
import asyncio
import os
from config import CONFIG_ITEMS


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
        app.router.add_get("/config", self.html_config)
        app.router.add_post("/config", self.html_config_post)
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

    def get_header(self, title, refresh=False):
        """
        Return the HTML header for a page
        """
        text = "<!doctype html><html><head><title>Predbat Plan</title>"

        text += """
<style>
        body {
            font-family: Arial, sans-serif;
            text-align: left;
            margin: 40px;
            background-color: #f8f8f8;
            color: #333;
        }
        h1 {
            color: #4CAF50;
        }
        h3 {
            margin-bottom: 10px;
        }
        table {
            border-collapse: collapse;
            width: 100%;
            margin: 10px auto;
        }
        th,
        td {
            padding: 10px;
            border: 1px solid #ddd;
            text-align: left;
        }
        th {
            background-color: #4CAF50;
            color: white;
        }
</style>
        """

        if refresh:
            text += '<meta http-equiv="refresh" content="60" >'
        text += "</head>\n"
        return text

    async def html_plan(self, request):
        """
        Return the Predbat plan as an HTML page
        """
        html_plan = self.base.html_plan
        text = self.get_header("Predbat Plan", refresh=True)
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
        loglines = logdata.split("\n")
        text = self.get_header("Predbat Log", refresh=False)
        text += "<body bgcolor=#ffffff>"
        text += "<h1>Predbat Log</h1>\n"
        text += "<table width=100%>\n"

        total_lines = len(loglines)
        start_line = max(0, total_lines - 1024)
        lineno = start_line
        while lineno < total_lines:
            line = loglines[lineno]
            start_line = line[0:27]
            rest_line = line[27:]
            text += "<tr><td>{}</td><td nowrap><font color=#33cc33>{}</font> {}</td></tr>\n".format(lineno, start_line, rest_line)
            lineno += 1
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
                    if old_value is None:
                        old_value = item.get("default", "")
                    if old_value != new_value:
                        self.log("set {} from {} to {}".format(pitem, old_value, new_value))
                        service_data = {}
                        itemtype = item.get("type", "")
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
        return await self.html_config(request)

    async def html_config(self, request):
        """
        Return the Predbat config as an HTML page
        """

        text = self.get_header("Predbat Config", refresh=False)
        text += "<body>\n"
        text += "<h1>Predbat Config</h1>\n"

        text += '<form class="form-inline" action="/config" method="post" enctype="multipart/form-data" id="configform">\n'
        text += '<input type="submit" name="save" value="save"/>\n'
        text += '<input type="checkbox" name="testing" id="testing"/>\n'
        text += "<table>\n"
        text += "<tr><td><b>Name</b></td><td><b>Entity</b></td><td><b>Type</b></td><td><b>Select</b></td><td><b>Current/Default</b></td></tr>\n"

        switch = """
            <input type="checkbox" id="{}" name="{}" {}/>
            """
        input_number = """
            <input type="number" id="{}" name="{}" value="{}" min="{}" max="{}" step="{}"/>
            """

        for item in CONFIG_ITEMS:
            if self.base.user_config_item_enabled(item):
                value = item.get("value", "")
                if value is None:
                    value = item.get("default", "")
                entity = item.get("entity")
                friendly = item.get("friendly_name", "")
                itemtype = item.get("type", "")
                default = item.get("default", "")
                useid = entity.replace(".", "__")

                text += "<tr><td>{}</td><td>{}</td><td>{}</td>".format(friendly, entity, itemtype)
                if itemtype == "switch":
                    text += "<td>{}</td>\n".format(switch.format(useid, useid, "checked" if value else ""))
                elif itemtype == "input_number":
                    text += "<td>{}</td>\n".format(input_number.format(useid, useid, value, item.get("min", 0), item.get("max", 100), item.get("step", 1)))
                elif itemtype == "select":
                    options = item.get("options", [])
                    if value not in options:
                        options.append(value)
                    text += '<td><select name="{}" id="{}" form="configform">'.format(useid, useid)
                    for option in options:
                        selected = option == value
                        option_label = option if option else "None"
                        text += '<option value="{}" label="{}" {}>{}</option>'.format(option, option_label, "selected" if selected else "", option)
                    text += "</select></td>\n"
                else:
                    text += "<td>{}</td>\n".format(value)
                text += "<td>{} / {}</td>\n".format(value, default)
                text += "</tr>\n"

        text += "</table>"
        text += "</form>"
        text += "</body></html>\n"
        return web.Response(content_type="text/html", text=text)

    async def html_index(self, request):
        """
        Return the Predbat index page as an HTML page
        """
        # If method get then decode the dictionary of parameters sent in
        # if request.method == 'GET':
        # self.log("Request {}".format(request.query))
        # for key in request.query:
        #    self.log("Key: {} Value: {}".format(key, request.query[key]))
        text = self.get_header("Predbat Index", refresh=False)
        text += "<body><h1>Predbat</h1>\n"
        text += "<table><tr><td><a href='/plan'>Plan</a></td></tr></table>\n"
        text += "<table><tr><td><a href='/config'>Config</a></td></tr></table>\n"
        text += "<table><tr><td><a href='/log'>Log</a></td></tr></table>\n"
        text += "</body></html>\n"
        return web.Response(content_type="text/html", text=text)
