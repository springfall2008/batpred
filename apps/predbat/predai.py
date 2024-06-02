from typing import Any
import pandas as pd
import numpy as np
import sqlite3
from datetime import datetime, timedelta, timezone
from neuralprophet import NeuralProphet, set_log_level
import os
import aiohttp
import requests
import asyncio
import json
import ssl
import math
import yaml

TIMEOUT = 240
TIME_FORMAT_HA = "%Y-%m-%dT%H:%M:%S%z"
TIME_FORMAT_HA_DOT = "%Y-%m-%dT%H:%M:%S.%f%z"

def timestr_to_datetime(timestamp):
    """
    Convert a Home Assistant timestamp string to a datetime object.
    """
    try:
        start_time = datetime.strptime(timestamp, TIME_FORMAT_HA)
    except ValueError:
        try:
            start_time = datetime.strptime(timestamp, TIME_FORMAT_HA_DOT)
        except ValueError:
            start_time = None
    if start_time:
        start_time = start_time.replace(second=0, microsecond=0)
    return start_time


class HAInterface():
    def __init__(self):
        self.ha_key = os.environ.get("SUPERVISOR_TOKEN")
        self.ha_url = "http://supervisor/core"
        print("HA Interface started key {} url {}".format(self.ha_key, self.ha_url))

    async def get_events(self):
        res = await self.api_call("/api/events")
        return res

    async def get_history(self, sensor, now, days=7):
        """
        Get the history for a sensor from Home Assistant.

        :param sensor: The sensor to get the history for.
        :return: The history for the sensor.
        """
        start = now - timedelta(days=days)
        end = now
        print("Getting history for sensor {} start {} end {}".format(sensor, start.strftime(TIME_FORMAT_HA), end.strftime(TIME_FORMAT_HA)))
        res = await self.api_call("/api/history/period/{}".format(start.strftime(TIME_FORMAT_HA)), {"filter_entity_id": sensor, "end_time": end.strftime(TIME_FORMAT_HA)})
        if res:
            res = res[0]
            start = timestr_to_datetime(res[0]["last_updated"])
            end = timestr_to_datetime(res[-1]["last_updated"])
        print("History for sensor {} starts at {} ends at {}".format(sensor, start, end))
        return res, start, end

    async def get_state(self, entity_id=None, default=None, attribute=None):
        """
        Get state of an entity in Home Assistant.
        """
        item = await self.api_call("/api/states/{}".format(entity_id))
        if not item:
            return default
        elif attribute:
            attributes = item.get("attributes", {})
            return attributes.get(attribute, default)
        else:
            return item.get("state", default)

    async def set_state(self, entity_id, state, attributes=None):
        """
        Set the state of an entity in Home Assistant.
        """
        data = {"state": state}
        if attributes:
            data["attributes"] = attributes
        await self.api_call("/api/states/{}".format(entity_id), data, post=True)

    async def api_call(self, endpoint, datain=None, post=False):
        """
        Make an API call to Home Assistant.

        :param endpoint: The API endpoint to call.
        :param datain: The data to send in the body of the request.
        :param post: True if this is a POST request, False for GET.
        :return: The response from the API.
        """
        url = self.ha_url + endpoint
        headers = {
            "Authorization": "Bearer " + self.ha_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if post:
            if datain:
                response = await asyncio.to_thread(requests.post, url, headers=headers, json=datain, timeout=TIMEOUT)
            else:
                response = await asyncio.to_thread(requests.post, url, headers=headers, timeout=TIMEOUT)
        else:
            if datain:
                response = await asyncio.to_thread(requests.get, url, headers=headers, params=datain, timeout=TIMEOUT)
            else:
                response = await asyncio.to_thread(requests.get, url, headers=headers, timeout=TIMEOUT)
        try:
            data = response.json()
        except requests.exceptions.JSONDecodeError:
            print("Failed to decode response from {}".format(url))
            data = None
        except (requests.Timeout, requests.exceptions.ReadTimeout):
            print("Timeout from {}".format(url))
            data = None
        return data

class Prophet:
    def __init__(self, period=30):
        set_log_level("ERROR")
        self.period = period

    async def process_dataset(self, sensor_name, new_data, start_time, end_time, incrementing=False, reset_low=0.0, reset_high=0.0):
        """
        Store the data in the dataset for training.
        """
        dataset = pd.DataFrame(columns=["ds", "y"])
        
        timenow = start_time
        timenow = timenow.replace(second=0, microsecond=0, minute=0)
        data_index = 0
        value = 0
        data_len = len(new_data)
        total = 0
        last_value = None

        print("Process dataset for sensor {} start {} end {} incrementing {} reset_low {} reset_high {}".format(sensor_name, start_time, end_time, incrementing, reset_low, reset_high))
        while timenow <= end_time and data_index < data_len:
            try:
                value = float(new_data[data_index]["state"])
                if last_value is None:
                    last_value = value
            except ValueError:
                if last_value is not None:
                    value = last_value
                else:
                    data_index += 1
                    continue

            last_updated = new_data[data_index]["last_updated"]
            start_time = timestr_to_datetime(last_updated)

            if incrementing:
                # Reset?
                if value < last_value and value < reset_low and last_value > reset_high:
                    total = total + value
                else:
                    total = max(total + value - last_value, 0)
            last_value = value
        
            if not start_time or start_time < timenow:
                data_index += 1
                continue

            real_value = value
            if incrementing:
                real_value = max(0, total)
                total = 0
            dataset.loc[len(dataset)] = {"ds": timenow, "y": real_value}
            timenow = timenow + timedelta(minutes=self.period)

        print(dataset)
        # dataset.to_csv('/config/{}.csv'.format(sensor_name), index=False) 

        return dataset, value
    
    async def train(self, dataset, future_periods, n_lags=0, country=None):
        """
        Train the model on the dataset.
        """
        self.model = NeuralProphet(n_lags=n_lags, yearly_seasonality=True, weekly_seasonality=True, daily_seasonality=True)
        if country:
            print("Adding country holidays for {}".format(country))
            self.model.add_country_holidays(country)
        # Fit the model on the dataset (this might take a bit)
        self.metrics = self.model.fit(dataset, freq=(str(self.period) + "min"), progress=None)
        # Create a new dataframe reaching 96 into the future for our forecast, n_historic_predictions also shows historic data
        self.df_future = self.model.make_future_dataframe(dataset, n_historic_predictions=True, periods=future_periods)
        self.forecast = self.model.predict(self.df_future)
        print(self.forecast)
 
    async def save_prediction(self, entity, now, interface, start, incrementing=False, reset_daily=False, units="", days=7):
        """
        Save the prediction to Home Assistant.
        """
        pred = self.forecast
        total = 0
        total_org = 0
        timeseries = {}
        timeseries_org = {}

        for index, row in pred.iterrows():
            ptimestamp = row["ds"].tz_localize(timezone.utc)
            diff = ptimestamp - now
            timestamp = now + diff
                
            time = timestamp.strftime(TIME_FORMAT_HA)
            value = row["yhat1"]
            value_org = row["y"]

            # Daily reset?
            if timestamp <= now:
                if reset_daily and timestamp.hour == 0 and timestamp.minute == 0:
                    total = 0
                    total_org = 0

            total += value
            if not math.isnan(value_org):
                total_org += value_org
            else:
                value_org = None

            # Avoid too much history in HA
            if diff.days < -days:
                continue
       
            if incrementing:
                timeseries[time] = round(total, 2)
                if value_org:
                    timeseries_org[time] = round(total_org, 2)
            else:
                timeseries[time] = round(value, 2)
                if value_org:
                    timeseries_org[time] = round(value_org, 2)

        final = total if incrementing else value
        attributes = {"last_updated": str(now), "unit_of_measurement": units, "state_class" : "measurement", "results" : timeseries, "source" : timeseries_org}
        print("Saving prediction to {} last_update {}".format(entity, str(now)))
        await interface.set_state(entity, state=round(final,2), attributes=attributes)

async def subtract_set(dataset, subset, now, incrementing=False):
    """
    Subtract the subset from the dataset.
    """
    pruned = pd.DataFrame(columns=["ds", "y"])
    count = 0
    for index, row in dataset.iterrows():
        ds = row["ds"]
        value = row["y"]
        car_value = 0

        car_row = subset.loc[subset["ds"] == ds]
        if not car_row.empty:
            car_value = car_row["y"].values[0]
            count += 1

        if incrementing:
            value = max(value - car_value, 0)
        else:
            value = value - car_value
        pruned.loc[len(pruned)] = {"ds": ds, "y": value}
    print("Subtracted {} values into new set: {}".format(count, pruned))
    return pruned

class Database():
    def __init__(self):
        self.con = sqlite3.connect('/config/predai.db')
        self.cur = self.con.cursor()

    async def create_table(self, table):
        """
        Create a table in the database by table if it does not exist.
        """
        print("Create table {}".format(table))
        self.cur.execute("CREATE TABLE IF NOT EXISTS {} (timestamp TEXT PRIMARY KEY, value REAL)".format(table))
        self.con.commit()

    async def get_history(self, table):
        """
        Get the history from the database, sorted by timestamp.
        Returns a Dataframe with the history data.
        """
        self.cur.execute("SELECT * FROM {} ORDER BY timestamp".format(table))
        rows = self.cur.fetchall()
        history = pd.DataFrame(columns=["ds", "y"])
        if not rows:
            return history
        for row in rows:
            timestamp = row[0]
            value = row[1]
            history.loc[len(history)] = {"ds": timestamp, "y": value}
        return history

    async def store_history(self, table, history, prev=None):
        """
        Store the history in the database.
        Only the data associated with TIMESTAMPs not already in the database will be stored.
        Returns the updated history DataFrame.

        :param table: The table to store the history in.
        :param history: The history data as a DataFrame.
        """
        added_rows = 0
        prev_values = prev["ds"].values
        prev_values = prev_values.tolist()

        for index, row in history.iterrows():
            timestamp = str(row["ds"])
            value = row["y"]
            if timestamp not in prev_values:
                prev.loc[len(prev)] = {"ds": timestamp, "y": value}
                self.cur.execute("INSERT INTO {} (timestamp, value) VALUES ('{}', {})".format(table, timestamp, value))
                added_rows += 1
        self.con.commit()
        print("Added {} rows to database table {}".format(added_rows, table))
        return prev

async def print_dataset(name, dataset):
    count = 0
    for index, row in dataset.iterrows():
        timestamp = str(row["ds"])
        value = row["y"]
        print("Got dataset {} row {} {}".format(name, timestamp, value))
        count += 1
        if count > 24:
            break

async def get_history(interface, nw, sensor_name, now, incrementing, days, use_db, reset_low, reset_high):
    """
    Get history from HA, combine it with the database if use_db is True.
    """
    dataset, start, end = await interface.get_history(sensor_name, now, days=days)
    dataset, last_dataset_value = await nw.process_dataset(sensor_name, dataset, start, end, incrementing=incrementing, reset_low=reset_low, reset_high=reset_high)

    if use_db:
        table_name = sensor_name.replace(".", "_")  # SQLite does not like dots in table names
        db = Database()
        await db.create_table(table_name)
        prev = await db.get_history(table_name)
        dataset = await db.store_history(table_name, dataset, prev)
        print("Stored dataset in database and retrieved full history from database length {}".format(len(dataset)))
    return dataset, start, end
    
async def main():
    """
    Main function for the prediction AI.
    """
    interface = HAInterface()
    while True:
        config = yaml.safe_load(open("/config/predai.yaml"))
        if not config:
            print("WARN: predai.yaml is missing, no work to do")
        else:
            print("Configuration loaded")
            update_every = config.get('update_every', 30)
            sensors = config.get("sensors", [])
            for sensor in sensors:
                sensor_name = sensor.get("name", None)
                subtract_names = sensor.get("subtract", None)
                days = sensor.get("days", 7)
                export_days = sensor.get("export_days", days)
                incrementing = sensor.get("incrementing", False)
                reset_daily = sensor.get("reset_daily", False)
                interval = sensor.get("interval", 30)
                units = sensor.get("units", "")
                future_periods = sensor.get("future_periods", 96)
                use_db = sensor.get("database", True)
                reset_low = sensor.get("reset_low", 1.0)
                reset_high = sensor.get("reset_high", 2.0)
                n_lags = sensor.get("n_lags", 0)
                country = sensor.get("country", None)

                if not sensor_name:
                    continue

                
                nw = Prophet(interval)
                now = datetime.now(timezone.utc).astimezone()
                now=now.replace(second=0, microsecond=0, minute=0)
                

                print("Update at time {} Processing sensor {} incrementing {} reset_daily {} interval {} days {} export_days {} subtract {}".format(now, sensor_name, incrementing, reset_daily, interval, days, export_days, subtract_names))

                # Get the data
                dataset, start, end = await get_history(interface, nw, sensor_name, now, incrementing, days, use_db, reset_low, reset_high)

                # Get the subtract data
                subtract_data_list = []
                if subtract_names:
                    if isinstance(subtract_names, str):
                        subtract_names = [subtract_names]
                    for subtract_name in subtract_names:
                        subtract_data, sub_start, sub_end = await get_history(interface, nw, subtract_name, now, incrementing, days, use_db, reset_low, reset_high)
                        subtract_data_list.append(subtract_data)

                # Subtract the data
                if subtract_data_list:
                    print("Subtracting data")
                    for subtract_data in subtract_data_list:
                        dataset = await subtract_set(dataset, subtract_data, now, incrementing=incrementing)

                # Start training
                await nw.train(dataset, future_periods, n_lags=n_lags, country=country)

                # Save the prediction
                await nw.save_prediction(sensor_name + "_prediction", now, interface, start=end, incrementing=incrementing, reset_daily=reset_daily, units=units, days=export_days)

        time_now = datetime.now(timezone.utc).astimezone()
        await interface.set_state("sensor.predai_last_run", state=str(time_now), attributes={"unit_of_measurement": "time"})
        print("Waiting for {} minutes at time {}".format(update_every, datetime.now(timezone.utc).astimezone()))
        for n in range(update_every):
            last_run = await interface.get_state("sensor.predai_last_run")
            if last_run is None:
                print("Restarting PredAI as last-run time has gone")
                break
            await asyncio.sleep(60)

asyncio.run(main())
