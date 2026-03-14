from typing import Optional
import requests

class GoodweInverter:
    def __init__(self, ip: str, username: str, password: str):
        self.ip = ip
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.login()

    def login(self):
        login_url = f"http://{self.ip}/login"
        login_data = {
            'username': self.username,
            'password': self.password
        }
        response = self.session.post(login_url, data=login_data)
        if response.status_code != 200:
            raise Exception("Login failed")

    def set_charge_discharge(self, mode: str, value: float):
        if mode not in ['charge', 'discharge']:
            raise ValueError("Mode must be 'charge' or 'discharge'")
        if not (0 <= value <= 100):
            raise ValueError("Value must be between 0 and 100")

        url = f"http://{self.ip}/set_charge_discharge"
        data = {
            'mode': mode,
            'value': value
        }
        response = self.session.post(url, data=data)
        if response.status_code != 200:
            raise Exception("Failed to set charge/discharge mode")

    def get_status(self) -> dict:
        url = f"http://{self.ip}/status"
        response = self.session.get(url)
        if response.status_code != 200:
            raise Exception("Failed to get status")
        return response.json()

# Test cases
if __name__ == "__main__":
    # Assuming the inverter is accessible at IP '192.168.1.100' with username 'admin' and password 'password'
    inverter = GoodweInverter('192.168.1.100', 'admin', 'password')

    try:
        # Set charge mode to 50%
        inverter.set_charge_discharge('charge', 50)
        print("Charge mode set to 50%")

        # Get current status
        status = inverter.get_status()
        print("Current status:", status)

        # Set discharge mode to 30%
        inverter.set_charge_discharge('discharge', 30)
        print("Discharge mode set to 30%")

        # Get current status
        status = inverter.get_status()
        print("Current status:", status)

    except Exception as e:
        print("An error occurred:", e)