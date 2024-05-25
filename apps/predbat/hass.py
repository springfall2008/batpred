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

async def main():
    print("Starting Standalone Predbat")
    try:
        p_han = predbat.PredBat()
        p_han.initialize()
    except Exception as e:
        print("Failed to start predbat {}".format(e))
        return

    # Runtime loop
    print("Started Predbat run loop")
    while True:
        time.sleep(1)
        p_han.timer_tick()

if __name__ == '__main__':
    import hass as hass
    print("Starting Predbat standalone")
    set_start_method('fork')   
    asyncio.run(main())
    sys.exit(0)

class Hass:
    def log(self, msg):
        message = "{}: {}\n".format(datetime.now(), msg)
        self.logfile.write(message)
        print(message, end='')

    def create_task(self, task):
        return asyncio.create_task(task)
        
    def __init__(self):
        """
        Start Predbat
        """
        self.args = {}
        self.run_list = []
        self.tasks = []

        self.logfile = open('predbat.log', 'a')

        # Open YAML file apps.yaml and read it
        print("Loading apps.yaml")
        with io.open('apps.yaml', 'r') as stream:
            try:
                config = yaml.safe_load(stream)
                self.args = config['pred_bat']
            except yaml.YAMLError as exc:
                print(exc)
                sys.exit(1)

        if 'ha_url' not in self.args:
            print("Error: ha_url not found in apps.yaml")
            sys.exit(1)
        if 'ha_key' not in self.args:
            print("Error: ha_key not found in apps.yaml")
            sys.exit(1)
    
    def run_every(self, callback, next_time, run_every, **kwargs):
        print("Run every triggered next time {} every {}".format(next_time, run_every))
        self.run_list.append({'callback': callback, 'next_time': next_time, 'run_every': run_every, 'kwargs': kwargs})
        return True
    
    def timer_tick(self):
        now = datetime.now()
        print("Timer tick at {}".format(now))
        for task in self.tasks:
            if task.done():
                print("Task done")
                self.tasks.remove(task)
        for item in self.run_list:
            if now > item['next_time']:
                print("Running callback next time {} and now is {}".format(item['next_time'], now))
                self.tasks.append(self.create_task(item['callback'](None)))
                while now > item['next_time']:
                    item['next_time'] += item['run_every']
                print("Task completed next time {}".format(item['next_time']))

