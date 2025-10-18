# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

import requests
import re
from datetime import datetime, timezone
from utils import str2time, dp1
import xml.etree.ElementTree as etree
import asyncio
import time


class AlertFeed:
    def __init__(self, alert_config, base):
        self.base = base
        self.prefix = self.base.prefix
        self.alert_cache = {}
        self.log = self.base.log
        self.alert_config = alert_config
        self.alert_url = self.alert_config.get("url", "https://feeds.meteoalarm.org/feeds/meteoalarm-legacy-atom-united-kingdom")
        self.last_success_timestamp = None
        self.stop_api = False
        self.api_started = False

    def wait_api_started(self):
        """
        Wait for the API to start
        """
        self.log("AlertFeed: Waiting for API to start")
        count = 0
        while not self.api_started and count < 240:
            time.sleep(1)
            count += 1
        if not self.api_started:
            self.log("Warn: AlertFeed: Failed to start")
            return False
        return True

    def is_alive(self):
        """
        Check if the API is alive
        """
        return self.api_started

    def last_updated_time(self):
        """
        Get the last successful update time
        """
        return self.last_success_timestamp

    async def start(self):
        """
        Main run loop
        """
        first = True
        count_seconds = 0
        self.api_started = True
        while not self.stop_api:
            try:
                if first or count_seconds % (60 * 30) == 0:
                    # Download alerts
                    self.alert_xml = self.download_alert_data(self.alert_url)
                    first = False
                    self.api_started = True
            except Exception as e:
                self.log("Warn: AlertFeed: Exception in alert feed main loop: {}".format(e))

            await asyncio.sleep(5)
            count_seconds += 5

        # Clean up on exit
        self.api_started = False

    async def stop(self):
        self.stop_api = True

    def process_alerts(self, minutes_now, midnight_utc, testing=False):
        """
        Process the alerts from the alert feed
        """

        alerts = []
        alert_active_keep = {}
        alert_config = self.alert_config
        if not alert_config:
            return alerts, alert_active_keep
        if not isinstance(alert_config, dict):
            self.log("Warn: AlertFeed: Alerts must be a dictionary, ignoring")
            return alerts, alert_active_keep

        # Try apps.yaml
        latitude = alert_config.get("latitude", None)
        longitude = alert_config.get("longitude", None)

        # If latitude and longitude are not provided, use zone.home
        if latitude is None:
            latitude = self.base.get_state_wrapper("zone.home", attribute="latitude")
        if longitude is None:
            longitude = self.base.get_state_wrapper("zone.home", attribute="longitude")

        # If latitude and longitude are not found, we cannot process alerts
        if latitude and longitude:
            self.log("AlertFeed: Processing alerts for approx position latitude {} longitude {}".format(dp1(latitude), dp1(longitude)))
        else:
            if not testing:
                self.log("Warn: AlertFeed: No latitude or longitude found, cannot process alerts")
                return alerts, alert_active_keep

        area = alert_config.get("area", "")
        event = alert_config.get("event", "")
        severity = alert_config.get("severity", "")
        certainty = alert_config.get("certainty", "")
        urgency = alert_config.get("urgency", "")
        keep = alert_config.get("keep", 100)

        if self.alert_xml:
            alerts = self.parse_alert_data(self.alert_xml)
            alerts = self.filter_alerts(alerts, area, event, severity, certainty, urgency, latitude, longitude)
            alert_active_keep = self.apply_alerts(alerts, keep, minutes_now, midnight_utc)

        return alerts, alert_active_keep

    def apply_alerts(self, alerts, keep, minutes_now, midnight_utc):
        """
        Apply the alerts to the active alert list
        """
        alert_active_keep = {}
        active_alert_text = ""
        active_alert = False

        if alerts:
            for alert in alerts:
                onset = alert.get("onset", None)
                expires = alert.get("expires", None)
                severity = alert.get("severity", "")
                certainty = alert.get("certainty", "")
                urgency = alert.get("urgency", "")
                area = alert.get("areaDesc", "")

                if onset and expires:
                    onset_minutes = int((onset - midnight_utc).total_seconds() / 60)
                    expires_minutes = int((expires - midnight_utc).total_seconds() / 60)
                    if expires_minutes >= minutes_now:
                        self.log("Info: AlertFeed: Active alert: {} severity {} certainty {} urgency {} from {} to {} applying keep {}".format(alert.get("event"), severity, certainty, urgency, onset, expires, keep))
                        for minute in range(onset_minutes, expires_minutes):
                            if minute not in alert_active_keep:
                                alert_active_keep[minute] = keep
                            else:
                                alert_active_keep[minute] = max(alert_active_keep[minute], keep)
                            if minute == minutes_now:
                                active_alert_text = alert.get("event") + " until " + str(expires)
                                active_alert = True

        alert_keep = alert_active_keep.get(minutes_now, 0)
        alert_show = []
        for alert in alerts:
            item = {}
            item["event"] = alert.get("event", "")
            item["severity"] = alert.get("severity", "")
            item["certainty"] = alert.get("certainty", "")
            item["urgency"] = alert.get("urgency", "")
            item["area"] = alert.get("areaDesc", "")
            item["onset"] = str(alert.get("onset", ""))
            item["expires"] = str(alert.get("expires", ""))
            item["title"] = alert.get("title", "")
            item["status"] = alert.get("status", "")
            alert_show.append(item)
        self.base.dashboard_item("sensor." + self.prefix + "_alerts", state=active_alert_text, attributes={"friendly_name": "Weather alerts", "icon": "mdi:alert-outline", "keep": alert_keep, "alerts": alert_show}, app="alertfeed")

        return alert_active_keep

    def is_point_in_polygon(self, lat, lon, polygon):
        """
        Determines if a given point is inside a polygon.

        Parameters:
            lat (float): Latitude of the point.
            lon (float): Longitude of the point.
            polygon (list of tuples): List of (latitude, longitude) tuples defining the polygon.

        Returns:
            bool: True if the point is inside the polygon, False otherwise.
        """
        num_vertices = len(polygon)
        inside = False

        # Loop through each edge of the polygon
        for i in range(num_vertices):
            lat1, lon1 = polygon[i]
            lat2, lon2 = polygon[(i + 1) % num_vertices]

            # Check if the point is on the boundary
            if (lat == lat1 and lon == lon1) or (lat == lat2 and lon == lon2):
                return True

            # Check if the edge crosses the ray
            if ((lon > lon1) != (lon > lon2)) and (lat < (lat2 - lat1) * (lon - lon1) / (lon2 - lon1) + lat1):
                inside = not inside

        return inside

    def filter_alerts(self, alerts, area=None, event=None, severity=None, certainty=None, urgency=None, latitude=None, longitude=None):
        # Filter alerts by area, event, severity, certainty, and urgency
        result = []
        for alert in alerts:
            if area:
                areaDesc = alert.get("areaDesc", [])
                areas = areaDesc.split("|")
                match = False
                for check_area in areas:
                    if area and re.search(area.lower(), check_area.lower()):
                        match = True
                if not match:
                    continue
            if event and not re.search(event.lower(), alert.get("event", "").lower()):
                continue
            if severity and not re.search(severity.lower(), alert.get("severity", "").lower()):
                continue
            if certainty and not re.search(certainty.lower(), alert.get("certainty", "").lower()):
                continue
            if urgency and not re.search(urgency.lower(), alert.get("urgency", "").lower()):
                continue

            if latitude and longitude:
                polygon_text = alert.get("polygon", "")
                polygon = []

                # Polygon is a list of lat/lon pairs
                if polygon_text:
                    polygon_arr = polygon_text.split()
                    for point in polygon_arr:
                        try:
                            lat, lon = point.split(",")
                            polygon.append((float(lat), float(lon)))
                        except (ValueError, TypeError):
                            pass

                # Check if the alert is relevant to our location
                if polygon:
                    # Check if our location is within the polygon
                    if not self.is_point_in_polygon(latitude, longitude, polygon):
                        continue

            result.append(alert)
        return result

    def download_alert_data(self, url):
        """
        Download octopus free session data directly from a URL
        """
        # Check the cache first
        now = datetime.now()
        if url in self.alert_cache:
            stamp = self.alert_cache[url]["stamp"]
            pdata = self.alert_cache[url]["data"]
            age = now - stamp
            if age.seconds < (30 * 60):
                self.log("AlertFeed: Return cached alert data for {} age {} minutes".format(url, dp1(age.seconds / 60)))
                self.last_success_timestamp = datetime.now(timezone.utc)
                return pdata

        r = requests.get(url)
        if r.status_code not in [200, 201]:
            self.log("Warn: AlertFeed: Error downloading Octopus data from URL {}, code {}".format(url, r.status_code))
            self.base.record_status("Warn: AlertFeed: Error downloading Octopus free session data", debug=url, had_errors=True)
            return None

        self.log("AlertFeed: Downloaded alert data from {} size {} bytes".format(url, len(r.text)))

        # Return new data
        self.alert_cache[url] = {}
        self.alert_cache[url]["stamp"] = now
        self.alert_cache[url]["data"] = r.text
        self.last_success_timestamp = datetime.now(timezone.utc)
        return r.text

    def parse_alert_data(self, xml):
        """
        Parse the alert data from the XML
        """
        namespace = "{http://www.w3.org/2005/Atom}"
        namespace2 = "{urn:oasis:names:tc:emergency:cap:1.2}"
        alerts = []
        root = None
        try:
            root = etree.fromstring(xml)
        except Exception as e:
            self.log("Warn: Failed to extract alerts from xml data exception: {}".format(e))

        if root:
            for entry in root:
                alert = {}
                if entry.tag == f"{namespace}entry":
                    for child in entry:
                        tag_name = child.tag.replace(namespace, "").replace(namespace2, "")
                        tag_value = child.text
                        if tag_name in ["effective", "expires", "onset", "sent", "published", "updated"]:
                            try:
                                tag_value = str2time(tag_value)
                            except (ValueError, TypeError):
                                tag_value = None
                        alert[tag_name] = tag_value
                    alerts.append(alert)
        return alerts
