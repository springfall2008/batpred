# Predbat Components Documentation

This document provides a comprehensive overview of all Predbat components, their purposes, and configuration options.

## Overview

Predbat uses a modular component architecture where each component provides specific functionality such as database management, cloud API integration, web interfaces, and energy provider integrations.
Each component can be enabled or disabled independently through your `apps.yaml` configuration file.

## Component List

### Database Manager (db)

**Can be restarted:** No

#### What it does (db)

Stores and manages all historical data for Predbat, including energy usage, sensor values, and system states. This allows Predbat to keep its own database of historical information independent of Home Assistant.

#### When to enable (db)

- You want to retain data longer than Home Assistant keeps or you want to run Predbat without Home Assistant

#### Configuration Options (db)

| Option | Type | Required | Default | Config Key | Description |
|--------|------|----------|---------|------------|-------------|
| `db_enable` | Boolean | Yes | - | `db_enable` | Set to `true` to enable the database, `false` to disable |
| `db_days` | Integer | No | 30 | `db_days` | Number of days of historical data to keep in the database |

---

### Home Assistant Interface (ha)

**Can be restarted:** No

#### What it does (ha)

Provides the connection between Predbat and Home Assistant. This is the core communication channel that allows Predbat to read sensor data, control devices, and update its status in Home Assistant.

If you are using Predbat without Home Assistant then this interface layer just talks directly to the Database Manager.

#### When to enable (ha)

This component is always enabled and required for Predbat to function.

#### Configuration Options (ha)

| Option | Type | Required | Default | Config Key | Description |
|--------|------|----------|---------|------------|-------------|
| `ha_url` | String | No | `http://supervisor/core` | `ha_url` | Home Assistant API URL (the default is for when using an HA add-on) |
| `ha_key` | String | No | Auto-detected | `ha_key` | Home Assistant access token (auto-detected when running as add-on) |
| `db_enable` | Boolean | No | False | `db_enable` | Enable database integration |
| `db_mirror_ha` | Boolean | No | False | `db_mirror_ha` | Copy Home Assistant data into Predbat's database |
| `db_primary` | Boolean | No | False | `db_primary` | Use Predbat's database instead of Home Assistant for the primary data source |

---

### Home Assistant History (ha_history)

**Can be restarted:** No

#### What it does (ha_history)

Retrieves and processes historical sensor data from Home Assistant's database (or from the Predbat database).
This component handles all lookups of past energy usage, battery levels, and other historical information.

#### When to enable (ha_history)

This component is always enabled.

#### Configuration Options (ha_history)

No configuration required. This component automatically uses your Home Assistant connection.

---

### Web Interface (web)

**Can be restarted:** Yes

#### What it does (web)

Provides a built-in web server that lets you view and manage Predbat through your web browser. Access dashboards, view battery plans, check logs, and edit configuration all from an easy-to-use web interface.

#### Configuration Options (web)

| Option | Type | Required | Default | Config Key | Description |
|--------|------|----------|---------|------------|-------------|
| `port` | Integer | No | 5052 | `web_port` | Port number for the web server |

#### How to access (web)

If you use Predbat is a Home Assistant add on then click 'Open Web UI' from the add-on or add Predbat Web UI to your side bar.
If you run Predbat outside then you can access it from the port as configured:  `http://homename:5052`

---

### MCP Server (mcp)

**Can be restarted:** Yes

#### What it does (mcp)

Provides a programmatic API that allows AI assistants (like ChatGPT, Claude, or other MCP-compatible tools) to read and control Predbat. This enables you to use natural language commands to check status, adjust settings, or override plans.

#### When to enable (mcp)

- You want to control Predbat through AI assistants
- You're building custom integrations or tools
- You want programmatic access to Predbat data

#### Security note (mcp)

The MCP server requires a secret key for authentication. Keep this secret secure and don't share it publicly.

***CAUTION*** Predbat WebUI does not support https currently, so exposing this MCP port externally to your home network would be unwise.

#### Configuration Options (mcp)

| Option | Type | Required | Default | Config Key | Description |
|--------|------|----------|---------|------------|-------------|
| `mcp_enable` | Boolean | Yes | False | `mcp_enable` | Set to `true` to enable the MCP server |
| `mcp_secret` | String | No | `predbat_mcp_secret` | `mcp_secret` | Secret key for authentication - change this! |
| `mcp_port` | Integer | No | 8199 | `mcp_port` | Port number for the MCP server |

#### How to configure your MCP client (mcp)

Below is an example MCP configuration inside VSCode, but it will be similar in Cline/Claude/Cursor etc.

```json
Example usage in VSCode
{
 "servers": {
  "predbat-mcp": {
   "url": "http://homeassistant.local:8199/mcp",
   "type": "http",
   "description": "Predbat Model Context Protocol Server",
   "headers": {
    "Authorization" : "Bearer predbat_mcp_secret",
   },
  }
 },
 "inputs": []
}
```

#### Available commands (mcp)

- Get current system status
- View and update configuration settings
- Browse all entities
- Retrieve battery plan data
- Override plan for specific time periods
- Access apps.yaml configuration

---

### GivEnergy Cloud Direct (gecloud)

**Can be restarted:** Yes

#### What it does (gecloud)

Connects directly to the GivEnergy Cloud to control your GivEnergy inverter and battery. This allows Predbat to automatically set charge/discharge times, power limits, and read real-time data from your inverter without relying on Home Assistant integrations.

#### When to enable (gecloud)

- You have a GivEnergy inverter
- You want direct cloud-based control (more reliable than local control)
- You have your GivEnergy Cloud API key
- You want automatic control of your battery

#### Important notes (gecloud)

- Requires a GivEnergy Cloud account and API key
- Can also control GivEnergy EV chargers and smart devices

#### Configuration Options (gecloud)

| Option | Type | Required | Default | Config Key | Description |
|--------|------|----------|---------|------------|-------------|
| `ge_cloud_direct` | Boolean | Yes | - | `ge_cloud_direct` | Set to `true` to enable GivEnergy Cloud control |
| `api_key` | String | Yes | - | `ge_cloud_key` | Your GivEnergy Cloud API key |
| `automatic` | Boolean | No | False | `ge_cloud_automatic` | Set to `true` to automatically configured Predbat to use GivEnergy Cloud direct (no additional apps.yaml changes required) |

#### How to get your API key (gecloud)

1. Log in to your GivEnergy account at <https://www.givenergy.cloud>
2. Go to Settings → API Keys
3. Generate a new API key
4. Copy the key into your `apps.yaml` configuration

---

### GivEnergy Cloud Data (gecloud_data)

**Can be restarted:** Yes

#### What it does (gecloud_data)

Downloads historical energy data from GivEnergy Cloud including consumption, generation, battery usage, and grid import/export. This provides accurate historical data for Predbat's calculations and predictions.

#### When to enable (gecloud_data)

- You have a GivEnergy system
- You want Predbat to use historical data from GivEnergy Cloud instead of from Home Assistant.

#### Configuration Options (gecloud_data)

| Option | Type | Required | Default | Config Key | Description |
|--------|------|----------|---------|------------|-------------|
| `ge_cloud_data` | Boolean | Yes | - | `ge_cloud_data` | Set to `true` to enable historical data download |
| `ge_cloud_key` | String | Yes | - | `ge_cloud_key` | Your GivEnergy Cloud API key (same as Cloud Direct) |
| `ge_cloud_serial` | String | No | Auto-detected | `ge_cloud_serial` | Your inverter serial number (usually auto-detected) |
| `days_previous` | List | No | [7] | `days_previous` | List of days to download data for, e.g., `[7]` for last week |

---

### Octopus Energy Direct (octopus)

**Can be restarted:** Yes

#### What it does (octopus)

Connects to your Octopus Energy account to automatically download your tariff rates, including support for dynamic tariffs like Agile and Intelligent Octopus. This ensures Predbat always has the most accurate and up-to-date energy pricing.

#### When to enable (octopus)

- You're an Octopus Energy customer
- You want automatic tariff updates
- You're on a variable tariff (Agile, Intelligent Octopus, etc.)
- You want to see your actual consumption data

#### Important notes (octopus)

- Works with all Octopus tariffs including Agile and Intelligent Octopus
- Automatically manages Intelligent Octopus smart charging slots
- Updates rates automatically, no manual intervention needed

#### Configuration Options (octopus)

| Option | Type | Required | Default | Config Key | Description |
|--------|------|----------|---------|------------|-------------|
| `key` | String | Yes | - | `octopus_api_key` | Your Octopus Energy API key |
| `account_id` | String | Yes | - | `octopus_api_account` | Your Octopus Energy account number (starts with A-) |
| `automatic` | Boolean | No | True | `octopus_automatic` | Set to `true` to automatically configure Predbat to use this Component (no need to update apps.yaml) |

#### How to get your API credentials (octopus)

1. Log in to your Octopus Energy account at <https://octopus.energy>
2. Go to your account dashboard
3. Find your API key (usually in Developer settings)
4. Your account number is shown on your dashboard (format: A-XXXXXXXX)

---

### Ohme Charger (ohme)

**Can be restarted:** Yes

#### What it does (ohme)

Integrates with Ohme EV chargers to monitor charging sessions and coordinate charging with your energy tariff. Works particularly well with Intelligent Octopus to optimize charging times and costs.

#### When to enable (ohme)

- You have an Ohme EV charger
- You want Predbat to factor in the charging plan within Ohme, this is mostly used with Octopus Intelligent GO.

#### Important notes (ohme)

- Requires your Ohme account credentials
- Can automatically manage Intelligent Octopus charging slots
- Monitors real-time charging status and energy consumption

#### Configuration Options (ohme)

| Option | Type | Required | Default | Config Key | Description |
|--------|------|----------|---------|------------|-------------|
| `email` | String | Yes | - | `ohme_login` | Your Ohme account email address |
| `password` | String | Yes | - | `ohme_password` | Your Ohme account password |
| `ohme_automatic_octopus_intelligent` | Boolean | No | - | `ohme_automatic_octopus_intelligent` | Set to `true` to automatically sync with Intelligent Octopus |

---

### Fox ESS API (fox)

**Can be restarted:** Yes

#### What it does (fox)

Integrates with Fox ESS inverters for monitoring and controlling Fox ESS battery systems. Similar to GivEnergy Cloud Direct, but for Fox ESS equipment.

#### When to enable (fox)

- You have a Fox ESS inverter
- You want direct API control of your Fox system
- You have your Fox ESS API key

#### Important notes (fox)

- Requires Fox ESS Cloud account and API key

#### Configuration Options (fox)

| Option | Type | Required | Default | Config Key | Description |
|--------|------|----------|---------|------------|-------------|
| `key` | String | Yes | - | `fox_key` | Your Fox ESS API key |
| `automatic` | Boolean | No | False | `fox_automatic` | Set to `true` to automatically configured Predbat to use the Fox inverter (no manual apps.yaml updates required) |

---

### Alert Feed (alert_feed)

**Can be restarted:** Yes

#### What it does (alert_feed)

Monitors weather alert feeds (MeteoAlarm) for severe weather warnings that might impact your energy usage or solar generation. Predbat can use this information to adjust its planning accordingly.

#### When to enable (alert_feed)

- You want Predbat to be aware of weather alerts
- You want to adjust battery strategy based on weather warnings
- You're in an area with frequent severe weather

#### How it works (alert_feed)

- Checks for alerts every 30 minutes
- Processes weather warnings for your area
- Can be configured with custom alert URLs and filters

#### Configuration Options (alert_feed)

| Option | Type | Required | Default | Config Key | Description |
|--------|------|----------|---------|------------|-------------|
| `alert_config` | Dictionary | Yes | {} | `alerts` | Alert configuration including URL and filters |

#### Configuration example

See the main configuration documentation for more details

---

### Carbon Intensity API (carbon)

**Can be restarted:** Yes

#### What it does (carbon)

Retrieves current and forecast carbon intensity data for the UK electricity grid. This allows Predbat to make environmentally-conscious decisions, charging your battery when grid electricity is greener and discharging when it's more carbon-intensive.

#### When to enable (carbon)

- You want to minimize your carbon footprint
- You're interested in carbon-aware energy management
- You're in the UK (uses UK National Grid data)
- You want to see carbon intensity alongside cost optimization

#### How it works (carbon)

- Uses your postcode to get regional carbon intensity data
- Provides both current intensity and forecasts
- Updates automatically throughout the day

Note: To use the carbon data in Predbat you also have to turn on **switch.predbat_carbon_enable**. If you want to optimise your plan for carbon then you also need to adjust the carbon weighting.

#### Configuration Options (carbon)

| Option | Type | Required | Default | Config Key | Description |
|--------|------|----------|---------|------------|-------------|
| `postcode` | String | Yes | - | `carbon_postcode` | Your UK postcode for regional carbon intensity data |
| `automatic` | Boolean | No | False | `carbon_automatic` | Set to `true` to automatically point Predbat to the carbon data |

---

## Managing Components

### Checking Component Status

You can check the status of all components through the web interface:

1. Open the web interface (default: `http://your-ha-ip:5052`)
2. Navigate to the **Components** page (`/components`)
3. View the status of each component:
   - **Enabled/Disabled** - Whether the component is configured
   - **Running/Stopped** - Current operational status
   - **Healthy/Unhealthy** - Whether the component is working correctly
   - **Last Updated** - When the component last successfully updated

### Restarting Components

Most components can be restarted if they encounter problems:

- Use the restart button on the Components page in the web interface
- Or restart Predbat entirely to restart all components

**Note:** Core components (Database Manager, Home Assistant Interface, and Home Assistant History) cannot be restarted individually and require a full Predbat restart.

### Component Health

A component is considered healthy when:

- Its task is running
- It has updated within the last 60 minutes
- It reports no errors

If a component becomes unhealthy:

1. Check your configuration in `apps.yaml`
2. Verify API keys and credentials are correct
3. Check network connectivity
4. Review Predbat logs for error messages
5. Try restarting the component

---
