---
name: Bug report
about: Report bugs in Predbat core functionality (NOT add-on installation issues)
title: ''
labels: ''
assignees: springfall2008

---

**⚠️ IMPORTANT: Is this an add-on installation or Docker issue?**

If you are experiencing issues with:
- Installing the Predbat Home Assistant Add-on
- Docker build failures or container errors
- Add-on configuration problems
- `ha supervisor logs` errors

**Please file your issue in the correct repository:**
- **Predbat Add-on issues**: <https://github.com/springfall2008/predbat_addon/issues>
- **Docker installation issues**: <https://github.com/nipar4/predbat_addon/issues>

---

**Describe the bug**
A clear and concise description of what the bug is (related to Predbat's prediction, planning, or control functionality).

**Expected behaviour**
A clear and concise description of what you expected to happen.

**Predbat version**

xxxx

**Environment details**

- Inverter and battery setup
- Standard HAOS installer or Docker
- Anything else?

**Screenshots**
If applicable, add screenshots to help explain your problem. The most useful ones can be your battery chart, the Predbat HTML plan and your current settings in HA.

**Log file**
Can you capture a log file from the time of the issue, debug mode is not normally required.

You can download the logfile from the WebUI on the Dash tab select **predbat_debug.log**

**Predbat debug yaml file**
This is important for any plan related issues.

Once you have captured the issue go to the Web UI, in the Dash tab click on **predbat_debug.yaml** and wait 30 seconds for it to download. Select 'keep' if your Web Browser thinks the file is dangerous.

Rename the download file to predbat_debug.yaml.txt and upload it to GitHub. This will allow your plan to be reproduced and also stores all your settings for review.
