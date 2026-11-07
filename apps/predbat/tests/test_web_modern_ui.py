"""Regression tests for flat modern UI assets and their HTTP behaviour."""

import asyncio
import gzip
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from web import WebInterface
from web_theme import WebTheme


async def check_empty_asset_responses(server, etag, modified):
    """Ensure cached and HEAD responses cannot corrupt a reused ingress connection."""
    for encoding in ("deflate", "gzip"):
        for method, condition, status in (
            ("GET", "If-None-Match: " + etag, 304),
            ("GET", "If-Modified-Since: " + modified, 304),
            ("HEAD", "", 200),
        ):
            reader, writer = await asyncio.open_connection(server.host, server.port)
            try:
                request = f"{method} /ui/ HTTP/1.1\r\nHost: localhost\r\nAccept-Encoding: {encoding}\r\n"
                if condition:
                    request += condition + "\r\n"
                request += "\r\nGET /ui/predbat_ui_scene.webp HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
                writer.write(request.encode("ascii"))
                await writer.drain()
                data = await asyncio.wait_for(reader.read(), timeout=5)
                headers, following = data.split(b"\r\n\r\n", 1)
                assert headers.startswith(f"HTTP/1.1 {status} ".encode()), headers
                assert following.startswith(b"HTTP/1.1 200 OK\r\n"), (encoding, method, condition, following[:100])
                assert following.endswith(b"RIFF-webp-fixture"), following
            finally:
                writer.close()
                await writer.wait_closed()


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
        (root / "predbat_ui_retro.woff2").write_bytes(b"wOF2-font-fixture")
        for name in ("apps.yaml", "secrets.yaml", "web.py", "other.js", "predbat_ui_secrets.yaml"):
            (root / name).write_text("private configuration")
        (root / "predbat_ui_link.js").symlink_to(root / "secrets.yaml")
        (root / "predbat_ui_directory.js").mkdir()
        interface = WebInterface.__new__(WebInterface)
        interface.theme_page = WebTheme(SimpleNamespace(storage=SimpleNamespace(load=AsyncMock(return_value=None), age=AsyncMock(return_value=None))))
        app = web.Application()
        interface.default_page = "./plan"
        app.router.add_get("/", interface.html_default)
        interface._register_modern_ui_routes(app)
        client = TestClient(TestServer(app))
        with patch("web.__file__", str(root / "web.py")):
            await client.start_server()
            try:
                landing = await client.get("/")
                assert landing.status == 200
                assert "predbat-interface:" in await landing.text()
                assert landing.headers["Cache-Control"] == "no-store"
                redirect = await client.get("/ui", allow_redirects=False)
                assert redirect.status == 302 and redirect.headers["Location"] == "./ui/"
                for page in ("plan", "apps_editor", "chat", "annual", "cards", "theme"):
                    page_response = await client.get("/ui/" + page)
                    assert page_response.status == 200 and await page_response.read() == html
                assert (await client.get("/ui/unknown")).status == 404
                theme = await client.get("/api/theme")
                assert theme.status == 200 and (await theme.json())["preset"] == "predbat"
                response = await client.get("/ui/", headers={"Accept-Encoding": "identity"})
                assert response.status == 200
                assert await response.read() == html
                assert response.content_type == "text/html"
                assert response.headers["Cache-Control"] == "no-cache"
                assert "Last-Modified" in response.headers
                etag = response.headers["ETag"]
                response = await client.get("/ui/", headers={"If-None-Match": etag, "Accept-Encoding": "identity"})
                assert response.status == 304
                response = await client.get("/ui/predbat_ui.js", headers={"Accept-Encoding": "gzip"}, auto_decompress=False)
                assert response.status == 200
                assert response.headers["Content-Encoding"] == "gzip"
                assert "Accept-Encoding" in response.headers["Vary"]
                assert gzip.decompress(await response.read()) == javascript
                assert response.headers["Cache-Control"] == "no-cache"
                for name in ("predbat_ui_style.css", "predbat_ui_scene.webp", "predbat_ui_retro.woff2"):
                    response = await client.get("/ui/" + name)
                    assert response.status == 200, name
                    if name.endswith(".woff2"):
                        assert response.content_type == "font/woff2"
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
                    response = await client.get("/ui/" + name)
                    assert response.status == 404, name
                    assert "private configuration" not in await response.text(), name
                os.utime(root / "predbat_ui.html", (1_700_000_000, 1_700_000_000))
                response = await client.get("/ui/", headers={"Accept-Encoding": "identity"})
                modified = response.headers["Last-Modified"]
                await response.read()
                await check_empty_asset_responses(client.server, response.headers["ETag"], modified)
                response = await client.get("/ui/", headers={"If-Modified-Since": modified, "Accept-Encoding": "identity"})
                assert response.status == 304
                (root / "predbat_ui.html").write_bytes(html + b"updated")
                response = await client.get("/ui/", headers={"If-None-Match": etag, "Accept-Encoding": "identity"})
                assert response.status == 200
                assert await response.read() == html + b"updated"
                (root / "predbat_ui.html").unlink()
                response = await client.get("/ui/")
                assert response.status == 503
            finally:
                await client.close()


def run_web_modern_ui_tests():
    """Run HTTP regressions in the standard web-functions test group."""
    print("**** Testing flat modern UI files, access restrictions, caching and compression ****")
    asyncio.run(check_modern_ui_assets())
    return 0
