# batpred
Home battery prediction for Home Assistant with GivTCP

To install:

- You must have GivTCP installed and running first
- Install AppDeamon add-on https://github.com/hassio-addons/addon-appdaemon
   - In python packages (in the config) add 'tzlocal'

- Copy predbat.py to 'config/appdeamon/apps' directory in home assistant
- Edit config/appdemon/apps.yml and put into it the contents of apps.yml, but change the entity names to match your own inverter serial number
- Customise any settings needed

To create the fancy chart 
- Install apex charts
- Create a new apexcharts card and copy the YML from below into the chart settings, updating the serial number to match your inverter
- Customeise

type: custom:apexcharts-card
header:
  show: true
  title: Home Battery Prediction
  show_states: true
  colorize_states: true
graph_span: 36h
span:
  start: minute
  offset: '-12h'
now:
  show: true
series:
  - entity: sensor.givtcp_sa2243g277_soc_kwh
    stroke_width: 1
    curve: smooth
    name: actual
  - entity: predbat.soc_kw
    stroke_width: 1
    curve: smooth
    name: predicted
    data_generator: >
      let res = []; for (const [key, value] of
      Object.entries(entity.attributes.results)) { res.push([new
      Date(key).getTime(), value]); } return res.sort((a, b) => { return a[0] -
      b[0]  })
