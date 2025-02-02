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
from datetime import datetime, timedelta
from config import TIME_FORMAT, TIME_FORMAT_OCTOPUS
from utils import str2time, minutes_to_time, dp1, dp2, dp4
import xml.etree.ElementTree as etree

class Alertfeed:
    # Example URL https://feeds.meteoalarm.org/feeds/meteoalarm-legacy-atom-united-kingdom
    # Data in XML format
    def download_alerts(url):
        self.alerts = []
        try:
            response = requests.get(url)
            if response.status_code == 200:
                # Decode XML
                xml = response.text
                # Extract alerts
                root = etree.fromstring(xml)
                print(root)
        except:
            self.log("Warn: Failed to download alerts from {}".format(url))
