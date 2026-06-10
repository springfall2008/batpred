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

        print("All storage tests passed!")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
