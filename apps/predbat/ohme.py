import aiohttp
import logging
import json
from time import time
from datetime import datetime, timedelta
from homeassistant.helpers.entity import DeviceInfo
from .const import DOMAIN, USER_AGENT, INTEGRATION_VERSION
from .utils import time_next_occurs

_LOGGER = logging.getLogger(__name__)

GOOGLE_API_KEY = "AIzaSyC8ZeZngm33tpOXLpbXeKfwtyZ1WrkbdBY"


class OhmeApiClient:
    """API client for Ohme EV chargers."""

    def __init__(self, email, password):
        if email is None or password is None:
            raise Exception("Credentials not provided")

        # Credentials from configuration
        self.email = email
        self._password = password

        # Charger and its capabilities
        self._device_info = None
        self._capabilities = {}
        self._ct_connected = False
        self._provision_date = None
        self._disable_cap = False
        self._solar_capable = False

        # Authentication
        self._token_birth = 0
        self._token = None
        self._refresh_token = None

        # User info
        self._user_id = ""
        self.serial = ""

        # Cache the last rule to use when we disable max charge or change schedule
        self._last_rule = {}

        # Sessions
        timeout = aiohttp.ClientTimeout(total=10)
        self._session = aiohttp.ClientSession(
            base_url="https://api.ohme.io", timeout=timeout)
        self._auth_session = aiohttp.ClientSession(timeout=timeout)

    # Auth methods

    async def async_create_session(self):
        """Refresh the user auth token from the stored credentials."""
        async with self._auth_session.post(
            f"https://www.googleapis.com/identitytoolkit/v3/relyingparty/verifyPassword?key={GOOGLE_API_KEY}",
            data={"email": self.email, "password": self._password,
                  "returnSecureToken": True}
        ) as resp:
            if resp.status != 200:
                return None

            resp_json = await resp.json()
            self._token_birth = time()
            self._token = resp_json['idToken']
            self._refresh_token = resp_json['refreshToken']
            return True

    async def async_refresh_session(self):
        """Refresh auth token if needed."""
        if self._token is None:
            return await self.async_create_session()

        # Don't refresh token unless its over 45 mins old
        if time() - self._token_birth < 2700:
            return

        async with self._auth_session.post(
            f"https://securetoken.googleapis.com/v1/token?key={GOOGLE_API_KEY}",
            data={"grantType": "refresh_token",
                  "refreshToken": self._refresh_token}
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                msg = f"Ohme auth refresh error: {text}"
                _LOGGER.error(msg)
                raise AuthException(msg)

            resp_json = await resp.json()
            self._token_birth = time()
            self._token = resp_json['id_token']
            self._refresh_token = resp_json['refresh_token']
            return True

    # Internal methods

    async def _handle_api_error(self, url, resp):
        """Raise an exception if API response failed."""
        if resp.status != 200:
            text = await resp.text()
            msg = f"Ohme API response error: {url}, {resp.status}; {text}"
            _LOGGER.error(msg)
            raise ApiException(msg)

    def _get_headers(self):
        """Get auth and content-type headers"""
        return {
            "Authorization": "Firebase %s" % self._token,
            "Content-Type": "application/json",
            "User-Agent": f"{USER_AGENT}/{INTEGRATION_VERSION}"
        }

    async def _post_request(self, url, skip_json=False, data=None):
        """Make a POST request."""
        await self.async_refresh_session()
        async with self._session.post(
            url,
            data=data,
            headers=self._get_headers()
        ) as resp:
            _LOGGER.debug(f"POST request to {url}, status code {resp.status}")
            await self._handle_api_error(url, resp)

            if skip_json:
                return await resp.text()

            return await resp.json()

    async def _put_request(self, url, data=None):
        """Make a PUT request."""
        await self.async_refresh_session()
        async with self._session.put(
            url,
            data=json.dumps(data),
            headers=self._get_headers()
        ) as resp:
            _LOGGER.debug(f"PUT request to {url}, status code {resp.status}")
            await self._handle_api_error(url, resp)

            return True

    async def _get_request(self, url):
        """Make a GET request."""
        await self.async_refresh_session()
        async with self._session.get(
            url,
            headers=self._get_headers()
        ) as resp:
            _LOGGER.debug(f"GET request to {url}, status code {resp.status}")
            await self._handle_api_error(url, resp)

            return await resp.json()

    # Simple getters

    def ct_connected(self):
        """Is CT clamp connected."""
        return self._ct_connected

    def is_capable(self, capability):
        """Return whether or not this model has a given capability."""
        return bool(self._capabilities[capability])

    def solar_capable(self):
        return self._solar_capable

    def cap_available(self):
        return not self._disable_cap

    def get_device_info(self):
        return self._device_info

    # Push methods

    async def async_pause_charge(self):
        """Pause an ongoing charge"""
        result = await self._post_request(f"/v1/chargeSessions/{self.serial}/stop", skip_json=True)
        return bool(result)

    async def async_resume_charge(self):
        """Resume a paused charge"""
        result = await self._post_request(f"/v1/chargeSessions/{self.serial}/resume", skip_json=True)
        return bool(result)

    async def async_approve_charge(self):
        """Approve a charge"""
        result = await self._put_request(f"/v1/chargeSessions/{self.serial}/approve?approve=true")
        return bool(result)

    async def async_max_charge(self, state=True):
        """Enable max charge"""
        result = await self._put_request(f"/v1/chargeSessions/{self.serial}/rule?maxCharge=" + str(state).lower())
        return bool(result)

    async def async_apply_session_rule(self, max_price=None, target_time=None, target_percent=None, pre_condition=None, pre_condition_length=None):
        """Apply rule to ongoing charge/stop max charge."""
        # Check every property. If we've provided it, use that. If not, use the existing.
        if max_price is None:
            if 'settings' in self._last_rule and self._last_rule['settings'] is not None and len(self._last_rule['settings']) > 1:
                max_price = self._last_rule['settings'][0]['enabled']
            else:
                max_price = False

        if target_percent is None:
            target_percent = self._last_rule['targetPercent'] if 'targetPercent' in self._last_rule else 80

        if pre_condition is None:
            pre_condition = self._last_rule['preconditioningEnabled'] if 'preconditioningEnabled' in self._last_rule else False

        if pre_condition_length is None:
            pre_condition_length = self._last_rule['preconditionLengthMins'] if (
                'preconditionLengthMins' in self._last_rule and self._last_rule['preconditionLengthMins'] is not None) else 30

        if target_time is None:
            # Default to 9am
            target_time = self._last_rule['targetTime'] if 'targetTime' in self._last_rule else 32400
            target_time = (target_time // 3600,
                           (target_time % 3600) // 60)

        target_ts = int(time_next_occurs(
            target_time[0], target_time[1]).timestamp() * 1000)

        # Convert these to string form
        max_price = 'true' if max_price else 'false'
        pre_condition = 'true' if pre_condition else 'false'

        result = await self._put_request(f"/v1/chargeSessions/{self.serial}/rule?enableMaxPrice={max_price}&targetTs={target_ts}&enablePreconditioning={pre_condition}&toPercent={target_percent}&preconditionLengthMins={pre_condition_length}")
        return bool(result)

    async def async_change_price_cap(self, enabled=None, cap=None):
        """Change price cap settings."""
        settings = await self._get_request("/v1/users/me/settings")
        if enabled is not None:
            settings['chargeSettings'][0]['enabled'] = enabled

        if cap is not None:
            settings['chargeSettings'][0]['value'] = cap

        result = await self._put_request("/v1/users/me/settings", data=settings)
        return bool(result)

    async def async_get_schedule(self):
        """Get the first schedule."""
        schedules = await self._get_request("/v1/chargeRules")

        return schedules[0] if len(schedules) > 0 else None

    async def async_update_schedule(self, target_percent=None, target_time=None, pre_condition=None, pre_condition_length=None):
        """Update the first listed schedule."""
        rule = await self.async_get_schedule()

        # Account for user having no rules
        if not rule:
            return None

        # Update percent and time if provided
        if target_percent is not None:
            rule['targetPercent'] = target_percent
        if target_time is not None:
            rule['targetTime'] = (target_time[0] * 3600) + \
                (target_time[1] * 60)

        # Update pre-conditioning if provided
        if pre_condition is not None:
            rule['preconditioningEnabled'] = pre_condition
        if pre_condition_length is not None:
            rule['preconditionLengthMins'] = pre_condition_length

        await self._put_request(f"/v1/chargeRules/{rule['id']}", data=rule)
        return True

    async def async_set_configuration_value(self, values):
        """Set a configuration value or values."""
        result = await self._put_request(f"/v1/chargeDevices/{self.serial}/appSettings", data=values)
        return bool(result)

    # Pull methods

    async def async_get_charge_sessions(self, is_retry=False):
        """Try to fetch charge sessions endpoint.
           If we get a non 200 response, refresh auth token and try again"""
        resp = await self._get_request('/v1/chargeSessions')
        resp = resp[0]

        # Cache the current rule if we are given it
        if resp["mode"] == "SMART_CHARGE" and 'appliedRule' in resp:
            self._last_rule = resp["appliedRule"]

        return resp

    async def async_get_account_info(self):
        resp = await self._get_request('/v1/users/me/account')

        return resp

    async def async_update_device_info(self, is_retry=False):
        """Update _device_info with our charger model."""
        resp = await self.async_get_account_info()

        device = resp['chargeDevices'][0]

        self._capabilities = device['modelCapabilities']
        self._user_id = resp['user']['id']
        self.serial = device['id']
        self._provision_date = device['provisioningTs']

        self._device_info = DeviceInfo(
            identifiers={(DOMAIN, f"ohme_charger_{self.serial}")},
            name=device['modelTypeDisplayName'],
            manufacturer="Ohme",
            model=device['modelTypeDisplayName'].replace("Ohme ", ""),
            sw_version=device['firmwareVersionLabel'],
            serial_number=self.serial
        )


        if resp['tariff'] is not None and resp['tariff']['dsrTariff']:
            self._disable_cap = True

        solar_modes = device['modelCapabilities']['solarModes']
        if isinstance(solar_modes, list) and len(solar_modes) == 1:
            self._solar_capable = True

        return True

    async def async_get_advanced_settings(self):
        """Get advanced settings (mainly for CT clamp reading)"""
        resp = await self._get_request(f"/v1/chargeDevices/{self.serial}/advancedSettings")

        # If we ever get a reading above 0, assume CT connected
        if resp['clampAmps'] and resp['clampAmps'] > 0:
            self._ct_connected = True

        return resp


# Exceptions
class ApiException(Exception):
    ...


class AuthException(ApiException):
    ...