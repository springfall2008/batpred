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
        text = "<html><head><title>Predbat Plan</title>"

        text += """
<style>
.switch {
  position: relative;
  display: inline-block;
  width: 60px;
  height: 34px;
}

.switch input {
  opacity: 0;
  width: 0;
  height: 0;
}

.slider {
  position: absolute;
  cursor: pointer;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background-color: #ccc;
  -webkit-transition: .4s;
  transition: .4s;
}

.slider:before {
  position: absolute;
  content: "";
  height: 26px;
  width: 26px;
  left: 4px;
  bottom: 4px;
  background-color: white;
  -webkit-transition: .4s;
  transition: .4s;
}

input:checked + .slider {
  background-color: #2196F3;
}

input:focus + .slider {
  box-shadow: 0 0 1px #2196F3;
}

input:checked + .slider:before {
  -webkit-transform: translateX(26px);
  -ms-transform: translateX(26px);
  transform: translateX(26px);
}

/* Rounded sliders */
.slider.round {
  border-radius: 34px;
}

.slider.round:before {
  border-radius: 50%;
}

body {font-family: Arial, Helvetica, sans-serif;}
* {box-sizing: border-box;}

.form-inline {
  display: flex;
  flex-flow: row wrap;
  align-items: center;
}

.form-inline label {
  margin: 5px 10px 5px 0;
}

.form-inline input {
  vertical-align: middle;
  margin: 5px 10px 5px 0;
  padding: 10px;
  background-color: #fff;
  border: 1px solid #ddd;
}

.form-inline button {
  padding: 5px 10px;
  background-color: dodgerblue;
  border: 1px solid #ddd;
  color: white;
  cursor: pointer;
}

.form-inline button:hover {
  background-color: royalblue;
}

@media (max-width: 800px) {
  .form-inline input {
    margin: 10px 0;
  }

  .form-inline {
    flex-direction: column;
    align-items: stretch;
  }
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
        self.log("post data {}".format(postdata))
        for pitem in postdata:
            new_value = postdata[pitem]
            for item in CONFIG_ITEMS:
                if item.get("entity") == pitem:
                    old_value = item.get("value", "")
                    if old_value != new_value:
                        item["value"] = new_value
                        self.log("set {} from {} to {}".format(pitem, old_value, new_value))
                        service_data = {}
                        itemtype = item.get("type", "")
                        service_data["domain"] = itemtype
                        service_data["entity_id"] = pitem
                        if itemtype == "switch":
                            service_data["service"] = "turn_on" if new_value else "turn_off"
                        elif itemtype == "input_number":
                            service_data["service"] = "set_value"
                            service_data["data"] = {"value": new_value}
                        elif itemtype == "select":
                            service_data["service"] = "select_option"
                            service_data["data"] = {"option": new_value}
                        else:
                            continue
                        self.log("Call service {}".format(service_data))
                        # await self.base.trigger_callback(service_data)
        return await self.html_config(request)

    async def html_config(self, request):
        """
        Return the Predbat config as an HTML page
        """

        text = self.get_header("Predbat Config", refresh=False)
        text += "<body bgcolor=#ffffff>\n"
        text += "<h1>Predbat Config</h1>\n"

        text += '<form class="form-inline" action="/config" method="post">'
        text += '<button type="save">Save</button>\n'
        text += "<table width=100%>\n"
        text += "<tr><td><b>Name</b></td><td><b>Entity</b></td><td><b>Type</b></td><td><b>Select</b></td><td><b>Current/Default</b></td></tr>\n"

        switch = """
            <label class="switch">
            <input type="checkbox" id="{}" {}>
            <span class="slider round"></span>
            </label>
            """
        input_number = """
            <input type="number" id="{}" value="{}" min="{}" max="{}" step="{}" style="width: 100px;">
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

                text += "<tr><td>{}</td><td>{}</td><td>{}</td>".format(friendly, entity, itemtype)
                if itemtype == "switch":
                    text += "<td>{}</td>\n".format(switch.format(entity, "checked" if value else ""))
                elif itemtype == "input_number":
                    text += "<td>{}</td>\n".format(input_number.format(entity, value, item.get("min", 0), item.get("max", 100), item.get("step", 1)))
                elif itemtype == "select":
                    options = item.get("options", [])
                    if value not in options:
                        options.append(value)
                    text += '<td><select name="{}" id="{}">'.format(entity, entity)
                    for option in options:
                        selected = option == value
                        text += '<option value="{}" {}>{}</option>'.format(option, "selected" if selected else "", option)
                    text += "</select></td>\n"
                else:
                    text += "<td>{}</td>\n".format(value)
                text += "<td>{} / {}</td></tr>\n".format(value, default)
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
