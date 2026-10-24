"""Regression checks for web listener configuration and file/HTML trust boundaries."""

import asyncio
import html
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from web import WebInterface


class WebSecurityTests(unittest.TestCase):
    """Exercise the actual handlers using local files and a fake listener."""

    def test_file_containment(self):
        """Allow in-root files while rejecting sibling prefixes and escaping symlinks."""
        original = os.getcwd()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            sibling = Path(directory) / "root-other"
            root.mkdir()
            sibling.mkdir()
            (root / "inside.txt").write_text("inside")
            (sibling / "outside.txt").write_text("outside")
            (root / "escape").symlink_to(sibling, target_is_directory=True)
            (root / "alias.txt").symlink_to(root / "inside.txt")
            stub = SimpleNamespace(get_header=lambda *args, **kwargs: "", log=lambda *args: None)
            try:
                os.chdir(root)
                for name in ("inside.txt", "alias.txt"):
                    response = asyncio.run(WebInterface.html_download_file(stub, SimpleNamespace(query={"file": name})))
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.body, b"inside")
                for query in (
                    {"path": "../root-other", "file": "outside.txt"},
                    {"file": "../root-other/outside.txt"},
                    {"path": "escape", "file": "outside.txt"},
                    {"file": "escape/outside.txt"},
                    {"file": str(sibling / "outside.txt")},
                ):
                    response = asyncio.run(WebInterface.html_download_file(stub, SimpleNamespace(query=query)))
                    self.assertEqual(response.status, 403, query)
                    response = asyncio.run(WebInterface.html_browse(stub, SimpleNamespace(query=query)))
                    self.assertNotIn('<pre class="code-content">outside</pre>', response.text, query)
                response = asyncio.run(WebInterface.html_browse(stub, SimpleNamespace(query={})))
                self.assertNotIn(">escape</a>", response.text)
                self.assertNotIn(" escape</a>", response.text)
            finally:
                os.chdir(original)

    def test_bind_address(self):
        """Pass the default or configured interface to the listener without opening a socket."""
        for address in (None, "127.0.0.1", "192.168.1.10"):
            stub = MagicMock()
            stub.web_port = 5052
            stub.get_arg.side_effect = lambda key, default, indirect: address if address is not None else default
            stub.base.plugin_system = None
            stub.registered_endpoints = []
            stub.api_stop = True
            stub.annual_page.job.cancel = AsyncMock()
            runner = SimpleNamespace(setup=AsyncMock(), cleanup=AsyncMock())
            site = SimpleNamespace(start=AsyncMock())
            with patch("web.web.Application"), patch("web.web.AppRunner", return_value=runner), patch("web.web.TCPSite", return_value=site) as listener:
                asyncio.run(WebInterface.start(stub))
                listener.assert_called_once_with(runner, address or "0.0.0.0", 5052)
                site.start.assert_awaited_once()

    def test_entity_html(self):
        """Entity state and unit markup remain text in the Apps editor."""
        payload = '<img src=x onerror="bad()">'
        stub = SimpleNamespace(get_state_wrapper=lambda **kwargs: payload)
        rendered = WebInterface.render_type(stub, "load_today", "sensor.example")
        self.assertNotIn(payload, rendered)
        self.assertIn(html.escape(payload), rendered)
        self.assertIn('href="./entity?entity_id=sensor.example"', rendered)

    def test_masked_yaml_parse_error(self):
        """Malformed configuration must not quote credentials in a masked attachment."""
        original = os.getcwd()
        with tempfile.TemporaryDirectory() as root:
            try:
                os.chdir(root)
                Path("apps.yaml").write_text('pred_bat:\n  ha_key: "DUMMY_PASSWORD_123456\n')
                handler = WebInterface.__new__(WebInterface)
                handler.log = lambda *args: None
                response = asyncio.run(handler.html_debug_apps(SimpleNamespace(query={})))
                self.assertEqual(response.status, 200)
                self.assertNotIn(b"DUMMY_PASSWORD_123456", response.body)
                self.assertIn(b"could not parse", response.body)
            finally:
                os.chdir(original)

    def test_debug_denylist(self):
        """Both stored and downloaded diagnostics scrub sensitive values under ordinary keys."""
        from userinterface import UserInterface
        import yaml

        with tempfile.TemporaryDirectory() as root:
            instance = UserInterface.__new__(UserInterface)
            instance.now_utc = datetime(2026, 10, 24, tzinfo=timezone.utc)
            instance.config_root = root
            instance.config_root_p = root
            instance.args = {"redact_strings": ["DUMMY_PRIVATE_METER_123456"]}
            instance.sensor_note = "DUMMY_PRIVATE_METER_123456"
            instance.inverters = []
            instance.CONFIG_ITEMS = []
            instance.components = None
            instance.log = lambda *args: None
            text = instance.create_debug_yaml(write_file=False)
            instance.create_debug_yaml(write_file=True)
            saved = next((Path(root) / "debug").glob("*.yaml")).read_text()
            for value in (text, saved):
                self.assertNotIn("DUMMY_PRIVATE_METER_123456", value)
                self.assertIn("sensor_note", yaml.unsafe_load(value))
            self.assertEqual(text, saved)
            from utils import compile_log_secret_pattern
            from userinterface import dump_debug_yaml
            from io import StringIO

            for secret in ("private\nmultiline", 'private "quoted" value', "private " * 30, "true"):
                section = StringIO()
                dump_debug_yaml({"sensor_note": "prefix " + secret + " suffix", "unrelated": True}, section, compile_log_secret_pattern({secret: "dummy"}))
                loaded = yaml.safe_load(section.getvalue())
                self.assertEqual(loaded["sensor_note"], "prefix <dummy> suffix")
                self.assertEqual(loaded["unrelated"], "<dummy>" if secret == "true" else True)

    def test_native_context_rejects_invalid_soc(self):
        """Invalid sensor values must fall back before a native pointer is constructed."""
        import prediction_kernel

        for soc, capacity in ((-2, 10), (11, 10), (float("nan"), 10), (float("inf"), 10), (2, 0), (2, float("nan"))):
            prediction = SimpleNamespace(log=lambda *args: None, soc_kw=soc, soc_max=capacity)
            with patch("prediction_kernel.load_kernel", return_value=MagicMock()) as loader:
                self.assertEqual(prediction_kernel.create_kernel_context(prediction), 0)
                loader.return_value.pk_create_context.assert_not_called()

    def test_native_curve_bounds(self):
        """Compile and exercise real C++ curve helpers when the local compiler is available."""
        compiler = shutil.which("g++") or shutil.which("clang++")
        if compiler is None:
            self.skipTest("No local C++ compiler available")
        with tempfile.TemporaryDirectory() as root:
            binary = str(Path(root) / "kernel_bounds")
            source = Path(__file__).with_name("test_kernel_bounds.cpp")
            with open(Path(root) / "compile.log", "w+") as output:
                result = subprocess.run([compiler, "-std=c++17", "-O0", "-pthread", str(source), "-o", binary], stdout=output, stderr=subprocess.STDOUT, timeout=30)
                output.seek(0)
                self.assertEqual(result.returncode, 0, output.read())
            with open(Path(root) / "run.log", "w+") as output:
                result = subprocess.run([binary], stdout=output, stderr=subprocess.STDOUT, timeout=5)
                output.seek(0)
                self.assertEqual(result.returncode, 0, output.read())


def run_web_security_tests(my_predbat):
    """Run the small web security regression suite through Predbat's test registry."""
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(WebSecurityTests))
    return not result.wasSuccessful()
