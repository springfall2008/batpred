# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from datetime import datetime, timezone

from ha import HAHistory
from web import WebInterface
from tests.test_hahistory import MockComponents


def run_web_attributes_history_tests(my_predbat):
    """get_attributes_html(from_db=True) reads an entity's attributes from history without tracking it."""
    failed = 0
    print("**** Running web attributes history tests ****")

    entity_id = "switch.predbat_web_attributes_test"
    ha = my_predbat.ha_interface
    saved = (my_predbat.components, ha.history, ha.history_enable)
    try:
        ha_history = HAHistory(my_predbat)
        ha_history.initialize()
        components = MockComponents()
        components.register_component("ha", ha)
        components.register_component("ha_history", ha_history)
        my_predbat.components = components
        ha.history = [{"state": "on", "last_updated": datetime.now(timezone.utc).isoformat(), "attributes": {"tariff_note": "from-history"}}]
        ha.history_enable = True

        web = WebInterface(my_predbat, web_port=5052)

        # ---------------------------------------------------------------------
        print("Test: attributes for an entity Predbat doesn't publish are read from history")
        html = web.get_attributes_html(entity_id, from_db=True)
        if "from-history" not in html:
            print(f"  ERROR: expected the history record's attributes in the HTML, got '{html}'")
            failed += 1

        # ---------------------------------------------------------------------
        # A tracked entity is re-read by HAHistory.run() every 2 minutes for the life of the
        # process; on a database-backed install that is a full one-day read each time. A page
        # listing every entity would leave all of them polled until restart.
        print("Test: reading attributes from history does not register the entity for history tracking")
        if entity_id in ha_history.history_entities:
            print(f"  ERROR: {entity_id} was added to HAHistory.history_entities ({ha_history.history_entities[entity_id]} days), so it will be re-read every 2 minutes")
            failed += 1
    finally:
        my_predbat.components, ha.history, ha.history_enable = saved

    return failed
