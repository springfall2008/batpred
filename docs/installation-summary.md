# Installation summary

Note there are [step by step installation](video-guides.md#basic-installation) videos, see the [video guides](video-guides.md) section for those and other videos.

Please see the sections below for how to achieve each step. This is just a checklist of things:

1. Before you start, its recommended that you watch the [Video Guides](video-guides.md)
2. Make sure the right inverter control module is installed and running - [GivTCP install](install.md#inverter-control-integration-install-givtcpsolax-modbus)
3. Install AppDaemon if you haven't already  - [AppDaemon install](install.md#appdaemon-install)
4. Install HACS if you haven't already - [HACS install](install.md#hacs-install)
5. Install Predbat using HACS - [Predbat install](install.md#install-predbat-through-hacs)
6. Install Solcast if you haven't already [Solcast install](install.md#solcast-install)
    - Also check Solcast is being auto-updated a few times a day and that you see the data in Home Assistant
7. Follow the [Energy Rates](energy-rates.md) to tell Predbat what your import and export energy rates are.
If you use Octopus Energy then this includes installing the Octopus Energy integration (if you haven't already) - [Octopus Energy](energy-rates.md#octopus-energy-integration)
8. Go and edit apps.yaml (in /config/appdaemon/apps/predbat/config/apps.yaml) to match your system - [apps.yaml settings](apps-yaml.md)
    - Inverter settings match the names in GivTCP - should be automatic (but if you have 2 names you will have to edit apps.yaml)
        - You have set the right number of inverters (**num_inverters**)
        - Adjust your **inverter_limit** and **export_limit** as required
    - You have your energy rates set correctly either using Octopus Energy integration or entered manually
    - That the Solcast plugin is matching the configuration correctly - should be automatic
    - If you have a car charging sensor you might want to add that also to help make predictions more accurate
9. Add the Predbat entities to your dashboard - [Output data](output-data.md)
10. Follow the configuration guide to tune things for your system  - [Configuration guide](configuration-guide.md)
11. Set up the Apex Charts so you can check what Predbat is doing - [Creating the charts](creating-charts.md)
12. Set up the Predbat Plan card for another view on what Predbat is doing - [Create the Predbat Plan card](predbat-plan-card.md)
13. Then check Predbat is working correctly:
    - Look at the AppDaemon log file and make sure you have no errors or warnings that are unexpected
    - Comment out or delete the [template: True line in apps.yaml](apps-yaml.md#basics) when you are ready to start Predbat
    - The **predbat.status** in Home Assistant should be Idle (errors are reported here too)
    - Start with **select.predbat_mode** as Monitor but remember to change it later to enable Predbat to control your inverter
14. Look at the [FAQ](faq.md) for help

Overview of the key configuration elements:

![image](https://github.com/springfall2008/batpred/assets/48591903/7c9350e0-2b6d-49aa-8f61-93d0547ae6d0)
