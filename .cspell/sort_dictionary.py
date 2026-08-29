#!/usr/bin/env python3
"""Sort the cspell custom dictionary, removing entries that differ only by case.

The upstream pre-commit file-contents-sorter cannot do this. Its --unique deduplicates with a
set(), which is case-sensitive, while --ignore-case sorts with a case-folded key - so a pair like
"Hanchu" and "hanchu" both survive and then compare equal. Python's sort is stable, so their order
is whatever order the set happened to iterate in, and set iteration order for strings varies with
PYTHONHASHSEED. The result flip-flops between runs, which meant every developer's pre-commit
reversed whatever CI had just committed.

cspell dictionaries are case-insensitive here (.cspell.json sets no caseSensitive flag), so a
capitalised duplicate adds nothing. The all-lowercase form is kept where one exists because it
also matches the capitalised and upper-case spellings, whereas the reverse is not true.
"""

import sys


def tidy(words):
    """Case-insensitively deduplicate and sort, preferring the all-lowercase spelling."""
    best = {}
    for word in words:
        key = word.lower()
        # Keep the lowercase spelling when the same word appears in more than one case
        if key not in best or (word.islower() and not best[key].islower()):
            best[key] = word
    # Every key is now unique under lower(), so this ordering is total and cannot vary by run
    return [best[key] for key in sorted(best)]


def sort_file(path):
    """Rewrite path if sorting changed it, returning 1 when it did."""
    with open(path, encoding="utf-8") as handle:
        before = handle.read()

    after = "".join(word + "\n" for word in tidy(line.strip() for line in before.splitlines() if line.strip()))
    if after == before:
        return 0

    with open(path, "w", encoding="utf-8") as handle:
        handle.write(after)
    print("Sorted {}".format(path))
    return 1


def main(argv):
    """Sort each file named on the command line."""
    return max((sort_file(path) for path in argv), default=0)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
