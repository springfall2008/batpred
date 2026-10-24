"""Regression tests for flat modern UI assets and their HTTP behaviour."""

import asyncio
import gzip
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from web import WebInterface


async def check_modern_ui_assets():
    """Exercise the real asset handlers without a Home Assistant connection."""
    with TemporaryDirectory() as directory:
        root = Path(directory)
        html = b'<html><div id="root"></div></html>'
        javascript = b"const text = 'Predbat';\n" * 200
        (root / "predbat_ui.html").write_bytes(html)
        (root / "predbat_ui.js").write_bytes(javascript)
        (root / "predbat_ui_style.css").write_text("body { color: black; }")
        (root / "predbat_ui_scene.webp").write_bytes(b"RIFF-webp-fixture")
        for name in ("apps.yaml", "secrets.yaml", "web.py", "other.js", "predbat_ui_secrets.yaml"):
            (root / name).write_text("private configuration")
        (root / "predbat_ui_link.js").symlink_to(root / "secrets.yaml")
        (root / "predbat_ui_directory.js").mkdir()
        interface = WebInterface.__new__(WebInterface)
        app = web.Application()
        app.router.add_get("/", interface.html_modern_ui)
        app.router.add_get("/{filename:.*}", interface.html_modern_ui_asset)
        client = TestClient(TestServer(app))
        with patch("web.__file__", str(root / "web.py")):
            await client.start_server()
            try:
                response = await client.get("/", headers={"Accept-Encoding": "identity"})
                assert response.status == 200
                assert await response.read() == html
                assert response.content_type == "text/html"
                assert response.headers["Cache-Control"] == "no-cache"
                assert "Last-Modified" in response.headers
                etag = response.headers["ETag"]
                response = await client.get("/", headers={"If-None-Match": etag, "Accept-Encoding": "identity"})
                assert response.status == 304
                response = await client.get("/predbat_ui.js", headers={"Accept-Encoding": "gzip"}, auto_decompress=False)
                assert response.status == 200
                assert response.headers["Content-Encoding"] == "gzip"
                assert "Accept-Encoding" in response.headers["Vary"]
                assert gzip.decompress(await response.read()) == javascript
                assert response.headers["Cache-Control"] == "no-cache"
                for name in ("predbat_ui_style.css", "predbat_ui_scene.webp"):
                    response = await client.get("/" + name)
                    assert response.status == 200, name
                for name in (
                    "apps.yaml",
                    "secrets.yaml",
                    "web.py",
                    "other.js",
                    "predbat_ui_secrets.yaml",
                    "predbat_ui_link.js",
                    "predbat_ui_directory.js",
                    "predbat_ui_missing.js",
                    "../secrets.yaml",
                    "predbat_ui_../secrets.yaml",
                    "predbat_ui_%2f",
                ):
                    response = await client.get("/" + name)
                    assert response.status == 404, name
                    assert "private configuration" not in await response.text(), name
                os.utime(root / "predbat_ui.html", (1_700_000_000, 1_700_000_000))
                response = await client.get("/", headers={"Accept-Encoding": "identity"})
                modified = response.headers["Last-Modified"]
                response = await client.get("/", headers={"If-Modified-Since": modified, "Accept-Encoding": "identity"})
                assert response.status == 304
                (root / "predbat_ui.html").write_bytes(html + b"updated")
                response = await client.get("/", headers={"If-None-Match": etag, "Accept-Encoding": "identity"})
                assert response.status == 200
                assert await response.read() == html + b"updated"
                (root / "predbat_ui.html").unlink()
                response = await client.get("/")
                assert response.status == 503
            finally:
                await client.close()


def run_web_modern_ui_tests():
    """Run HTTP regressions in the standard web-functions test group."""
    print("**** Testing flat modern UI files, access restrictions, caching and compression ****")
    asyncio.run(check_modern_ui_assets())
    return 0
