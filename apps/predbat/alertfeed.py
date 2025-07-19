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
from datetime import datetime
from utils import str2time, dp1
import xml.etree.ElementTree as etree


class Alertfeed:
    def process_alerts(self, testing=False):
        """
        Process the alerts from the alert feed
        """

        self.alerts = []
        self.alert_active_keep = {}

        alerts = self.get_arg("alerts", {})
        if not alerts:
            return
        if not isinstance(alerts, dict):
            self.log("Warn: Alerts must be a dictionary, ignoring")
            return

        # Try apps.yaml
        latitude = alerts.get("latitude", None)
        longitude = alerts.get("longitude", None)

        # If latitude and longitude are not provided, use zone.home
        if latitude is None:
            latitude = self.get_state_wrapper("zone.home", attribute="latitude")
        if longitude is None:
            longitude = self.get_state_wrapper("zone.home", attribute="longitude")

        # If latitude and longitude are not found, we cannot process alerts
        if latitude and longitude:
            self.log("Processing alerts for approx position latitude {} longitude {}".format(dp1(latitude), dp1(longitude)))
        else:
            if not testing:
                self.log("Warn: No latitude or longitude found, cannot process alerts")
                return

        alert_url = alerts.get("url", "https://feeds.meteoalarm.org/feeds/meteoalarm-legacy-atom-united-kingdom")
        area = alerts.get("area", "")
        event = alerts.get("event", "")
        severity = alerts.get("severity", "")
        certainty = alerts.get("certainty", "")
        urgency = alerts.get("urgency", "")
        keep = alerts.get("keep", 100)

        alert_xml = self.download_alert_data(alert_url)
        if alert_xml:
            self.alerts = self.parse_alert_data(alert_xml)
            self.alerts = self.filter_alerts(self.alerts, area, event, severity, certainty, urgency, latitude, longitude)
            self.alert_active_keep = self.apply_alerts(self.alerts, keep)

    def apply_alerts(self, alerts, keep):
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
                    onset_minutes = int((onset - self.midnight_utc).total_seconds() / 60)
                    expires_minutes = int((expires - self.midnight_utc).total_seconds() / 60)
                    if expires_minutes >= self.minutes_now:
                        self.log("Info: Active alert: {} severity {} certainty {} urgency {} from {} to {} applying keep {}".format(alert.get("event"), severity, certainty, urgency, onset, expires, keep))
                        for minute in range(onset_minutes, expires_minutes):
                            if minute not in alert_active_keep:
                                alert_active_keep[minute] = keep
                            else:
                                alert_active_keep[minute] = max(alert_active_keep[minute], keep)
                            if minute == self.minutes_now:
                                active_alert_text = alert.get("event") + " until " + str(expires)
                                active_alert = True

        alert_keep = alert_active_keep.get(self.minutes_now, 0)
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
        self.dashboard_item(self.prefix + ".alerts", state=active_alert_text, attributes={"friendly_name": "Weather alerts", "icon": "mdi:alert-outline", "keep": alert_keep, "alerts": alert_show})

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
                self.log("Return cached alert data for {} age {} minutes".format(url, dp1(age.seconds / 60)))
                return pdata

        r = requests.get(url)
        if r.status_code not in [200, 201]:
            self.log("Warn: Error downloading Octopus data from URL {}, code {}".format(url, r.status_code))
            self.record_status("Warn: Error downloading Octopus free session data", debug=url, had_errors=True)
            return None

        # Return new data
        self.alert_cache[url] = {}
        self.alert_cache[url]["stamp"] = now
        self.alert_cache[url]["data"] = r.text
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
