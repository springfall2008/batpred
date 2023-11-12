# %%
lines = open("predbat.py", "r").readlines()

THIS_VERSION = "v7.11.14"
TIME_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
TIME_FORMAT_SECONDS = "%Y-%m-%dT%H:%M:%S.%f%z"
TIME_FORMAT_OCTOPUS = "%Y-%m-%d %H:%M:%S%z"
TIME_FORMAT_SOLIS = "%Y-%m-%d %H:%M:%S"
PREDICT_STEP = 5
RUN_EVERY = 5

# 240v x 100 amps x 3 phases / 1000 to kw / 60 minutes in an hour is the maximum kWh in a 1 minute period
MAX_INCREMENT = 240 * 100 * 3 / 1000 / 60

SIMULATE = False  # Debug option, when set don't write to entities but simulate each 30 min period
SIMULATE_LENGTH = 23 * 60  # How many periods to simulate, set to 0 for just current
INVERTER_TEST = False  # Run inverter control self test

"""
Create an array of times
"""
OPTIONS_TIME = []

CONFIG_ITEMS = [
    {
        "name": "version",
        "friendly_name": "Predbat Core Update",
        "type": "update",
        "title": "Predbat",
        "installed_version": THIS_VERSION,
        "release_url": "https://github.com/springfall2008/batpred/releases/tag/" + THIS_VERSION,
        "entity_picture": "https://user-images.githubusercontent.com/48591903/249456079-e98a0720-d2cf-4b71-94ab-97fe09b3cee1.png",
    },
    {"name": "pv_metric10_weight", "friendly_name": "Metric 10 Weight", "type": "input_number", "min": 0, "max": 1.0, "step": 0.01, "unit": "fraction", "icon": "mdi:percent"},
    {"name": "pv_scaling", "friendly_name": "PV Scaling", "type": "input_number", "min": 0, "max": 2.0, "step": 0.01, "unit": "multiple", "icon": "mdi:multiplication"},
    {"name": "load_scaling", "friendly_name": "Load Scaling", "type": "input_number", "min": 0, "max": 2.0, "step": 0.01, "unit": "multiple", "icon": "mdi:multiplication"},
    {
        "name": "battery_rate_max_scaling",
        "friendly_name": "Battery rate max scaling",
        "type": "input_number",
        "min": 0,
        "max": 1.0,
        "step": 0.01,
        "unit": "multiple",
        "icon": "mdi:multiplication",
    },
    {"name": "battery_loss", "friendly_name": "Battery loss charge ", "type": "input_number", "min": 0, "max": 1.0, "step": 0.01, "unit": "fraction", "icon": "mdi:call-split"},
    {
        "name": "battery_loss_discharge",
        "friendly_name": "Battery loss discharge",
        "type": "input_number",
        "min": 0,
        "max": 1.0,
        "step": 0.01,
        "unit": "fraction",
        "icon": "mdi:call-split",
    },
    {"name": "inverter_loss", "friendly_name": "Inverter Loss", "type": "input_number", "min": 0, "max": 1.0, "step": 0.01, "unit": "fraction", "icon": "mdi:call-split"},
    {"name": "inverter_hybrid", "friendly_name": "Inverter Hybrid", "type": "switch"},
    {"name": "inverter_soc_reset", "friendly_name": "Inverter SOC Reset", "type": "switch"},
    {"name": "battery_capacity_nominal", "friendly_name": "Use the Battery Capacity Nominal size", "type": "switch"},
    {
        "name": "car_charging_energy_scale",
        "friendly_name": "Car charging energy scale",
        "type": "input_number",
        "min": 0,
        "max": 1.0,
        "step": 0.01,
        "unit": "fraction",
        "icon": "mdi:multiplication",
    },
    {
        "name": "car_charging_threshold",
        "friendly_name": "Car charging threshold",
        "type": "input_number",
        "min": 4,
        "max": 8.5,
        "step": 0.10,
        "unit": "kw",
        "icon": "mdi:ev-station",
    },
    {"name": "car_charging_rate", "friendly_name": "Car charging rate", "type": "input_number", "min": 1, "max": 8.5, "step": 0.10, "unit": "kw", "icon": "mdi:ev-station"},
    {"name": "car_charging_loss", "friendly_name": "Car charging loss", "type": "input_number", "min": 0, "max": 1.0, "step": 0.01, "unit": "fraction", "icon": "mdi:call-split"},
    {"name": "best_soc_margin", "friendly_name": "Best SOC Margin", "type": "input_number", "min": 0, "max": 30.0, "step": 0.10, "unit": "kwh", "icon": "mdi:battery-50"},
    {"name": "best_soc_min", "friendly_name": "Best SOC Min", "type": "input_number", "min": 0, "max": 30.0, "step": 0.10, "unit": "kwh", "icon": "mdi:battery-50"},
    {"name": "best_soc_max", "friendly_name": "Best SOC Max", "type": "input_number", "min": 0, "max": 30.0, "step": 0.10, "unit": "kwh", "icon": "mdi:battery-50"},
    {"name": "best_soc_keep", "friendly_name": "Best SOC Keep", "type": "input_number", "min": 0, "max": 30.0, "step": 0.10, "unit": "kwh", "icon": "mdi:battery-50"},
    {"name": "best_soc_step", "friendly_name": "Best SOC Step", "type": "input_number", "min": 0.1, "max": 1.0, "step": 0.05, "unit": "kwh", "icon": "mdi:battery-50"},
    {
        "name": "metric_min_improvement",
        "friendly_name": "Metric Min Improvement",
        "type": "input_number",
        "min": -50,
        "max": 50.0,
        "step": 0.1,
        "unit": "p",
        "icon": "mdi:currency-usd",
    },
    {
        "name": "metric_min_improvement_discharge",
        "friendly_name": "Metric Min Improvement Discharge",
        "type": "input_number",
        "min": -50,
        "max": 50.0,
        "step": 0.1,
        "unit": "p",
        "icon": "mdi:currency-usd",
    },
    {
        "name": "metric_battery_cycle",
        "friendly_name": "Metric Battery Cycle Cost",
        "type": "input_number",
        "min": -50,
        "max": 50.0,
        "step": 0.1,
        "unit": "p/kwh",
        "icon": "mdi:currency-usd",
    },
    {
        "name": "metric_future_rate_offset_import",
        "friendly_name": "Metric Future Rate Offset Import",
        "type": "input_number",
        "min": -50,
        "max": 50.0,
        "step": 0.1,
        "unit": "p/kwh",
        "icon": "mdi:currency-usd",
    },
    {
        "name": "metric_future_rate_offset_export",
        "friendly_name": "Metric Future Rate Offset Export",
        "type": "input_number",
        "min": -50,
        "max": 50.0,
        "step": 0.1,
        "unit": "p/kwh",
        "icon": "mdi:currency-usd",
    },
    {
        "name": "metric_inday_adjust_damping",
        "friendly_name": "In-day adjustment damping factor",
        "type": "input_number",
        "min": 0.5,
        "max": 2.0,
        "step": 0.05,
        "unit": "fraction",
        "icon": "mdi:call-split",
    },
    {
        "name": "metric_octopus_saving_rate",
        "friendly_name": "Octopus Saving session assumed rate",
        "type": "input_number",
        "min": 1,
        "max": 500,
        "step": 5,
        "unit": "fraction",
        "icon": "mdi:currency-usd",
    },
    {"name": "metric_cloud_enable", "friendly_name": "Simulate clouds (beta)", "type": "switch"},
    {
        "name": "set_window_minutes",
        "friendly_name": "Set Window Minutes",
        "type": "input_number",
        "min": 5,
        "max": 720,
        "step": 5,
        "unit": "minutes",
        "icon": "mdi:timer-settings-outline",
    },
    {
        "name": "set_soc_minutes",
        "friendly_name": "Set SOC Minutes",
        "type": "input_number",
        "min": 5,
        "max": 720,
        "step": 5,
        "unit": "minutes",
        "icon": "mdi:timer-settings-outline",
    },
    {"name": "set_reserve_min", "friendly_name": "Set Reserve Min", "type": "input_number", "min": 4, "max": 100, "step": 1, "unit": "%", "icon": "mdi:percent"},
    {
        "name": "rate_low_threshold",
        "friendly_name": "Rate Low Threshold",
        "type": "input_number",
        "min": 0.00,
        "max": 2.00,
        "step": 0.05,
        "unit": "multiple",
        "icon": "mdi:multiplication",
    },
    {
        "name": "rate_high_threshold",
        "friendly_name": "Rate High Threshold",
        "type": "input_number",
        "min": 0.00,
        "max": 2.00,
        "step": 0.05,
        "unit": "multiple",
        "icon": "mdi:multiplication",
    },
    {"name": "car_charging_hold", "friendly_name": "Car charging hold", "type": "switch"},
    {"name": "octopus_intelligent_charging", "friendly_name": "Octopus Intelligent Charging", "type": "switch"},
    {"name": "car_charging_plan_smart", "friendly_name": "Car Charging Plan Smart", "type": "switch"},
    {"name": "car_charging_from_battery", "friendly_name": "Allow car to charge from battery", "type": "switch"},
    {"name": "calculate_best", "friendly_name": "Calculate Best", "type": "switch"},
    {"name": "calculate_best_charge", "friendly_name": "Calculate Best Charge", "type": "switch"},
    {"name": "calculate_best_discharge", "friendly_name": "Calculate Best Discharge", "type": "switch"},
    {"name": "calculate_discharge_first", "friendly_name": "Calculate Discharge First", "type": "switch"},
    {"name": "calculate_discharge_oncharge", "friendly_name": "Calculate Discharge on charge slots", "type": "switch"},
    {"name": "calculate_second_pass", "friendly_name": "Calculate second pass (slower)", "type": "switch"},
    {"name": "calculate_inday_adjustment", "friendly_name": "Calculate in-day adjustment", "type": "switch"},
    {
        "name": "calculate_max_windows",
        "friendly_name": "Max charge/discharge windows",
        "type": "input_number",
        "min": 8,
        "max": 128,
        "step": 8,
        "unit": "kwh",
        "icon": "mdi:vector-arrange-above",
    },
    {
        "name": "calculate_plan_every",
        "friendly_name": "Calculate plan every N minutes",
        "type": "input_number",
        "min": 5,
        "max": 60,
        "step": 5,
        "unit": "kwh",
        "icon": "mdi:clock-end",
    },
    {"name": "combine_charge_slots", "friendly_name": "Combine Charge Slots", "type": "switch"},
    {"name": "combine_discharge_slots", "friendly_name": "Combine Discharge Slots", "type": "switch"},
    {"name": "combine_mixed_rates", "friendly_name": "Combined Mixed Rates", "type": "switch"},
    {"name": "set_charge_window", "friendly_name": "Set Charge Window", "type": "switch"},
    {"name": "set_charge_freeze", "friendly_name": "Set Charge Freeze", "type": "switch"},
    {"name": "set_window_notify", "friendly_name": "Set Window Notify", "type": "switch"},
    {"name": "set_status_notify", "friendly_name": "Set Status Notify", "type": "switch"},
    {"name": "set_discharge_window", "friendly_name": "Set Discharge Window", "type": "switch"},
    {"name": "set_discharge_freeze", "friendly_name": "Set Discharge Freeze", "type": "switch"},
    {"name": "set_discharge_freeze_only", "friendly_name": "Set Discharge Freeze Only", "type": "switch"},
    {"name": "set_discharge_notify", "friendly_name": "Set Discharge Notify", "type": "switch"},
    {"name": "set_discharge_during_charge", "friendly_name": "Set Discharge During Charge", "type": "switch"},
    {"name": "set_soc_enable", "friendly_name": "Set Soc Enable", "type": "switch"},
    {"name": "set_soc_notify", "friendly_name": "Set Soc Notify", "type": "switch"},
    {"name": "set_reserve_enable", "friendly_name": "Set Reserve Enable", "type": "switch"},
    {"name": "set_reserve_hold", "friendly_name": "Set Reserve Hold", "type": "switch"},
    {"name": "set_reserve_notify", "friendly_name": "Set Reserve Notify", "type": "switch"},
    {"name": "set_read_only", "friendly_name": "Read Only mode", "type": "switch"},
    {"name": "balance_inverters_enable", "friendly_name": "Balance Inverters Enable (Beta)", "type": "switch"},
    {"name": "balance_inverters_charge", "friendly_name": "Balance Inverters for charging", "type": "switch"},
    {"name": "balance_inverters_discharge", "friendly_name": "Balance Inverters for discharge", "type": "switch"},
    {"name": "balance_inverters_crosscharge", "friendly_name": "Balance Inverters for cross-charging", "type": "switch"},
    {
        "name": "balance_inverters_threshold_charge",
        "friendly_name": "Balance Inverters threshold charge",
        "type": "input_number",
        "min": 1,
        "max": 20,
        "step": 1,
        "unit": "%",
        "icon": "mdi:percent",
    },
    {
        "name": "balance_inverters_threshold_discharge",
        "friendly_name": "Balance Inverters threshold discharge",
        "type": "input_number",
        "min": 1,
        "max": 20,
        "step": 1,
        "unit": "%",
        "icon": "mdi:percent",
    },
    {"name": "debug_enable", "friendly_name": "Debug Enable", "type": "switch", "icon": "mdi:bug-outline"},
    {"name": "car_charging_plan_time", "friendly_name": "Car charging planned ready time", "type": "select", "options": OPTIONS_TIME, "icon": "mdi:clock-end"},
    {"name": "rate_low_match_export", "friendly_name": "Rate Low Match Export", "type": "switch"},
    {"name": "load_filter_modal", "friendly_name": "Apply modal filter historical load", "type": "switch"},
    {"name": "iboost_enable", "friendly_name": "IBoost enable", "type": "switch"},
    {"name": "iboost_max_energy", "friendly_name": "IBoost max energy", "type": "input_number", "min": 0, "max": 5, "step": 0.1, "unit": "kwh"},
    {"name": "iboost_today", "friendly_name": "IBoost today", "type": "input_number", "min": 0, "max": 5, "step": 0.1, "unit": "kwh"},
    {"name": "iboost_max_power", "friendly_name": "IBoost max power", "type": "input_number", "min": 0, "max": 3500, "step": 100, "unit": "w"},
    {"name": "iboost_min_power", "friendly_name": "IBoost min power", "type": "input_number", "min": 0, "max": 3500, "step": 100, "unit": "w"},
    {"name": "iboost_min_soc", "friendly_name": "IBoost min soc", "type": "input_number", "min": 0, "max": 100, "step": 5, "unit": "%", "icon": "mdi:percent"},
    {"name": "holiday_days_left", "friendly_name": "Holiday days left", "type": "input_number", "min": 0, "max": 28, "step": 1, "unit": "days", "icon": "mdi:clock-end"},
]

edits = []
old_lines = lines.copy()
new_lines = []
for i, line in enumerate(lines[8243:8439]):
    if "self." in line:
        print(line)
    #     for item in CONFIG_ITEMS:
    #         if f"self.{item['name']}" in line:
    #             x = '"' + item['name'] + '"'
    #             line = line.replace(f"self.{item['name']}",f"self.config[{x}]")
    #             z = line
    #             edits.append(i)
    # new_lines.append(line)

# %%
with open("test.py", "w") as file_out:
    # Writing data to a file
    file_out.writelines(lines)

# %%
