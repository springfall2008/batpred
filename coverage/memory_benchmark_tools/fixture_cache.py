# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Export the memory benchmark's cache entries from a live Predbat cache/ directory.

The benchmark runs the forecast (forecast.solar) and Octopus components with no network, so everything they would
fetch has to be in the storage cache, fresh at T0. This copies the live entries, re-keys the forecast ones under
neutral request URLs (the real ones carry the home's location and a key, and the component logs every URL it serves),
scrubs identifiers, and writes every entry's meta as created at T0.
"""

import ast
import hashlib
import json
import re
import types
from datetime import timedelta
from pathlib import Path

import yaml

import scrub

PREDBAT_DIR = Path(__file__).resolve().parent.parent.parent / "apps" / "predbat"

# The postcode lookup the forecast component makes (solcast.py download_forecast_solar_data)
POSTCODE_URL = "https://api.postcodes.io/postcodes/{}"

# Defaults the forecast component applies to a plane with a setting missing (solcast.py download_forecast_solar_data)
PLANE_DEFAULTS = {"latitude": 51.5072, "longitude": -0.1276, "declination": 35.0, "azimuth": 180.0, "kwp": 3.0, "efficiency": 0.95}

# Live cache entry families copied for the Octopus component and Predbat's own Octopus and future-rate fetches
OCTOPUS_FAMILIES = ("octopus_", "futurerate_", "agilepredict_")
ACCOUNT_STEM = "octopus_user_account"

STORAGE_EXTENSIONS = {"yaml": "yaml", "json": "json", "text": "txt"}
SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_\-]")


def _safe_name(value):
    """Sanitise a module or key for a file name exactly as storage.py does."""
    return SAFE_NAME_RE.sub("_", value)


def write_entry(cache_dir, module, key, data, fmt, t0):
    """Write one cache entry and its meta sidecar as storage.py's StorageLocalFiles.save does, created at t0."""
    cache_dir = Path(cache_dir)
    stem = "{}_{}".format(_safe_name(module), _safe_name(key))
    text = json.dumps(data) if fmt == "json" else yaml.safe_dump(data)
    (cache_dir / "{}.{}".format(stem, STORAGE_EXTENSIONS[fmt])).write_text(text)
    _write_meta(cache_dir / "{}.meta".format(stem), fmt, module, t0)


def _write_meta(path, fmt, module, t0):
    """Write a meta sidecar created at t0 and expiring 7 days later."""
    meta = {"format": fmt, "expiry": (t0 + timedelta(days=7)).isoformat(), "module": module, "created": t0.isoformat()}
    Path(path).write_text(json.dumps(meta))


def _class_body(tree, name):
    """Return the body of the top-level class called name in an ast tree."""
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node.body
    raise ValueError("class {} not found".format(name))


def _function(nodes, name, namespace):
    """Compile the function called name from ast nodes (decorators dropped) and return it."""
    for node in nodes:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            node = ast.FunctionDef(**{**{field: getattr(node, field) for field in node._fields}, "decorator_list": []})
            module = ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[]))
            exec(compile(module, "<{}>".format(name), "exec"), namespace)  # noqa: S102 - repo source, not input
            return namespace[name]
    raise ValueError("function {} not found".format(name))


def solcast_source():
    """Return the forecast component's URL templates, cache key and azimuth conversion, read from the repo's source.

    They are read with ast rather than imported, because importing solcast.py pulls in most of the app; reading them
    keeps this tool in step with the component's own code.
    """
    tree = ast.parse((PREDBAT_DIR / "solcast.py").read_text())
    body = _class_body(tree, "SolarAPI")
    urls = {}
    for node in body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and node.targets[0].id.startswith("URL_"):
            urls[node.targets[0].id] = ast.literal_eval(node.value)
    cache_key = _function(body, "solar_cache_key", {"hashlib": hashlib})
    convert_azimuth = _function(ast.parse((PREDBAT_DIR / "solar_model.py").read_text()).body, "convert_azimuth", {})
    return types.SimpleNamespace(url_free=urls["URL_FREE"], url_personal=urls["URL_PERSONAL"], url_personal_dual=urls["URL_PERSONAL_DUAL"], cache_key=cache_key, convert_azimuth=convert_azimuth)


def forecast_requests(config, locations, source):
    """Return the (url, params) forecast.solar requests the component makes for a forecast_solar config.

    locations maps a plane's postcode to the (latitude, longitude) its lookup returns. Mirrors the component's grouping:
    consecutive planes with the same api_key and location are fetched in one dual-plane request.
    """
    planes = []
    for plane in config if isinstance(config, list) else [config]:
        lat = plane.get("latitude", PLANE_DEFAULTS["latitude"])
        lon = plane.get("longitude", PLANE_DEFAULTS["longitude"])
        if plane.get("postcode") and plane["postcode"] in locations:
            lat, lon = locations[plane["postcode"]]
        az = plane.get("azimuth", PLANE_DEFAULTS["azimuth"])
        if not plane.get("azimuth_zero_south", False):
            az = source.convert_azimuth(az)
        kwp = plane.get("kwp", PLANE_DEFAULTS["kwp"]) * plane.get("efficiency", PLANE_DEFAULTS["efficiency"])
        planes.append({"lat": lat, "lon": lon, "dec": plane.get("declination", PLANE_DEFAULTS["declination"]), "az": az, "kwp": kwp, "api_key": plane.get("api_key")})

    requests = []
    index = 0
    while index < len(planes):
        plane = planes[index]
        following = planes[index + 1] if index + 1 < len(planes) else None
        if plane["api_key"] and following and following["api_key"] == plane["api_key"] and (following["lat"], following["lon"]) == (plane["lat"], plane["lon"]):
            url = source.url_personal_dual.format(api_key=plane["api_key"], lat=plane["lat"], lon=plane["lon"], dec1=plane["dec"], az1=plane["az"], kwp1=plane["kwp"], dec2=following["dec"], az2=following["az"], kwp2=following["kwp"])
            index += 2
        else:
            if plane["api_key"]:
                url = source.url_personal.format(api_key=plane["api_key"], lat=plane["lat"], lon=plane["lon"], dec=plane["dec"], az=plane["az"], kwp=plane["kwp"])
            else:
                url = source.url_free.format(lat=plane["lat"], lon=plane["lon"], dec=plane["dec"], az=plane["az"], kwp=plane["kwp"])
            index += 1
        requests.append((url, {}))
    return requests


def _read_json(path):
    """Return the JSON in path, or None when it does not exist."""
    path = Path(path)
    return json.loads(path.read_text()) if path.exists() else None


def _newest_forecasts(live_cache, count):
    """Return the count newest forecast.solar responses (those with result.watts) in the live cache, newest first."""
    found = []
    for path in Path(live_cache).glob("solar_*.json"):
        data = _read_json(path)
        if isinstance(data, dict) and isinstance(data.get("result"), dict) and "watts" in data["result"]:
            meta = _read_json(path.with_suffix(".meta")) or {}
            found.append((meta.get("created", ""), data))
    found.sort(key=lambda pair: pair[0], reverse=True)
    responses = [data for _, data in found[:count]]
    return responses + [None] * (count - len(responses))


def export_forecast_cache(live_cache, out_cache, t0, live_config):
    """Write the forecast.solar entries the benchmark's forecast component needs, under neutral request values.

    live_config is the live apps.yaml forecast_solar list. Returns (neutral forecast_solar config for the generated
    apps.yaml, the real values that must not appear anywhere in the fixture). Raises ValueError when the live cache
    has no response for one of the configured requests, or the neutral planes would group differently.
    """
    live_cache, out_cache = Path(live_cache), Path(out_cache)
    source = solcast_source()
    planes = live_config if isinstance(live_config, list) else [live_config]
    secrets = []
    locations = {}
    for plane in planes:
        postcode = plane.get("postcode")
        if postcode:
            secrets.append(postcode)
            lookup = _read_json(live_cache / "solar_{}.json".format(source.cache_key(POSTCODE_URL.format(postcode), {})))
            result = (lookup or {}).get("result") or {}
            if "latitude" in result and "longitude" in result:
                locations[postcode] = (result["latitude"], result["longitude"])
        if isinstance(plane.get("api_key"), str):
            secrets.append(plane["api_key"])
        for field in ("latitude", "longitude"):
            if field in plane:
                secrets.append(str(plane[field]))
    secrets.extend(str(value) for location in locations.values() for value in location)

    neutral = []
    for plane in planes:
        copy = {key: value for key, value in plane.items() if key not in ("latitude", "longitude", "postcode", "api_key")}
        copy["postcode"] = scrub.FAKE_POSTCODE
        if plane.get("api_key"):
            copy["api_key"] = scrub.FAKE_FORECAST_KEY
        neutral.append(copy)

    real_requests = forecast_requests(planes, locations, source)
    neutral_requests = forecast_requests(neutral, {scrub.FAKE_POSTCODE: (scrub.FAKE_LATITUDE, scrub.FAKE_LONGITUDE)}, source)
    if len(real_requests) != len(neutral_requests):
        raise ValueError("the neutral forecast_solar planes group into {} requests but the live ones into {}".format(len(neutral_requests), len(real_requests)))

    if all(isinstance(plane.get("api_key"), (str, type(None))) for plane in planes):
        # The real request keys can be computed, so each response is found under the key the component stored it at
        responses = [_read_json(live_cache / "solar_{}.json".format(source.cache_key(url, params))) for url, params in real_requests]
    else:
        # An api_key is a !secret reference, whose value this tool does not read: take the newest cached forecast.solar
        # responses instead, one per request
        responses = _newest_forecasts(live_cache, len(real_requests))
    if not all(responses):
        raise ValueError("the live cache has no forecast.solar response for one of the configured requests")

    for data, (neutral_url, neutral_params) in zip(responses, neutral_requests):
        info = (data.get("message") or {}).get("info") or {}
        if info.get("place"):
            secrets.append(info["place"])
        if info:
            info.update({"latitude": scrub.FAKE_LATITUDE, "longitude": scrub.FAKE_LONGITUDE, "place": "Benchmark", "distance": 0})
        write_entry(out_cache, "solar", source.cache_key(neutral_url, neutral_params), data, "json", t0)

    lookup = {"status": 200, "result": {"postcode": scrub.FAKE_POSTCODE, "latitude": scrub.FAKE_LATITUDE, "longitude": scrub.FAKE_LONGITUDE}}
    write_entry(out_cache, "solar", source.cache_key(POSTCODE_URL.format(scrub.FAKE_POSTCODE), {}), lookup, "json", t0)
    return neutral, sorted(set(secret for secret in secrets if secret))


def export_octopus_cache(live_cache, out_cache, t0, tokens=None):
    """Copy the Octopus (and Predbat's own Octopus and future-rate) cache entries with identifiers scrubbed.

    The account entry loses its Kraken token and has its fetch times set to t0, so the component treats its account and
    tariffs as fresh; every copied entry's meta is created at t0. Returns {real value: fake value}, the token included
    (mapped to ""), for the caller's leak check and database export.

    tokens are the device ids read off entity names (see scrub.name_id_tokens), applied to every copied entry. A device id
    whose last segment is one of them gets a fake ending in that token's fake, because the component names a device's
    entities after that segment and the database's names were renamed with it. Every value is also scrubbed lower-cased
    and with dashes as underscores, as integrations rewrite them.
    """
    live_cache, out_cache = Path(live_cache), Path(out_cache)
    replacements = {}
    account_path = live_cache / "{}.yaml".format(ACCOUNT_STEM)
    if account_path.exists():
        account = yaml.safe_load(account_path.read_text()) or {}
        replacements = scrub.collect_sensitive(account)
        for real, fake in list(replacements.items()):
            segment = real.rsplit("-", 1)[-1].lower()
            if fake.startswith("BENCH-DEVICE") and "-" in real and tokens and segment in tokens:
                replacements[real] = "BENCH-DEVICE-" + tokens[segment]
        if account.get("kraken_token"):
            replacements[account["kraken_token"]] = ""
        replacements = scrub.with_case_variants(replacements)
        account = scrub.replace_everywhere(account, replacements)
        account["kraken_token"] = None
        account["tariff_fetched_at"] = t0.replace(tzinfo=None)
        account["device_fetched_at"] = t0.replace(tzinfo=None)
        write_entry(out_cache, "octopus_user", "account", account, "yaml", t0)
        if tokens:
            path = out_cache / "{}.yaml".format(ACCOUNT_STEM)
            path.write_text(scrub.replace_tokens(path.read_text(), tokens))

    ordered = sorted(((real, fake) for real, fake in replacements.items() if real), key=lambda pair: len(pair[0]), reverse=True)
    for meta_path in sorted(live_cache.glob("*.meta")):
        stem = meta_path.name[: -len(".meta")]
        if stem == ACCOUNT_STEM or not stem.startswith(OCTOPUS_FAMILIES):
            continue
        meta = _read_json(meta_path) or {}
        fmt = meta.get("format", "yaml")
        data_path = live_cache / "{}.{}".format(stem, STORAGE_EXTENSIONS.get(fmt, fmt))
        if not data_path.exists():
            continue
        text = data_path.read_text()
        for real, fake in ordered:
            text = text.replace(real, fake)
        if tokens:
            text = scrub.replace_tokens(text, tokens)
        (out_cache / data_path.name).write_text(text)
        _write_meta(out_cache / meta_path.name, fmt, meta.get("module", stem.split("_")[0]), t0)
    return replacements
