"""
Tests for how OctopusAPI classifies savingSessions events by eventType
"""

from unittest.mock import MagicMock
from octopus import OctopusAPI


def test_octopus_saving_event_type(my_predbat):
    """
    Test get_saving_session_data() splits events by eventType.

    Octopus publishes Power Up and Power Down through the same savingSessions feed and tells them
    apart with eventType. A WEEKEND_HAPPY_HOUR cannot be joined through the API - Octopus either
    allocates one or the user books it on the website - so it must not be offered as available,
    matching BottleCapDave's integration from v19.0.1. It must still reach joined_events carrying
    its type, because that is what the planner uses to price it as free (issue #4851).
    """
    print("**** Running Octopus saving session eventType tests ****")
    failed = False

    def make_api(events, joined):
        api = OctopusAPI(my_predbat, key="test-key", account_id="A-TEST", automatic=False)
        api.dashboard_item = MagicMock()
        api.expose_config = MagicMock()
        api.saving_sessions = {"events": events, "account": {"hasJoinedCampaign": True, "joinedEvents": joined, "signedUpMeterPoint": {"regionId": 6}}}
        return api

    # A national Happy Hour the account was allocated, three more it was not, and a real Power Down
    happy_joined = {"id": 5797, "code": "EVENT_56", "rewardPerKwhInOctoPoints": 0, "startAt": "2099-01-05T10:00:00+00:00", "endAt": "2099-01-05T11:00:00+00:00", "eventType": "WEEKEND_HAPPY_HOUR", "targetRegion": []}
    happy_offered = {"id": 5798, "code": "EVENT_57", "rewardPerKwhInOctoPoints": 0, "startAt": "2099-01-05T11:00:00+00:00", "endAt": "2099-01-05T12:00:00+00:00", "eventType": "WEEKEND_HAPPY_HOUR", "targetRegion": []}
    power_down = {"id": 5799, "code": "EVENT_58", "rewardPerKwhInOctoPoints": 93, "startAt": "2099-01-06T17:00:00+00:00", "endAt": "2099-01-06T18:00:00+00:00", "eventType": "TURN_DOWN", "targetRegion": []}

    api = make_api(
        [happy_joined, happy_offered, power_down],
        [{"eventId": 5797, "startAt": happy_joined["startAt"], "endAt": happy_joined["endAt"], "rewardGivenInOctoPoints": None}],
    )
    available, joined = api.get_saving_session_data()

    available_codes = [event.get("code") for event in available]
    if available_codes != ["EVENT_58"]:
        print("ERROR: Expected only the TURN_DOWN to be offered as available, got {}".format(available_codes))
        failed = True
    elif len(joined) != 1 or joined[0].get("id") != 5797:
        print("ERROR: Expected the allocated Happy Hour in joined_events, got {}".format(joined))
        failed = True
    elif joined[0].get("event_type") != "WEEKEND_HAPPY_HOUR":
        # The type is looked up from the events list, which is also where the skip happens - if the
        # skip were applied before the lookup map is filled, the planner would price this as a
        # saving session instead of as free
        print("ERROR: The joined Happy Hour lost its event_type, got {}".format(joined[0]))
        failed = True
    elif available[0].get("event_type") != "TURN_DOWN":
        print("ERROR: Expected the available TURN_DOWN to carry its event_type, got {}".format(available[0]))
        failed = True
    else:
        print("PASS: Happy Hours are withheld from available_events but keep their type when joined")

    # An event with no eventType is unchanged - older API responses must keep working
    untyped = {"id": 5900, "code": "EVENT_59", "rewardPerKwhInOctoPoints": 200, "startAt": "2030-01-07T17:00:00+00:00", "endAt": "2030-01-07T18:00:00+00:00", "targetRegion": []}
    api = make_api([untyped], [])
    available, _ = api.get_saving_session_data()
    if [event.get("code") for event in available] != ["EVENT_59"]:
        print("ERROR: An event with no eventType must still be offered, got {}".format(available))
        failed = True
    elif available[0].get("event_type") is not None:
        print("ERROR: Expected event_type None for an untyped event, got {}".format(available[0]))
        failed = True
    else:
        print("PASS: An event with no eventType is unaffected")

    if failed:
        print("\n**** Octopus saving session eventType tests FAILED ****")
        return 1
    print("\n**** All Octopus saving session eventType tests PASSED ****")
    return 0
