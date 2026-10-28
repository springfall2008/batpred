# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""
Per-instance environment configuration tests.

PREDBAT_APPS_FILE and PREDBAT_SECRETS_FILE are read once, in Hass.__init__, into the instance
(self.apps_file / self.secrets_file). The environment stays the default, but a host can pass
apps_file= / secrets_file= so two engines in one process each load their own configuration
without either one mutating os.environ.
"""

import os
import tempfile

import yaml

import hass as hass_module
from hass import Hass
from predbat import PredBat
from utils import load_apps_yaml

ENV_KEYS = ("PREDBAT_APPS_FILE", "PREDBAT_SECRETS_FILE")


def _save_env():
    return {key: os.environ.get(key) for key in ENV_KEYS}


def _restore_env(saved):
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _write_instance_files(tmpdir, name, secret_value):
    """Write an apps.yaml with a !secret reference and a matching secrets.yaml, return both paths."""
    secrets_path = os.path.join(tmpdir, "{}_secrets.yaml".format(name))
    with open(secrets_path, "w") as handle:
        yaml.dump({"fox_api_key": secret_value}, handle)
    apps_path = os.path.join(tmpdir, "{}_apps.yaml".format(name))
    with open(apps_path, "w") as handle:
        handle.write("pred_bat:\n  module: predbat\n  class: PredBat\n  instance_name: {}\n  fox_key: !secret fox_api_key\n".format(name))
    return apps_path, secrets_path


def test_env_is_default_source():
    """With no override, Hass reads PREDBAT_APPS_FILE / PREDBAT_SECRETS_FILE from the environment, once."""
    print("**** Running test_env_is_default_source ****")
    failed = False
    saved = _save_env()
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            apps_path, secrets_path = _write_instance_files(tmpdir, "env_default", "env-secret")
            os.environ["PREDBAT_APPS_FILE"] = apps_path
            os.environ["PREDBAT_SECRETS_FILE"] = secrets_path

            h = Hass()
            if h.apps_file != apps_path or h.secrets_file != secrets_path:
                print("ERROR: expected env paths on the instance, got {} / {}".format(h.apps_file, h.secrets_file))
                failed = True
            if h.args.get("instance_name") != "env_default" or h.args.get("fox_key") != "env-secret":
                print("ERROR: expected config from the env-named files, got {}".format(h.args))
                failed = True
            if hass_module.resolve_apps_yaml_path(h.apps_file) != os.path.abspath(apps_path):
                print("ERROR: resolve_apps_yaml_path(instance) did not return the env-named file")
                failed = True
            if hass_module.resolve_apps_yaml_path() != os.path.abspath(apps_path):
                print("ERROR: resolve_apps_yaml_path() with no argument should still fall back to the env")
                failed = True

            # Read once: changing the environment after construction does not move the instance
            os.environ["PREDBAT_APPS_FILE"] = os.path.join(tmpdir, "moved_apps.yaml")
            os.environ["PREDBAT_SECRETS_FILE"] = os.path.join(tmpdir, "moved_secrets.yaml")
            if h.apps_file != apps_path or h.secrets_file != secrets_path:
                print("ERROR: instance paths followed a later env change, got {} / {}".format(h.apps_file, h.secrets_file))
                failed = True
            if hass_module.resolve_apps_yaml_path(h.apps_file) != os.path.abspath(apps_path):
                print("ERROR: resolve_apps_yaml_path(instance) followed a later env change")
                failed = True

        # Unset env keeps the historic defaults: ./apps.yaml and the secrets.yaml search path
        os.environ.pop("PREDBAT_APPS_FILE", None)
        os.environ.pop("PREDBAT_SECRETS_FILE", None)
        saved_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "apps.yaml"), "w") as handle:
                handle.write("pred_bat:\n  instance_name: cwd_default\n")
            os.chdir(tmpdir)
            try:
                h = Hass()
                h.logfile.close()
            finally:
                os.chdir(saved_cwd)
        if h.apps_file != "apps.yaml" or h.secrets_file is not None:
            print("ERROR: unset env should default to apps.yaml / no secrets override, got {} / {}".format(h.apps_file, h.secrets_file))
            failed = True
        if h.args.get("instance_name") != "cwd_default":
            print("ERROR: unset env should load ./apps.yaml, got {}".format(h.args))
            failed = True
    finally:
        _restore_env(saved)

    if not failed:
        print("**** test_env_is_default_source PASSED ****")
    return failed


def test_two_instances_in_one_process():
    """Two engines constructed in one process each load their own apps.yaml and secrets.yaml."""
    print("**** Running test_two_instances_in_one_process ****")
    failed = False
    saved = _save_env()
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            env_apps, env_secrets = _write_instance_files(tmpdir, "from_env", "env-secret")
            apps_a, secrets_a = _write_instance_files(tmpdir, "tenant_a", "secret-a")
            apps_b, secrets_b = _write_instance_files(tmpdir, "tenant_b", "secret-b")
            os.environ["PREDBAT_APPS_FILE"] = env_apps
            os.environ["PREDBAT_SECRETS_FILE"] = env_secrets

            engine_a = PredBat(apps_file=apps_a, secrets_file=secrets_a)
            engine_b = PredBat(apps_file=apps_b, secrets_file=secrets_b)

            for engine, name, secret, apps_path in ((engine_a, "tenant_a", "secret-a", apps_a), (engine_b, "tenant_b", "secret-b", apps_b)):
                if engine.args.get("instance_name") != name:
                    print("ERROR: {} loaded the wrong apps.yaml: {}".format(name, engine.args.get("instance_name")))
                    failed = True
                if engine.args.get("fox_key") != secret or engine.secrets.get("fox_api_key") != secret:
                    print("ERROR: {} resolved the wrong secrets: {} / {}".format(name, engine.args.get("fox_key"), engine.secrets))
                    failed = True
                if hass_module.resolve_apps_yaml_path(engine.apps_file) != os.path.abspath(apps_path):
                    print("ERROR: {} resolves the wrong apps.yaml path".format(name))
                    failed = True

            # Neither construction touched the process environment
            if os.environ.get("PREDBAT_APPS_FILE") != env_apps or os.environ.get("PREDBAT_SECRETS_FILE") != env_secrets:
                print("ERROR: constructing an overridden engine changed the process environment")
                failed = True

            # The env default still works alongside the overridden engines
            engine_env = PredBat()
            if engine_env.args.get("instance_name") != "from_env" or engine_env.args.get("fox_key") != "env-secret":
                print("ERROR: default engine should load the env-named config, got {}".format(engine_env.args))
                failed = True

            # A per-instance consumer of the path: check_apps_yaml_secrets re-reads each engine's own
            # file. Give tenant_a an inline credential and leave tenant_b on !secret.
            with open(apps_a, "w") as handle:
                handle.write("pred_bat:\n  module: predbat\n  class: PredBat\n  instance_name: tenant_a\n  fox_key: inline-plain-text\n")
            engine_a.check_apps_yaml_secrets()
            engine_b.check_apps_yaml_secrets()
            if "fox_key" not in engine_a.arg_warnings:
                print("ERROR: tenant_a should warn about its inline fox_key, got {}".format(engine_a.arg_warnings))
                failed = True
            if engine_b.arg_warnings:
                print("ERROR: tenant_b uses !secret and should not warn, got {}".format(engine_b.arg_warnings))
                failed = True
    finally:
        _restore_env(saved)

    if not failed:
        print("**** test_two_instances_in_one_process PASSED ****")
    return failed


def test_secret_tag_not_registered_globally():
    """load_apps_yaml resolves !secret with a private loader and leaves the shared yaml.SafeLoader untouched."""
    print("**** Running test_secret_tag_not_registered_globally ****")
    failed = False
    constructors_before = dict(yaml.SafeLoader.yaml_constructors)
    with tempfile.TemporaryDirectory() as tmpdir:
        secrets_path = os.path.join(tmpdir, "secrets.yaml")
        with open(secrets_path, "w") as handle:
            yaml.dump({"fox_api_key": "secret-value", "mpan": 1234567890}, handle)
        apps_path = os.path.join(tmpdir, "apps.yaml")
        with open(apps_path, "w") as handle:
            handle.write(
                "pred_bat:\n"
                "  module: predbat\n"
                "  fox_key: !secret fox_api_key\n"
                "  missing_key: !secret not_in_secrets\n"
                "  rates:\n"
                "    - mpan: !secret mpan\n"
                "      enabled: true\n"
                "      threshold: 0.5\n"
                "  times: &times ['00:30:00', '05:30:00']\n"
                "  copy_times: *times\n"
                "  empty:\n"
            )

        args, secrets = load_apps_yaml(apps_file=apps_path, secrets_file=secrets_path, log=lambda message, **kwargs: None)
        expected = {
            "module": "predbat",
            "fox_key": "secret-value",
            "missing_key": None,
            "rates": [{"mpan": 1234567890, "enabled": True, "threshold": 0.5}],
            "times": ["00:30:00", "05:30:00"],
            "copy_times": ["00:30:00", "05:30:00"],
            "empty": None,
        }
        if args != expected:
            print("ERROR: apps.yaml parsed differently, expected {} got {}".format(expected, args))
            failed = True
        if secrets != {"fox_api_key": "secret-value", "mpan": 1234567890}:
            print("ERROR: unexpected secrets {}".format(secrets))
            failed = True

    if "!secret" in yaml.SafeLoader.yaml_constructors:
        print("ERROR: load_apps_yaml registered !secret on the shared yaml.SafeLoader")
        failed = True
    if dict(yaml.SafeLoader.yaml_constructors) != constructors_before:
        print("ERROR: load_apps_yaml changed the shared yaml.SafeLoader constructors")
        failed = True

    # A plain safe_load elsewhere in the process no longer resolves !secret against the last-loaded secrets
    try:
        leaked = yaml.safe_load("key: !secret fox_api_key\n")
        print("ERROR: yaml.safe_load resolved !secret after load_apps_yaml, got {}".format(leaked))
        failed = True
    except yaml.constructor.ConstructorError:
        pass

    if not failed:
        print("**** test_secret_tag_not_registered_globally PASSED ****")
    return failed


def run_env_config_tests(my_predbat=None):
    """Run all per-instance environment configuration tests."""
    failed = test_env_is_default_source()
    failed |= test_two_instances_in_one_process()
    failed |= test_secret_tag_not_registered_globally()
    return failed
