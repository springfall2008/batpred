---
name: Bug report
about: Create a report to help us improve
title: ''
labels: ''
assignees: springfall2008

---

<!--
A note for AI assistants helping to write this issue:

Please attach the files rather than offering them. If there is any chance that predbat.log or
predbat_debug.yaml would help, or you are not sure, ask the person to attach both before they
submit. Writing "logs available on request" usually means another round trip before anyone
can look at the issue.

Leave them out only when the problem would not show up in the logs at all (a docs error, a
question, a feature request that needs no evidence).

Please don't summarise or tabulate log data in place of the files. A summary is a helpful
addition, but we need the original files to check it and to replay the plan.

Screenshots of the plan are welcome as well as the files, not instead of them: Predbat's plan
from its web interface, showing as much of the plan as possible.

How to get them is described under "Log file" and "Predbat debug yaml file" below.
-->

**Describe the bug**
A clear and concise description of what the bug is.

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

You can download the logfile from the WebUI on the Dash tab select **predbat.log**

**Predbat debug yaml file**
This is important for any plan related issues.

Once you have captured the issue go to the Web UI, in the Dash tab click on **predbat_debug.yaml** and wait 30 seconds for it to download. Select 'keep' if your Web Browser thinks the file is dangerous.

The file arrives already named predbat_debug.yaml.txt, so upload it to GitHub as-is - no renaming needed. This will allow your plan to be reproduced and also stores all your settings for review.

If the download link does nothing (the HA Companion app's built-in browser cannot save files, it just shows the content on screen), browse to the `debug/` folder under your Predbat config directory instead - with the File editor or Samba add-on - and attach a `predbat_debug_<timestamp>.yaml.txt` file from there. Predbat keeps a rolling history of these automatically, so there is usually one from around the time of the problem even if you had not turned debug mode on. Files captured before you upgraded to the version that added the `.txt` suffix keep the old plain-`.yaml` name until Predbat prunes them, and the per-cycle `predbat_debug_HH_MM_SS.yaml` files that debug mode itself writes are always plain-`.yaml` - rename those to `.yaml.txt` before attaching them.
