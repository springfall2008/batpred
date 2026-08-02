# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Shared mock base object for standalone command-line runs of components.

Most component modules (fox, solis, octopus, teslemetry, ...) can be executed directly
from the command line to exercise their API against a live vendor endpoint. Those
harnesses need an object to pass as the ``base`` argument of ComponentBase. MockBase
provides the minimal PredBat base surface that ComponentBase and the components read:
a clock, an in-memory entity store, argument accessors and a logger.

It is deliberately concrete rather than abstract - modules instantiate it directly, and
the few needing extra state subclass it. ``components`` is always None, so
``ComponentBase.storage`` resolves to None and the disk cache is skipped for a
standalone run.
"""

from datetime import datetime
import json


class MockBase:
    """Minimal stand-in for the PredBat base object, used by the standalone CLI harnesses."""

    def __init__(self, config_root="./temp_predbat", local_tz=None, **kwargs):
        """Initialise the mock with a clock, empty entity/arg stores and the full base attribute superset.

        Surplus keyword arguments are stored into self.args, with None values skipped so an
        unset optional argument stays absent rather than shadowing a caller's default.
        """
        self.local_tz = local_tz if local_tz is not None else datetime.now().astimezone().tzinfo
        self.now_utc = datetime.now(self.local_tz)
        self.now_utc_exact = self.now_utc
        self.midnight_utc = self.now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        self.minutes_now = self.now_utc.hour * 60 + self.now_utc.minute
        self.prefix = "predbat"
        self.entities = {}
        self.config_root = config_root
        self.plan_interval_minutes = 30
        self.fatal_error = False
        self.had_errors = False
        self.components = None
        self.num_cars = 0
        self.currency_symbols = "£p"
        self.arg_errors = {}
        self.args = {key: value for key, value in kwargs.items() if value is not None}

    def log(self, message, quiet=True):
        """Print a timestamped log line.

        Accepts the real Hass.log's quiet keyword for signature compatibility with callers
        that pass it; the mock always prints regardless of its value.
        """
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")

    def get_state_wrapper(self, entity_id=None, default=None, attribute=None, refresh=False, required_unit=None, raw=False):
        """Return a stored entity state, one of its attributes, or the whole record when raw is set."""
        entity = self.entities.get(entity_id, {})
        if raw:
            return entity
        if attribute is not None:
            return entity.get("attributes", {}).get(attribute, default)
        return entity.get("state", default)

    def set_state_wrapper(self, entity_id, state, attributes=None, app=None, required_unit=None):
        """Store an entity's state and attributes in memory.

        Accepts both app and required_unit because the component modules disagree on which
        one they pass, and ComponentBase.set_state_wrapper forwards required_unit.
        """
        self.entities[entity_id] = {"state": state, "attributes": attributes or {}}

    def dashboard_item(self, entity_id, state=None, attributes=None, app=None):
        """Print a published entity and store it.

        The options list is elided in a copy of the attributes, so the caller's dict - which
        is then stored verbatim - is never mutated.
        """
        print(f"ENTITY: {entity_id} = {state}")
        if attributes:
            print_attrs = dict(attributes)
            if "options" in print_attrs:
                print_attrs["options"] = "..."
            print(f"  Attributes: {json.dumps(print_attrs, indent=2, default=str)}")
        self.set_state_wrapper(entity_id, state, attributes)

    def get_arg(self, arg, default=None, indirect=True, combine=False, attribute=None, index=None, domain=None, can_override=True, required_unit=None):
        """Return a configured argument, falling back to the caller's default."""
        return self.args.get(arg, default)

    def set_arg(self, key, value):
        """Record an argument set by automatic_config, printing it with any referenced entity's state.

        Matches userinterface.py's Fetch.set_arg: a None value deletes the key rather than
        storing it, so a later get_arg(key, default) falls back to the caller's default
        instead of returning None.
        """
        if value is None:
            self.args.pop(key, None)
        else:
            self.args[key] = value
        if isinstance(value, str) and "." in value:
            state = self.get_state_wrapper(value, default=None)
        elif isinstance(value, list):
            state = "n/a []"
            for item in value:
                if isinstance(item, str) and "." in item:
                    state = self.get_state_wrapper(item, default=None)
                    break
        else:
            state = "n/a"
        print(f"Set arg {key} = {value} (state={state})")

    def get_ha_config(self, name, default):
        """Return the caller's default - a standalone run has no Home Assistant config."""
        return default

    def get_history_wrapper(self, entity_id, days=30, required=True, tracked=True):
        """Return None - a standalone run has no Home Assistant recorder, matching PredBat's no-interface path."""
        return None

    def call_notify(self, message):
        """Print a notification message."""
        print(f"NOTIFY: {message}")

    def record_status(self, message, debug="", had_errors=False, notify=False, extra=""):
        """Print a status record and track the error flag."""
        print(f"STATUS: {message}" + (f" ({debug})" if debug else ""))
        if had_errors:
            self.had_errors = True
