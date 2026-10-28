# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init


class _FakeHA:
    """Stands in for get_state_wrapper / get_history_wrapper and records the calls made."""

    def __init__(self, states=None, history=None):
        self.states = states or {}
        self.history = history or {}
        self.state_calls = []
        self.history_calls = []

    def get_state_wrapper(self, entity_id=None, default=None, attribute=None, refresh=False, required_unit=None, raw=False):
        self.state_calls.append((entity_id, attribute))
        return self.states.get((entity_id, attribute), default)

    def get_history_wrapper(self, entity_id, days=30, required=True, tracked=True):
        self.history_calls.append(entity_id)
        return self.history.get(entity_id)


def _install(my_predbat, fake, provider):
    saved = {name: my_predbat.__dict__.get(name, None) for name in ("get_state_wrapper", "get_history_wrapper", "previous_value_provider")}
    my_predbat.get_state_wrapper = fake.get_state_wrapper
    my_predbat.get_history_wrapper = fake.get_history_wrapper
    my_predbat.previous_value_provider = provider
    return saved


def _restore(my_predbat, saved):
    for name, value in saved.items():
        if value is None:
            my_predbat.__dict__.pop(name, None)
        else:
            setattr(my_predbat, name, value)


def _check(label, got, expected):
    if got != expected:
        print("ERROR: {} - expected {!r}, got {!r}".format(label, expected, got))
        return True
    return False


def _test_default_unchanged(my_predbat):
    """No provider: state, attribute, unavailable-to-history fallback all read HA exactly as before."""
    failed = False
    entity = "predbat.savings_total_predbat"
    fake = _FakeHA(
        states={(entity, None): "12.5", (entity, "start_date"): "2026-01-01", ("predbat.carbon_total", None): "unavailable"},
        history={"predbat.carbon_total": [[{"state": "1.0", "attributes": {}}, {"state": "7.25", "attributes": {"x": 3}}]]},
    )
    saved = _install(my_predbat, fake, None)
    try:
        failed |= _check("default state", my_predbat.load_previous_value_from_ha(entity), "12.5")
        failed |= _check("default attribute", my_predbat.load_previous_value_from_ha(entity, attribute="start_date"), "2026-01-01")
        failed |= _check("default history fallback", my_predbat.load_previous_value_from_ha("predbat.carbon_total"), "7.25")
        failed |= _check("default history attribute", my_predbat.load_previous_value_from_ha("predbat.carbon_total", attribute="x"), 3)
        failed |= _check("default missing", my_predbat.load_previous_value_from_ha("predbat.nothing"), None)
    finally:
        _restore(my_predbat, saved)
    return failed


def _test_provider_state_and_attribute(my_predbat):
    """Provider values win for both a state and an attribute, and HA is not consulted."""
    failed = False
    entity = "predbat.control_conflicts"
    asked = []

    def provider(entity_id, attribute):
        asked.append((entity_id, attribute))
        if attribute == "events":
            return [{"t": 1}]
        if attribute is None:
            return "42"
        return None

    fake = _FakeHA(states={(entity, None): "ha-state", (entity, "events"): ["ha-events"]})
    saved = _install(my_predbat, fake, provider)
    try:
        failed |= _check("provider state", my_predbat.load_previous_value_from_ha(entity), "42")
        failed |= _check("provider attribute", my_predbat.load_previous_value_from_ha(entity, attribute="events"), [{"t": 1}])
        failed |= _check("provider asked with entity/attribute", asked, [(entity, None), (entity, "events")])
        failed |= _check("HA state not read", fake.state_calls, [])
        failed |= _check("HA history not read", fake.history_calls, [])
    finally:
        _restore(my_predbat, saved)
    return failed


def _test_provider_none_falls_back(my_predbat):
    """Provider returning None falls back to the existing HA state and history reads."""
    failed = False
    entity = "predbat.cost_total_car"
    fake = _FakeHA(
        states={(entity, None): "3.5", (entity, "last_updated"): "2026-10-01"},
        history={"predbat.inverter_register_writes": [[{"state": "99", "attributes": {}}]]},
    )
    saved = _install(my_predbat, fake, lambda entity_id, attribute: None)
    try:
        failed |= _check("fallback state", my_predbat.load_previous_value_from_ha(entity), "3.5")
        failed |= _check("fallback attribute", my_predbat.load_previous_value_from_ha(entity, attribute="last_updated"), "2026-10-01")
        failed |= _check("fallback history", my_predbat.load_previous_value_from_ha("predbat.inverter_register_writes"), "99")
        failed |= _check("fallback HA state reads", fake.state_calls, [(entity, None), (entity, "last_updated"), ("predbat.inverter_register_writes", None)])
    finally:
        _restore(my_predbat, saved)
    return failed


def test_previous_value_provider(my_predbat):
    """
    Test the optional previous_value_provider hook on load_previous_value_from_ha
    """
    print("**** Testing previous_value_provider ****")
    failed = False
    failed |= _test_default_unchanged(my_predbat)
    failed |= _test_provider_state_and_attribute(my_predbat)
    failed |= _test_provider_none_falls_back(my_predbat)
    failed |= _check("provider cleared after tests", my_predbat.previous_value_provider, None)
    return failed
