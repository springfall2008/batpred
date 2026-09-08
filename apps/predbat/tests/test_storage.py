# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt: off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
# fmt: on

import json
import os
import shutil
import tempfile
from datetime import datetime, timedelta, timezone

from storage import StorageLocalFiles
from tests.test_infra import run_async


def test_storage(my_predbat=None):
    """Run all StorageLocalFiles unit tests."""
    print("--- Storage tests ---")

    tmpdir = tempfile.mkdtemp()
    try:
        log_messages = []

        def log(msg):
            log_messages.append(msg)

        storage = StorageLocalFiles(tmpdir, log)

        # 1. yaml round-trip
        data_yaml = {"key": "value", "number": 42, "nested": {"a": 1}}
        assert run_async(storage.save("mod", "yaml_file", data_yaml, format="yaml")) is True
        loaded = run_async(storage.load("mod", "yaml_file"))
        assert loaded == data_yaml, "yaml round-trip failed: {}".format(loaded)

        # 2. json round-trip
        data_json = {"json_key": "json_value", "list": [1, 2, 3]}
        assert run_async(storage.save("mod", "json_file", data_json, format="json")) is True
        loaded = run_async(storage.load("mod", "json_file"))
        assert loaded == data_json, "json round-trip failed: {}".format(loaded)

        # 3. text round-trip
        data_text = "Hello, World!\nLine two."
        assert run_async(storage.save("mod", "text_file", data_text, format="text")) is True
        loaded = run_async(storage.load("mod", "text_file"))
        assert loaded == data_text, "text round-trip failed: {}".format(loaded)

        # 4. missing file returns None
        assert run_async(storage.load("mod", "nonexistent")) is None, "missing file should return None"

        # 5. expired file returns None
        past_expiry = datetime.now(timezone.utc) - timedelta(hours=1)
        assert run_async(storage.save("mod", "expired_file", {"data": 1}, format="yaml", expiry=past_expiry)) is True
        assert run_async(storage.load("mod", "expired_file")) is None, "expired file should return None"

        # 6. not-yet-expired file loads normally
        future_expiry = datetime.now(timezone.utc) + timedelta(hours=1)
        data_future = {"future": True}
        assert run_async(storage.save("mod", "future_file", data_future, format="yaml", expiry=future_expiry)) is True
        loaded = run_async(storage.load("mod", "future_file"))
        assert loaded == data_future, "non-expired file should load: {}".format(loaded)

        # 7. no-expiry file loads normally
        data_perm = {"permanent": True}
        assert run_async(storage.save("mod", "perm_file", data_perm, format="yaml", expiry=None)) is True
        loaded = run_async(storage.load("mod", "perm_file"))
        assert loaded == data_perm, "no-expiry file should load: {}".format(loaded)

        # 8. metadata sidecar is JSON and has expected fields
        meta_path = os.path.join(tmpdir, "cache", "mod_yaml_file.meta")
        assert os.path.exists(meta_path), "meta sidecar should exist"
        with open(meta_path, "r") as f:
            meta = json.load(f)
        assert meta["format"] == "yaml"
        assert meta["module"] == "mod"
        assert meta["expiry"] is None
        assert "created" in meta

        # 9. cleanup deletes expired files, leaves non-expired and no-expiry files
        run_async(storage.cleanup())

        cache_dir = os.path.join(tmpdir, "cache")
        assert not os.path.exists(os.path.join(cache_dir, "mod_expired_file.meta")), "expired meta should be deleted"
        assert not os.path.exists(os.path.join(cache_dir, "mod_expired_file.yaml")), "expired data should be deleted"
        assert os.path.exists(os.path.join(cache_dir, "mod_future_file.meta")), "non-expired meta should remain"
        assert os.path.exists(os.path.join(cache_dir, "mod_future_file.yaml")), "non-expired data should remain"
        assert os.path.exists(os.path.join(cache_dir, "mod_perm_file.meta")), "no-expiry meta should remain"
        assert os.path.exists(os.path.join(cache_dir, "mod_perm_file.yaml")), "no-expiry data should remain"

        # 10. unknown format falls back to yaml
        assert run_async(storage.save("mod", "bad_format", {"x": 1}, format="bad_format")) is True
        loaded = run_async(storage.load("mod", "bad_format"))
        assert loaded == {"x": 1}, "unknown format fallback to yaml should load: {}".format(loaded)

        # 11. age() returns a small positive number for a just-saved file
        assert run_async(storage.save("mod", "age_file", {"a": 1}, format="json")) is True
        age = run_async(storage.age("mod", "age_file"))
        assert age is not None, "age() should return a value for a saved file"
        assert 0.0 <= age < 1.0, "age of just-saved file should be less than 1 minute, got {}".format(age)

        # 12. age() returns None for a missing file
        assert run_async(storage.age("mod", "nonexistent_age")) is None, "age() should return None for missing file"

        # fetch_cached: miss → calls fetch_fn once, stores, returns
        calls = {"n": 0}

        async def _fetch():
            calls["n"] += 1
            return {"v": calls["n"]}

        first = run_async(storage.fetch_cached("fc", "k", _fetch, fresh_minutes=30, stale_minutes=35, format="json"))
        assert first == {"v": 1}, "fetch_cached miss should fetch: {}".format(first)
        assert calls["n"] == 1, "fetch_fn should be called exactly once on miss"

        # fetch_cached: fresh hit → does NOT call fetch_fn again
        second = run_async(storage.fetch_cached("fc", "k", _fetch, fresh_minutes=30, stale_minutes=35, format="json"))
        assert second == {"v": 1}, "fresh hit should return cached value: {}".format(second)
        assert calls["n"] == 1, "fetch_fn must not be called on a fresh hit"

        # fetch_cached: with fresh_minutes=0 every existing entry is "stale" → refresh path runs once
        calls2 = {"n": 0}

        async def _fetch2():
            calls2["n"] += 1
            return {"w": calls2["n"]}

        run_async(storage.save("fc2", "k", {"w": 0}, format="json"))
        out = run_async(storage.fetch_cached("fc2", "k", _fetch2, fresh_minutes=0, stale_minutes=999999, format="json"))
        assert out == {"w": 1}, "stale path should refresh and return fresh data: {}".format(out)
        assert calls2["n"] == 1, "stale path should call fetch_fn once"

        # fetch_cached: fetch_fn returning None on a hard miss → returns None, no crash
        async def _fetch_none():
            return None

        assert run_async(storage.fetch_cached("fc3", "missing", _fetch_none, format="json")) is None

        # fetch_cached: stale window + fetch_fn returns None → serve cached stale value
        run_async(storage.save("fc4", "k", {"w": 0}, format="json"))

        async def _fetch_none_stale():
            return None

        out = run_async(storage.fetch_cached("fc4", "k", _fetch_none_stale, fresh_minutes=0, stale_minutes=999999, format="json"))
        assert out == {"w": 0}, "stale path with None fetch should return cached stale: {}".format(out)

        # fetch_cached: stale window + fetch_fn RAISES → serve cached stale value, do not propagate
        run_async(storage.save("fc5", "k", {"w": 7}, format="json"))

        async def _fetch_raise():
            raise RuntimeError("boom")

        out = run_async(storage.fetch_cached("fc5", "k", _fetch_raise, fresh_minutes=0, stale_minutes=999999, format="json"))
        assert out == {"w": 7}, "stale path with raising fetch should return cached stale: {}".format(out)

        # fetch_cached: hard miss + fetch_fn RAISES → return None, do not propagate
        out = run_async(storage.fetch_cached("fc6", "missing", _fetch_raise, fresh_minutes=30, stale_minutes=35, format="json"))
        assert out is None, "hard miss with raising fetch should return None: {}".format(out)

        # save_debug_copy() writes a plain file straight into config_root/debug/, not cache/ (#4720)
        assert run_async(storage.save_debug_copy("predbat_debug_20260101-000000.yaml", "raw: text\n")) is True
        debug_dir = os.path.join(tmpdir, "debug")
        debug_copy_path = os.path.join(debug_dir, "predbat_debug_20260101-000000.yaml")
        assert os.path.exists(debug_copy_path), "save_debug_copy should create config_root/debug/<filename>"
        with open(debug_copy_path, "r") as f:
            assert f.read() == "raw: text\n", "debug copy should hold the text verbatim, with no format envelope"
        assert not os.path.exists(os.path.join(debug_dir, "predbat_debug_20260101-000000.meta")), "debug copy should have no metadata sidecar"

        # load_debug_copy() reads it straight back, verbatim
        assert run_async(storage.load_debug_copy("predbat_debug_20260101-000000.yaml")) == "raw: text\n"

        # load_debug_copy() on a file that was never written returns None rather than raising
        assert run_async(storage.load_debug_copy("never_written.yaml")) is None

        # delete_debug_copy() removes it; a second call on an already-missing file must not raise
        run_async(storage.delete_debug_copy("predbat_debug_20260101-000000.yaml"))
        assert not os.path.exists(debug_copy_path), "delete_debug_copy should remove the file"
        run_async(storage.delete_debug_copy("predbat_debug_20260101-000000.yaml"))
        assert run_async(storage.load_debug_copy("predbat_debug_20260101-000000.yaml")) is None, "a deleted debug copy should no longer load"

        # a path-separator-bearing filename is confined to debug/, not written outside it
        run_async(storage.save_debug_copy("../escape.yaml", "x"))
        assert os.path.exists(os.path.join(debug_dir, "escape.yaml")), "save_debug_copy should strip any directory component from filename"
        assert not os.path.exists(os.path.join(tmpdir, "escape.yaml")), "save_debug_copy must not write outside config_root/debug/"

        print("All storage tests passed!")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
