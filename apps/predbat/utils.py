# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init


"""Utility functions for data processing, time manipulation, and calculations.

Provides helpers for parsing Home Assistant history data into per-minute
dictionaries, time string parsing, data filtering/pruning, rounding,
and historical data extraction from incrementing energy counters.
"""

import re
import array
import ctypes
import os
from datetime import datetime, timedelta, timezone, time
from io import StringIO
from functools import lru_cache
from const import (
    MINUTE_WATT,
    PREDICT_STEP,
    TIME_FORMAT,
    TIME_FORMAT_SECONDS,
    TIME_FORMAT_OCTOPUS,
    MAX_INCREMENT,
    TIME_FORMAT_DAILY,
    EXPORT_LIMIT_FREEZE,
    EXPORT_LIMIT_IDLE,
    EXPORT_MODE_TARGET,
    EXPORT_MODE_FREEZE,
    EXPORT_MODE_IDLE,
    FULL_EXPORT_POWER,
)
import copy
import json

DAY_OF_WEEK_MAP = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

# The live log and the one rotated out from under it - both read whole when serving logs.
PREDBAT_LOG_FILE = "predbat.log"

# Rotated logs are numbered predbat.01.log .. predbat.99.log. Two digits so a directory listing
# sorts in rotation order; before this they were single-digit, which is still read (see
# predbat_log_file_prev()) so an upgrade does not lose the previous log.
PREDBAT_LOG_COUNT_DEFAULT = 10
PREDBAT_LOG_COUNT_MIN = 2
PREDBAT_LOG_COUNT_MAX = 100


def predbat_log_name(number):
    """
    Return the rotated log filename for a rotation slot, zero-padded to two digits.
    """
    return "predbat.{:02d}.log".format(number)


def predbat_log_name_legacy(number):
    """
    Return the pre-#5076 un-padded rotated log filename for a rotation slot.

    Only for finding files written by an older version; nothing writes this form any more.
    """
    return "predbat.{}.log".format(number)


def predbat_log_file_prev():
    """
    Return the path of the most recently rotated log, or None when there is not one.

    Prefers the two-digit name and falls back to the single-digit one an older Predbat wrote, so
    the first run after an upgrade still shows the previous log rather than silently dropping it.
    """
    for candidate in (predbat_log_name(1), predbat_log_name_legacy(1)):
        if os.path.exists(candidate):
            return candidate
    return None


def rotate_predbat_logs(max_logs):
    """
    Shift rotated log slots up by one and drop anything past max_logs, against the current
    working directory. Does not touch the live predbat.log - the caller closes/reopens that.

    Walk downwards so each slot is free before anything moves into it. Both the two-digit name
    and the single-digit one an older Predbat wrote are considered at every slot, which is what
    migrates an existing set to the padded form: whichever name is found is renamed to the
    two-digit name of the next slot up.

    The shift runs through max_logs itself, not max_logs - 1: a legacy single-digit file sitting
    in the oldest kept slot is a different filename from that slot's two-digit target, so it is
    never reached by a rename landing *on* that slot from below - it has to be the *source* of a
    rename once, onto the slot that is about to be dropped, or it is orphaned on disk under the
    old name forever (Copilot review on #5076).
    """
    for num_logs in range(max_logs, 0, -1):
        for filename in (predbat_log_name(num_logs), predbat_log_name_legacy(num_logs)):
            if os.path.isfile(filename):
                os.rename(filename, predbat_log_name(num_logs + 1))
                break

    # Drop anything that has aged out past the configured count - both spellings, and every slot
    # up to the maximum rather than just the one above max_logs, so lowering the setting clears
    # the now-surplus files instead of stranding them forever.
    for num_logs in range(max_logs + 1, PREDBAT_LOG_COUNT_MAX + 1):
        for filename in (predbat_log_name(num_logs), predbat_log_name_legacy(num_logs)):
            if os.path.isfile(filename):
                os.remove(filename)


def predbat_log_count(args):
    """
    Return the configured number of log files to keep, including the live one.

    Clamped to [PREDBAT_LOG_COUNT_MIN, PREDBAT_LOG_COUNT_MAX]: below 2 there is no rotation to
    speak of, and above 100 the numbering would need a third digit. A non-numeric value falls
    back to the default rather than stopping the log working.
    """
    value = (args or {}).get("log_count", PREDBAT_LOG_COUNT_DEFAULT)
    try:
        value = int(value)
    except (TypeError, ValueError, OverflowError):
        # OverflowError: YAML accepts non-finite numeric scalars (.inf, -.inf), which parse as a
        # float int() cannot convert - falling back rather than raising out of every log() call
        # is the same "don't stop the log working" contract as the other invalid shapes here
        # (Copilot review on #5076).
        return PREDBAT_LOG_COUNT_DEFAULT
    return max(PREDBAT_LOG_COUNT_MIN, min(PREDBAT_LOG_COUNT_MAX, value))


# Key-name substrings that mark an apps.yaml value as a credential, for mask_secret_args().
# "_key" and "password" were the original pair; "secret" and "token" were added for #4768,
# which promotes apps.yaml over MCP as the config-review route and so hands it to a cloud AI -
# sigenergy_app_secret, solis_api_secret, solis_access_token, gateway_mqtt_token and
# mcp_secret were all being served in the clear.
SECRET_KEY_SUBSTRINGS = ("_key", "password", "secret", "token")

# Key suffixes that match a credential substring but hold no secret - timing metadata about a
# token rather than the token itself. An expiry time is exactly what you want to see when
# debugging "my cloud integration stopped working", so keep it readable.
SECRET_KEY_EXEMPT_SUFFIXES = ("_expires_at", "_expires", "_expiry", "_expiration", "_birth")

# Top-level apps.yaml keys not owned by any component (so not in the components.py registry) whose
# own CONTENTS are sensitive rather than the key name matching a credential substring -
# redact_strings/redact_strings_labelled are themselves the user's lists of values to redact
# (GH#4770), so they must be masked wholesale in a debug dump or they would defeat their own
# purpose - and for redact_strings_labelled, masking wholesale rather than per-value additionally
# means the user's own chosen labels never end up in the dump either, which could themselves hint
# at what the values are (a key named "landlord_mpan" is as informative as the MPAN itself).
SECRET_KEY_EXPLICIT_NAMES = ("redact_strings", "redact_strings_labelled")

# Credential key names that no substring catches and no component registers, because they are not
# apps.yaml keys: "account_id" is the annual tool's own raw-schema spelling
# (annual.load.octopus.account_id) for the credential apps.yaml calls octopus_api_account /
# kraken_account_id. Without it the value reached the annual result and web surface in the clear
# after being masked everywhere else (#5053 review).
#
# Kept separate from SECRET_KEY_EXPLICIT_NAMES because that list carries a second meaning -
# _collect_secret_values() skips those names at the top level so the redact_strings denylists get
# their labels from a later, more specific pass. These names have no such pass and must keep
# collecting normally wherever they appear.
SECRET_KEY_EXTRA_NAMES = ("account_id",)

# What a redacted credential is replaced with. Named because find_redacted_secret_overwrite()
# has to recognise it coming back in on a write, so the writer and the redactor must agree.
SECRET_MASK = "xxx"

# Use datetime.fromisoformat in str2time rather than strptime, set False to revert to strptime
STR2TIME_USE_FROMISOFORMAT = True


class MinuteArray:
    """Dense array-backed replacement for dict[int, float] keyed by contiguous minute indices.

    Stores values in a stdlib array.array('d') to reduce memory ~29x versus a Python dict
    (8 bytes per entry instead of ~232 bytes). Exposes the same get/[] interface used by
    get_from_incrementing and get_now_from_cumulative so no callers need updating.

    Only suitable when the key range is contiguous from 0 to size-1 (i.e. after smoothing).
    """

    def __init__(self, data, size):
        """Initialise from an existing dense dict, pre-allocated to size entries."""
        self._data = array.array("d", (data.get(i, 0.0) for i in range(size)))

    def get(self, key, default=0.0):
        """Return the value at key, or default if key is out of range."""
        if 0 <= key < len(self._data):
            return self._data[key]
        return default

    def __getitem__(self, key):
        """Return the value at key (no bounds check — callers are responsible)."""
        return self._data[key]

    def __len__(self):
        """Return the number of entries in the array."""
        return len(self._data)

    def __contains__(self, key):
        """True when key is a valid in-bounds index."""
        return isinstance(key, int) and 0 <= key < len(self._data)

    def __bool__(self):
        """True when the array is non-empty."""
        return len(self._data) > 0

    def __setitem__(self, key, value):
        """Set the value at key."""
        self._data[key] = float(value)

    def __iter__(self):
        """Iterate over all valid indices, mirroring dict iteration over keys."""
        return iter(range(len(self._data)))

    def keys(self):
        """Return a range covering all valid indices, mirroring dict.keys() for dense data."""
        return range(len(self._data))

    def copy(self):
        """Return a shallow copy of this MinuteArray."""
        new = MinuteArray.__new__(MinuteArray)
        new._data = array.array("d", self._data)
        return new


# Predbat member variables never included in a debug dump or served over MCP - live object
# graphs, the HA interface, loaded secrets and the URL caches. Shared with is_debug_excluded_key().
DEBUG_EXCLUDE_LIST = [
    "ha_interface",
    "components",
    "coordinator",
    "prediction",
    "logfile",
    "predheat",
    "inverters",
    "run_list",
    "threads",
    "EVENT_LISTEN_LIST",
    "local_tz",
    "CONFIG_ITEMS",
    "config_index",
    "comparison",
    "plugin_system",
    "ge_url_cache",
    "github_url_cache",
    "octopus_url_cache",
    "secrets",
]


def is_debug_excluded_key(key):
    """
    Return True when a Predbat member variable must be kept out of a debug dump or state query.

    The "db" prefix drops the database internals and "_key" drops credentials; both predate
    is_secret_key(), which is applied on top so secrets and tokens are caught here too (#4768).
    """
    if key.startswith("__") or key.startswith("db"):
        return True
    if key in DEBUG_EXCLUDE_LIST:
        return True
    return is_secret_key(key)


_REGISTRY_SECRET_NAMES = None


def registry_secret_key_names():
    """
    Return the apps.yaml config names components.py explicitly flags with "secret": True.

    utils is imported by every component module, so components cannot be imported at module
    scope here - it is imported on first use instead. An empty or failed result is not cached,
    so a redaction that runs while components is still importing (a partially initialised
    module) resolves properly on the next call rather than silently losing these names for the
    life of the process. Standalone tools that never import components keep working on the
    substring heuristic alone.
    """
    global _REGISTRY_SECRET_NAMES
    if _REGISTRY_SECRET_NAMES is None:
        try:
            import components

            names = components.secret_config_names()
        except Exception:
            names = None
        if not names:
            return frozenset()
        _REGISTRY_SECRET_NAMES = frozenset(names)
    return _REGISTRY_SECRET_NAMES


def is_secret_key(key, registry=True):
    """
    Return True when an apps.yaml key name holds a credential and must not be served in the clear.

    An explicit "secret": True flag in the component registry wins over both the substring
    heuristic and the exempt-suffix list - the registry names a credential the key name alone
    cannot reveal, such as an account number or a login identifier.

    registry=False drops back to the key-name substrings alone, for callers asking the narrower
    question "does this grant access?" rather than "must this be redacted?". Only
    find_unmasked_secret_paths() does: an account number identifies rather than authenticates, so
    telling every user with an inline octopus_api_account to move it into secrets.yaml would be
    noise. Redaction is the strict default so a new caller fails safe rather than leaking.
    """
    key_lower = str(key).lower()
    if key_lower in SECRET_KEY_EXPLICIT_NAMES or key_lower in SECRET_KEY_EXTRA_NAMES:
        return True
    if registry and key_lower in registry_secret_key_names():
        return True
    if key_lower.endswith(SECRET_KEY_EXEMPT_SUFFIXES):
        return False
    return any(substring in key_lower for substring in SECRET_KEY_SUBSTRINGS)


def _mask_secrets_in_place(value):
    """
    Redact credential-like keys anywhere inside an already-copied structure, in place.
    """
    if isinstance(value, dict):
        for key in value:
            if is_secret_key(key):
                value[key] = SECRET_MASK
            else:
                _mask_secrets_in_place(value[key])
    elif isinstance(value, list):
        for entry in value:
            _mask_secrets_in_place(entry)


def load_secrets(log=None):
    """
    Load secrets from secrets.yaml file
    Priority: PREDBAT_SECRETS_FILE env var, ./secrets.yaml, /config/secrets.yaml
    """
    import yaml

    log = log or (lambda message, **kwargs: print(message))
    secrets = {}
    secrets_file = None

    # Try loading from different locations in priority order
    possible_locations = [
        os.getenv("PREDBAT_SECRETS_FILE"),
        "secrets.yaml",
        "/homeassistant/secrets.yaml",
        "/conf/secrets.yaml",
        "/config/secrets.yaml",
    ]

    for location in possible_locations:
        if location and os.path.isfile(location):
            secrets_file = location
            break

    if secrets_file:
        log(f"Loading secrets from {secrets_file}", quiet=False)
        try:
            with open(secrets_file, "r") as stream:
                loaded = yaml.safe_load(stream) or {}
                if not isinstance(loaded, dict):
                    # Valid YAML (a bare scalar or list at the top level) but the wrong shape -
                    # yaml.safe_load() raises nothing here, so without this check `secrets`
                    # below would be reassigned to that scalar/list before the .get() call two
                    # lines down throws AttributeError. The generic except then logs the crash
                    # but the reassignment has already happened and is never undone, so
                    # load_secrets() still returns the malformed value - and the very next
                    # log() call reaches collect_log_secret_values()'s secrets.items(), which
                    # raises unhandled and aborts startup entirely (#5053 review).
                    log(f"Error: secrets.yaml at {secrets_file} must be a mapping of name: value, found {type(loaded).__name__} - ignoring it", quiet=False)
                else:
                    secrets = loaded
                    # Check for debug logging option
                    if secrets.get("logger") == "debug":
                        log(f"Info: Secrets loaded from {secrets_file}", quiet=False)
        except yaml.YAMLError as exc:
            log(f"Error: Failed to load secrets from {secrets_file}: {exc}", quiet=False)
        except Exception as exc:
            log(f"Error: Failed to open secrets file {secrets_file}: {exc}", quiet=False)
    else:
        log("Info: No secrets.yaml file found", quiet=False)

    return secrets


def load_apps_yaml(apps_file=None, log=None):
    """
    Load an apps.yaml-format file and return its pred_bat section, with !secret references resolved

    Shared so anything reading Predbat's configuration reads it the same way - Predbat's own
    startup below, and fox.py's --config option for a standalone CLI run. Raises yaml.YAMLError
    for a malformed file and KeyError when the pred_bat section is missing, leaving the caller to
    decide whether that is fatal.

    Returns (args, secrets).
    """
    import yaml

    log = log or (lambda message, **kwargs: print(message))
    secrets = load_secrets(log=log)

    def secret_constructor(loader, node):
        """YAML constructor for the !secret tag, resolving against the secrets just loaded."""
        secret_key = loader.construct_scalar(node)
        if secret_key in secrets:
            return secrets[secret_key]
        log(f"Warn: Secret '{secret_key}' not found in secrets.yaml")
        return None

    yaml.add_constructor("!secret", secret_constructor, Loader=yaml.SafeLoader)

    apps_file = apps_file or os.getenv("PREDBAT_APPS_FILE", "apps.yaml")
    log(f"Loading {apps_file}", quiet=False)
    with open(apps_file, "r") as stream:
        config = yaml.safe_load(stream)
    return config["pred_bat"], secrets


def mask_secret_args(args):
    """
    Return a deep copy of an apps.yaml-style args dict with credential-like keys redacted.

    Recurses through nested dicts and lists rather than checking only top-level names. apps.yaml
    routinely nests credentials one level down - the shipped template documents
    forecast_solar as a list of dicts each carrying its own api_key - and 'forecast_solar'
    matches none of SECRET_KEY_SUBSTRINGS, so a top-level-only pass hands that key over intact.
    That matters because everything this redacts is on its way to a third-party model.
    """
    masked = copy.deepcopy(args)
    _mask_secrets_in_place(masked)
    return masked


def _collect_secret_values(value, found, label_prefix=""):
    """
    Recursively gather {value: label} for the string values of credential-like keys, mirroring
    _mask_secrets_in_place()'s traversal but collecting rather than redacting. label_prefix lets
    a nested call (e.g. inside a forecast_solar list entry) qualify the label with the parent
    key, since the leaf key name alone ("api_key") is rarely distinctive on its own.
    """
    if isinstance(value, dict):
        for key, item in value.items():
            # redact_strings/redact_strings_labelled are secret-flagged here (via
            # SECRET_KEY_EXPLICIT_NAMES in is_secret_key()) so mask_secret_args()'s debug-dump
            # masking hides them wholesale - but collect_log_secret_values() already gathers
            # both explicitly afterward, in a specific order (a more specific label wins over
            # the generic "redact_strings" one for the same value). Collecting them here too,
            # at the top level, would race that ordering: this pass runs first, so a value
            # present in both would keep this pass's generic "redact_strings" label instead of
            # the more specific one redact_strings_labelled would have given it (#5053 review).
            # Only exempt the true top-level keys (label_prefix empty) - an unrelated nested key
            # that happens to share the name is not these denylists and should still collect.
            # str(key) first: apps.yaml keys are always strings in practice, but is_secret_key()
            # below already tolerates a non-string key the same way, and log() runs on the very
            # first startup line - an unguarded .lower() here would crash before validation ever
            # gets a chance to report the malformed input (#5053 review).
            if not label_prefix and str(key).lower() in SECRET_KEY_EXPLICIT_NAMES:
                continue
            if is_secret_key(key):
                key_label = (label_prefix + "." + str(key)) if label_prefix else str(key)
                if isinstance(item, (str, int, float)) and not isinstance(item, bool) and str(item) and str(item) not in found:
                    found[str(item)] = key_label
                elif isinstance(item, list):
                    # A secret-flagged key can itself hold a list (e.g. teslemetry_site_id,
                    # sigenergy_system_id are "string|string_list") - collect each element
                    # individually rather than dropping the whole list, since a log line needs
                    # each real value recognised on its own, not the list masked as one blob the
                    # way mask_secret_args()'s debug-dump redaction is allowed to.
                    # Coerced like the scalar branch above: an unquoted numeric element of a
                    # secret-flagged list (sigenergy_system_id, teslemetry_site_id) loads from
                    # YAML as an int, and log() serializes with str(msg), so a str-only check
                    # left it outside the pattern (#5053 review).
                    for entry in item:
                        if isinstance(entry, (str, int, float)) and not isinstance(entry, bool) and str(entry) and str(entry) not in found:
                            found[str(entry)] = key_label
            else:
                nested_prefix = label_prefix
                if isinstance(item, (dict, list)):
                    # str(key): a YAML mapping may legitimately have a non-string key
                    # ({123: {password: ...}}), and this traversal runs from log() on the very
                    # first startup line - a raw concatenation raised TypeError there, aborting
                    # startup before config validation could report it (#5053 review).
                    nested_prefix = (label_prefix + "." + str(key)) if label_prefix else str(key)
                _collect_secret_values(item, found, nested_prefix)
    elif isinstance(value, list):
        for entry in value:
            _collect_secret_values(entry, found, label_prefix)


def _flatten_denylist_value(value):
    """
    Yield every scalar inside a redact_strings/redact_strings_labelled entry, as strings.

    The denylists are the user's explicit "never log these values" list, so a shape this does not
    understand must not be dropped on the floor - dropping one leaves the credential the user
    asked to hide in the clear, which is worse than redacting something harmless. Both entry
    points previously tested `isinstance(value, (str, int, float))` and silently skipped anything
    else, so `redact_strings: [[1234567890123]]` or `redact_strings_labelled: {mpan: [123...]}`
    never entered the pattern even though validation accepted them (#5053 review).

    Recurses lists/tuples/sets and dict values, coerces scalars with str() the way log() does when
    it serialises a message, and drops only None, bools and empty strings - None and True/False
    have no useful log representation to match on, and an empty string would match everywhere.
    """
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, (str, int, float)):
        text = str(value)
        if text:
            yield text
        return
    if isinstance(value, dict):
        for item in value.values():
            yield from _flatten_denylist_value(item)
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _flatten_denylist_value(item)
        return
    # Any other type (a YAML date, say): str() it rather than ignore it, for the same
    # fail-safe reason - the user put it on the denylist deliberately.
    text = str(value)
    if text:
        yield text


def collect_log_secret_values(args, secrets, redact_strings=None, redact_strings_labelled=None):
    """
    Return {value: label} for every credential string value a log line must never be allowed to
    contain, and for every value in redact_strings/redact_strings_labelled (user-maintained
    apps.yaml denylists).

    Four sources, because not every user routes credentials through secrets.yaml and Predbat
    cannot infer every credential-shaped string a third-party integration exposes (GH#4770):
      - the resolved values of every secrets.yaml entry, labelled by their secrets.yaml key;
      - the resolved values of every credential-like key in args (an apps.yaml written with the
        key inline, `!secret` already resolved by the time args is built), labelled by that key;
      - redact_strings - values Predbat cannot recognise as a credential by key name or registry
        entry at all (an MPAN embedded in a third-party sensor's state or attributes, say), which
        the user lists explicitly because only they know it is sensitive. Labelled generically
        "redact_strings" - a bare string list carries no name to attach to any one entry;
      - redact_strings_labelled - the same idea as redact_strings, but a {label: value} mapping
        the user writes to get their own identifying label back in the log instead, the same way
        a built-in credential is labelled by its own apps.yaml key name - e.g.
        "my_landlords_mpan: '1234567890123'" redacts as <my_landlords_mpan> rather than every
        entry collapsing into the one generic <redact_strings> label.
    A value appearing in more than one source keeps whichever label it was found under first, in
    the order above - args/secrets/redact_strings_labelled all identify the credential, a bare
    redact_strings entry does not, so a more specific label wins when both would otherwise apply
    to the same value.

    Short values (len < 6) are dropped from the secrets.yaml and args sources - a one- or
    two-character secret is either a placeholder/empty default or would false-positive-redact
    ordinary log text constantly, and is not a credential worth the noise either way. Not applied
    to redact_strings/redact_strings_labelled: those are the user's own deliberate denylist, not
    a key-name heuristic that could coincidentally catch an ordinary word, so a short entry is
    still exactly what was asked to be redacted.

    redact_strings/redact_strings_labelled are not trusted to be well-formed: log() calls this on
    the very first startup log line, before APPS_SCHEMA validation has run at all, so anything
    malformed here must degrade rather than crash the whole of Predbat's startup on a config
    typo, before the user ever sees the validation warning. Both go through
    _flatten_denylist_value(), which accepts any shape - a bare scalar (apps.yaml's
    "redact_strings: !secret my_mpan" reaches validate_config() unwrapped by get_arg()), a nested
    list, or a dict - and yields every scalar inside it as a string. Values are coerced with
    str() because log() serialises messages the same way, so an unquoted numeric MPAN or account
    ID (landlord_mpan: 1234567890123, an int out of YAML) still matches (#5053 review).
    """
    found = {}
    if secrets:
        for key, value in secrets.items():
            if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                value = str(value)
                if len(value) >= 6 and value not in found:
                    found[value] = key
    if args:
        collected = {}
        _collect_secret_values(args, collected)
        for value, label in collected.items():
            if len(value) >= 6 and value not in found:
                found[value] = label
    # No length floor below this point: redact_strings/redact_strings_labelled are entries the
    # user put there deliberately, not something Predbat inferred from a key-name heuristic that
    # could coincidentally catch an ordinary short word - the false-positive risk the floor
    # exists to avoid above is the user's own call to accept here, and a short denylisted value
    # (a 4-digit PIN, say) is still exactly what they asked to have redacted.
    if isinstance(redact_strings_labelled, dict):
        for label, value in redact_strings_labelled.items():
            for scalar in _flatten_denylist_value(value):
                if scalar not in found:
                    found[scalar] = str(label)
    for scalar in _flatten_denylist_value(redact_strings):
        if scalar not in found:
            found[scalar] = "redact_strings"
    return found


def compile_log_secret_pattern(secret_values):
    """
    Compile the {value: label} map into a single alternation pattern plus a value->label lookup
    for redact_log_line(), or None when there is nothing to redact.

    Compiled once whenever the value set changes (hass.py caches this alongside the values
    themselves) rather than per log line: log() runs on every line, and matching one compiled
    alternation is a single scan of the line regardless of how many secrets there are to check
    for, where re-scanning the line once per value (the naive str.replace() loop) costs O(line
    length x secret count) on every single line Predbat ever logs.

    Returns (pattern, labels) rather than just a pattern: the label lookup is what lets
    redact_log_line() report *which* credential a masked line held (octopus_api_key, say)
    without ever writing out the value itself, so a log still tells you which integration to
    check when something goes wrong, instead of every credential collapsing into one opaque
    "xxx" indistinguishable from every other.
    """
    if not secret_values:
        return None
    # Longest-first: a shorter secret that happens to be a substring of a longer one (an API key
    # and a derived token sharing a prefix, say) must not pre-empt the longer, more specific match.
    ordered = sorted(secret_values, key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(value) for value in ordered))
    return pattern, secret_values


def redact_log_line(line, secret_pattern):
    """
    Replace any occurrence of a known secret value in a log line with a labelled mask, e.g.
    "<octopus_api_key>", identifying which credential was redacted without exposing it.

    Written at the point a log line is produced (hass.py log()), not at serve/download time: some
    users copy predbat.log directly off a Samba share exposing the addon's config directory,
    bypassing every HTTP/MCP endpoint entirely, so redacting only at those endpoints would leave
    the on-disk file itself carrying the plaintext value (GH#4770).

    Takes the already-compiled (pattern, labels) pair from compile_log_secret_pattern(), not the
    raw value map, so log() never pays compilation cost on the hot path.

    Two secrets can overlap as substrings starting at different offsets (e.g. "sec1" and "c123x"
    both present in "sec123x") - pattern.sub() alone only ever finds the first alternative that
    matches at the earliest position ("sec1"), then resumes scanning after it, so it never
    considers "c123x" starting one character in and leaves "23x" exposed. Extend each match to
    the longest secret that starts anywhere inside it before emitting the mask, so a longer
    secret overlapping a shorter one is always fully covered. Every secret that contributed to an
    extended span keeps its own label in the mask (joined with "+"), rather than falling back to
    the generic mask just because the merged span itself is not a single known value (#5053
    review) - the point of a labelled mask is telling an operator which credential to check.
    """
    if secret_pattern is None or not line:
        return line
    pattern, labels = secret_pattern
    out = []
    pos = 0
    for match in pattern.finditer(line):
        start, end = match.span()
        if start < pos:
            # Already covered by the extended span of a previous match.
            continue
        # Collect the label of every secret found to contribute to the (possibly extended) span,
        # in the order encountered, rather than looking up labels[line[start:end]] once at the
        # end - a merged span covering more than one overlapping secret is not itself a key in
        # labels, so that lookup would silently fall back to the generic mask and the line would
        # read no differently from an unrecognised value, losing the "which credential" guarantee
        # this feature exists to provide (#5053 review).
        span_labels = [labels.get(match.group(0), SECRET_MASK)]
        # Look for a longer secret starting at each position within this match's span and extend
        # to cover it - finditer() itself won't report an overlapping match once it has already
        # consumed the earlier one, so each candidate start position must be probed directly with
        # match(). Repeat in case the extension is itself overlapped by a still-longer secret.
        extended = True
        while extended:
            extended = False
            for probe in range(start + 1, end):
                rescan = pattern.match(line, probe)
                if rescan and rescan.end() > end:
                    end = rescan.end()
                    rescan_label = labels.get(rescan.group(0), SECRET_MASK)
                    if rescan_label not in span_labels:
                        span_labels.append(rescan_label)
                    extended = True
        out.append(line[pos:start])
        out.append("<{}>".format("+".join(span_labels)))
        pos = end
    out.append(line[pos:])
    return "".join(out)


def find_unmasked_secret_paths(node, path=""):
    """
    Recursively walk a ruamel round-trip-loaded apps.yaml section and yield the dotted path
    of every credential-like key (per is_secret_key()) whose value is a plain scalar rather
    than a '!secret' reference into secrets.yaml (loaded as a ruamel TaggedScalar).

    Deliberately asks is_secret_key(registry=False): this drives the "stored in plain text,
    consider !secret" advice, which is about values that grant access. The registry additionally
    flags account numbers, meter point numbers and login identifiers so they are redacted out of
    anything shared, but an inline octopus_api_account is the documented normal setup and
    warning every user about it would be noise rather than advice.

    Only usable against a document loaded with ruamel's round-trip loader - a plain
    yaml.safe_load() has already resolved '!secret' tags to their real value and lost the
    distinction this depends on.
    """
    from ruamel.yaml.comments import TaggedScalar

    if isinstance(node, dict):
        for key, value in node.items():
            key_path = "{}.{}".format(path, key) if path else str(key)
            if is_secret_key(key, registry=False):
                if value not in (None, "") and not isinstance(value, TaggedScalar):
                    yield key_path
            else:
                yield from find_unmasked_secret_paths(value, key_path)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from find_unmasked_secret_paths(item, "{}[{}]".format(path, index))


def _mask_secrets_in_yaml_node(node):
    """
    Redact credential values in a ruamel round-trip node, in place, leaving layout alone.

    A '!secret name' reference (a TaggedScalar) is left exactly as written: it holds no
    credential, only the name of one in secrets.yaml, and which secret a key resolves to is
    what makes a misconfigured integration diagnosable.
    """
    from ruamel.yaml.comments import TaggedScalar

    if isinstance(node, dict):
        for key in node:
            value = node[key]
            if is_secret_key(key):
                if value not in (None, "") and not isinstance(value, TaggedScalar):
                    node[key] = SECRET_MASK
            else:
                _mask_secrets_in_yaml_node(value)
    elif isinstance(node, list):
        for item in node:
            _mask_secrets_in_yaml_node(item)


def mask_secret_yaml_text(text):
    """
    Return apps.yaml text with credential values redacted, preserving comments and layout.

    mask_secret_args() redacts the parsed args Predbat is running on; this redacts the file as
    the user wrote it, so a download still reads like their own apps.yaml - comments, ordering,
    quoting and '!secret' references intact - with only the credential values replaced.

    Raises rather than returning anything on a file that will not parse: the caller asked for
    a redacted document, and serving unredacted text because the parse failed is exactly the
    leak this exists to prevent.
    """
    from ruamel.yaml import YAML

    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = YAML_DUMP_WIDTH
    data = yaml.load(text)
    _mask_secrets_in_yaml_node(data)
    buf = StringIO()
    yaml.dump(data, buf)
    return buf.getvalue()


def read_predbat_log(logfile=PREDBAT_LOG_FILE, logfile_prev=None):
    """
    Return the contents of predbat.log, prefixed with the rotated previous log when one exists.

    logfile_prev defaults to whichever previous log is actually present - the two-digit name, or
    the single-digit one an older Predbat wrote. Pass it explicitly only to read a specific file.
    """
    if logfile_prev is None:
        logfile_prev = predbat_log_file_prev()
    # Decoded explicitly rather than with the platform default: a single non-UTF-8 byte anywhere
    # in the log - an inverter API error message carrying one, say - would otherwise raise
    # UnicodeDecodeError and take out both /api/log and the get_log MCP tool.
    logdata = ""
    if os.path.exists(logfile):
        with open(logfile, "r", encoding="utf-8", errors="replace") as f:
            logdata = f.read()
    if logfile_prev and os.path.exists(logfile_prev):
        with open(logfile_prev, "r", encoding="utf-8", errors="replace") as f:
            logdata = f.read() + "\n" + logdata
    return logdata


def classify_log_line(line):
    """
    Return the severity bucket ("error", "warning", "info" or "log") for one predbat.log line.
    """
    line_lower = line.lower()
    if "error" in line_lower:
        return "error"
    if "warn" in line_lower:
        return "warning"
    if "info" in line_lower:
        return "info"
    return "log"


def log_line_included(line_type, filter_type):
    """
    Return True when a log line of the given severity belongs in the requested view.

    Errors appear on every view; warnings on "all" and "warnings"; info on "all" and
    "info"; everything else only on "all".
    """
    if line_type == "error":
        return True
    if line_type == "warning":
        return filter_type in ("all", "warnings")
    if line_type == "info":
        return filter_type in ("all", "info")
    return filter_type == "all"


def parse_log_timestamp(line):
    """
    Return the datetime a predbat.log line was written, or None when it carries no timestamp.

    Lines are written as "{datetime.now()}: {message}", so the stamp is the leading 26
    characters - or 19 when the microseconds happened to be zero and str() dropped them.
    """
    for length, time_format in ((26, "%Y-%m-%d %H:%M:%S.%f"), (19, "%Y-%m-%d %H:%M:%S")):
        if len(line) >= length:
            try:
                return datetime.strptime(line[0:length], time_format)
            except ValueError:
                continue
    return None


# Helper to make dict hashable for caching
def charge_curve_to_tuple(d):
    """Convert dict to tuple for use as cache key"""
    if not d:
        return ()
    return tuple(sorted(d.items()))


def get_now_from_cumulative(data, minutes_now, backwards):
    """
    Get current value from cumulative data
    """
    if backwards:
        # Work out the lowest value in a 5 minute period between minutes_now and minutes_now - 5
        lowest = 9999999999
        for minute in range(0, 5):
            lowest = min(data.get(minutes_now - minute, lowest), lowest)
        value = data.get(0, 0) - lowest
    else:
        lowest = 9999999999
        for minute in range(0, 5):
            lowest = min(data.get(minute, lowest), lowest)
        value = data.get(minutes_now, 0) - lowest
    return max(value, 0)


def prune_today(data, now_utc, midnight_utc, prune=True, group=15, prune_future=False, prune_future_days=0, prune_past_days=0, intermediate=False, offset_minutes=0):
    """
    Remove data from before today
    """
    results = {}
    last_time = None
    prev_value = None
    for key in data:
        # Convert key in format '2024-09-07T15:40:09.799567+00:00' into a datetime
        timekey = str2time(key)
        if last_time and (timekey - last_time).total_seconds() < group * 60:
            continue
        if intermediate and last_time and ((timekey - last_time).total_seconds() > group * 60):
            # Large gap, introduce intermediate data point
            seconds_gap = int((timekey - last_time).total_seconds())
            for i in range(1, seconds_gap // int(group * 60)):
                new_time = last_time + timedelta(seconds=i * group * 60) + timedelta(minutes=offset_minutes)
                results[new_time.isoformat()] = prev_value
        if not prune or (timekey > (midnight_utc - timedelta(days=prune_past_days))):
            if prune_future and (timekey > (now_utc + timedelta(days=prune_future_days))):
                continue
            new_time = timekey + timedelta(minutes=offset_minutes)
            results[new_time.isoformat()] = data[key]
            last_time = timekey
            prev_value = data[key]
    return results


def is_entity_id(value):
    """
    Whether a resolved apps.yaml value names a Home Assistant entity rather than being a literal.

    The same test resolve_arg() uses to decide whether to look a value up in HA: a string with a
    domain separator in it. Anything else - a number, a boolean, None - is a hard-wired value the
    user gave directly, which several shipped templates do for settings the inverter has no register
    for (huawei.yaml and sofar.yaml both hard-wire reserve).

    Anything fetched with indirect=False can therefore be a literal, and the state wrappers and the
    write_and_poll helpers all used to index straight into it - "$" in 12, or 12.split("."). That
    raised out of Inverter.__init__ and failed inverter creation outright, so no plan could be
    computed at all (GH#5003). They gate on this instead, so a literal is a warning about a control
    Predbat cannot read or write rather than a crash.
    """
    return isinstance(value, str) and "." in value


def is_data_numerical(history, attribute=None):
    """
    Check if history data is numerical (supports both state and attribute checking)
    Returns True if at least 10% of values are numeric or boolean
    """
    count_nums = 0
    count_total = 0

    if history and len(history) >= 1:
        for item in history[0]:
            if attribute:
                # Check attribute value
                attr_value = item.get("attributes", {}).get(attribute, None)
                if attr_value is None:
                    continue
                value = str(attr_value)
            else:
                # Check state value
                value = item.get("state", None)
                if value is None:
                    continue
                value = str(value)

            if value.lower() in ["on", "off", "true", "false"]:
                count_nums += 1
            else:
                try:
                    float(value)
                    count_nums += 1
                except (ValueError, TypeError):
                    pass
            count_total += 1

    if count_total > 0 and (count_nums / count_total) >= 0.1:
        return True
    elif count_total == 0:
        return True
    return False


# The top-level key apps.yaml wraps its whole Predbat configuration section in. Shared between
# web.py's apps.yaml editor and the AI tool layer (agent_tools.py/chat_tools.py) so both read and
# write the same section under one name - moved here, alongside update_nested_yaml_value() below,
# for the same reason is_data_numerical() was: the tool layer must not import from web.py (#4768).
ROOT_YAML_KEY = "pred_bat"

# Line width for any dump of apps.yaml. ruamel defaults to 80, which folds a long plain scalar onto
# a following, more-indented line - so rewriting the file to change one setting silently re-wraps
# every long value in it, API keys included. That still parses back to the same string, but it
# turns a one-line edit into a diff across the whole file and leaves credentials looking mangled.
# Set high enough that nothing Predbat writes ever wraps.
YAML_DUMP_WIDTH = 4096


def parse_yaml_path(path):
    """
    Split a dot-notation apps.yaml path into its segments, with "[n]" indexes as their own entry.

    "forecast_solar[0].azimuth" becomes ["forecast_solar", "[0]", "azimuth"]. Shared by
    update_nested_yaml_value(), resolve_nested_yaml_value() and set_apps_config()'s guards so all
    three agree on what a path means - a second copy of this parsing would eventually disagree
    with the writer about which segment is the leaf, which is the segment the credential checks
    depend on.
    """
    keys = []
    for component in path.split("."):
        # Split every bracket group into its own key, so a directly nested index - "foo[0][1]" -
        # becomes "foo", "[0]", "[1]". The earlier version split on the first "[" and unpacked
        # into two, which raised ValueError on any path with more than one index rather than
        # returning anything: reachable from set_apps_config, where it surfaced as a failed tool
        # call against the user's real configuration. Matches WebInterface._split_yaml_path, which
        # arrived at the same algorithm independently for the apps.yaml editor.
        for token in re.split(r"(\[[^\[\]]*\])", component):
            if token:
                keys.append(token)
    return keys


def resolve_nested_yaml_value(data, path):
    """
    Return the value a dot-notation path points at, raising KeyError if any segment is missing.

    The read-only twin of update_nested_yaml_value(), so a caller can confirm a path exists and
    read its current value *before* taking a backup and writing - update_nested_yaml_value raises
    part-way through otherwise, after the caller has already committed to the write.
    """
    keys = parse_yaml_path(path)
    current = data
    for key in keys:
        if key.startswith("[") and key.endswith("]"):
            index = int(key[1:-1])
            if not isinstance(current, list) or index >= len(current):
                raise KeyError("Index '{}' out of range in path '{}'".format(index, path))
            current = current[index]
        else:
            try:
                contains = key in current
            except TypeError:
                contains = False
            if not contains:
                raise KeyError("Key '{}' not found in path '{}'".format(key, path))
            current = current[key]
    return current


def find_redacted_secret_overwrite(previous_value, new_value):
    """
    Return the name of a credential a write would replace with the redaction placeholder.

    get_apps_config redacts credentials to "xxx", so a model that reads a container, edits one
    field and writes the whole thing back would store the literal "xxx" over a live key - the
    read-modify-write round trip silently destroys the credential it was careful not to read.
    Returns None when nothing is at risk, so the caller can refuse and point at the nested path
    instead of the container.
    """
    if isinstance(new_value, dict) and isinstance(previous_value, dict):
        for key, item in new_value.items():
            if is_secret_key(key) and item == SECRET_MASK and previous_value.get(key) not in (None, SECRET_MASK):
                return key
            found = find_redacted_secret_overwrite(previous_value.get(key), item)
            if found:
                return found
    elif isinstance(new_value, list) and isinstance(previous_value, list):
        for index, item in enumerate(new_value):
            if index < len(previous_value):
                found = find_redacted_secret_overwrite(previous_value[index], item)
                if found:
                    return found
    return None


def update_nested_yaml_value(data, path, value):
    """
    Update a nested value in YAML data using a dot-notation path, e.g. "battery_charge_low.normal"
    or a plain top-level key such as "num_inverters" (a path with no dots).

    Shared by web.py's apps.yaml batch editor (WebInterface.html_apps_post) and the chat agent's
    set_apps_config tool (chat_tools.py) - moved here so the tool layer can reuse it without
    importing from web.py (#4768). Raises KeyError when a key in the path - including the final
    one - is not already present, which is what gives both callers their "a key must already exist
    to be changed" rule for free, rather than each having to check it separately.
    """
    keys = parse_yaml_path(path)

    current = data

    # Navigate to the parent of the target value
    for key in keys[:-1]:
        if key.startswith("[") and key.endswith("]"):
            # Handle numerical index in square brackets
            index = int(key[1:-1])
            if not isinstance(current, list) or index >= len(current):
                raise KeyError(f"Index '{index}' out of range in path '{path}'")
            current = current[index]
        elif key in current:
            current = current[key]
        else:
            raise KeyError(f"Key '{key}' not found in path '{path}'")

    # Set the final value
    key = keys[-1]
    if key.startswith("[") and key.endswith("]"):
        # Handle numerical index in square brackets
        index = int(key[1:-1])
        if not isinstance(current, list) or index >= len(current):
            raise KeyError(f"Index '{index}' out of range in path '{path}'")
        current[index] = value
    elif key in current:
        current[key] = value
    else:
        # If final key is numerical try it as an integer
        if key.isdigit():
            key = int(key)
            if key not in current:
                raise KeyError(f"Final key '{key}' not found in path '{path}'")
            else:
                current[key] = value
        else:
            raise KeyError(f"Final key '{key}' not found in path '{path}'")


def history_attribute(history, state_key="state", last_updated_key="last_updated", scale=1.0, attributes=False, daily=False, offset_days=0, first=True, pounds=False, is_numerical=True, fallback_to_state=False):
    """
    Get historical data for an attribute

    fallback_to_state: when attributes=True and a point's attributes don't carry state_key
    (e.g. history recorded before the attribute existed), fall back to that point's own
    "state" field instead of dropping the point. Keeps a window that mixes pre/post-upgrade
    points from being silently truncated to only the post-upgrade tail.
    """
    results = {}
    last_updated_time = None
    last_day_stamp = None

    if not isinstance(history, list):
        return results

    if history and len(history) >= 1:
        history = history[0]

    if not isinstance(history, list):
        return results

    # Process history
    for item in history:
        if last_updated_key not in item:
            continue

        if attributes:
            if state_key not in item.get("attributes", {}):
                if not fallback_to_state or "state" not in item or item["state"] in ("unavailable", "unknown"):
                    continue
                state = item["state"]
            else:
                state = item["attributes"][state_key]
        else:
            # Ignore data without correct keys
            if state_key not in item:
                continue

            # Unavailable or bad values
            if item[state_key] == "unavailable" or item[state_key] == "unknown":
                continue

            state = item[state_key]

        # Get the numerical key and the timestamp and ignore if in error
        if is_numerical:
            try:
                state = float(state) * scale
                if pounds:
                    state = dp2(state / 100)
                else:
                    state = dp4(state)

            except (ValueError, TypeError):
                if isinstance(state, str):
                    if state.lower() in ["on", "true", "yes"]:
                        state = 1
                    elif state.lower() in ["off", "false", "no"]:
                        state = 0
                    else:
                        continue
                else:
                    continue

        try:
            last_updated_time = item[last_updated_key]
            last_updated_stamp = str2time(last_updated_time)
        except (ValueError, TypeError):
            continue

        day_stamp = last_updated_stamp.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        if offset_days:
            day_stamp += timedelta(days=offset_days)

        if first and daily and day_stamp == last_day_stamp:
            continue
        last_day_stamp = day_stamp

        # Add the state to the result
        if daily:
            # Convert day stamp from UTC into localtime
            results[day_stamp.strftime(TIME_FORMAT_DAILY)] = state
        else:
            results[last_updated_time] = state

    return results


def get_override_time_from_string(now_utc, time_str, plan_interval_minutes):
    """
    Convert a time string like "Sun 13:00" into a datetime object
    """
    # Parse the time string into a datetime object
    # Format is Sun 13:00
    try:
        override_time = datetime.strptime(time_str, "%a %H:%M")
        day_of_week_text = time_str.split()[0].lower()
        day_of_week = DAY_OF_WEEK_MAP.get(day_of_week_text, 0)
        has_day = True
    except ValueError:
        try:
            override_time = datetime.strptime(time_str, "%H:%M")
            day_of_week = now_utc.weekday()
            has_day = False
        except ValueError:
            return None

    # Convert day of week text to a number (0=Monday, 6=Sunday)
    day_of_week_today = now_utc.weekday()

    override_time = now_utc.replace(hour=override_time.hour, minute=override_time.minute, second=0, microsecond=0)

    # Ensure minutes are rounded down to the nearest plan_interval_minutes (e.g., 15 or 10)
    minute = (override_time.minute // plan_interval_minutes) * plan_interval_minutes
    override_time = override_time.replace(minute=minute)

    # Calculate days to add
    add_days = day_of_week - day_of_week_today

    # If the day has passed this week then use next week
    if add_days < 0:
        add_days += 7
    elif not has_day and override_time <= now_utc:
        # Check if override_time is within the current active time slot
        # A slot is active if it started within plan_interval_minutes ago
        minutes_since_override = (now_utc - override_time).total_seconds() / 60
        is_outside_current_slot = minutes_since_override >= plan_interval_minutes
        if is_outside_current_slot:
            # Not in current slot, use tomorrow
            add_days += 1
        # else: override_time is within current active slot, use today (add_days stays 0)

    override_time += timedelta(days=add_days)

    return override_time


def minute_data_state(history, days, now, state_key, last_updated_key, prev_last_updated_time=None):
    """
    Get historical data for state (e.g. predbat status)
    """
    mdata = {}
    last_state = "unknown"
    newest_state = "unknown"
    newest_age = 999999

    if not history:
        return mdata

    # Process history
    for item in history:
        # Ignore data without correct keys
        if state_key not in item:
            continue
        if last_updated_key not in item:
            continue

        # Unavailable or bad values
        if item[state_key] == "unavailable" or item[state_key] == "unknown":
            continue

        state = item[state_key]
        last_updated_time = str2time(item[last_updated_key])

        # Update prev to the first if not set
        if not prev_last_updated_time:
            prev_last_updated_time = last_updated_time
            last_state = state

        timed = now - last_updated_time
        timed_to = now - prev_last_updated_time

        minutes_to = int(timed_to.seconds / 60) + int(timed_to.days * 60 * 24)
        minutes = int(timed.seconds / 60) + int(timed.days * 60 * 24)

        minute = minutes
        while minute <= minutes_to:
            mdata[minute] = last_state
            minute += 1
        mdata[minutes] = state

        # Store previous state
        prev_last_updated_time = last_updated_time
        last_state = state

        if minutes <= newest_age:
            newest_age = minutes
            newest_state = state

    state = newest_state
    for minute in range(0, 60 * 24 * days):
        rindex = 60 * 24 * days - minute - 1
        state = mdata.get(rindex, state)
        mdata[rindex] = state

    return mdata


def history_attribute_to_minute_data(now_utc, data, backwards=True):
    """
    Get historical data for an attribute with history attribute filtering first
    """
    history = []
    oldest_date = now_utc
    for key in data:
        try:
            timestamp_key = str2time(key)
            oldest_date = min(oldest_date, timestamp_key)
        except (ValueError, TypeError):
            continue

        value = data[key]
        history.append({"last_updated": key, "state": value})
    max_age = now_utc - oldest_date
    max_days = max(max_age.days, 1)
    mdata, _ = minute_data(history, max_days, now_utc, "state", "last_updated", backwards=backwards, smoothing=False, scale=1.0, clean_increment=False, required_unit=None)
    return [mdata, max_days]


def filter_payment_method(rates, preferred="DIRECT_DEBIT"):
    """
    Keep one payment method variant when Octopus returns overlapping rows for the same window.

    The REST tariff endpoints return a DIRECT_DEBIT row and a NON_DIRECT_DEBIT row covering the
    same validity window. minute_data() writes each row over its range, so whichever row comes last
    in the response wins, and that order is not stable across periods. Rows with no payment_method
    (Agile, day/night) are left untouched, and so is a response that never mentions the preferred
    method, which keeps single-variant tariffs behaving exactly as before.
    """
    if not rates:
        return rates
    methods = {rate.get("payment_method") for rate in rates if isinstance(rate, dict)}
    if preferred not in methods:
        return rates
    return [rate for rate in rates if not isinstance(rate, dict) or rate.get("payment_method") in (preferred, None)]


def minute_data(
    history,
    days,
    now,
    state_key,
    last_updated_key,
    backwards=False,
    to_key=None,
    smoothing=False,
    clean_increment=False,
    divide_by=0,
    scale=1.0,
    accumulate=None,
    adjust_key=None,
    spreading=None,
    required_unit=None,
    prev_last_updated_time=None,
    last_state=0,
    attributes=False,
    max_increment=MAX_INCREMENT,
    interpolate=False,
    debug=False,
    can_modify_history=False,
):
    """
    Turns data from HA into a hash of data indexed by minute with the data being the value
    Can be backwards in time for history (N minutes ago) or forward in time (N minutes in the future)
    """
    if accumulate is None:
        accumulate = []
    mdata = {}
    adata = {}
    io_adjusted = {}
    newest_state = 0
    newest_age = 999999

    # Bounds on the data we store
    minute_min = -days * 24 * 60
    minute_max = days * 24 * 60

    # Check history is valid, if not empty return
    if not history:
        return mdata, io_adjusted

    # The glitch filter below is the only code here that writes to history, and it only runs for
    # backwards incrementing data, so that is the only case worth copying for. Copying regardless
    # cost ~150k deepcopy calls on a plan cycle for the two calculate_yesterday calls alone, neither
    # of which asks for the filter. can_modify_history stays the caller's explicit opt-out on top.
    if clean_increment and backwards and not can_modify_history:
        history = copy.deepcopy(history)  # Copy to avoid modifying original history

    # Glitch filter, cleans glitches in the data and removes bad values, only for incrementing data
    if clean_increment and backwards:
        if len(history) > 2:
            prev_prev_item = history[0]
            prev_item = history[1]

            if state_key in prev_item and state_key in prev_prev_item:
                try:
                    prev_value = float(prev_item[state_key])
                except (ValueError, TypeError):
                    prev_value = 0

                try:
                    prev_prev_value = float(prev_prev_item[state_key])
                except (ValueError, TypeError):
                    prev_prev_value = 0

                for item in history[2:]:
                    try:
                        value = float(item[state_key])
                    except (ValueError, TypeError):
                        value = prev_value
                        item[state_key] = value

                    # Filter simple glitch
                    if (prev_value > value) and (prev_value > prev_prev_value) and abs(prev_value - value) >= 0.1 and (value >= prev_prev_value):
                        prev_item[state_key] = value
                        prev_value = value

                    prev_prev_item = prev_item
                    prev_prev_value = prev_value
                    prev_value = value
                    prev_item = item

    # Process history
    for item in history:
        if last_updated_key not in item:
            continue

        if attributes:
            if state_key not in item["attributes"]:
                continue
            if item["attributes"][state_key] == "unavailable" or item["attributes"][state_key] == "unknown":
                continue
            state = item["attributes"][state_key]
        else:
            # Ignore data without correct keys
            if state_key not in item:
                continue
            # Unavailable or bad values
            if item[state_key] == "unavailable" or item[state_key] == "unknown":
                continue
            state = item[state_key]

        # Get the numerical key and the timestamp and ignore if in error
        try:
            state = float(state) * scale
            last_updated_time = str2time(item[last_updated_key])
            # Truncate sub-minute precision: a timestamp of 23:30:04 should land on the 23:30 minute boundary,
            # not be floored to 23:31 due to int() truncation of the elapsed seconds.
            last_updated_time = last_updated_time.replace(second=0, microsecond=0)
        except (ValueError, TypeError):
            continue

        # Find and converter units
        integrate = False
        if required_unit and ("attributes" in item):
            if "unit_of_measurement" in item["attributes"]:
                unit = item["attributes"]["unit_of_measurement"]
                if unit != required_unit:
                    if required_unit in ["kWh"] and unit in ["W"]:
                        state = state / 1000.0
                        integrate = True
                    elif required_unit in ["kWh"] and unit in ["kW"]:
                        integrate = True
                    elif required_unit in ["kW", "kWh", "kg", "kg/kWh"] and unit in ["W", "Wh", "g", "g/kWh"]:
                        state = state / 1000.0
                    elif required_unit in ["W", "Wh", "g", "g/kWh"] and unit in ["kW", "kWh", "kg", "kg/kWh"]:
                        state = state * 1000.0
                    elif required_unit in ["MW", "MWh"] and unit in ["kW", "kWh"]:
                        state = state / 1000.0
                    elif required_unit in ["kW", "kWh"] and unit in ["MW", "MWh"]:
                        state = state * 1000.0
                    else:
                        # Ignore data in wrong units if we can't converter
                        continue

        # Divide down the state if required
        if divide_by:
            state /= divide_by

        # Update prev to the first if not set
        if not prev_last_updated_time:
            prev_last_updated_time = last_updated_time
            last_state = state

        # Intelligent adjusted?
        if adjust_key:
            adjusted = item.get(adjust_key, False)
        else:
            adjusted = False

        # Work out end of time period
        # If we don't get it assume it's to the previous update, this is for historical data only (backwards)
        if to_key:
            to_value = item[to_key]
            if not to_value:
                to_time = now + timedelta(minutes=24 * 60 * days)
            else:
                to_time = str2time(item[to_key])
        else:
            if backwards:
                to_time = prev_last_updated_time
            else:
                if smoothing:
                    to_time = last_updated_time
                    last_updated_time = prev_last_updated_time
                else:
                    to_time = None

        if backwards:
            timed = now - last_updated_time
            if to_time:
                timed_to = now - to_time
        else:
            timed = last_updated_time - now
            if to_time:
                timed_to = to_time - now

        minutes = int(timed.total_seconds() / 60)
        if to_time:
            minutes_to = int(timed_to.total_seconds() / 60)
            minutes_delta = (timed_to.total_seconds() - timed.total_seconds()) / 60.0

        if minutes < newest_age:
            newest_age = minutes
            newest_state = state

        # Power to Energy
        if integrate and to_time:
            total_minutes = abs(minutes_delta)
            state = last_state + state * total_minutes / 60.0

        if to_time:
            minute = minutes
            if minute == minutes_to:
                if minute >= minute_min and minute <= minute_max:
                    mdata[minute] = state
            else:
                if smoothing:
                    near_midnight = (last_updated_time.time() < time(0, 6)) or (last_updated_time.time() > time(23, 58))
                    if clean_increment and state < last_state and (near_midnight or (last_state - state >= 1)):
                        # If there is a large drop in the data or we are near midnight where the sensor resets to zero,
                        # then smooth out the drop.
                        if debug:
                            print(f"Found drop at minute {minute}, where {state} < {last_state} (near midnight = {near_midnight}). Padding to {minutes_to}")
                        while minute < minutes_to:
                            if minute >= minute_min and minute <= minute_max:
                                mdata[minute] = state
                            minute += 1
                    else:
                        # Otherwise linearly interpolate between the two points, ignoring small dips in the data
                        if clean_increment and state < last_state:
                            state = last_state
                        diff = (state - last_state) / minutes_delta

                        if debug:
                            print(f"Smoothing from minute {minute} to {minutes_to}, where diff = {diff} = ({state} - {last_state}) / {minutes_delta}")

                        # If the spike is too big don't smooth it, it will removed in the clean function later
                        if clean_increment and max_increment > 0 and diff > max_increment:
                            if debug:
                                print(f"    Increment larger than max {max_increment}, setting diff to 0.")
                            diff = 0

                        index = 0
                        while minute < minutes_to:
                            if minute >= minute_min and minute <= minute_max:
                                if backwards:
                                    mdata[minute] = state - diff * index
                                else:
                                    mdata[minute] = last_state + diff * index
                            minute += 1
                            index += 1
                else:
                    if backwards:
                        # In backwards (oldest-first) mode, this item's `state` became active AT `minutes`.
                        # Write the current state at the transition minute, then fill the older period
                        # (minutes+1 to minutes_to inclusive) with `last_state` (the previous value).
                        if minutes >= minute_min and minutes <= minute_max:
                            mdata[minutes] = state
                        minute = minutes + 1
                        while minute <= minutes_to:
                            if minute >= minute_min and minute <= minute_max:
                                mdata[minute] = last_state
                                if adjusted:
                                    adata[minute] = True
                            minute += 1
                    else:
                        while minute < minutes_to:
                            if minute >= minute_min and minute <= minute_max:
                                mdata[minute] = state
                                if adjusted:
                                    adata[minute] = True
                            minute += 1
        else:
            if spreading:
                for minute in range(minutes, minutes + spreading):
                    if minute >= minute_min and minute <= minute_max:
                        mdata[minute] = state
            else:
                if minutes >= minute_min and minutes <= minute_max:
                    mdata[minutes] = state

        # Store previous time & state
        if to_time and not backwards:
            prev_last_updated_time = to_time
        else:
            prev_last_updated_time = last_updated_time
        last_state = state

    # If we only have a start time then fill the gaps with the last values
    if not to_key:
        # Fill from last sample until now with interpolation if enabled
        if interpolate and clean_increment and backwards:
            last_sample_minute = 0
            for minute in range(60):
                if minute in mdata:
                    last_sample_minute = minute
                    break
            last_but_one_sample_minute = last_sample_minute
            for minute in range(last_sample_minute + 5, 60):
                if minute in mdata and (mdata[minute] != mdata[last_sample_minute]):
                    last_but_one_sample_minute = minute
                    break
            sample_gap = last_but_one_sample_minute - last_sample_minute
            if last_sample_minute > 0 and sample_gap > 0 and last_sample_minute < 15:
                last_sample_value = mdata[last_sample_minute]
                last_but_one_minute_sample = mdata[last_but_one_sample_minute]
                step = (last_sample_value - last_but_one_minute_sample) / sample_gap
                if step > 0:
                    for minute in range(last_sample_minute):
                        if minute >= minute_min and minute <= minute_max:
                            mdata[minute] = dp4(last_sample_value + step * (last_sample_minute - minute))

        # Fill from last sample until now
        for minute in range(60 * 24 * days):
            if backwards:
                rindex = minute
            else:
                rindex = 60 * 24 * days - minute - 1

            if rindex not in mdata:
                if rindex >= minute_min and rindex <= minute_max:
                    mdata[rindex] = newest_state
            else:
                break

        # Find the first value
        state = 0
        for minute in range(60 * 24 * days):
            if backwards:
                rindex = 60 * 24 * days - minute - 1
            else:
                rindex = minute
            if rindex in mdata:
                state = mdata[rindex]
                break

        # Fill gaps in the middle
        for minute in range(60 * 24 * days):
            if backwards:
                rindex = 60 * 24 * days - minute - 1
            else:
                rindex = minute
            state = mdata.get(rindex, state)
            if rindex >= minute_min and rindex <= minute_max:
                mdata[rindex] = state

    # Reverse data with smoothing
    if clean_increment:
        mdata = clean_incrementing_reverse(mdata, max_increment)

    # Accumulate to previous data?
    if accumulate:
        for minute in range(60 * 24 * days):
            if minute in mdata:
                mdata[minute] += accumulate.get(minute, 0)
            else:
                if minute >= minute_min and minute <= minute_max:
                    mdata[minute] = accumulate.get(minute, 0)

    if adjust_key:
        io_adjusted = adata

    # Rounding
    for minute in mdata.keys():
        mdata[minute] = dp4(mdata[minute])

    return mdata, io_adjusted


def clean_incrementing_reverse(data, max_increment=0):
    """
    Cleanup an incrementing sensor data that runs backwards in time to remove the
    resets (where it goes back to 0) and make it always increment
    """
    new_data = {}
    if not data:
        return new_data
    length = max(data) + 1

    increment = 0
    last = data[length - 1]

    for index in range(length):
        rindex = length - index - 1
        nxt = data.get(rindex, last)
        if nxt >= last:
            if (max_increment > 0) and ((nxt - last) > max_increment):
                # Smooth out big spikes
                pass
            else:
                increment += nxt - last
            last = nxt
        elif nxt < last:
            if nxt <= 0 or ((last - nxt) >= (1.0)):
                last = nxt
        new_data[rindex] = increment

    return new_data


def format_time_ago(last_updated):
    """
    Format a timestamp to show how many minutes ago it was updated
    """
    if not last_updated:
        return "Never updated"

    try:
        now = datetime.now(timezone.utc)
        if not last_updated:
            return "Never updated"

        # Calculate time difference
        time_diff = now - last_updated
        total_minutes = int(time_diff.total_seconds() / 60)

        if total_minutes < 0:
            return "Just now"
        elif total_minutes == 0:
            return "Just now"
        elif total_minutes == 1:
            return "1 minute ago"
        elif total_minutes < 60:
            return f"{total_minutes} minutes ago"
        elif total_minutes < 120:
            return "1 hour ago"
        elif total_minutes < 1440:  # Less than 24 hours
            hours = total_minutes // 60
            return f"{hours} hours ago"
        else:  # More than 24 hours
            days = total_minutes // 1440
            if days == 1:
                return "1 day ago"
            else:
                return f"{days} days ago"

    except Exception as e:
        print(f"Error formatting time ago: {e}")
        return "Unknown ({})".format(last_updated)


# The format Predbat publishes car charging plan windows in. No year, because a plan never
# reaches more than 48 hours ahead - parse_car_plan_windows() puts one back.
CAR_PLAN_TIME_FORMAT = "%m-%d %H:%M:%S"

# How far from now a parsed window has to land before the year stamped on it is treated as
# the wrong one. Comfortably beyond the 48 hours a plan covers, so a genuinely distant
# window is never dragged into a different year, and far short of the ~12 months a
# mis-stamped year produces.
CAR_PLAN_YEAR_MARGIN = timedelta(days=180)


def parse_car_plan_windows(planned, now, local_tz):
    """Turn one car's published charging plan into a list of localised (start, end) pairs.

    Shared by the components that drive a charger from the plan (myenergi, GivEnergy EVC)
    so the awkward parts stay in one place: the plan carries no year, so each window is
    rebuilt around now - without that, a plan read either side of New Year lands eleven
    months out - and a malformed entry is skipped rather than costing the rest of the plan.

    The rebuild is symmetric. A window read at 23:30 on 31 December whose end is stamped
    01-01 parses as January of the year just ending, and needs shifting forward; the same
    window read at 00:30 on 1 January has its 12-31 start parsed as December of the year
    just started, and needs shifting back. Only the second case ever hides an active
    window, which is why it is the one that stops a car mid-charge if it is missed.

    Args:
        planned: The 'planned' attribute of a car charging slot sensor, a list of dicts
            with 'start' and 'end' keys.
        now: The instant every window is judged against, localised.
        local_tz: The timezone the plan's wall clock times are expressed in.
    """
    parsed = []
    for window in planned or []:
        try:
            start = local_tz.localize(datetime.strptime(window["start"], CAR_PLAN_TIME_FORMAT).replace(year=now.year))
            end = local_tz.localize(datetime.strptime(window["end"], CAR_PLAN_TIME_FORMAT).replace(year=now.year))
        except (KeyError, TypeError, ValueError):
            continue
        # Shift both ends together so their spacing survives, then close a window whose
        # end is in January while its start is still in December
        if start > now + CAR_PLAN_YEAR_MARGIN:
            start = start.replace(year=start.year - 1)
            end = end.replace(year=end.year - 1)
        elif start < now - CAR_PLAN_YEAR_MARGIN:
            start = start.replace(year=start.year + 1)
            end = end.replace(year=end.year + 1)
        if end < start:
            end = end.replace(year=end.year + 1)
        parsed.append((start, end))
    return parsed


def in_car_plan_window(windows, now):
    """Is now inside one of the (start, end) pairs returned by parse_car_plan_windows."""
    return any(start <= now < end for start, end in windows)


def in_iboost_slot(minute, iboost_plan):
    """
    Is the given minute inside a car slot
    """
    load_amount = 0

    if iboost_plan:
        for slot in iboost_plan:
            start_minutes = slot["start"]
            end_minutes = slot["end"]
            kwh = slot["kwh"]
            slot_minutes = end_minutes - start_minutes
            slot_hours = slot_minutes / 60.0

            # Return the load in that slot
            if minute >= start_minutes and minute < end_minutes:
                load_amount = abs(kwh / slot_hours)
                break
    return load_amount


def in_car_slot(minute, num_cars, car_charging_slots, slot_cap=None, slot_cap_period=None):
    """
    Is the given minute inside a car slot
    """
    load_amount = [0 for car_n in range(num_cars)]
    rate_amount = [0 for car_n in range(num_cars)]

    for car_n in range(num_cars):
        if car_charging_slots[car_n]:
            for slot in car_charging_slots[car_n]:
                start_minutes = slot["start"]
                end_minutes = slot["end"]
                kwh = slot["kwh"]
                octopus = slot.get("octopus", False)
                slot_minutes = end_minutes - start_minutes
                slot_hours = slot_minutes / 60.0

                # Return the load in that slot
                if minute >= start_minutes and minute < end_minutes:
                    load_amount[car_n] = abs(kwh / slot_hours)
                    # Only return rate for octopus as its used for premium calculations
                    rate_amount[car_n] = slot.get("average", 0) if octopus else 0
                    break
    return load_amount, rate_amount


def time_string_to_stamp(time_string):
    """
    Convert a time string to a timestamp
    """
    if time_string is None:
        return None
    if time_string == "unknown":
        return None

    if isinstance(time_string, str) and len(time_string) == 5:
        time_string += ":00"

    # Some inverters (e.g. GivEnergy) use "24:00:00" for midnight end-of-day
    if isinstance(time_string, str) and time_string.startswith("24:"):
        time_string = "00:" + time_string[3:]

    try:
        return datetime.strptime(time_string, "%H:%M:%S")
    except (ValueError, TypeError):
        print("WARN: time_string_to_stamp: invalid time string '{}', returning None".format(time_string))
        return None


def compute_window_minutes(start_time, end_time, minutes_now):
    """
    Convert start/end times to minutes and adjust for midnight-spanning windows and past windows.

    Args:
        start_time: Start time (datetime, time, or object with hour/minute attributes)
        end_time: End time (datetime, time, or object with hour/minute attributes)
        minutes_now: Current time in minutes from midnight

    Returns:
        Tuple of (start_minute, end_minute) adjusted for midnight spanning and current time
    """
    if start_time is None or end_time is None:
        # Invalid time, return 0,0
        return 0, 0

    start_minute = start_time.hour * 60 + start_time.minute
    end_minute = end_time.hour * 60 + end_time.minute

    if end_minute < start_minute:
        # Window spans midnight - adjust based on current time
        if end_minute > minutes_now:
            # We're past midnight but before end - move start back
            start_minute -= 24 * 60
        else:
            # End has passed - move end forward
            end_minute += 24 * 60

    # Window already passed, move it forward until the next one
    if end_minute <= minutes_now:
        start_minute += 24 * 60
        end_minute += 24 * 60

    return start_minute, end_minute


def window2minutes(start, end, minutes_now):
    """
    Convert time start/end window string into minutes
    """
    start = time_string_to_stamp(start)
    end = time_string_to_stamp(end)
    return compute_window_minutes(start, end, minutes_now)


def minutes_since_midnight(now, midnight):
    """
    Compute minutes from midnight to now, floored to a PREDICT_STEP boundary

    Both arguments come from the same clock - predbat's now_utc and the midnight derived from it -
    so they carry the same tzinfo and this stays a wall-clock difference across a DST change.
    """
    return int((now - midnight).total_seconds() / 60 / PREDICT_STEP) * PREDICT_STEP


def minutes_since_yesterday(now):
    """
    Calculate the number of minutes since 23:59 yesterday
    """
    yesterday = now - timedelta(days=1)
    # replace() rather than datetime.combine(): combine drops the tzinfo, and now is timezone
    # aware. Keeping the same tzinfo also keeps this a wall-clock difference, as it was when
    # both sides were naive.
    yesterday_at_2359 = yesterday.replace(hour=23, minute=59, second=59, microsecond=999999)
    difference = now - yesterday_at_2359
    difference_minutes = int((difference.seconds + 59) / 60)
    return difference_minutes


def dp0(value):
    """
    Round to 0 decimal places
    """
    return round(value)


def dp1(value):
    """
    Round to 1 decimal place
    """
    return round(value, 1)


def dp2(value):
    """
    Round to 2 decimal places
    """
    return round(value, 2)


def dp3(value):
    """
    Round to 3 decimal places
    """
    return round(value, 3)


def dp4(value):
    """
    Round to 4 decimal places
    """
    return round(value, 4)


def minutes_to_time(updated, now):
    """
    Compute the number of minutes between a time (now) and the updated time
    """
    timeday = updated - now
    minutes = int(timeday.seconds / 60) + int(timeday.days * 60 * 24)
    return minutes


def str2time_strptime(time_str):
    """
    Parse a timezone-aware time string into a datetime using strptime (legacy implementation)
    """
    if "." in time_str:
        tdata = datetime.strptime(time_str, TIME_FORMAT_SECONDS)
    elif "T" in time_str:
        tdata = datetime.strptime(time_str, TIME_FORMAT)
    else:
        tdata = datetime.strptime(time_str, TIME_FORMAT_OCTOPUS)
    return tdata


def str2time(time_str):
    """
    Parse a timezone-aware time string into a datetime

    Uses datetime.fromisoformat as a fast path (C-implemented, far less allocation churn than
    strptime) when STR2TIME_USE_FROMISOFORMAT is set, falling back to strptime for any string
    fromisoformat cannot handle or parses without a UTC offset (the strptime formats all
    require an offset, so the fallback preserves the ValueError contract for naive strings).
    """
    if STR2TIME_USE_FROMISOFORMAT:
        try:
            tdata = datetime.fromisoformat(time_str)
        except (ValueError, TypeError):
            return str2time_strptime(time_str)
        if tdata.tzinfo is None:
            return str2time_strptime(time_str)
        return tdata
    return str2time_strptime(time_str)


def calc_percent_limit(charge_limit, soc_max):
    """
    Calculate a charge limit in percent
    """
    if isinstance(charge_limit, list):
        if soc_max <= 0:
            return [0 for i in range(len(charge_limit))]
        else:
            return [min(int((float(charge_limit[i]) / soc_max * 100.0) + 0.5), 100) for i in range(len(charge_limit))]
    else:
        if soc_max <= 0:
            return 0
        else:
            return min(int((float(charge_limit) / soc_max * 100.0) + 0.5), 100)


# ---------------------------------------------------------------------------
# Export limit encoding
#
# An export window's instruction is a 3-tuple: (mode, target SoC percentage, export power). A
# plain tuple rather than a class because a plan builds, hashes and compares millions of them -
# a tuple is built at C speed, indexes as fast as an attribute reads, and hashes without a
# Python-level call. The fields are named by the accessors below and by these indices, so the
# layout is written down once.
#
# Three orthogonal signals as three fields, rather than the single double they used to be packed
# into. That encoding put the target in the integer part, the power in the fraction and the mode
# in two reserved whole values (EXPORT_LIMIT_FREEZE 99.0, EXPORT_LIMIT_IDLE 100.0), so one value
# answered three questions and no consumer could ask a clean one of it. It was also lossy: the
# power was recovered by subtracting the integer part, so 0.7 came back as 0.69999999999999929
# or 0.70000000000000284 depending on which target it was packed against, and two windows both at
# 70% did not compare equal.
#
# Every accessor also takes a bare number: plans and debug dumps written before the split arrive
# indefinitely (a bug report carries whatever version the user was running), so decoding the old
# packed value is a permanent compatibility path, not a migration.
# ---------------------------------------------------------------------------

EXPORT_FIELD_MODE = 0
EXPORT_FIELD_TARGET = 1
EXPORT_FIELD_POWER = 2


def export_mode_of(export_limit):
    """Which of the three export modes an export limit represents.

    Returns EXPORT_MODE_TARGET, EXPORT_MODE_FREEZE or EXPORT_MODE_IDLE.

    For a bare number (the legacy packed value) the freeze sentinel is matched exactly, not by
    range: most sites tested `== EXPORT_LIMIT_FREEZE`, so a value in (99.0, 100.0) - which the
    packed encoding could not itself produce - reads as a normal export.
    """
    if isinstance(export_limit, tuple):
        return export_limit[EXPORT_FIELD_MODE]
    if export_limit >= EXPORT_LIMIT_IDLE:
        return EXPORT_MODE_IDLE
    if export_limit == EXPORT_LIMIT_FREEZE:
        return EXPORT_MODE_FREEZE
    return EXPORT_MODE_TARGET


def export_target_of(export_limit):
    """The target SoC percentage an export limit exports down to.

    Only meaningful for EXPORT_MODE_TARGET; the other modes carry no target and return None so a
    caller cannot silently use 99 or 100 as if it were one.
    """
    if isinstance(export_limit, tuple):
        return export_limit[EXPORT_FIELD_TARGET]
    if export_mode_of(export_limit) != EXPORT_MODE_TARGET:
        return None
    return int(export_limit)


def export_power_of(export_limit):
    """The export power fraction of an export limit, 1.0 being full rate.

    For a bare number this mirrors the decode in Prediction.run_prediction and
    prediction_kernel.cpp: the stored fraction counts down from full power, so 47.3 means 70%
    rate. The other modes carry no power level and return full rate.
    """
    if isinstance(export_limit, tuple):
        return export_limit[EXPORT_FIELD_POWER]
    if export_mode_of(export_limit) != EXPORT_MODE_TARGET:
        return FULL_EXPORT_POWER
    return 1 - (export_limit - int(export_limit))


def export_limit_exports_no_battery(export_limit):
    """Whether this export limit discharges no battery - it is idle, or a freeze.

    Wraps what plan.py's trim pass expressed as `limit >= EXPORT_LIMIT_FREEZE`, which worked only
    because both reserved values sorted above every real target. As a mode field that is a
    membership test; the bare-number path keeps the >= behaviour for legacy values, including a
    value in the unreachable [99.0, 100.0) interval where it and export_mode_of disagree.
    """
    if isinstance(export_limit, tuple):
        return export_limit[EXPORT_FIELD_MODE] in (EXPORT_MODE_IDLE, EXPORT_MODE_FREEZE)
    return export_limit >= EXPORT_LIMIT_FREEZE


def export_limit_is_full_discharge(export_limit):
    """Whether this instruction exports the battery all the way down, at full power.

    Wraps what the planner's passes express as `limit == 0`, which only worked while a limit was a
    bare number whose zero value meant "target 0% at full rate".
    """
    return export_mode_of(export_limit) == EXPORT_MODE_TARGET and export_target_of(export_limit) == 0 and export_power_of(export_limit) == FULL_EXPORT_POWER


def export_limit_sort_key(export_limit):
    """The packed value an export limit represents, for ordering and for the display paths.

    The planner's passes compare limits to decide whether one is a shallower discharge than
    another (see the trim pass in optimise_plan_pass), and the modes must sort above every real
    target as the reserved values did. Tuples order lexicographically, which is not that order, so
    anything comparing two limits by depth - or formatting one as a number for a chart - goes
    through this rather than the raw value.

    Deliberately lossy at the top of the range: a 99% target at full power and a freeze both come
    back as 99.0, and a 100% target and an idle window both as 100.0, because this has to stay the
    number the display paths already print (window["target"], the plan_debug limit, the export limit
    chart series) and those are the numbers they print. The tie is benign - the two sides of it are
    a discharge that moves almost nothing and one that moves nothing - and nothing decides *what* a
    window does from this value: every mode test goes through export_mode_of, which reads the field
    and never confuses the two. Use this to order or to display; never to identify.
    """
    if not isinstance(export_limit, tuple):
        return export_limit
    mode = export_limit[EXPORT_FIELD_MODE]
    if mode == EXPORT_MODE_IDLE:
        return EXPORT_LIMIT_IDLE
    if mode == EXPORT_MODE_FREEZE:
        return EXPORT_LIMIT_FREEZE
    return export_limit[EXPORT_FIELD_TARGET] + (FULL_EXPORT_POWER - export_limit[EXPORT_FIELD_POWER])


def pack_export_limit(mode, target=None, power=FULL_EXPORT_POWER):
    """Build an export limit from the three signals it carries.

    The inverse of export_mode_of / export_target_of / export_power_of, kept beside them so the
    layout is written down in exactly one place instead of being re-derived at each call site
    (see plan.py's ladder, which builds the same values by hand).

    The two modes carry neither a target nor a power, so they normalise to None and full rate - a
    caller cannot then read 99 or 100 back out as if it were a target.
    """
    if mode != EXPORT_MODE_TARGET:
        return (mode, None, FULL_EXPORT_POWER)
    return (mode, int(target or 0), power)


def unpack_export_limit(packed):
    """Rebuild an export limit tuple from the packed float the encoding used to be.

    The reserved whole values are the two modes; anything else is a target in the integer part
    with the export power in the fraction. Used for plans and debug dumps written before the
    fields were split, which arrive indefinitely, so this is a permanent compatibility path.
    """
    if isinstance(packed, tuple):
        return packed
    if packed >= EXPORT_LIMIT_IDLE:
        return pack_export_limit(EXPORT_MODE_IDLE)
    if packed == EXPORT_LIMIT_FREEZE:
        return pack_export_limit(EXPORT_MODE_FREEZE)
    target = int(packed)
    # The packed fraction is 1 - power, and the subtraction is inexact in binary floating point:
    # 99.3 - 99 gives 0.30000000000000284, so the power comes back as 0.7000000000000028 rather
    # than 0.7. The encoding only ever carried one decimal place of power, so round to that - both
    # to recover the value that was packed and because an inexact power would otherwise be handed
    # to the C kernel.
    return pack_export_limit(EXPORT_MODE_TARGET, target, round(FULL_EXPORT_POWER - (packed - target), 1))


EXPORT_MODE_NAMES = {EXPORT_MODE_TARGET: "target", EXPORT_MODE_FREEZE: "freeze", EXPORT_MODE_IDLE: "idle"}
EXPORT_MODE_BY_NAME = {name: mode for mode, name in EXPORT_MODE_NAMES.items()}


def export_limit_to_stored(export_limit):
    """Serialise one export limit as a self-describing mapping.

    The packed float is an internal encoding, not a format worth persisting: 99.0 does not say
    "freeze" to anything that has not read const.py, and the fraction silently carries the export
    power. A plain mapping says what it means, survives yaml.safe_dump, and leaves room for fields
    the packed double has nowhere to put.

    Only the fields that apply to the mode are written, so a freeze does not claim a meaningless
    target or power.
    """
    mode = export_mode_of(export_limit)
    if mode != EXPORT_MODE_TARGET:
        return {"mode": EXPORT_MODE_NAMES[mode]}
    # Round the power so the stored file reads cleanly - the packed float form carried binary noise
    # (0.7 as 0.7000000000000028); the tuple is exact but a legacy value decoded here may not be.
    return {"mode": EXPORT_MODE_NAMES[mode], "target": export_target_of(export_limit), "power": round(export_power_of(export_limit), 6)}


def _export_limit_from_fields(mode, target, power):
    """Validate and build a target-mode export limit from raw mode/target/power fields.

    Shared by both branches of export_limit_from_stored() that carry real field values (the mapping
    form and the 3-element sequence form) so a malformed value is rejected the same way regardless
    of which shape it arrived in. GitHub Copilot review on PR #5047 found the sequence branch
    skipped this entirely - export_limit_from_stored(stored) returned tuple(stored) unvalidated, so
    a malformed 3-element sequence such as [EXPORT_MODE_TARGET, None, 0.7] reached the kernel
    marshaller's struct.pack and crashed there instead of falling back to idle as the docstring
    promises. A non-target mode (freeze/idle) carries no target or power to validate, so those go
    straight to pack_export_limit without calling this.
    """
    if mode not in (EXPORT_MODE_TARGET, EXPORT_MODE_FREEZE, EXPORT_MODE_IDLE):
        return pack_export_limit(EXPORT_MODE_IDLE)
    if mode != EXPORT_MODE_TARGET:
        return pack_export_limit(mode)
    try:
        target = int(target)
        power = float(power)
    except (TypeError, ValueError, OverflowError):
        return pack_export_limit(EXPORT_MODE_IDLE)
    # A target is any whole SoC percentage, 0 to 100 inclusive. The bound used to be the freeze
    # sentinel, which is where the packed encoding ran out of room - but the fields have no reserved
    # range, and the planner genuinely produces the top of it: clip_export_slots narrows a target
    # towards the SoC the simulation says is reachable, so a near-full battery with a derated
    # discharge rate clips to 99 or 100. Rejecting those made the write side and the read side
    # disagree - export_limit_to_stored wrote the target out faithfully and this read it back as an
    # idle window, silently dropping the export from a restored plan or a replayed debug dump.
    if target < 0 or target > 100 or power < 0 or power > FULL_EXPORT_POWER:
        return pack_export_limit(EXPORT_MODE_IDLE)
    return pack_export_limit(mode, target, power)


def export_limit_from_stored(stored):
    """Read one export limit from the mapping form, a bare packed float, or a 3-element sequence.

    The float branch is the translation layer for plans and debug dumps written before the mapping
    existed. Those arrive indefinitely - a bug report carries whatever version the user was running
    - so it is a permanent compatibility path, not a migration. A YAML or JSON round trip turns a
    tuple into a list, so a three-element sequence is an already-split limit that lost its type.

    Anything unrecognised becomes an idle window rather than raising: a debug dump is a diagnostic
    artefact and a malformed limit must not stop a replay.
    """
    if isinstance(stored, (list, tuple)) and len(stored) == 3 and not isinstance(stored[0], str):
        mode, target, power = stored
        return _export_limit_from_fields(mode, target, power)
    if isinstance(stored, dict):
        mode = EXPORT_MODE_BY_NAME.get(stored.get("mode"))
        if mode is None:
            return pack_export_limit(EXPORT_MODE_IDLE)
        if mode != EXPORT_MODE_TARGET:
            return pack_export_limit(mode)
        return _export_limit_from_fields(mode, stored.get("target", 0), stored.get("power", FULL_EXPORT_POWER))
    try:
        return unpack_export_limit(float(stored))
    except (TypeError, ValueError):
        return pack_export_limit(EXPORT_MODE_IDLE)


def export_limits_to_stored(export_limits):
    """Serialise a list of export limits for the persisted plan or a debug dump."""
    return [export_limit_to_stored(limit) for limit in export_limits or []]


def export_limits_from_stored(stored):
    """Read a list of export limits written in any of the accepted forms."""
    return [export_limit_from_stored(limit) for limit in stored or []]


def clone_windows(windows):
    """Shallow-copy a list of window dicts (start/end/average/... primitive fields only).

    Window dicts never hold nested mutable values, so copying each dict is equivalent to
    copy.deepcopy(windows) here but far cheaper - deepcopy's generic recursive walk measured
    ~275us per call on a typical export_window, this is a few us.
    """
    return [w.copy() for w in windows]


def remove_intersecting_windows(charge_limit_best, charge_window_best, export_limit_best, export_window_best):
    """
    Filters and removes intersecting charge windows

    This runs on every simulation (see Prediction.run_prediction and run_prediction_kernel), so it
    sits in front of the C++ kernel on the hot path and only does the work that can change something:

    - only export windows that are enabled (limit < 100) can clip anything, so they are collected
      once and the function returns immediately when there are none
    - only charge windows that are enabled (limit > 0) can be clipped, so a disabled one
      short-circuits instead of being scanned against every export window

    Clipping is a single pass. Export windows are processed in start order, so when a charge window
    is split the head segment it emits ends at the current export window's start and no later export
    window can reach back into it - which is what the previous "clip again" pass over the whole
    window list existed to catch. The sort is kept even though callers already provide sorted
    windows, so correctness does not depend on that.

    See run_intersect_window_tests, which pins this behaviour with hand-written cases and compares
    the result against a naive reference implementation over randomised window layouts.
    """
    # Enabled export windows only - the sole candidates for clipping anything
    export_active = sorted((export_window_best[n]["start"], export_window_best[n]["end"]) for n in range(len(export_limit_best)) if export_mode_of(export_limit_best[n]) != EXPORT_MODE_IDLE)
    if not export_active:
        # Rebuild the windows rather than passing the caller's dicts back, so the returned windows
        # carry exactly the same keys (and are as freshly owned) as on the clipping path below
        return list(charge_limit_best), [{"start": w["start"], "end": w["end"], "average": w["average"]} for w in charge_window_best]

    new_limit_best = []
    new_window_best = []

    # For each charge window
    for window_n in range(len(charge_limit_best)):
        window = charge_window_best[window_n]
        start = window["start"]
        end = window["end"]
        average = window["average"]
        limit = charge_limit_best[window_n]
        clipped = False

        if limit <= 0.0:
            # A disabled charge window can never be clipped; rebuild it exactly as the clipping
            # path below would have done, so the returned dicts are equivalent either way
            new_window_best.append({"start": start, "end": end, "average": average})
            new_limit_best.append(limit)
            continue

        # For each enabled discharge window, in start order
        for dstart, dend in export_active:
            # Overlapping window?
            if (dstart < end) and (dend >= start):
                if dstart <= start:
                    if start != dend:
                        start = dend
                        clipped = True
                elif dend >= end:
                    if end != dstart:
                        end = dstart
                        clipped = True
                else:
                    # Two segments - emit the head now, carry on clipping the tail
                    if (dstart - start) >= 5:
                        new_window_best.append({"start": start, "end": dstart, "average": average})
                        new_limit_best.append(limit)
                    start = dend
                    clipped = True

        if not clipped or ((end - start) >= 5):
            new_window_best.append({"start": start, "end": end, "average": average})
            new_limit_best.append(limit)

    return new_limit_best, new_window_best


@lru_cache(maxsize=8192)
def get_charge_rate_curve_cached(soc, charge_rate_setting, soc_max, battery_rate_max_charge, battery_charge_power_curve_tuple, battery_rate_min, battery_temperature, battery_temperature_curve_tuple):
    """
    Cached computation of true charging rate from SoC and charge rate setting
    """
    battery_charge_power_curve = dict(battery_charge_power_curve_tuple) if battery_charge_power_curve_tuple else {}
    battery_temperature_curve = dict(battery_temperature_curve_tuple) if battery_temperature_curve_tuple else {}

    soc_percent = calc_percent_limit(soc, soc_max)
    max_charge_rate = battery_rate_max_charge * get_curve_value(battery_charge_power_curve, soc_percent, 1.0)

    # Temperature cap
    max_rate_cap = find_battery_temperature_cap(battery_temperature, battery_temperature_curve, soc_max, battery_rate_max_charge)
    max_charge_rate = min(max_charge_rate, max_rate_cap)

    return max(min(charge_rate_setting, max_charge_rate), battery_rate_min)


def get_charge_rate_curve(soc, charge_rate_setting, soc_max, battery_rate_max_charge, battery_charge_power_curve, battery_rate_min, battery_temperature, battery_temperature_curve):
    """
    Compute true charging rate from SoC and charge rate setting
    """
    return get_charge_rate_curve_cached(round(soc, 1), charge_rate_setting, soc_max, battery_rate_max_charge, charge_curve_to_tuple(battery_charge_power_curve), battery_rate_min, battery_temperature, charge_curve_to_tuple(battery_temperature_curve))


@lru_cache(maxsize=8192)
def get_discharge_rate_curve_cached(soc, discharge_rate_setting, soc_max, battery_rate_max_discharge, battery_discharge_power_curve_tuple, battery_rate_min, battery_temperature, battery_temperature_curve_tuple):
    """
    Cached computation of true discharging rate from SoC and charge rate setting
    """
    battery_discharge_power_curve = dict(battery_discharge_power_curve_tuple) if battery_discharge_power_curve_tuple else {}
    battery_temperature_curve = dict(battery_temperature_curve_tuple) if battery_temperature_curve_tuple else {}

    soc_percent = calc_percent_limit(soc, soc_max)
    max_discharge_rate = battery_rate_max_discharge * get_curve_value(battery_discharge_power_curve, soc_percent, 1.0)
    max_rate_cap = find_battery_temperature_cap(battery_temperature, battery_temperature_curve, soc_max, battery_rate_max_discharge)
    max_discharge_rate = min(max_discharge_rate, max_rate_cap)

    return max(min(discharge_rate_setting, max_discharge_rate), battery_rate_min)


def get_discharge_rate_curve(soc, discharge_rate_setting, soc_max, battery_rate_max_discharge, battery_discharge_power_curve, battery_rate_min, battery_temperature, battery_temperature_curve):
    """
    Compute true discharging rate from SoC and charge rate setting
    """
    return get_discharge_rate_curve_cached(
        round(soc, 1), discharge_rate_setting, soc_max, battery_rate_max_discharge, charge_curve_to_tuple(battery_discharge_power_curve), battery_rate_min, battery_temperature, charge_curve_to_tuple(battery_temperature_curve)
    )


"""
Get value from curve with integer or string index
"""


def get_curve_value(curve, index, default=1.0):
    return curve.get(index, default)


def find_battery_temperature_cap(battery_temperature, battery_temperature_curve, soc_max, max_rate):
    """
    Find the battery temperature cap
    """
    battery_temperature_idx = min(battery_temperature, 20)
    battery_temperature_idx = max(battery_temperature_idx, -20)
    battery_temperature_idx = int(battery_temperature_idx)  # Convert to int for proper key matching
    # Try to get the temperature adjustment from the curve (handles both int and string keys)
    battery_temperature_adjust = get_curve_value(battery_temperature_curve, battery_temperature_idx, None)
    if battery_temperature_adjust is None:
        # If not found, try fallback values
        if battery_temperature_idx > 0:
            battery_temperature_adjust = get_curve_value(battery_temperature_curve, 20, 1.0)
        else:
            battery_temperature_adjust = get_curve_value(battery_temperature_curve, 0, 1.0)
    battery_temperature_rate_cap = soc_max * battery_temperature_adjust / 60.0

    return min(battery_temperature_rate_cap, max_rate)


def find_charge_rate(
    minutes_now,
    soc,
    window,
    target_soc,
    max_rate,
    soc_max,
    battery_charge_power_curve,
    set_charge_low_power,
    charge_low_power_margin,
    battery_rate_min,
    battery_rate_max_scaling,
    battery_loss,
    log_to,
    battery_temperature=20,
    battery_temperature_curve=None,
    current_charge_rate=None,
    pv_window_kwh=0.0,
    low_power_pv_threshold_w=0.0,
    solar_full_rate=True,
):
    """
    Find the lowest charge rate that fits the charge slow

    pv_window_kwh is the PV forecast in kWh over the remainder of the charge window, when the window's
    own average power over that remainder exceeds low_power_pv_threshold_w, low power charging is
    abandoned - the throttled rate applies for the whole window and would push that PV out of the
    battery, raising the cost above the planned full rate charge. Comparing an average rather than
    pv_window_kwh directly against a fixed energy figure keeps the decision independent of how long the
    remaining window happens to be - a long window at a low constant trickle should not accumulate its
    way past a threshold sized for "is this bright enough to matter" (#4699 follow-up).

    solar_full_rate turns that abandon off (#4975). Whether spilling PV to hold a throttled rate is
    worth it depends on what import costs in this particular window, which is not something the PV
    forecast can answer - a user charging in a free or very cheap daytime window loses nothing by
    throttling, while on a normal tariff the exported surplus has to be bought back later. Defaults to
    True, the behaviour of #4373.
    """
    if battery_temperature_curve is None:
        battery_temperature_curve = {}
    margin = charge_low_power_margin
    target_soc = round(target_soc, 2)

    # Current charge rate
    if current_charge_rate is None:
        current_charge_rate = max_rate

    battery_temperature_curve_tuple = charge_curve_to_tuple(battery_temperature_curve)
    battery_charge_power_curve_tuple = charge_curve_to_tuple(battery_charge_power_curve)

    # Real achieved max rate
    max_rate_real = get_charge_rate_curve_cached(round(soc, 1), max_rate, soc_max, max_rate, battery_charge_power_curve_tuple, battery_rate_min, battery_temperature, battery_temperature_curve_tuple) * battery_rate_max_scaling

    min_battery_rate = max(400, int(round(battery_rate_min * MINUTE_WATT)))
    if set_charge_low_power:
        minutes_left = window["end"] - minutes_now - margin
        abs_minutes_left = window["end"] - minutes_now

        # If the charge window's own average PV power over its remainder is above the threshold, charge
        # at max rate instead - a throttled rate would cap the PV going into the battery, exporting the
        # surplus and importing to make the target up later. Turned off by
        # set_charge_low_power_solar_full_rate for a window where that trade does not apply, e.g. a
        # free import period, where the throttled rate is wanted even though PV will spill (#4975)
        low_power_pv_threshold_kwh = (low_power_pv_threshold_w / MINUTE_WATT) * max(abs_minutes_left, 0)
        if solar_full_rate and pv_window_kwh > 0 and pv_window_kwh >= low_power_pv_threshold_kwh:
            if log_to:
                log_to("Low power mode: PV forecast in window {}kWh > {}kWh ({}W over {} minutes), default to max rate".format(dp2(pv_window_kwh), dp2(low_power_pv_threshold_kwh), low_power_pv_threshold_w, abs_minutes_left))
            return max_rate, max_rate_real

        # If we don't have enough minutes left go to max
        if abs_minutes_left < 0:
            if log_to:
                log_to("Low power mode: abs_minutes_left {} < 0, default to max rate".format(dp2(abs_minutes_left)))
            return max_rate, max_rate_real

        # If we already have reached target go back to max
        if round(soc, 2) >= target_soc:
            if log_to:
                log_to("Low power mode: SoC {}kW >= target_SoC {}kW, default to max rate".format(soc, target_soc))
            return max_rate, max_rate_real

        # Work out the charge left in kw
        charge_left = round(target_soc - soc, 2)

        # If we can never hit the target then go to max
        if round(max_rate_real * abs_minutes_left, 2) <= charge_left:
            if log_to:
                log_to(
                    "Low power mode: Can't hit target: max_rate * abs_minutes_left = {}kW <= charge_left {}kW, minutes_left {}, window_end {}, minutes_now {}, default to max rate".format(
                        dp2(max_rate_real * abs_minutes_left), charge_left, abs_minutes_left, window["end"], minutes_now
                    )
                )
            return max_rate, max_rate_real

        # What's the lowest we could go?
        min_rate = charge_left / abs_minutes_left
        min_rate_w = int(min_rate * MINUTE_WATT)

        # Apply the curve at each rate to pick one that works
        rate_w = max_rate * MINUTE_WATT
        best_rate = max_rate
        best_rate_real = max_rate_real
        highest_achievable_rate = 0

        if log_to:
            log_to(
                "Find charge rate for low power mode: SoC: {}kW, target_SoC: {}kW, charge_left: {}kW, minutes_left: {}, abs_minutes_left: {}, max_rate: {}W, min_rate: {}W, min_rate_w: {}W".format(
                    soc, target_soc, charge_left, minutes_left, abs_minutes_left, dp0(max_rate * MINUTE_WATT), dp0(min_rate * MINUTE_WATT), dp0(min_rate_w)
                )
            )

        while rate_w >= min_battery_rate:
            rate = rate_w / MINUTE_WATT
            if rate_w >= min_rate_w:
                charge_now = soc
                rate_scale_max = 0
                # Compute over the time period, include the completion time
                for _minute in range(0, minutes_left, PREDICT_STEP):
                    rate_scale = get_charge_rate_curve_cached(round(charge_now, 1), rate, soc_max, max_rate, battery_charge_power_curve_tuple, battery_rate_min, battery_temperature, battery_temperature_curve_tuple)
                    highest_achievable_rate = max(highest_achievable_rate, rate_scale)
                    rate_scale *= battery_rate_max_scaling
                    rate_scale_max = max(rate_scale_max, rate_scale)
                    charge_amount = rate_scale * PREDICT_STEP * battery_loss
                    charge_now += charge_amount
                    if (round(charge_now, 2) >= target_soc) and (rate_scale_max < best_rate_real):
                        best_rate = rate
                        best_rate_real = rate_scale_max
                        break
                # if log_to:
                #   log_to("Low Power mode: rate: {} minutes: {} SOC: {} Target SoC: {} Charge left: {} Charge now: {} Rate scale: {} Charge amount: {} Charge now: {} best rate: {} highest achievable_rate {}".format(
                #        rate * MINUTE_WATT, minute, soc, target_soc, charge_left, charge_now, rate_scale * MINUTE_WATT, charge_amount, round(charge_now, 2), best_rate*MINUTE_WATT, highest_achievable_rate*MINUTE_WATT))
            else:
                break
            rate_w -= 100.0

        # Stick with current rate if it doesn't matter
        if best_rate >= highest_achievable_rate and current_charge_rate >= highest_achievable_rate:
            best_rate = current_charge_rate
            if log_to:
                log_to(
                    "Low Power mode: best rate {}W is greater than highest achievable rate {}W and current rate {}W, so sticking with current rate".format(
                        dp0(best_rate * MINUTE_WATT), dp0(highest_achievable_rate * MINUTE_WATT), dp0(current_charge_rate * MINUTE_WATT)
                    )
                )

        best_rate_real = get_charge_rate_curve_cached(round(soc, 1), best_rate, soc_max, max_rate, battery_charge_power_curve_tuple, battery_rate_min, battery_temperature, battery_temperature_curve_tuple) * battery_rate_max_scaling
        if log_to:
            log_to(
                "Low Power mode: minutes left: {}, absolute: {}, SoC: {}kW, Target SoC: {}kW, Charge left: {}kW, Max rate: {}W, Min rate: {}W, Best rate: {}W, Best rate real: {}W, Battery temp {}°C".format(
                    minutes_left, abs_minutes_left, soc, target_soc, charge_left, dp0(max_rate * MINUTE_WATT), dp0(min_rate * MINUTE_WATT), dp0(best_rate * MINUTE_WATT), dp0(best_rate_real * MINUTE_WATT), battery_temperature
                )
            )
        return best_rate, best_rate_real
    else:
        return max_rate, max_rate_real


CDN_BLOCK_MARKERS = ("cloudfront", "request blocked", "the request could not be satisfied")
HTML_DOCUMENT_PREFIXES = ("<!doctype", "<html")
# Every Kraken-based provider mints its JWT through the same CDN-fronted endpoint, so an
# edge block can catch the mint as well as the queries. Unlike a query the mint has no cached
# result to fall back on: once the JWT expires every authenticated call needs a new one, so
# without a backoff a component re-mints on every poll and keeps hammering an endpoint that
# is already refusing it. Back off exponentially instead, capped so a block that lifts is
# still picked up within the hour.
TOKEN_MINT_BACKOFF_BASE_SECONDS = 300
TOKEN_MINT_BACKOFF_MAX_SECONDS = 3600
# Bound the exponent so a long block cannot grow 2 ** block_count without limit; the delay
# is capped well before this, so the clamp only stops the arithmetic running away.
TOKEN_MINT_BACKOFF_MAX_DOUBLINGS = 16
# While suppressed the mint makes no request and so logs nothing, which leaves a reader of a
# short log window unable to tell a deliberate cooldown from a bad API key. Repeat the reason
# at most this often.
TOKEN_MINT_BACKOFF_LOG_INTERVAL_SECONDS = 600


def token_mint_backoff_seconds(block_count):
    """Backoff delay in seconds after this many consecutive CDN blocks on a token mint.

    Doubles per consecutive block from TOKEN_MINT_BACKOFF_BASE_SECONDS, capped at
    TOKEN_MINT_BACKOFF_MAX_SECONDS so a block that lifts is still picked up within the hour.

    Args:
        block_count: Number of consecutive blocks so far, 1 for the first.

    Returns:
        int: Delay in seconds.
    """
    exponent = min(max(block_count - 1, 0), TOKEN_MINT_BACKOFF_MAX_DOUBLINGS)
    return min(TOKEN_MINT_BACKOFF_BASE_SECONDS * (2**exponent), TOKEN_MINT_BACKOFF_MAX_SECONDS)


def is_edge_block_body(text):
    """Return True if a 403 body is positively identifiable as a CDN/WAF error page.

    Kraken reports authentication problems as a JSON GraphQL error body (normally with
    HTTP 200) or as a 401. A 403 carrying an HTML error page - e.g. CloudFront's
    "Request blocked" - is edge rate limiting, not a credential problem, so the cached
    token must be kept rather than discarded and immediately re-minted.

    Two conditions must both hold: the body must not parse as JSON (anything the API
    itself produces is JSON), and it must look like an HTML document or name a known CDN.
    Matching on wording alone would misclassify a genuine JSON error that happens to say
    something like "access denied", which would keep an invalid token forever - the same
    permanent lockout this check exists to prevent, arrived at from the other direction.

    Detection is deliberately conservative: a 403 we cannot identify as a CDN page keeps
    the existing "refresh the token and retry" behaviour, which recovers genuinely revoked
    tokens without needing a restart.

    Args:
        text: The raw response body.

    Returns:
        bool: True if the body carries a known CDN/WAF block signature.
    """
    if not isinstance(text, str) or not text:
        return False
    try:
        json.loads(text)
    except (ValueError, TypeError):
        pass
    else:
        # A parseable JSON body came from the API, not from an edge appliance
        return False
    stripped = text.lstrip().lower()
    return stripped.startswith(HTML_DOCUMENT_PREFIXES) or any(marker in stripped for marker in CDN_BLOCK_MARKERS)


# glibc's mallopt() parameter for the arena cap (from malloc.h)
M_ARENA_MAX = -8

# Arenas glibc is allowed to create for Predbat's threads, see limit_malloc_arenas()
MALLOC_ARENA_LIMIT = 2


def _libc_function(name, argtypes, restype):
    """
    Look up a C library function by name through ctypes, or return None where it does not exist.

    Resolved against the running process (dlopen(NULL)), so it finds glibc's allocator extensions on
    Linux without naming a library, and returns None on macOS, musl (Alpine) or Windows, where the
    symbol is simply absent. Any failure to load or resolve counts as "not available", never an error.
    """
    try:
        function = getattr(ctypes.CDLL(None), name)
    except (OSError, AttributeError):
        return None
    function.argtypes = argtypes
    function.restype = restype
    return function


def malloc_trim():
    """
    Hand the heap's free pages back to the operating system, returning True if any memory was released.

    Predbat's memory use is spiky - the plan search, and above all the debug yaml dump, allocate far
    more than the steady state keeps - and glibc holds on to the freed pages rather than returning
    them, so RSS stays at the high-water mark of the last cycle and the process looks bigger than it
    is to the Home Assistant supervisor. malloc_trim(0) releases every free page it can find across
    all arenas; it takes a few milliseconds and is safe to call from any thread. A no-op that returns
    False on platforms without glibc.
    """
    trim = _libc_function("malloc_trim", [ctypes.c_size_t], ctypes.c_int)
    if trim is None:
        return False
    return bool(trim(0))


def limit_malloc_arenas(max_arenas=MALLOC_ARENA_LIMIT):
    """
    Cap the number of malloc arenas glibc may create, returning True if the cap was applied.

    glibc gives each thread that allocates its own arena, up to eight per core, and every arena keeps
    its own pool of freed-but-retained memory. Predbat runs a thread (with its own event loop and
    executor) per component, so it spreads its allocations across dozens of arenas and pays that
    retention dozens of times over. Two arenas is plenty for threads that are idle nearly all the
    time. Only affects arenas created after the call, so run it before the component threads start.
    A no-op that returns False on platforms without glibc.
    """
    mallopt = _libc_function("mallopt", [ctypes.c_int, ctypes.c_int], ctypes.c_int)
    if mallopt is None:
        return False
    return bool(mallopt(M_ARENA_MAX, max_arenas))


# Spare PV below this (W) is noise rather than a surplus worth keeping the fleet charging for.
# Matches the +-50W floor already used to decide whether an inverter is doing anything at all.
PV_SURPLUS_THRESHOLD = 50.0


def balance_inverters(intent, snapshot, balance_charge, balance_discharge, balance_crosscharge, threshold_charge, threshold_discharge, log_to=None):
    """
    Mutate the executor's per-inverter rate intent to correct fleet imbalance.

    Ported from the old Execute.balance_inverters() timer loop, with the actuator changed: instead
    of writing rate 0 and later resetting to max, this adjusts the intent that execute_plan built,
    which is then applied once. Convergence therefore returns to the executor's value rather than
    the register ceiling, so a deliberate hold can no longer be overwritten (F5 / GH#829).

    Partner selection no longer uses the original's fixed (this_inverter + 1) % num_inverters ring,
    which judged each inverter against its index neighbour and so gave different answers for the
    same fleet depending on configuration order (F7 in GH#4856). The energy guards now ask whether
    ANY other inverter qualifies, and the rate guards consult no partner at all - SoC is never used
    as a proxy for rate capability, because a fleet's fullest battery may be its weakest inverter.

    A pass only ever holds in ONE direction. The inverters share an AC bus, so holding a charger
    and a discharger together leaves the remaining units absorbing or supplying the difference,
    against guards that were each evaluated as though its own hold were the only change. Holds
    within the chosen direction are therefore applied cumulatively as well.

    Args:
        intent (dict): inverter id -> rate intent, mutated in place
        snapshot (list): per-inverter plain readings, indexed by inverter id
        balance_charge (bool): balance SoC while the fleet is charging
        balance_discharge (bool): balance SoC while the fleet is discharging
        balance_crosscharge (bool): stop one inverter charging from another
        threshold_charge (float): minimum SoC% divergence to act on during charge
        threshold_discharge (float): minimum SoC% divergence to act on during discharge
        log_to (callable): optional logger, called with a single string
    """
    num_inverters = len(snapshot)
    if num_inverters < 2:
        return
    for entry in snapshot:
        if entry["in_calibration"]:
            if log_to:
                log_to("BALANCE: an inverter is in calibration, not balancing")
            return

    socs = [entry["soc_percent"] for entry in snapshot]
    reserves = [entry["reserve_percent"] for entry in snapshot]
    battery_powers = [entry["battery_power"] for entry in snapshot]
    grid_powers = [entry.get("grid_power", 0.0) for entry in snapshot]
    charge_rates = [entry["charge_rate_now"] for entry in snapshot]
    discharge_rates = [entry["discharge_rate_now"] for entry in snapshot]

    # The rates that will actually be in force after this pass. Balancing runs BEFORE the apply
    # pass, so the measured rates above can overstate what the fleet is about to be able to do -
    # an export allocation stepping down, for instance. The capacity guards below reason about
    # these; the measured rates are kept for spotting an inverter that is already held at zero.
    effective_charge_rates = []
    effective_discharge_rates = []
    for id in range(num_inverters):
        if id in intent:
            claimed_charge = intent[id].get("charge_rate", None)
            claimed_discharge = intent[id].get("discharge_rate", None)
            effective_charge_rates.append(snapshot[id]["battery_rate_max_charge"] if claimed_charge is None else claimed_charge)
            effective_discharge_rates.append(snapshot[id]["battery_rate_max_discharge"] if claimed_discharge is None else claimed_discharge)
        else:
            # Not being written this pass (read-only, or skipped for calibration), so whatever it
            # reads now is what it will keep
            effective_charge_rates.append(charge_rates[id])
            effective_discharge_rates.append(discharge_rates[id])
    total_effective_charge_rates = sum(effective_charge_rates)
    total_effective_discharge_rates = sum(effective_discharge_rates)

    total_battery_power = sum(battery_powers)
    total_grid_power = sum(grid_powers)
    total_charge_rates = sum(charge_rates)
    total_discharge_rates = sum(discharge_rates)

    out_of_balance = any(soc != socs[0] for soc in socs)
    during_discharge = total_battery_power >= 0.0
    during_charge = total_battery_power < 0.0
    soc_min = min(socs)
    soc_max = max(socs)

    soc_low = [(soc < soc_max) and (abs(soc - soc_max) >= threshold_discharge) for soc in socs]
    soc_high = [(soc > soc_min) and (abs(soc - soc_min) >= threshold_charge) for soc in socs]

    above_reserve = [(socs[id] - reserves[id]) >= 4.0 for id in range(num_inverters)]
    # Not an existence test - that form was unreachable, since soc_high[id] already implies some
    # peer is below full. This is a per-peer filter on who can actually absorb charge.
    below_full = [socs[id] < 100.0 for id in range(num_inverters)]
    power_enough_discharge = [battery_powers[id] >= 50.0 for id in range(num_inverters)]
    power_enough_charge = [battery_powers[id] <= -50.0 for id in range(num_inverters)]

    if log_to:
        log_to(
            "BALANCE: socs {}% reserves {}% battery_powers {}W total {}W charge_rates {}W discharge_rates {}W out_of_balance {} soc_low {} soc_high {}".format(
                socs, reserves, battery_powers, total_battery_power, charge_rates, discharge_rates, out_of_balance, soc_low, soc_high
            )
        )

    # An inverter working against the fleet direction is the wasteful case: energy makes a round
    # trip through two batteries for no benefit. Correct that first and EXCLUSIVELY - a pass must
    # never pause in both directions, because the inverters share an AC bus and the remaining units
    # simply absorb or supply whatever the paused pair stopped doing. Stopping an against-direction
    # inverter needs no rate guard: it only removes load (fleet discharging) or removes draw (fleet
    # charging), so it can never leave the house short.
    # Which direction the fleet SHOULD be going is a question about the whole site's energy
    # balance, not about the sign of the battery power. Per-inverter readings cannot answer it:
    # one inverter discharging looks like less load to another, the PV wiring is not declared, and
    # an AC-coupled unit has no PV of its own. The fleet totals can, and they are trustworthy.
    #
    # Predbat's grid convention is +ve EXPORT, -ve import (apps-yaml.md, grid_power_invert), and
    # battery_power is +ve discharging, so conservation at the house gives:
    #
    #     PV + battery = load + grid  =>  load  = total_pv + total_battery_power - total_grid
    #                                     spare = total_pv - load = total_grid - total_battery_power
    #
    # Spare PV therefore falls out of grid and battery alone, with no PV attribution needed. That
    # grid sign is load-bearing - inverting it inverts every decision below - which is why it is
    # spelled out here and pinned by the random-fleet property test.
    #
    # With spare PV the fleet should be soaking it up, so an inverter DISCHARGING into the surplus
    # is the anomaly; blaming the chargers there drops PV absorption to zero during an export
    # window. Only when the batteries are net discharging with no surplus is a charging inverter
    # being fed by import or by another battery - the cross-charge worth stopping.
    spare_pv = total_grid_power - total_battery_power

    # Where the executor has claimed rates it already knows what the fleet is meant to be doing,
    # and that beats anything inferred from the meters. Without this a planned export on a sunny
    # day reads as "the fleet should be charging" - grid export exceeds what the batteries supply,
    # so spare_pv is positive - and every inverter carrying out that export looks like an anomaly
    # and gets held, cancelling it until the next plan run. The energy balance is the fallback for
    # demand and idle, where nothing has been claimed and the meters are all there is to go on.
    claimed = {intent[id].get("owner", "demand") for id in intent}
    if "export" in claimed:
        fleet_should_discharge = True
    elif "charge" in claimed:
        fleet_should_discharge = False
    else:
        fleet_should_discharge = during_discharge and spare_pv <= PV_SURPLUS_THRESHOLD

    if fleet_should_discharge:
        against_fleet = [id for id in range(num_inverters) if power_enough_charge[id] and id in intent]
    else:
        against_fleet = [id for id in range(num_inverters) if power_enough_discharge[id] and id in intent]

    if against_fleet and balance_crosscharge:
        for id in against_fleet:
            if log_to:
                log_to("BALANCE: Inverter {} is working against the fleet, holding it".format(id))
            if fleet_should_discharge:
                intent[id]["charge_rate"] = 0
            else:
                intent[id]["discharge_rate"] = 0
        # This pass is done: SoC balancing on top would be the second direction of hold that the
        # coupling between inverters rules out. When the correction is switched OFF we hold nothing
        # here, so a same-direction SoC hold below is still a single-direction pass and is allowed.
        return

    if not out_of_balance:
        return

    # SoC balancing, in the fleet's own direction only. Holds are applied cumulatively - the rate
    # guard asks "if I stop this one, can the rest cover?", so each hold has to account for the
    # ones already taken this pass or two holds each pass a check computed for one.
    if during_discharge and balance_discharge and total_discharge_rates > 0:
        held = set()
        for id in sorted((id for id in range(num_inverters) if id in intent), key=lambda id: socs[id]):
            if not soc_low[id]:
                continue
            if not (power_enough_discharge[id] or discharge_rates[id] == 0):
                continue
            # Somebody else has to have the energy to take over - any of them, not an arbitrary
            # index neighbour, which is what the (i + 1) % n ring used to ask (F7).
            # Count only the peers that could actually take over: above their reserve, and not
            # already held this pass. Asking "somebody is above reserve" and "the fleet has rate
            # headroom" separately lets two different inverters answer them - a peer with energy
            # but no rate, and a peer with rate but sitting on its reserve - and holding this one
            # then leaves only unusable capacity behind, with the shortfall coming off the grid.
            usable = sum(effective_discharge_rates[other] for other in range(num_inverters) if other != id and above_reserve[other] and other not in held)
            if (usable - 200) < total_battery_power:
                continue
            if log_to:
                log_to("BALANCE: Inverter {} is low at {}% against {}%, holding its discharge".format(id, socs[id], soc_max))
            intent[id]["discharge_rate"] = 0
            held.add(id)
    elif during_charge and balance_charge and total_charge_rates > 0:
        held = set()
        for id in sorted((id for id in range(num_inverters) if id in intent), key=lambda id: -socs[id]):
            if not soc_high[id]:
                continue
            if not (power_enough_charge[id] or charge_rates[id] == 0):
                continue
            # No "somebody else has room" check is needed here, unlike above_reserve on the
            # discharge side: soc_high[id] already requires socs[id] > soc_min, so the inverter
            # holding the minimum is strictly below this one and therefore below 100%. The
            # original's below_full[other_inverter] asked this of one arbitrary neighbour; asked
            # of the fleet it is implied, so it is not restated.
            # Same on the charge side: a battery already at 100% absorbs nothing, so its rate must
            # not be counted towards what is left to soak up the surplus.
            usable = sum(effective_charge_rates[other] for other in range(num_inverters) if other != id and below_full[other] and other not in held)
            if spare_pv > usable:
                continue
            if log_to:
                log_to("BALANCE: Inverter {} is high at {}% against {}%, holding its charge".format(id, socs[id], soc_min))
            intent[id]["charge_rate"] = 0
            held.add(id)


def allocate_export_rates(needs, max_rates, p_fleet):
    """
    Split the planned fleet export power across inverters by how much each still has to shed.

    The planner costs a specific fleet export power (the low power ladder in plan.py), so the sum
    of the allocation is pinned to it and only the split varies. An inverter already at its target
    takes none of the budget and its share spills to inverters that can still deliver, which is
    what stops fleet export power sagging below plan as inverters finish one by one.

    At full rate p_fleet equals the sum of the ceilings, so every inverter clamps at its own
    maximum and this reduces to exactly today's uniform scaling.

    Args:
        needs (list): kWh each inverter still has to shed before reaching its own target
        max_rates (list): per-inverter export ceiling in W
        p_fleet (float): planned fleet export power in W

    Returns:
    - list: export rate in W per inverter, summing to min(p_fleet, sum(max_rates))
    """
    count = len(max_rates)
    if count == 0:
        return []

    remaining = min(p_fleet, sum(max_rates))
    if remaining <= 0:
        return [0.0] * count

    # At or above full fleet power there is nothing to ration, so every inverter takes its own
    # maximum - the documented no-op outside low power mode. Handled before zero-need entries are
    # filtered out below, or their share would have nowhere to go and the sum would come up short
    # of the power the planner costed. An inverter already at its export target is stopped by that
    # target, not by having its rate held down.
    if p_fleet >= sum(max_rates):
        return list(max_rates)

    shares = [need if need > 0 else 0.0 for need in needs]
    if sum(shares) <= 0:
        # Nothing to shed anywhere - fall back to the uniform split rather than dividing by zero
        shares = [1.0] * count

    alloc = [0.0] * count
    open_set = [id for id in range(count) if shares[id] > 0]

    # Water-fill: hand out the budget in proportion to need, clamp anyone who hits their ceiling,
    # then redistribute what they could not take among those still open. Terminates because each
    # pass either closes at least one inverter or places the whole remainder.
    while remaining > 0.01 and open_set:
        share_total = sum(shares[id] for id in open_set)
        if share_total <= 0:
            break
        clamped_any = False
        budget = remaining
        for id in list(open_set):
            want = alloc[id] + budget * (shares[id] / share_total)
            if want >= max_rates[id]:
                remaining -= max_rates[id] - alloc[id]
                alloc[id] = max_rates[id]
                open_set.remove(id)
                clamped_any = True
        if not clamped_any:
            for id in open_set:
                alloc[id] += remaining * (shares[id] / share_total)
            remaining = 0.0

    return alloc
