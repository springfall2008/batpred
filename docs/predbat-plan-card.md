# Predbat Plan card

Predbat can create its own plan card which can be added to your HA dashboard.

* First install html-template-card in HACS (from the Frontend list), it will already be available to add, but for reference the repository can be found here:
    * <https://github.com/PiotrMachowski/Home-Assistant-Lovelace-HTML-Jinja2-Template-card>

Next create a new card as follows:

```yaml
type: custom:html-template-card
title: Predbat plan
ignore_line_breaks: true
content: |
  {{ state_attr('predbat.plan_html', 'html') }}
```

You should see something like this:

![image](https://github.com/springfall2008/batpred/assets/48591903/3c0a2a53-4d83-4b64-aa49-822a233f7554)
