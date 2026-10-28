#!/usr/bin/env python3
"""Reject code that treats an export limit as the number it used to be packed into.

An export limit is a three-field instruction (mode, target SoC, export power). It used to be one
double with the target in the integer part, the export power in the fraction, and the mode in two
reserved whole values - so intent was read back out by comparing against 99 and 100. That encoding
caused real bugs: a target of 99 at reduced power landed in the reserved range and left the window
doing nothing (GH#4914), and the power fraction leaked into SoC arithmetic that only wanted the
target.

The fields are the interface now. This hook stops the old idiom coming back, because the reserved
constants still exist for the compatibility paths that read old plans and debug dumps, so nothing
in the language prevents a new comparison against them.

Ask the accessors instead:
    export_mode_of(limit) == EXPORT_MODE_FREEZE     not  limit == EXPORT_LIMIT_FREEZE
    export_mode_of(limit) == EXPORT_MODE_IDLE       not  limit >= EXPORT_LIMIT_IDLE
    export_target_of(limit)                         not  int(limit)
    export_power_of(limit)                          not  1 - (limit - int(limit))
    export_limit_exports_no_battery(limit)          not  limit >= EXPORT_LIMIT_FREEZE
"""

import re
import sys

# Where the encoding is legitimately still written down: the definition itself, the accessors that
# decode a legacy bare number, and the constants.
ALLOWED_FILES = {
    "apps/predbat/const.py",
    "apps/predbat/utils.py",
    "apps/predbat/prediction_kernel.cpp",
    "tools/check_export_limit_encoding.py",
    # Pins the legacy decode itself - it must compare against the sentinels to prove the
    # compatibility path still reads an old plan or debug dump the way it always did
    "apps/predbat/tests/test_export_encoding.py",
}

# A comparison of anything against the reserved sentinels, in either operand order.
SENTINEL_COMPARE = re.compile(r"(==|!=|<=|>=|<|>)\s*EXPORT_LIMIT_(FREEZE|IDLE)\b|\bEXPORT_LIMIT_(FREEZE|IDLE)\s*(==|!=|<=|>=|<|>)")

# The bare numbers the sentinels stand for, compared against something that names an export limit.
EXPORT_LIMIT_NAME = r"\b(export_limit|export_limits|limits|limit|this_export_limit|export_limit_now|peak_limit|best_export)\w*(\[[^\]]*\])?"
BARE_NUMBER_COMPARE = re.compile(r"({}\s*(==|!=|<=|>=|<|>)\s*(99|100)(\.\d+)?\b|\b(99|100)(\.\d+)?\s*(==|!=|<=|>=|<|>)\s*{})".format(EXPORT_LIMIT_NAME, EXPORT_LIMIT_NAME))


def check_file(path):
    """Return a list of (line number, line, reason) for the offending lines in one file."""
    problems = []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except (OSError, UnicodeDecodeError):
        return problems

    in_docstring = False
    docstring_quote = ""
    for number, line in enumerate(lines, 1):
        stripped = line.strip()

        # Track triple-quoted blocks so prose describing the old encoding is not flagged
        if in_docstring:
            if docstring_quote in line:
                in_docstring = False
            continue
        for quote in ('"""', "'''"):
            if quote in stripped:
                before, _, after = stripped.partition(quote)
                if quote not in after:
                    in_docstring = True
                    docstring_quote = quote
                    stripped = before
                else:
                    stripped = before + after.partition(quote)[2]
                break

        if stripped.startswith("#") or stripped.startswith("//"):
            continue
        if "encoding-ok" in line:
            # Deliberate use, justified in a comment on the same line
            continue
        if SENTINEL_COMPARE.search(stripped):
            problems.append((number, stripped, "compares against EXPORT_LIMIT_FREEZE/IDLE - ask export_mode_of() instead"))
        elif BARE_NUMBER_COMPARE.search(stripped):
            problems.append((number, stripped, "compares an export limit against 99/100 - ask export_mode_of() instead"))
    return problems


def main(argv):
    """Check every file named on the command line, returning 1 if any problems were found."""
    failed = False
    for path in argv[1:]:
        normalised = path.replace("\\", "/")
        if normalised in ALLOWED_FILES:
            continue
        for number, line, reason in check_file(path):
            print("{}:{}: {}".format(path, number, reason))
            print("    {}".format(line))
            failed = True
    if failed:
        print("")
        print("An export limit is an instruction, not the number it used to pack into.")
        print("Use export_mode_of / export_target_of / export_power_of / export_limit_exports_no_battery.")
        print("If a use really is deliberate, add an 'encoding-ok' comment on the line saying why.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
