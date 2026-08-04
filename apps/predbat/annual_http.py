# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------

"""Shared JSON-over-HTTP helper for the annual prediction tool's default fetchers.

``annual_weather.py``, ``annual_tariff.py`` and ``annual_load.py`` each had their own
default JSON fetcher, differing only in timeout, headers and the module-specific prefix
on the warning log line. A third near-copy (this module collapses the first two plus
``OctopusConsumptionLoadProfile``'s) was the trigger to extract one shared implementation
instead of a fourth review flagging the same duplication again.
"""

import aiohttp


async def fetch_json(url, log, log_prefix, headers, timeout_seconds, session=None):
    """Download and decode one JSON document, returning None on any failure.

    ``log_prefix`` names the caller in the warning line (e.g. "Open-Meteo request",
    "Octopus rate request", "Octopus consumption request"), so the three callers keep
    their own distinguishable log wording despite sharing this implementation.

    ``session`` is optional: ``OctopusConsumptionLoadProfile`` passes one in so a single
    TCP connection is reused across its meter-resolution call and its paginated
    consumption downloads, exactly as it did before this helper was extracted. When
    omitted, a fresh session is opened and closed around this one request, matching what
    the weather and tariff fetchers did on their own.
    """

    async def _request(active_session):
        """Issue the GET on the given session and return its decoded JSON, or None on a bad status."""
        async with active_session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout_seconds)) as response:
            if response.status not in [200, 201]:
                log("Warn: Annual: {} to {} returned {}".format(log_prefix, url, response.status))
                return None
            return await response.json()

    try:
        if session is not None:
            return await _request(session)
        async with aiohttp.ClientSession() as new_session:
            return await _request(new_session)
    except (aiohttp.ClientError, ValueError, TimeoutError) as error:
        log("Warn: Annual: {} to {} failed: {}".format(log_prefix, url, error))
        return None
