"""Physical inverter routing foundation, with no production grant installer.

An immutable route selects one gateway per complete physical identity. Discovery
is configuration, never permission to write. A coordinator must eventually
supply independently verified per-inverter gates before this can run in service.
"""

import contextvars
import copy
import functools
import inspect
import json
import re
import threading
import uuid
from dataclasses import dataclass
from types import MappingProxyType

from control_handoff import ControlWriteBlocked, ControlWriteGate, gated_mqtt_publish

_CAPTURE = contextvars.ContextVar("inverter_gateway_capture", default=None)
_PROVIDERS = frozenset({"givenergy", "predbat_gateway"})
_SLOT_KEYS = frozenset(
    "inverter_type inverter_mode soc_percent soc_max battery_power battery_temperature battery_scaling battery_rate_max inverter_limit inverter_time "
    "charge_rate discharge_rate reserve charge_limit charge_limit_enable charge_start_time charge_end_time discharge_start_time discharge_end_time "
    "scheduled_charge_enable scheduled_discharge_enable charge_rate_percent discharge_rate_percent pause_mode pause_start_time pause_end_time discharge_target_soc "
    "grid_power load_power pv_power load_today import_today export_today pv_today export_limit idle_start_time idle_end_time".split()
)
_IGNORED_KEYS = frozenset({"ge_cloud_serial", "ge_cloud_data", "ge_cloud_direct", "battery_temperature_history", "num_inverters", "givtcp_rest"})
_REQUIRED_KEYS = frozenset({"inverter_type", "soc_percent", "soc_max", "battery_power", "charge_rate", "discharge_rate", "reserve", "charge_limit"})


def physical_identity(manufacturer, serial):
    """Only GivEnergy's proven case-insensitive convention is supported today."""
    if manufacturer != "givenergy" or not isinstance(serial, str) or not re.fullmatch(r"[A-Za-z0-9]{6,32}", serial):
        raise ValueError("Unsupported physical inverter identity")
    return manufacturer, serial.upper()


def gateway_id(value):
    """Endpoint identities are stable server-issued UUIDs, never provider labels."""
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


@dataclass(frozen=True)
class GatewayEndpoint:
    """A gateway transport references server-owned credential provenance."""

    endpoint_id: str
    provider: str
    credential_ref_id: str

    def __post_init__(self):
        """Validate identifiers only; ownership still requires the coordinator."""
        if not gateway_id(self.endpoint_id) or not gateway_id(self.credential_ref_id) or self.provider not in _PROVIDERS:
            raise ValueError("Invalid gateway endpoint")


@dataclass(frozen=True)
class InverterRoute:
    """An effective primary is separate from configured secondary gateways."""

    manufacturer: str
    serial: str
    primary: str
    secondary: tuple = ()

    def __post_init__(self):
        """Canonicalise complete identities and reject duplicate gateway roles."""
        manufacturer, serial = physical_identity(self.manufacturer, self.serial)
        if not gateway_id(self.primary) or not isinstance(self.secondary, tuple) or any(not gateway_id(endpoint) for endpoint in self.secondary):
            raise ValueError("Unsupported gateway endpoint")
        if self.primary in self.secondary or len(set(self.secondary)) != len(self.secondary):
            raise ValueError("Duplicate gateway role")
        object.__setattr__(self, "manufacturer", manufacturer)
        object.__setattr__(self, "serial", serial)

    @property
    def identity(self):
        """A manufacturer and full serial identify one physical inverter."""
        return self.manufacturer, self.serial


class InverterGatewayRuntime:
    """Freeze selected slots and fence each physical mutation independently.

    One configured endpoint per provider is currently supported. This object is
    deliberately not installed from apps.yaml or an unsigned config envelope.
    No activation or automatic takeover operation exists here.
    """

    def __init__(self, base, routes, endpoints, gates=None):
        """Accept exact immutable routes and optional externally owned gates."""
        if not isinstance(routes, tuple) or not routes or len(routes) > 16 or any(type(route) is not InverterRoute for route in routes):
            raise ValueError("Invalid inverter routes")
        if len({route.identity for route in routes}) != len(routes):
            raise ValueError("Duplicate physical inverter")
        if not isinstance(endpoints, tuple) or not endpoints or any(type(endpoint) is not GatewayEndpoint for endpoint in endpoints):
            raise ValueError("Invalid gateway inventory")
        if len({endpoint.endpoint_id for endpoint in endpoints}) != len(endpoints) or len({endpoint.provider for endpoint in endpoints}) != len(endpoints):
            raise ValueError("Multiple endpoints for one provider need a component adapter")
        by_endpoint = {endpoint.endpoint_id: endpoint for endpoint in endpoints}
        if {endpoint for route in routes for endpoint in (route.primary,) + route.secondary} != set(by_endpoint):
            raise ValueError("Gateway inventory and routes differ")
        self.endpoints = MappingProxyType(by_endpoint)
        self._by_provider = MappingProxyType({endpoint.provider: endpoint for endpoint in endpoints})
        if base.__dict__.get("control_write_gate") is not None:
            raise ValueError("Instance-wide and physical-inverter control scopes cannot mix")
        if base.args.get("ge_cloud_data") or base.args.get("givtcp_rest"):
            raise ValueError("Uncovered legacy data or REST path")
        self.base = base
        self.routes = tuple(sorted(routes, key=lambda route: route.identity))
        self._by_identity = MappingProxyType({route.identity: route for route in self.routes})
        supplied = dict(gates or {})
        if set(supplied) - set(self._by_identity):
            raise ValueError("Gate outside physical inventory")
        if len({id(gate) for gate in supplied.values()}) != len(supplied):
            raise ValueError("A gate cannot cover multiple physical inverters")
        for identity, gate in supplied.items():
            if type(gate) is not ControlWriteGate or gate.provider != self.primary_provider(self._by_identity[identity]):
                raise ValueError("Gate does not match selected primary")
        self._gates = MappingProxyType(supplied)
        self._contributions = {}
        self._config = None
        self._failed = set()
        self._lock = threading.RLock()

    def primary_provider(self, route):
        """Gateway choice and transport provider remain different identities."""
        return self.endpoints[route.primary].provider

    def scope(self, provider):
        """Return the full configured scope, including retained standby gateways."""
        endpoint = self._by_provider.get(provider)
        return tuple(route.serial for route in self.routes if endpoint is not None and endpoint.endpoint_id in (route.primary,) + route.secondary)

    def fail(self, provider=None):
        """A failed or changed discovery seals every owned gate permanently."""
        with self._lock:
            affected = _PROVIDERS if provider is None else {provider}
            self._failed.update(affected)
            for identity, gate in self._gates.items():
                if self.primary_provider(self._by_identity[identity]) in affected:
                    gate.seal()

    def contribute(self, provider, serials, values):
        """Compose all selected slots once, never use discovery as failover."""
        try:
            if provider not in _PROVIDERS:
                raise ValueError("Unsupported gateway provider")
            keys = tuple(physical_identity("givenergy", serial) for serial in serials)
            if len(set(keys)) != len(keys) or {key[1] for key in keys} != set(self.scope(provider)):
                raise ValueError("Discovery differs from configured full inverter scope")
            if values.get("num_inverters") != len(keys) or not _REQUIRED_KEYS.issubset(values):
                raise ValueError("Incomplete direct inverter mappings")
            if values.get("givtcp_rest") is not None:
                raise ValueError("Uncovered REST mapping")
            for name, value in values.items():
                if name not in _SLOT_KEYS | _IGNORED_KEYS:
                    raise ValueError("Unsupported automatic inverter mapping")
                if name in _SLOT_KEYS and value is not None and (not isinstance(value, list) or len(value) != len(keys)):
                    raise ValueError("Aggregate, PV-only or incomplete slot mapping")
            contribution = {identity: {name: copy.deepcopy(value[index]) if value is not None else None for name, value in values.items() if name in _SLOT_KEYS} for index, identity in enumerate(keys)}
            with self._lock:
                if provider in self._failed:
                    raise ControlWriteBlocked("Inverter discovery is sealed")
                self._contributions[provider] = contribution
                if any(self.primary_provider(route) in self._failed or route.identity not in self._contributions.get(self.primary_provider(route), {}) for route in self.routes):
                    return False
                output = {name: [self._contributions[self.primary_provider(route)][route.identity].get(name) for route in self.routes] for name in _SLOT_KEYS}
                output.update(num_inverters=len(self.routes), givtcp_rest=None)
                if self._config is not None and output != self._config:
                    raise ValueError("Loaded inverter bindings changed, a new boot is required")
                # Atomic dictionary replacement prevents a half-published mapping. Slots
                # never change after this first composition, even on rediscovery.
                self.base.args = {**self.base.args, **output}
                self._config = copy.deepcopy(output)
                return True
        except BaseException:
            self.fail(provider)
            raise

    def selected(self, provider, serial):
        """Let background defaults skip standby, without granting write authority."""
        with self._lock:
            route = self._by_identity.get(physical_identity("givenergy", serial))
            return self._config is not None and provider not in self._failed and route is not None and self.primary_provider(route) == provider

    def serial_for_slot(self, provider, slot):
        """Global optimiser slots are independent of each provider's local order."""
        if type(slot) is not int or not 0 <= slot < len(self.routes):
            return None
        route = self.routes[slot]
        return route.serial if self.primary_provider(route) == provider else None

    def gate_for(self, provider, serial):
        """Admit only the selected full physical identity after complete composition."""
        with self._lock:
            identity = physical_identity("givenergy", serial)
            route = self._by_identity.get(identity)
            if provider in self._failed or self._config is None or route is None or self.primary_provider(route) != provider or identity not in self._gates:
                raise ControlWriteBlocked("Physical inverter controller is unavailable")
            return self._gates[identity]


def runtime_for(component):
    """Ordinary installs have no physical routing runtime."""
    runtime = getattr(component, "base", None).__dict__.get("__inverter_gateway_runtime") if getattr(component, "base", None) is not None else None
    if runtime is not None and type(runtime) is not InverterGatewayRuntime:
        raise ControlWriteBlocked("Invalid inverter gateway runtime")
    return runtime


def capture_arg(component, name, value):
    """Capture only the current component's battery auto-configuration batch."""
    frame = _CAPTURE.get()
    if frame is None or frame[0] is not component:
        return False
    # Charger configuration is independent of inverter selection.
    if name == "num_cars" or name.startswith("car_"):
        return False
    frame[1][name] = copy.deepcopy(value)
    return True


def capture_inverter_config(provider, serials):
    """Run the real provider auto-configurer into a task-local transaction."""

    def decorate(function):
        """Preserve sync/async calling conventions and ordinary behaviour."""

        def begin(component, args, kwargs):
            """Validate direct scope before any auto-config side effect."""
            runtime = runtime_for(component)
            if runtime is None:
                return None
            try:
                discovered = serials(component, *args, **kwargs)
                if {physical_identity("givenergy", serial)[1] for serial in discovered} != set(runtime.scope(provider)):
                    raise ValueError("Gateway discovery scope mismatch")
                return runtime, tuple(discovered), {}, None
            except BaseException:
                runtime.fail(provider)
                raise

        def finish(component, frame):
            """Commit only a complete successful batch."""
            runtime, discovered, values, _token = frame
            runtime.contribute(provider, discovered, values)

        if inspect.iscoroutinefunction(function):

            @functools.wraps(function)
            async def asynchronous(component, *args, **kwargs):
                """Capture one async provider discovery without leaking its context."""
                frame = begin(component, args, kwargs)
                if frame is None:
                    return await function(component, *args, **kwargs)
                token = _CAPTURE.set((component, frame[2]))
                try:
                    result = await function(component, *args, **kwargs)
                    finish(component, frame)
                    return result
                except BaseException:
                    frame[0].fail(provider)
                    raise
                finally:
                    _CAPTURE.reset(token)

            return asynchronous

        @functools.wraps(function)
        def synchronous(component, *args, **kwargs):
            """Capture a synchronous provider discovery independently of threads."""
            frame = begin(component, args, kwargs)
            if frame is None:
                return function(component, *args, **kwargs)
            token = _CAPTURE.set((component, frame[2]))
            try:
                result = function(component, *args, **kwargs)
                finish(component, frame)
                return result
            except BaseException:
                frame[0].fail(provider)
                raise
            finally:
                _CAPTURE.reset(token)

        return synchronous

    return decorate


def cloud_direct_scope(component, devices):
    """Reject aggregate EMS/vendor Gateway actuation and extra PV sources."""
    batteries, count = component.logical_inverters(devices)
    if devices.get("ems") or devices.get("pv") or batteries != devices.get("battery") or count != len(batteries):
        raise ValueError("Aggregate or PV-only Cloud scope is not supported")
    return tuple(batteries)


def hub_direct_scope(component):
    """Only current direct battery units qualify, never cached discovery."""
    status = component._last_status
    if status is None or component.gateway_display_only or component.gateway_integrate_power:
        raise ValueError("Uncovered Hub configuration")
    from gateway_status_pb2 import INVERTER_TYPE_GIVENERGY

    # Coordinators and PV-only units have
    # broader/different actuation or measurement scope and need an adapter.
    units = tuple(status.inverters)
    if not units or any(unit.type != INVERTER_TYPE_GIVENERGY or unit.battery.ByteSize() == 0 for unit in units) or (len(units) > 1 and component.gateway_shared_ct):
        raise ValueError("Aggregate or PV-only Hub scope is not supported")
    serials = tuple(sorted(unit.serial for unit in units))
    if len({serial[-6:].lower() for serial in serials}) != len(serials):
        raise ValueError("Hub entity suffix collision")
    return serials


def route_cloud_mutation(endpoint, definitive_result, definitive_error):
    """Fence actual per-serial HTTP attempts, including retries, independently."""

    def decorate(function):
        """Bind serial and post from the real transport signature."""
        signature = inspect.signature(function)

        @functools.wraps(function)
        async def guarded(component, *args, **kwargs):
            """Never let secondary writes reach the provider transport."""
            runtime = runtime_for(component)
            if runtime is None:
                return await function(component, *args, **kwargs)
            bound = signature.bind(component, *args, **kwargs)
            bound.apply_defaults()
            values = bound.arguments
            if values["endpoint"] != endpoint or not values["post"]:
                return await function(component, *args, **kwargs)
            gate = runtime.gate_for("givenergy", values["serial"])
            token = gate.admit("givenergy")
            definitive = False
            try:
                result = await function(component, *args, **kwargs)
                definitive = definitive_result(result) is True
                return result
            except Exception as error:
                definitive = definitive_error(error) is True
                raise
            finally:
                gate.complete(token, definitive)

        return guarded

    return decorate


async def publish_routed_hub(component, topic, payload, retain):
    """Guard the actual MQTT sink; executable schedules require a scope adapter."""
    runtime = runtime_for(component)
    if runtime is None or topic not in {component.topic_command, component.topic_schedule}:
        return False
    if topic == component.topic_schedule or retain:
        raise ControlWriteBlocked("Executable Hub schedules lack per-inverter scope")
    try:
        command = json.loads(payload)
        allowed = {"set_charge_rate", "set_discharge_rate", "set_reserve", "set_target_soc", "set_charge_enable", "set_discharge_enable", "set_rate_cap", "set_charge_slot", "set_discharge_slot"}
        if not isinstance(command, dict) or command.get("command") not in allowed or not isinstance(command.get("serial"), str):
            raise ValueError("Full inverter serial required")
        gate = runtime.gate_for("predbat_gateway", command["serial"])
    except (ValueError, TypeError, UnicodeError) as error:
        raise ControlWriteBlocked("Unscoped Hub battery command") from error
    await gated_mqtt_publish(gate, "predbat_gateway", lambda: component._mqtt_client.publish(topic, payload, qos=1, retain=False), component._loop)
    return True
