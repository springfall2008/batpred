
import ipaddress
from time import perf_counter
from time import sleep
import sys
import socket
import asyncio
from pymodbus.client import AsyncModbusTcpClient
from threading import Thread, Lock


class Threader:
    """
    This is a class that calls a list of functions in a limited number of
    threads. It uses locks to make sure the data is thread safe.
    This class also provides a lock called: `<Threader>.print_lock`
    """
    def __init__(self, threads=30):
        self.thread_lock = Lock()
        self.functions_lock = Lock()
        self.functions = []
        self.threads = []
        self.nthreads = threads
        self.running = True
        self.print_lock = Lock()

    def stop(self) -> None:
        # Signal all worker threads to stop
        self.running = False

    def append(self, function, *args) -> None:
        # Add the function to a list of functions to be run
        self.functions.append((function, args))

    def start(self) -> None:
        # Create a limited number of threads
        for i in range(self.nthreads):
            thread = Thread(target=self.worker, daemon=True)
            # We need to pass in `thread` as a parameter so we
            # have to use `<threading.Thread>._args` like this:
            thread._args = (thread, )
            self.threads.append(thread)
            thread.start()

    def join(self) -> None:
        # Joins the threads one by one until all of them are done.
        for thread in self.threads:
            thread.join()

    def worker(self, thread:Thread) -> None:
        # While we are running and there are functions to call:
        while self.running and (len(self.functions) > 0):
            # Get a function
            with self.functions_lock:
                function, args = self.functions.pop(0)
            # Call that function
            function(*args)

        # Remove the thread from the list of threads.
        # This may cause issues if the user calls `<Threader>.join()`
        # But I haven't seen this problem while testing/using it.
        with self.thread_lock:
            self.threads.remove(thread)

def findInvertor(subnet, ports):
    baseip=subnet.split('/')[0]
    mask=subnet.split('/')[1]
    segs=baseip.split('.')
    if not segs[-1]=="0":
        subnet=segs[0]+'.'+segs[1]+'.'+segs[2]+'.0/'+mask
    ips=[str(ip) for ip in ipaddress.IPv4Network(subnet)]
    start = perf_counter()
    # I didn't need a timeout of 1 so I used 0.1
    socket.setdefaulttimeout(1)

    error_dict = {}
    invlist = {}
    def connect(hostname, port):
        
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            result = sock.connect_ex((hostname, port))
        with threader.print_lock:
            if result == 0:
                invlist[len(invlist)+1]=[hostname, port]

    # add more or less threads to complete your scan
    threader = Threader(20)
    for ip in ips:
        for port in ports:
            threader.append(connect, ip, port)
    threader.start()
    threader.join()
    return(invlist)

async def main():
    list_client = findInvertor("192.168.0.0/24", [8899, 502])
    for id in list_client:
        host, port = list_client[id]
        print("attempt to connect to modbus on {}:{}".format(host, port))
        client = AsyncModbusTcpClient(host, port=port, retries=10, timeout=10, reconnect_delay=10)
        await client.connect()
        if client.connected:
            print("Able to connect via modbus")
            client.close
        else:
            print("Unable to connect via modbus")


if __name__ == '__main__':
    print("findInvertor")
    asyncio.run(main())
