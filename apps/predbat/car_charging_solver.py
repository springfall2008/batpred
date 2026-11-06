"""Pure bounded EV/battery scheduling; no HA, file, network or actuator access.

All powers are AC kW at the site bus, except stored energies, which are cell-side
kWh. Demand mode is battery-first, hold permits PV charging only, and charge uses
the full configured charging rate. Battery saturation, reserve exhaustion, EV
completion and departure split a step into constant-power segments. Caps are
checked on every segment, never on an average that hides a short power peak.

This is a single-EV AC-equivalent search model, not a hardware controller or a
proof of global optimality. Expected availability never establishes connection.
"""

from dataclasses import dataclass, replace
import math
from typing import Optional, Sequence, Tuple

STATUS_FEASIBLE = "feasible"
STATUS_BOUNDED_NO_PLAN = "bounded_no_plan"
BATTERY_DEMAND, BATTERY_HOLD, BATTERY_CHARGE = "demand", "hold", "charge"
BATTERY_MODES = (BATTERY_DEMAND, BATTERY_HOLD, BATTERY_CHARGE)
BATTERY_LIMITED_DEMAND = "limited_demand"
EV_OFF, EV_ON = "off", "on"
EPSILON = 1e-9


@dataclass(frozen=True)
class Slot:
    """Constant-power/rate interval; house and PV are AC kWh over duration_min."""

    index: int
    start_min: int
    duration_min: int
    ac_load_kwh: float
    pv_kwh: float
    import_price: float
    export_price: float
    battery_min_kwh: float = 0


@dataclass(frozen=True)
class EVConfig:
    """Fixed EV rate, cell-side energy request and expected availability indices.

    Pause/restart percentages refer to the DOMESTIC battery, not the EV. An
    OFF-to-ON transition requires restart SoC and the minimum off interval;
    continuing ON requires strictly more than pause SoC. Initial relay state is
    explicit modelling input, never inferred from an expected arrival. The default
    models a car already off for ten minutes, not permission to energise it.
    """

    battery_kwh: float
    start_soc_kwh: float
    stop_soc_kwh: float
    rate_kw: float
    efficiency: float
    deadline_min: int = 0
    availability: Optional[Tuple[int, ...]] = None
    pause_battery_percent: float = 15
    restart_battery_percent: float = 25
    min_off_minutes: float = 10
    initially_on: bool = False
    initial_off_minutes: float = 10


@dataclass(frozen=True)
class BatteryConfig:
    """Cell-side energy bounds, AC rate limits and AC/cell efficiencies.

    Cycle cost is a configured wear surrogate per AC kWh discharged. Terminal
    value is per stored kWh; the objective subtracts that credit exactly once.
    """

    capacity_kwh: float
    initial_kwh: float
    reserve_kwh: float
    max_charge_kw: float
    max_discharge_kw: float
    charge_efficiency: float
    discharge_efficiency: float
    end_min_kwh: float
    cycle_cost_per_kwh: float = 0
    terminal_value_per_kwh: float = 0


@dataclass(frozen=True)
class SiteConfig:
    """Whole-site AC grid caps, separately from an optional house-plus-EV load-port cap."""

    import_cap_kw: float
    export_cap_kw: Optional[float] = None
    load_port_cap_kw: Optional[float] = None
    charge_import_cap_kw: Optional[float] = None


@dataclass(frozen=True)
class PlannerConfig:
    """Bounded search controls; beam=None retains exact states for at most 12 slots.

    Bucketing merges search labels, never stored energy. Such merging and beam
    pruning can miss a feasible/cheaper plan, so a miss is not infeasibility proof.

    `allowed_battery_modes` declares the subset of battery dispatch modes the
    planner may emit. It must be a tuple or list of string tags drawn from
    the supported battery-mode vocabulary (`demand`, `hold`, `charge`). The
    default equals the full base vocabulary; a tighter subset is honoured by
    `_validate` and the search loop. When `allow_limited_discharge` is true,
    `limited_demand` is added on top of whatever subset is declared here.
    The planner never returns a mode that is not in this subset.

    `battery_min_window_minutes` declares the minimum lifetime (in minutes) of
    any newly committed battery mode before the next mode can be selected.
    Accepted values are 5, 15, 30 or 60. The default of 5 reproduces the
    previous solver: each new mode persists for at least one slot, so mode
    changes can happen every step. The window constraint only governs the
    battery mode; EV on/off and delivered kWh are evaluated independently
    inside every slot, so EV decisions are never frozen within a window.

    `initial_window_mode` and `initial_window_remaining_minutes` describe the
    battery mode that is in effect at the horizon start and the minutes it
    still has left in its current window. They are orthogonal to
    `initial_battery_mode` (which remains the "first-step" hard constraint
    for ASAP-style first-action forcing). When `initial_window_remaining_minutes`
    is zero, no commitment is carried in: the first node simply holds the
    declared mode tag with no remaining time, so any mode may be picked for
    slot 0 and, if a different mode is picked, it must persist for the full
    new window. A non-zero remaining time locks the initial mode until it
    elapses; then a fresh full window begins for the next chosen mode.
    """

    time_step_minutes: int = 5
    beam_width: Optional[int] = 128
    soc_bucket_kwh: float = 0.05
    allow_limited_discharge: bool = False
    allowed_battery_modes: Tuple[str, ...] = BATTERY_MODES
    economic_diversity: bool = False
    initial_battery_mode: Optional[str] = None
    require_ev_start: bool = False
    solar_ev_only: bool = False
    battery_min_window_minutes: int = 5
    initial_window_mode: Optional[str] = None
    initial_window_remaining_minutes: int = 0


@dataclass(frozen=True)
class FlowSegment:
    """One constant-power portion of a slot, including instantaneous constraint evidence."""

    duration_min: float
    load_kw: float
    ev_kw: float
    pv_kw: float
    battery_charge_kw: float
    battery_discharge_kw: float
    grid_import_kw: float
    grid_export_kw: float
    curtailed_kw: float
    soc_start_kwh: float
    soc_end_kwh: float


@dataclass(frozen=True)
class SlotAction:
    """Integrated slot flows; load_kwh excludes EV, which is reported separately."""

    slot_index: int
    battery_mode: str
    ev_action: str
    load_kwh: float
    pv_kwh: float
    pv_to_load_kwh: float
    pv_to_battery_kwh: float
    pv_curtailed_kwh: float
    pv_exported_kwh: float
    grid_import_kwh: float
    grid_export_kwh: float
    battery_discharge_kwh: float
    battery_charge_kwh: float
    battery_soc_kwh_start: float
    battery_soc_kwh_end: float
    ev_soc_kwh_start: float
    ev_soc_kwh_end: float
    ev_delivered_kwh: float
    import_price: float
    export_price: float
    slot_cost: float
    ev_on_minutes: float
    segments: Tuple[FlowSegment, ...]
    discharge_limit_kw: Optional[float] = None


@dataclass(frozen=True)
class Diagnostics:
    """Completion, physical accounting and deterministic search-work evidence."""

    unmet_load_kwh: float = 0
    pv_curtailed_total_kwh: float = 0
    ev_delivered_total_kwh: float = 0
    final_battery_soc_kwh: float = 0
    final_ev_soc_kwh: float = 0
    ev_stop_target_met: bool = False
    battery_end_min_met: bool = False
    ev_on_slot_count: int = 0
    mode_counts: Tuple[Tuple[str, int], ...] = ()
    notes: Tuple[str, ...] = ()
    transitions_evaluated: int = 0
    max_frontier_size: int = 0


@dataclass(frozen=True)
class PlanResult:
    """A complete feasible candidate or an explicit search miss with no proposed actions."""

    status: str
    total_cost: Optional[float]
    actions: Tuple[SlotAction, ...]
    battery_soc_trace: Tuple[float, ...]
    ev_soc_trace: Tuple[float, ...]
    import_kwh_trace: Tuple[float, ...]
    export_kwh_trace: Tuple[float, ...]
    curtail_kwh_trace: Tuple[float, ...]
    diagnostics: Diagnostics
    deadline_slot_index: int


class PlannerInputError(ValueError):
    """Malformed inputs, not a verdict that a correctly formed request is infeasible."""


def _require_finite(name, value, low=None, high=None):
    """Validate without coercing strings/booleans or echoing private input values."""
    if type(value) not in (int, float) or not math.isfinite(value) or (low is not None and value < low) or (high is not None and value > high):
        raise PlannerInputError("Invalid numeric field: " + name)


def _validate(slots, ev, battery, site, planner):
    """Check units' domains, chronological coverage, state and bounded-search configuration."""
    if not isinstance(ev, EVConfig) or not isinstance(battery, BatteryConfig) or not isinstance(site, SiteConfig) or not isinstance(planner, PlannerConfig):
        raise PlannerInputError("Expected typed planner configuration")
    if type(planner.time_step_minutes) is not int or planner.time_step_minutes not in (5, 15, 30):
        raise PlannerInputError("Unsupported planning step")
    if planner.beam_width is not None and (type(planner.beam_width) is not int or not 1 <= planner.beam_width <= 4096):
        raise PlannerInputError("beam_width must be an integer in 1..4096 or None")
    if type(planner.allow_limited_discharge) is not bool:
        raise PlannerInputError("allow_limited_discharge must be boolean")
    if type(planner.require_ev_start) is not bool:
        raise PlannerInputError("require_ev_start must be boolean")
    if type(planner.solar_ev_only) is not bool:
        raise PlannerInputError("solar_ev_only must be boolean")
    allowed = planner.allowed_battery_modes
    if not isinstance(allowed, (tuple, list)) or isinstance(allowed, (str, bytes)):
        raise PlannerInputError("allowed_battery_modes must be a tuple or list")
    if not allowed:
        raise PlannerInputError("allowed_battery_modes must not be empty")
    seen = set()
    for mode in allowed:
        if type(mode) is not str:
            raise PlannerInputError("allowed_battery_modes entries must be strings")
        if mode not in BATTERY_MODES:
            raise PlannerInputError("allowed_battery_modes contains unsupported mode")
        if mode in seen:
            raise PlannerInputError("allowed_battery_modes contains duplicate mode")
        seen.add(mode)
    if planner.initial_battery_mode is not None and (type(planner.initial_battery_mode) is not str or planner.initial_battery_mode not in allowed):
        raise PlannerInputError("initial_battery_mode must be an allowed mode or None")
    if type(planner.battery_min_window_minutes) is not int or planner.battery_min_window_minutes not in (5, 15, 30, 60):
        raise PlannerInputError("battery_min_window_minutes must be an int in {5, 15, 30, 60}")
    if planner.initial_window_mode is not None:
        if type(planner.initial_window_mode) is not str:
            raise PlannerInputError("initial_window_mode must be a string or None")
        supported = set(allowed) | ({BATTERY_LIMITED_DEMAND} if planner.allow_limited_discharge else set())
        if planner.initial_window_mode not in supported:
            raise PlannerInputError("initial_window_mode must be an allowed mode (+limited_demand when supported) or None")
    if type(planner.initial_window_remaining_minutes) is not int or not 0 <= planner.initial_window_remaining_minutes <= 60:
        raise PlannerInputError("initial_window_remaining_minutes must be an int in 0..60")
    if planner.initial_window_remaining_minutes > planner.battery_min_window_minutes:
        raise PlannerInputError("initial_window_remaining_minutes must not exceed battery_min_window_minutes")
    if planner.initial_window_remaining_minutes > 0 and planner.initial_window_mode is None:
        raise PlannerInputError("initial_window_remaining_minutes > 0 requires initial_window_mode")
    _require_finite("soc_bucket_kwh", planner.soc_bucket_kwh, 1e-6)
    if not slots or len(slots) > 576 or (planner.beam_width is None and len(slots) > 12):
        raise PlannerInputError("Horizon must have 1..576 slots (at most 12 for exhaustive search)")
    end = None
    for index, slot in enumerate(slots):
        if not isinstance(slot, Slot) or type(slot.index) is not int or slot.index != index:
            raise PlannerInputError("Slot indices must be consecutive")
        if type(slot.start_min) is not int or slot.start_min < 0 or type(slot.duration_min) is not int or slot.duration_min <= 0 or slot.duration_min % planner.time_step_minutes:
            raise PlannerInputError("Invalid slot time")
        if end is not None and slot.start_min != end:
            raise PlannerInputError("Slots must be chronological and contiguous")
        end = slot.start_min + slot.duration_min
        for field in ("ac_load_kwh", "pv_kwh"):
            _require_finite(field, getattr(slot, field), 0)
        for field in ("import_price", "export_price"):
            _require_finite(field, getattr(slot, field))
        _require_finite("slot_battery_min", slot.battery_min_kwh, 0, battery.capacity_kwh)
    _require_finite("capacity_kwh", battery.capacity_kwh, 1e-6)
    for field in ("initial_kwh", "reserve_kwh", "end_min_kwh"):
        _require_finite(field, getattr(battery, field), 0, battery.capacity_kwh)
    if battery.initial_kwh < battery.reserve_kwh or battery.end_min_kwh < battery.reserve_kwh:
        raise PlannerInputError("Battery energy and terminal floor must respect domestic reserve")
    for field in ("max_charge_kw", "max_discharge_kw", "cycle_cost_per_kwh", "terminal_value_per_kwh"):
        _require_finite(field, getattr(battery, field), 0)
    for field in ("charge_efficiency", "discharge_efficiency"):
        _require_finite(field, getattr(battery, field), 1e-6, 1)
    _require_finite("ev_capacity", ev.battery_kwh, 1e-6)
    for field in ("start_soc_kwh", "stop_soc_kwh"):
        _require_finite(field, getattr(ev, field), 0, ev.battery_kwh)
    _require_finite("ev_rate", ev.rate_kw, 1e-6)
    _require_finite("ev_efficiency", ev.efficiency, 1e-6, 1)
    _require_finite("pause_battery_percent", ev.pause_battery_percent, 0, 100)
    _require_finite("restart_battery_percent", ev.restart_battery_percent, ev.pause_battery_percent, 100)
    for field in ("min_off_minutes", "initial_off_minutes"):
        _require_finite(field, getattr(ev, field), 0)
    if type(ev.initially_on) is not bool or type(ev.deadline_min) is not int or ev.deadline_min < 0:
        raise PlannerInputError("Invalid EV initial state or deadline")
    if ev.availability is not None and (not isinstance(ev.availability, tuple) or any(type(i) is not int or not 0 <= i < len(slots) for i in ev.availability)):
        raise PlannerInputError("Availability must contain original slot indices")
    _require_finite("import_cap_kw", site.import_cap_kw, 0)
    if site.charge_import_cap_kw is not None:
        _require_finite("charge_import_cap_kw", site.charge_import_cap_kw, 1e-6, site.import_cap_kw)
    for field in ("export_cap_kw", "load_port_cap_kw"):
        if getattr(site, field) is not None:
            _require_finite(field, getattr(site, field), 0)


def _ev_is_available(ev, slot_index):
    """Expected availability only, deliberately unrelated to actual connection permission."""
    return ev.availability is None or slot_index in ev.availability


def _deadline_slot_index(slots, deadline_min):
    """Last original slot containing any time before departure, including a partial interval."""
    return max((s.index for s in slots if s.start_min < deadline_min), default=-1)


def _bucket(value, width):
    """Bucket search labels only; physical state values are never rounded or overwritten."""
    return math.floor(value / width)


@dataclass(slots=True)
class _Node:
    """One retained search label, including the EV's execution-relevant temporal state.

    `mode` and `mode_remaining_minutes` carry the minimum-mode-window state:
    the battery mode currently in effect for the next slot expansion and the
    minutes left in its current window. Without an explicit initial commitment,
    the default 5-minute window retains the original search shape and dedup key.
    When `battery_min_window_minutes` is greater than 5 these fields
    participate in the dedup key to keep semantically distinct commitments
    from collapsing in `_prune`.
    """

    battery_kwh: float
    ev_kwh: float
    on: bool
    off_minutes: float
    cost: float = 0
    switches: int = 0
    parent: Optional["_Node"] = None
    action: Optional[SlotAction] = None
    mode: Optional[str] = None
    mode_remaining_minutes: float = 0


def _segment(duration, house_kw, ev_kw, pv_kw, soc, mode, battery, ev, site):
    """Simulate constant demand, splitting at full/reserve boundaries; reject any power excess."""
    segments = []
    remaining = duration / 60
    while remaining > 1e-12:
        demand = house_kw + ev_kw
        if site.load_port_cap_kw is not None and demand > site.load_port_cap_kw + EPSILON:
            return None
        charge = discharge = 0.0
        if soc < battery.capacity_kwh - EPSILON:
            charge = battery.max_charge_kw if mode == BATTERY_CHARGE else min(max(pv_kw - demand, 0), battery.max_charge_kw)
            if mode == BATTERY_CHARGE and site.charge_import_cap_kw is not None:
                # A grid-charge REQUEST need not mean physical battery charging:
                # house/EV loads take priority. Use headroom to charge, or supply
                # the grid deficit from the battery. The site cap below still
                # rejects insufficient support, including reserve exhaustion.
                charge = min(charge, max(site.charge_import_cap_kw - demand + pv_kw, 0))
                if soc > battery.reserve_kwh + EPSILON:
                    discharge = min(max(demand - pv_kw - site.charge_import_cap_kw, 0), battery.max_discharge_kw)
        if mode == BATTERY_DEMAND and soc > battery.reserve_kwh + EPSILON:
            discharge = min(max(demand - pv_kw, 0), battery.max_discharge_kw)
        net = demand + charge - pv_kw - discharge
        imported = max(net, 0)
        if imported > site.import_cap_kw + EPSILON:
            return None
        surplus = max(-net, 0)
        exported = min(surplus, site.export_cap_kw) if site.export_cap_kw is not None else surplus
        derivative = charge * battery.charge_efficiency - discharge / battery.discharge_efficiency
        hours = remaining
        if derivative > 0:
            hours = min(hours, (battery.capacity_kwh - soc) / derivative)
        elif derivative < 0:
            hours = min(hours, (soc - battery.reserve_kwh) / -derivative)
        next_soc = soc + hours * derivative
        if ev_kw and min(soc, next_soc) <= battery.capacity_kwh * ev.pause_battery_percent / 100 + EPSILON:
            return None
        segments.append(FlowSegment(hours * 60, house_kw, ev_kw, pv_kw, charge, discharge, imported, exported, surplus - exported, soc, next_soc))
        remaining -= hours
        soc = next_soc
    return tuple(segments), soc


def _resolve_action(slot, ev_on, mode, node, battery, ev, site):
    """Compute a complete physical step, never a partially served household/EV load."""
    on_minutes = 0.0
    discharge_limit = None
    if ev_on:
        if not _ev_is_available(ev, slot.index) or slot.start_min >= ev.deadline_min or node.ev_kwh >= ev.stop_soc_kwh - EPSILON:
            return None
        if not node.on and (node.off_minutes + EPSILON < ev.min_off_minutes or node.battery_kwh + EPSILON < battery.capacity_kwh * ev.restart_battery_percent / 100):
            return None
        on_minutes = min(slot.duration_min, ev.deadline_min - slot.start_min, (ev.stop_soc_kwh - node.ev_kwh) / ev.efficiency / ev.rate_kw * 60)
    flow_mode = mode
    if mode == BATTERY_LIMITED_DEMAND:
        # Still battery-first, but under a reproducible per-period power ceiling.
        # The fixed ceiling remains in place after a partly completed EV step;
        # it is not a fictitious continuously modulating grid-first controller.
        net_kw = (slot.ac_load_kwh - slot.pv_kwh) * 60 / slot.duration_min + (ev.rate_kw if on_minutes else 0)
        discharge_limit = min(battery.max_discharge_kw, max(0, math.ceil((net_kw - site.import_cap_kw) * 1000 + 2) / 1000))
        battery = replace(battery, max_discharge_kw=discharge_limit)
        flow_mode = BATTERY_DEMAND
    segments = []
    soc = node.battery_kwh
    for duration, ev_kw in ((on_minutes, ev.rate_kw), (slot.duration_min - on_minutes, 0)):
        if duration <= 1e-10:
            continue
        result = _segment(duration, slot.ac_load_kwh * 60 / slot.duration_min, ev_kw, slot.pv_kwh * 60 / slot.duration_min, soc, flow_mode, battery, ev, site)
        if result is None:
            return None
        parts, soc = result
        segments.extend(parts)
    if any(min(part.soc_start_kwh, part.soc_end_kwh) < slot.battery_min_kwh - EPSILON for part in segments):
        return None
    imported = sum(s.grid_import_kw * s.duration_min / 60 for s in segments)
    exported = sum(s.grid_export_kw * s.duration_min / 60 for s in segments)
    discharged = sum(s.battery_discharge_kw * s.duration_min / 60 for s in segments)
    charged = sum(s.battery_charge_kw * s.duration_min / 60 for s in segments)
    delivered = ev.rate_kw * on_minutes / 60
    pv_load = sum(min(s.pv_kw, s.load_kw + s.ev_kw) * s.duration_min / 60 for s in segments)
    pv_charge = sum(min(max(s.pv_kw - s.load_kw - s.ev_kw, 0), s.battery_charge_kw) * s.duration_min / 60 for s in segments)
    curtailed = sum(s.curtailed_kw * s.duration_min / 60 for s in segments)
    cost = imported * slot.import_price - exported * slot.export_price + discharged * battery.cycle_cost_per_kwh
    return SlotAction(
        slot.index,
        mode,
        EV_ON if on_minutes else EV_OFF,
        slot.ac_load_kwh,
        slot.pv_kwh,
        pv_load,
        pv_charge,
        curtailed,
        exported,
        imported,
        exported,
        discharged,
        charged,
        node.battery_kwh,
        soc,
        node.ev_kwh,
        node.ev_kwh + delivered * ev.efficiency,
        delivered,
        slot.import_price,
        slot.export_price,
        cost,
        on_minutes,
        tuple(segments),
        discharge_limit,
    )


def _prune(nodes, width, bucket, terminal_value, preserve_battery=False, economic_diversity=False):
    """Preserve EV progress and optionally stored-energy resilience within the same beam bound.

    The fixed beam budget often causes a small number of distinct EV-progress
    buckets to share it with many near-duplicate cost-ranked candidates. With no
    hold action, the cheapest label of a bucket may sit on a stored-energy path
    that prematurely imports during the current band and forecloses a cheap
    later pre-charge. Promoting only the highest-energy neighbour (the
    previous behaviour) keeps one extreme but can still drop the intermediate
    stored-energy trajectory that completes the cheaper plan.

    When ``preserve_battery`` is true, the cost leader stays at position 0 of
    its bucket (so the layer-by-layer budget picks it first across buckets).
    With opt-in ``economic_diversity``, the positions up to ``width`` are re-sorted by stored
    energy descending, so each per-bucket layer picks a different SoC level
    instead of `width`-many near-duplicates. Beyond the head, ranking order
    resumes unchanged. The frontier size and the per-slot evaluation budget
    stay within ``width``; this is a within-bucket ordering change, never an
    addition of new candidates. This remains bounded search, not an optimality proof.
    """
    groups = {}
    for node in nodes:
        groups.setdefault(_bucket(node.ev_kwh, bucket), []).append(node)
    for group in groups.values():
        group.sort(key=lambda n: (n.cost - terminal_value * n.battery_kwh, n.switches))
        if preserve_battery and len(group) > 2:
            # Cost leader stays at position 0; the next ``width - 1``
            # candidates are re-ordered by stored energy descending so layer
            # picks span SoC tiers instead of cost-ranked duplicates. Past
            # the head, ranking order resumes so all cheap-end behaviour is
            # unchanged.
            leader = group[0]
            high = max(group, key=lambda n: n.battery_kwh)
            if high is not leader:
                group.remove(high)
                group.insert(1, high)
            if economic_diversity:
                head_rest = list(group[1 : max(1, width)])
                tail = list(group[max(1, width) :])
                head_rest.sort(key=lambda n: -n.battery_kwh)
                group.clear()
                group.extend([leader] + head_rest + tail)
    ordered = [groups[key] for key in sorted(groups, reverse=True)]
    kept = []
    layer = 0
    while len(kept) < width:
        added = False
        for group in ordered:
            if layer < len(group):
                kept.append(group[layer])
                added = True
                if len(kept) == width:
                    break
        if not added:
            break
        layer += 1
    return kept


def _bounded_no_plan(deadline_idx, transitions, max_frontier, reason):
    """A bounded search miss is neither a partial schedule nor proof of physical impossibility."""
    diagnostic = Diagnostics(notes=(reason, "no_global_optimality_or_infeasibility_claim"), transitions_evaluated=transitions, max_frontier_size=max_frontier)
    return PlanResult(STATUS_BOUNDED_NO_PLAN, None, (), (), (), (), (), (), diagnostic, deadline_idx)


def _reachable_floor_envelope(slots, battery, site):
    """Optimistic charging bound that preserves time-local floors during pruning.

    Assume the EV is off and any grid/PV headroom can charge the battery, even
    where an actual discrete action could not use all that headroom. This only
    rejects states unable to reach a future floor under the most favourable
    charging assumption; it does not claim a feasible continuation exists.
    """
    required_end = [0.0] * len(slots)
    required = battery.end_min_kwh
    for slot in reversed(slots):
        required = max(required, slot.battery_min_kwh, battery.reserve_kwh)
        required_end[slot.index] = required
        headroom = max(0, site.import_cap_kw + (slot.pv_kwh - slot.ac_load_kwh) * 60 / slot.duration_min)
        gain = min(battery.max_charge_kw, headroom) * slot.duration_min / 60 * battery.charge_efficiency
        required = max(slot.battery_min_kwh, battery.reserve_kwh, required - gain)
    return required_end


def plan(slots: Sequence[Slot], ev: EVConfig, battery: BatteryConfig, site: SiteConfig, planner: Optional[PlannerConfig] = None) -> PlanResult:
    """Search eligible prices, coupled EV decisions and executable-mode physics.

    Hard constraints reject transitions before monetary ranking. All returned
    candidates meet useful demand and the terminal floor. The objective includes
    losses, actual grid purchases/exports, cycle cost and terminal stored energy.
    """
    planner = planner if planner is not None else PlannerConfig()
    _validate(slots, ev, battery, site, planner)
    base_modes = tuple(planner.allowed_battery_modes)
    modes = base_modes + ((BATTERY_LIMITED_DEMAND,) if planner.allow_limited_discharge and BATTERY_LIMITED_DEMAND not in base_modes else ())
    min_window = planner.battery_min_window_minutes
    # Only partition the dedup key by mode/remaining when the window
    # actually restricts the search. With the default 5-minute window and
    # any horizon, slot_duration >= 5 so the remaining minutes always fall to
    # zero after one slot, the new key components would carry no information,
    # and adding them would silently change the dedup partition (and therefore
    # the pruning frontier). Gate the new key components on a window strictly
    # larger than 5 so the default reproduces the previous solver exactly.
    windowed_dedup = min_window > 5
    required_end = _reachable_floor_envelope(slots, battery, site)
    deadline_idx = _deadline_slot_index(slots, ev.deadline_min)
    future = [0.0] * (len(slots) + 1)
    for i in range(len(slots) - 1, -1, -1):
        slot = slots[i]
        available_minutes = min(slot.duration_min, max(ev.deadline_min - slot.start_min, 0)) if _ev_is_available(ev, slot.index) else 0
        future[i] = future[i + 1] + available_minutes / 60 * ev.rate_kw * ev.efficiency
    if ev.start_soc_kwh + future[0] < ev.stop_soc_kwh - EPSILON:
        return _bounded_no_plan(deadline_idx, 0, 0, "insufficient_available_charging_time")
    initial_mode = planner.initial_window_mode
    initial_remaining = planner.initial_window_remaining_minutes
    # An explicit initial commitment also applies at min=5. Without one, the
    # compatibility default retains the original frontier and dedup shape.
    initial_carry_mode = initial_mode if windowed_dedup or initial_remaining else None
    initial_carry_remaining = initial_remaining
    frontier = [
        _Node(
            battery.initial_kwh,
            ev.start_soc_kwh,
            ev.initially_on,
            min(ev.initial_off_minutes, ev.min_off_minutes),
            mode=initial_carry_mode,
            mode_remaining_minutes=initial_carry_remaining,
        )
    ]
    max_frontier = 1
    transitions = 0
    pruned = False
    for i, slot in enumerate(slots):
        next_states = {}
        for node in frontier:
            if node.mode_remaining_minutes > 0:
                allowed_modes = (node.mode,)
            elif i == 0 and planner.initial_battery_mode is not None:
                allowed_modes = (planner.initial_battery_mode,)
            else:
                allowed_modes = modes
            if i == 0 and planner.initial_battery_mode is not None:
                allowed_modes = tuple(mode for mode in allowed_modes if mode == planner.initial_battery_mode)
            for mode in allowed_modes:
                for on in (True,) if i == 0 and planner.require_ev_start else (False, True):
                    if on and planner.solar_ev_only and (mode != BATTERY_DEMAND or slot.pv_kwh - slot.ac_load_kwh < ev.rate_kw * slot.duration_min / 60 - EPSILON):
                        continue
                    transitions += 1
                    action = _resolve_action(slot, on, mode, node, battery, ev, site)
                    if action is None:
                        continue
                    if action.battery_soc_kwh_end < required_end[i] - EPSILON:
                        continue
                    if action.ev_soc_kwh_end + future[i + 1] < ev.stop_soc_kwh - EPSILON:
                        continue
                    if i == len(slots) - 1 and action.battery_soc_kwh_end < battery.end_min_kwh - EPSILON:
                        continue
                    on_end = action.ev_on_minutes >= slot.duration_min - EPSILON and action.ev_on_minutes > 0
                    off_minutes = slot.duration_min - action.ev_on_minutes if action.ev_on_minutes else (0 if node.on else node.off_minutes) + slot.duration_min
                    off_minutes = 0 if on_end else min(off_minutes, ev.min_off_minutes)
                    # The chosen mode persists into the next step for as long
                    # as the new window has minutes left. Carried-over time
                    # (from the parent) is decremented first; a freshly
                    # different mode starts with the full min_window minus
                    # this slot. Continuing a mature mode never renews its lock.
                    # The final run can continue beyond the bounded horizon.
                    if windowed_dedup:
                        if mode == node.mode:
                            next_remaining = max(0.0, node.mode_remaining_minutes - slot.duration_min)
                        else:
                            next_remaining = max(0.0, float(min_window) - float(slot.duration_min))
                        next_mode = mode
                    else:
                        next_remaining = 0.0
                        next_mode = None
                    new = _Node(
                        action.battery_soc_kwh_end,
                        action.ev_soc_kwh_end,
                        on_end,
                        off_minutes,
                        node.cost + action.slot_cost,
                        node.switches + int(on != node.on),
                        node,
                        action,
                        mode=next_mode,
                        mode_remaining_minutes=next_remaining,
                    )
                    # Exact search does not bucket unequal physical states together.
                    energy_key = (new.battery_kwh, new.ev_kwh) if planner.beam_width is None else (_bucket(new.battery_kwh, planner.soc_bucket_kwh), _bucket(new.ev_kwh, planner.soc_bucket_kwh))
                    key = energy_key + (on_end, off_minutes)
                    if windowed_dedup:
                        key = key + (new.mode, new.mode_remaining_minutes)
                    existing = next_states.get(key)
                    rank = (new.cost - battery.terminal_value_per_kwh * new.battery_kwh, new.switches)
                    if existing is None or rank < (existing.cost - battery.terminal_value_per_kwh * existing.battery_kwh, existing.switches):
                        next_states[key] = new
        if not next_states:
            return _bounded_no_plan(deadline_idx, transitions, max_frontier, "no_complete_transition_at_slot_{}".format(i))
        frontier = list(next_states.values())
        if planner.beam_width is not None and len(frontier) > planner.beam_width:
            frontier = _prune(frontier, planner.beam_width, planner.soc_bucket_kwh, battery.terminal_value_per_kwh, preserve_battery=BATTERY_HOLD not in base_modes, economic_diversity=planner.economic_diversity)
            pruned = True
        max_frontier = max(max_frontier, len(frontier))
    best = min(frontier, key=lambda n: (n.cost - battery.terminal_value_per_kwh * n.battery_kwh, n.switches))
    actions = []
    node = best
    while node.action is not None:
        actions.append(node.action)
        node = node.parent
    actions = tuple(reversed(actions))
    notes = ("beam_pruned" if pruned else "beam_not_pruned", "bucketed_search" if planner.beam_width is not None else "exact_discrete_search", "no_global_optimality_claim")
    diagnostic = Diagnostics(
        pv_curtailed_total_kwh=sum(a.pv_curtailed_kwh for a in actions),
        ev_delivered_total_kwh=sum(a.ev_delivered_kwh for a in actions),
        final_battery_soc_kwh=best.battery_kwh,
        final_ev_soc_kwh=best.ev_kwh,
        ev_stop_target_met=True,
        battery_end_min_met=True,
        ev_on_slot_count=sum(a.ev_action == EV_ON for a in actions),
        mode_counts=tuple((mode, sum(a.battery_mode == mode for a in actions)) for mode in modes),
        notes=notes,
        transitions_evaluated=transitions,
        max_frontier_size=max_frontier,
    )
    return PlanResult(
        STATUS_FEASIBLE,
        best.cost - battery.terminal_value_per_kwh * best.battery_kwh,
        actions,
        (battery.initial_kwh,) + tuple(a.battery_soc_kwh_end for a in actions),
        (ev.start_soc_kwh,) + tuple(a.ev_soc_kwh_end for a in actions),
        tuple(a.grid_import_kwh for a in actions),
        tuple(a.grid_export_kwh for a in actions),
        tuple(a.pv_curtailed_kwh for a in actions),
        diagnostic,
        deadline_idx,
    )
