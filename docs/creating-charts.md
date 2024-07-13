# Creating the charts

There are a number of fancy Apex charts that can be produced from Predbat data - things like Home Battery SoC prediction, Cost prediction, Energy Rates, etc.

There's a [Video Guide to the different charts](https://www.youtube.com/watch?v=KXiRzMyRZyM) available on YouTube.

To install the charts:

- Install Apex Charts [https://github.com/RomRider/apexcharts-card](https://github.com/RomRider/apexcharts-card):
    - In HACS, click on Frontend
    - Click the blue *Explore and download repositories* button and type 'apex'
    - Click on 'apexcharts-card', then click the blue 'Download', then 'Download' again to install it
    - When prompted to 'reload your browser', click 'Reload'

- Next, on a Home Assistant dashboard you create the charts you want.
- There are multiple charts, for each section of the
[example chart yaml file](https://raw.githubusercontent.com/springfall2008/batpred/main/templates/example_chart.yml),
create a new apexcharts card and copy the YAML into it:
    - Click the blue 'Add card', scroll down the list of cards to the bottom and click 'Custom: ApexCharts card'
    - Delete the template card configuration and copy/paste ONE Apex chart from the example file
    - Click 'Save'
    - Repeat (adding Apex chart card, copy paste card config) for all of the charts you want to see
- Customise as you like

If you get an error 'Custom element doesn't exist: apexcharts-card' then you've not installed the Apex Charts card correctly from HACS.

See the [video guides](video-guides.md) for a walkthrough of what the different charts show.

Example charts:

![image](https://github.com/springfall2008/batpred/assets/48591903/28f29756-2502-4079-9c75-398e8a1a0699)

![image](https://github.com/springfall2008/batpred/assets/48591903/4c3df49c-52e5-443f-b9c5-7a673c96b205)

![image](https://github.com/springfall2008/batpred/assets/48591903/5f1f504d-9251-4610-9403-2a5f4d0bf332)

![image](https://github.com/springfall2008/batpred/assets/48591903/c02d65cf-e502-4484-a58d-cff8fb93d0f3)

![image](https://github.com/springfall2008/batpred/assets/48591903/a96934d3-753a-49da-800b-925896f87cb6)

![image](https://github.com/springfall2008/batpred/assets/48591903/0dfb7c90-c4b5-455b-9510-44ecfc86f12c)

![image](https://github.com/springfall2008/batpred/assets/48591903/cb52d4cd-8424-4080-93c2-8a58743bcd7a)

![image](https://github.com/springfall2008/batpred/assets/48591903/28e79e42-84df-4a5d-a078-7b6261a8fd1b)

![image](https://github.com/springfall2008/batpred/assets/48591903/ff1c1e1e-e43c-46a5-95a9-75f631425422)
