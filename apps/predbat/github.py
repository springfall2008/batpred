# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2025-2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from datetime import datetime, timedelta, timezone
import requests
from ha import run_async
from download import DEFAULT_PREDBAT_REPOSITORY, resolve_predbat_repository
from utils import dp1
from predbat import THIS_VERSION


class GitHub:
    """GitHub Mixin for Predbat self-update and release discovery."""

    def get_predbat_repository(self):
        """Return the GitHub repository used for main-branch self-update operations.

        This override is applied to ``download_predbat_version('main')`` only.
        Release discovery and tagged-version updates remain pinned to
        ``DEFAULT_PREDBAT_REPOSITORY``.
        """
        repository = self.get_arg("predbat_repository", default="", indirect=False)
        if isinstance(repository, str):
            repository = repository.strip()
        return resolve_predbat_repository(repository=repository)

    def _load_github_url_cache_from_storage(self):
        """Load the persisted GitHub URL cache from storage on the first call."""
        components = getattr(self, "components", None)
        storage = components.get_component("storage") if components else None
        if self.github_url_cache_loaded or not storage:
            return
        try:
            self.github_url_cache_loaded = True
            data = run_async(storage.load("predbat", "github_url_cache"))
            if isinstance(data, dict):
                self.github_url_cache = data
                self.log("Loaded GitHub URL cache from storage ({} entries)".format(len(data)))
            elif data is not None:
                self.log("Warn: GitHub URL cache in storage has unexpected type {}, ignoring".format(type(data).__name__))
        except Exception as e:
            self.log("Warn: Failed to load GitHub URL cache from storage: {}".format(e))

    def _save_github_url_cache_to_storage(self):
        """Persist the GitHub URL cache to storage so it survives restarts."""
        components = getattr(self, "components", None)
        storage = components.get_component("storage") if components else None
        if not storage:
            return
        now = datetime.now()
        stale = [url for url, entry in self.github_url_cache.items() if not isinstance(entry, dict) or not isinstance(entry.get("stamp"), datetime) or (now - entry["stamp"]) > timedelta(hours=24)]
        for url in stale:
            del self.github_url_cache[url]
        if stale:
            self.log("Pruned {} stale GitHub URL cache entries".format(len(stale)))
        try:
            run_async(storage.save("predbat", "github_url_cache", self.github_url_cache, format="yaml", expiry=datetime.now(timezone.utc) + timedelta(hours=8)))
        except Exception as e:
            self.log("Warn: Failed to save GitHub URL cache to storage: {}".format(e))

    def download_predbat_releases_url(self, url):
        """
        Download release data from GitHub, but use the cache for 2 hours
        """
        self._load_github_url_cache_from_storage()

        # Check the cache first
        now = datetime.now()
        if url in self.github_url_cache:
            entry = self.github_url_cache[url]
            stamp = entry.get("stamp")
            pdata = entry.get("data")
            if stamp is not None and pdata is not None:
                age = now - stamp
                if age.total_seconds() < (120 * 60):
                    self.log("Using cached GitHub data for {} age {} minutes".format(url, dp1(age.total_seconds() / 60)))
                    return pdata

        try:
            r = requests.get(url)
        except Exception:
            self.log("Warn: Unable to load data from GitHub URL: {}".format(url))
            return []

        try:
            pdata = r.json()
        except requests.exceptions.JSONDecodeError:
            self.log("Warn: Unable to decode data from GitHub URL: {}".format(url))
            return []

        # Save to in-memory cache and persist to storage
        self.github_url_cache[url] = {}
        self.github_url_cache[url]["stamp"] = now
        self.github_url_cache[url]["data"] = pdata
        self._save_github_url_cache_to_storage()

        return pdata

    def download_predbat_releases(self):
        """
        Download release data
        """
        global PREDBAT_UPDATE_OPTIONS
        auto_update = self.get_arg("auto_update")
        repository = DEFAULT_PREDBAT_REPOSITORY
        url = "https://api.github.com/repos/{}/releases".format(repository)
        data = self.download_predbat_releases_url(url)
        self.releases = {}
        if data and isinstance(data, list):
            found_latest = False
            found_latest_beta = False

            release = data[0]
            self.releases["this"] = THIS_VERSION
            self.releases["latest"] = "Unknown"
            self.releases["latest_beta"] = "Unknown"

            for release in data:
                if release.get("tag_name", "Unknown") == THIS_VERSION:
                    self.releases["this_name"] = release.get("name", "Unknown")
                    self.releases["this_body"] = release.get("body", "Unknown")

                if not found_latest and not release.get("prerelease", True):
                    self.releases["latest"] = release.get("tag_name", "Unknown")
                    self.releases["latest_name"] = release.get("name", "Unknown")
                    self.releases["latest_body"] = release.get("body", "Unknown")
                    found_latest = True

                if not found_latest_beta:
                    self.releases["latest_beta"] = release.get("tag_name", "Unknown")
                    self.releases["latest_beta_name"] = release.get("name", "Unknown")
                    self.releases["latest_beta_body"] = release.get("body", "Unknown")
                    found_latest_beta = True

            self.log("Predbat {} repository {} version {} currently running, latest version is {}, latest beta is {}".format(__file__, repository, self.releases["this"], self.releases["latest"], self.releases["latest_beta"]))
            PREDBAT_UPDATE_OPTIONS = ["main"]
            this_tag = THIS_VERSION
            new_version = False

            # Find all versions for the dropdown menu
            for release in data:
                prerelease = release.get("prerelease", True)
                tag = release.get("tag_name", None)
                if tag:
                    if prerelease:
                        full_name = tag + " (beta) " + release.get("name", "")
                    else:
                        full_name = tag + " " + release.get("name", "")
                    PREDBAT_UPDATE_OPTIONS.append(full_name)
                    if this_tag == tag:
                        this_tag = full_name
                if len(PREDBAT_UPDATE_OPTIONS) >= 25:
                    break

            # Update the drop down menu
            item = self.config_index.get("update", None)
            if item:
                item["options"] = PREDBAT_UPDATE_OPTIONS
                item["value"] = None

            # See what version we are on and auto-update
            if this_tag not in PREDBAT_UPDATE_OPTIONS:
                this_tag = this_tag + " (?)"
                PREDBAT_UPDATE_OPTIONS.append(this_tag)
                self.log("Autoupdate: Currently on unknown version {}".format(this_tag))
            else:
                if self.releases["this"] == self.releases["latest"]:
                    self.log("Autoupdate: Currently up to date")
                elif self.releases["this"] == self.releases["latest_beta"]:
                    self.log("Autoupdate: Currently on latest beta")
                else:
                    latest_version = self.releases["latest"] + " " + self.releases["latest_name"]
                    if auto_update:
                        self.log("Autoupdate: There is an update pending {} - auto update triggered!".format(latest_version))
                        self.download_predbat_version(latest_version)
                    else:
                        self.log("Autoupdate: There is an update pending {} - auto update is off".format(latest_version))
                        new_version = True

            # Refresh the list
            self.expose_config("update", this_tag)
            self.expose_config("version", new_version, force=True)

        else:
            self.log("Warn: Unable to download Predbat version information from GitHub, return code: {}".format(data))
            self.expose_config("version", False, force=True)

        return self.releases
