# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

import os
import yaml
import tempfile
from hass import Hass


def test_secrets_loading():
    """
    Test secrets loading mechanism
    """
    print("**** Running test_secrets_loading ****")

    # Test 1: No secrets file - should work without error
    print("  Test 1: No secrets file")
    if os.path.exists("secrets.yaml"):
        os.remove("secrets.yaml")
    if os.path.exists("/config/secrets.yaml"):
        os.remove("/config/secrets.yaml")

    h = Hass()
    assert h.secrets == {}, "Expected empty secrets dict"
    print("    PASS - No secrets file handled correctly")

    # Test 2: Secrets file in current directory
    print("  Test 2: Secrets file in current directory")
    secrets_data = {"api_key": "test_api_key_123", "password": "test_password_456"}
    with open("secrets.yaml", "w") as f:
        yaml.dump(secrets_data, f)

    h = Hass()
    assert h.secrets == secrets_data, f"Expected {secrets_data}, got {h.secrets}"
    os.remove("secrets.yaml")
    print("    PASS - Secrets loaded from current directory")

    # Test 3: Secrets file from PREDBAT_SECRETS_FILE env var
    print("  Test 3: Secrets file from PREDBAT_SECRETS_FILE")
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        temp_secrets_file = f.name
        yaml.dump(secrets_data, f)

    os.environ["PREDBAT_SECRETS_FILE"] = temp_secrets_file
    h = Hass()
    assert h.secrets == secrets_data, f"Expected {secrets_data}, got {h.secrets}"
    del os.environ["PREDBAT_SECRETS_FILE"]
    os.remove(temp_secrets_file)
    print("    PASS - Secrets loaded from PREDBAT_SECRETS_FILE")

    # Test 4: Test !secret tag in apps.yaml
    print("  Test 4: Test !secret tag resolution")
    secrets_data = {"test_api_key": "secret_value_789", "test_username": "secret_user"}
    with open("secrets.yaml", "w") as f:
        yaml.dump(secrets_data, f)

    # Create a test apps.yaml with !secret tags
    test_config = {"pred_bat": {"module": "predbat", "class": "PredBat", "api_key": "!secret test_api_key", "username": "!secret test_username"}}

    # Write YAML with !secret tags (manually to preserve the tag)
    with open("test_apps.yaml", "w") as f:
        f.write("pred_bat:\n")
        f.write("  module: predbat\n")
        f.write("  class: PredBat\n")
        f.write("  api_key: !secret test_api_key\n")
        f.write("  username: !secret test_username\n")

    os.environ["PREDBAT_APPS_FILE"] = "test_apps.yaml"
    h = Hass()
    assert h.args.get("api_key") == "secret_value_789", f"Expected 'secret_value_789', got {h.args.get('api_key')}"
    assert h.args.get("username") == "secret_user", f"Expected 'secret_user', got {h.args.get('username')}"

    del os.environ["PREDBAT_APPS_FILE"]
    os.remove("test_apps.yaml")
    os.remove("secrets.yaml")
    print("    PASS - !secret tags resolved correctly")

    # Test 5: Missing secret key should return None and warn
    print("  Test 5: Missing secret key handling")
    secrets_data = {"existing_key": "value"}
    with open("secrets.yaml", "w") as f:
        yaml.dump(secrets_data, f)

    with open("test_apps.yaml", "w") as f:
        f.write("pred_bat:\n")
        f.write("  module: predbat\n")
        f.write("  class: PredBat\n")
        f.write("  missing_key: !secret non_existent_key\n")

    os.environ["PREDBAT_APPS_FILE"] = "test_apps.yaml"
    h = Hass()
    assert h.args.get("missing_key") is None, f"Expected None for missing secret, got {h.args.get('missing_key')}"
    print("    PASS - Missing secret key returns None and warns correctly")
    del os.environ["PREDBAT_APPS_FILE"]
    os.remove("test_apps.yaml")
    os.remove("secrets.yaml")

    print("**** test_secrets_loading PASSED ****")
    return False  # False = success in Predbat test framework


def run_secrets_tests(my_predbat=None):
    """
    Run all secrets tests
    """
    return test_secrets_loading()
