# Installation summary

Note there are [step by step installation](video-guides.md#basic-installation) videos, see the [video guides](video-guides.md) section for those and other videos.

Please see the sections below for how to achieve each step. This is just a checklist of things:

1. Make sure GivTCP is installed and running - [GivTCP install](install.md#givtcp-install)
2. Install AppDaemon if you haven't already  - [AppDaemon install](install.md#appdaemon-install)
3. Install HACS if you haven't already - [HACS install](install.md#hacs-install)
4. Install Predbat using HACS - [Predbat install](install.md#predbat-install)
5. Install Solcast if you haven't already [Solcast install](install.md#solcast-install)
    - Also check Solcast is being auto-updated a few times a day and that you see the data in Home Assistant
6. If you use Octopus Energy then install the Octopus Energy plugin (if you haven't already)  - [Octopus Energy](install.md#octopus-energy)
    - CAUTION: You need to enable the events for previous, current and next rates for import and export in HA as the plugin leaves them disabled by default (see the above link for details)
8. Go and edit apps.yaml (in config/appdaemon/apps/predbat/config/apps.yaml) to match your system - [apps.yaml settings](config-yml-settings.md)
    - Inverter settings match the names in GivTCP -  should be automatic (but if you have 2 names you will have to edit apps.yml)
        - You have set the right number of inverters (**num_inverters**)
        - Adjust your **inverter_limit** and **export_limit** as required
    - You have your energy rates set correctly either using Octopus Plugin or entered manually
    - That the Solcast plugin is matching the configuration correctly - should be automatic
    - If you have a car charging sensor you might want to add that also to help make predictions more accurate
    - Then check the AppDaemon log file and make sure you have no errors or warnings that are unexpected
    - And check **predbat.status** in Home Assistant to check it's now Idle (errors are reported here too)
9. Add the Predbat entities to your dashboard  - [Output data](output-data.md)
10. Follow the configuration guide to tune things for your system  - [Configuration guide](configuration-guide.md)
11. Set up the Apex Charts so you can check what Predbat is doing - [Creating the charts](creating-charts.md)
12. Set up the Predbat Plan card for another view on what Predbat is doing - [Create the Predbat Plan card](predbat-plan-card.md)
13. Look at the [FAQ](faq.md) and [Video Guides](video-guides.md) for help

Overview of the key configuration elements:

![image](https://github.com/springfall2008/batpred/assets/48591903/7c9350e0-2b6d-49aa-8f61-93d0547ae6d0)
