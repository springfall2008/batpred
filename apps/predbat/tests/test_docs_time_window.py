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
Keep script-controlled inverter templates off the inverter's real window time entities, and keep the docs warning about it (issue #6069).

For an inverter type whose charge_time_format is not "HH:MM:SS" Predbat creates its own placeholder entities for
charge/discharge_start/end_time, but since #4738 an entity mapped in apps.yaml wins. Predbat's window writes - including
the 00:00:00 - 00:00:00 disable on inverters with no charge or discharge enable switch - then land on the real inverter
window. has_time_window and an apps.yaml set_charge_window/set_discharge_window stop none of this, so the docs say so.
"""

import glob
import os

import yaml

from config import INVERTER_DEF

REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
DOCS_DIR = os.path.join(REPO_ROOT, "docs")
TEMPLATES_DIR = os.path.join(REPO_ROOT, "templates")
WINDOW_TIME_ARGS = ["charge_start_time", "charge_end_time", "discharge_start_time", "discharge_end_time"]


class _TemplateLoader(yaml.SafeLoader):
    """YAML loader that reads tags such as !secret as plain values, so any template can be parsed."""


_TemplateLoader.add_multi_constructor("!", lambda loader, suffix, node: loader.construct_scalar(node) if isinstance(node, yaml.ScalarNode) else None)


def _as_list(value):
    """Return value as a list, wrapping a single value."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _charge_time_formats(config):
    """Return the charge_time_format of every inverter a template configures, from its own inverter block or the built-in type."""
    formats = []
    inline = config.get("inverter")
    if isinstance(inline, dict) and "charge_time_format" in inline:
        formats.append(inline["charge_time_format"])
    else:
        for inverter_type in _as_list(config.get("inverter_type", "GE")):
            if inverter_type in INVERTER_DEF:
                formats.append(INVERTER_DEF[inverter_type]["charge_time_format"])
    return formats


def _real_window_entities(config):
    """Return the (arg, value) pairs where a template maps a window time to a Home Assistant entity rather than a placeholder."""
    found = []
    for arg in WINDOW_TIME_ARGS:
        for value in _as_list(config.get(arg)):
            if isinstance(value, str) and ("." in value or value.startswith("re:")):
                found.append((arg, value))
    return found


def _section(text, heading):
    """Return the markdown section under heading, up to the next heading of the same or a higher level, ignoring fenced code blocks."""
    lines = text.splitlines()
    level = len(heading) - len(heading.lstrip("#"))
    out = None
    in_fence = False
    for line in lines:
        if out is None:
            if line.strip() == heading:
                out = []
            continue
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        if not in_fence and line.startswith("#"):
            this_level = len(line) - len(line.lstrip("#"))
            if line[this_level : this_level + 1] == " " and this_level <= level:
                break
        out.append(line)
    return "\n".join(out) if out is not None else None


def _read_doc(name):
    """Return the text of a docs page."""
    with open(os.path.join(DOCS_DIR, name), "r", encoding="utf-8") as handle:
        return handle.read()


def test_docs_time_window(my_predbat):
    """Check no script-controlled template maps real window time entities, and the docs warn about it."""
    failed = False
    print("**** Running docs/template time window tests ****")

    # The checks themselves: a real entity is flagged, a placeholder string is not, a built-in type is resolved
    if _real_window_entities({"discharge_end_time": ["time.solax_discharge_end_1"]}) != [("discharge_end_time", "time.solax_discharge_end_1")]:
        print("ERROR: a real time entity mapping was not flagged")
        failed = True
    if _real_window_entities({"charge_start_time": ["00:00:00"], "discharge_end_time": "00:01:00"}):
        print("ERROR: a literal placeholder time was flagged as an entity")
        failed = True
    if _charge_time_formats({"inverter_type": "GE"}) != [INVERTER_DEF["GE"]["charge_time_format"]]:
        print("ERROR: the charge_time_format of a built-in inverter type was not resolved")
        failed = True
    if _section("# Top\n## A\n```yaml\n# comment\n```\nafter fence\n## B\n", "## A") != "```yaml\n# comment\n```\nafter fence":
        print("ERROR: a # comment inside a fenced code block ended the markdown section")
        failed = True

    template_paths = sorted(glob.glob(os.path.join(TEMPLATES_DIR, "*.yaml")))
    if not template_paths:
        print("ERROR: found no templates to scan under {}".format(TEMPLATES_DIR))
        return True

    print("Test: {} templates keep script-controlled inverters off real window time entities".format(len(template_paths)))
    for path in template_paths:
        with open(path, "r", encoding="utf-8") as handle:
            data = yaml.load(handle, Loader=_TemplateLoader)  # nosec B506 - _TemplateLoader is a SafeLoader subclass
        config = (data or {}).get("pred_bat") if isinstance(data, dict) else None
        if not isinstance(config, dict):
            continue
        if not any(fmt != "HH:MM:SS" for fmt in _charge_time_formats(config)):
            continue
        for arg, value in _real_window_entities(config):
            print("ERROR: templates/{} maps {} to {} but its charge_time_format is not HH:MM:SS, so Predbat's window writes would land on the inverter".format(os.path.basename(path), arg, value))
            failed = True

    print("Test: inverter-setup.md has_time_window says it and set_charge_window/set_discharge_window gate nothing")
    has_time_window = _section(_read_doc("inverter-setup.md"), "### has_time_window") or ""
    for needed in ["set_charge_window", "set_discharge_window", "ignored", "Predbat mode", "00:00:00"]:
        if needed not in has_time_window:
            print("ERROR: inverter-setup.md has_time_window section does not mention '{}'".format(needed))
            failed = True

    print("Test: apps-yaml.md Schedule section warns about the 00:00:00 charge and discharge disable")
    schedule = _section(_read_doc("apps-yaml.md"), "#### Schedule") or ""
    for needed in ["has_discharge_enable_time", "has_charge_enable_time", "00:00:00", "charge_time_format", "set_discharge_window"]:
        if needed not in schedule:
            print("ERROR: apps-yaml.md Schedule section does not mention '{}'".format(needed))
            failed = True

    if failed:
        print("ERROR: test_docs_time_window FAILED")
    else:
        print("test_docs_time_window PASSED")
    return failed
