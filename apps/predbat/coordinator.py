# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Discovery catalogue: what each component found, assembled into one document.

Components report inverters, chargers, cars, meters, forecast providers and flexibility
programmes as plain dicts during their first successful run. The coordinator validates,
assembles and redacts them into a catalogue published in the debug YAML dump, so real user
topologies can be read. Nothing here allocates anything or writes to self.args - see
docs/superpowers/specs/2026-09-10-discovery-catalogue-design.md.

Safety comes from typed containers rather than from enumerating every field: a record is a
few structural fields plus containers that each declare what they accept and what happens
to them when shared. A container taking only numbers cannot leak a name or a credential
however the catalogue grows, so components add facts freely by choosing a container.
"""

import re
import threading

from utils import is_secret_key

SCHEMA_VERSION = 1

MAX_STRING = 64
VOCAB_RE = re.compile(r"^[a-z0-9_]{1,32}$")

# Containers whose value is a list of vocabulary tokens
VOCAB_CONTAINERS = ("functions", "capabilities", "flags", "effects")

# Descriptor attributes carried through; anything else on a descriptor is dropped
DESCRIPTOR_FIELDS = ("entity_id", "domain", "access", "unit", "device_class", "min", "max", "step", "options", "format", "precision")

SECTION_SPEC = {
    "inverters": {"structural": ("device_id", "inverter_type", "control", "composition", "measures_meter", "serials"), "sub_records": ()},
    "chargers": {"structural": ("device_id", "serves_cars"), "sub_records": ()},
    "cars": {"structural": ("device_id", "charged_by"), "sub_records": ()},
    "meters": {"structural": ("device_id", "direction"), "sub_records": ("tariff",)},
    "forecasts": {"structural": ("device_id", "kind"), "sub_records": ()},
    "programmes": {"structural": ("device_id", "kind", "meter"), "sub_records": ()},
}


def _clean_string(value):
    """A vendor descriptor string, or None if it is not one.

    Length-capped and free of "@" so an address, an email or a pasted blob cannot ride in on
    a descriptive field. Vendor model and firmware strings are comfortably inside the cap.
    """
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > MAX_STRING or "@" in value:
        return None
    return value


def _clean_number(value):
    """A numeric or boolean fact, or None. Strings are refused however numeric they look."""
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return value
    return None


def _clean_token(value):
    """A vocabulary token (lower case, no spaces), or None."""
    return value if isinstance(value, str) and VOCAB_RE.match(value) else None


def _clean_scalar(value):
    """Any scalar, for account_ids - the container pseudonymises whatever it holds, so its type is open."""
    return value if isinstance(value, (str, int, float, bool)) else None


def _clean_descriptor(value):
    """One entity descriptor, keeping only known descriptor fields, or None if it lacks a string entity_id."""
    if not isinstance(value, dict) or not isinstance(value.get("entity_id"), str):
        return None
    kept = {}
    for field in DESCRIPTOR_FIELDS:
        if field in value and value[field] is not None:
            kept[field] = value[field]
    return kept


# container name -> (redaction class, value cleaner applied per key)
CONTAINER_SPEC = {
    "hardware_ids": ("clear", _clean_string),
    "account_ids": ("pseudonym", _clean_scalar),
    "info": ("clear", _clean_string),
    "ratings": ("clear", _clean_number),
    "coverage": ("clear", _clean_number),
    "entities": ("clear", _clean_descriptor),
}


class Coordinator:
    """Collects component discovery reports and assembles them into one catalogue.

    Thread-safe: components report from their own threads while assembly runs on the main
    thread at startup.
    """

    def __init__(self, base):
        """Create an empty coordinator."""
        self.base = base
        self.log = base.log
        self.lock = threading.Lock()
        self.reports = {}

    def report(self, component_name, report):
        """Validate and store one component's discovery report, replacing any previous one."""
        cleaned = validate_report(report, component_name, self.log)
        with self.lock:
            self.reports[component_name] = cleaned
        counts = ", ".join("{} {}".format(len(cleaned.get(section, [])), section) for section in SECTION_SPEC if cleaned.get(section))
        self.log("Coordinator: {} reported {}".format(component_name, counts or "nothing"))


def _validate_container(container_name, value, component_name, section, log):
    """Clean one container's contents against its declared type, dropping and logging what does not fit.

    The credential guard applies to every container, not only the dict-shaped ones in
    CONTAINER_SPEC: an entities descriptor is keyed by Predbat's standard name, so that key is
    checked exactly like a hardware_ids or ratings key, and a vocabulary token stands in as its
    own key since a token list has no separate key/value split. No container can accept a key
    whose name trips is_secret_key(), however it is nested.
    """
    if container_name in VOCAB_CONTAINERS:
        if not isinstance(value, list):
            return None
        out = []
        for entry in value:
            token = _clean_token(entry)
            if token is None:
                continue
            if is_secret_key(token):
                log("Warn: Coordinator: {} {} {} token '{}' looks like a credential - refused".format(component_name, section, container_name, token))
                continue
            out.append(token)
        return out
    if not isinstance(value, dict):
        return None
    _, cleaner = CONTAINER_SPEC[container_name]
    out = {}
    for key, entry in value.items():
        if not isinstance(key, str):
            continue
        if is_secret_key(key):
            log("Warn: Coordinator: {} {} field '{}' looks like a credential - refused".format(component_name, section, key))
            continue
        cleaned = cleaner(entry)
        if cleaned is None:
            log("Warn: Coordinator: {} {}.{} value does not fit the container's type - dropped".format(component_name, container_name, key))
            continue
        out[key] = cleaned
    return out


def _validate_record(record, section, component_name, log):
    """Clean one record: its structural fields, its containers and any nested sub-records."""
    if not isinstance(record, dict) or not isinstance(record.get("device_id"), str):
        log("Warn: Coordinator: {} {} record without a device_id - dropped".format(component_name, section))
        return None
    spec = SECTION_SPEC[section]
    out = {}
    for field in spec["structural"]:
        if field not in record or record[field] is None:
            continue
        value = record[field]
        if isinstance(value, list):
            out[field] = [entry for entry in value if isinstance(entry, str) and len(entry) <= MAX_STRING]
        elif isinstance(value, bool) or isinstance(value, (int, float)):
            out[field] = value
        elif isinstance(value, str) and len(value) <= MAX_STRING:
            out[field] = value
    for container_name in list(CONTAINER_SPEC) + list(VOCAB_CONTAINERS):
        if container_name in record:
            cleaned = _validate_container(container_name, record[container_name], component_name, section, log)
            if cleaned:
                out[container_name] = cleaned
    for sub_name in spec["sub_records"]:
        sub = record.get(sub_name)
        if isinstance(sub, dict):
            sub_out = {}
            for container_name in list(CONTAINER_SPEC) + list(VOCAB_CONTAINERS):
                if container_name in sub:
                    cleaned = _validate_container(container_name, sub[container_name], component_name, section, log)
                    if cleaned:
                        sub_out[container_name] = cleaned
            if sub_out:
                out[sub_name] = sub_out
    return out


def validate_report(report, component_name, log):
    """Return a cleaned copy of one component's report - never raises, drops what does not fit."""
    cleaned = {"schema_version": SCHEMA_VERSION, "automatic": bool(report.get("automatic", True))}
    for section in SECTION_SPEC:
        records = []
        for record in report.get(section, []) or []:
            validated = _validate_record(record, section, component_name, log)
            if validated:
                records.append(validated)
        if records:
            cleaned[section] = records
    return cleaned
