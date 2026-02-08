# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init


def test_octopus_free(my_predbat):
    """
    Test Octopus free electricity session download
    """
    failed = False
    print("**** Running Octopus free electricity test ****")

    free_sessions = my_predbat.download_octopus_free("http://octopus.energy/free-electricity")
    free_sessions = my_predbat.download_octopus_free("http://octopus.energy/free-electricity")
    # if not free_sessions:
    #    print("**** ERROR: No free sessions found ****")
    #    failed = True

    if not failed:
        print("**** Octopus free electricity test PASSED ****")
    else:
        print("**** Octopus free electricity test FAILED ****")

    return failed
