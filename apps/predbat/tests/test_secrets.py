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

    # Test 6: secrets.yaml that is valid YAML but the wrong shape (a bare scalar/list, not a
    # mapping) must not crash Predbat's startup entirely. yaml.safe_load() raises nothing for
    # this - it is valid YAML - so without an explicit shape check, load_secrets() would
    # reassign its local `secrets` to that scalar/list right before the very next line's
    # secrets.get("logger") call throws AttributeError. The generic except then logs the crash,
    # but the reassignment had already happened and is never undone, so load_secrets() still
    # returned the malformed value - and the next log() call would reach
    # collect_log_secret_values()'s secrets.items(), raising unhandled and aborting startup
    # entirely (#5053 review).
    print("  Test 6: Malformed secrets.yaml (bare scalar, not a mapping)")
    with open("secrets.yaml", "w") as f:
        f.write("just_a_plain_string_not_a_mapping\n")
    with open("test_apps.yaml", "w") as f:
        f.write("pred_bat:\n")
        f.write("  module: predbat\n")
        f.write("  class: PredBat\n")

    os.environ["PREDBAT_APPS_FILE"] = "test_apps.yaml"
    try:
        h = Hass()
        assert h.secrets == {}, f"Expected malformed secrets.yaml to degrade to {{}}, got {h.secrets}"
        # The crash this guards against happens on the NEXT log() call after load_secrets()
        # returns, not inside load_secrets() itself - so a log() call here is the real assertion.
        h.log("Info: Predbat started despite the malformed secrets.yaml")
    finally:
        del os.environ["PREDBAT_APPS_FILE"]
        os.remove("test_apps.yaml")
        os.remove("secrets.yaml")
        if os.path.exists("predbat.log"):
            os.remove("predbat.log")
    print("    PASS - Malformed secrets.yaml degrades to {} instead of crashing startup")

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


def test_collect_log_secret_values():
    """collect_log_secret_values() gathers {value: label} from args, secrets.yaml, the user's
    bare redact_strings denylist and the labelled redact_strings_labelled mapping, from nested
    structures, and leaves non-credential values out (GH#4770)."""
    from utils import collect_log_secret_values

    failed = False
    print("**** Testing collect_log_secret_values ****")

    args = {
        "octopus_api_key": "REAL-OCTOPUS-KEY",
        "octopus_api_account": "A-REAL-ACCOUNT",  # registry-flagged (account number), not name-matched
        "battery_size": 9.5,  # not a credential - must not be collected
        "forecast_solar": [{"api_key": "REAL-NESTED-KEY"}],  # nested one level down
    }
    secrets = {"ha_token": "REAL-SECRETS-YAML-VALUE"}
    redact_strings = ["THIRD-PARTY-MPAN-1234567890123"]
    redact_strings_labelled = {"my_landlords_mpan": "LANDLORD-MPAN-9876543210987"}

    found = collect_log_secret_values(args, secrets, redact_strings, redact_strings_labelled)

    expected_labels = {
        "REAL-OCTOPUS-KEY": "octopus_api_key",
        "A-REAL-ACCOUNT": "octopus_api_account",
        "REAL-NESTED-KEY": "forecast_solar.api_key",
        "REAL-SECRETS-YAML-VALUE": "ha_token",
        "THIRD-PARTY-MPAN-1234567890123": "redact_strings",
        "LANDLORD-MPAN-9876543210987": "my_landlords_mpan",
    }
    for value, label in expected_labels.items():
        if value not in found:
            print("ERROR: {} missing from collected secret values: {}".format(value, found))
            failed = True
        elif found[value] != label:
            print("ERROR: {} labelled {}, expected {}".format(value, found[value], label))
            failed = True

    if 9.5 in found or "9.5" in found:
        print("ERROR: a non-credential value was collected: {}".format(found))
        failed = True

    # Short values are dropped from args/secrets - a 1-2 char "secret" picked up by the key-name
    # heuristic would false-positive-redact ordinary text constantly.
    if "x" in collect_log_secret_values({"password": "x"}, {}):
        print("ERROR: a short value was collected despite the length floor")
        failed = True

    # The floor does NOT apply to redact_strings/redact_strings_labelled - those are the user's
    # own deliberate denylist entries, not an automatic key-name match, so a short one must still
    # be redacted (Copilot review on #5053: PR originally applied the floor everywhere).
    short_found = collect_log_secret_values({}, {}, ["ab"], {"short_pin": "99"})
    if "ab" not in short_found or short_found["ab"] != "redact_strings":
        print("ERROR: a short redact_strings entry should still be collected, got {}".format(short_found))
        failed = True
    if "99" not in short_found or short_found["99"] != "short_pin":
        print("ERROR: a short redact_strings_labelled entry should still be collected, got {}".format(short_found))
        failed = True

    # A secret-flagged key holding a LIST (teslemetry_site_id, sigenergy_system_id are
    # "string|string_list") must collect each element individually, not drop the whole list
    # (Copilot review on #5053).
    list_found = collect_log_secret_values({"teslemetry_site_id": ["SITE-VALUE-ONE-123456", "SITE-VALUE-TWO-654321"]}, {})
    for site_value in ("SITE-VALUE-ONE-123456", "SITE-VALUE-TWO-654321"):
        if site_value not in list_found or list_found[site_value] != "teslemetry_site_id":
            print("ERROR: {} from a secret-flagged list key should be collected and labelled teslemetry_site_id, got {}".format(site_value, list_found))
            failed = True

    # A malformed redact_strings_labelled (the shape APPS_SCHEMA's own validator rejects) must
    # degrade to "nothing from this source", not crash - log() runs before validation has had a
    # chance to run at all (Copilot review on #5053: AttributeError on .items() froze startup).
    try:
        malformed_found = collect_log_secret_values({}, {}, "not_a_list", "not_a_dict")
    except (AttributeError, TypeError) as e:
        print("ERROR: malformed redact_strings/redact_strings_labelled crashed instead of degrading: {}".format(e))
        failed = True
        malformed_found = {}
    if malformed_found:
        print("ERROR: malformed redact_strings/redact_strings_labelled should collect nothing, got {}".format(malformed_found))
        failed = True

    # Missing/None args, secrets, redact_strings and redact_strings_labelled must not raise -
    # log() calls this on every line, including ones written before apps.yaml/secrets.yaml load.
    if collect_log_secret_values(None, None, None, None) != {}:
        print("ERROR: collect_log_secret_values(None, None, None) should return an empty dict")
        failed = True

    # An unquoted numeric MPAN/account ID loads from YAML as an int, not a str - both
    # secrets.yaml entries and redact_strings_labelled values must be coerced to string and
    # collected rather than silently dropped by an isinstance(value, str) check, since log()
    # serializes every message with str(msg) and the value would otherwise reach the log in the
    # clear (#5053 review).
    numeric_found = collect_log_secret_values({}, {"landlord_mpan": 1234567890123}, None, {"my_mpan": 9876543210987})
    if "1234567890123" not in numeric_found or numeric_found["1234567890123"] != "landlord_mpan":
        print("ERROR: a numeric secrets.yaml value should be collected as a string, got {}".format(numeric_found))
        failed = True
    if "9876543210987" not in numeric_found or numeric_found["9876543210987"] != "my_mpan":
        print("ERROR: a numeric redact_strings_labelled value should be collected as a string, got {}".format(numeric_found))
        failed = True

    # redact_strings is itself secret-flagged (SECRET_KEY_EXPLICIT_NAMES) so mask_secret_args()'s
    # debug-dump masking hides it wholesale - but that same flag made the generic args traversal
    # in _collect_secret_values() pick up redact_strings as an ordinary secret-valued key too,
    # labelling its values generically as "redact_strings" BEFORE the dedicated
    # redact_strings_labelled pass ran. For a value present in both denylist forms, that broke
    # the documented priority (a specific label should win): the generic label from the args pass
    # was already in `found` by the time the more specific pass got to it (#5053 review).
    priority_found = collect_log_secret_values({"redact_strings": ["SHARED-DENYLIST-VALUE-123456"]}, {}, ["SHARED-DENYLIST-VALUE-123456"], {"my_landlords_mpan": "SHARED-DENYLIST-VALUE-123456"})
    if priority_found.get("SHARED-DENYLIST-VALUE-123456") != "my_landlords_mpan":
        print("ERROR: a value present in both redact_strings and redact_strings_labelled should keep the more specific label, got {}".format(priority_found))
        failed = True

    # A key merely named "redact_strings" nested somewhere other than the true top level is not
    # this denylist and must still be collected normally, labelled by its own key/prefix.
    nested_found = collect_log_secret_values({"some_component": {"redact_strings": "NESTED-VALUE-123456"}}, {})
    if nested_found.get("NESTED-VALUE-123456") != "some_component.redact_strings":
        print("ERROR: a nested (non-top-level) key merely named redact_strings should still be collected normally, got {}".format(nested_found))
        failed = True

    # A bool is technically an int subclass in Python - a stray "some_flag: true" secrets.yaml
    # entry must not be coerced to the string "True" and start matching that word everywhere.
    bool_found = collect_log_secret_values({}, {"some_flag": True})
    if bool_found:
        print("ERROR: a boolean secrets.yaml value should not be collected, got {}".format(bool_found))
        failed = True

    # An unquoted numeric MPAN in the bare redact_strings list form (not just the labelled dict
    # form) loads from YAML as an int and must still be collected, coerced to string - the same
    # gap as the labelled form, just missed on the first pass (#5053 review).
    numeric_bare_found = collect_log_secret_values({}, {}, [1234567890123], None)
    if "1234567890123" not in numeric_bare_found or numeric_bare_found["1234567890123"] != "redact_strings":
        print("ERROR: a numeric redact_strings (bare list) value should be collected as a string, got {}".format(numeric_bare_found))
        failed = True

    # A non-string dict key (an integer key in a nested mapping, reached at the true top level of
    # args) must not crash the traversal - log() runs on the very first startup line, before
    # validation has had any chance to report the malformed input (#5053 review).
    try:
        non_string_key_found = collect_log_secret_values({123: "some_value"}, {})
    except AttributeError as e:
        print("ERROR: a non-string dict key crashed the traversal instead of being tolerated: {}".format(e))
        failed = True
        non_string_key_found = {}
    if non_string_key_found:
        print("ERROR: a non-secret-flagged non-string key should not itself be collected, got {}".format(non_string_key_found))
        failed = True

    if not failed:
        print("**** test_collect_log_secret_values PASSED ****")
    return failed


def test_compile_log_secret_pattern_and_redact_log_line():
    """compile_log_secret_pattern() + redact_log_line() replace every known secret value in a
    log line with a labelled mask, e.g. "<octopus_api_key>", and leave everything else
    untouched (GH#4770). The label identifies which credential was found without exposing it.

    Regex-special characters in a secret value (a plausible API key shape: '+', '/', '=' are all
    valid base64 alphabet) must be escaped before compiling, or a value containing one either
    fails to match its own literal text or matches unrelated text the value never appears in.
    """
    from utils import compile_log_secret_pattern, redact_log_line

    failed = False
    print("**** Testing compile_log_secret_pattern + redact_log_line ****")

    found = {"REAL-KEY-VALUE": "octopus_api_key", "REAL-PASSWORD-VALUE": "ha_password", "REAL+KEY/WITH=SPECIALS": "gateway_mqtt_token"}
    pattern = compile_log_secret_pattern(found)

    redacted = redact_log_line("OctopusAPI: using REAL-KEY-VALUE for auth, retry with REAL-PASSWORD-VALUE", pattern)
    if "REAL-KEY-VALUE" in redacted or "REAL-PASSWORD-VALUE" in redacted:
        print("ERROR: a known secret value survived redact_log_line: {}".format(redacted))
        failed = True
    if "<octopus_api_key>" not in redacted or "<ha_password>" not in redacted:
        print("ERROR: redacted text is missing its credential label: {}".format(redacted))
        failed = True

    regex_special = redact_log_line("Warn: auth failed with REAL+KEY/WITH=SPECIALS", pattern)
    if "REAL+KEY/WITH=SPECIALS" in regex_special:
        print("ERROR: a secret value containing regex-special characters survived redaction: {}".format(regex_special))
        failed = True
    if "<gateway_mqtt_token>" not in regex_special:
        print("ERROR: the regex-special-character value was not labelled correctly: {}".format(regex_special))
        failed = True

    # A longer value that shares a prefix with a shorter one must match in full - the shorter
    # value pre-empting the match would leave the rest of the longer one exposed in the line.
    overlap_found = {"sk_live_abcdef": "short_key", "sk_live_abcdef_extended_token": "long_token"}
    overlap_pattern = compile_log_secret_pattern(overlap_found)
    overlap_redacted = redact_log_line("Info: using sk_live_abcdef_extended_token for the call", overlap_pattern)
    if "sk_live_abcdef_extended_token" in overlap_redacted or "_extended_token" in overlap_redacted:
        print("ERROR: the shorter value pre-empted the longer one, leaving part of it exposed: {}".format(overlap_redacted))
        failed = True
    if "<long_token>" not in overlap_redacted:
        print("ERROR: the longer, more specific value should have matched: {}".format(overlap_redacted))
        failed = True

    # Two secrets can overlap starting at DIFFERENT positions, not just share a common prefix -
    # e.g. "sec1" and "c123x" both appear in "sec123x". Sorting alternatives longest-first only
    # helps when both could match at the same starting position; pattern.sub() still finds "sec1"
    # first (the only one that CAN match at position 0) and resumes scanning after it, so
    # "c123x" starting at position 1 is never considered and "23x" leaks in the clear (#5053
    # review, confirmed leaking before this fix).
    diff_position_found = {"sec1": "short_secret", "c123x": "long_secret"}
    diff_position_pattern = compile_log_secret_pattern(diff_position_found)
    diff_position_redacted = redact_log_line("the value is sec123x here", diff_position_pattern)
    if "23x" in diff_position_redacted:
        print("ERROR: part of a longer secret overlapping a shorter one at a different start position leaked: {}".format(diff_position_redacted))
        failed = True
    if "sec1" in diff_position_redacted or "c123x" in diff_position_redacted:
        print("ERROR: an overlapping secret value survived redaction: {}".format(diff_position_redacted))
        failed = True
    # The merged span itself is not a key in `labels` (it is neither secret value alone), so a
    # naive labels.get(merged_value, SECRET_MASK) falls back to the generic mask - losing the
    # "which credential" guarantee a labelled mask exists to provide, even though nothing leaks
    # (Copilot review on #5053). Both contributing labels must survive, joined together.
    if "<short_secret+long_secret>" not in diff_position_redacted:
        print("ERROR: an overlapping match should keep both labels, got {}".format(diff_position_redacted))
        failed = True

    # A three-way overlap chain (v1/1v2/v2345 all present in "v1v2345") must extend through
    # every overlap, not just the first one found - and keep every contributing label.
    chain_found = {"v1": "l1", "1v2": "l2", "v2345": "l3"}
    chain_pattern = compile_log_secret_pattern(chain_found)
    chain_redacted = redact_log_line("value v1v2345 end", chain_pattern)
    if "v1v2345" in chain_redacted or "2345" in chain_redacted or "v2345" in chain_redacted:
        print("ERROR: a chained overlap was not fully covered: {}".format(chain_redacted))
        failed = True
    if "<l1+l2+l3>" not in chain_redacted:
        print("ERROR: a chained overlap should keep every contributing label, got {}".format(chain_redacted))
        failed = True

    # A line with nothing secret in it must come back byte-identical.
    clean_line = "Info: battery_size is 9.5 today"
    if redact_log_line(clean_line, pattern) != clean_line:
        print("ERROR: a clean line was altered: {}".format(redact_log_line(clean_line, pattern)))
        failed = True

    # An empty value dict compiles to None (nothing to redact) - must be a no-op, not an error.
    empty_pattern = compile_log_secret_pattern({})
    if empty_pattern is not None:
        print("ERROR: compile_log_secret_pattern({{}}) should return None, got {}".format(empty_pattern))
        failed = True
    if redact_log_line(clean_line, empty_pattern) != clean_line:
        print("ERROR: an empty pattern altered the line")
        failed = True
    if redact_log_line(clean_line, None) != clean_line:
        print("ERROR: redact_log_line(line, None) should be a no-op")
        failed = True

    if not failed:
        print("**** test_compile_log_secret_pattern_and_redact_log_line PASSED ****")
    return failed


def test_log_redacts_at_write_time():
    """Hass.log() must redact a known secret value BEFORE it reaches predbat.log on disk, not
    only when the log is later served over HTTP/MCP.

    Some users copy predbat.log directly off a Samba share exposing the addon's config
    directory, bypassing every download/serve endpoint entirely - a scrub applied only at those
    endpoints would leave the on-disk file itself holding the plaintext value (GH#4770).
    """
    failed = False
    print("**** Testing Hass.log() redacts secrets at write time ****")

    secrets_data = {"my_octopus_key": "REAL-WRITE-TIME-SECRET"}
    with open("secrets.yaml", "w") as f:
        yaml.dump(secrets_data, f)
    with open("test_apps.yaml", "w") as f:
        f.write("pred_bat:\n")
        f.write("  module: predbat\n")
        f.write("  class: PredBat\n")
        f.write("  octopus_api_key: !secret my_octopus_key\n")
        f.write("  ordinary_setting: not_a_secret\n")
        f.write("  redact_strings:\n")
        f.write("    - THIRD-PARTY-MPAN-1234567890123\n")
        f.write("  redact_strings_labelled:\n")
        f.write("    my_landlords_mpan: LANDLORD-MPAN-9876543210987\n")

    # Save/restore rather than an unconditional del: the suite runs every registered test in one
    # shared process, so blindly deleting a var this test did not itself set would drop a value a
    # caller or another test left in place, rather than restoring the environment it found.
    had_apps_file = "PREDBAT_APPS_FILE" in os.environ
    saved_apps_file = os.environ.get("PREDBAT_APPS_FILE")
    os.environ["PREDBAT_APPS_FILE"] = "test_apps.yaml"
    try:
        h = Hass()
        h.log("Info: connecting with REAL-WRITE-TIME-SECRET to Octopus", quiet=False)
        h.log("Info: ordinary_setting is not_a_secret today", quiet=False)
        h.log("Warn: sensor exposed THIRD-PARTY-MPAN-1234567890123 in its state", quiet=False)
        h.log("Warn: sensor exposed LANDLORD-MPAN-9876543210987 in its state", quiet=False)
        h.logfile.close()

        with open("predbat.log") as f:
            content = f.read()

        if "REAL-WRITE-TIME-SECRET" in content:
            print("ERROR: secret value present in predbat.log on disk:\n{}".format(content))
            failed = True
        if "THIRD-PARTY-MPAN-1234567890123" in content:
            print("ERROR: a redact_strings entry (user denylist, GH#4770) survived to disk:\n{}".format(content))
            failed = True
        if "LANDLORD-MPAN-9876543210987" in content:
            print("ERROR: a redact_strings_labelled entry survived to disk:\n{}".format(content))
            failed = True
        # Labelled by the secrets.yaml key (my_octopus_key), not the apps.yaml key
        # (octopus_api_key): secrets.yaml is checked first in collect_log_secret_values(), and
        # this value was resolved through a !secret reference so it is found there.
        if "<my_octopus_key>" not in content:
            print("ERROR: the write-time redaction lost its credential label:\n{}".format(content))
            failed = True
        if "<redact_strings>" not in content:
            print("ERROR: the redact_strings entry was not labelled as such:\n{}".format(content))
            failed = True
        if "<my_landlords_mpan>" not in content:
            print("ERROR: the redact_strings_labelled entry did not get the user's own label:\n{}".format(content))
            failed = True
        if "not_a_secret" not in content:
            print("ERROR: an ordinary log line was altered/lost:\n{}".format(content))
            failed = True
    finally:
        if had_apps_file:
            os.environ["PREDBAT_APPS_FILE"] = saved_apps_file
        else:
            del os.environ["PREDBAT_APPS_FILE"]
        for name in ("test_apps.yaml", "secrets.yaml", "predbat.log"):
            if os.path.exists(name):
                os.remove(name)

    if not failed:
        print("**** test_log_redacts_at_write_time PASSED ****")
    return failed


def test_redact_strings_masked_in_debug_dump():
    """redact_strings/redact_strings_labelled are themselves the user's lists of values to
    redact (GH#4770), so mask_secret_args() - what create_debug_yaml() applies to args before
    writing a debug yaml - must mask both wholesale rather than leaving their contents (the
    sensitive values, and for the labelled form the user's own possibly-revealing label names
    too) sitting in the clear next to them."""
    from utils import mask_secret_args

    failed = False
    print("**** Testing redact_strings/redact_strings_labelled are masked in a debug dump ****")

    args = {
        "redact_strings": ["THIRD-PARTY-MPAN-1234567890123"],
        "redact_strings_labelled": {"my_landlords_mpan": "LANDLORD-MPAN-9876543210987"},
        "ordinary_setting": "keep_me",
    }
    masked = mask_secret_args(args)

    if masked["redact_strings"] != "xxx":
        print("ERROR: redact_strings should be masked wholesale, got {}".format(masked["redact_strings"]))
        failed = True
    if masked["redact_strings_labelled"] != "xxx":
        print("ERROR: redact_strings_labelled should be masked wholesale, got {}".format(masked["redact_strings_labelled"]))
        failed = True
    if masked["ordinary_setting"] != "keep_me":
        print("ERROR: an unrelated key was altered by masking redact_strings: {}".format(masked))
        failed = True
    if args["redact_strings"] != ["THIRD-PARTY-MPAN-1234567890123"] or args["redact_strings_labelled"] != {"my_landlords_mpan": "LANDLORD-MPAN-9876543210987"}:
        print("ERROR: mask_secret_args must not mutate its input")
        failed = True

    if not failed:
        print("**** test_redact_strings_masked_in_debug_dump PASSED ****")
    return failed


def test_set_arg_invalidates_log_secret_cache(my_predbat):
    """set_arg() must invalidate log()'s cached redaction pattern (Copilot review on #5053).

    The pattern is cached on first use and only invalidated in Hass.__init__ - but self.args is
    mutated after startup too, via set_arg() and (separately, in web.py) the apps.yaml web
    editor's batch clear()/update(). A credential added or changed through either path would
    otherwise keep leaking into the log under the stale pre-change pattern until Predbat
    restarts. This covers set_arg(); the web.py batch path shares the same
    _log_secret_pattern_cache invalidation but isn't reachable from this test's fixture.
    """
    if my_predbat is None:
        return False
    print("**** Testing set_arg() invalidates the cached log redaction pattern ****")
    failed = False

    import io

    saved_args = my_predbat.args.copy()
    saved_logfile = my_predbat.logfile
    saved_cache = my_predbat._log_secret_pattern_cache
    try:
        my_predbat.args.pop("test_marker_secret_key_xyz", None)
        my_predbat._log_secret_pattern_cache = my_predbat._LOG_SECRET_PATTERN_UNSET
        my_predbat.logfile = io.StringIO()

        my_predbat.log("Info: nothing secret published yet")
        my_predbat.set_arg("test_marker_secret_key_xyz", "NEWLY-ADDED-SECRET-VALUE-123456")
        my_predbat.log("Info: now using NEWLY-ADDED-SECRET-VALUE-123456")

        content = my_predbat.logfile.getvalue()
        if "NEWLY-ADDED-SECRET-VALUE-123456" in content:
            print("ERROR: a secret added via set_arg() after the cache was built still leaked into the log: {}".format(content))
            failed = True
        if "<test_marker_secret_key_xyz>" not in content:
            print("ERROR: the newly-added secret was not redacted with a label: {}".format(content))
            failed = True
    finally:
        my_predbat.args.clear()
        my_predbat.args.update(saved_args)
        my_predbat.logfile = saved_logfile
        my_predbat._log_secret_pattern_cache = saved_cache

    if not failed:
        print("**** test_set_arg_invalidates_log_secret_cache PASSED ****")
    return failed


def test_log_secret_pattern_build_is_not_racy(my_predbat):
    """_log_secret_pattern() must not let a build started before an invalidation overwrite that
    invalidation once it finishes (#5053 review).

    log() runs from component threads as well as the main thread, so two threads can both
    observe the UNSET sentinel and race to rebuild. Without a lock around "read sentinel, build,
    store" as one atomic step, this interleaving is possible: thread A sees UNSET and starts
    building from the current (stale) args; thread B changes a credential and invalidates the
    cache; thread A finishes and stores its pattern - built from args that predate B's change -
    clobbering B's fresh invalidation. The credential B just added would then stay unredacted
    until something invalidates the cache again.

    Reproduced deterministically (not by timing) by pausing thread A's build midway with an Event
    and having the invalidation happen while it is paused, then resuming it.
    """
    import threading as _threading

    if my_predbat is None:
        return False
    print("**** Testing _log_secret_pattern() build is not racy against a concurrent invalidation ****")
    failed = False

    import hass as hass_module

    saved_cache = my_predbat._log_secret_pattern_cache
    saved_collect = hass_module.collect_log_secret_values
    build_started = _threading.Event()
    release_build = _threading.Event()

    def paused_collect(*args, **kwargs):
        build_started.set()
        release_build.wait(timeout=5)
        return saved_collect(*args, **kwargs)

    try:
        my_predbat._log_secret_pattern_cache = my_predbat._LOG_SECRET_PATTERN_UNSET
        hass_module.collect_log_secret_values = paused_collect

        builder = _threading.Thread(target=my_predbat._log_secret_pattern)
        builder.start()
        if not build_started.wait(timeout=5):
            print("ERROR: the build thread never reached collect_log_secret_values")
            failed = True

        # The build thread holds the lock while paused inside collect_log_secret_values(), so an
        # invalidation attempted right now would itself block on the same lock rather than run
        # concurrently - which is exactly the property under test: the lock turns "read sentinel,
        # build, store" into one atomic step, so an invalidation can only ever happen cleanly
        # before a build starts or after it finishes, never interleaved with it. Fire the
        # invalidation from a second thread and confirm it is still blocked while the build is
        # paused, then release the build and confirm the invalidation completes immediately after.
        invalidator = _threading.Thread(target=my_predbat._invalidate_log_secret_pattern)
        invalidator.start()
        invalidator.join(timeout=0.3)
        if not invalidator.is_alive():
            print("ERROR: the invalidation completed while the build thread was still paused mid-build - the lock did not serialize them")
            failed = True

        release_build.set()
        builder.join(timeout=5)
        invalidator.join(timeout=5)
        if invalidator.is_alive():
            print("ERROR: the invalidation never completed after the build was released")
            failed = True

        # The invalidation ran after the build finished and stored its pattern, so the cache must
        # end up UNSET (the invalidation's effect), not a stale pattern built from pre-change
        # args - that would be exactly the leak this fix closes.
        if my_predbat._log_secret_pattern_cache is not my_predbat._LOG_SECRET_PATTERN_UNSET:
            print("ERROR: a build that started before an invalidation was allowed to overwrite that invalidation's result")
            failed = True
    finally:
        hass_module.collect_log_secret_values = saved_collect
        my_predbat._log_secret_pattern_cache = saved_cache

    if not failed:
        print("**** test_log_secret_pattern_build_is_not_racy PASSED ****")
    return failed


def run_secrets_tests(my_predbat=None):
    """
    Run all secrets tests
    """
    failed = test_secrets_loading()
    failed |= test_mask_secret_yaml_text()
    failed |= test_collect_log_secret_values()
    failed |= test_compile_log_secret_pattern_and_redact_log_line()
    failed |= test_log_redacts_at_write_time()
    failed |= test_redact_strings_masked_in_debug_dump()
    failed |= test_set_arg_invalidates_log_secret_cache(my_predbat)
    failed |= test_log_secret_pattern_build_is_not_racy(my_predbat)
    return failed
