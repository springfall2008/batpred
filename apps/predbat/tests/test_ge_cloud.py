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

from gecloud import GECloudDirect
import time


def run_test_ge_cloud(my_predbat):
    """
    GE Cloud test
    """
    failed = False

    ge_cloud_direct = GECloudDirect(my_predbat)
    ge_cloud_direct_task = my_predbat.create_task(ge_cloud_direct.start())
    while not "devices" in ge_cloud_direct.__dict__:
        time.sleep(1)
    devices = ge_cloud_direct.devices
    if not devices:
        print("ERROR: No devices found")
        failed = True
    else:
        for device in devices:
            print("Device {} found:".format(device))
            while not ge_cloud_direct.settings.get(device):
                time.sleep(1)
            print("Device {} synced".format(device))

        my_predbat.create_task(ge_cloud_direct.switch_event("switch.predbat_gecloud_sa2243g277_ac_charge_enable", "turn_on"))
        time.sleep(1)
    print("Stopping cloud")
    ge_cloud_direct.stop_cloud = True
    time.sleep(1)

    return failed
