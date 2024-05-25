import io
import yaml
import sys
import asyncio
import predbat
import time

async def main():
    print("Starting Standalone Predbat")
    #try:
    p_han = predbat.PredBat()
    p_han.initialize()
    #except Exception as e:
    #    print("Failed to start predbat {}".format(e))
    #    return

    # Runtime loop
    while True:
        print("In Predbat run loop")
        time.sleep(1)
        try:
            predbat.timer_tick()
        except Exception as e:
            print("Error, exception raised in predbat {}".format(e))

if __name__ == '__main__':
    import hass as hass
    print("Starting Predbat standalone")
    asyncio.run(main())
    sys.exit(0)

class Hass:
    def log(self, msg):
        print(msg)

    def create_task(self, task):
        return asyncio.create_task(task)
        
    def __init__(self):
        print("Sys init")
        self.args = {}
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
    
    def timer_tick(self):
        print("Timer tick")
