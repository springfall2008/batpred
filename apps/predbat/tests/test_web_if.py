# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
# fmt on
import time
import requests
import os
import shutil
import tempfile
from components import Components


def run_test_web_if(my_predbat):
    """
    Test the web interface
    """
    failed = 0
    print("**** Running web interface test ****\n")

    # Create temp directory and copy apps.yaml
    original_dir = os.getcwd()
    temp_dir = tempfile.mkdtemp(prefix="predbat_test_")
    print(f"Using temporary directory: {temp_dir}")

    try:
        # Copy apps.yaml to temp directory
        if os.path.exists("apps.yaml"):
            shutil.copy("apps.yaml", os.path.join(temp_dir, "apps.yaml"))
        # Create dummy predbat.log
        with open(os.path.join(temp_dir, "predbat.log"), "w") as f:
            f.write("Predbat debug log\n")

        # Change to temp directory
        os.chdir(temp_dir)

        orig_ha_if = my_predbat.ha_interface
        my_predbat.components = Components(my_predbat)
        my_predbat.components.initialize()
        my_predbat.components.start("ha_interface")
        my_predbat.components.start("db")
        my_predbat.components.start("web")
        ha = my_predbat.ha_interface

        # Define all registered endpoints from web.py
        # Format: (method, path)
        all_endpoints = [
            ("GET", "/"),
            ("GET", "/plan"),
            ("GET", "/log"),
            ("GET", "/apps"),
            ("POST", "/apps"),
            ("GET", "/charts"),
            ("GET", "/config"),
            ("GET", "/entity"),
            ("POST", "/entity"),
            ("POST", "/config"),
            ("GET", "/dash"),
            ("POST", "/dash"),
            ("GET", "/components"),
            ("GET", "/component_entities"),
            ("POST", "/component_restart"),
            ("GET", "/component_config"),
            ("POST", "/component_config_save"),
            ("GET", "/debug_yaml"),
            ("GET", "/debug_log"),
            ("GET", "/debug_apps"),
            ("GET", "/debug_plan"),
            ("GET", "/compare"),
            ("POST", "/compare"),
            ("GET", "/apps_editor"),
            ("POST", "/apps_editor"),
            ("GET", "/apps_editor_checksum"),
            ("POST", "/plan_override"),
            ("POST", "/rate_override"),
            ("POST", "/restart"),
            ("GET", "/api/state"),
            ("GET", "/api/ping"),
            ("POST", "/api/state"),
            ("POST", "/api/service"),
            ("GET", "/api/log"),
            ("GET", "/api/entities"),
            ("POST", "/api/login"),
            ("GET", "/browse"),
            ("GET", "/download"),
            ("GET", "/internals"),
            ("GET", "/api/internals"),
            ("GET", "/api/internals/download"),
            ("GET", "/api/status"),
        ]

        # Track accessed endpoints
        accessed_endpoints = set()

        # Fetch all GET pages from 127.0.0.1:5052
        for method, page in all_endpoints:
            if method != "GET":
                continue
            print("Fetch page {}".format(page))
            address = "http://127.0.0.1:5052" + page

            # Add required parameters for endpoints that need them
            params = {}
            if page == "/component_config":
                params = {"component_name": "web"}
            elif page == "/download":
                params = {"file": "apps.yaml"}

            if params:
                res = requests.get(address, params=params)
            else:
                res = requests.get(address)

            # /api/ping returns 500 when Predbat isn't fully initialized (expected in test)
            # Other endpoints may return 400 for missing optional params, which is fine
            acceptable_statuses = [200]
            if page == "/api/ping":
                acceptable_statuses.append(500)
            if res.status_code in acceptable_statuses:
                accessed_endpoints.add(("GET", page))
            else:
                print("ERROR: Unexpected status from {} got {} value {}".format(address, res.status_code, res.text))
                failed = 1

        # Test POST endpoints
        print("\n**** Testing POST endpoints ****")

        # Test /compare POST
        print("Test POST /compare")
        address = "http://127.0.0.1:5052/compare"
        data = {"run": "run"}
        res = requests.post(address, data=data)
        if res.status_code != 200:
            print("ERROR: Failed to post to /compare got status {} value {}".format(res.status_code, res.text))
            failed = 1
        else:
            accessed_endpoints.add(("POST", "/compare"))

        time.sleep(0.1)

        # Test /api/state POST
        print("Test POST /api/state")
        address = "http://127.0.0.1:5052/api/state"
        data = {"entity_id": "sensor.predbat_status", "state": "Idle"}
        res = requests.post(address, json=data)
        # Accept 200 (success) or 500 (entity doesn't exist in test)
        if res.status_code in [200, 500]:
            accessed_endpoints.add(("POST", "/api/state"))
        else:
            print("ERROR: Unexpected response from /api/state: {} - {}".format(res.status_code, res.text))
            failed = 1

        # Test /api/service POST
        print("Test POST /api/service")
        address = "http://127.0.0.1:5052/api/service"
        # Correct format: service field should be full service name like "switch.turn_on"
        data = {"service": "switch/turn_on", "data": {"entity_id": "switch.predbat_active"}}
        res = requests.post(address, json=data)
        if res.status_code in [200]:  # May fail if service doesn't exist in test
            accessed_endpoints.add(("POST", "/api/service"))
        else:
            print("ERROR: Unexpected response from /api/service: {} - {}".format(res.status_code, res.text))
            failed = 1

        # Test /config POST
        print("Test POST /config")
        address = "http://127.0.0.1:5052/config"
        data = {"set_read_only": "true"}
        res = requests.post(address, data=data)
        if res.status_code in [200]:  # Redirects are OK
            accessed_endpoints.add(("POST", "/config"))
        else:
            print("ERROR: Unexpected response from /config: {} - {}".format(res.status_code, res.text))
            failed = 1

        # Test /dash POST
        print("Test POST /dash")
        address = "http://127.0.0.1:5052/dash"
        data = {"mode": "Monitor"}
        res = requests.post(address, data=data)
        if res.status_code in [200]:
            accessed_endpoints.add(("POST", "/dash"))
        else:
            print("ERROR: Unexpected response from /dash: {} - {}".format(res.status_code, res.text))
            failed = 1

        # Test /entity POST
        print("Test POST /entity")
        address = "http://127.0.0.1:5052/entity"
        data = {"entity_id": "switch.predbat_active", "value": "on"}
        res = requests.post(address, data=data)
        if res.status_code in [200]:
            accessed_endpoints.add(("POST", "/entity"))
        else:
            print("ERROR: Unexpected response from /entity: {} - {}".format(res.status_code, res.text))
            failed = 1

        # Test /apps POST
        print("Test POST /apps")
        address = "http://127.0.0.1:5052/apps"
        data = {"apps_content": "test: value"}
        res = requests.post(address, data=data)
        if res.status_code in [200]:
            accessed_endpoints.add(("POST", "/apps"))
        else:
            print("ERROR: Unexpected response from /apps: {} - {}".format(res.status_code, res.text))
            failed = 1

        # Test /apps_editor POST
        print("Test POST /apps_editor")
        address = "http://127.0.0.1:5052/apps_editor"
        data = {"dummy": "data"}
        res = requests.post(address, data=data)
        if res.status_code in [200]:
            accessed_endpoints.add(("POST", "/apps_editor"))
        else:
            print("ERROR: Unexpected response from /apps_editor: {} - {}".format(res.status_code, res.text))
            failed = 1

        # Test /plan_override POST
        print("Test POST /plan_override")
        address = "http://127.0.0.1:5052/plan_override"
        data = {"time": "00:00", "action": "Clear"}
        res = requests.post(address, data=data)
        if res.status_code in [200]:
            accessed_endpoints.add(("POST", "/plan_override"))
        else:
            print("ERROR: Unexpected response from /plan_override: {} - {}".format(res.status_code, res.text))
            failed = 1

        # Test /rate_override POST
        print("Test POST /rate_override")
        address = "http://127.0.0.1:5052/rate_override"
        data = {"time": "00:00", "rate": "15", "action": "Clear SOC"}
        res = requests.post(address, data=data)
        if res.status_code in [200]:
            accessed_endpoints.add(("POST", "/rate_override"))
        else:
            print("ERROR: Unexpected response from /rate_override: {} - {}".format(res.status_code, res.text))
            failed = 1

        # Test /restart POST
        print("Test POST /restart")
        address = "http://127.0.0.1:5052/restart"
        res = requests.post(address, data={})
        if res.status_code in [200]:
            accessed_endpoints.add(("POST", "/restart"))
        else:
            print("ERROR: Unexpected response from /restart: {} - {}".format(res.status_code, res.text))
            failed = 1

        # Test /component_restart POST
        print("Test POST /component_restart")
        address = "http://127.0.0.1:5052/component_restart"
        data = {"component": "db"}
        res = requests.post(address, data=data)
        if res.status_code in [200]:
            accessed_endpoints.add(("POST", "/component_restart"))
        else:
            print("ERROR: Unexpected response from /component_restart: {} - {}".format(res.status_code, res.text))
            failed = 1

        # Test /component_config_save POST
        print("Test POST /component_config_save")
        address = "http://127.0.0.1:5052/component_config_save"
        # Correct format: JSON with component_name, changes, deletions
        data = {"component_name": "web", "changes": {}, "deletions": []}
        res = requests.post(address, json=data)
        if res.status_code in [200]:  # May fail if component doesn't support config changes
            accessed_endpoints.add(("POST", "/component_config_save"))
        else:
            print("ERROR: Unexpected response from /component_config_save: {} - {}".format(res.status_code, res.text))
            failed = 1

        # Test /api/login POST
        print("Test POST /api/login")
        address = "http://127.0.0.1:5052/api/login"
        data = {"token": "invalid_token"}
        res = requests.post(address, json=data)
        if res.status_code in [200]:  # Expect auth failure
            accessed_endpoints.add(("POST", "/api/login"))
        else:
            print("ERROR: Unexpected response from /api/login: {} - {}".format(res.status_code, res.text))
            failed = 1

        print("\n**** Verifying compare tariffs functionality ****")
        time.sleep(0.1)
        # Get service data
        entity_id = "switch.predbat_compare_active"
        result = ha.get_state(entity_id)

        if result != "on":
            print("ERROR: Compare tariffs not triggered - expected {} got {}".format("on", result))
            failed = 1

        # Check endpoint coverage
        print("\n**** Checking endpoint coverage ****")
        untested_endpoints = []
        for endpoint in all_endpoints:
            if endpoint not in accessed_endpoints:
                untested_endpoints.append(endpoint)

        if untested_endpoints:
            print("\nWARNING: The following endpoints were not tested:")
            for method, path in sorted(untested_endpoints):
                print(f"  {method:6s} {path}")
            print(f"\nTotal: {len(untested_endpoints)} untested endpoints out of {len(all_endpoints)}")
            print(f"Coverage: {len(accessed_endpoints)}/{len(all_endpoints)} ({100*len(accessed_endpoints)//len(all_endpoints)}%)")
            failed = 1
        else:
            if failed == 0:
                print("\nSUCCESS: All endpoints were tested successfully!")
            else:
                print("\nFAILED: All endpoints were accessed but some tests failed. Please review the errors above.")

        # Run stop as task as we need to await it
        my_predbat.create_task(my_predbat.components.stop("ha_interface"))
        my_predbat.create_task(my_predbat.components.stop("web"))
        my_predbat.create_task(my_predbat.components.stop("db"))
        time.sleep(0.1)
        my_predbat.components = Components(my_predbat)
        my_predbat.ha_interface = orig_ha_if

    finally:
        # Clean up: return to original directory and remove temp dir
        os.chdir(original_dir)
        try:
            shutil.rmtree(temp_dir)
            print(f"\nCleaned up temporary directory: {temp_dir}")
        except Exception as e:
            print(f"\nWarning: Failed to clean up temp directory {temp_dir}: {e}")

    return failed
