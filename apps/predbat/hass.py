import io
import yaml
import sys
import asyncio
import predbat
import time
from datetime import datetime, timedelta
import logging
import logging.config
from multiprocessing import Pool, cpu_count, set_start_method
import concurrent.futures
from aiohttp import web, ClientSession, WSMsgType
import threading
import os


async def main():
    print("**** Starting Standalone Predbat ****")

    try:
        p_han = predbat.PredBat()
        p_han.initialize()
    except Exception as e:
        print("Error: Failed to start predbat {}".format(e))
        return

    # Runtime loop
    while True:
        time.sleep(1)
        await p_han.timer_tick()


if __name__ == "__main__":
    set_start_method("fork")
    asyncio.run(main())
    sys.exit(0)


class Hass:
    def log(self, msg, quiet=True):
        """
        Log a message to the logfile
        """
        message = "{}: {}\n".format(datetime.now(), msg)
        self.logfile.write(message)
        self.logfile.flush()
        if not quiet:
            print(message, end="")
        log_size = self.logfile.tell()
        if log_size > 10000000:
            self.logfile.close()
            os.rename("predbat.log", "predbat.log.1")
            self.logfile = open("predbat.log", "w")

    async def run_in_executor(self, callback, *args):
        """
        Run a function in the executor
        """
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(callback, *args)
            return future

    async def task_waiter_async(self, task):
        """
        Waits for a task to complete async
        """
        await task

    def task_waiter(self, task):
        """
        Waits for a task to complete
        """
        asyncio.run(self.task_waiter_async(task))

    def create_task(self, task):
        """
        Creates a new thread to run the task in
        """
        self.log("Creating task: {}".format(task), quiet=False)
        t1 = threading.Thread(name="TaskCreate", target=self.task_waiter, args=[task])
        t1.start()
        return t1

    def __init__(self):
        """
        Start Predbat
        """
        self.args = {}
        self.run_list = []

        self.logfile = open("predbat.log", "a")

        # Open YAML file apps.yaml and read it
        self.log("Loading apps.yaml", quiet=False)
        with io.open("apps.yaml", "r") as stream:
            try:
                config = yaml.safe_load(stream)
                self.args = config["pred_bat"]
            except yaml.YAMLError as exc:
                print(exc)
                sys.exit(1)

        if "ha_url" not in self.args:
            print("Error: ha_url not found in apps.yaml")
            sys.exit(1)
        if "ha_key" not in self.args:
            print("Error: ha_key not found in apps.yaml")
            sys.exit(1)

    def run_every(self, callback, next_time, run_every, **kwargs):
        """
        Run a function every x seconds
        """
        self.run_list.append({"callback": callback, "next_time": next_time, "run_every": run_every, "kwargs": kwargs})
        return True

    async def timer_tick(self):
        """
        Timer tick function, executes tasks at the correct time
        """
        now = datetime.now()
        for item in self.run_list:
            if now > item["next_time"]:
                self.log("Running task: {}".format(item["callback"]), quiet=False)
                item["callback"](None)
                while now > item["next_time"]:
                    run_every = timedelta(seconds=item["run_every"])
                    item["next_time"] += run_every
