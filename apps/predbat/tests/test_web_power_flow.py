# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

import math
import re

from config import APPS_SCHEMA
from web import WebInterface

# Where each node is drawn in the diagram's SVG, keyed by the colour of the arm that reaches it
HOUSE_CENTRE = (300, 200)
NODE_RADIUS = 50
NODE_BY_COLOUR = {
    "#2196F3": ("PV", (150, 100)),
    "#FF9800": ("Battery", (150, 300)),
    "#4CAF50": ("Grid", (450, 300)),
    "#00BCD4": ("Car", (450, 100)),
}

# The arrowhead is drawn beyond the end of the line, not on it. The markers use the default
# markerUnits of "strokeWidth", so markerWidth="10" on a stroke-width="2" line is 20 user units
# of arrowhead past the line's end vertex - which is where the point of the arrow actually lands.
ARROW_HEAD_LENGTH = 20


def make_web(my_predbat):
    """Create a WebInterface instance bound to the given predbat."""
    return WebInterface(my_predbat, web_port=5053)


def set_power_sensor(my_predbat, entity_id, watts):
    """Publish a power sensor into the HA mock for get_arg() to resolve."""
    my_predbat.ha_interface.dummy_items[entity_id] = {"state": watts, "unit_of_measurement": "W"}


def arrow_lines(html):
    """Return (colour, (x1, y1, x2, y2)) for every arrow drawn in the diagram."""
    found = []
    for match in re.finditer(r'<line x1="([-\d.]+)" y1="([-\d.]+)" x2="([-\d.]+)" y2="([-\d.]+)" stroke="(#[0-9A-Fa-f]{6})"', html):
        found.append((match.group(5), tuple(float(match.group(index)) for index in range(1, 5))))
    return found


def distance(point_a, point_b):
    """Distance between two (x, y) points."""
    return math.hypot(point_a[0] - point_b[0], point_a[1] - point_b[1])


def distance_from_line(point, line_start, line_end):
    """Perpendicular distance of a point from the infinite line through two points."""
    run_x = line_end[0] - line_start[0]
    run_y = line_end[1] - line_start[1]
    cross = (point[0] - line_start[0]) * run_y - (point[1] - line_start[1]) * run_x
    return abs(cross) / math.hypot(run_x, run_y)


def check_arm_geometry(html, state_description):
    """Every arm runs along the line joining the two circles, tail on one edge and point on the other.

    Drawn at any other angle the arrow leaves its circle off-centre and its head stops in open
    space short of the House, which is what the PV, battery and grid arms did (all three were
    drawn at 45 degrees when the true angle between the circles is 33.7).

    The point of the arrow is ARROW_HEAD_LENGTH beyond the line's end vertex, so the line has to
    stop short by exactly that much. Ending it on the circle edge instead drives the arrowhead
    inside the circle it is pointing at.
    """
    failed = 0
    for colour, (x1, y1, x2, y2) in arrow_lines(html):
        name, node_centre = NODE_BY_COLOUR[colour]
        start, end = (x1, y1), (x2, y2)

        # marker-end puts the arrowhead on (x2, y2), so the line is always drawn tail first and
        # an arm reversed by the flow direction swaps which circle each end belongs to
        tail, head = start, end
        tail_at_node = distance(tail, node_centre) < distance(tail, HOUSE_CENTRE)
        tail_centre, head_centre = (node_centre, HOUSE_CENTRE) if tail_at_node else (HOUSE_CENTRE, node_centre)
        tail_name, head_name = (name, "House") if tail_at_node else ("House", name)

        tail_gap = distance(tail, tail_centre) - NODE_RADIUS
        if abs(tail_gap) > 1.5:
            print(f"  ERROR [{state_description}]: the {name} arm starts {tail_gap:+.1f}px off the {tail_name} circle edge, it should touch it")
            failed += 1

        # Where the point of the arrow actually lands, once the marker past the line end is counted
        length = distance(tail, head)
        if length > 0:
            direction = ((head[0] - tail[0]) / length, (head[1] - tail[1]) / length)
            tip = (head[0] + direction[0] * ARROW_HEAD_LENGTH, head[1] + direction[1] * ARROW_HEAD_LENGTH)
            tip_gap = distance(tip, head_centre) - NODE_RADIUS
            if tip_gap < -1.5:
                print(f"  ERROR [{state_description}]: the {name} arrowhead reaches {abs(tip_gap):.1f}px inside the {head_name} circle, it should stop on the edge")
                failed += 1
            elif tip_gap > 1.5:
                print(f"  ERROR [{state_description}]: the {name} arrowhead stops {tip_gap:.1f}px short of the {head_name} circle, leaving it pointing at nothing")
                failed += 1

        for point in (tail, head):
            offset = distance_from_line(point, node_centre, HOUSE_CENTRE)
            if offset > 1.5:
                print(f"  ERROR [{state_description}]: the {name} arm is {offset:.1f}px off the line joining {name} to the House, so it points the wrong way")
                failed += 1
    return failed


def power_labels(html):
    """Return (colour, text, x, y) for each 'NNN W' label on an arm."""
    found = []
    for match in re.finditer(r'<text x="([-\d.]+)" y="([-\d.]+)" text-anchor="middle" fill="(#[0-9A-Fa-f]{6})">([^<]*)</text>', html):
        if match.group(3) in NODE_BY_COLOUR:
            found.append((match.group(3), match.group(4), float(match.group(1)), float(match.group(2))))
    return found


def segment_hits_box(segment, box):
    """True if a line segment crosses (or ends inside) an axis-aligned box."""
    (x1, y1), (x2, y2) = segment
    left, top, right, bottom = box

    def inside(x, y):
        return left <= x <= right and top <= y <= bottom

    if inside(x1, y1) or inside(x2, y2):
        return True

    def segments_cross(p1, p2, p3, p4):
        def orientation(a, b, c):
            value = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
            return (value > 0) - (value < 0)

        return orientation(p1, p2, p3) != orientation(p1, p2, p4) and orientation(p3, p4, p1) != orientation(p3, p4, p2)

    corners = [(left, top), (right, top), (right, bottom), (left, bottom)]
    for index in range(4):
        if segments_cross((x1, y1), (x2, y2), corners[index], corners[(index + 1) % 4]):
            return True
    return False


def check_label_clearance(html, state_description):
    """A power reading must sit beside its arm, not printed across it.

    The text is rendered at the browser default of 16px with no font-size set, so it is estimated
    generously here - a label that only just clears at this width is too close to be readable.
    """
    failed = 0
    lines = [((x1, y1), (x2, y2)) for _, (x1, y1, x2, y2) in arrow_lines(html)]
    for colour, text, x, y in power_labels(html):
        name = NODE_BY_COLOUR[colour][0]
        half_width = len(text) * 9 / 2.0
        box = (x - half_width, y - 12, x + half_width, y + 4)
        for line in lines:
            if segment_hits_box(line, box):
                print(f"  ERROR [{state_description}]: the {name} label '{text}' at ({x:.0f},{y:.0f}) is printed across an arm")
                failed += 1
                break
    return failed


def run_power_flow_geometry_tests(my_predbat, web):
    """The arms line up with the circles they join, in every flow direction."""
    failed = 0
    print("Test: every arm runs circle edge to circle edge, whichever way the power is flowing")

    saved = (my_predbat.pv_power, my_predbat.load_power, my_predbat.battery_power, my_predbat.grid_power, my_predbat.car_charging_power, my_predbat.car_charging_power_configured)

    my_predbat.car_charging_power_configured = True

    # Importing, battery discharging, PV generating, car charging
    my_predbat.pv_power = 2000
    my_predbat.load_power = 4000
    my_predbat.battery_power = 1500
    my_predbat.grid_power = -1000
    my_predbat.car_charging_power = 2000
    html = web.get_power_flow_diagram()
    failed += check_arm_geometry(html, "importing, battery discharging")
    failed += check_label_clearance(html, "importing, battery discharging")

    # The opposite of every branch: exporting, battery charging, PV idle, car idle
    my_predbat.pv_power = 0
    my_predbat.battery_power = -1500
    my_predbat.grid_power = 1000
    my_predbat.car_charging_power = 0
    html = web.get_power_flow_diagram()
    failed += check_arm_geometry(html, "exporting, battery charging")
    failed += check_label_clearance(html, "exporting, battery charging")

    (my_predbat.pv_power, my_predbat.load_power, my_predbat.battery_power, my_predbat.grid_power, my_predbat.car_charging_power, my_predbat.car_charging_power_configured) = saved
    return failed


def run_web_power_flow_tests(my_predbat):
    """Car charging power input, published sensor and its arm of the power flow diagram."""
    failed = 0
    print("**** Running web power flow car charging tests ****")

    original_args = my_predbat.args.copy()
    original_load_power = my_predbat.load_power
    web = make_web(my_predbat)

    # -------------------------------------------------------------------------
    print("Test: car_charging_power is a known apps.yaml key")
    if "car_charging_power" not in APPS_SCHEMA:
        print("  ERROR: car_charging_power missing from APPS_SCHEMA, apps.yaml validation would reject it")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: no car_charging_power configured leaves the reading at zero and unconfigured")
    my_predbat.args.pop("car_charging_power", None)
    my_predbat.update_car_charging_power()
    if my_predbat.car_charging_power != 0:
        print(f"  ERROR: expected 0 W with nothing configured, got {my_predbat.car_charging_power}")
        failed += 1
    if my_predbat.car_charging_power_configured:
        print("  ERROR: car_charging_power_configured should be False when the key is not set")
        failed += 1

    # -------------------------------------------------------------------------
    # The apps.yaml templates ship car_charging_power as a regular expression matching the
    # common chargers. auto_config(final=True) deletes it when nothing matches, but until then
    # the literal "re:" string is still sitting in args - and a household with no car charger
    # must not get a Car node drawn from it
    print("Test: an unmatched regular expression default counts as not configured")
    my_predbat.args["car_charging_power"] = "re:(sensor.myenergi_zappi_[0-9a-z]+_internal_load_ct1|sensor.wallbox_portal_charging_power)"
    my_predbat.update_car_charging_power()
    if my_predbat.car_charging_power_configured:
        print("  ERROR: an unresolved 're:' expression should not count as a configured charger")
        failed += 1
    if my_predbat.car_charging_power != 0:
        print(f"  ERROR: expected 0 W from an unresolved 're:' expression, got {my_predbat.car_charging_power}")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: a single car_charging_power sensor is read")
    set_power_sensor(my_predbat, "sensor.car_charger_power", 3200)
    my_predbat.args["car_charging_power"] = "sensor.car_charger_power"
    my_predbat.update_car_charging_power()
    if my_predbat.car_charging_power != 3200:
        print(f"  ERROR: expected 3200 W from the configured sensor, got {my_predbat.car_charging_power}")
        failed += 1
    if not my_predbat.car_charging_power_configured:
        print("  ERROR: car_charging_power_configured should be True once the key is set")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: multiple chargers are summed")
    set_power_sensor(my_predbat, "sensor.car_charger_power_2", 1500)
    my_predbat.args["car_charging_power"] = ["sensor.car_charger_power", "sensor.car_charger_power_2"]
    my_predbat.update_car_charging_power()
    if my_predbat.car_charging_power != 4700:
        print(f"  ERROR: expected 3200 + 1500 = 4700 W summed over both chargers, got {my_predbat.car_charging_power}")
        failed += 1

    # -------------------------------------------------------------------------
    # auto_config() replaces a list entry whose regular expression found nothing with None,
    # leaving holes in the middle of the list rather than shortening it
    print("Test: a hole in the sensor list does not hide the chargers after it")
    my_predbat.args["car_charging_power"] = [None, "sensor.car_charger_power_2"]
    my_predbat.update_car_charging_power()
    if my_predbat.car_charging_power != 1500:
        print(f"  ERROR: expected the second charger's 1500 W to still be read past the hole, got {my_predbat.car_charging_power}")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: kW sensors are converted to W")
    my_predbat.ha_interface.dummy_items["sensor.car_charger_power_kw"] = {"state": 7.2, "unit_of_measurement": "kW"}
    my_predbat.args["car_charging_power"] = "sensor.car_charger_power_kw"
    my_predbat.update_car_charging_power()
    if my_predbat.car_charging_power != 7200:
        print(f"  ERROR: expected 7.2 kW to be read as 7200 W, got {my_predbat.car_charging_power}")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: an unavailable sensor reads as zero rather than breaking the total")
    my_predbat.ha_interface.dummy_items["sensor.car_charger_offline"] = {"state": "unavailable", "unit_of_measurement": "W"}
    my_predbat.args["car_charging_power"] = ["sensor.car_charger_power", "sensor.car_charger_offline"]
    original_had_errors = my_predbat.had_errors
    my_predbat.had_errors = False
    my_predbat.update_car_charging_power()
    if my_predbat.car_charging_power != 3200:
        print(f"  ERROR: expected the good charger's 3200 W with the other unavailable, got {my_predbat.car_charging_power}")
        failed += 1
    if not my_predbat.car_charging_power_configured:
        print("  ERROR: an unavailable sensor should still count as configured")
        failed += 1
    # A charger reporting 'unavailable' while nothing is plugged in is normal, so it must not
    # flag the run as errored - that would leave Predbat sitting in "with Errors" all day
    if my_predbat.had_errors:
        print("  ERROR: an unavailable car charger sensor should not put the run into an error state")
        failed += 1
    my_predbat.had_errors = original_had_errors

    # -------------------------------------------------------------------------
    print("Test: car charging power is published as a sensor for upstream consumers")
    my_predbat.args["car_charging_power"] = "sensor.car_charger_power"
    my_predbat.update_car_charging_power()
    my_predbat.publish_inverter_data()
    entity = my_predbat.prefix + ".car_charging_power"
    attrs = my_predbat.ha_interface.dummy_items.get(entity)
    if not attrs:
        print(f"  ERROR: {entity} was not published")
        failed += 1
    else:
        if attrs.get("state") != 3.2:
            print(f"  ERROR: expected {entity} to publish 3.2 kW, got {attrs.get('state')}")
            failed += 1
        if attrs.get("unit_of_measurement") != "kW":
            print(f"  ERROR: expected {entity} in kW like the other power sensors, got {attrs.get('unit_of_measurement')}")
            failed += 1
        if attrs.get("device_class") != "power":
            print(f"  ERROR: expected {entity} to carry device_class power, got {attrs.get('device_class')}")
            failed += 1

    # -------------------------------------------------------------------------
    print("Test: the sensor is not published when no charger is configured")
    my_predbat.ha_interface.dummy_items.pop(entity, None)
    my_predbat.args.pop("car_charging_power", None)
    my_predbat.update_car_charging_power()
    my_predbat.publish_inverter_data()
    if entity in my_predbat.ha_interface.dummy_items:
        print(f"  ERROR: {entity} should not be published when no car charging power sensor is configured")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: the flow diagram is unchanged when no car charging power is configured")
    my_predbat.load_power = 3000
    my_predbat.car_charging_power = 0
    my_predbat.car_charging_power_configured = False
    html = web.get_power_flow_diagram()
    if ">Car<" in html:
        print("  ERROR: the Car node should not be drawn when no car charging power sensor is configured")
        failed += 1
    if ">3000 W<" not in html:
        print("  ERROR: the House circle should show the full load power when there is no car to subtract")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: a charging car is drawn with its own arm and subtracted from the house load")
    my_predbat.load_power = 3000
    my_predbat.car_charging_power = 2000
    my_predbat.car_charging_power_configured = True
    html = web.get_power_flow_diagram()
    if ">Car<" not in html:
        print("  ERROR: expected a Car node in the diagram once a car charging power sensor is configured")
        failed += 1
    if ">2000 W<" not in html:
        print("  ERROR: expected the car charging power to be labelled on its arrow")
        failed += 1
    if ">1000 W<" not in html:
        print("  ERROR: expected the House circle to show the load remainder (3000 - 2000) once the car is drawn separately")
        failed += 1
    if "car-house-path" in html and "house-car-path" not in html:
        print("  ERROR: the car arm should flow from the house to the car, not the other way")
        failed += 1
    if "animateMotion" not in html:
        print("  ERROR: expected animated flow dots on the car arm while it is charging")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: a configured but idle charger keeps its node with a dashed arm")
    my_predbat.car_charging_power = 0
    my_predbat.car_charging_power_configured = True
    html = web.get_power_flow_diagram()
    if ">Car<" not in html:
        print("  ERROR: a configured charger should stay on the diagram when it is not charging")
        failed += 1
    car_arm = html[html.find("<!-- House to Car") :] if "<!-- House to Car" in html else ""
    if "stroke-dasharray" not in car_arm:
        print("  ERROR: expected the idle car arm to be dashed, as the PV arm is when not generating")
        failed += 1
    if ">3000 W<" not in html:
        print("  ERROR: the House circle should show the full load power when the car is drawing nothing")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: a car reading above the house load clamps the house remainder at zero")
    my_predbat.load_power = 1000
    my_predbat.car_charging_power = 3000
    my_predbat.car_charging_power_configured = True
    html = web.get_power_flow_diagram()
    if ">0 W<" not in html:
        print("  ERROR: expected the House remainder to clamp at 0 W rather than go negative")
        failed += 1
    if "-2000 W" in html:
        print("  ERROR: the House circle must never show a negative load")
        failed += 1

    failed += run_power_flow_geometry_tests(my_predbat, web)

    my_predbat.args = original_args
    my_predbat.load_power = original_load_power
    my_predbat.car_charging_power = 0
    my_predbat.car_charging_power_configured = False

    print("**** Web power flow car charging tests completed ****")
    return failed
