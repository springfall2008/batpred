# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
# fmt on
import time
import requests
from components import Components

def run_test_web_if(my_predbat):
    """
    Test the web interface
    """
    failed = 0
    print("**** Running web interface test ****\n")
    orig_ha_if = my_predbat.ha_interface
    my_predbat.components = Components(my_predbat)
    my_predbat.components.initialize()
    my_predbat.components.start("ha_interface")
    my_predbat.components.start("db")
    my_predbat.components.start("web")
    ha = my_predbat.ha_interface

    # Fetch page from 127.0.0.1:5052
    for page in ["/", "/dash", "/plan", "/config", "/apps", "/charts", "/compare", "/log", "/entity", "/components", "/browse"]:
        print("Fetch page {}".format(page))
        address = "http://127.0.0.1:5052" + page
        res = requests.get(address)
        if res.status_code != 200:
            print("ERROR: Failed to fetch from page {} got status {} value {}".format(address, res.status_code, res.text))
            failed = 1

    # Perform a post to /compare page with data for form 'compareform' value 'run'
    print("**** Running test: Fetch page /compare with post")

    address = "http://127.0.0.1:5052/compare"
    data = {"run": "run"}
    res = requests.post(address, data=data)
    if res.status_code != 200:
        print("ERROR: Failed to post to pagepage {} got status {} value {}".format(address, res.status_code, res.text))
        failed = 1
    time.sleep(0.1)
    # Get service data
    entity_id = "switch.predbat_compare_active"
    result = ha.get_state(entity_id)

    if result != "on":
        print("ERROR: Compare tariffs not triggered - expected {} got {}".format("on", result))
        failed = 1

    # Run stop as task as we need to await it
    my_predbat.create_task(my_predbat.components.stop("ha_interface"))
    my_predbat.create_task(my_predbat.components.stop("web"))
    my_predbat.create_task(my_predbat.components.stop("db"))
    time.sleep(0.1)
    my_predbat.components = Components(my_predbat)
    my_predbat.ha_interface = orig_ha_if
    return failed