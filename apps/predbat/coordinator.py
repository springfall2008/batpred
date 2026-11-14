# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Discovery catalogue: what each component found, assembled into one document.

Components report inverters, chargers, cars, meters, forecast providers and flexibility
programmes as plain dicts during their first successful run. The coordinator validates,
assembles and redacts them into a catalogue published in the debug YAML dump, so real user
topologies can be read - see docs/superpowers/specs/2026-09-10-discovery-catalogue-design.md.

With the apps.yaml switch discovery_automatic on, the coordinator also configures the inverters of
components that opt in (allow_automatic()): configure() builds per-slot bindings from their reports and
replays them into args. bindings.py makes every decision about slots and values; this module holds the
state, the triggers and the orchestration. With the switch off nothing here writes to args - see
docs/superpowers/specs/2026-11-14-discovery-automatic-inverter-config-design.md.

Safety comes from typed containers rather than from enumerating every field: a record is a
few structural fields plus containers that each declare what they accept and what happens
to them when shared. A container taking only numbers cannot leak a name or a credential
however the catalogue grows, so components add facts freely by choosing a container.
"""

import copy
import re
import threading
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from bindings import BINDING_SETTINGS, COMPACT_SETTINGS, SUMMED_SETTINGS, build_bindings, replay_bindings, slot_map, slot_value
from utils import is_secret_key

SCHEMA_VERSION = 2

# The strings get_arg() reads as True for a boolean setting (userinterface.py)
TRUE_STRINGS = ("on", "true", "yes", "enabled", "enable", "connected")

# Settings indexed by slot, so a whole-value write to one can be traced to the slots whose entry changed
SLOT_INDEXED_SETTINGS = (BINDING_SETTINGS - set(COMPACT_SETTINGS)) | {"inverter_type"}

# Marks a setting that was absent from args before a pass wrote it, so a failed pass can delete it again
_ABSENT = object()

MAX_STRING = 64
VOCAB_RE = re.compile(r"^[a-z0-9_]{1,32}$")

# Containers whose value is a list of vocabulary tokens
VOCAB_CONTAINERS = ("functions", "flags", "effects")

# The seven INVERTER_DEF fields that describe behaviour rather than whether an entity exists.
# A record's capabilities dict may hold only these.
CAPABILITY_KEYS = (
    "support_charge_freeze",
    "support_discharge_freeze",
    "support_feedin_first",
    "can_span_midnight",
    "charge_discharge_with_rate",
    "charge_control_immediate",
    "target_soc_used_for_discharge",
)

# Behaviour fields added to INVERTER_DEF after the record vocabulary was written, each with the value
# inverter.py uses when a row leaves it out. A record may state one, but leaving it out is not a gap:
# the field takes base's value, or this default - the rate_step_percent_of_capacity pattern. Only the
# rows that need one (EnphaseCloud and TESLA set reserve_above_soc_charges_from_grid, TESLA alone sets
# reserve_full_only_above_80, SIG alone sets reserve_via_discharge_rate) have to state it.
OPTIONAL_CAPABILITY_DEFAULTS = {"reserve_above_soc_charges_from_grid": False, "reserve_full_only_above_80": False, "reserve_via_discharge_rate": False}

# INVERTER_DEF presence flags -> the settings that must be bound to a real rw entity for the flag to be
# True. inverter.py creates a dummy entity for exactly these settings when the flag is False
# (inverter.py:612-655), and turns has_timed_pause off at runtime when no pause_mode entity exists.
PRESENCE_FLAGS = {
    "has_charge_enable_time": ("scheduled_charge_enable",),
    "has_discharge_enable_time": ("scheduled_discharge_enable",),
    "has_reserve_soc": ("reserve",),
    "has_target_soc": ("charge_limit",),
    "has_idle_time": ("idle_start_time", "idle_end_time"),
    "has_timed_pause": ("pause_mode",),
}

# The value a protocol field takes when the setting it is read from is not bound at all, so the field
# does not apply to this device. current_dp matches inverter.py's own .get() default.
NOT_APPLICABLE_DEFAULTS = {
    "charge_time_entity_is_option": True,
    "charge_time_format": "HH:MM:SS",
    "clock_time_format": "%Y-%m-%dT%H:%M:%S",
    "current_dp": 1,
}

ACCESS_VALUES = ("rw", "r")

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


def _clean_percent(value):
    """A percentage above 0 and up to 100, or None. Bools are refused, unlike _clean_number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if 0 < value <= 100 else None


def _clean_token(value):
    """A vocabulary token (lower case, no spaces), or None."""
    return value if isinstance(value, str) and VOCAB_RE.match(value) else None


def _clean_scalar(value):
    """Any scalar, for account_ids - an identifier may be a string or a number - with strings held to MAX_STRING.

    The cap is the container's value-side backstop: identifiers are published as reported, and the
    credential check is by key name only, so a blob pasted under an innocent key (a token, say) is
    dropped here like an over-long info string. Every real identifier fits - an MPAN, an account
    number, a 36-character UUID.
    """
    if isinstance(value, str):
        return value if len(value) <= MAX_STRING else None
    return value if isinstance(value, (int, float, bool)) else None


def _clean_bool(value):
    """A True/False fact, or None. Numbers are refused: 1 is not a capability statement."""
    return value if isinstance(value, bool) else None


def _clean_descriptor_value(value):
    """A descriptor's fixed stand-in value: a number, a bool, or a string passing the info-string guard.

    entities is published unredacted in dumps users post to public issues, so a stand-in string is held
    to the same length cap and "@" ban as an info string - free text cannot ride in as a "value".
    """
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return value
    return _clean_string(value)


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


# Descriptor field name -> cleaner. access, entity_id and value are required/alternative fields and are
# handled in _clean_descriptor; every field here is optional and dropped rather than disqualifying.
DESCRIPTOR_FIELD_CLEANERS = {
    "domain": _clean_token,
    "unit": _clean_string,
    "device_class": _clean_string,
    "min": _clean_number,
    "max": _clean_number,
    "step": _clean_number,
    "step_percent_of_capacity": _clean_percent,
    "precision": _clean_number,
    "options": _clean_option_list,
    "format": _clean_string,
    "invert": _clean_bool,
}


def _clean_descriptor(value):
    """One entity descriptor: access required, exactly one of entity_id and value, every other field cleaned by its own declared type.

    entities is a "clear" container, republished unredacted into debug dumps users post to public
    GitHub issues, so a field is kept only if it fits its type - free text cannot ride in on unit,
    device_class or options just because the container's name sounds safe.

    access says whether Predbat writes the setting ("rw") or only reads it ("r"); the coordinator will
    configure from it, so a descriptor without one is dropped rather than guessed. entity_id binds the
    setting to an HA entity; value is a fixed stand-in where no entity exists (a PV-less inverter's
    pv_power of 0). Both, or neither, is ambiguous and dropped.
    """
    if not isinstance(value, dict) or value.get("access") not in ACCESS_VALUES:
        return None
    has_entity = isinstance(value.get("entity_id"), str)
    has_value = value.get("value") is not None
    if has_entity == has_value:
        return None
    if has_entity:
        kept = {"entity_id": value["entity_id"], "access": value["access"]}
    else:
        stand_in = _clean_descriptor_value(value["value"])
        if stand_in is None:
            return None
        kept = {"value": stand_in, "access": value["access"]}
    for field, cleaner in DESCRIPTOR_FIELD_CLEANERS.items():
        if field in value and value[field] is not None:
            cleaned = cleaner(value[field])
            if cleaned is not None:
                kept[field] = cleaned
    return kept


# container name -> value cleaner applied per key. Every container is published as reported: identifiers
# are not secret (maintainer decision, 2 Nov 2026), so there is no pseudonymised class any more - see Redactor.
CONTAINER_SPEC = {
    "hardware_ids": _clean_string,
    "account_ids": _clean_scalar,
    "info": _clean_string,
    "ratings": _clean_number,
    "coverage": _clean_measure_or_tokens,
    "entities": _clean_descriptor,
    "capabilities": _clean_bool,
}

# Containers whose keys are a closed set - anything else is dropped and logged
CONTAINER_KEYS = {"capabilities": CAPABILITY_KEYS + tuple(OPTIONAL_CAPABILITY_DEFAULTS)}

# An email address anywhere in a string: the one kind of personal data the published catalogue withholds
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+")
EMAIL_PLACEHOLDER = "<email>"


class Redactor:
    """Removes email addresses from an assembled catalogue - the only thing the published document withholds.

    Identifiers are not secret (maintainer decision, 2 Nov 2026): hardware serials, account, site,
    plant and system ids, charger ids and location are all published exactly as the components
    reported them. Replacing them with tokens broke the records for the very reader they exist for - a
    tokenised entity id is a dead end for anything that re-binds configuration from a record - and
    an identifier shape guard kept mistaking ordinary values (a Sigenergy system-id slug, a firmware
    build hash) for misfiled ones.

    Credentials never get this far: validation refuses any field whose name reads as one
    (is_secret_key), in every container. What is left to withhold is an email address a component
    put into a value or a dict key - a login identifier in a device_id, say - which this replaces
    with EMAIL_PLACEHOLDER wherever it appears.
    """

    def redact(self, catalogue):
        """Return a copy of an assembled catalogue with every email address replaced."""
        return self._walk(catalogue)

    def _walk(self, node):
        """Recursively copy a node, scrubbing every string - dict keys included."""
        if isinstance(node, dict):
            return {self._scrub(key) if isinstance(key, str) else key: self._walk(value) for key, value in node.items()}
        if isinstance(node, list):
            return [self._walk(entry) for entry in node]
        if isinstance(node, str):
            return self._scrub(node)
        return node

    @staticmethod
    def _scrub(text):
        """`text` with each email address in it replaced by EMAIL_PLACEHOLDER."""
        return EMAIL_RE.sub(EMAIL_PLACEHOLDER, text)


class Coordinator:
    """Collects component discovery reports and assembles them into one catalogue.

    Thread-safe: components report from their own threads while assembly runs on the main
    thread at startup.

    Two locks, always taken in this order and never the other way round: configure_lock (an RLock,
    held for a whole configure() pass - build, replay and check) and then lock (held only briefly, to
    read or replace the stored reports and opt-ins). report() and allow_automatic() release lock before
    they call configure(), so a component thread reporting while another thread is part way through a
    pass waits for that pass to finish and then runs its own.
    """

    def __init__(self, base):
        """Create an empty coordinator."""
        self.base = base
        self.log = base.log
        self.lock = threading.Lock()
        self.configure_lock = threading.RLock()
        # Automatic configuration: component name -> its allow_automatic() options, the last bindings
        # built (None before the first pass) and their device_id -> slot map
        self.automatic = {}
        self.bindings = None
        self.slot_map = {}
        # The live SUMMED_SETTINGS values on the first pass, after auto_config() resolved their re:
        # patterns and before anything was replayed - stands in for the snapshot's raw entries (see configure())
        self.summed_baseline = None
        self.warned = set()
        self.reports = {}
        # UTC ISO-8601 timestamp of the last report(), by component name - surfaced as
        # "reported_at" for a status "ok" component in _component_status(). Kept apart from
        # self.reports rather than folded into the cleaned report dict, since it describes when
        # the coordinator heard from the component, not anything the component itself reported.
        self.reported_at = {}
        # The most recently assembled document, kept for in-process introspection only - neither
        # catalogue() nor catalogue_raw() read this any more, since both re-assemble on every call
        # (see catalogue()'s docstring for why a frozen snapshot missed every report filed after
        # the original startup-barrier assemble() call).
        self.assembled = None

    def report(self, component_name, report):
        """Validate and store one component's discovery report, replacing any previous one."""
        cleaned = validate_report(report, component_name, self.log)
        with self.lock:
            changed = self.reports.get(component_name) != cleaned
            self.reports[component_name] = cleaned
            self.reported_at[component_name] = datetime.now(timezone.utc).isoformat()
            opted_in = component_name in self.automatic
        counts = ", ".join("{} {}".format(len(cleaned.get(section, [])), section) for section in SECTION_SPEC if cleaned.get(section))
        self.log("Coordinator: {} reported {}".format(component_name, counts or "nothing"))
        # Outside lock: configure() takes configure_lock first and lock only briefly inside it
        if opted_in and changed:
            self.configure()

    def _automatic_enabled(self):
        """Whether the apps.yaml switch discovery_automatic is on. False on a base without get_arg (the CLI harnesses)."""
        get_arg = getattr(self.base, "get_arg", None)
        if not callable(get_arg):
            return False
        try:
            value = get_arg("discovery_automatic", False, indirect=False)
        except Exception:
            return False
        if isinstance(value, str):
            return value.strip().lower() in TRUE_STRINGS
        return value is True

    def allow_automatic(self, component_name, skip_settings=(), keep_user_settings=(), include_pv_only=False):
        """Opt a component in to automatic configuration by the coordinator; returns whether the coordinator will configure it.

        False, recording nothing, while discovery_automatic is off - the component then configures itself as
        before. Otherwise the options are stored under the component's name and a pass runs at once.
        skip_settings are left exactly as args hold them; keep_user_settings yield to an apps.yaml value at the
        slot; include_pv_only lets records with no inverter_type contribute pv_power and pv_today.
        """
        if not self._automatic_enabled():
            return False
        options = {"skip_settings": frozenset(skip_settings or ()), "keep_user_settings": frozenset(keep_user_settings or ()), "include_pv_only": bool(include_pv_only)}
        with self.lock:
            self.automatic[component_name] = options
        self.configure()
        return True

    def configure(self):
        """One automatic configuration pass: build the bindings, replay what differs into args, then check. Never raises.

        A no-op while discovery_automatic is off, or on a base without the apps.yaml snapshot, args or set_arg.
        Runs on whichever thread triggered it, under configure_lock for the whole pass. A pass with nothing new
        writes nothing and logs nothing.

        bindings.py reads a manual inverter's SUMMED_SETTINGS entries from the snapshot it is given, but
        args_from_apps_yaml is taken before Predbat resolves re: patterns. So the first pass keeps a copy of those
        settings' live values (resolved by then, nothing yet replayed) and every pass hands bindings.py the snapshot
        with that copy in place of its own summed entries.

        A failure is logged as a warning and the writes the pass had already made are undone, so args are as they
        were before it; the bindings stay as the last good pass left them.
        """
        if not self._automatic_enabled():
            return
        with self.configure_lock:
            before = {}
            try:
                self._configure_pass(before)
            except Exception as e:
                self.log("Warn: Coordinator: automatic configuration failed: {}".format(e))
                self._undo(before)

    def _configure_pass(self, before):
        """The body of configure(): build, replay, then log and check what changed. `before` collects each written setting's prior value."""
        base = self.base
        snapshot = getattr(base, "args_from_apps_yaml", None)
        args = getattr(base, "args", None)
        if not isinstance(snapshot, dict) or not isinstance(args, dict) or not callable(getattr(base, "set_arg", None)):
            return
        if self.summed_baseline is None:
            self.summed_baseline = {setting: copy.deepcopy(args[setting]) for setting in SUMMED_SETTINGS if setting in args}
        apps_yaml = {key: value for key, value in snapshot.items() if key not in SUMMED_SETTINGS}
        apps_yaml.update(copy.deepcopy(self.summed_baseline))

        with self.lock:
            automatic = dict(self.automatic)
            reports = dict(self.reports)
        sources = [dict(automatic[name], name=name, records=list(reports.get(name, {}).get("inverters", []))) for name in sorted(automatic)]

        bindings, warnings = build_bindings(apps_yaml, args, sources, self.bindings)
        for warning in warnings:
            self._warn_once(warning)
        written = replay_bindings(bindings, apps_yaml, args, self._recording_set_arg(before))
        changed_slots = self._changed_slots(written, before, bindings)
        self.bindings = bindings
        self.slot_map = slot_map(bindings)
        # Committed: from here on a failure leaves this pass's writes in place, matching the bindings just stored
        before.clear()
        # Every pass, not only one that wrote: a component not opted in never triggers a pass, so one that reports
        # after the opted-in components are configured is only seen here; _warn_once keeps a repeat silent
        self._warn_unconverted(reports, automatic)
        if not written:
            return

        for index in sorted(changed_slots):
            entry = bindings["inverters"][index]
            self.log("Coordinator: inverter slot {} configured from {} ({})".format(index, entry["source"], entry["device_id"]))
        self._check(written, bindings, sources)

    def _warn_once(self, text):
        """Log a warning the first time its text is seen in this coordinator's life; passes repeat them while the situation lasts."""
        if text not in self.warned:
            self.warned.add(text)
            self.log("Warn: Coordinator: " + text)

    def _recording_set_arg(self, before):
        """base.set_arg, recording each setting's value before this pass first writes it into `before`."""
        args = self.base.args
        set_arg = self.base.set_arg

        def record(arg, value, index=None):
            """Note arg's prior value once, then write through to base.set_arg."""
            if arg not in before:
                before[arg] = copy.deepcopy(args[arg]) if arg in args else _ABSENT
            if index is None:
                set_arg(arg, value)
            else:
                set_arg(arg, value, index=index)

        return record

    def _undo(self, before):
        """Put back the args a failed pass wrote, directly rather than through set_arg, which may be what failed."""
        try:
            for arg, value in before.items():
                if value is _ABSENT:
                    self.base.args.pop(arg, None)
                else:
                    self.base.args[arg] = value
            # What set_arg itself would have done after changing args (userinterface.py)
            invalidate = getattr(self.base, "_invalidate_log_secret_pattern", None)
            if before and callable(invalidate):
                invalidate()
        except Exception as e:
            self.log("Warn: Coordinator: could not undo a failed automatic configuration pass: {}".format(e))

    def _changed_slots(self, written, before, bindings):
        """The discovered slots this pass wrote: an indexed write's slot, and for a whole-value write to a slot-indexed setting each slot whose entry changed.

        A compact summed list (load_today and the like) leaves unbound slots out, so its index is not a slot
        and its writes name no slot here.
        """
        entries = bindings.get("inverters", [])
        slots = set()
        for setting, index in written:
            if setting in COMPACT_SETTINGS:
                continue
            if index is not None:
                candidates = [index]
            elif setting in SLOT_INDEXED_SETTINGS:
                old = before.get(setting)
                old = None if old is _ABSENT else old
                new = self.base.args.get(setting)
                candidates = [slot for slot in range(len(entries)) if slot_value(old, slot) != slot_value(new, slot)]
            else:
                candidates = []
            slots.update(slot for slot in candidates if slot < len(entries) and entries[slot].get("origin") == "discovered")
        return slots

    def _warn_unconverted(self, reports, automatic):
        """Warn about a component that configures inverters itself (automatic, with a driven inverter) alongside the opted-in ones."""
        if not automatic:
            return
        opted_in = ", ".join(sorted(automatic))
        for name in sorted(reports):
            report = reports[name]
            if name in automatic or not report.get("automatic"):
                continue
            if any(isinstance(record.get("inverter_type"), str) and record["inverter_type"].strip() for record in report.get("inverters", [])):
                self._warn_once("{} configures inverters itself and is not yet handled by discovery - running it alongside {} is not supported".format(name, opted_in))

    def _check(self, written, bindings, sources):
        """Call check_automatic_config(slots) on each opted-in component a write was attributed to, if it is running and defines one.

        A write at index i is attributed to the source of slot i. A whole-value write, or one to a compact summed
        list (whose index is not a slot), is attributed to every opted-in source. slots is the slot of each of the
        component's records with an inverter_type, in report order, None for one that got no slot. A checker that
        raises is logged and the pass stands.
        """
        entries = bindings.get("inverters", [])
        names = set()
        for setting, index in written:
            if index is None or setting in COMPACT_SETTINGS:
                names.update(source["name"] for source in sources)
            elif index < len(entries) and entries[index].get("source"):
                names.add(entries[index]["source"])
        get_component = getattr(getattr(self.base, "components", None), "get_component", None)
        if not callable(get_component):
            return
        for source in sources:
            if source["name"] not in names:
                continue
            component = get_component(source["name"])
            checker = getattr(component, "check_automatic_config", None) if component is not None else None
            if not callable(checker):
                continue
            slots = [self.slot_map.get(record["device_id"]) for record in source["records"] if isinstance(record.get("inverter_type"), str) and record["inverter_type"].strip()]
            try:
                checker(slots)
            except Exception as e:
                self.log("Warn: Coordinator: {} automatic configuration check failed: {}".format(source["name"], e))

    def assemble(self):
        """Merge every report into one catalogue, with a status per component and the observation layer.

        Unredacted: catalogue() is what consumers get. Cheap - a merge over a handful of dicts
        under a lock briefly held just to copy self.reports/self.reported_at - so it is safe to
        call on every catalogue()/catalogue_raw() request, not only once at the startup barrier
        (predbat.py, straight after every component has started or timed out): a component keeps
        reporting for the rest of the process's life (a retry, a rediscovered device, a changed
        tariff), and only re-assembling on every read lets that later work ever reach a dump.
        """
        with self.lock:
            reports = {name: report for name, report in self.reports.items()}
            reported_at = dict(self.reported_at)
        # Read once: configure() replaces both attributes rather than changing them in place
        bindings = self.bindings
        slots = self.slot_map
        catalogue = {"schema_version": SCHEMA_VERSION, "generated": datetime.now(timezone.utc).isoformat(), "components": self._component_status(reports, reported_at)}
        for section in SECTION_SPEC:
            merged = []
            for name in sorted(reports):
                for record in reports[name].get(section, []):
                    entry = {"source": name}
                    entry.update(record)
                    if section == "inverters" and entry.get("device_id") in slots:
                        entry["slot"] = slots[entry["device_id"]]
                    merged.append(entry)
            catalogue[section] = merged
        catalogue["observations"] = {"conflicts": self._conflicts(catalogue), "resulting_config": self._resulting_config()}
        if bindings is not None:
            catalogue["observations"]["bindings"] = copy.deepcopy(bindings)
        self.assembled = catalogue
        return catalogue

    def _component_status(self, reports, reported_at):
        """A status for every component that reported, plus every one that was expected to and did not.

        Deliberately NOT a status for every component in the registry. COMPONENT_LIST holds 30
        components and five of them can report at all, so listing the registry made the map ~30
        entries of which a handful said anything - the rest were a static list already readable in
        components.py, restated on every dump and in sensor.predbat_discovery's attributes. Two
        kinds of entry are dropped, and they are dropped for the same reason: silence that carries
        no information.

        - A component the user never configured. Its absence from the map IS its status.
        - An active component with no build_discovery() at all - storage, web, db, ha and most of
          the rest. It is working exactly as intended and is never going to report, so "no_report"
          against it says nothing. This is what separates it from the entry below.

        What survives is the set worth reading: everything that reported, and everything that was
        capable of reporting and did not. That second group is the whole point - a component
        carrying a build_discovery() that never filed is either broken or starved, and it reads as
        an anomaly here precisely because its silent, reporter-less neighbours are gone.

        A "load_error" component is kept whatever it can do: construction failed, so there is no
        instance to ask about a reporter, and a component that would not build is an anomaly on any
        reading. Its status deliberately carries no message. components.load_error() is the raw
        str() of whatever exception construction raised, which is free text this document has no
        way to bound: every other value here reaches the catalogue through a typed container that
        cannot hold a name or a credential however the schema grows, and one unbounded string
        outside that boundary would be the single field a future component could leak a URL or an
        account value through into a dump attached to a public issue. The message is not lost -
        components.initialize() logs it (with a traceback for anything but an ImportError) and
        inverter_source_status() puts it on the component-status entity - so a maintainer reading
        a bug report still has it, just not from this document.

        reported_at is a component-name -> ISO-8601 UTC timestamp snapshot, taken under the same
        lock as reports so the two agree with each other; it is only ever populated for a status
        "ok" component - one the coordinator has actually heard from. "automatic" is included only
        when the component's own report actually carried one (see validate_report()) - a component
        with no such concept (Solcast: solar-forecast sourcing is a plain apps.yaml choice, never
        something this catalogue auto-wires) must not have the catalogue claim `automatic: true`
        for it just because every OTHER field here defaults to something.
        """
        components = getattr(self.base, "components", None)
        names = components.get_all() if components else sorted(reports)
        out = {}
        for name in names:
            if name in reports:
                entry = {"status": "ok", "reported_at": reported_at.get(name)}
                if "automatic" in reports[name]:
                    entry["automatic"] = reports[name]["automatic"]
                entry["counts"] = {section: len(reports[name][section]) for section in SECTION_SPEC if reports[name].get(section)}
            elif components and components.load_error(name):
                entry = {"status": "load_error", "reported_at": None}
            elif components and components.is_active(name) and _can_report(components.get_component(name)):
                entry = {"status": "no_report" if components.is_alive(name) else "not_started", "reported_at": None}
            else:
                continue
            out[name] = entry
        return out

    def _conflicts(self, catalogue):
        """Collisions that today resolve silently by component ordering - recorded, never resolved.

        A duplicate_serial claim covers every serial a record speaks for, not only its own
        hardware_ids.serial: a gateway record names the gateway in hardware_ids and the batteries
        it fronts in the structural `serials` list (see gecloud.py's build_discovery). GE Cloud in
        gateway composition and GivTCP therefore claim the SAME physical battery under different
        record identities, which is exactly the collision this observation exists to count - and
        grouping on hardware_ids alone missed it, since the two never share a top-level serial.
        """
        conflicts = []
        serials = {}
        for record in catalogue["inverters"]:
            claimed = [record.get("hardware_ids", {}).get("serial")] + list(record.get("serials") or [])
            for serial in claimed:
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

    def catalogue(self):
        """The assembled catalogue with email addresses scrubbed (see Redactor). This is what every consumer gets.

        Re-assembles on every call rather than reusing whatever self.assembled last held. A report
        arrives from a component's own thread at any point in the process's life - a retry after a
        transient failure, a rediscovered inverter, a changed tariff or vehicle, a newly-discovered
        forecast site - and assemble() was previously called exactly once, at the startup barrier
        (see predbat.py), so none of that later work could ever reach a dump: catalogue() returned
        the same frozen document for the rest of the run. assemble() is a merge over a handful of
        dicts under a lock briefly held to copy self.reports, so redoing it on every debug dump or
        publish() call - its only two callers - costs milliseconds, not a measurable resource.
        """
        return Redactor().redact(self.assemble())

    def catalogue_raw(self):
        """The assembled catalogue as reported, email addresses included. In-process diagnostics only - never write this anywhere.

        Re-assembles on every call, consistent with catalogue() - see its docstring.
        """
        return self.assemble()

    def publish(self):
        """Publish a SUMMARY of the redacted catalogue as an entity, for HA users to glance at.

        Deliberately not the full catalogue: folding every section straight into the entity's
        attributes (as an earlier version of this method did via attributes.update(catalogue))
        puts every inverter's full entity-descriptor map into a Home Assistant entity's
        attributes, which the recorder stores to disk - tens of kilobytes of JSON per snapshot
        for an install with only a few inverters, bloating the user's database for data nothing
        reads back out of the entity. The full document already reaches its intended consumer
        through the debug dump (create_debug_yaml() -> coordinator.catalogue()); the web viewer
        that will eventually want the whole thing is out of scope for this release and, when
        built, should ask the coordinator directly rather than parse it back out of an HA
        entity's attributes - the better coupling anyway. Do not "helpfully" restore the section
        record lists here.

        The state is still the total record count, and the attributes still carry enough to be
        useful at a glance: schema_version, generated, a per-section count map, the components
        status map (small, and the first thing worth checking), and observations.conflicts
        (small, and the thing a maintainer most wants to spot) - never a section's record list.
        """
        catalogue = self.catalogue()
        counts = {section: len(catalogue.get(section, [])) for section in SECTION_SPEC}
        attributes = {
            "friendly_name": "Predbat discovery",
            "icon": "mdi:sitemap",
            "schema_version": catalogue.get("schema_version"),
            "generated": catalogue.get("generated"),
            "counts": counts,
            "components": catalogue.get("components", {}),
            "observations": {"conflicts": catalogue.get("observations", {}).get("conflicts", [])},
        }
        self.base.dashboard_item("sensor.{}_discovery".format(self.base.prefix), state=sum(counts.values()), attributes=attributes)


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
    cleaner = CONTAINER_SPEC[container_name]
    allowed = CONTAINER_KEYS.get(container_name)
    out = {}
    for key, entry in value.items():
        if not isinstance(key, str):
            continue
        if is_secret_key(key):
            log("Warn: Coordinator: {} {} field '{}' looks like a credential - refused".format(component_name, section, key))
            continue
        if allowed is not None and key not in allowed:
            log("Warn: Coordinator: {} {}.{} is not a recognised key - dropped".format(component_name, container_name, key))
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


def _can_report(component):
    """Whether this component instance is one the catalogue should ever expect a report from.

    A component opts in to discovery by defining build_discovery() - the same test
    ComponentBase.refresh_discovery() makes before doing anything. Anything else is working as
    intended by staying silent, so _component_status() leaves it out rather than filing a
    "no_report" against it.

    An instance that cannot be resolved at all (None) counts as reportable. is_active() is true
    exactly when get_component() returns a real object, so this should not arise in practice; if
    it ever does, a redundant line in the map costs a reader a moment, whereas wrongly hiding a
    component that failed to report costs them the bug.
    """
    return component is None or hasattr(component, "build_discovery")


def inverter_record(
    device_id,
    *,
    inverter_type=None,
    control=None,
    composition=None,
    measures_meter=None,
    serials=None,
    functions=None,
    capabilities=None,
    flags=None,
    effects=None,
    hardware_ids=None,
    account_ids=None,
    info=None,
    ratings=None,
    coverage=None,
    entities=None,
):
    """Assemble one inverters-section record, omitting every field that is unset or empty.

    The parameter list IS the inverters section's schema, spelled out rather than taken as
    **kwargs: a mistyped field name is then a TypeError a test catches at the call site, instead
    of a key that reaches validate_report() and is silently dropped from a user's dump. Every
    field after device_id is keyword-only for the same reason - fifteen optional parameters in a
    row are easy to slip by one, and inverter_record("x:1", "direct", True) would otherwise build
    inverter_type="direct", control=True without complaint.

    Unset fields are omitted rather than written as None, {}, [] or "". Every reporter previously
    carried its own `if info: record["info"] = info` ladder, which is how a record ends up
    carrying `"ratings": {}` in one component and omitting it in another. A falsy value that is
    real data is kept: a rating of 0 inside a container, and control=False, which is the fact
    "monitor-only" rather than an absence. Only None, an empty string and an empty container are
    dropped.

    Every container is copied, never stored as the caller's own object. refresh_discovery()
    compares each new build against the report it last filed, so a record holding a reporter's
    live list (serials=self.serials) would change whenever that list did, the rebuilt report
    would always compare equal to it, and the report would freeze at its first state. The copy is
    shallow - the top level only - which is enough because reporters build nested descriptor
    dicts fresh on every call rather than handing over long-lived ones.

    Tuples become lists because validate_report() keeps only a list for a structural or
    vocabulary field: a tuple `serials` or `functions` - a module-level constant, say - would be
    silently dropped from the catalogue. A set or frozenset becomes a sorted list for the same
    reason, sorted by string form so that a set mixing types cannot raise and withhold the whole
    report, and sorted because string hashing is randomised per process, so list(some_set) comes
    out in a different order after a restart and two dumps of the same hardware would diff for nothing.
    """
    return _record(
        device_id,
        {
            "inverter_type": inverter_type,
            "control": control,
            "composition": composition,
            "measures_meter": measures_meter,
            "serials": serials,
            "functions": functions,
            "capabilities": capabilities,
            "flags": flags,
            "effects": effects,
            "hardware_ids": hardware_ids,
            "account_ids": account_ids,
            "info": info,
            "ratings": ratings,
            "coverage": coverage,
            "entities": entities,
        },
    )


def _record(device_id, fields):
    """One record from `device_id` and a field-name -> value dict, by the rules inverter_record() documents.

    Shared by every section's builder so the omit/copy/sort rules live in one place: None, an empty
    string and an empty container are left out; a bool is always kept; each container is copied;
    a set or tuple becomes a list, a set sorted by string form.
    """
    record = {"device_id": device_id}
    for name, value in fields.items():
        if isinstance(value, (set, frozenset)):
            value = sorted(value, key=str)
        elif isinstance(value, (list, tuple)):
            value = list(value)
        elif isinstance(value, dict):
            value = dict(value)
        # A bool is never dropped: it is not a str/list/dict, so control=False survives
        if value is None or (isinstance(value, (str, list, dict)) and not value):
            continue
        record[name] = value
    return record


def charger_record(device_id, *, serves_cars=None, hardware_ids=None, account_ids=None, info=None, ratings=None, entities=None):
    """Assemble one chargers-section record, omitting every field that is unset or empty.

    The chargers counterpart of inverter_record(), with the same keyword-only schema-as-parameters
    design and the same omit/copy rules (see _record()). serves_cars names the car records this
    charger charges - a cross-link that may dangle.
    """
    return _record(device_id, {"serves_cars": serves_cars, "hardware_ids": hardware_ids, "account_ids": account_ids, "info": info, "ratings": ratings, "entities": entities})


def car_record(device_id, *, charged_by=None, flags=None, hardware_ids=None, info=None, ratings=None, entities=None):
    """Assemble one cars-section record, omitting every field that is unset or empty.

    The cars counterpart of inverter_record(). charged_by names the charger record that charges
    this car. A car the reporter knows nothing about carries flags ["stub"] - see
    charger_with_stub_car().
    """
    return _record(device_id, {"charged_by": charged_by, "flags": flags, "hardware_ids": hardware_ids, "info": info, "ratings": ratings, "entities": entities})


def charger_with_stub_car(device_id, *, hardware_ids=None, account_ids=None, info=None, entities=None):
    """A (charger, car) record pair for a charger that knows nothing about the vehicle it charges.

    Predbat indexes cars, not chargers, so every charger is reported with a car even when the
    integration has no vehicle data at all. That car is a stub - device_id "{charger}:car", flags
    ["stub"] - cross-linked both ways. The marker is a flag rather than info.stub because info only
    holds strings, and validation dropped a boolean there (GH#5849).
    """
    car_device_id = "{}:car".format(device_id)
    charger = charger_record(device_id, serves_cars=[car_device_id], hardware_ids=hardware_ids, account_ids=account_ids, info=info, entities=entities)
    car = car_record(car_device_id, charged_by=device_id, flags=["stub"])
    return charger, car


def _bound_entity(entities, name, access=None):
    """The descriptor binding `name` to a real HA entity (not a value stand-in), optionally of one access, or None."""
    descriptor = entities.get(name)
    if not isinstance(descriptor, dict) or not isinstance(descriptor.get("entity_id"), str):
        return None
    if access is not None and descriptor.get("access") != access:
        return None
    return descriptor


def _decimal_places(step):
    """How many decimal places a numeric step has: 0.1 -> 1, 1 -> 0, 0.05 -> 2. None if it is not a number."""
    try:
        exponent = Decimal(str(step)).normalize().as_tuple().exponent
    except (InvalidOperation, ValueError):
        return None
    return max(0, -exponent) if isinstance(exponent, int) else None


def inverter_definition(record, write_and_poll_sleep, base=None, write_max_retry=None, write_backoff=False):
    """Rebuild an INVERTER_DEF-shaped definition for one inverter from its discovery record.

    Returns (definition, gaps, not_applicable). definition is always a new dict: base, when given, is
    copied and never written to - inverter.py:381-389 applies apps.yaml's `inverter:` override by
    writing into the shared INVERTER_DEF[type] row, so two inverters of one type end up sharing the last
    one's override, and this must not repeat that.

    Sources, per the vocabulary spec (docs/superpowers/specs/2026-09-24-discovery-inverter-record-vocabulary-design.md):
    the seven CAPABILITY_KEYS from record["capabilities"]; the PRESENCE_FLAGS and the two GE mode flags
    from which settings record["entities"] binds to a real rw entity; the protocol fields from those
    entities' descriptors; write_and_poll_sleep, write_max_retry and write_backoff from the component
    (the last two only when the component sets them, as only GWMQTT's row does). The optional
    capabilities in OPTIONAL_CAPABILITY_DEFAULTS are never gaps: absent, they keep base's value or
    take the default. A field it cannot work out - a
    missing capability, or a bound entity lacking the format/unit/domain it needs - is listed in gaps
    and keeps base's value (or is left out without a base). A protocol field whose source setting is not
    bound at all does not apply to this device: it takes NOT_APPLICABLE_DEFAULTS' value (or base's) and
    is listed in not_applicable, not in gaps.
    """
    definition = dict(base) if base else {}
    gaps = []
    not_applicable = []
    capabilities = record.get("capabilities") if isinstance(record.get("capabilities"), dict) else {}
    entities = record.get("entities") if isinstance(record.get("entities"), dict) else {}

    if not definition.get("name"):
        definition["name"] = record.get("inverter_type")

    for key in CAPABILITY_KEYS:
        if isinstance(capabilities.get(key), bool):
            definition[key] = capabilities[key]
        else:
            gaps.append(key)
    for key, default in OPTIONAL_CAPABILITY_DEFAULTS.items():
        if isinstance(capabilities.get(key), bool):
            definition[key] = capabilities[key]
        else:
            definition.setdefault(key, default)

    for flag, settings in PRESENCE_FLAGS.items():
        definition[flag] = all(_bound_entity(entities, setting, access="rw") is not None for setting in settings)

    mode = _bound_entity(entities, "inverter_mode", access="rw")
    definition["has_ge_inverter_mode"] = mode is not None and mode.get("domain") == "select"
    definition["has_ge_eco_toggle"] = mode is not None and mode.get("domain") == "switch"
    definition.setdefault("has_mqtt_api", False)

    def not_applicable_field(field):
        """Mark a protocol field as not applying to this device and give it its default."""
        not_applicable.append(field)
        definition.setdefault(field, NOT_APPLICABLE_DEFAULTS[field])

    start = _bound_entity(entities, "charge_start_time")
    if start is None:
        not_applicable_field("charge_time_entity_is_option")
        not_applicable_field("charge_time_format")
    else:
        if start.get("domain"):
            definition["charge_time_entity_is_option"] = start["domain"] == "select"
        else:
            gaps.append("charge_time_entity_is_option")
        if start.get("format"):
            definition["charge_time_format"] = start["format"]
        else:
            gaps.append("charge_time_format")

    clock = _bound_entity(entities, "inverter_time")
    if clock is None:
        not_applicable_field("clock_time_format")
    elif clock.get("format"):
        definition["clock_time_format"] = clock["format"]
    else:
        gaps.append("clock_time_format")

    if _bound_entity(entities, "soc_kw") is not None:
        definition["soc_units"] = "kWh"
    elif _bound_entity(entities, "soc_percent") is not None:
        definition["soc_units"] = "%"
    else:
        gaps.append("soc_units")

    rate = _bound_entity(entities, "charge_rate")
    if rate is None and _bound_entity(entities, "charge_rate_percent") is not None:
        # GE Cloud's percentage-rate models: charge_rate is left unbound and charge_rate_percent is
        # written instead, which inverter.py still treats as power control
        definition["output_charge_control"] = "power"
        not_applicable_field("current_dp")
    elif rate is None:
        definition["output_charge_control"] = "none"
        not_applicable_field("current_dp")
    elif rate.get("unit") == "W":
        definition["output_charge_control"] = "power"
        not_applicable_field("current_dp")
    elif rate.get("unit") == "A":
        definition["output_charge_control"] = "current"
        places = _decimal_places(rate.get("step")) if rate.get("step") is not None else None
        if places is None:
            gaps.append("current_dp")
        else:
            definition["current_dp"] = places
    else:
        gaps.append("output_charge_control")
        not_applicable_field("current_dp")
    # charge_rate's step stands for both rates - an inverter holding them coarsely holds both alike. Most
    # inverters have no such step, so its absence is not a gap: base's value stands, else none
    if rate is not None and rate.get("step_percent_of_capacity"):
        definition["rate_step_percent_of_capacity"] = rate["step_percent_of_capacity"]
    else:
        definition.setdefault("rate_step_percent_of_capacity", 0)

    definition["time_button_press"] = _bound_entity(entities, "schedule_write_button", access="rw") is not None

    count = 1
    while _bound_entity(entities, "load_power_{}".format(count)) is not None:
        count += 1
    definition["num_load_entities"] = count

    definition["write_and_poll_sleep"] = write_and_poll_sleep
    if write_max_retry is not None:
        definition["write_max_retry"] = write_max_retry
    if write_backoff:
        definition["write_backoff"] = True
    return definition, gaps, not_applicable


def validate_report(report, component_name, log):
    """Return a cleaned copy of one component's report - never raises, drops what does not fit.

    "automatic" is carried through only when the raw report actually provided one - a component
    with no such concept (Solcast) omits the key entirely rather than have it default to True, so
    the catalogue never claims an auto-config relationship for a component that has none. See
    _component_status(), the only other place this key is read.

    A section whose value is not a list is dropped whole rather than iterated. "Never raises" has
    to hold for a malformed section exactly as it already does for a malformed report: a scalar
    ({"inverters": 1}) is not iterable at all, and a bare string would iterate as its characters,
    turning one component's mistake into a TypeError out of report() or a log line per character.
    """
    if not isinstance(report, dict):
        log("Warn: Coordinator: {} report is a {}, not a dict - treated as empty".format(component_name, type(report).__name__))
        report = {}
    cleaned = {"schema_version": SCHEMA_VERSION}
    if "automatic" in report:
        cleaned["automatic"] = bool(report["automatic"])
    for section in SECTION_SPEC:
        raw = report.get(section) or []
        if not isinstance(raw, list):
            log("Warn: Coordinator: {} {} is a {}, not a list of records - dropped".format(component_name, section, type(raw).__name__))
            continue
        records = []
        for record in raw:
            validated = _validate_record(record, section, component_name, log)
            if validated:
                records.append(validated)
        if records:
            cleaned[section] = records
    return cleaned
