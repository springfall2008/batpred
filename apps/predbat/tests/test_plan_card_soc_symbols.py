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
Keep the "Battery SoC symbols" legend in docs/predbat-plan-card.md consistent with the symbols the
HTML plan actually puts on the SoC % cell (issue #6037).

The legend listed only the three trend arrows, so the weather alert triangle and the manual SoC
markers had no explanation and users had to ask what they meant (#6037, and #4107 before it).
"""

import os
import re

REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
OUTPUT_PATH = os.path.join(REPO_ROOT, "apps", "predbat", "output.py")
PLAN_CARD_PATH = os.path.join(REPO_ROOT, "docs", "predbat-plan-card.md")

# soc_sym = "&rarr;" (the trend arrow) or soc_sym = "&#9888; " + soc_sym (a marker in front of it)
SOC_SYM_ASSIGN_RE = re.compile(r'soc_sym\s*=\s*"(&[#a-zA-Z0-9]+;)')
# Each legend line is "- <symbol> - <meaning>"
LEGEND_LINE_RE = re.compile(r"^- (&[#a-zA-Z0-9]+;) - ", re.M)


def _soc_symbols_in_plan():
    """Return the set of HTML entities output.py writes into the plan's SoC % cell."""
    with open(OUTPUT_PATH, "r") as handle:
        source = handle.read()
    return set(SOC_SYM_ASSIGN_RE.findall(source))


def _soc_symbols_in_legend():
    """Return the set of HTML entities in the plan card's battery SoC symbols legend, or None if it isn't there."""
    with open(PLAN_CARD_PATH, "r") as handle:
        text = handle.read()
    match = re.search(r"^Battery SoC symbols:\n\n((?:- .*\n)+)", text, re.M)
    if not match:
        return None
    return set(LEGEND_LINE_RE.findall(match.group(1)))


def test_plan_card_soc_symbols(my_predbat):
    """Check every symbol the plan can show on the SoC % cell is explained in the plan card legend."""
    failed = False
    print("**** Running plan card SoC symbol legend tests ****")

    legend = _soc_symbols_in_legend()
    if legend is None:
        print("ERROR: docs/predbat-plan-card.md no longer contains a 'Battery SoC symbols:' legend to check")
        return True

    used = _soc_symbols_in_plan()
    if not used:
        print("ERROR: found no soc_sym assignments in output.py, the source scan must be broken")
        return True
    print("Test: the plan card legend explains all {} SoC % cell symbols the plan uses".format(len(used)))

    for symbol in sorted(used):
        if symbol not in legend:
            print("ERROR: the HTML plan shows {} on the SoC % cell but docs/predbat-plan-card.md doesn't explain it".format(symbol))
            failed = True

    if failed:
        print("ERROR: test_plan_card_soc_symbols FAILED")
    else:
        print("test_plan_card_soc_symbols PASSED")

    return failed
