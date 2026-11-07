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
Keep the docs and apps.yaml templates from giving HACS-only install instructions (issue #6040).

From Home Assistant 2026.11 HACS is built in as the Marketplace (Settings -> Marketplace) and can no
longer be installed, so an instruction that only says "install via HACS" or "click the HACS icon"
can't be followed on a current Home Assistant. HACS is still right for 2026.10 and older, so the
rule is that any docs section (or template) mentioning HACS also mentions the Marketplace.
"""

import glob
import os
import re

REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
DOCS_DIR = os.path.join(REPO_ROOT, "docs")
TEMPLATES_DIR = os.path.join(REPO_ROOT, "templates")

# HACS mentions that are names rather than install instructions: the deprecated "HACS, Appdaemon
# app then Predbat" install method, the "Old Ohme HACS Integration" heading, and YouTube video titles
HISTORICAL_HACS_RE = re.compile(r"HACS,? Appdaemon|Old Ohme HACS Integration|\]\(https://(?:www\.)?youtu", re.IGNORECASE)
HEADING_RE = re.compile(r"^#{1,6} ")


def _markdown_sections(text):
    """Split markdown text into (heading, lines) sections at each heading line, ignoring fenced code blocks."""
    sections = []
    heading = "(top of file)"
    lines = []
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        if not in_fence and HEADING_RE.match(line):
            sections.append((heading, lines))
            heading = line.strip()
            lines = []
        lines.append(line)
    sections.append((heading, lines))
    return sections


def _hacs_only_sections(text):
    """Return the headings of sections that give a HACS instruction without mentioning the Marketplace."""
    bad = []
    for heading, lines in _markdown_sections(text):
        instructions = [line for line in lines if "HACS" in line and not HISTORICAL_HACS_RE.search(line)]
        if instructions and not any("Marketplace" in line for line in lines):
            bad.append(heading)
    return bad


def test_docs_marketplace(my_predbat):
    """Check every docs section and template that mentions HACS also covers the Marketplace."""
    failed = False
    print("**** Running docs HACS/Marketplace tests ****")

    doc_paths = sorted(glob.glob(os.path.join(DOCS_DIR, "*.md")))
    template_paths = sorted(glob.glob(os.path.join(TEMPLATES_DIR, "*.yaml")))
    if not doc_paths or not template_paths:
        print("ERROR: found no docs or templates to scan under {}".format(REPO_ROOT))
        return True

    # The rule itself: a section with a HACS instruction must mention the Marketplace, a historical name alone is fine
    if _hacs_only_sections("## Install\n\nInstall the card via HACS.\n") != ["## Install"]:
        print("ERROR: a HACS-only install section was not flagged")
        failed = True
    if _hacs_only_sections("## Install\n\nInstall it from the Marketplace (or HACS on older Home Assistant).\n"):
        print("ERROR: a section covering both the Marketplace and HACS was flagged")
        failed = True
    if _hacs_only_sections("## Old\n\nThe deprecated HACS, Appdaemon app then Predbat installation method.\n\n## Old Ohme HACS Integration\n"):
        print("ERROR: a historical HACS name was flagged as an install instruction")
        failed = True

    print("Test: {} docs pages give no HACS-only install instructions".format(len(doc_paths)))
    for path in doc_paths:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
        for heading in _hacs_only_sections(text):
            print("ERROR: docs/{} section '{}' mentions HACS but not the Marketplace (HACS can't be installed on Home Assistant 2026.11+)".format(os.path.basename(path), heading))
            failed = True

    print("Test: {} templates give no HACS-only install instructions".format(len(template_paths)))
    for path in template_paths:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
        if "HACS" in text and "Marketplace" not in text:
            print("ERROR: templates/{} mentions HACS but not the Marketplace".format(os.path.basename(path)))
            failed = True

    if failed:
        print("ERROR: test_docs_marketplace FAILED")
    else:
        print("test_docs_marketplace PASSED")
    return failed
