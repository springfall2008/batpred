# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Tests for adding and deleting apps.yaml list items and settings from the structured /apps
editor (issue #4714 - compare profiles could only be edited, never added or removed).
Follows the FakeRequest pattern of test_web_debug_history_routes.py.
"""

import asyncio
import json
import os
import shutil
import tempfile

from ruamel.yaml import YAML

from web import WebInterface
from web_helper import get_apps_js

APPS_YAML_FIXTURE = """pred_bat:
  # Tariffs to compare against the current one
  compare_list:
    - name: Tariff One
      id: one
    - name: Tariff Two
      id: two
    - name: Tariff Three
      id: three
  battery_charge_low:
    normal: 10
  nested_matrix:
    - - 1
      - 2
    - - 3
      - 4
"""


class FakeRequest:
    """A minimal aiohttp-request stand-in exposing only the POST data the handler reads."""

    def __init__(self, postdata):
        """Store the POST data this request will hand back."""
        self.postdata = postdata

    async def post(self):
        """Return the stored POST data, as aiohttp does."""
        return self.postdata


def _load_yaml(path="apps.yaml"):
    """Load a YAML file from the temporary working directory."""
    yaml = YAML()
    yaml.preserve_quotes = True
    with open(path, "r") as handle:
        return yaml.load(handle)


def _reset_fixture(my_predbat):
    """Write a fresh apps.yaml fixture and return a WebInterface bound to a matching args copy."""
    with open("apps.yaml", "w") as handle:
        handle.write(APPS_YAML_FIXTURE)

    web_interface = WebInterface.__new__(WebInterface)
    web_interface.base = my_predbat
    web_interface.log = my_predbat.log
    web_interface.prefix = my_predbat.prefix
    # The live args are a separate copy of the same structure, as they are in Predbat itself
    web_interface.args = _load_yaml()["pred_bat"]
    return web_interface


def _post_changes(web_interface, changes):
    """Post a set of changes to html_apps_post and return the decoded JSON response."""
    request = FakeRequest({"changes": json.dumps(changes)})
    response = asyncio.run(web_interface.html_apps_post(request))
    return json.loads(response.text)


def _delete(path):
    """Build the change entry the browser sends when a row is marked for deletion."""
    return {"rowId": 1001, "originalValue": "", "newValue": "", "type": "delete", "isNested": True, "path": path}


def _add(path, value):
    """Build the change entry the browser sends when a new entry or setting is added."""
    return {"rowId": None, "originalValue": "", "newValue": value, "type": "add", "isNested": True, "path": path}


def _names(compare_list):
    """Return the name of every entry in a compare_list."""
    return [entry.get("name", "") for entry in compare_list]


def run_web_apps_edit_tests(my_predbat):
    """Unit tests for the delete and add change types of the structured apps.yaml editor."""
    failed = 0
    print("**** Running apps.yaml editor add/delete tests ****")

    original_dir = os.getcwd()
    temp_dir = tempfile.mkdtemp(prefix="predbat_test_apps_edit_")
    try:
        os.chdir(temp_dir)

        # ---------------------------------------------------------------------
        print("Test: deleting one compare profile removes just that profile")
        web_interface = _reset_fixture(my_predbat)
        result = _post_changes(web_interface, {"compare_list[1]": _delete("compare_list[1]")})
        if not result.get("success"):
            print("  ERROR: expected the delete to succeed, got: {}".format(result))
            failed += 1
        saved = _load_yaml()
        if _names(saved["pred_bat"]["compare_list"]) != ["Tariff One", "Tariff Three"]:
            print("  ERROR: expected One and Three left in apps.yaml, got: {}".format(_names(saved["pred_bat"]["compare_list"])))
            failed += 1
        if _names(web_interface.args["compare_list"]) != ["Tariff One", "Tariff Three"]:
            print("  ERROR: expected the live args to match the file, got: {}".format(_names(web_interface.args["compare_list"])))
            failed += 1
        with open("apps.yaml", "r") as handle:
            raw = handle.read()
        if "# Tariffs to compare against the current one" not in raw:
            print("  ERROR: expected the apps.yaml comment to survive the round trip, got:\n{}".format(raw))
            failed += 1

        # ---------------------------------------------------------------------
        print("Test: deleting two profiles at once is not confused by the shifting indices")
        web_interface = _reset_fixture(my_predbat)
        result = _post_changes(web_interface, {"compare_list[0]": _delete("compare_list[0]"), "compare_list[2]": _delete("compare_list[2]")})
        if not result.get("success"):
            print("  ERROR: expected the two deletes to succeed, got: {}".format(result))
            failed += 1
        saved = _load_yaml()
        if _names(saved["pred_bat"]["compare_list"]) != ["Tariff Two"]:
            print("  ERROR: expected only Tariff Two left, got: {}".format(_names(saved["pred_bat"]["compare_list"])))
            failed += 1

        # ---------------------------------------------------------------------
        print("Test: delete paths sort deepest and highest index first")
        ordered = sorted(["compare_list[2]", "compare_list[10]", "compare_list[2].name"], key=web_interface._yaml_path_sort_key, reverse=True)
        if ordered != ["compare_list[10]", "compare_list[2].name", "compare_list[2]"]:
            print("  ERROR: unexpected delete ordering: {}".format(ordered))
            failed += 1

        # ---------------------------------------------------------------------
        print("Test: a directly nested list tokenizes every bracket pair in a path component")
        if web_interface._split_yaml_path("foo[0][1]") != ["foo", "[0]", "[1]"]:
            print("  ERROR: expected foo[0][1] to split into foo, [0], [1], got: {}".format(web_interface._split_yaml_path("foo[0][1]")))
            failed += 1
        if web_interface._split_yaml_path("foo[0][]") != ["foo", "[0]", "[]"]:
            print("  ERROR: expected foo[0][] to split into foo, [0], [], got: {}".format(web_interface._split_yaml_path("foo[0][]")))
            failed += 1

        # ---------------------------------------------------------------------
        print("Test: a directly nested list item can be deleted")
        web_interface = _reset_fixture(my_predbat)
        result = _post_changes(web_interface, {"nested_matrix[0][1]": _delete("nested_matrix[0][1]")})
        if not result.get("success"):
            print("  ERROR: expected the nested-list delete to succeed, got: {}".format(result))
            failed += 1
        saved = _load_yaml()
        if list(saved["pred_bat"]["nested_matrix"][0]) != [1]:
            print("  ERROR: expected nested_matrix[0] to be [1], got: {}".format(list(saved["pred_bat"]["nested_matrix"][0])))
            failed += 1

        # ---------------------------------------------------------------------
        print("Test: a directly nested list can have an item appended")
        web_interface = _reset_fixture(my_predbat)
        result = _post_changes(web_interface, {"nested_matrix[1][]#1": _add("nested_matrix[1][]", "5")})
        if not result.get("success"):
            print("  ERROR: expected the nested-list add to succeed, got: {}".format(result))
            failed += 1
        saved = _load_yaml()
        if list(saved["pred_bat"]["nested_matrix"][1]) != [3, 4, 5]:
            print("  ERROR: expected nested_matrix[1] to be [3, 4, 5], got: {}".format(list(saved["pred_bat"]["nested_matrix"][1])))
            failed += 1

        # ---------------------------------------------------------------------
        print("Test: two new compare profiles can be added in one save")
        web_interface = _reset_fixture(my_predbat)
        changes = {
            "compare_list[]#1": _add("compare_list[]", "name: Tariff Four\nid: four\nrates_import_octopus_url: https://example.com/four\n"),
            "compare_list[]#2": _add("compare_list[]", "name: Tariff Five\nid: five\n"),
        }
        result = _post_changes(web_interface, changes)
        if not result.get("success"):
            print("  ERROR: expected the adds to succeed, got: {}".format(result))
            failed += 1
        saved = _load_yaml()
        if _names(saved["pred_bat"]["compare_list"]) != ["Tariff One", "Tariff Two", "Tariff Three", "Tariff Four", "Tariff Five"]:
            print("  ERROR: unexpected profiles after adding, got: {}".format(_names(saved["pred_bat"]["compare_list"])))
            failed += 1
        added = saved["pred_bat"]["compare_list"][3]
        if added.get("id") != "four" or added.get("rates_import_octopus_url") != "https://example.com/four":
            print("  ERROR: the added profile did not keep its settings, got: {}".format(dict(added)))
            failed += 1
        if _names(web_interface.args["compare_list"]) != ["Tariff One", "Tariff Two", "Tariff Three", "Tariff Four", "Tariff Five"]:
            print("  ERROR: expected the live args to match the file, got: {}".format(_names(web_interface.args["compare_list"])))
            failed += 1

        # ---------------------------------------------------------------------
        print("Test: a new setting can be added to an existing profile, keeping its type")
        web_interface = _reset_fixture(my_predbat)
        changes = {
            "compare_list[0].rates_import_octopus_url#1": _add("compare_list[0].rates_import_octopus_url", "https://example.com/one"),
            "battery_charge_low.low#2": _add("battery_charge_low.low", "5"),
        }
        result = _post_changes(web_interface, changes)
        if not result.get("success"):
            print("  ERROR: expected the new settings to be added, got: {}".format(result))
            failed += 1
        saved = _load_yaml()
        if saved["pred_bat"]["compare_list"][0].get("rates_import_octopus_url") != "https://example.com/one":
            print("  ERROR: the new profile setting was not saved, got: {}".format(dict(saved["pred_bat"]["compare_list"][0])))
            failed += 1
        if saved["pred_bat"]["battery_charge_low"].get("low") != 5:
            print("  ERROR: expected a numeric 5, got: {!r}".format(saved["pred_bat"]["battery_charge_low"].get("low")))
            failed += 1

        # ---------------------------------------------------------------------
        print("Test: an edit and a delete in the same save both land on the right entries")
        web_interface = _reset_fixture(my_predbat)
        changes = {
            "compare_list[2].name": {"rowId": 1002, "originalValue": "Tariff Three", "newValue": "Renamed", "type": "string", "isNested": True, "path": "compare_list[2].name"},
            "compare_list[0]": _delete("compare_list[0]"),
        }
        result = _post_changes(web_interface, changes)
        if not result.get("success"):
            print("  ERROR: expected the edit and delete to succeed, got: {}".format(result))
            failed += 1
        saved = _load_yaml()
        if _names(saved["pred_bat"]["compare_list"]) != ["Tariff Two", "Renamed"]:
            print("  ERROR: expected Tariff Two and Renamed, got: {}".format(_names(saved["pred_bat"]["compare_list"])))
            failed += 1

        # ---------------------------------------------------------------------
        print("Test: editing a profile and deleting it in the same save is not an error")
        web_interface = _reset_fixture(my_predbat)
        changes = {
            "compare_list[0].name": {"rowId": 1002, "originalValue": "Tariff One", "newValue": "Renamed", "type": "string", "isNested": True, "path": "compare_list[0].name"},
            "compare_list[0]#delete": _delete("compare_list[0]"),
        }
        result = _post_changes(web_interface, changes)
        if not result.get("success"):
            print("  ERROR: expected an edit of a deleted profile to be harmless, got: {}".format(result))
            failed += 1
        saved = _load_yaml()
        if _names(saved["pred_bat"]["compare_list"]) != ["Tariff Two", "Tariff Three"]:
            print("  ERROR: expected Tariff One to be gone, got: {}".format(_names(saved["pred_bat"]["compare_list"])))
            failed += 1

        # ---------------------------------------------------------------------
        print("Test: adding a setting that already exists is refused and apps.yaml is left alone")
        web_interface = _reset_fixture(my_predbat)
        result = _post_changes(web_interface, {"compare_list[0].name#1": _add("compare_list[0].name", "Clash")})
        if result.get("success"):
            print("  ERROR: expected the duplicate setting to be refused, got: {}".format(result))
            failed += 1
        saved = _load_yaml()
        if _names(saved["pred_bat"]["compare_list"]) != ["Tariff One", "Tariff Two", "Tariff Three"]:
            print("  ERROR: expected apps.yaml to be unchanged, got: {}".format(_names(saved["pred_bat"]["compare_list"])))
            failed += 1

        # ---------------------------------------------------------------------
        print("Test: deleting an entry that is not there is refused")
        web_interface = _reset_fixture(my_predbat)
        result = _post_changes(web_interface, {"compare_list[9]": _delete("compare_list[9]")})
        if result.get("success"):
            print("  ERROR: expected the out of range delete to be refused, got: {}".format(result))
            failed += 1

        # ---------------------------------------------------------------------
        print("Test: appending to something that is not a list is refused")
        web_interface = _reset_fixture(my_predbat)
        result = _post_changes(web_interface, {"battery_charge_low[]#1": _add("battery_charge_low[]", "10")})
        if result.get("success"):
            print("  ERROR: expected appending to a dictionary to be refused, got: {}".format(result))
            failed += 1

        # ---------------------------------------------------------------------
        print("Test: a top-level argument cannot be added or deleted")
        web_interface = _reset_fixture(my_predbat)
        top_level_delete = {"rowId": 1, "originalValue": "", "newValue": "", "type": "delete", "isNested": False}
        result = _post_changes(web_interface, {"compare_list": top_level_delete})
        if result.get("success"):
            print("  ERROR: expected a top-level delete to be refused, got: {}".format(result))
            failed += 1
        saved = _load_yaml()
        if "compare_list" not in saved["pred_bat"]:
            print("  ERROR: the top-level compare_list was removed from apps.yaml")
            failed += 1

        # ---------------------------------------------------------------------
        print("Test: isNested cannot be spoofed true to delete a top-level argument")
        web_interface = _reset_fixture(my_predbat)
        spoofed_delete = {"rowId": 1, "originalValue": "", "newValue": "", "type": "delete", "isNested": True, "path": "compare_list"}
        result = _post_changes(web_interface, {"compare_list": spoofed_delete})
        if result.get("success"):
            print("  ERROR: expected the spoofed top-level delete to be refused, got: {}".format(result))
            failed += 1
        saved = _load_yaml()
        if "compare_list" not in saved["pred_bat"]:
            print("  ERROR: the top-level compare_list was removed from apps.yaml by a spoofed isNested flag")
            failed += 1

        # ---------------------------------------------------------------------
        print("Test: an empty value is refused rather than written as null")
        web_interface = _reset_fixture(my_predbat)
        result = _post_changes(web_interface, {"compare_list[]#1": _add("compare_list[]", "   ")})
        if result.get("success"):
            print("  ERROR: expected an empty added value to be refused, got: {}".format(result))
            failed += 1

        # ---------------------------------------------------------------------
        print("Test: adding a compare profile without an id is refused")
        web_interface = _reset_fixture(my_predbat)
        result = _post_changes(web_interface, {"compare_list[]#1": _add("compare_list[]", "name: No Id\n")})
        if result.get("success"):
            print("  ERROR: expected the id-less profile to be refused, got: {}".format(result))
            failed += 1
        saved = _load_yaml()
        if _names(saved["pred_bat"]["compare_list"]) != ["Tariff One", "Tariff Two", "Tariff Three"]:
            print("  ERROR: expected apps.yaml to be unchanged, got: {}".format(_names(saved["pred_bat"]["compare_list"])))
            failed += 1
        if _names(web_interface.args["compare_list"]) != ["Tariff One", "Tariff Two", "Tariff Three"]:
            print("  ERROR: expected the live args to be unchanged too, got: {}".format(_names(web_interface.args["compare_list"])))
            failed += 1

        # ---------------------------------------------------------------------
        print("Test: adding a compare profile with a duplicate id is refused")
        web_interface = _reset_fixture(my_predbat)
        result = _post_changes(web_interface, {"compare_list[]#1": _add("compare_list[]", "name: Clash\nid: one\n")})
        if result.get("success"):
            print("  ERROR: expected the duplicate id to be refused, got: {}".format(result))
            failed += 1

        # ---------------------------------------------------------------------
        print("Test: deleting a compare profile's id is refused, since results are indexed by it")
        web_interface = _reset_fixture(my_predbat)
        result = _post_changes(web_interface, {"compare_list[0].id": _delete("compare_list[0].id")})
        if result.get("success"):
            print("  ERROR: expected removing a profile's id to be refused, got: {}".format(result))
            failed += 1
        saved = _load_yaml()
        if "id" not in saved["pred_bat"]["compare_list"][0]:
            print("  ERROR: apps.yaml was written with an id-less compare profile")
            failed += 1

        # ---------------------------------------------------------------------
        print("Test: a batch that fails partway leaves apps.yaml and the live args untouched")
        web_interface = _reset_fixture(my_predbat)
        changes = {
            "compare_list[]#1": _add("compare_list[]", "name: Should Not Land\nid: unsaved\n"),
            "compare_list[9]": _delete("compare_list[9]"),
        }
        result = _post_changes(web_interface, changes)
        if result.get("success"):
            print("  ERROR: expected the batch to fail because of the out-of-range delete, got: {}".format(result))
            failed += 1
        saved = _load_yaml()
        if _names(saved["pred_bat"]["compare_list"]) != ["Tariff One", "Tariff Two", "Tariff Three"]:
            print("  ERROR: expected apps.yaml to be unaffected by the failed batch, got: {}".format(_names(saved["pred_bat"]["compare_list"])))
            failed += 1
        if _names(web_interface.args["compare_list"]) != ["Tariff One", "Tariff Two", "Tariff Three"]:
            print("  ERROR: expected the live args to be unaffected by the failed batch too, got: {}".format(_names(web_interface.args["compare_list"])))
            failed += 1

        # ---------------------------------------------------------------------
        print("Test: the rendered compare_list offers delete and add buttons")
        web_interface = _reset_fixture(my_predbat)
        html = web_interface.render_type("compare_list", web_interface.args["compare_list"], "", [1000])
        for expected in ["data-nested-path='compare_list[0]'", "deleteNestedValue(1001)", "addListItem('compare_list', 'compare_list'", "addDictKey('compare_list[0]'"]:
            if expected not in html:
                print("  ERROR: expected {!r} in the rendered compare_list, got:\n{}".format(expected, html))
                failed += 1

        # ---------------------------------------------------------------------
        # There is no JS engine in this test suite, so the client half is checked structurally,
        # as test_debug_history_client_js.py does
        print("Test: the page JS defines the delete and add handlers the buttons call")
        apps_js = get_apps_js("{}")
        for expected in ["function deleteNestedValue(", "function undoDeleteNestedValue(", "function addListItem(", "function addDictKey(", "function registerPendingAdd(", "function cancelPendingAdd("]:
            if expected not in apps_js:
                print("  ERROR: expected {!r} in the apps page JS".format(expected))
                failed += 1

        print("Test: a delete is queued rather than applied, and the button is found by id not by class")
        delete_src = apps_js[apps_js.index("function deleteNestedValue(") : apps_js.index("function undoDeleteNestedValue(")]
        if "type: 'delete'" not in delete_src or "path: nestedPath" not in delete_src:
            print("  ERROR: expected deleteNestedValue to queue a nested delete change, got:\n{}".format(delete_src))
            failed += 1
        # Keyed apart from an edit of the same value, so undoing the delete keeps the edit
        if "pendingChanges[nestedPath + '#delete']" not in delete_src:
            print("  ERROR: expected the deletion to be keyed separately from an edit of the same value, got:\n{}".format(delete_src))
            failed += 1
        # A row containing a nested table also contains its children's delete buttons, so a
        # querySelector('.delete-button') would relabel the wrong one
        if "querySelector('.delete-button')" in apps_js:
            print("  ERROR: the delete button must be looked up by id, as a row can contain its children's buttons")
            failed += 1
        if "getElementById('delete_button_' + rowId)" not in apps_js:
            print("  ERROR: expected the delete button to be looked up by its own id")
            failed += 1

        print("Test: each addition gets a unique change key so several can target one list")
        add_src = apps_js[apps_js.index("function registerPendingAdd(") : apps_js.index("function cancelPendingAdd(")]
        if "addCounter += 1" not in add_src or "path + '#' + addCounter" not in add_src:
            print("  ERROR: expected registerPendingAdd to key each addition uniquely, got:\n{}".format(add_src))
            failed += 1
        if "type: 'add'" not in add_src:
            print("  ERROR: expected registerPendingAdd to queue an add change, got:\n{}".format(add_src))
            failed += 1

        print("Test: discarding changes undoes queued deletions and additions too")
        discard_src = apps_js[apps_js.index("function discardAllChanges(") :]
        if "change.type === 'delete'" not in discard_src or "change.type === 'add'" not in discard_src:
            print("  ERROR: expected discardAllChanges to restore deleted rows and drop added ones, got:\n{}".format(discard_src))
            failed += 1

    finally:
        os.chdir(original_dir)
        shutil.rmtree(temp_dir, ignore_errors=True)

    if failed:
        print("**** ERROR: {} apps.yaml editor add/delete test(s) failed ****".format(failed))
    return failed
