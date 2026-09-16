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
from utils import load_apps_yaml


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


def test_mask_secret_yaml_text():
    """The apps.yaml file download is redacted without being rewritten.

    /debug_apps serves the file as the user wrote it, and it sits next to the live download as
    the file people attach to bug reports. Redacting the parsed args would hand back a
    regenerated document with the comments stripped, so this redacts the text instead: comments,
    ordering and quoting survive, credential values do not.

    Mutation check: dropping the TaggedScalar guard redacts '!secret' references too, and
    removing the is_secret_key() call leaves every credential in place - both fail below.
    """
    from utils import mask_secret_yaml_text

    failed = False
    print("**** Testing apps.yaml text redaction ****")

    source = """# My Predbat config
pred_bat:
  # Octopus settings
  octopus_api_key: 'REAL-OCTOPUS-KEY'
  octopus_api_account: A-REAL-ACCOUNT
  ha_key: !secret ha_token
  battery_size: 9.5
  solis_inverter_sn: SN-VISIBLE
  forecast_solar:
    - postcode: SW1A 1AA
      api_key: REAL-NESTED-KEY
"""
    masked = mask_secret_yaml_text(source)

    for secret in ("REAL-OCTOPUS-KEY", "A-REAL-ACCOUNT", "REAL-NESTED-KEY"):
        if secret in masked:
            print("ERROR: {} survived text redaction:\n{}".format(secret, masked))
            failed = True

    # A '!secret' reference names a credential, it does not contain one - and which secret a key
    # resolves to is exactly what you need when an integration will not authenticate.
    if "!secret ha_token" not in masked:
        print("ERROR: a '!secret' reference was redacted or rewritten:\n{}".format(masked))
        failed = True

    # The point of redacting the text rather than the args: it still reads like their own file.
    if "# My Predbat config" not in masked or "# Octopus settings" not in masked:
        print("ERROR: comments were lost, so the download no longer matches the user's file:\n{}".format(masked))
        failed = True

    if "9.5" not in masked or "SN-VISIBLE" not in masked or "SW1A 1AA" not in masked:
        print("ERROR: redaction damaged values that must stay readable:\n{}".format(masked))
        failed = True

    # Unparseable input must raise, so the route fails closed rather than serving raw text.
    try:
        mask_secret_yaml_text("pred_bat:\n  key: [unclosed\n")
        print("ERROR: invalid YAML was accepted instead of raising, so the route could serve it unredacted")
        failed = True
    except Exception:
        pass

    if not failed:
        print("**** test_mask_secret_yaml_text PASSED ****")
    return failed


def run_secrets_tests(my_predbat=None):
    """
    Run all secrets tests
    """
    failed = test_secrets_loading()
    failed |= test_mask_secret_yaml_text()
    failed |= test_load_apps_yaml_resolves_secrets()
    return failed


def test_load_apps_yaml_resolves_secrets():
    """
    Test load_apps_yaml reads a given apps.yaml and resolves its !secret references

    fox.py's --config option loads credentials through this rather than carrying its own parser,
    so !secret keeps working for a standalone CLI run exactly as it does for Predbat itself.
    """
    print("**** Running test_load_apps_yaml_resolves_secrets ****")
    failed = False

    saved_secrets_env = os.environ.get("PREDBAT_SECRETS_FILE")
    with tempfile.TemporaryDirectory() as tmpdir:
        secrets_path = os.path.join(tmpdir, "secrets.yaml")
        with open(secrets_path, "w") as handle:
            yaml.dump({"fox_api_key": "secret-fox-key"}, handle)

        apps_path = os.path.join(tmpdir, "apps.yaml")
        with open(apps_path, "w") as handle:
            handle.write("pred_bat:\n  fox_key: !secret fox_api_key\n  fox_automatic: True\n  num_inverters: 1\n")

        os.environ["PREDBAT_SECRETS_FILE"] = secrets_path
        try:
            args, secrets = load_apps_yaml(apps_path)
        finally:
            if saved_secrets_env is None:
                os.environ.pop("PREDBAT_SECRETS_FILE", None)
            else:
                os.environ["PREDBAT_SECRETS_FILE"] = saved_secrets_env

    if args.get("fox_key") != "secret-fox-key":
        print("ERROR: expected the !secret reference to resolve, got {}".format(args.get("fox_key")))
        failed = True
    if args.get("fox_automatic") is not True:
        print("ERROR: expected fox_automatic True, got {}".format(args.get("fox_automatic")))
        failed = True
    if secrets.get("fox_api_key") != "secret-fox-key":
        print("ERROR: expected the secrets dict to be returned, got {}".format(secrets))
        failed = True

    if not failed:
        print("**** test_load_apps_yaml_resolves_secrets PASSED ****")
    return failed
