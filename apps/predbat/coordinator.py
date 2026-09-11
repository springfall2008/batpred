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

import hashlib
import re
import secrets
import threading
from datetime import datetime, timezone

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


MAX_OPTIONS = 256


def _clean_option_list(value):
    """A select's legal option list: bounded strings, capped at MAX_OPTIONS entries, or None.

    Not vocabulary tokens - real select options look like "00:30" or "PauseCharge", which the
    lowercase-token pattern would reject outright.
    """
    if not isinstance(value, list):
        return None
    return [option for option in (_clean_string(entry) for entry in value[:MAX_OPTIONS]) if option is not None]


def _clean_measure_or_tokens(value):
    """A coverage fact: a number, a boolean, or a list of vocabulary tokens (e.g. forecast variants), or None."""
    cleaned = _clean_number(value)
    if cleaned is not None:
        return cleaned
    if isinstance(value, list):
        return [token for token in (_clean_token(entry) for entry in value) if token is not None]
    return None


# Descriptor field name -> cleaner. entity_id is required and handled separately in
# _clean_descriptor, since every other field is optional and dropped rather than disqualifying.
DESCRIPTOR_FIELD_CLEANERS = {
    "domain": _clean_token,
    "access": _clean_token,
    "unit": _clean_string,
    "device_class": _clean_string,
    "min": _clean_number,
    "max": _clean_number,
    "step": _clean_number,
    "precision": _clean_number,
    "options": _clean_option_list,
    "format": _clean_string,
}


def _clean_descriptor(value):
    """One entity descriptor: entity_id required verbatim, every other field cleaned by its own declared type.

    entities is a "clear" container, republished unredacted into debug dumps users post to public
    GitHub issues, so a field is kept only if it fits its type - free text cannot ride in on unit,
    device_class or options just because the container's name sounds safe.
    """
    if not isinstance(value, dict) or not isinstance(value.get("entity_id"), str):
        return None
    kept = {"entity_id": value["entity_id"]}
    for field, cleaner in DESCRIPTOR_FIELD_CLEANERS.items():
        if field in value and value[field] is not None:
            cleaned = cleaner(value[field])
            if cleaned is not None:
                kept[field] = cleaned
    return kept


# container name -> (redaction class, value cleaner applied per key)
CONTAINER_SPEC = {
    "hardware_ids": ("clear", _clean_string),
    "account_ids": ("pseudonym", _clean_scalar),
    "info": ("clear", _clean_string),
    "ratings": ("clear", _clean_number),
    "coverage": ("clear", _clean_measure_or_tokens),
    "entities": ("clear", _clean_descriptor),
}

# Container name sets derived from CONTAINER_SPEC's own class tags rather than hardcoded, so a
# container added there later is redacted correctly with nothing extra to keep in sync - a
# hardcoded tuple here already went stale once, when entities moved into CONTAINER_SPEC as a
# clear container and a copy of the old tuple would have kept quietly skipping it.
CLEAR_CONTAINERS = tuple(name for name, (redaction_class, _) in CONTAINER_SPEC.items() if redaction_class == "clear")
PSEUDONYM_CONTAINERS = tuple(name for name, (redaction_class, _) in CONTAINER_SPEC.items() if redaction_class == "pseudonym")

# A clear-container value shaped like an identifier rather than a measurement, once separators a
# component might have used to format one are stripped: unanchored so it matches an identifier
# embedded in a longer string ("MPAN 1234567890123"), not only a value that is nothing else.
# str() of a misfiled float ("1234567890123.0") still trips it, since the "." is one of the
# stripped separators, so casting an API number through float() cannot launder it. Underscore and
# comma are included alongside whitespace/hyphen/slash/dot - grouping digits with "_" (a Python-
# style numeric literal) or "," (thousands separators) reads identically to a human as the same
# identifier and was verified, in an adversarial pass, to otherwise slip through untouched.
DIGIT_RUN_RE = re.compile(r"\d{10,}")
SEPARATOR_RE = re.compile(r"[\s\-/.,_]")

# Field names that name a location fact by themselves, regardless of what their value looks like.
# A latitude/longitude pair cannot be recognised from one value in isolation - a plausible
# latitude and an ordinary rating overlap in numeric range - so this checks what the field is
# called instead of what it contains.
LOCATION_KEY_NAMES = frozenset({"lat", "latitude", "lon", "lng", "longitude", "postcode", "post_code"})


class Redactor:
    """Applies the catalogue's redaction classes to an assembled document.

    Pseudonymises everything in a pseudonym container (account_ids today) and anything
    cross-linking to it, substitutes those originals wherever else they appear (entity ids and
    dict keys included), and defensively pseudonymises identifier-shaped values misfiled into a
    clear container or used as a container key - including one nested inside a descriptor dict
    or a token list, not just a container's top-level scalars. Two asymmetries keep this from
    over-reaching: substring replacement is reserved for genuine identifiers (never a whole dict
    key, which is rewritten by exact match only - see _substitute_key), and a value inside
    hardware_ids is only shape-flagged when it is nothing BUT digits, since a letter-prefixed
    vendor serial with a long digit tail is that container's entire declared purpose.
    """

    # Minimum length of an original before it is substituted inside other strings; below this a
    # substring replacement would corrupt unrelated text more often than it would hide anything.
    # Applied to whole-string matches too, so a one- or two-character coincidence cannot trigger
    # a false-positive rewrite of unrelated data.
    MIN_SUBSTITUTE = 6

    def __init__(self, salt, log=None):
        """Hold the installation salt and an optional logger for misfiled values."""
        self.salt = salt
        self.log = log
        self.originals = {}
        # Subset of self.originals eligible for substring replacement inside a VALUE (never a
        # dict key - see _substitute_key) - true identifiers: account_ids values, an
        # identity-derived device_id (one whose record also carries account_ids - see _walk), and
        # anything the shape or key guard catches. A device_id with no account_ids alongside it is
        # never noted at all, so an ordinary word used as one (e.g. "charger") cannot corrupt
        # unrelated text it happens to share a substring with.
        self.substring_ok = set()
        self._substring_order = []

    def token(self, value):
        """The stable pseudonym for one value under this installation's salt."""
        digest = hashlib.sha256((self.salt + str(value)).encode("utf-8")).hexdigest()
        return "#" + digest[:8]

    def _note(self, value, substring=False):
        """Record an original so it can later be swapped for its token wherever it appears.

        Registers every numeric variant of the value too (see _numeric_variants), so an
        identifier noted as a string in one place and echoed as an int or a float in another
        still resolves to the same token in both. substring=True additionally makes it eligible
        for substring replacement inside a VALUE (never a dict key - see _substitute_key);
        otherwise it is only ever matched by whole-string equality.
        """
        text = str(value)
        token = self.token(text)
        for variant in self._numeric_variants(text):
            self.originals[variant] = token
            if substring:
                self.substring_ok.add(variant)
        return token

    def _numeric_variants(self, text):
        """Every textual form representing the same integral value as `text`.

        An identifier can be reported as a string, an int or a float depending on where a
        component got it from ("123456" in account_ids, 123456.0 echoed in a ratings field), and
        the exact-match substitution pass has to catch it whichever form it turns up in elsewhere
        - a bare digit string and its "X.0" float repr must map to the same token in BOTH
        directions, or the pair leaks each other's raw form right next to the one that got
        tokenised.
        """
        variants = {text}
        sign, body = ("-", text[1:]) if text.startswith("-") else ("", text)
        if body.isdigit():
            variants.add(sign + body + ".0")
        elif body.endswith(".0") and body[:-2].isdigit():
            variants.add(sign + body[:-2])
        return variants

    def _misfiled(self, value, strict_numeric=False):
        """Whether a value looks like an identifier rather than a measurement, a vendor code, or ordinary text.

        A NUMERIC value is judged on its INTEGER PART's digit count, not its stringified repr: a
        genuine measurement computed as a float (an efficiency, a percentage, a unit conversion)
        can easily carry ten-plus digits after the decimal point - str(1/3) is
        "0.3333333333333" - and judging the whole repr would flag every one of those as a
        misfiled identifier, silently turning a number into a string for every consumer of a
        numbers-only container. int(1234567890123.0) is still 1234567890123, so a genuinely
        misfiled float identifier is still caught. A STRING keeps the separator-stripped
        digit-run search.

        strict_numeric additionally requires a string to be nothing BUT digits before it counts -
        used only inside hardware_ids, where a letter-prefixed serial with a long digit tail (the
        industry-standard shape: "HV2160123456") is that container's entire declared purpose, not
        a misfiling; a BARE all-digit string there is still genuinely suspicious, since an MPAN
        misfiled where a serial belongs looks exactly like one.
        """
        if isinstance(value, bool):
            return False
        if isinstance(value, (int, float)):
            try:
                integer_part = abs(int(value))
            except (ValueError, OverflowError):
                return False
            return len(str(integer_part)) >= 10
        text = str(value)
        if "@" in text:
            return True
        stripped = SEPARATOR_RE.sub("", text)
        if strict_numeric:
            return len(stripped) >= 10 and stripped.isdigit()
        return bool(DIGIT_RUN_RE.search(stripped))

    def _is_location_key(self, name):
        """Whether a field name itself names a location fact, independent of its value's shape."""
        if not name:
            return False
        return name.rsplit(".", 1)[-1].lower() in LOCATION_KEY_NAMES

    def _guard_scalar(self, container, name, value):
        """Pseudonymise one scalar value if it looks misfiled or sits under a location-named field, logging where it was found."""
        if not (self._misfiled(value, strict_numeric=(container == "hardware_ids")) or self._is_location_key(name)):
            return value
        if self.log:
            label = container if container == name else "{}.{}".format(container, name)
            self.log("Warn: Coordinator: {} looks like an identifier in a clear container - pseudonymised".format(label))
        return self._note(value, substring=True)

    def _guard_value(self, container, name, value):
        """Shape-guard one clear-container value, recursing through nested dicts and lists to reach every scalar.

        Most clear containers (hardware_ids, info, ratings, coverage) hold a flat dict of
        scalars, so guarding the top-level value would be enough for those. entities is the
        exception every future reporter populates: its values are descriptor dicts (entity_id,
        unit, min, max, options, ...), so a scalar-only guard would stringify a whole dict and
        never match a shape pattern - an identifier misfiled into descriptor["max"] or
        descriptor["unit"] would then reach the published catalogue untouched. Recursing into
        dict and list values closes that gap for every present and future descriptor-shaped
        container, not just entities.
        """
        if isinstance(value, dict):
            return {key: self._guard_value(container, "{}.{}".format(name, key), entry) for key, entry in value.items()}
        if isinstance(value, list):
            return [self._guard_value(container, name, entry) for entry in value]
        return self._guard_scalar(container, name, value)

    def _guard_key(self, container, name):
        """A container dict key, pseudonymised if the key text ITSELF looks like a misfiled identifier.

        A component could key its data by a raw identifier-shaped string (hardware_ids keyed by
        the serial itself, say) rather than only ever putting one in a value; being a dict key
        rather than a value does not make it any less publishable.
        """
        if not self._misfiled(name, strict_numeric=(container == "hardware_ids")):
            return name
        if self.log:
            self.log("Warn: Coordinator: {} key '{}' looks like an identifier - pseudonymised".format(container, name))
        return self._note(name, substring=True)

    def _walk(self, node, container=None):
        """Recursively redact a node: pseudonym containers by class, clear containers via the shape guard.

        A record carrying a pseudonym container (account_ids) also has its own device_id noted as
        an original, alongside that container's values. Nothing here has to know that "meter" or
        "measures_meter" are cross-link field names: the later substitution pass rewrites any
        string equal to (or, since the id is identity-derived, containing) a noted original
        wherever it appears, so noting the owning record's device_id is what lets a cross-link
        field resolve to the same token as the record it points to, even when that field merely
        repeats the device_id rather than embedding the account identifier itself - and what
        catches the SAME device_id text left in the clear elsewhere in that record (an entity_id,
        a free-text note) rather than just tokenising the one place it was noted. A device_id with
        no pseudonym container alongside it is never noted at all, so an ordinary word used as one
        still cannot corrupt unrelated text it happens to share a substring with. Every scalar
        reached through the generic else branch - structural fields (device_id, serials, meter,
        measures_meter, ...) included - is routed through the same shape guard as a clear
        container's values, since a misfiled identifier does not stop being one just because it
        landed outside CONTAINER_SPEC.
        """
        if isinstance(node, dict):
            if isinstance(node.get("device_id"), str) and any(name in node for name in PSEUDONYM_CONTAINERS):
                self._note(node["device_id"], substring=True)
            out = {}
            for key, value in node.items():
                if key in PSEUDONYM_CONTAINERS:
                    out[key] = {self._guard_key(key, name): self._note(entry, substring=True) for name, entry in value.items()}
                elif key in CLEAR_CONTAINERS:
                    out[key] = {self._guard_key(key, name): self._guard_value(key, name, entry) for name, entry in value.items()}
                elif key in VOCAB_CONTAINERS:
                    # Vocabulary lists are clear too, and a token is free-form enough (digits
                    # are legal in the pattern) that a misfiled identifier can hide as one.
                    out[key] = [self._guard_scalar(key, "token", entry) for entry in value]
                else:
                    out[key] = self._walk(value, container=key)
            return out
        if isinstance(node, list):
            return [self._walk(entry, container=container) for entry in node]
        if container:
            return self._guard_scalar(container, container, node)
        return node

    def _exact_match(self, text):
        """The token for a noted original if `text` equals it exactly and clears the length floor, else None.

        Exact equality is what lets a device_id with no account_ids alongside it (never
        substring-eligible - see __init__) resolve nothing at all since it was never noted, what
        lets a dict key resolve only when it wholly IS a noted original (see _substitute_key), and
        what catches a non-string scalar echoed verbatim elsewhere (str(node) compared as text) -
        none of those is a substring-corruption risk, since the entire value is being replaced
        rather than a fragment of a larger string.
        """
        if len(text) >= self.MIN_SUBSTITUTE and text in self.originals:
            return self.originals[text]
        return None

    def _substitute_text(self, text):
        """Replace a VALUE string: an exact match first, then substring-eligible originals, longest first.

        Longest-first matters when one noted original is itself a substring of another (a short
        MSN inside a longer MPAN): substituting the longer one first replaces it whole, so the
        shorter original no longer appears as a fragment afterwards. Substituting the shorter one
        first would splice a token into the middle of the longer identifier and leave the
        surrounding digits of the longer one exposed on either side. Never called for a dict key -
        see _substitute_key, which does not do the substring pass at all.
        """
        exact = self._exact_match(text)
        if exact is not None:
            return exact
        for original in self._substring_order:
            if len(original) >= self.MIN_SUBSTITUTE and original in text:
                text = text.replace(original, self.originals[original])
        return text

    def _substitute_key(self, key):
        """Rewrite a dict key by exact match only - never by substring.

        A key drawn from Predbat's own naming - a section name, a container name, a descriptor
        field a component chose to call something - is short and structural enough that an
        ordinary account_ids value (nothing stops a component reporting something as mundane as
        "charge" for an account label) can coincidentally appear as a substring of one:
        substring-rewriting keys turned exactly that coincidence into an entire published section
        vanishing from the document. _guard_key (applied earlier, during _walk) is what catches a
        key that IS ITSELF shaped like a misfiled identifier; this pass only catches a key that
        happens to equal a noted original in full.
        """
        exact = self._exact_match(key)
        return exact if exact is not None else key

    def _substitute(self, node):
        """Replace every noted original wherever it appears: in string values, dict keys, and non-string scalars.

        A dict key is rewritten by _substitute_key (exact match only, never substring - see its
        docstring); a value string goes through the full exact-then-substring pass. A non-string
        scalar (an int or float identifier echoed outside its guarded container) is matched by
        exact equality against every noted numeric variant (see _numeric_variants), since a
        numeric value cannot meaningfully contain a "substring" of another number the way a
        longer string can.
        """
        if isinstance(node, dict):
            return {(self._substitute_key(key) if isinstance(key, str) else key): self._substitute(value) for key, value in node.items()}
        if isinstance(node, list):
            return [self._substitute(entry) for entry in node]
        if isinstance(node, str):
            return self._substitute_text(node)
        if isinstance(node, (int, float)) and not isinstance(node, bool):
            return self._exact_match(str(node)) or node
        return node

    def redact(self, catalogue):
        """Return a redacted copy of an assembled catalogue.

        "generated" is restored verbatim afterwards: it is the catalogue's own timestamp, stamped
        by assemble() itself rather than sourced from any component report, so it can never
        legitimately hold a cross-link or an embedded identifier - only ever a coincidental digit
        collision with an unrelated pseudonymised original, which the substitution pass would
        otherwise be free to corrupt it with.
        """
        generated = catalogue.get("generated")
        walked = self._walk(catalogue)
        self._substring_order = sorted(self.substring_ok, key=len, reverse=True)
        substituted = self._substitute(walked)
        if "generated" in catalogue:
            substituted["generated"] = generated
        return substituted


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
        self.assembled = None
        self.salt = None

    def report(self, component_name, report):
        """Validate and store one component's discovery report, replacing any previous one."""
        cleaned = validate_report(report, component_name, self.log)
        with self.lock:
            self.reports[component_name] = cleaned
        counts = ", ".join("{} {}".format(len(cleaned.get(section, [])), section) for section in SECTION_SPEC if cleaned.get(section))
        self.log("Coordinator: {} reported {}".format(component_name, counts or "nothing"))

    def assemble(self):
        """Merge every report into one catalogue, with a status per component and the observation layer.

        Unredacted: catalogue() is what consumers get. Called after phase-1 startup, which is
        the point at which every component has started or timed out.
        """
        with self.lock:
            reports = {name: report for name, report in self.reports.items()}
        catalogue = {"schema_version": SCHEMA_VERSION, "generated": datetime.now(timezone.utc).isoformat(), "components": self._component_status(reports)}
        for section in SECTION_SPEC:
            merged = []
            for name in sorted(reports):
                for record in reports[name].get(section, []):
                    entry = {"source": name}
                    entry.update(record)
                    merged.append(entry)
            catalogue[section] = merged
        catalogue["observations"] = {"conflicts": self._conflicts(catalogue), "resulting_config": self._resulting_config()}
        self.assembled = catalogue
        return catalogue

    def _component_status(self, reports):
        """A status for every component the registry knows, not only those that reported.

        A component that never reports is not an error: in this version only a handful report
        at all, so "no_report" has to read differently from "started but never answered".
        """
        components = getattr(self.base, "components", None)
        names = components.get_all() if components else sorted(reports)
        out = {}
        for name in names:
            entry = {"status": "not_configured", "reported_at": None}
            if name in reports:
                entry["status"] = "ok"
                entry["automatic"] = reports[name].get("automatic", True)
                entry["counts"] = {section: len(reports[name][section]) for section in SECTION_SPEC if reports[name].get(section)}
            elif components and components.load_error(name):
                entry["status"] = "load_error"
                entry["error"] = components.load_error(name)
            elif components and components.is_active(name):
                entry["status"] = "no_report" if components.is_alive(name) else "not_started"
            out[name] = entry
        return out

    def _conflicts(self, catalogue):
        """Collisions that today resolve silently by component ordering - recorded, never resolved."""
        conflicts = []
        serials = {}
        for record in catalogue["inverters"]:
            serial = record.get("hardware_ids", {}).get("serial")
            if serial:
                serials.setdefault(str(serial).casefold(), set()).add(record["source"])
        for serial, sources in sorted(serials.items()):
            if len(sources) > 1:
                conflicts.append({"kind": "duplicate_serial", "serial": serial, "claimed_by": sorted(sources)})
        inverter_sources = sorted({record["source"] for record in catalogue["inverters"]})
        if len(inverter_sources) > 1:
            conflicts.append({"kind": "multiple_inverter_sources", "claimed_by": inverter_sources})
        import_sources = sorted({record["source"] for record in catalogue["meters"] if record.get("direction") == "import"})
        if len(import_sources) > 1:
            conflicts.append({"kind": "multiple_import_meters", "claimed_by": import_sources})
        charger_sources = {record["source"] for record in catalogue["chargers"]}
        car_sources = {record["source"] for record in catalogue["cars"]}
        if charger_sources and (car_sources - charger_sources):
            conflicts.append({"kind": "contested_car_slots", "claimed_by": sorted(car_sources | charger_sources)})
        return conflicts

    def _resulting_config(self):
        """What apps.yaml actually ended up as, so every dump compares discovered against configured."""
        return {key: self.base.get_arg(key, None) for key in ("num_inverters", "num_cars", "inverter_type")}

    def load_salt(self):
        """The per-installation pseudonym salt, generated and stored on first use.

        Without Storage (MockBase, CLI harnesses) a per-process salt is generated instead, so
        redaction never silently falls back to an unsalted digest - a 13-digit MPAN under one of
        those is brute-forceable in seconds. ``ha`` is imported lazily here, and only once a
        Storage component actually exists, rather than at module level: ha.py pulls in
        aiohttp/requests via component_base, and a later task adds a module-level
        "from coordinator import Coordinator" to components.py, so a module-level ha import here
        would widen the startup import graph for every install - including the common case, this
        method's other branch, where there is no Storage component to talk to at all.
        """
        if self.salt:
            return self.salt
        components = getattr(self.base, "components", None)
        storage = components.get_component("storage") if components else None
        if not storage:
            self.salt = secrets.token_hex(16)
            return self.salt
        from ha import run_async

        try:
            stored = run_async(storage.load("coordinator", "salt"))
            if isinstance(stored, dict) and stored.get("salt"):
                self.salt = str(stored["salt"])
                return self.salt
        except Exception as e:
            self.log("Warn: Coordinator: could not load the pseudonym salt: {}".format(e))
        self.salt = secrets.token_hex(16)
        try:
            run_async(storage.save("coordinator", "salt", {"salt": self.salt}, format="json"))
        except Exception as e:
            self.log("Warn: Coordinator: could not save the pseudonym salt: {}".format(e))
        return self.salt

    def catalogue(self):
        """The assembled catalogue, redacted. This is what every consumer gets."""
        return Redactor(self.load_salt(), log=self.log).redact(self.assembled or self.assemble())

    def catalogue_raw(self):
        """The assembled catalogue, unredacted. In-process diagnostics only - never write this anywhere."""
        return self.assembled or self.assemble()


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
    if not isinstance(report, dict):
        log("Warn: Coordinator: {} report is a {}, not a dict - treated as empty".format(component_name, type(report).__name__))
        report = {}
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
