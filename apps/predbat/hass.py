import io
import yaml
import sys
import asyncio
import predbat
import time
from datetime import datetime, timedelta
from multiprocessing import set_start_method
import concurrent.futures
import threading
import os
import traceback


def check_modified(py_files, start_time):
    """
    Check if .py file was changed since we started
    """

    # Check the last modified timestamp of each .py file
    for file_path in py_files:
        if os.path.exists(file_path):
            last_modified = os.path.getmtime(file_path)
            last_modified_timestamp = datetime.fromtimestamp(last_modified)
            if last_modified_timestamp > start_time:
                print("File {} was modified".format(file_path))
                return True
        else:
            print("File {} does not exist".format(file_path))
            return True
    return False


async def main():
    print("**** Starting Standalone Predbat ****")
    start_time = datetime.now()

    try:
        p_han = predbat.PredBat()
    except Exception as e:
        print("Error: Failed to construct predbat {}".format(e))
        print(traceback.format_exc())
        return

    try:
        p_han.initialize()
    except Exception as e:
        print("Error: Failed to initialize predbat {}".format(e))
        print(traceback.format_exc())
        await p_han.stop_all()
        return

    # Find all .py files in the directory hierarchy
    py_files = []
    for root, dirs, files in os.walk("."):
        for file in files:
            if (file.endswith(".py") or file == "apps.yaml") and not file.startswith("."):
                py_files.append(os.path.join(root, file))
    print("Watching {} for changes".format(py_files))

    # Runtime loop
    count = 0
    while True:
        time.sleep(1)
        await p_han.timer_tick()
        if (count % 5 == 0) and check_modified(py_files, start_time):
            print("Stopping Predbat due to file changes....")
            await p_han.stop_all()
            break
        if p_han.fatal_error:
            print("Stopping Predbat due to fatal error....")
            await p_han.stop_all()
            break
        count += 1


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
        msg_lower = msg.lower()
        if not quiet or msg_lower.startswith("error") or msg_lower.startswith("warn") or msg_lower.startswith("info"):
            print(message, end="")

        # maximum number of historic logfiles to retain
        max_logs = 9

        log_size = self.logfile.tell()
        if log_size > 10000000:
            # check for existence of previous logfiles and rename each in turn
            for num_logs in range(max_logs - 1, 0, -1):
                filename = "predbat." + format(num_logs) + ".log"
                if os.path.isfile(filename):
                    newfile = "predbat." + format(num_logs + 1) + ".log"
                    os.rename(filename, newfile)

            self.logfile.close()
            os.rename("predbat.log", "predbat.1.log")
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
        self.threads.append(t1)
        return t1

    async def stop_all(self):
        """
        Stop Predbat
        """
        self.log("Stopping Predbat", quiet=False)
        await self.terminate()

        for t in self.threads:
            t.join(5 * 60)
        self.logfile.close()

    def __init__(self):
        """
        Start Predbat
        """
        self.args = {}
        self.run_list = []
        self.threads = []
        self.fatal_error = False
        self.hass_api_version = 2

        self.logfile = open("predbat.log", "a")

        # Open YAML file apps.yaml and read it
        apps_file = os.getenv("PREDBAT_APPS_FILE", "apps.yaml")
        self.log(f"Loading {apps_file}", quiet=False)
        with io.open(apps_file, "r") as stream:
            try:
                config = yaml.safe_load(stream)
                self.args = config["pred_bat"]
            except yaml.YAMLError as exc:
                print(exc)
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
                try:
                    item["callback"](None)
                except Exception as e:
                    self.log("Error: {}".format(e), quiet=False)
                    print(traceback.format_exc())
                while now > item["next_time"]:
                    run_every = timedelta(seconds=item["run_every"])
                    item["next_time"] += run_every
