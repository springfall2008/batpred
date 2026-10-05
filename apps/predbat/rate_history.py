"""Provider-independent, observed effective-rate history and alternating snapshots.

All scopes are half-open UTC intervals. Adapters own source evidence, billing
boundaries and automatic precedence; this module never infers them from prices.
"""

import asyncio
import copy
import hashlib
import json
import math
from datetime import date, datetime, timedelta, timezone as datetime_timezone
from zoneinfo import ZoneInfo


SCHEMA_VERSION = 1


def _utc(value):
    """Convert an aware datetime or ISO string into an aware UTC instant."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("History timestamps must be timezone aware")
    return value.astimezone(datetime_timezone.utc)


def _iso(value):
    """Return a canonical UTC timestamp."""
    return _utc(value).isoformat()


def _local_date(value):
    """Validate a caller-supplied local calendar date without guessing a zone."""
    if isinstance(value, str):
        value = date.fromisoformat(value)
    if type(value) is not date:
        raise ValueError("History context requires a local calendar date")
    return value.isoformat()


def _rate(value):
    """Validate a finite numerical price, allowing negative and zero prices."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("Invalid history price")
    return float(value)


def _scope(item, price=True):
    """Copy and validate a transport interval without guessing provenance."""
    result = copy.deepcopy(item)
    result["start"], result["end"] = _iso(item["start"]), _iso(item["end"])
    if result["start"] >= result["end"]:
        raise ValueError("Empty or reversed history scope")
    if price:
        result["rate"] = _rate(item["rate"])
    if "quality" in result and result["quality"] not in ("observed", "confirmed", "last_known"):
        raise ValueError("Invalid history quality")
    if "sources" in result and (not isinstance(result["sources"], list) or not all(isinstance(source, str) for source in result["sources"])):
        raise ValueError("Invalid history sources")
    return result


def _offer(item):
    """Validate full-bound source evidence, separate from effective child prices."""
    result = _scope(item)
    if result.get("direction") not in ("import", "export") or result.get("kind") not in ("increment", "minimum", "replace"):
        raise ValueError("Invalid retained source offer")
    if result.get("source") not in ("saving", "free", "axle", "axle_managed") or not isinstance(result.get("identity"), str) or not result["identity"]:
        raise ValueError("Invalid source offer identity")
    if isinstance(result.get("stage"), bool) or not isinstance(result.get("stage"), int) or result.get("stage") not in (1, 2, 3):
        raise ValueError("Invalid source offer stage")
    if not isinstance(result.get("sticky", True), bool):
        raise ValueError("Invalid source offer persistence policy")
    result["sticky"] = result.get("sticky", True)
    result["observed_at"] = _iso(result["observed_at"])
    if not result["start"] <= result["observed_at"] < result["end"]:
        raise ValueError("Unobserved future source offer")
    return result


def _offer_key(offer):
    """Deduplicate source evidence without applying its reward twice."""
    return offer["identity"]


def _allocation_key(allocation):
    """Identify a per-car cap reservation independently of feed provenance."""
    return allocation.get("car_n"), allocation["start"], allocation.get("cap_bucket")


def _allocation(item):
    """Validate retained cap evidence without recalculating cap policy."""
    result = _scope(item)
    car_n = result.get("car_n")
    if car_n is not None and (isinstance(car_n, bool) or not isinstance(car_n, int) or car_n < 0):
        raise ValueError("Invalid confirmation car identity")
    if not isinstance(result.get("source_id", ""), str) or (result.get("cap_bucket") is not None and not isinstance(result["cap_bucket"], str)):
        raise ValueError("Invalid confirmation allocation metadata")
    return result


def _clip(item, start, end):
    """Intersect a validated interval with a parent interval."""
    result = copy.deepcopy(item)
    result["start"], result["end"] = max(item["start"], start), min(item["end"], end)
    return result if result["start"] < result["end"] else None


def _overlay(base, additions):
    """Replace exactly the supplied scopes, preserving gaps and subdivisions."""
    result = copy.deepcopy(base)
    for addition in additions:
        remaining = []
        for segment in result:
            if segment["end"] <= addition["start"] or segment["start"] >= addition["end"]:
                remaining.append(segment)
                continue
            if segment["start"] < addition["start"]:
                left = copy.deepcopy(segment)
                left["end"] = addition["start"]
                remaining.append(left)
            if segment["end"] > addition["end"]:
                right = copy.deepcopy(segment)
                right["start"] = addition["end"]
                remaining.append(right)
        remaining.append(copy.deepcopy(addition))
        result = sorted(remaining, key=lambda segment: segment["start"])
    return result


def _apply_offer(segments, offer, start, end):
    """Apply one source operation exactly to known overlapping price scopes."""
    scope = _clip(offer, start, end)
    if scope is None:
        return segments
    changed = []
    for segment in segments:
        overlap = _clip(segment, scope["start"], scope["end"])
        if overlap is None:
            continue
        if offer["kind"] == "increment":
            overlap["rate"] = _rate(segment["rate"] + offer["rate"])
        elif offer["kind"] == "minimum":
            overlap["rate"] = min(segment["rate"], offer["rate"])
        else:
            overlap["rate"] = offer["rate"]
        overlap["sources"] = segment.get("sources", []) + [offer["source"]]
        overlap["quality"] = "observed"
        changed.append(overlap)
    return _overlay(segments, changed)


def _checksum(payload, generation):
    """Hash deterministic JSON including its generation number."""
    text = json.dumps({"generation": generation, "payload": payload}, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class RateHistory:
    """Small history ledger, independent of Home Assistant and provider clients."""

    def __init__(self, log=None, timezone="Europe/London", retention_days=2):
        """Create an empty ledger with local-calendar retention."""
        self.log = log
        self.timezone = timezone
        self._zone = ZoneInfo(timezone)
        self.retention_days = max(2, int(retention_days))
        self.records = {}
        self.contexts = {}
        self.offers = []
        self.dirty = False
        self.write_disabled = False
        self.generation = 0
        self._winner = None
        self._revision = 0
        self._save_lock = asyncio.Lock()

    @property
    def accounting_context(self):
        """Expose retained date-keyed accounting context to cost consumers."""
        return self.contexts

    def set_context(self, local_date, standing_charge, premium=False):
        """Retain the active local date's charge and car-premium coverage flag.

        The caller updates only the current date; an applied premium flag is
        sticky for that date. This is accounting context, not a historical
        correction interface or a premium amount/energy ledger.
        """
        key = _local_date(local_date)
        if not isinstance(premium, bool):
            raise ValueError("Car premium context must be a boolean")
        premium = premium or self.contexts.get(key, {}).get("car_premium_present", False)
        context = {"standing_charge": _rate(standing_charge), "car_premium_present": premium}
        if self.contexts.get(key) != context:
            self.contexts[key] = context
            self._changed()

    def retain_offer(self, offer, observed_at):
        """Retain an active source operation at full bounds, never future parents.

        Adapters pass qualified identities to observe; the resolver applies
        these operations once, at their stages, against the latest base.
        """
        scoped = _scope(offer)
        now = _iso(observed_at)
        if not scoped["start"] <= now < scoped["end"]:
            return False
        retained = _offer(dict(scoped, observed_at=now))
        key = _offer_key(retained)
        for index, previous in enumerate(self.offers):
            if _offer_key(previous) == key:
                if retained["direction"] != previous["direction"] or retained["source"] != previous["source"]:
                    raise ValueError("Source offer identity reused across directions or sources")
                if now < previous["observed_at"] or not previous["start"] <= now < previous["end"]:
                    return False
                if retained != previous:
                    self.offers[index] = retained
                    self._changed()
                return True
        self.offers.append(retained)
        self._changed()
        return True

    def active_offers(self, direction, now):
        """Return applicable full-bound evidence for single-stage adapter pricing."""
        now = _iso(now)
        if direction not in ("import", "export"):
            raise ValueError("Invalid offer direction")
        return copy.deepcopy([offer for offer in self.offers if offer["direction"] == direction and offer["start"] <= now < offer["end"] and offer["observed_at"] <= now])

    def offers_for(self, direction, start, end):
        """Return all observed source operations intersecting a billing parent."""
        start, end = _iso(start), _iso(end)
        if direction not in ("import", "export") or start >= end:
            raise ValueError("Invalid source offer lookup scope")
        return copy.deepcopy(sorted([offer for offer in self.offers if offer["direction"] == direction and offer["start"] < end and start < offer["end"]], key=lambda offer: offer["stage"]))

    def withdraw_offer(self, identity, observed_at):
        """Withdraw mutable active evidence without rewriting closed prices."""
        now = _iso(observed_at)
        for index, offer in enumerate(self.offers):
            if offer["identity"] != identity:
                continue
            if offer["sticky"] or offer["end"] <= now or now < offer["observed_at"]:
                return False
            del self.offers[index]
            for record in self.records.values():
                if record["state"] == "open" and identity in record["offer_ids"]:
                    record["offer_ids"].remove(identity)
            self._changed()
            return True
        return False

    def _changed(self):
        """Mark a mutation for a durable retry."""
        self.dirty = True
        self._revision += 1

    def _warn(self, message):
        """Report recovery issues without requiring a logger."""
        if self.log:
            self.log("Rate history: " + message)

    def _key(self, direction, start, end):
        """Build an identity from actual direction and UTC billing bounds."""
        if direction not in ("import", "export"):
            raise ValueError("Invalid history direction")
        return direction, start, end

    def _new_record(self, direction, period, observed_at):
        """Construct an observed parent; future parents are never constructed."""
        return {
            "direction": direction,
            "start": period["start"],
            "end": period["end"],
            "source_id": period.get("source_id", ""),
            "boundary_origin": period.get("boundary_origin", "provider"),
            "state": "open",
            "quality": "observed",
            "last_observed_at": observed_at,
            "automatic": [],
            "events": [],
            "offer_ids": [],
            "confirmations": [],
            "allocations": [],
            "manual": [],
            "baseline": [],
        }

    def observe(self, direction, periods, automatic_segments, manual_operations, observed_at, baseline_segments=None, base_segments=None, event_segments=None, offer_ids=None):
        """Observe only current parents and applicable automatic evidence.

        ``base_segments`` supplies the latest event-free automatic profile for
        the parent and replaces it each cycle, including withdrawn provisional
        IOG prices. Otherwise only currently applicable ``automatic_segments``
        are admitted and elapsed observed automatic scopes are retained.
        ``offer_ids`` references full observed operations, resolved in stage order.
        Previously admitted ended child offers remain until the parent closes.
        ``event_segments`` supplies already-resolved effective prices at exact event
        bounds, not additive rewards; future offers are ignored. Baseline is a
        separate no-IO profile. Manual operations are ordered replace/increment
        operations from the existing parser, never inferred from effective prices.
        """
        now = _iso(observed_at)
        self.close_elapsed(observed_at)
        periods = [_scope(period, price=False) for period in periods]
        automatic = [_scope(segment) for segment in automatic_segments]
        base = None if base_segments is None else [_scope(segment) for segment in base_segments]
        events = [_scope(segment) for segment in (event_segments or [])]
        baseline = [_scope(segment) for segment in (baseline_segments or [])]
        manual = [_scope(operation) for operation in manual_operations]
        offers_by_id = {offer["identity"]: offer for offer in self.offers}
        if offer_ids is not None and (not isinstance(offer_ids, list) or any(not isinstance(identity, str) or identity not in offers_by_id for identity in offer_ids)):
            raise ValueError("Unknown observed source offer identity")
        for operation in manual:
            if operation.get("kind") not in ("replace", "increment"):
                raise ValueError("Invalid manual operation")
        for period in periods:
            if not period["start"] <= now < period["end"]:
                continue
            key = self._key(direction, period["start"], period["end"])
            record = self.records.get(key)
            if record and record["state"] == "closed":
                continue
            # Conflicting new boundaries cannot reopen or overlap an existing parent.
            if any(other_key != key and other["direction"] == direction and other["start"] < period["end"] and period["start"] < other["end"] for other_key, other in self.records.items()):
                continue
            record = record or self._new_record(direction, period, now)
            if now < record["last_observed_at"]:
                continue
            candidates = base if base is not None else [segment for segment in automatic if segment["start"] <= now < segment["end"]]
            additions = [clipped for segment in candidates if (clipped := _clip(segment, record["start"], record["end"]))]
            if base is not None:
                # Explicit base is provisional; only events and confirmations stick.
                record["automatic"] = _overlay([], additions)
            else:
                elapsed = [segment for segment in record["automatic"] if segment["end"] <= now]
                record["automatic"] = _overlay(_overlay(record["automatic"], additions), elapsed)
            active_events = [clipped for segment in events if segment["start"] <= now < segment["end"] and (clipped := _clip(segment, record["start"], record["end"]))]
            record["events"] = _overlay(record["events"], active_events)
            if offer_ids is not None:
                ended = [identity for identity in record["offer_ids"] if identity in offers_by_id and offers_by_id[identity]["end"] <= now]
                qualified = [
                    identity
                    for identity in offer_ids
                    if offers_by_id[identity]["direction"] == direction and offers_by_id[identity]["observed_at"] <= now and offers_by_id[identity]["start"] < record["end"] and record["start"] < offers_by_id[identity]["end"]
                ]
                record["offer_ids"] = list(dict.fromkeys(ended + qualified))
            record["manual"] = [operation for operation in manual if operation["start"] <= now < operation["end"]]
            if baseline_segments is not None:
                record["baseline"] = [clipped for segment in baseline if (clipped := _clip(segment, record["start"], record["end"]))]
            record["last_observed_at"] = now
            self.records[key] = record
            self._changed()

    def confirm_iog(self, start, end, rate, observed_at, source_id="", car_n=None, cap_bucket=None):
        """Accept caller-qualified evidence for its whole import billing half-hour.

        Future confirmations are rejected. Late evidence may fill a missing
        parent, but cannot rewrite a different closed price. Eligibility, meter
        freshness and cap policy are exclusively the caller's responsibility.
        """
        segment = _scope({"start": start, "end": end, "rate": rate, "sources": ["iog_confirmed"], "quality": "confirmed", "source_id": source_id, "car_n": car_n, "cap_bucket": cap_bucket})
        now = _iso(observed_at)
        if _utc(end) - _utc(start) != timedelta(minutes=30) or _utc(start).minute % 30 or _utc(start).second or _utc(start).microsecond or now < segment["start"]:
            return False
        allocation = _allocation({field: segment[field] for field in ("start", "end", "rate", "source_id", "car_n", "cap_bucket")})
        allocation_key = _allocation_key(allocation)
        key = self._key("import", segment["start"], segment["end"])
        record = self.records.get(key)
        if record and record["state"] == "closed":
            if not record["segments"] or any(item["rate"] != segment["rate"] for item in record["segments"]):
                return False
            if not any(_allocation_key(item) == allocation_key for item in record["allocations"]):
                record["allocations"].append(allocation)
                self._changed()
            return True
        if record is None:
            if any(other["direction"] == "import" and other["start"] < segment["end"] and segment["start"] < other["end"] for other in self.records.values()):
                return False
            record = self._new_record("import", segment, min(now, _iso(_utc(end) - timedelta(microseconds=1))))
            self.records[key] = record
        allocated = any(_allocation_key(item) == allocation_key for item in record["allocations"])
        if record["confirmations"] == [segment] and record["quality"] == "confirmed" and allocated:
            return True
        if not allocated:
            record["allocations"].append(allocation)
        record["confirmations"] = [segment]
        record["quality"] = "confirmed"
        self._changed()
        return True

    def _resolve(self, record, closing=False):
        """Resolve automatic evidence then the last applicable manual state."""
        segments = _overlay(record["automatic"], record["confirmations"])
        operations = [offer for offer in self.offers if offer["identity"] in record["offer_ids"]]
        for offer in sorted(operations, key=lambda operation: operation["stage"]):
            segments = _apply_offer(segments, offer, record["start"], record["end"])
        segments = _overlay(segments, record["events"])
        for operation in record["manual"]:
            # Closing uses the instant immediately inside the boundary, not rounding.
            applies = operation["start"] < record["end"] <= operation["end"] if closing else operation["start"] <= record["last_observed_at"] < operation["end"]
            if not applies:
                continue
            sources = [operation.get("source", "manual")]
            if operation["kind"] == "replace":
                segments = [{"start": record["start"], "end": record["end"], "rate": operation["rate"], "sources": sources, "quality": "observed"}]
            else:
                segments = [dict(segment, rate=_rate(segment["rate"] + operation["rate"]), sources=segment.get("sources", []) + sources, quality="observed") for segment in segments]
        return segments

    def close_elapsed(self, observed_at):
        """Close saved observations before any post-boundary rebuild is admitted."""
        now = _iso(observed_at)
        for record in self.records.values():
            if record["state"] == "open" and record["end"] <= now:
                record["segments"] = self._resolve(record, closing=True)
                record["state"] = "closed"
                record["closed_at"] = now
                missed = _utc(record["end"]) - _utc(record["last_observed_at"]) > timedelta(minutes=5)
                for segment in record["segments"]:
                    segment["quality"] = "confirmed" if segment.get("quality") == "confirmed" else "last_known" if missed else "observed"
                record["quality"] = "confirmed" if record["segments"] and all(segment["quality"] == "confirmed" for segment in record["segments"]) else "last_known" if missed else "observed"
                self._changed()
        self.prune(observed_at)

    def prune(self, observed_at):
        """Retain closed scopes overlapping today and the retained local dates."""
        today = _utc(observed_at).astimezone(self._zone).date()
        cutoff_date = today - timedelta(days=self.retention_days - 1)
        cutoff = _iso(datetime.combine(cutoff_date, datetime.min.time(), tzinfo=self._zone))
        removed = [key for key, record in self.records.items() if record["state"] == "closed" and record["end"] <= cutoff]
        for key in removed:
            del self.records[key]
        old_contexts = [key for key in self.contexts if key < cutoff_date.isoformat()]
        for key in old_contexts:
            del self.contexts[key]
        open_ids = {identity for record in self.records.values() if record["state"] == "open" for identity in record["offer_ids"]}
        retained_offers = [offer for offer in self.offers if offer["end"] > cutoff or offer["identity"] in open_ids]
        pruned_offers = len(retained_offers) != len(self.offers)
        self.offers = retained_offers
        if removed or old_contexts or pruned_offers:
            self._changed()

    def lookup(self, direction, start, end, include_open=True, baseline=False):
        """Return observed segments with gaps omitted and no future-parent projection."""
        start, end = _iso(start), _iso(end)
        self._key(direction, start, end)
        if start >= end:
            return []
        result = []
        for record in self.records.values():
            if record["direction"] != direction or (record["state"] == "open" and not include_open):
                continue
            segments = record["baseline"] if baseline else record["segments"] if record["state"] == "closed" else self._resolve(record)
            for segment in segments:
                clipped = _clip(segment, start, end)
                if clipped:
                    clipped["quality"] = segment.get("quality", "observed" if record["state"] == "open" else record["quality"])
                    if baseline and clipped["quality"] == "confirmed":
                        clipped["quality"] = "observed"
                    clipped["state"] = record["state"]
                    result.append(clipped)
        return sorted(result, key=lambda segment: segment["start"])

    def snapshot(self):
        """Return versioned plain transport suitable for debug replay or persistence."""
        return {
            "schema_version": SCHEMA_VERSION,
            "timezone": self.timezone,
            "currency_unit": "minor_per_kwh",
            "accounting_context": copy.deepcopy(dict(sorted(self.contexts.items()))),
            "offers": copy.deepcopy(self.offers),
            "records": copy.deepcopy(sorted(self.records.values(), key=lambda record: (record["direction"], record["start"], record["end"]))),
        }

    def restore(self, payload):
        """Atomically validate and restore observed records; reject projections."""
        if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("Unsupported history schema")
        ZoneInfo(payload["timezone"])
        if payload.get("currency_unit") != "minor_per_kwh" or not isinstance(payload.get("records"), list):
            raise ValueError("Invalid history envelope")
        records = {}
        for item in payload["records"]:
            record = _scope(item, price=False)
            record["offer_ids"] = record.get("offer_ids", [])
            if not isinstance(record["offer_ids"], list) or any(not isinstance(identity, str) for identity in record["offer_ids"]) or len(set(record["offer_ids"])) != len(record["offer_ids"]):
                raise ValueError("Invalid parent source offer identities")
            key = self._key(record["direction"], record["start"], record["end"])
            if key in records or record["state"] not in ("open", "closed") or record["quality"] not in ("observed", "confirmed", "last_known"):
                raise ValueError("Invalid history record")
            observed = _iso(record["last_observed_at"])
            if not record["start"] <= observed < record["end"]:
                raise ValueError("Unobserved future history record")
            record["last_observed_at"] = observed
            for field in ("automatic", "events", "confirmations", "baseline", "manual") + (("segments",) if record["state"] == "closed" else ()):
                values = [_scope(segment) for segment in record[field]]
                if any(segment["start"] < record["start"] or segment["end"] > record["end"] for segment in values) and field != "manual":
                    raise ValueError("History segment outside parent")
                if field != "manual" and any(left["end"] > right["start"] for left, right in zip(values, values[1:])):
                    raise ValueError("Unordered or overlapping history segments")
                if field == "manual" and any(operation.get("kind") not in ("replace", "increment") or not operation["start"] <= observed < operation["end"] for operation in values):
                    raise ValueError("Invalid or unobserved manual operation")
                if field in ("events", "confirmations") and any(segment["start"] > observed for segment in values):
                    raise ValueError("Unobserved future history evidence")
                record[field] = values
            proof_rates = {item["rate"] for item in record["confirmations"]}
            allocation_items = []
            for allocation in record["allocations"]:
                allocation = copy.deepcopy(allocation)
                if "rate" not in allocation:
                    if len(proof_rates) != 1:
                        raise ValueError("Missing allocation price evidence")
                    allocation["rate"] = next(iter(proof_rates))
                allocation_items.append(_allocation(allocation))
            record["allocations"] = allocation_items
            if any(allocation["start"] != record["start"] or allocation["end"] != record["end"] for allocation in record["allocations"]):
                raise ValueError("Invalid confirmation allocation")
            if len({_allocation_key(allocation) for allocation in record["allocations"]}) != len(record["allocations"]):
                raise ValueError("Duplicate confirmation allocation")
            if record["state"] == "closed":
                record["closed_at"] = _iso(record["closed_at"])
                if record["closed_at"] < record["end"]:
                    raise ValueError("Premature history closure")
            if any(other["direction"] == record["direction"] and other["start"] < record["end"] and record["start"] < other["end"] for other in records.values()):
                raise ValueError("Overlapping history parents")
            records[key] = record
        contexts = {}
        accounting_context = payload.get("accounting_context", payload.get("contexts", {}))
        if not isinstance(accounting_context, dict):
            raise ValueError("Invalid history contexts")
        for key, context in accounting_context.items():
            canonical_date = _local_date(key)
            if not isinstance(context, dict) or not isinstance(context.get("car_premium_present"), bool):
                raise ValueError("Invalid daily accounting context")
            contexts[canonical_date] = {"standing_charge": _rate(context["standing_charge"]), "car_premium_present": context["car_premium_present"]}
        if not isinstance(payload.get("offers", []), list):
            raise ValueError("Invalid retained source offers")
        offers = [_offer(offer) for offer in payload.get("offers", [])]
        if len({_offer_key(offer) for offer in offers}) != len(offers):
            raise ValueError("Duplicate retained source offer")
        offers_by_id = {offer["identity"]: offer for offer in offers}
        for record in records.values():
            if record["state"] == "open":
                for identity in record["offer_ids"]:
                    offer = offers_by_id.get(identity)
                    if offer is None or offer["direction"] != record["direction"] or offer["start"] >= record["end"] or offer["end"] <= record["start"]:
                        raise ValueError("Unknown or inapplicable open-parent source offer")
        self.records = records
        self.contexts = contexts
        self.offers = offers
        self._changed()

    async def load(self, storage):
        """Load the newest valid snapshot, retaining fallback and unknown schemas."""
        valid = []
        unsupported = []
        failed = False
        for key in ("snapshot_a", "snapshot_b"):
            try:
                envelope = await storage.load("rate_history", key)
                if envelope is None:
                    continue
                generation = envelope["generation"]
                if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
                    raise ValueError("Invalid generation")
                payload = envelope["payload"]
                if envelope["checksum"] != _checksum(payload, generation):
                    raise ValueError("History checksum mismatch")
                if payload.get("schema_version", 0) > SCHEMA_VERSION:
                    unsupported.append(generation)
                    continue
                probe = RateHistory(timezone=self.timezone, retention_days=self.retention_days)
                probe.restore(payload)
                valid.append((generation, key, payload))
            except Exception as error:
                failed = True
                self._warn("Ignoring invalid {}: {}".format(key, error))
        winner = max(valid, key=lambda item: item[0]) if valid else None
        self.write_disabled = bool(unsupported and max(unsupported) > (winner[0] if winner else 0)) or (failed and not valid)
        if winner:
            self.restore(winner[2])
            self.generation, self._winner = winner[:2]
            self.dirty = False
            return True
        return False

    async def save_dirty(self, storage):
        """Write only the spare snapshot; unsuccessful writes retain dirty memory."""
        async with self._save_lock:
            if not self.dirty or self.write_disabled:
                return False
            payload = self.snapshot()
            generation = self.generation + 1
            envelope = {"generation": generation, "payload": payload, "checksum": _checksum(payload, generation)}
            key = "snapshot_b" if self._winner == "snapshot_a" else "snapshot_a"
            revision = self._revision
            try:
                saved = await storage.save("rate_history", key, envelope, format="json", expiry=None)
            except Exception as error:
                self._warn("Save failed: {}".format(error))
                return False
            if saved is not True:
                return False
            self.generation, self._winner = generation, key
            if revision == self._revision:
                self.dirty = False
            return True
