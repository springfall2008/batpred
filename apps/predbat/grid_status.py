# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Grid outage (power cut) detection
# -----------------------------------------------------------------------------

"""Grid outage (power cut) detection.

Systems with a backup gateway (for example a SigenStor with a Sigen Gateway) keep the house running
from the battery when the grid fails. Predbat does not know the grid has gone, so it carries on
executing its plan: a charge window, a freeze or a reserve hold (a discharge rate of 0) can leave the
battery unable to supply the house for as long as the outage lasts.

When the optional ``grid_status`` entity reports the grid is down, Predbat:

- resets the inverter to safe mode once (maximum charge and discharge rate, reserve 0, no charge or
  export window), the same reset that turning on Read-Only mode performs, so the battery can carry
  the house;
- then stays in read-only mode until the grid has been back for ``grid_restore_delay`` minutes, so a
  flickering supply does not hand control back and forth.

An entity that is missing, unknown or unavailable is no evidence either way, so the current state is
kept: a sensor dropping out never starts or ends an outage.
"""

GRID_STATUS_ON_GRID_DEFAULT = ["on_grid", "on grid", "ongrid", "on", "true", "connected", "1"]
GRID_STATUS_NO_EVIDENCE = ["unknown", "unavailable", "none", ""]
GRID_RESTORE_DELAY_DEFAULT = 5


def grid_status_on_values(base):
    """Return the lower-cased entity states that mean the grid is connected.

    ``grid_status_on_grid`` in apps.yaml may be a single string or a list; anything else, or an empty
    value, falls back to GRID_STATUS_ON_GRID_DEFAULT.
    """
    values = base.get_arg("grid_status_on_grid", default=None, indirect=False)
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple)):
        values = []
    values = [str(value).strip().lower() for value in values if value is not None and str(value).strip()]
    return values if values else list(GRID_STATUS_ON_GRID_DEFAULT)


def grid_status_reading(base):
    """Read the grid_status entity.

    Returns:
        True when the grid is connected, False when it is not, or None when there is no evidence:
        grid_status is not configured, is a literal rather than an entity, or the entity is missing,
        unknown or unavailable.
    """
    entity_id = base.get_arg("grid_status", default=None, indirect=False)
    if not entity_id or not isinstance(entity_id, str) or "." not in entity_id:
        return None

    state = base.get_state_wrapper(entity_id=entity_id, default=None)
    if state is None:
        return None
    state = str(state).strip().lower()
    if state in GRID_STATUS_NO_EVIDENCE:
        return None
    return state in grid_status_on_values(base)


def grid_restore_delay_minutes(base):
    """Return how many minutes the grid must be back before Predbat takes control again (never negative)."""
    delay = base.get_arg("grid_restore_delay", GRID_RESTORE_DELAY_DEFAULT)
    try:
        delay = float(delay)
    except (TypeError, ValueError):
        delay = GRID_RESTORE_DELAY_DEFAULT
    return max(delay, 0)


def update_grid_outage(base, now):
    """Update the grid outage state from the grid_status entity.

    Called once per update from Fetch.fetch_config_options(). On the change into an outage, and on the
    change out of one, an inverter reset is requested; going in, it is forced so it still runs although
    Predbat is about to be read-only (see Execute.reset_inverter()).

    Args:
        base: the PredBat instance; grid_outage_active and grid_restore_seen are kept on it.
        now: the current time (timezone aware), used to time the restore delay.

    Returns:
        bool: True while Predbat should treat the grid as down.
    """
    reading = grid_status_reading(base)

    if reading is False:
        # Grid down: a restore that was being timed did not hold
        base.grid_restore_seen = None
        if not base.grid_outage_active:
            base.log("Warn: Grid outage detected by {}, Predbat will reset the inverter to safe mode and stay in read-only mode until the grid is back".format(base.get_arg("grid_status", indirect=False)))
            base.grid_outage_active = True
            base.inverter_needs_reset = True
            base.inverter_needs_reset_force = "set_read_only"
    elif reading is True and base.grid_outage_active:
        if base.grid_restore_seen is None:
            base.grid_restore_seen = now
        delay = grid_restore_delay_minutes(base)
        elapsed = (now - base.grid_restore_seen).total_seconds() / 60.0
        if elapsed >= delay:
            base.log("Grid restored for {:.0f} minutes, Predbat is taking back control of the inverter".format(elapsed))
            base.grid_outage_active = False
            base.grid_restore_seen = None
            base.inverter_needs_reset = True
        else:
            base.log("Grid restored, Predbat will take back control after {:.0f} more minutes if it stays up".format(delay - elapsed))
    elif reading is True:
        base.grid_restore_seen = None

    return base.grid_outage_active


def grid_status_poll(base):
    """Ask for an update as soon as the grid goes down, rather than at the next 5 minute run.

    Called from the 15 second loop. Only the start of an outage is urgent - the battery may be held
    while the house runs from it - so only that sets update_pending; the restore is handled by the
    normal run once grid_restore_delay has passed. Does nothing while the user has Read-Only mode on.

    Returns:
        bool: True when it set update_pending.
    """
    if base.get_arg("set_read_only", False):
        return False
    if grid_status_reading(base) is False and not base.grid_outage_active:
        base.log("Grid outage detected, will update now")
        base.update_pending = True
        return True
    return False
