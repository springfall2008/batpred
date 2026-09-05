# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
# -----------------------------------------------------------------------------
# Charger registry - composes EV chargers discovered by several components into
# the flat car_charging_* arrays that fetch.py consumes, and tells each component
# which car slot its own charger was given.
#
# Before this existed, every charger component assigned its own discovered list
# straight to car_charging_planned/_energy/_power, so the last component to run
# erased the others' chargers; and num_cars was composed as a max of each
# component's local count, so one GivEnergy charger plus one Ohme charger gave
# num_cars=1 with both claiming slot 0.
# -----------------------------------------------------------------------------


"""Charger registry - one car slot per physical EV charger, across every component."""

import re
import threading

from const import PREDBAT_MAX_CARS

LEGACY_SOURCE = "legacy"

# Slot-aligned args: one entry per car, so a gap cannot be represented. apps.yaml
# validation rejects non-string list elements (predbat.py:1644), so a slot the registry
# would have to invent a value for means the user's own key is left exactly as it stands
# rather than written (see materialise) - never cleared, which would delete their config for
# the cars that do have a value. A list the registry composed itself is cut back to the slots
# that still line up instead, because its positions move when the charger set does. A legacy
# slot's raw value is not such a gap - it is written back exactly as the user's own config
# produced it, None included.
SLOT_ALIGNED = ("planned", "now", "soc")

# Site aggregates: minute_data_import_export() sums the list, so gaps are simply
# dropped rather than padded.
AGGREGATE = ("energy", "power")

# Shortest device_id that may be matched inside a legacy entity id to call the two the same
# charger (_drop_duplicated_legacy). Real manufacturer serials are far longer; anything this short
# would collide with unrelated entities by chance.
MIN_IDENTITY_MATCH_LENGTH = 4

# Sources whose device_id is a manufacturer serial that third-party integrations embed verbatim in
# their own entity names, so finding it inside a legacy entity id really is evidence of the same
# hardware: myenergi's Zappi serial and GivEnergy Cloud's charger serial.
#
# The gateway is deliberately absent. Its device_id is an OCPP charge-point id, which the charger's
# own installer chooses - it can be anything from "1" to an English word - so a substring of it
# turning up in an unrelated entity id says nothing about identity. Gateway chargers are still
# deduped against a legacy slot that names the exact same entity; only the looser
# serial-inside-the-name match is withheld.
SERIAL_IDENTITY_SOURCES = ("myenergi", "gecloud")


class ChargerEntry:
    """One physical charger, as reported by the component that discovered it.

    Identity is (source, device_id) - a durable, source-native pair. Deliberately NOT
    the entity-id suffix, which for the gateway is a 6-character slug its own docstring
    documents as collision-prone.
    """

    __slots__ = ("source", "device_id", "planned", "now", "energy", "power", "soc", "max_rate_kw")

    def __init__(self, source, device_id, planned=None, now=None, energy=None, power=None, soc=None, max_rate_kw=None):
        self.source = source
        self.device_id = str(device_id)
        self.planned = planned
        self.now = now
        self.energy = energy
        self.power = power
        self.soc = soc
        self.max_rate_kw = max_rate_kw

    def key(self):
        """The identity this charger is deduped and slot-mapped by."""
        return (self.source, self.device_id)

    def sort_key(self):
        """Legacy slots keep their apps.yaml positions; everything else sorts by identity.

        Sorting rather than discovery order matters because automatic_config() is not
        centrally orchestrated - each component calls its own at a different time and
        re-calls it on rediscovery - so insertion order is not reproducible across restarts.
        """
        if self.source == LEGACY_SOURCE:
            return (0, "", int(self.device_id))
        return (1, self.source, self.device_id)


class ChargerRegistry:
    """Collects chargers from every component and materialises the flat car_charging_* args.

    Components each run on their own thread (hass.py:223 starts them) and the gateway
    registers its chargers from an MQTT callback, so every public entry point that reads or
    mutates the shared state takes ``self._lock``. It is an RLock rather than a plain Lock
    because replace_source() holds the lock across the arg composition, which in turn calls
    entries() - both are public, so the lock has to be re-entrant or the outer call would
    deadlock on the inner one.

    Nothing that blocks on Home Assistant runs under the lock: the composition is pure arg
    writes, and the one HA round trip (car_charging_rate) is handed back to the caller to
    make after it releases - see _expose_rates.
    """

    def __init__(self, base):
        self.base = base
        self._lock = threading.RLock()
        self._by_source = {}
        self._slots = {}
        self.generation = 0
        self.plan_generation = None
        self._legacy_aggregates = {"energy": [], "power": []}
        # Aggregate key -> the exact value the registry last wrote there, so a key some other
        # component has overwritten since can be recognised as no longer ours to restore.
        self._aggregates_written = {}
        # Slot-aligned fields the registry has actually written, each mapped to the exact value
        # it last wrote there. A key holding only legacy slots is left alone (materialise), so
        # the presence of a field tells the difference between "untouched user config" and "we
        # put a discovered charger in there and it has since gone away", which does have to be
        # rewritten or the vanished charger's entity would stay behind; and the value tells
        # whether what is standing there is still ours - the same ownership idiom
        # _write_aggregates applies to car_charging_energy/_power.
        self._slot_args_written = {}
        # Slot-aligned field -> the chargers that had no value for it the last time the key was
        # left as it was, so the explanation is logged when the gap appears or changes rather
        # than on every rediscovery.
        self._slot_gap_logged = {}
        self._populated = False
        # The last value the registry wrote to num_cars, or None if it never has. Distinct from
        # _populated, which is only about having held a charger: an external-only claim writes
        # num_cars with no entries behind it, and releasing it has to be able to write again.
        self._num_cars_written = None
        # Cars other components own outright, which the registry has no charger for, by source
        # name - declared through set_external_num_cars() rather than inferred.
        self._external_counts = {}
        # A floor carried over from hand-written apps.yaml num_cars (see preregister_legacy).
        self._declared_floor = 0
        # Legacy slots already dropped as duplicates of a discovered charger, so that the
        # explanation is logged once rather than on every rediscovery.
        self._deduped_legacy = set()

    def replace_source(self, source, entries):
        """Atomically replace everything `source` contributes, then re-materialise.

        One call covers add, update and removal, and is idempotent - which is what makes it
        safe for a component to call on every rediscovery, including with an empty list when
        its chargers have gone away. Atomic in the real sense: the lock is held across the
        arg composition too, so a component registering on another thread can neither observe
        nor interleave with a half-composed set of args. The one thing deliberately left
        outside it is the car_charging_rate exposure - see _expose_rates.
        """
        with self._lock:
            deduped = {}
            for entry in entries or []:
                deduped[entry.key()] = entry
            if deduped:
                self._by_source[source] = list(deduped.values())
            else:
                self._by_source.pop(source, None)
            pending = self._materialise_locked()
        self._expose_rates(pending)

    def set_external_num_cars(self, source, count):
        """Declare that `source` needs num_cars held at `count` for cars it owns itself.

        Octopus Intelligent and Kraken wire dispatch sensors for cars that have no charger in
        this registry (octopus.py:1341, kraken.py:1271), and fetch.py only reads
        octopus_intelligent_slot for car indices below num_cars - so those cars disappear if
        num_cars is written as the registered charger count alone.

        Ownership is declared, not inferred. Inferring it from "num_cars has moved since we
        last wrote it" was blind to an external raise that happened to equal our own write,
        and since both callers are raise-only they could never take the claim back either, so
        the floor it produced could never be released. A `count` of 0 or None drops the claim.
        """
        with self._lock:
            try:
                count = int(count or 0)
            except (TypeError, ValueError):
                count = 0
            if count > 0:
                self._external_counts[source] = count
            else:
                self._external_counts.pop(source, None)
            pending = self._materialise_locked()
        self._expose_rates(pending)

    def entries(self):
        """Every registered charger, in deterministic slot order, clamped to the kernel ceiling."""
        with self._lock:
            out = []
            for source_entries in self._by_source.values():
                out.extend(source_entries)
            out.sort(key=lambda entry: entry.sort_key())
            out = self._drop_duplicated_legacy(out)
            if len(out) > PREDBAT_MAX_CARS:
                self.base.log("Warn: ChargerRegistry: {} chargers registered but PREDBAT_MAX_CARS is {} - ignoring the excess".format(len(out), PREDBAT_MAX_CARS))
                out = out[:PREDBAT_MAX_CARS]
            return out

    def _drop_duplicated_legacy(self, entries):
        """Drop a legacy slot that names the same physical charger as a discovered one.

        The stock apps.yaml car_charging_planned regex matches the third-party ha-myenergi
        integration's Zappi entities. On a site running that integration alongside Predbat's
        own myenergi component the regex resolves, so the same physical Zappi arrives twice -
        once as a legacy slot, once as a registered charger - and its energy is counted for
        two cars. The component's entry is the one kept: it carries a real identity, so
        control can address it, where the legacy slot has only its position.

        The two never name the *same* entity in practice, though: ha-myenergi publishes
        sensor.myenergi_zappi_<serial>_plug_status while our own component registers
        sensor.predbat_myenergi_zappi_<serial>_plug_status. What they share is the serial, so
        the match is on the discovered charger's device_id appearing inside the legacy entity id.
        Three things keep that from firing on a coincidence:

        - Only sources whose device_id *is* a manufacturer serial are eligible for it
          (SERIAL_IDENTITY_SOURCES); an installer-chosen OCPP charge-point id is not evidence.
        - Short ids are excluded (MIN_IDENTITY_MATCH_LENGTH), since a two- or three-character one
          would appear in any entity that happens to contain those characters.
        - The match is on a word boundary, not a bare substring. Entity ids separate their parts
          with "_", so requiring a non-alphanumeric on each side is a real boundary - and without
          it serial 10000001 would swallow a legacy slot naming Zappi 100000010, dropping a car
          that genuinely is a different charger.

        An exact entity match still counts for every source, for a hand-written slot that names
        our own entity directly.

        The limit is honest: a legacy slot naming the same charger through an entity that
        carries no device id (a user-renamed entity, or a helper) cannot be recognised and
        stays as its own car.
        """
        component_planned = set()
        component_ids = set()
        for entry in entries:
            if entry.source == LEGACY_SOURCE:
                continue
            if entry.planned:
                component_planned.add(entry.planned)
            # Only a manufacturer serial, and only one long enough that finding it inside an
            # unrelated entity id would not happen by chance, is evidence of identity.
            if entry.source in SERIAL_IDENTITY_SOURCES and entry.device_id and len(entry.device_id) >= MIN_IDENTITY_MATCH_LENGTH:
                component_ids.add(entry.device_id)
        if not component_planned and not component_ids:
            return entries
        kept = []
        for entry in entries:
            if entry.source == LEGACY_SOURCE and self._names_a_discovered_charger(entry, component_planned, component_ids):
                if entry.key() not in self._deduped_legacy:
                    self._deduped_legacy.add(entry.key())
                    self.base.log("Info: ChargerRegistry: legacy car slot {} names {}, which a discovered charger already supplies - dropping the legacy slot so the car is not counted twice".format(entry.device_id, entry.planned))
                continue
            kept.append(entry)
        return kept

    @staticmethod
    def _names_a_discovered_charger(entry, component_planned, component_ids):
        """True when this legacy slot's planned entity is a discovered charger's, by id or name.

        The serial has to sit on a word boundary in the entity id - no alphanumeric immediately
        either side of it. Entity ids join their parts with "_", so that is exactly the boundary
        an embedded serial has, while a bare substring test would read serial 10000001 as naming
        sensor.myenergi_zappi_100000010_plug_status and drop a different charger's car.

        The match is case insensitive because the two sides are not written the same way. HA
        entity ids are always lower case, and async_automatic_config_evc lower-cases the serial
        when it builds one - but a GivEnergy Cloud serial is upper case at source ("EVC123456",
        "WE1913G005"), which is what device_id carries. A case-sensitive test could therefore
        never match a gecloud serial at all, so that half of the dedup silently did nothing and
        the duplicated charger stayed as its own car. myenergi serials are numeric, which is
        why the tests did not see it.
        """
        if not entry.planned:
            return False
        if entry.planned in component_planned:
            return True
        if not isinstance(entry.planned, str):
            return False
        return any(re.search(r"(?<![0-9a-zA-Z])" + re.escape(device_id) + r"(?![0-9a-zA-Z])", entry.planned, re.IGNORECASE) for device_id in component_ids)

    def slot_for(self, source, device_id):
        """The car index this charger was given, or None if it is not registered.

        Control loops MUST use this rather than assuming their own charger is car 0 or that
        their Nth charger is car N. Getting it wrong drives a charger from another car's plan.
        """
        with self._lock:
            return self._slots.get((source, str(device_id)))

    def snapshot_generation(self):
        """Read the allocation version after any in-flight materialisation finishes."""
        with self._lock:
            return self.generation

    def confirm_plan(self, generation):
        """Enable control only after publishing a plan built with the current slot map."""
        with self._lock:
            if generation == self.generation:
                self.plan_generation = generation

    def plan_is_current(self, generation=None):
        """Whether published car windows belong to the current charger allocation."""
        with self._lock:
            return self.plan_generation == self.generation and (generation is None or generation == self.generation)

    def owns_aggregate(self, field):
        """Whether the live aggregate still equals the last value composed here."""
        with self._lock:
            return field in self._aggregates_written and self.base.get_arg("car_charging_" + field, None, indirect=False) == self._aggregates_written[field]

    def can_compose_aggregate(self, field):
        """Whether a component may contribute its own sensor to this aggregate.

        Two very different things can be standing in car_charging_energy/_power, and only one of
        them is a reason for a component to hold back:

        - A hand-written sensor the registry captured at startup (preregister_legacy), or an
          aggregate the registry composed itself. Both are safe to add to, because _write_aggregates
          concatenates: the captured legacy sensor is written back ahead of every discovered
          charger's, so contributing does not replace it - the two are summed by
          minute_data_import_export(). A component that backs off here instead simply omits its own
          charging from load subtraction and power display for no gain.
        - A value another direct writer put there at runtime, alphaess.py:805 being the one in the
          tree. That writer owns the key and will set it again from its own discovery, so a
          contribution here is both unwanted and short lived. Components must still back off.

        The two are told apart by what the key holds now: the captured snapshot means nobody has
        written since preregister_legacy read it, so it is still the user's own config, while
        anything else appeared afterwards and belongs to whoever wrote it.
        """
        with self._lock:
            if self.owns_aggregate(field):
                return True
            current = self.base.get_arg("car_charging_{}".format(field), None, indirect=False)
            if not isinstance(current, list):
                current = [current]
            return [value for value in current if not is_unconfigured(value)] == self._legacy_aggregates[field]

    def _write_num_cars(self, count):
        """Write num_cars: the registry's own count, floored by every declared claim on it.

        Both floors are explicit: `_declared_floor` is what hand-written apps.yaml asked for
        (preregister_legacy) and `_external_counts` is what other components have claimed for
        cars of their own (set_external_num_cars). Nothing else is treated as a floor, so a
        count the registry itself wrote is always its own to lower - which is what lets a
        charger that has gone away reduce the number of cars again.
        """
        num_cars = max(count, self._declared_floor, max(self._external_counts.values(), default=0))
        self._num_cars_written = num_cars
        self.base.set_arg("num_cars", num_cars)

    def _owns_num_cars(self):
        """True when the num_cars standing in the args is one the registry may write over.

        Only consulted on a site where no charger has ever been registered, so the registry has
        composed nothing and cannot simply assume the key is its own. Three values qualify:

        - nothing there at all, so writing one takes nobody's config away;
        - the single most recent value the registry itself wrote;
        - the largest claim standing *right now*. Both claimants raise num_cars directly the
          instant before declaring (octopus.py:1352, kraken.py:1287), so on a site the registry
          has never written that raise is the only thing behind num_cars - without recognising
          it, a claim could be honoured but never released.

        Every term is bounded in time: the last write is a single value, replaced on each write,
        and the live claims empty out the moment they are released. An earlier version kept every
        count ever claimed, which let a long-released claim of N vouch for an unrelated num_cars
        of N that somebody else set much later - a released claim must leave no residue behind it.

        Anything else belongs to whoever put it there and is left alone. This is the same
        ownership doctrine _write_aggregates applies to car_charging_energy/_power, and not the
        rejected inference of "num_cars has moved, so somebody must have raised it": every value
        accepted here is one the registry or a currently-standing claim is answerable for.

        The residual limit is honest and unavoidable: if another owner happens to set num_cars to
        exactly the count a claim is declaring, the two are indistinguishable from here. Writing
        is harmless then (it is the same number); the release afterwards would lower it.

        The user's own apps.yaml num_cars needs none of this - preregister_legacy reads it into
        _declared_floor, which every write is floored by.
        """
        current = self.base.get_arg("num_cars", None, indirect=False)
        if current is None or current == self._num_cars_written:
            return True
        return bool(self._external_counts) and current == max(self._external_counts.values())

    def _write_slot_arg(self, arg, field, values):
        """Write a slot-aligned key and remember exactly what was written there.

        Recording the value, not just the fact of having written, is what lets a later
        composition tell "this is still the list we composed" from "somebody else has set this
        since" - the same distinction _write_aggregates draws for the aggregates.
        """
        value = values if any(values) else None
        self.base.set_arg(arg, value)
        self._slot_args_written[field] = value

    def _owns_slot_arg(self, arg, field):
        """True when this slot-aligned key still holds exactly what the registry last wrote.

        A key the registry has never written belongs to the user's apps.yaml, and one that has
        moved since belongs to whoever moved it; neither is ours to rewrite on a gap.
        """
        return field in self._slot_args_written and self.base.get_arg(arg, None, indirect=False) == self._slot_args_written[field]

    def _trim_owned_slot_arg(self, arg, field, entries, values, missing):
        """Cut a registry-composed slot list back to the slots that are still positionally true.

        A composed list is tied to the charger set it was composed from: slot N holds whatever
        charger sorted Nth *then*, which need not be the charger in slot N now. A charger that
        sorts ahead of one already registered shifts every later slot along - myenergi joining
        an ohme takes slot 0, because "myenergi" sorts before "ohme" - so keeping the old list
        hands the ohme's SoC to the myenergi car, and fetch.py plans that car's charge from
        another car's battery level. Leaving a stale list alone is only harmless while it stays
        aligned; once it does not, it is actively wrong.

        So everything up to the first gap is kept - each of those values still belongs to the
        entry standing at its own index - and the rest is dropped. Nothing of the user's is lost
        by that: legacy slots sort first (ChargerEntry.sort_key), so they are always inside the
        kept head, and what is dropped is only ever what the registry itself invented. Where the
        head is empty the key goes back to unset, which is what the user had before the charger
        that cannot fill it was discovered.

        Only reached for a key the registry owns (_owns_slot_arg). A hand-written key is left
        exactly as it stands instead - see the caller.
        """
        gap = next(index for index, (entry, value) in enumerate(zip(entries, values)) if not value and entry.source != LEGACY_SOURCE)
        head = values[:gap]
        dropped = self._slot_args_written[field] != (head if any(head) else None)
        self._write_slot_arg(arg, field, head)
        if missing != self._slot_gap_logged.get(field):
            self._slot_gap_logged[field] = missing
            if dropped:
                self.base.log("Warn: ChargerRegistry: {} no longer lines up with the current car slots - keeping the first {} slot(s) and dropping the rest, as {} supply no value".format(arg, gap, missing))
            else:
                self.base.log("Warn: ChargerRegistry: leaving {} as it is - no value for {}".format(arg, missing))

    def _write_aggregates(self, entries):
        """Write the site-aggregate args, but only the keys the registry actually owns.

        car_charging_energy/_power are not slot aligned, so they are the one place another
        component can legitimately be the author: alphaess sets car_charging_energy from its own
        discovery at runtime, and ohme deliberately backs off when somebody else already owns it.
        Replaying the legacy snapshot on every materialise would clobber both. So a key is
        written while some registered charger supplies it, plus exactly one more time - to put
        the legacy value back, or clear the key - when the last contributing charger goes away.

        That last write is guarded on the key still holding the value we put there. Having
        written a key once is not ownership of it forever: alphaess.py:805 can set
        car_charging_energy at any point afterwards, and restoring over the top of that would
        silently unmap its EV charger. A value that is no longer ours ends our claim on it.
        """
        for field in AGGREGATE:
            arg = "car_charging_{}".format(field)
            discovered = [getattr(entry, field) for entry in entries if getattr(entry, field)]
            if discovered:
                serials = {entry.device_id for entry in entries if getattr(entry, field) and entry.source in SERIAL_IDENTITY_SOURCES and len(entry.device_id) >= MIN_IDENTITY_MATCH_LENGTH}
                legacy = [value for value in self._legacy_aggregates[field] if not self._names_a_discovered_charger(ChargerEntry(LEGACY_SOURCE, "0", planned=value), set(discovered), serials)]
                values = list(dict.fromkeys(legacy + discovered))
                self.base.set_arg(arg, values)
                self._aggregates_written[field] = list(values)
            elif field in self._aggregates_written:
                current = self.base.get_arg(arg, None, indirect=False)
                if current != self._aggregates_written[field]:
                    self.base.log("Info: ChargerRegistry: {} has been set by another component since the registry wrote it - leaving it alone".format(arg))
                    self._aggregates_written.pop(field, None)
                    continue
                restore = list(self._legacy_aggregates[field])
                self.base.set_arg(arg, restore if restore else None)
                self._aggregates_written.pop(field, None)

    def materialise(self):
        """Write the flat car_charging_* args from the current registry contents.

        Uses set_arg (not set_arg_auto) to avoid the autodiscovery warning (#4494/#4500).
        The warning exists because autodiscovery used to discard user config; the registry
        merges instead, so the warning would fire misleadingly. Legacy config is preserved:
        a slot-aligned key holding only legacy slots is not written at all, and where a
        composed list mixes legacy and discovered slots the legacy ones carry their raw
        apps.yaml values through verbatim.

        Takes the lock, so it is safe to call directly; replace_source() already holds it,
        which is why the lock is re-entrant. The car_charging_rate exposure runs after the
        lock is dropped - see _expose_rates.
        """
        with self._lock:
            pending = self._materialise_locked()
        self._expose_rates(pending)

    def _expose_rates(self, pending):
        """Write the per-slot car_charging_rate UI items, with the registry lock NOT held.

        car_charging_rate is a UI config item (input_number), not an arg - get_arg consults the
        UI config before args, so set_arg would be ignored for it. But expose_config() reaches
        ha.set_state and a synchronous HTTP POST (ha.py:1076), and replace_source() runs on
        component threads - the gateway's straight from an MQTT callback - while the main loop
        waits on the same lock in snapshot_generation() (fetch.py:2836). Holding the lock across
        that POST made every registration block the main loop for as long as HA took to answer.

        So the (item, value) pairs are composed under the lock and written once it is released.
        Only gateway chargers set max_rate_kw, so only gateway installs ever had the stall.

        The honest cost of moving it out: two components registering at once can have their
        writes land in the opposite order to their compositions, leaving the older rate
        standing. It is self-correcting - each rediscovery composes and writes the value again -
        and it is a config default a user may override anyway, which is a far smaller thing
        than blocking the main loop on an HA round trip.
        """
        expose = getattr(self.base, "expose_config", None)
        if expose is None:
            return
        for item, value in pending:
            expose(item, value)

    def _materialise_locked(self):
        """Compose and write the args; the caller must already hold the lock.

        Returns the car_charging_rate (item, value) pairs for _expose_rates to write after the
        lock is released, so no blocking HA I/O happens inside the critical section.
        """
        entries = self.entries()
        slots = {entry.key(): slot for slot, entry in enumerate(entries)}
        if slots != self._slots:
            self.generation += 1
            # Only on a change: this runs on every rediscovery, and the map is what a support
            # log needs to explain which charger a car number refers to.
            self.base.log("Info: ChargerRegistry: slots {}".format({"{}/{}".format(source, device_id): slot for (source, device_id), slot in sorted(slots.items(), key=lambda item: item[1])}))
        self._slots = slots
        if not entries:
            self._slot_gap_logged.clear()
            if self._populated:
                # The registry held chargers before and is now empty - clear the stale args,
                # but only the ones the registry itself wrote. A key holding nothing but the
                # user's own legacy config was never ours, so clearing it would delete config
                # the registry only ever read.
                self._write_num_cars(0)
                for field in SLOT_ALIGNED:
                    if field in self._slot_args_written:
                        self._write_slot_arg("car_charging_{}".format(field), field, [])
                self._write_aggregates([])
            elif (self._num_cars_written is not None or self._external_counts) and self._owns_num_cars():
                # No charger has ever been registered here, so num_cars is carrying nothing but
                # declared claims - Octopus/Kraken cars that have no charger of their own. That
                # still has to be written from here, with no entries behind it, and lowered
                # again when the claim is released; gating it on _populated meant such a claim
                # could be raised but never taken back, holding a phantom car up for the rest
                # of the run. Slot-aligned and aggregate keys are untouched: a claim never
                # composed any, so there is nothing there that is the registry's to clear.
                #
                # A registry that has never written num_cars and holds no claim does nothing at
                # all - a site with neither chargers nor claims is left exactly as its
                # apps.yaml configured it.
                self._write_num_cars(0)
            return []

        self._populated = True
        self._write_num_cars(len(entries))

        for field in SLOT_ALIGNED:
            values = [getattr(entry, field) for entry in entries]
            arg = "car_charging_{}".format(field)
            if all(entry.source == LEGACY_SOURCE for entry in entries) and field not in self._slot_args_written:
                # Nothing here but the user's own apps.yaml slots, and the registry has never
                # written this key: it already holds exactly what their config produced, so
                # leave it completely alone rather than round-tripping it through here. That
                # matters because auto_config() runs first and turns an unmatched list regex
                # into a None (userinterface.py:1098), which the all-or-omit rule below would
                # otherwise read as a gap and drop the whole key - taking the *valid* sensors
                # in the other slots with it.
                continue
            # Only a component slot with no value is a gap the registry would have to invent
            # something for. A legacy slot's value - including that auto_config None - is the
            # user's own config, and goes back verbatim, which is precisely what Predbat read
            # from the key before the registry existed.
            missing = [entry.key() for entry, value in zip(entries, values) if not value and entry.source != LEGACY_SOURCE]
            if missing:
                if self._owns_slot_arg(arg, field):
                    # The key holds a list the registry composed itself, which is positionally
                    # tied to the charger set it was composed from - so it is not simply "the
                    # closest thing to the truth available" once the slots move under it. It is
                    # cut back to the slots that are still true rather than kept whole; see
                    # _trim_owned_slot_arg for what that protects against.
                    self._trim_owned_slot_arg(arg, field, entries, values, missing)
                    continue
                # A component-caused gap cannot be expressed: None fails validation and a
                # placeholder would be read as a real entity. So the user's own key is left
                # exactly as it stands - not written, and above all not cleared.
                #
                # Clearing it (which this used to do) was destructive rather than merely
                # incomplete. myenergi supplies neither soc nor now, ohme supplies no now, and
                # gateway supplies no now under gateway_evc_control, so on those installs the
                # gap is permanent - and none of those components ever wrote these keys before
                # the registry existed, which is exactly why a hand-written car_charging_soc
                # used to survive discovery. Deleting it made fetch.py:1383 read the SoC as 0.0
                # and plan a full charge into a possibly-full car, and dropped
                # car_charging_now (fetch.py:2400 then falls back to "no") for the gateway car
                # that did supply one as soon as an ohme registered alongside it.
                #
                # Leaving it produces the pre-registry outcome instead: a list that is short for
                # the new car count, plus fetch.py's own out-of-range warning for the cars past
                # its end. A stale value is recoverable; a deleted one is not, and
                # car_charging_manual_soc (or the plan, for "now") covers the slots it does not
                # reach. That holds because the value is the *user's*: positions in it are
                # theirs, so a car appended after them cannot shift what they wrote.
                if missing != self._slot_gap_logged.get(field):
                    self._slot_gap_logged[field] = missing
                    self.base.log("Warn: ChargerRegistry: leaving {} as it is - no value for {}".format(arg, missing))
                continue
            self._slot_gap_logged.pop(field, None)
            self._write_slot_arg(arg, field, values)

        self._write_aggregates(entries)

        return [("car_charging_rate" if slot == 0 else "car_charging_rate_{}".format(slot), entry.max_rate_kw) for slot, entry in enumerate(entries) if entry.max_rate_kw]


def is_unconfigured(value):
    """True when this apps.yaml value is not real config the user chose.

    The stock apps.yaml template ships car_charging_planned and car_charging_energy as `re:`
    patterns matching third-party Zappi/Wallbox entities, and auto_config() only strips the ones
    it cannot match on its final pass. So between the two passes an unmatched pattern is still
    sitting there as its literal "re:" string, and reading it as configuration invents a car for
    virtually every installation. Same idiom as ohme.py's owns_energy check.

    This answers "is this slot droppable" only. It is deliberately not used to null out values:
    a legacy slot that survives keeps whatever the user actually wrote, unresolved pattern and
    all, so materialise() puts back exactly what Predbat read before the registry existed.
    """
    return value is None or (isinstance(value, str) and value.startswith("re:"))


def preregister_legacy(registry, base):
    """Seed the registry from hand-written apps.yaml car_charging_* config.

    Position is all the identity legacy config has: an index cannot say whether two entries
    mean two chargers, two cars sharing a charger, or two profiles watching one sensor - and
    shared-charger households deliberately do the last of these. So these are recorded as
    degraded slots that keep their positions and make no identity claims, and autodiscovery
    appends after them.
    """
    # Aggregates are not per-slot, so they are held aside and concatenated ahead of every
    # discovered charger's sensor rather than round-tripped through entries. Capture them
    # first so they survive component registration even if num_cars is 0.
    for field in AGGREGATE:
        existing = base.get_arg("car_charging_{}".format(field), None, indirect=False)
        if not isinstance(existing, list):
            existing = [existing]
        # An unmatched template regex is not a sensor, and prepending it to every discovered
        # charger's sensor would put a "re:" string into a list apps.yaml validation requires
        # to be entity names.
        registry._legacy_aggregates[field] = [value for value in existing if not is_unconfigured(value)]

    # Determine slot count: explicit num_cars if present; implicit 1 if any car_charging config
    # is set and num_cars is absent (to match fetch.py:2834's default); else 0.
    raw_num_cars = base.get_arg("num_cars", None, indirect=False)
    if raw_num_cars is not None:
        declared = int(raw_num_cars or 0)
    else:

        def is_configured(arg):
            """True when this apps.yaml key holds at least one value the user actually chose."""
            value = base.get_arg(arg, None, indirect=False)
            if isinstance(value, list):
                return any(not is_unconfigured(item) for item in value)
            return not is_unconfigured(value)

        # num_cars is absent - default to 1 if any car config is present.
        has_car_config = is_configured("car_charging_planned") or is_configured("car_charging_now") or is_configured("car_charging_soc")
        declared = 1 if has_car_config else 0

    if declared <= 0:
        return

    def slot_values(arg):
        """Read one apps.yaml key into exactly `declared` raw values, matching today's semantics.

        A scalar resolves for every index in resolve_arg(), so it is broadcast rather than
        padded - padding would silently blank every car after the first. A list is truncated
        to num_cars, because fetch.py reads no further than that today. Values are kept exactly
        as written, unresolved "re:" patterns included, because a legacy slot is written back
        verbatim; nulling them here would rewrite the user's own config.
        """
        value = base.get_arg(arg, None, indirect=False)
        if value is None:
            return [None] * declared
        if not isinstance(value, list):
            return [value] * declared
        return [value[index] if index < len(value) else None for index in range(declared)]

    planned = slot_values("car_charging_planned")
    now = slot_values("car_charging_now")
    soc = slot_values("car_charging_soc")

    def is_slot_configured(index):
        """True when slot `index` has at least one value behind it the user actually chose."""
        return not (is_unconfigured(planned[index]) and is_unconfigured(now[index]) and is_unconfigured(soc[index]))

    # A slot with nothing behind it is a count, not a charger: the stock template declares
    # num_cars: 1 with only unmatched regexes, and turning that into an entry both pushed a real
    # auto-discovered charger down to slot 1 and left a slot supplying no car_charging_soc - which
    # makes the omit-on-gap rule drop the SoC list entirely, so Predbat plans a full charge into
    # a car whose level it cannot see. The declared count is still honoured, as a floor on
    # num_cars, so nothing that used to be planned for stops being planned for.
    #
    # Only the *trailing* empty slots go, though. An interior hole - "nothing matched for car 0,
    # here is car 1" - cannot be compacted away: per-car settings (battery size, charge limit,
    # exclusive, manual SoC) are addressed by position, so moving car 1 down to slot 0 hands it
    # car 0's settings and hands slot 1 to the next charger discovered. The hole keeps its place
    # and its raw value.
    last_configured = -1
    for index in range(declared):
        if is_slot_configured(index):
            last_configured = index

    registry._declared_floor = declared
    entries = [ChargerEntry(LEGACY_SOURCE, index, planned=planned[index], now=now[index], soc=soc[index]) for index in range(last_configured + 1)]
    registry.replace_source(LEGACY_SOURCE, entries)


def slot_entity_suffix(slot):
    """The entity-id suffix Predbat publishes for a given car slot.

    publish_car_plan() names car 0's entities with no suffix and car N's with "_N"
    (output.py:80-88), so a control loop reading its own car's plan must build the same.
    """
    return "" if not slot else "_{}".format(slot)
