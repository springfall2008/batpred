# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
from datetime import datetime, timedelta
from alertfeed import AlertFeed
import json

def test_alert_feed(my_predbat):
    """
    Test the alert feed
    """
    failed = 0
    ha = my_predbat.ha_interface
    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    tz_offset = int(my_predbat.midnight_utc.tzinfo.utcoffset(my_predbat.midnight_utc).total_seconds() / 3600)
    tz_offset = f"{tz_offset:02d}"

    birmingham = [52.4823, -1.8900]
    bristol = [51.4545, -2.5879]
    manchester = [53.4808, -2.2426]
    fife = [56.2082, -3.1495]

    alert_data = f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:cap="urn:oasis:names:tc:emergency:cap:1.2">
  <link href="https://pubsubhubbub.appspot.com/" rel="hub"/>
  <link href="https://feeds.meteoalarm.org/feeds/meteoalarm-legacy-atom-united-kingdom" rel="self" type="application/atom+xml"/>
  <link href="https://meteoalarm.org" rel="alternate" type="text/html"/>
  <rights>Copyright © 2025 MeteoAlarm.Org. Licensed under terms equivalent to CC BY 4.0, with additional requirements for redistributing outlined in our Terms and Conditions.</rights>
  <generator>MeteoAlarm Producer Server</generator>
  <logo>https://feeds.meteoalarm.org/images/logo.svg</logo>
  <author>
    <name>meteoalarm.org</name>
    <uri>https://meteoalarm.org</uri>
    <email>meteoalarm@geosphere.at</email>
  </author>
  <id>tag:meteoalarm.org,2021-02-19:UK</id>
  <title>MeteoAlarm - Alerting Europe for Extreme Weather</title>
  <updated>2025-01-24T18:07:55.906349Z</updated>
  <entry>
    <cap:polygon>56.3439,-7.4487 55.9892,-7.1082 55.5659,-6.8445 55.3573,-6.8774 55.2572,-7.0367 55.1569,-7.1466 55.1161,-7.3718 55.0768,-7.4625 55.0217,-7.5064 54.9287,-7.5586 54.8402,-7.6245 54.7991,-7.6959 54.7959,-7.8113 54.788,-7.9019 54.7595,-7.9623 54.7183,-7.9926 54.6659,-7.9926 54.6246,-7.9953 54.4956,-7.207 54.4892,-6.4929 54.355,-5.7623 54.0658,-4.8999 53.5468,-3.1311 53.7032,-2.2302 53.927,-1.7523 54.2396,-0.5383 54.3742,-0.2307 54.4956,-0.2637 54.6421,-0.4724 54.8133,-0.8459 55.0091,-1.0547 55.3541,-1.1865 55.6528,-1.2964 55.8691,-1.4502 56.1149,-1.8457 56.3043,-2.0215 56.4989,-2.0544 56.8009,-1.9885 57.0766,-1.8018 57.3087,-1.582 57.5099,-1.5271 57.6454,-1.571 57.7687,-1.7084 57.8097,-1.9336 57.8184,-2.2028 57.7921,-2.7081 57.7921,-3.0817 57.8331,-3.3069 57.8973,-3.4277 57.9732,-3.4662 58.0692,-3.3838 58.2546,-3.0267 58.5224,-2.6093 58.9273,-1.9995 59.3612,-1.3623 59.7841,-0.835 60.1634,-0.5328 60.6301,-0.3571 60.8155,-0.401 60.9224,-0.5328 60.9758,-0.7471 60.9758,-1.1096 60.8556,-1.5875 60.6058,-2.1094 60.3568,-2.428 59.7896,-3.0322 59.322,-3.6584 59.0179,-4.4495 58.8535,-5.4822 58.6826,-6.5479 58.4937,-7.2729 58.3153,-7.6904 58.0023,-8.053 57.5571,-8.2288 57.172,-8.1738 56.903,-8.0859 56.5353,-7.6904 56.3439,-7.4487</cap:polygon>
    <link href="https://meteoalarm.org?polygon=3ec5a08b-995f-45fc-88eb-d364b1613e41,0,0,0" hreflang="en" title="Central, Tayside &amp; Fife | Grampian | Highlands &amp; Eilean Siar | North East England | North West England | Northern Ireland | Orkney &amp; Shetland | Strathclyde | SW Scotland, Lothian Borders | Yorkshire &amp; Humber"/>
    <cap:areaDesc>Central, Tayside &amp; Fife | Grampian | Highlands &amp; Eilean Siar | North East England | North West England | Northern Ireland | Orkney &amp; Shetland | Strathclyde | SW Scotland, Lothian Borders | Yorkshire &amp; Humber</cap:areaDesc>
    <cap:event>Yellow wind warning</cap:event>
    <cap:sent>2025-01-24T18:01:26+{tz_offset}:00</cap:sent>
    <cap:expires>{today}T23:59:59+{tz_offset}:00</cap:expires>
    <cap:effective>{yesterday}T10:40:36+{tz_offset}:00</cap:effective>
    <cap:onset>{today}T00:00:00+{tz_offset}:00</cap:onset>
    <cap:certainty>Possible</cap:certainty>
    <cap:severity>Moderate</cap:severity>
    <cap:urgency>Immediate</cap:urgency>
    <cap:scope>Public</cap:scope>
    <cap:message_type>Update</cap:message_type>
    <cap:status>Actual</cap:status>
    <cap:identifier>2.49.0.0.826.0.GB_250124180126_cecc0a37.v6.0.W</cap:identifier>
    <link href="https://feeds.meteoalarm.org/api/v1/warnings/feeds-united-kingdom/3ec5a08b-995f-45fc-88eb-d364b1613e41" type="application/cap+xml"/>
    <link href="https://meteoalarm.org?region=UK" hreflang="en" rel="related" title="United Kingdom"/>
    <author>
      <name>meteoalarm.org</name>
      <uri>https://meteoalarm.org</uri>
    </author>
    <published>2025-01-24T18:01:26Z</published>
    <id>https://feeds.meteoalarm.org/api/v1/warnings/feeds-united-kingdom/3ec5a08b-995f-45fc-88eb-d364b1613e41?index_info=0&amp;index_area=0&amp;index_polygon=0</id>
    <title>Yellow Wind Warning issued for United Kingdom - Central, Tayside &amp; Fife | Grampian | Highlands &amp; Eilean Siar | North East England | North West England | Northern Ireland | Orkney &amp; Shetland | Strathclyde | SW Scotland, Lothian Borders | Yorkshire &amp; Humber</title>
    <updated>2025-01-24T18:01:26Z</updated>
  </entry>
<entry>
    <cap:polygon>60.877,-1.4282 59.4674,-3.23 59.249,-3.7573 58.95,-4.4275 58.9103,-4.8395 58.4618,-6.7053 57.7687,-7.8949 56.7768,-7.8333 56.7407,-7.3059 56.7557,-6.7841 56.7768,-6.4545 56.7286,-6.1908 56.6139,-5.9601 56.4534,-5.5872 56.3653,-5.4108 56.2769,-5.1306 56.2586,-5.0153 56.2403,-4.8395 56.2189,-4.3671 56.222,-4.2462 56.2525,-4.0869 56.2678,-3.8892 56.283,-3.8068 56.3835,-3.4113 56.5776,-2.9169 56.6562,-2.774 56.6894,-2.7301 56.8099,-2.3816 56.8079,-2.1936 57.1184,-1.8896 57.4509,-1.593 57.7218,-1.8018 57.7628,-3.3838 57.9906,-3.6035 58.3499,-2.9114 59.2996,-1.4612 60.5222,-0.4834 60.9411,-0.7031 60.877,-1.4282</cap:polygon>
    <link href="https://meteoalarm.org?polygon=05f2a1ec-58ec-4b6e-b05b-21ddac714680,0,0,0" hreflang="en" title="Central, Tayside &amp; Fife | Grampian | Highlands &amp; Eilean Siar | Orkney &amp; Shetland | Strathclyde"/>
    <cap:areaDesc>Central, Tayside &amp; Fife | Grampian | Highlands &amp; Eilean Siar | Orkney &amp; Shetland | Strathclyde</cap:areaDesc>
    <cap:event>Amber wind warning</cap:event>
    <cap:sent>2025-01-23T10:42:05+{tz_offset}:00</cap:sent>
    <cap:expires>{tomorrow}T06:00:00+{tz_offset}:00</cap:expires>
    <cap:effective>{yesterday}T10:42:05+{tz_offset}:00</cap:effective>
    <cap:onset>{today}T13:00:00+{tz_offset}:00</cap:onset>
    <cap:certainty>Likely</cap:certainty>
    <cap:severity>Severe</cap:severity>
    <cap:urgency>Future</cap:urgency>
    <cap:scope>Public</cap:scope>
    <cap:message_type>Alert</cap:message_type>
    <cap:status>Actual</cap:status>
    <cap:identifier>2.49.0.0.826.0.GB_250123104205_72085267.v1.0.W</cap:identifier>
    <link href="https://feeds.meteoalarm.org/api/v1/warnings/feeds-united-kingdom/05f2a1ec-58ec-4b6e-b05b-21ddac714680" type="application/cap+xml"/>
    <link href="https://meteoalarm.org?region=UK" hreflang="en" rel="related" title="United Kingdom"/>
    <author>
      <name>meteoalarm.org</name>
      <uri>https://meteoalarm.org</uri>
    </author>
    <published>2025-01-23T10:42:05Z</published>
    <id>https://feeds.meteoalarm.org/api/v1/warnings/feeds-united-kingdom/05f2a1ec-58ec-4b6e-b05b-21ddac714680?index_info=0&amp;index_area=0&amp;index_polygon=0</id>
    <title>Orange Wind Warning issued for United Kingdom - Central, Tayside &amp; Fife | Grampian | Highlands &amp; Eilean Siar | Orkney &amp; Shetland | Strathclyde</title>
    <updated>2025-01-23T10:42:05Z</updated>
  </entry>
</feed>
"""
    print("Test alert feed")

    alert_feed = AlertFeed(my_predbat, alert_config={})

    result = alert_feed.parse_alert_data(alert_data)
    if not result:
        print("ERROR: Could not parse stored alert data")
        failed = 1
        return failed

    filter = alert_feed.filter_alerts(result, area="North West England")
    if len(filter) != 1:
        print("ERROR: Expecting 1 alert for North West England got {}".format(len(filter)))
        failed = 1
        return failed

    filter = alert_feed.filter_alerts(result, area="South West England")
    if len(filter) != 0:
        print("ERROR: Expecting 0 alert for South West England got {}".format(len(filter)))
        failed = 1
        return failed

    filter = alert_feed.filter_alerts(result, latitude=birmingham[0], longitude=birmingham[1])
    if len(filter) != 0:
        print("ERROR: Expecting 0 alert for Birmingham got {}".format(len(filter)))
        failed = 1
        return failed

    filter = alert_feed.filter_alerts(result, latitude=fife[0], longitude=fife[1])
    if len(filter) != 1:
        print("ERROR: Expecting 1 alert for Fife got {}".format(len(filter)))
        failed = 1
        return failed

    filter = alert_feed.filter_alerts(result, area="Grampian", severity="Moderate|Severe", certainty="Likely")
    if len(filter) != 1:
        print("ERROR: Expecting 1 alert for Grampian got {}".format(len(filter)))
        failed = 1
        return failed

    filter = alert_feed.filter_alerts(result, event="(Amber|Yellow|Orange|Red).*(Wind|Snow|Fog|Thunderstorm|Avalanche|Frost|Heat|Coastal event|Flood|Forestfire|Ice|Low temperature|Storm|Tornado|Tsunami|Volcano|Wildfire)")
    if len(filter) != 2:
        print("ERROR: Expecting 2 alerts for Yellow|Amber but got {}".format(len(filter)))
        failed = 1
        return failed

    alert_active_keep = alert_feed.apply_alerts(result, 1.0, my_predbat.minutes_now, my_predbat.midnight_utc)
    show = []
    for minute in range(0, 48 * 60, 15):
        show.append(alert_active_keep.get(minute, 0))
    expect_show = [
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
    ]
    if json.dumps(show) != json.dumps(expect_show):
        print("ERROR: Expecting show should be {} got {}".format(expect_show, show))
        failed = 1

    url = "https://feeds.meteoalarm.org/feeds/meteoalarm-legacy-atom-united-kingdom"
    xml = alert_feed.download_alert_data(url)
    if not xml:
        print("ERROR: Could not download alert data")
        failed = 1
        return failed

    alert_config = {
        "url": url,
        "area": "North West England",
        "event": "Yellow|Amber",
        "keep": 0.5,
    }
    original_download_alert_data = alert_feed.download_alert_data
    alert_feed.alert_config = alert_config
    alert_feed.alert_xml = alert_data
    alerts, alert_active_keep = alert_feed.process_alerts(my_predbat.minutes_now, my_predbat.midnight_utc, testing=True)
    alert_active_keep = alert_active_keep
    show = []
    for minute in range(0, 48 * 60, 15):
        show.append(alert_active_keep.get(minute, 0))

    expect_show = [
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
    ]
    if json.dumps(show) != json.dumps(expect_show):
        print("ERROR: Expecting show should be {} got {}".format(expect_show, show))
        failed = 1

    alert_text = ha.get_state("sensor." + my_predbat.prefix + "_alertfeed_status")
    expect_text = "Yellow wind warning until " + today + " 23:59:59+{}:00".format(tz_offset)
    if alert_text != expect_text:
        print("ERROR: Expecting alert text to be '{}' got '{}'".format(expect_text, alert_text))
        failed = 1

    return failed
