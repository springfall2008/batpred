# Step by step guide

Note there is a step by step guide video, see the video guides section below for a link. 

Please see the sections below for how to achieve each step. This is just a checklist of things:

1. Make sure GivTCP is installed and running - [GivTCP install](install.md#givtcp-install)
2. Install AppDaemon if you haven't already  - [AppDaemon install](install.md#appdaemon-install)
3. Install HACS if you haven't already - [HACS install](install.md#hacs-install)
4. Install Predbat using HACS - [Predbat install](install.md#predbat-install)
5. Install Solcast if you haven't already [Solcast install](install.md#solcast-install)
    - Also check Solcast is being auto-updated a few times a day and that you see the data in Home Assistant
6. If you use Octopus Energy then install the Octopus Energy plugin (if you haven't already)  - [Octopus Energy](install.md#octopus-energy)
7. Go and edit apps.yml (in config/appdaemon/apps/predbat/config/apps.yml) to match your system - [config.yml settings](config-yml-settings.md)
    - Inverter settings match the names in GivTCP -  should be automatic but if you have _2 names you will have to edit apps.yml)
        - You have set the right number of inverters (**num_inverters**)
        - Adjust your **inverter_limit** and **export_limit** as required
    - You have your energy rates set correctly either using Octopus Plugin or entered manually
    - That the Solcast plugin is matching the configuration correctly - should be automatic
    - If you have a car charging sensor you might want to add that also to help make predictions more accurate
    - Then check the AppDaemon log file and make sure you have no errors or warnings that are unexpected
    - And check **predbat.status** in Home Assistant to check it's now Idle (errors are reported here too)
8. Add the Predbat entities to your dashboard  - [Output data](output-data.md)
9. Follow the configuration guide to tune things for your system  - [Configuration guide](configuration-guide.md)
10. Set up the Apex Charts so you can check what Predbat is doing - [Creating the charts](creating-charts.md)
11. Set up the Predbat Plan card for another view on what Predbat is doing - [Create the Predbat Plan card](predbat-plan-card.md)
12. Look at the [FAQ](faq.md) and [Video Guides](video-guides.md) for help

Overview of the key configuration elements:

![image](https://github.com/springfall2008/batpred/assets/48591903/7c9350e0-2b6d-49aa-8f61-93d0547ae6d0)

