# Installation summary

Please see the sections below for how to achieve each step. This is just a checklist of things:

1. Before you start, its recommended that you watch the [step by step installation](video-guides.md#basic-installation) videos,
see the [video guides](video-guides.md) section for those and other videos
2. Make sure the right inverter control module is installed and running - [GivTCP or Solax install](install.md#inverter-control-integration-install-givtcpsolax-modbus)
3. Install a file editor (either the File editor or Studio Code Server add-on) to enable you to edit configuration files if you haven't already - [Editing configuration files](install.md#editing-configuration-files-in-home-assistant)
4. Install Predbat, either:
    - a) Install the Predbat add-on - [Predbat add-on install](install.md#predbat-add-on-install)
    - b) Install the AppDaemon-Predbat combined add-on - [AppDaemon-Predbat combined install](install.md#appdaemon-predbat-combined-install)
    - c) Complete the separate installation steps:
        - i) Install HACS if you haven't already - [HACS install](install.md#hacs-install)
        - ii) Install AppDaemon if you haven't already - [AppDaemon install](install.md#appdaemon-install)
        - iii) Install Predbat using HACS - [Predbat install](install.md#install-predbat-through-hacs)
5. Install Solcast if you haven't already [Solcast install](install.md#solcast-install).
If using the Solcast integration then create an automation to update Solcast a times a day, and check that you see the Solcast data in Home Assistant
6. Follow the [Energy Rates](energy-rates.md) instructions to tell Predbat what your import and export energy rates are.
If you use Octopus Energy then this includes installing the Octopus Energy integration (if you haven't already) - [Octopus Energy](energy-rates.md#octopus-energy-integration)
7. Edit Predbat's `apps.yaml` configuration file to to match your system - [apps.yaml settings](apps-yaml.md)<BR>
The apps.yaml file will be in either the directory `/addon_configs/46f69597_appdaemon-predbat/apps`
or `/config/appdaemon/apps/predbat/config/` depending on which Predbat install method you used.
    - Inverter settings match the names in GivTCP - should be automatic (but if you have 2 names you will have to edit apps.yaml)
        - You have set the right number of inverters (**num_inverters**)
        - Adjust your **inverter_limit** and **export_limit** as required
    - You have your energy rates set correctly either using Octopus Energy integration or entered manually
    - That the Solcast plugin is matching the configuration correctly - should be automatic
    - If you have a car charging sensor you might want to add that also to help make predictions more accurate
8. Add the Predbat entities to your dashboard - [Output data](output-data.md)
9. Follow the [Configuration Guide](configuration-guide.md) to set 'standard' Predbat configuration settings depending on your import and export tariffs
10. The detailed [Customisation Guide](customisation.md) lists all Predbat's controls and settings in Home Assistant that can be tuned for your system
11. Set up the Predbat Plan card so you can check what Predbat is planning to do - [Create the Predbat Plan card](predbat-plan-card.md)
12. Set up the Apex Charts for other views on what Predbat is doing - [Creating the charts](creating-charts.md)
13. Then check Predbat is working correctly:
    - Look at the [Predbat AppDaemon log file](output-data.md#predbat-logfile) and make sure you have no errors or warnings that are unexpected
    - Comment out or delete the [template: True line in apps.yaml](apps-yaml.md#basics) when you are ready to start Predbat
    - The **predbat.status** in Home Assistant should be 'Idle' (if there are any errors then they are reported here too)
    - Start with **select.predbat_mode** set to 'Monitor' but remember to change it later to enable Predbat to control your inverter
14. Look at the [FAQ](faq.md) for help

Overview of the key configuration elements:

![image](https://github.com/springfall2008/batpred/assets/48591903/7c9350e0-2b6d-49aa-8f61-93d0547ae6d0)
