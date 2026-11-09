# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""
Keep the Solax Gen4+ Mode 8 setup docs naming the sensors the Mode 8 trigger depends on (issue #6059).

The Solax Modbus integration treats House Load Alt as a required input to its Mode 8 trigger and
sets the power control mode back to Disabled when it is invalid, cancelling every Predbat command.
House Load Alt ships disabled and can be left disabled (with Meter 2) after an integration update,
so the docs entity list and the template must tell users to keep both enabled.
"""

import os

REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
INVERTER_SETUP = os.path.join(REPO_ROOT, "docs", "inverter-setup.md")
SOLAX_TEMPLATE = os.path.join(REPO_ROOT, "templates", "solax_sx4.yaml")
MODE8_SENSORS = ["sensor.solax_house_load_alt", "sensor.solax_meter_2_measured_power"]
MODE8_LIST_HEADER = "To use Mode 8 power control, ensure the following entities are enabled"


def _mode8_entity_list(text):
    """Return the entity names listed under the Mode 8 'ensure the following entities are enabled' bullet."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if MODE8_LIST_HEADER in line:
            entities = []
            for item in lines[index + 1 :]:
                stripped = item.strip()
                if not stripped:
                    if entities:
                        break
                    continue
                if not stripped.startswith("- ") or not item.startswith(" "):
                    break
                entities.append(stripped[2:].strip())
            return entities
    return None


def test_docs_solax_mode8(my_predbat):
    """Check the Solax Mode 8 docs and template tell users to keep the Mode 8 trigger's sensors enabled."""
    failed = False
    print("**** Running docs Solax Mode 8 tests ****")

    # The parser itself: picks up the indented list and stops at the next top-level bullet
    sample = "- {}:\n\n    - button.a\n    - sensor.b\n\n- Next bullet\n".format(MODE8_LIST_HEADER)
    if _mode8_entity_list(sample) != ["button.a", "sensor.b"]:
        print("ERROR: Mode 8 entity list parser returned {}".format(_mode8_entity_list(sample)))
        failed = True

    with open(INVERTER_SETUP, "r", encoding="utf-8") as handle:
        docs_text = handle.read()
    entities = _mode8_entity_list(docs_text)
    if not entities:
        print("ERROR: docs/inverter-setup.md has no Mode 8 entity list")
        failed = True
    else:
        for sensor in MODE8_SENSORS:
            if sensor not in entities:
                print("ERROR: docs/inverter-setup.md Mode 8 entity list is missing {}".format(sensor))
                failed = True
    if "house_load_alt" not in docs_text or "powercontrolmode8_trigger because a required control input is invalid" not in docs_text:
        print("ERROR: docs/inverter-setup.md does not explain the House Load Alt Mode 8 dependency and its log message")
        failed = True

    with open(SOLAX_TEMPLATE, "r", encoding="utf-8") as handle:
        template_text = handle.read()
    for sensor in MODE8_SENSORS:
        if sensor not in template_text:
            print("ERROR: templates/solax_sx4.yaml Mode 8 note is missing {}".format(sensor))
            failed = True

    if failed:
        print("ERROR: test_docs_solax_mode8 FAILED")
    else:
        print("test_docs_solax_mode8 PASSED")
    return failed
