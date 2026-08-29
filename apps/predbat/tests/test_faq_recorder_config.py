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
Keep the recorder filter example in docs/faq.md consistent with the entities Predbat reads history
for (issue #4583).

Users copy that example into configuration.yaml verbatim, so any Predbat entity it stops Home
Assistant recording is an entity Predbat can no longer read the history of - which silently breaks
whatever feature needed it. In #4583 the reporter's Plan History and Yesterday Without Predbat
views were empty and PV calibration was disabled on every cycle, both because the recorder wasn't
storing the predbat.* entities those features read.
"""

import os
import re

import yaml

REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
SOURCE_DIR = os.path.join(REPO_ROOT, "apps", "predbat")
FAQ_PATH = os.path.join(REPO_ROOT, "docs", "faq.md")

PREFIX = "predbat"

# Entities reached through a local variable rather than written inline at the history call, so the
# source scan below can't see them - the ML load model builds these two from self.prefix before
# handing them to minute_data_import_export()
INDIRECT_HISTORY_ENTITIES = ["{}.rates".format(PREFIX), "{}.rates_export".format(PREFIX)]

# First argument of a get_history_wrapper() call, i.e. the entity being asked for
HISTORY_CALL_RE = re.compile(r"get_history_wrapper\(\s*(?:entity_id\s*=\s*)?([^,)]+)")
# Predbat's own entities, written as self.prefix + ".name" / "sensor." + self.prefix + "_name"
OWN_DOMAIN_RE = re.compile(r'self\.prefix\s*\+\s*"\.([a-z0-9_]+)"')
OWN_SENSOR_RE = re.compile(r'"sensor\."\s*\+\s*self\.prefix\s*\+\s*"_([a-z0-9_]+)"')


def _load_recorder_example():
    """Return the recorder filter example from the FAQ as a dict, or None if it isn't there."""
    with open(FAQ_PATH, "r") as handle:
        text = handle.read()
    for block in re.findall(r"```yaml\n(.*?)```", text, re.S):
        parsed = yaml.safe_load(block)
        if isinstance(parsed, dict) and "recorder" in parsed:
            return parsed["recorder"] or {}
    return None


def _entities_read_from_history():
    """Return the set of Predbat owned entities whose history the Predbat source reads."""
    entities = set(INDIRECT_HISTORY_ENTITIES)
    for name in sorted(os.listdir(SOURCE_DIR)):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(SOURCE_DIR, name), "r") as handle:
            source = handle.read()
        for argument in HISTORY_CALL_RE.findall(source):
            for match in OWN_DOMAIN_RE.findall(argument):
                entities.add("{}.{}".format(PREFIX, match))
            for match in OWN_SENSOR_RE.findall(argument):
                entities.add("sensor.{}_{}".format(PREFIX, match))
    return entities


def _is_recorded(entity_id, recorder):
    """Return True if Home Assistant would record this entity under the given filter.

    Mirrors the "exclude domains, include entities" case of Home Assistant's own recorder entity
    filter: an entity in an excluded domain is recorded only when it is named in include.entities,
    and an entity in any other domain is recorded unless it is named in exclude.entities. Note that
    include.entities does *not* rescue an entity that exclude.entities names in a domain which
    isn't itself excluded.
    """
    exclude = recorder.get("exclude", {}) or {}
    include = recorder.get("include", {}) or {}
    exclude_domains = exclude.get("domains", []) or []
    exclude_entities = exclude.get("entities", []) or []
    include_entities = include.get("entities", []) or []

    domain = entity_id.split(".")[0]
    if domain in exclude_domains:
        return entity_id in include_entities
    return entity_id not in exclude_entities


def test_faq_recorder_config(my_predbat):
    """Check the FAQ's recorder example still records every entity Predbat reads history for."""
    failed = False
    print("**** Running FAQ recorder configuration tests ****")

    recorder = _load_recorder_example()
    if recorder is None:
        print("ERROR: docs/faq.md no longer contains a recorder filter example to check")
        return True

    needed = _entities_read_from_history()
    if not needed:
        print("ERROR: found no history reads in the Predbat source, the source scan must be broken")
        return True
    print("Test: the FAQ recorder example records all {} entities Predbat reads history for".format(len(needed)))

    for entity_id in sorted(needed):
        if not _is_recorded(entity_id, recorder):
            print("ERROR: docs/faq.md recorder example stops Home Assistant recording {}, whose history Predbat reads".format(entity_id))
            failed = True

    if failed:
        print("ERROR: test_faq_recorder_config FAILED")
    else:
        print("test_faq_recorder_config PASSED")

    return failed
