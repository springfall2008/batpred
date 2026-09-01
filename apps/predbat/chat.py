# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
"""The Predbat AI chat agent component.

Presents Predbat's tools directly to an OpenRouter-served model as function-calling tools and
runs the agentic loop. Deliberately holds no loop-bound state: the turn itself runs on whichever
event loop invoked it, which in practice is the web component's, while this component's own
thread only flushes and prunes. See spec section 3.

Prompt caching only ever helps a request whose prefix is byte-identical to one already seen -
OpenAI, DeepSeek, Grok, Groq and Gemini 2.5 cache such a prefix automatically once it is stable;
Anthropic and Qwen additionally need an explicit cache_control breakpoint, which build_messages()
sends unconditionally on the system message's one content block. There is deliberately no
model-family detection here: a request against a real OpenRouter account measured a Claude model's
cost per turn drop to roughly a ninth once the breakpoint was in place, while a free-tier model
with no cache_control support was measured returning an identical response whether the field was
present or not - a provider that does not understand the field just ignores it, so sending it
blind costs nothing and still helps every provider that does support it, not only the ones this
component happens to know about. That is why the system prompt is built once per conversation and
stored verbatim rather than rebuilt from live state on every turn - see build_system_prompt() and
ChatAgent._frozen_system_prompt() - and why the one instruction that still varies within a turn
(the title reminder) is appended to that turn's user message instead of folded into the prompt:
see build_messages() and append_title_instruction().
"""

import aiohttp
import asyncio
import functools
import hashlib
import ipaddress
import json
import traceback
import threading
import time
from datetime import datetime, timedelta
from urllib.parse import urlparse

from agent_tools import TOOL_DEFS, PredbatTools, openai_tool_list
from component_base import ComponentBase
from chat_store import APPROVAL_APPROVED, APPROVAL_REJECTED, ConversationStore, NEW_CONVERSATION_TITLE, derive_title, extract_cached_tokens, trim_history
from chat_tools import APPS_YAML_RESTART_WARNING, CHAT_TOOL_DEFS, DEFAULT_FETCH_ALLOWLIST, fetch_url, read_docs, read_source, search_docs, search_source, set_apps_config
from utils import SECRET_MASK, is_secret_key, parse_yaml_path, resolve_nested_yaml_value

# Shown when a turn is attempted with no model chosen anywhere. A provider's own 'model' is
# optional, so this is the ordinary state of a fresh install rather than a misconfiguration - the
# wording points at the picker, which is where the user can fix it without touching apps.yaml.
# Shown when no endpoint is configured, or the configured one is missing what it needs. Names the
# block to add rather than a single key, because the failure is "nothing is set up" rather than
# "one setting is wrong".
NO_PROVIDER_MESSAGE = "No chat provider is configured. Add one to the 'chat: providers:' block in apps.yaml - for example " "'openrouter: {api_key: ...}' for a hosted model, or 'ollama: {url: http://localhost:11434/v1}' to use a local one."

NO_MODEL_MESSAGE = "No model has been selected. Choose one from the model picker below the message box, or set a default for this provider in Settings."

# How much of a provider's raw error text to keep. Enough to carry a real message, bounded because
# a failing provider can return an entire HTML page, and this is stored on the conversation and
# pushed down the SSE stream.
PROVIDER_DETAIL_MAX = 1000

EVENT_BUFFER_MAX = 2000

# How long a fetched model catalogue is trusted before list_models() refreshes it. list_models()
# passes this plus a further 60 minutes as the stale ceiling - the window during which a stale copy
# is still served while one caller refreshes in the background - so the effective outer limit is 25
# hours, not 24. OpenRouter's catalogue changes rarely enough that once a day is plenty either way.
MODEL_CACHE_MINUTES = 1440

# The same, for an endpoint on your own machine. A day is right for a hosted catalogue of several
# hundred models fetched over the internet; it is wrong for a local server, where the catalogue is
# whatever you have pulled and changes precisely because you just changed it. Pulling a model and
# then not seeing it in the picker until tomorrow is the bug this exists to prevent. Not zero,
# because the list still costs one /api/show per model to enrich - about 0.8s for ten models -
# which is not worth repeating on every page load.
LOCAL_MODEL_CACHE_MINUTES = 5

# How long past its own deadline a turn must go before its slot is assumed abandoned. Only a
# component restart can strand a slot, and that is rare - so the grace period is generous.
STALE_TURN_GRACE_SECONDS = 60

# How long a write tool waits for a user's confirm/reject answer before it is treated as declined,
# and how often await_confirmation polls for it. Polling rather than an asyncio.Event because the
# answer arrives from the web thread's loop while the turn runs on the component's own loop, and
# an Event is bound to whichever loop created it.
CONFIRM_TIMEOUT_SECONDS = 300
CONFIRM_POLL_SECONDS = 0.2

# Retry policy for one model completion. Live traffic against a free-tier model showed roughly one
# in three completions fail mid-stream with a transient provider-side error, so a failure worth
# retrying gets ten attempts total (the first try plus nine retries) before the turn gives up.
# COMPLETION_RETRY_DELAYS_SECONDS[0] is the backoff before the second attempt,
# COMPLETION_RETRY_DELAYS_SECONDS[1] before the third, and COMPLETION_RETRY_DELAYS_SECONDS[2] (5s)
# repeats for every attempt after that - see retry_delay_for()'s docstring for the mechanism.
COMPLETION_MAX_ATTEMPTS = 10
COMPLETION_RETRY_DELAYS_SECONDS = (1, 3, 5)

# A 429 is treated quite differently from a provider error. It is not a fault: the request was
# refused because a quota window is full, and the only thing that fixes it is waiting - which the
# free-tier models this is most often pointed at do constantly. Giving up after three attempts
# meant a user hit "Rate limited by OpenRouter, gave up after 3 attempts" within about six
# seconds and had to retype the question.
#
# So rate limits get their own budget: 20 attempts on a schedule that starts short, in case the
# window has already rolled, and settles at 30s - a rate-limit window that has not cleared in ten
# seconds will not clear in eleven, and polling harder only spends the quota being waited for.
# The last delay repeats for every attempt beyond the list, so the full run is roughly eight
# minutes, comfortably inside the 1800s turn budget that bounds it either way.
# Ceiling on an honoured Retry-After. A provider asking for longer than this gets waited on for
# this instead: parking a turn for ten minutes on one header, only for the turn deadline to kill
# it with nothing to show, is worse than trying again sooner and failing honestly.
RETRY_AFTER_MAX_SECONDS = 60

COMPLETION_RATE_LIMIT_MAX_ATTEMPTS = 20
COMPLETION_RATE_LIMIT_DELAYS_SECONDS = (1, 5, 10, 30)

# error.code / error.metadata.error_type values that mark a mid-stream "error" chunk as the
# provider's own trouble rather than something wrong with the request itself - see
# classify_completion_failure() for where these are read from.
RETRYABLE_PROVIDER_ERROR_MARKERS = ("provider_unavailable", "server_error")

# The exact provider_message _run_completion raises when a chunk ends a choice with finish_reason
# "error" but carries no top-level "error" object of its own to explain why. Shared between that
# raise site and classify_completion_failure() so the two can never drift out of step.
FINISH_REASON_ERROR_MESSAGE = "The provider ended the response with an error"

# The short, user-facing reason shown for a completion that produced neither a visible answer nor
# a tool call - see is_empty_completion(). Deliberately distinct from empty_completion_message()'s
# longer wording: this is what appears in a 'retry' event's reason field, next to the attempt
# count, not the fuller explanation stored as the turn's final error if every attempt is empty.
EMPTY_COMPLETION_RETRY_REASON = "Empty response from the model"

PRIMER = """You are an assistant built into Predbat, a home battery optimisation system that plans when to charge and discharge a household battery based on electricity rates, solar forecasts and historical load.
The person you are talking to owns this system and is looking at its web interface.

Answer concisely and quote the user's real values rather than generalities. Call a tool rather than guessing: the tools read this specific installation. Never invent an entity name; look it up with get_entities or get_config.

Do not answer configuration questions from memory. Predbat changes continuously - settings are added, renamed and removed between releases - so whatever you learned during training describes some older version and may name settings that no longer exist,
or miss ones that do. Anything you recall is a hint about where to look, never an answer. Check before you answer: search_docs then read_docs for how to configure something, and search_source then read_source for what the code actually does.
Use read_docs rather than fetch_url for documentation - it returns the one section you asked for, where fetching the page returns all of it. Both read the exact version running here, so they are the only authority on it.
If the documentation and your recollection disagree, the documentation is right.

For inverter configuration the user must pick either a) A Cloud integration built into Predbat, usually supports automatic config or b) A local Home Assistant configuration. Do not confuse the two options.
For car charging the car can either be controlled by Octopus Intelligent GO (Or similar EDF/EON smart charging) or by Predbat's own control. Do not confuse the two options.

Predbat has two separate kinds of setting, and they are changed by different tools. Getting this wrong silently does nothing:

- Live settings are Home Assistant entities such as switch.predbat_expert_mode. Read them with get_config and change them with set_config. They take effect immediately.
- apps.yaml settings are the installation's configuration file - inverter and sensor wiring, car and solar setup, API keys. Read them with get_apps_config and change them with set_apps_config, which rewrites the file and restarts Predbat.

If a name is not in get_config it is not a live setting, so do not pass it to set_config. Names like car_charging_exclusive, num_cars, forecast_solar and the inverter and sensor entity names all live in apps.yaml.

Prefer the snapshot below for simple facts about the setup, and reach for a tool when the answer needs current values, detail the snapshot lacks, or anything the user is about to act on."""


# The LLM endpoint providers this understands, and what each one needs done differently. Kept as
# a table rather than branches through the code so adding one is a single entry here: as more
# providers appear, "which is this?" gets harder to answer in scattered conditionals.
#
#   needs_key       - a hosted endpoint that will reject an unauthenticated request
#   openrouter_ext  - OpenRouter's own extensions: usage.include, the web plugin, and the pricing
#                     and supported_parameters fields on its model catalogue
#   stream_usage    - the standard OpenAI stream_options.include_usage, which is how everyone
#                     else reports token counts (OpenRouter ignores it, Ollama honours it)
#   ollama_details  - talk to Ollama's own API rather than the OpenAI-compatible one: /api/tags
#                     for the model list, which is the only place cloud models are marked as
#                     such, and /api/show per model for the capabilities and context length
#                     /v1/models omits. True only for Ollama itself - 'local' is the generic
#                     OpenAI-compatible option (llama.cpp, LM Studio and the rest), and those
#                     serve neither endpoint, so asking would cost a 404 for the whole catalogue
#   metered         - somebody bills per token. False for an endpoint running on your own
#                     hardware, where every model is free however little it says about pricing
PROVIDERS = {
    "openrouter": {"needs_key": True, "openrouter_ext": True, "stream_usage": False, "ollama_details": False, "metered": True},
    "ollama": {"needs_key": False, "openrouter_ext": False, "stream_usage": True, "ollama_details": True, "metered": False},
    "openai": {"needs_key": True, "openrouter_ext": False, "stream_usage": True, "ollama_details": False, "metered": True},
    "local": {"needs_key": False, "openrouter_ext": False, "stream_usage": True, "ollama_details": False, "metered": False},
}

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# Hosts that mean "this endpoint is on your own machine or your own network", so no key is
# expected. Anything else is assumed to be somebody's hosted service until told otherwise.
LOCAL_HOST_NAMES = ("localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal")
LOCAL_HOST_SUFFIXES = (".local", ".lan", ".internal", ".home", ".arpa")

# Ollama's default port. Used only as a hint by detect_provider() - a local endpoint on this port
# is almost certainly Ollama, which unlocks the richer model list.
OLLAMA_DEFAULT_PORT = 11434

# Where each provider lives when the user names a type but no url - the point of naming a type.
PROVIDER_DEFAULT_URLS = {
    "openrouter": OPENROUTER_BASE_URL,
    "ollama": "http://localhost:11434/v1",
    "openai": "https://api.openai.com/v1",
}

# What the Settings dialog offers when somebody adds a provider of each type, where that differs
# from what an under-specified apps.yaml entry resolves to. The two answer different questions:
# PROVIDER_DEFAULT_URLS answers "what did this existing entry mean", and changing it would
# silently repoint installs that are working today, while this answers "what is most likely to
# work for somebody starting now".
#
# For Ollama those answers genuinely differ. localhost is the conventional address and the right
# reading of an entry that names no url, but it is almost never right for a new install: Predbat
# runs inside its Home Assistant container, where localhost is the container and not the machine
# the user is thinking of. Ollama's own cloud works from there without any of that, so it is what
# the form suggests - with a note saying how to point it at your own server instead.
PROVIDER_SETUP_HINTS = {
    "ollama": {
        "url": "https://ollama.com/v1",
        "model": "gpt-oss:120b",
        "note": "This is Ollama's own cloud, which needs an API key from ollama.com. If you run Ollama yourself, replace the URL with your server's address - for example http://localhost:11434/v1 when it is on this machine, or http://192.168.1.50:11434/v1 elsewhere on your network - and leave the key empty.",
    },
}

# The model a provider starts on when the entry names none, so a freshly configured endpoint can
# answer immediately rather than sending the user to the picker first. Per type, because a model
# id only means anything to the endpoint serving it.
#
# The OpenRouter default is a free model deliberately: a default that quietly bills someone on
# their first question is a poor introduction. There is no sensible generic default for an
# arbitrary OpenAI-compatible endpoint, so those have none and the picker is the way in.
PROVIDER_DEFAULT_MODELS = {
    "openrouter": "nvidia/nemotron-3-ultra-550b-a55b:free",
    "ollama": "gpt-oss:20b",
}

# Timeout for probing an endpoint the user is still typing into (see probe_models). Shorter
# than the catalogue fetch's own 30s: this one has somebody sat watching a dialog waiting for it,
# and a wrong URL should say so quickly rather than look like a hang.
PROBE_TIMEOUT_SECONDS = 20

# Per-model timeout for the /api/show enrichment. Short because it is a local call on the machine
# next to Predbat: if it is slow, the catalogue is better off without the extra detail than the
# picker is waiting for it.
OLLAMA_DETAIL_TIMEOUT_SECONDS = 10


def is_local_endpoint(url):
    """Return True if a URL points at this machine or this network rather than a hosted service."""
    try:
        host = (urlparse(str(url or "")).hostname or "").lower()
    except (ValueError, UnicodeError):
        return False
    if not host:
        return False
    if host in LOCAL_HOST_NAMES or host.endswith(LOCAL_HOST_SUFFIXES):
        return True
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        # Not an IP literal, and not a name that looks local.
        return False


def detect_provider(url):
    """Work out which provider a URL is, for a provider entry that names no type.

    Deliberately decided from the URL alone rather than by probing: this runs during component
    start-up, and a start-up that makes a network call is a start-up that can hang. A user whose
    endpoint the guess gets wrong sets the entry's 'type' explicitly, which is what it is for.
    """
    text = str(url or "")
    if not text:
        return "openrouter"
    try:
        parsed = urlparse(text)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except (ValueError, UnicodeError):
        return "openrouter"
    if "openrouter.ai" in host:
        return "openrouter"
    if is_local_endpoint(text):
        # Port is the only signal distinguishing Ollama from llama.cpp, LM Studio and the rest,
        # all of which serve the same OpenAI-compatible API. Guessing wrong only costs the extra
        # model detail, not the ability to chat.
        return "ollama" if port == OLLAMA_DEFAULT_PORT else "local"
    return "openai"


def build_providers(block):
    """Turn the providers sub-block of apps.yaml's chat: block into usable provider entries.

    Each entry is {name, type, url, api_key, configured}. The dict key is the user's own name for
    an endpoint, not the provider type - so two Ollama servers, or two OpenRouter accounts, are
    just two entries. type is optional and falls back to the name when the name is itself a known
    provider, then to detection from the url.
    """
    entries = []
    for name, settings in (block or {}).items() if isinstance(block, dict) else []:
        # Skip anything that is not a provider entry - see extract_providers(), which is what
        # decides that; this loop only ever receives entries.
        if not isinstance(settings, dict):
            continue
        url = str(settings.get("url") or "").strip()
        api_type = str(settings.get("type") or "").strip().lower()
        if not api_type:
            api_type = str(name).lower() if str(name).lower() in PROVIDERS else "auto"
        resolved_name, resolved = resolve_provider(api_type, url or default_url_for(api_type))
        entries.append(
            {
                "name": str(name),
                "type": resolved_name,
                "url": url or default_url_for(resolved_name),
                "api_key": settings.get("api_key") or None,
                # A model id only means anything to the provider that serves it -
                # openai/gpt-4o-mini does not exist on Ollama and qwen3:latest does not exist on
                # OpenRouter - so the default model belongs to the entry, not the block.
                "model": settings.get("model") or default_model_for(resolved_name),
                "settings": resolved,
            }
        )

    for entry in entries:
        # Usable means "a turn sent to this would not fail immediately": a hosted endpoint needs
        # its key, a local one only needs to be pointed at.
        entry["configured"] = bool(entry["url"]) and (bool(entry["api_key"]) or not entry["settings"]["needs_key"])
    return entries


def extract_providers(block, log=None):
    """Return the provider entries from an apps.yaml chat: block.

    Normally they live under `providers:`. They are also accepted written directly in the block,
    because that was the documented shape briefly before the nesting was added, and an install
    written against it would otherwise report no providers at all and fail with a bare 401.

    Unambiguous either way: every other setting in the block is a scalar or a list, so a
    dict-valued key that is not `providers` can only be a provider entry.
    """
    if not isinstance(block, dict):
        return {}
    nested = block.get("providers")
    if isinstance(nested, dict) and nested:
        return nested
    loose = {name: value for name, value in block.items() if name != "providers" and isinstance(value, dict)}
    if loose and log:
        log("Warn: chat providers are written directly in the chat: block - move them under 'providers:' as documented; they still work for now")
    return loose


def default_url_for(provider_name):
    """Return the endpoint a provider type uses when the user gave no url."""
    return PROVIDER_DEFAULT_URLS.get(provider_name, OPENROUTER_BASE_URL)


def default_model_for(provider_name):
    """Return the model a provider type starts on when the entry names none, or None."""
    return PROVIDER_DEFAULT_MODELS.get(provider_name)


def resolve_provider(api_type, url):
    """Return the provider settings for a configured type, resolving 'auto' from the URL.

    Whether somebody bills for a provider is settled here rather than in the table, because for
    Ollama it depends on where it is: the same software is free on your own machine and paid for
    at ollama.com, which serves the identical API and publishes no prices either way. Marking a
    hosted one unmetered would offer every model in it under "show only free models", which is a
    filter asking specifically not to be billed.
    """
    name = str(api_type or "auto").strip().lower()
    if name in ("", "auto"):
        name = detect_provider(url)
    settings = PROVIDERS.get(name, PROVIDERS["openai"])
    if not settings["metered"] and not is_local_endpoint(url):
        settings = dict(settings, metered=True)
    return name, settings


def conversation_model_for(meta, active_provider):
    """Return a conversation's model override, but only for the provider it was chosen on.

    A model id only means anything to the endpoint serving it, so an override picked on OpenRouter
    is not an override at all once Ollama is answering - it is a name that endpoint has never heard
    of, and every turn fails on it until somebody notices.

    An override with no provider recorded predates this being tracked. Treated as belonging to
    nobody, which costs nothing: the same choice sits in the store's per-provider memory, written
    at the moment the override was.
    """
    return meta.get("model") if meta.get("model_provider") == active_provider else None


# Bumped whenever a stored catalogue entry gains or changes a field, so an existing cache is
# ignored rather than served without it. Without this a fix to how entries are built takes up to a
# day to reach anyone who has used the picker recently - the catalogue is cached daily - and does
# so silently, which is the worst way for a user to experience a fix.
MODEL_CACHE_VERSION = 2


def model_cache_name(base_url):
    """Return the cache filename holding one endpoint's model catalogue.

    Keyed on the endpoint, because that is whose catalogue it is. A single shared name - which is
    what this was - hands OpenRouter's model list straight to Ollama the moment somebody switches
    provider, and keeps doing it for a day, since the catalogue is cached daily. The user sees a
    picker full of models their endpoint has never heard of and no way to tell why.

    Hashed because a URL is not a filename, and truncated because the only collisions that matter
    are between one user's own handful of endpoints.
    """
    return "models_v{}_{}".format(MODEL_CACHE_VERSION, hashlib.md5(str(base_url or "").encode("utf-8")).hexdigest()[:16])


def is_free_model(provider, prompt_price, completion_price, remote=False):
    """Return whether a model costs nothing to run on a given provider.

    Decided here rather than in the browser because only this side knows what the endpoint is. A
    local endpoint publishes no pricing at all - Ollama's /v1/models has no pricing field - and the
    picker read that absence as "not known to be free", so ticking "show only free models" emptied
    the list of a server where every model is free by definition. The box is ticked by default, so
    that was the first thing a new Ollama user saw.

    On a metered endpoint the prices decide it, and an absent price stays not-free: OpenRouter's
    routing models quote -1 because their cost depends on where they route, and offering those as
    free would bill somebody who asked not to be.

    A remote model is the exception on an unmetered endpoint. Ollama's cloud models run on
    somebody else's hardware and are paid for, even though the server offering them is your own
    and publishes no prices - so "local endpoint, therefore free" is exactly wrong for them.
    """
    if remote:
        return False
    if not provider.get("metered", True):
        return True
    if prompt_price is None or completion_price is None:
        return False
    try:
        return float(prompt_price) == 0 and float(completion_price) == 0
    except (TypeError, ValueError):
        return False


def ollama_native_url(base_url, path):
    """Return an Ollama native API URL for an endpoint given as its OpenAI-compatible base.

    Only a trailing "/v1" is stripped, and only as a whole path segment. Splitting on "/v1"
    anywhere in the string matches inside a longer segment - "https://host/v1beta" becomes
    "https://host" - and then asks a quite different server for its models.

    A base URL that is simply wrong is left wrong: "https://ollama.com/api/v1" still yields
    "https://ollama.com/api/api/tags", which 404s. That is deliberate. Quietly repairing a
    mistyped URL would hide the mistake until something subtler went wrong with it, and the 404
    names the URL it tried, which is what tells the user which part they got wrong.
    """
    base = str(base_url or "").rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return "{}{}".format(base, path)


def ollama_tags_to_catalogue(payload):
    """Turn Ollama's /api/tags response into the catalogue shape the rest of this expects.

    Read for the model list because it is the only endpoint that says which models are cloud ones:
    remote_model and remote_host appear here and not in the OpenAI-compatible /v1/models shim.
    Both endpoints list the same models - measured against a live server with a cloud model pulled,
    /v1/models does include it - so this is not about which models are visible. It is about telling
    a cloud model from a local one, which decides whether it is free.

    Anything running elsewhere is marked remote: a cloud model is somebody else's hardware, paid
    for, whatever the local server says about pricing. Reading the native endpoint for a native
    provider also matches what already happens per model, since the capability and context-length
    enrichment has always come from /api/show.
    """
    models = []
    for entry in (payload or {}).get("models") or []:
        name = entry.get("model") or entry.get("name")
        if not name:
            continue
        models.append({"id": name, "remote": bool(entry.get("remote_model") or entry.get("remote_host"))})
    return {"data": models}


def model_catalogue_headers(api_key):
    """Return the headers for a /models request, omitting the bearer token when there is no key.

    Sending `Authorization: Bearer None` - which is what formatting an absent key produces - is
    not the same as sending nothing. Ollama ignores it, but an endpoint that validates the header
    it was given answers 401 to a request that would have succeeded unauthenticated, which reads
    as "your key is wrong" for an endpoint that never wanted a key in the first place.
    """
    return {"Authorization": "Bearer {}".format(api_key)} if api_key else {}


# Defaults for the apps.yaml chat: block, kept here rather than in the component registry so each
# one sits beside the code that reads it and the reasoning that chose it. A setting absent from
# the block, or explicitly null, takes the value here.
CHAT_DEFAULTS = {
    "max_tokens": 0,
    "max_tool_rounds": 32,
    "max_history": 0,
    "max_conversations": 20,
    "expiry_days": 30,
    "turn_timeout": 1800,
    "request_timeout": 300,
}


class TurnStopped(RuntimeError):
    """Raised when a turn must end while a completion is still streaming.

    Deliberately not a ChatRequestError: _run_completion_with_retry catches those and tries again,
    and retrying a turn the user just stopped is the opposite of what they asked for. Carries the
    text already streamed so the transcript can keep it rather than leaving the browser holding a
    bubble that vanishes on the next reload.
    """

    def __init__(self, content="", reason="stopped"):
        """Hold what had been streamed when the stop landed, and what caused it."""
        super().__init__("The turn was stopped")
        self.content = content
        self.reason = reason


class ChatBusyError(RuntimeError):
    """Raised when a turn is requested while another is already running."""


class AgentNotReadyError(RuntimeError):
    """Raised when work is handed to the component before its event loop exists."""


class ChatRequestError(RuntimeError):
    """Raised when the chat-completions endpoint returns a non-200 response, or fails mid-stream.

    Also covers a mid-stream provider failure: a 200 SSE stream that carries a top-level "error"
    object on one of its chunks, or ends a choice with finish_reason "error". Those have no real
    HTTP status of their own, so provider_message carries the provider's own wording straight
    through friendly() instead of being forced through the status-code branches below.
    """

    def __init__(self, status, body, provider_message=None, error_type=None, final_message=None, retry_after=None):
        """Keep the status and body so the message can name what actually went wrong.

        error_type is error.metadata.error_type from a mid-stream error chunk, when the provider
        sent one - see classify_completion_failure(), which reads it alongside status and
        provider_message to decide whether a failure is worth retrying. final_message is set only
        by the retry wrapper once every attempt has been used: it is the complete text friendly()
        should return verbatim, bypassing the status-code branches below entirely, so a give-up
        message is never built by feeding an already-formatted string back through them.
        """
        super().__init__("HTTP {}: {}".format(status, str(body)[:500]))
        self.status = status
        self.body = body
        self.provider_message = provider_message
        self.error_type = error_type
        self.final_message = final_message
        # Seconds the provider asked us to wait, from the Retry-After header, or None.
        self.retry_after = retry_after

    def detail(self):
        """Return the provider's own error payload, for the transcript and the saved conversation.

        friendly() deliberately says one short line, but OpenRouter's generic wrappers -
        "Provider returned error" is the common one - carry the actual cause in the error object's
        metadata: which provider failed, and its raw response. Without that a user has nothing to
        act on and nothing to report. Returns None when there is no more to say than friendly()
        already did, so a caller can skip the detail block entirely.
        """
        parts = []
        if self.status:
            parts.append("code {}".format(self.status))
        if self.error_type:
            parts.append("type {}".format(self.error_type))

        raw = self.body
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (TypeError, ValueError):
                raw = None
        if isinstance(raw, dict):
            metadata = raw.get("metadata")
            if isinstance(metadata, dict):
                if metadata.get("provider_name"):
                    parts.append("provider {}".format(metadata["provider_name"]))
                # OpenRouter documents provider_code as the original upstream error code, which is
                # often the only thing that distinguishes two failures sharing one HTTP status.
                if metadata.get("provider_code"):
                    parts.append("provider code {}".format(metadata["provider_code"]))
                # The provider's own words, which is the part worth reading.
                provider_raw = metadata.get("raw")
                if provider_raw:
                    text = provider_raw if isinstance(provider_raw, str) else json.dumps(provider_raw)
                    parts.append(text[:PROVIDER_DETAIL_MAX])

        if not parts:
            # Nothing structured to show, so fall back to the body itself rather than nothing -
            # truncated, because a provider can return a whole HTML error page.
            body_text = self.body if isinstance(self.body, str) else json.dumps(self.body)
            if body_text and body_text not in ("null", "{}"):
                return body_text[:PROVIDER_DETAIL_MAX]
            return None
        return " | ".join(parts)

    def friendly(self):
        """Return a message worth showing a user rather than a raw HTTP error."""
        if self.final_message:
            return self.final_message
        if self.provider_message:
            return "OpenRouter reported an error: {}".format(self.provider_message)
        if self.status == 401:
            return "The provider rejected the API key - check the api_key for this provider in apps.yaml's chat: block"
        if self.status == 402:
            return "OpenRouter reports insufficient credit: {}".format(self.body[:200])
        if self.status == 429:
            return "Rate limited by OpenRouter, try again shortly"
        return "OpenRouter returned HTTP {}: {}".format(self.status, self.body[:200])


# Window dict keys whose value is minutes since local midnight rather than a quantity. Rendered as
# a weekday and clock time in the snapshot: a charge window at "start: 1590" is 26.5 hours after
# midnight, i.e. half past two tomorrow morning, which no reader works out from the number - and a
# model that does not work it out either will happily tell a user their battery charges at 15:90.
WINDOW_MINUTE_KEYS = ("start", "end", "start_orig", "end_orig")


def format_clock(minutes, midnight):
    """Render minutes-since-midnight as a short weekday and time, e.g. "Sat 02:30".

    Falls back to the raw value whenever the conversion cannot be made - a base that has not
    finished starting has no midnight, and a window key can hold None. A snapshot line that says
    something slightly less useful is always better than one that raises and takes the whole
    system prompt with it.
    """
    if midnight is None or isinstance(minutes, bool) or not isinstance(minutes, (int, float)):
        return str(minutes)
    try:
        return (midnight + timedelta(minutes=int(minutes))).strftime("%a %H:%M")
    except (ValueError, OverflowError, TypeError):
        return str(minutes)


def format_window(window, midnight):
    """Render a charge or export window with clock times instead of raw minute counts.

    Every key the window carries is kept - the extras (average, target, set) are what the model
    reasons about - but the minute-valued ones become times. Written generically rather than
    naming the fields, so a window that gains a key still renders rather than silently dropping it.
    """
    if not isinstance(window, dict):
        return str(window)
    span = "{} to {}".format(format_clock(window.get("start"), midnight), format_clock(window.get("end"), midnight))
    extras = []
    for key, value in window.items():
        if key in ("start", "end"):
            continue
        extras.append("{} {}".format(key, format_clock(value, midnight) if key in WINDOW_MINUTE_KEYS else value))
    return "{} ({})".format(span, ", ".join(extras)) if extras else span


def format_percent_of(value, total):
    """Render " (N%)" for a value against a total, or "" when that cannot be worked out.

    kWh on its own does not tell a reader whether a battery is nearly full without them dividing
    by a capacity that appears elsewhere in the line, and a reserve in kWh is close to meaningless
    without it. Returns a fragment to append rather than a number, so a caller never has to decide
    what to do about an unknown.
    """
    try:
        if not total or isinstance(value, bool) or isinstance(total, bool):
            return ""
        return " ({:.0f}%)".format(float(value) / float(total) * 100.0)
    except (TypeError, ValueError, ZeroDivisionError):
        return ""


def snapshot_version(base):
    """Return Predbat's version string for the snapshot.

    There is no instance attribute for this - the version is the module-level THIS_VERSION_DISPLAY
    in predbat.py, which is what web.py renders in its header - so reading base.this_version, as
    this used to, reported "unknown" on every real install. Imported inside the function because
    predbat.py and config.py are a circular pair that only resolves because predbat.py is imported
    first; by the time a snapshot is built it always has been.
    """
    version = getattr(base, "this_version", None)
    if version:
        return version
    try:
        from predbat import THIS_VERSION_DISPLAY

        return THIS_VERSION_DISPLAY or "unknown"
    except Exception:
        return "unknown"


def snapshot_inverter_types(base):
    """Return the inverter type(s) in use, as a readable string.

    inverter_type is a string_list in APPS_SCHEMA with one entry per inverter, read as
    get_arg("inverter_type", index=<n>) - so the unindexed read this used to do never resolved and
    always fell back to "unknown". The live Inverter objects each carry the type they actually
    resolved to, which is also what a mixed-inverter install needs: reporting one type for a system
    running two different ones would be worse than reporting none.
    """
    types = []
    for inverter in getattr(base, "inverters", None) or []:
        found = getattr(inverter, "inverter_type", None)
        if found and found not in types:
            types.append(str(found))
    if types:
        return ", ".join(types)

    # Before the inverters are built, fall back to what apps.yaml asked for.
    try:
        configured = base.get_arg("inverter_type", None, indirect=False)
    except Exception:
        configured = None
    if isinstance(configured, list):
        unique = []
        for entry in configured:
            if entry and str(entry) not in unique:
                unique.append(str(entry))
        return ", ".join(unique) if unique else "unknown"
    return str(configured) if configured else "unknown"


def parse_retry_after(value):
    """Return a Retry-After header as seconds, or None if it is absent or not a plain number.

    The header is defined as either a delay in seconds or an HTTP date. Only the numeric form is
    honoured: the date form needs the server's clock to agree with ours, and a wrong answer here
    means either hammering a limit that has not cleared or idling long past one that has. Falling
    back to the schedule is the safer of the two.
    """
    if value is None:
        return None
    try:
        seconds = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


def max_attempts_for(rate_limited):
    """Return how many attempts a failure of this kind gets."""
    return COMPLETION_RATE_LIMIT_MAX_ATTEMPTS if rate_limited else COMPLETION_MAX_ATTEMPTS


def retry_delay_for(attempt, rate_limited):
    """Return the backoff before the next attempt, in seconds.

    attempt is 1-based and counts attempts already made, so attempt 1 picks the first delay. Past
    the end of the schedule the last value repeats rather than the list being indexed off the end
    - which is what lets the rate-limit budget run to 20 attempts on a four-entry schedule.
    """
    delays = COMPLETION_RATE_LIMIT_DELAYS_SECONDS if rate_limited else COMPLETION_RETRY_DELAYS_SECONDS
    return delays[min(attempt, len(delays)) - 1]


def build_snapshot(base):
    """Render a compact description of the live system for the system prompt.

    A pure read of base's current state - this function itself keeps nothing and always reflects
    whatever is true when it is called. build_system_prompt() is the only caller that matters for
    a real turn, and it calls this exactly once per conversation, at the moment the conversation's
    first turn runs, then stores the result; see that function and the module docstring for why a
    frozen, byte-identical prompt is what actually makes caching work, and its caveat text for how
    a model is told these figures are a snapshot rather than live. Everything deeper than this
    stays behind a tool call, which is what keeps the per-turn cost bounded.
    """

    def arg(name, default=None):
        """Read one Predbat argument, tolerating a base that has not finished starting."""
        try:
            return base.get_arg(name, default)
        except Exception:
            return default

    def state(name, default=None):
        """Read one attribute off the base instance."""
        return getattr(base, name, default)

    # Despite the name, now_utc/midnight_utc are local time carrying a tz offset (predbat.py sets
    # them from datetime.now(local_tz)), so a clock time derived from them is the wall clock a
    # user reads off, not UTC.
    now = state("now_utc", None) or datetime.now()
    midnight = state("midnight_utc", None)

    lines = ["Predbat state as it was when this conversation started:"]
    # The weekday matters more than it looks: window minutes routinely run past 1440 into
    # tomorrow, so without a day the model cannot tell today's 02:30 from tomorrow's.
    lines.append("- Conversation started at: {}".format(now.strftime("%a %Y-%m-%d %H:%M:%S%z") if isinstance(now, datetime) else now))
    lines.append("- Predbat version: {}".format(snapshot_version(base)))
    lines.append("- Status: {}".format(state("current_status", "unknown")))
    lines.append("- Mode: {}".format(arg("mode", "unknown")))
    soc_kw = state("soc_kw", "unknown")
    soc_max = state("soc_max", "unknown")
    reserve = state("reserve", "unknown")
    lines.append("- SOC: {} kWh of {} kWh{}, reserve {} kWh{}".format(soc_kw, soc_max, format_percent_of(soc_kw, soc_max), reserve, format_percent_of(reserve, soc_max)))
    lines.append("- Inverters: {} of type {}".format(state("num_inverters", "unknown"), snapshot_inverter_types(base)))
    lines.append("- Cars configured: {}".format(state("num_cars", 0)))
    lines.append("- Currency: {}".format(state("currency_symbols", ["p", "£"])))
    lines.append("- Errors seen this run: {}".format(bool(state("had_errors", False))))

    rate_import = state("rate_import", {}) or {}
    rate_export = state("rate_export", {}) or {}
    minutes_now = state("minutes_now", 0) or 0
    if rate_import:
        lines.append("- Import rate now: {}".format(rate_import.get(minutes_now, "unknown")))
    if rate_export:
        lines.append("- Export rate now: {}".format(rate_export.get(minutes_now, "unknown")))

    windows = state("charge_window_best", []) or []
    if windows:
        lines.append("- Next planned charge window: {}".format(format_window(windows[0], midnight)))
    exports = state("export_window_best", []) or []
    if exports:
        lines.append("- Next planned export window: {}".format(format_window(exports[0], midnight)))

    return "\n".join(lines)


# Filled in with build_system_prompt()'s captured_at, formatted for a person rather than an
# isoformat string - "09:15 on 28 August 2026" - so the caveat reads naturally inline. Deliberately
# spells out both that the figures are frozen and where to get a live one instead: a model holding
# a stale-but-plausible number in its own frozen context, told nothing more than "may be out of
# date", tends to quote it anyway rather than re-checking - see build_system_prompt()'s docstring.
SYSTEM_PROMPT_SNAPSHOT_CAVEAT = (
    "The figures above were captured when this conversation started and are frozen for the "
    "rest of its lifetime - they are never refreshed on later turns, however long this conversation runs. "
    "Treat them only as a starting point, not as live data: they may already be wrong. Before stating a "
    "current SOC, rate, status or window, call get_status or get_plan and answer from what the tool "
    "returns, not from the snapshot above."
)


def build_system_prompt(base):
    """Build a conversation's frozen system prompt, and the moment its snapshot was captured.

    Called exactly once per conversation, when its first turn runs - see
    ChatAgent._frozen_system_prompt(), which stores the result and reuses it verbatim on every
    later turn instead of calling this again. That is what keeps the request's stable prefix
    byte-identical across turns, which is what actually lets a caching-capable provider (see the
    module docstring) hit its cache; rebuilding this fresh every turn, the way build_snapshot()
    alone used to be used, guarantees a different prefix on every single request instead.

    The obvious risk of freezing a live snapshot is a model later quoting a stale figure as
    current - SYSTEM_PROMPT_SNAPSHOT_CAVEAT exists specifically to close that: it names the exact
    capture time and points at get_status/get_plan as the source of truth, so the model has no
    excuse to treat a figure it read here as live data.

    Returns (prompt, captured_at): captured_at is exactly the timestamp build_snapshot()'s "Time
    now" line reports, read once here so the prompt's own caveat can never name a different moment.
    The caller stores captured_at.isoformat() as the conversation's system_prompt_at.
    """
    captured_at = getattr(base, "now_utc", None) or datetime.now()
    # No timestamp interpolated: the snapshot's own first line already states when it was taken,
    # with a weekday, and saying it twice in three lines spends tokens to no purpose.
    caveat = SYSTEM_PROMPT_SNAPSHOT_CAVEAT
    prompt = "\n\n".join([PRIMER, build_snapshot(base), caveat])
    return prompt, captured_at


# The cache_control breakpoint placed on the system message's one content block - see the module
# docstring for why this is sent to every provider unconditionally rather than only ones known to
# need it. "ephemeral" is the only breakpoint type OpenRouter and Anthropic define.
# Copied into each request rather than shared by reference: this dict sits inside the cached
# prefix, so a stray mutation anywhere would silently change every later request and
# invalidate caching for the whole install.
SYSTEM_PROMPT_CACHE_CONTROL = {"type": "ephemeral"}

# Appended to that turn's user message, never folded into the system prompt - see
# append_title_instruction() for why, and build_messages() for where this is used.
TITLE_INSTRUCTION = "\n\nThis conversation has no title yet. Call set_chat_title once, early in your reply, with a short descriptive title of at most 60 characters summarising what the user is asking about."


def append_title_instruction(messages):
    """Return a copy of messages with the title reminder appended to the last user message.

    Placed on the user message rather than folded into the system prompt because the system
    prompt must stay byte-identical across every turn to remain cacheable (see
    build_system_prompt() and the module docstring), while this instruction is only relevant until
    the conversation is titled and so cannot be part of that frozen prefix. Appending it after the
    stable prefix costs nothing: a caching provider still hits the cache on the unchanged part and
    only processes what follows it fresh.

    Only the copy handed to the model is touched here - the message actually stored in the
    conversation must keep the user's own words verbatim, which is build_messages()'s job, not
    this function's; corrupting the transcript with our own instruction, replayed back on every
    later turn as if the user had typed it, is exactly the failure that split responsibility
    avoids. Returns messages unchanged if there is no user message to attach it to, which should
    not happen in practice - _execute_turn always appends one before the turn loop runs.
    """
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") == "user":
            copied = list(messages)
            original = copied[index]
            copied[index] = dict(original, content=(original.get("content") or "") + TITLE_INSTRUCTION)
            return copied
    return list(messages)


def classify_completion_failure(error):
    """Classify one failed completion attempt for the retry wrapper.

    Returns (retryable, reason, rate_limited). reason is None when the failure is not retryable -
    the caller has nothing further to add - or a short, user-facing phrase otherwise, shared
    between the mid-turn 'retry' event and the eventual give-up message (via _give_up_error) so
    the two can never describe the same failure differently. rate_limited is True only for a 429,
    which the retry wrapper backs off longer for than a plain provider failure.

    Deliberately narrow: a 401 (the key is wrong) or 402 (out of credit) will be wrong again in a
    second, and a 400 (malformed request) will be malformed again - retrying either only delays
    the user for a request that cannot succeed, so both fail on the spot. Everything this function
    does not explicitly recognise - including an HTTP status it has never seen - fails on the spot
    too, rather than guessing that an unfamiliar failure is safe to retry.

    The mid-stream shape this reads (error.status carrying either an HTTP-like code such as 502 or
    a provider marker string such as "server_error", plus an optional error.error_type from
    error.metadata.error_type) is exactly what _run_completion's mid-stream "error" chunk handling
    raises - see its docstring and the live capture the task that added this was built from.
    """
    if isinstance(error, asyncio.TimeoutError):
        return True, "Timed out reaching the provider", False
    if isinstance(error, aiohttp.ClientError):
        return True, "Could not reach the provider", False
    if not isinstance(error, ChatRequestError):
        return False, None, False
    if error.status == 429:
        return True, "Rate limited by the provider", True
    if error.status in (401, 402, 400):
        return False, None, False
    if error.status in (502, 503):
        return True, "Provider overloaded", False
    marker = str(error.status if error.status is not None else "").lower()
    error_type = str(error.error_type or "").lower()
    message = str(error.provider_message or "").lower()
    if marker in RETRYABLE_PROVIDER_ERROR_MARKERS or error_type in RETRYABLE_PROVIDER_ERROR_MARKERS or "overload" in marker or "overload" in error_type or "overload" in message:
        return True, "Provider overloaded", False
    if error.provider_message == FINISH_REASON_ERROR_MESSAGE:
        return True, FINISH_REASON_ERROR_MESSAGE, False
    return False, None, False


def is_empty_completion(message):
    """Return whether a completion produced neither a visible answer nor a tool call.

    Live traffic showed this is almost always provider trouble - a reasoning model that spent its
    whole turn on reasoning and never emitted anything else - rather than the model genuinely
    declining to answer, so the retry wrapper treats it the same as any other retryable failure
    instead of ending the turn on the spot. See empty_completion_message() for the wording used
    once every attempt still comes back this way.
    """
    return not message.get("content") and not message.get("tool_calls")


def empty_completion_message(message):
    """Return the user-facing explanation for a completion with no content and no tool call.

    Names reasoning as the likely cause when the completion carried reasoning fragments - exactly
    the shape a live captured OpenRouter run showed, per is_empty_completion().
    """
    reasoned = bool(message.get("reasoning_details") or message.get("reasoning"))
    detail = " It produced only reasoning and no visible answer." if reasoned else ""
    return "The model returned no answer and no tool call.{} Try asking again.".format(detail)


class ChatAgent(ComponentBase):
    """Runs the OpenRouter-backed chat agent for the Predbat web interface."""

    # Defaults for an agent that has not been through initialize() yet - a partially built
    # instance in a test, or one inspected before start-up. Every real agent overwrites both in
    # select_provider(), from its chosen entry in the chat: providers: block. Present so reading
    # them is never an AttributeError, since
    # they are consulted on paths (list_models, the request payload) that a half-built agent can
    # still reach.
    provider_name = "openrouter"
    provider = PROVIDERS["openrouter"]

    def initialize(self, config=None):
        """Store configuration and build the conversation store and event buffer.

        turn_timeout and request_timeout bound two different things and must not be conflated:
        turn_timeout is the whole turn's budget - self.deadline, checked once per round in
        _turn_loop and again between tool calls within a round - which has to be generous enough
        to cover every round trip a multi-tool-call turn makes. request_timeout is the
        aiohttp.ClientTimeout on one completion request (see _stream_chunks), which only needs to
        be long enough to catch a single hung or very slow provider call. Sharing one value between
        the two (the previous behaviour) meant a turn with several rounds could die on its total
        budget after only two or three completions even though no single request was slow.

        max_history <= 0 (0 is the default) means unlimited - trim_history() itself implements
        that, this only stores whatever was passed through.
        """
        settings = config if isinstance(config, dict) else {}

        def setting(name):
            """Read one setting from the block, falling back to its documented default."""
            value = settings.get(name)
            return CHAT_DEFAULTS[name] if value is None else value

        self.providers = build_providers(extract_providers(settings, self.log))
        self.select_provider(None)
        self.max_tokens = setting("max_tokens")
        self.max_tool_rounds = setting("max_tool_rounds")
        # 0 means unlimited, and is also the default, so an explicit 0 and an absent value reach
        # the same place - no special handling needed. That equivalence is worth knowing if the
        # default ever changes: setting() cannot then tell "unset" from a deliberate 0.
        self.max_history = setting("max_history")
        self.turn_timeout = setting("turn_timeout")
        self.request_timeout = setting("request_timeout")
        allowlist = settings.get("fetch_allowlist")
        self.fetch_allowlist = list(allowlist) if allowlist else list(DEFAULT_FETCH_ALLOWLIST)
        self.lock = threading.Lock()
        # Set on the first run() tick, from inside this component's own thread. Everything the
        # web layer hands over is scheduled onto it; until it exists, the component is still
        # starting and handlers answer 503.
        self.loop = None
        self.events = []
        self.event_seq = 0
        self.event_base = 0
        self.active = None
        self.pending_confirm = {}
        self.warned_web_search_base_url = False
        # Why the last catalogue fetch failed, or None. Set by _fetch_model_catalogue and read by
        # the /chat/models route; initialised here so reading it is never an AttributeError.
        self.catalogue_error = None
        # Set to the turn id the user pressed Stop on. Distinct from zeroing self.deadline, which
        # only the turn loop reads: a turn parked on a confirmation is not in that loop, so
        # without this it could not be stopped at all.
        self.stop_requested = None
        self.store = ConversationStore(self.storage, self.log, max_history=self.max_history, max_conversations=setting("max_conversations"), expiry_days=setting("expiry_days"))
        self.index_loaded = False
        self.turn_counter = 0
        self.tools = PredbatTools(self.base, log_func=self.log)
        self.tool_defs_by_name = {entry["name"]: entry for entry in list(TOOL_DEFS) + list(CHAT_TOOL_DEFS)}
        # The retry wrapper's only seam onto real time - see _wait_before_retrying(). Replaced by
        # a fast recorder in tests, exactly like _stream_chunks is, so a fixture that happens to
        # trigger a retry never actually pauses the test suite for the real backoff.
        self._retry_sleep = asyncio.sleep

    def emit(self, conversation_id, event_type, data=None):
        """Append an event to the buffer and return its sequence number.

        A conversation_id of None marks a global event - busy, idle and reload - which every
        browser receives whatever conversation it is currently looking at.
        """
        with self.lock:
            self.event_seq += 1
            event = {"seq": self.event_seq, "conversation_id": conversation_id, "type": event_type, "data": data or {}}
            self.events.append(event)
            while len(self.events) > EVENT_BUFFER_MAX:
                self.events.pop(0)
                self.event_base += 1
            return self.event_seq

    def events_since(self, cursor, conversation_id):
        """Return events after cursor for one conversation, plus the new cursor and reload flag.

        Cursor replay rather than a live queue is what lets two browsers follow the same turn and
        a mid-turn reload resume: the buffer is the single source and each reader keeps a position.
        """
        with self.lock:
            cursor = int(cursor or 0)
            reload_needed = bool(self.events) and cursor < self.event_base
            selected = [event for event in self.events if event["seq"] > cursor and event["conversation_id"] in (None, conversation_id)]
            return selected, self.event_seq, reload_needed

    def pending_conversations(self):
        """Return the set of conversation ids with a write awaiting confirmation.

        pending_confirm is mutated (inserted, popped and wholesale rebuilt) from the component
        thread under self.lock - see claim/confirm handling in _run_one_tool, confirm() and
        _execute_turn's cleanup. Iterating it directly from the web thread without the lock races
        those mutations: a confirmation landing mid-iteration can raise "dictionary changed size
        during iteration" and 500 the request that called this. Snapshotting the values under the
        lock, then building the id set outside it, keeps the lock held for as short as possible.
        """
        with self.lock:
            entries = list(self.pending_confirm.values())
        return {entry["conversation_id"] for entry in entries}

    async def run_on_agent_loop(self, coro):
        """Await a coroutine on this component's own loop, from another thread's loop.

        run_coroutine_threadsafe schedules the work on the component loop and hands back a
        concurrent Future; wrap_future turns that into something the *calling* loop can await.
        The web loop therefore yields rather than blocking, and the work runs where it belongs.
        """
        loop = self.loop
        if loop is None:
            coro.close()
            raise AgentNotReadyError("The chat component has not finished starting")
        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro, loop))

    async def run(self, seconds, first):
        """Record this component's loop, load the index, then flush and stay healthy.

        No network call happens here. Validating credentials at startup would let a slow or
        unreachable OpenRouter block Predbat's boot inside wait_api_started() for ten minutes.
        """
        if self.loop is None:
            self.loop = asyncio.get_running_loop()
        if not self.index_loaded:
            try:
                await self.store.load_index()
            except Exception as error:
                self.log("Warn: chat agent could not load its conversation index: {}".format(error))
            self.index_loaded = True
            # Only now is the remembered choice readable - initialize() runs before the index is
            # loaded, so the select_provider(None) there can only pick the first usable entry.
            # Re-selecting here is what makes the Settings dialog's choice survive the restart
            # that saving apps.yaml causes. A name that no longer exists falls back the same way.
            self.select_provider(self.store.get_selected_provider())
        else:
            try:
                await self.store.flush()
            except Exception as error:
                self.log("Warn: chat agent could not flush conversations: {}".format(error))
                self.count_errors += 1
        self._release_stale_turn()
        self.update_success_timestamp()
        return True

    def _release_stale_turn(self):
        """Clear a turn slot whose coroutine died without running its own cleanup.

        A turn scheduled on this loop is killed outright if the component is stopped or restarted
        mid-turn, because asyncio.run() closes the loop and the finally in _execute_turn never
        runs, leaving the composer locked in every browser until Predbat restarts.

        The test is elapsed wall-clock against the turn's own deadline, NOT a count of quiet
        ticks. A turn emits one busy event and can then legitimately produce nothing for a minute
        or more while the model thinks, and the housekeeping tick only fires every 60 seconds - so
        a two-tick rule frees the slot of a turn that is merely slow. Waiting until the turn has
        outlived its own deadline plus a grace period means a live turn is never touched.

        A turn parked in await_confirmation is a further exception, and it does not depend on the
        defaults lining up. At the shipped values CONFIRM_TIMEOUT_SECONDS (300s) sits inside
        turn_timeout + STALE_TURN_GRACE_SECONDS (1800 + 60 = 1860s), so the arithmetic is
        comfortable - but chat_turn_timeout is user-configurable, and anything below 240s puts the
        confirmation window back outside the stale threshold. The exception is therefore written to
        hold whatever those numbers are: a user reading an Approve/Reject prompt gets a generous
        window rather than the turn's own budget for talking to the model. So a turn whose active call is still
        in self.pending_confirm is left alone regardless of elapsed time - its own timeout is
        CONFIRM_TIMEOUT_SECONDS, enforced by await_confirmation itself, not this one.
        """
        with self.lock:
            active = self.active
            if active is None:
                return
            started = active.get("started")
            if started is None or time.monotonic() - started < self.turn_timeout + STALE_TURN_GRACE_SECONDS:
                return
            turn_id = active.get("turn_id")
            if any(entry.get("turn_id") == turn_id for entry in self.pending_confirm.values()):
                return
            self.active = None
        self.log("Warn: chat turn {} outlived its {}s timeout with no cleanup - releasing the turn slot".format(turn_id, self.turn_timeout))
        self.emit(None, "idle", {})

    async def _frozen_system_prompt(self, conversation_id):
        """Return a conversation's frozen system prompt, building and storing it on first use.

        Every later turn reuses the exact stored string rather than calling build_system_prompt()
        again - that is the whole point, since a byte-identical prefix is what lets an
        automatically-caching provider actually cache it (see the module docstring), and a single
        differing byte defeats that. A conversation with no stored prompt yet - every conversation
        that predates this feature, plus a brand new one on its very first turn - gets one built
        from the live snapshot at this exact moment and stored; those are the same case, so there
        is no separate migration path to write.
        """
        stored, _ = await self.store.get_system_prompt(conversation_id)
        if stored is not None:
            return stored
        prompt, captured_at = build_system_prompt(self.base)
        self.store.set_system_prompt(conversation_id, prompt, captured_at.isoformat())
        return prompt

    async def build_messages(self, conversation_id, history):
        """Assemble the request messages: the conversation's frozen system prompt plus history.

        The system prompt itself never varies once a conversation exists - see
        _frozen_system_prompt() - so the only thing that can still change turn to turn is the
        title reminder, which is why it is appended to the outgoing copy of the last user message
        (append_title_instruction()) rather than folded in here. Everything before that point is
        therefore byte-identical across every turn of one conversation, which is the actual
        cacheable prefix a provider sees.

        The system message's content is sent as a one-element array rather than a plain string, its
        single text block carrying SYSTEM_PROMPT_CACHE_CONTROL - see the module docstring for why
        every provider gets this unconditionally. The frozen prompt itself is still stored, and read
        back here, as a plain string (_frozen_system_prompt(), ConversationStore.get/set_system_prompt())
        - this wrapping is built fresh on every call and never stored, so a human inspecting the saved
        conversation still sees plain text rather than this request-only shape.
        """
        system_prompt = await self._frozen_system_prompt(conversation_id)
        trimmed = trim_history(history, self.max_history, log=self.log)
        meta = self.store.get_meta(conversation_id) or {}
        if meta.get("title", NEW_CONVERSATION_TITLE) == NEW_CONVERSATION_TITLE:
            trimmed = append_title_instruction(trimmed)
        system_message = {"role": "system", "content": [{"type": "text", "text": system_prompt, "cache_control": dict(SYSTEM_PROMPT_CACHE_CONTROL)}]}
        return [system_message] + trimmed

    def tool_payload(self):
        """Return the tool list offered to the model: the shared tools plus the chat-only ones."""
        return openai_tool_list() + openai_tool_list(CHAT_TOOL_DEFS)

    async def _fetch_model_catalogue(self):
        """Download the model catalogue from the configured endpoint.

        Returns None rather than raising on a non-200, because Storage's caching helper treats a
        None as "nothing to cache" - which is also why probe_models() does its own request instead
        of reusing this one.

        Why it failed is recorded on self.catalogue_error on the way past, because by the time the
        answer reaches the picker there is nothing left to say: a None here and a None from an
        endpoint that simply serves no models look identical, and the picker was left telling the
        user "catalogue unavailable" and nothing else. An unreachable URL and a rejected key need
        very different things doing about them.
        """
        headers = model_catalogue_headers(self.api_key)
        timeout = aiohttp.ClientTimeout(total=30)
        native = bool(self.provider.get("ollama_details"))
        url = ollama_native_url(self.base_url, "/api/tags") if native else "{}/models".format(self.base_url)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers) as response:
                    if response.status in (401, 403):
                        self.catalogue_error = "{} rejected the API key (HTTP {})".format(self.base_url, response.status)
                        return None
                    if response.status != 200:
                        self.catalogue_error = "{} returned HTTP {}".format(url, response.status)
                        return None
                    body = await response.json()
                    return ollama_tags_to_catalogue(body) if native else body
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            self.catalogue_error = "Could not reach {}: {}".format(self.base_url, error)
            raise

    async def list_models(self):
        """Return the tool-capable models on offer, always including the configured one.

        A model with no tool support cannot drive this agent at all - it would answer from the
        snapshot alone and never call get_plan - so the picker hides them rather than letting a
        user select one and wonder why the answers got worse. The catalogue itself is cached once
        a day via Storage's stale-while-revalidate helper, under a name derived from the endpoint
        (see model_cache_name) so switching provider does not serve the previous one's list;
        it is only ever consulted to populate a dropdown and does not need to be fetched on every
        page load; a custom
        openrouter_base_url with no /models endpoint at all still works because the configured
        model is added whether or not the catalogue could be read.

        Each model carries context_length straight from OpenRouter's catalogue entry (None when the
        catalogue could not be read, or for the configured model when the catalogue has no entry
        for it) - the Chat tab's footer uses it to show how full the context window is against the
        model actually in use, alongside the token count itself; see html_chat_models() and
        renderContextUsage() in web_chat.py.
        """
        catalogue = None
        # Cleared per call: a cache hit never runs the fetch, so a reason left over from an
        # earlier failure would be reported against a catalogue that arrived perfectly well.
        self.catalogue_error = None
        storage = self.storage
        try:
            if storage:
                fresh = MODEL_CACHE_MINUTES if self.provider.get("metered", True) else LOCAL_MODEL_CACHE_MINUTES
                catalogue = await storage.fetch_cached("chat", model_cache_name(self.base_url), self._fetch_model_catalogue, fresh_minutes=fresh, stale_minutes=fresh + 60, format="json")
            else:
                catalogue = await self._fetch_model_catalogue()
        except Exception as error:
            self.log("Warn: could not fetch the model catalogue from {}: {}".format(self.base_url, error))

        return await self._catalogue_to_models(catalogue, self.provider, self.base_url, self.default_model)

    async def probe_models(self, api_type, url, api_key):
        """Return (models, error) for an endpoint that is not the active provider.

        This is what lets the Settings dialog offer a real model list for a provider that is not
        in apps.yaml yet - the catalogue has to be readable before the entry is saved, or the user
        would have to save a provider blind, restart, and only then find out what it serves.

        Deliberately uncached and deliberately not routed through _fetch_model_catalogue(): the
        endpoint being probed is one the user is still editing, so a cached answer would describe
        the previous URL, and "returned None" is not a good enough answer for a dialog. The status
        codes are separated out because they mean different things to whoever is typing - 401 is
        "your key is wrong", a connection error is "your URL is wrong".
        """
        name, provider = resolve_provider(api_type, url or default_url_for(api_type))
        base_url = str(url or default_url_for(name)).rstrip("/")
        native = bool(provider.get("ollama_details"))
        catalogue_url = ollama_native_url(base_url, "/api/tags") if native else "{}/models".format(base_url)
        timeout = aiohttp.ClientTimeout(total=PROBE_TIMEOUT_SECONDS)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(catalogue_url, headers=model_catalogue_headers(api_key)) as response:
                    if response.status in (401, 403):
                        return None, "{} rejected the API key (HTTP {})".format(base_url, response.status)
                    if response.status != 200:
                        return None, "{} returned HTTP {}".format(catalogue_url, response.status)
                    catalogue = await response.json()
                    if native:
                        catalogue = ollama_tags_to_catalogue(catalogue)
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            return None, "Could not reach {}: {}".format(base_url, error)
        except ValueError:
            return None, "{} did not return JSON - is that the right URL?".format(catalogue_url)
        models = await self._catalogue_to_models(catalogue, provider, base_url, None)
        if not models:
            return None, "{} served no tool-capable models".format(base_url)
        return models, None

    async def _catalogue_to_models(self, catalogue, provider, base_url, default_model):
        """Turn one endpoint's raw catalogue into the entries the model picker shows.

        Takes its provider, endpoint and default model as arguments rather than reading self, so
        the Settings dialog's probe of an unsaved endpoint runs through exactly the same filtering
        and enrichment as the live picker does - one behaviour, not two that drift apart.
        """
        models = []
        for entry in (catalogue or {}).get("data") or []:
            # Only OpenRouter publishes supported_parameters. Requiring it elsewhere filtered out
            # every model and left an empty picker, because "the catalogue does not say" is not
            # the same as "this model cannot use tools".
            if provider["openrouter_ext"] and "tools" not in (entry.get("supported_parameters") or []):
                continue
            pricing = entry.get("pricing") or {}
            models.append(
                {
                    "id": entry.get("id"),
                    "name": entry.get("name") or entry.get("id"),
                    "prompt_price": pricing.get("prompt"),
                    "completion_price": pricing.get("completion"),
                    "context_length": entry.get("context_length"),
                    "remote": bool(entry.get("remote")),
                    "free": is_free_model(provider, pricing.get("prompt"), pricing.get("completion"), remote=bool(entry.get("remote"))),
                }
            )
        if provider["ollama_details"]:
            models = await self._add_ollama_details(models, base_url)
        models.sort(key=lambda entry: str(entry.get("id")))
        if default_model and default_model not in [entry["id"] for entry in models]:
            models.insert(0, {"id": default_model, "name": "{} (from apps.yaml)".format(default_model), "prompt_price": None, "completion_price": None, "context_length": None, "free": is_free_model(provider, None, None)})
        return models

    async def _add_ollama_details(self, models, base_url=None):
        """Fill in tool capability and context length from Ollama's native /api/show.

        Ollama's OpenAI-compatible /v1/models returns only id/object/created/owned_by - no
        capabilities, no context length - so without this the picker cannot tell a tool-capable
        model from one that will fail mid-turn, and the context counter has nothing to measure
        against. /api/show has both.

        Models that do not advertise tools are dropped, which is the same promise the OpenRouter
        path makes: everything offered in the picker can actually run a tool call.

        One request per model, measured at about 76ms each against a local server, and
        list_models() is cached for a day - but a single slow or missing model must not lose the
        whole catalogue, so a failed lookup keeps the model rather than dropping it.

        /api/show reports the context length baked into the model, which is a ceiling rather than
        what the model will actually be loaded with: the server caps every model at
        OLLAMA_CONTEXT_LENGTH - on macOS an Ollama app setting, not an environment variable - and
        Qwen3.8 27B's 262144 becomes 131072 behind a 128k cap. /api/ps carries the effective value
        for each loaded model, so it is read once and used in preference. A model the server has
        never loaded is not listed there and keeps its own ceiling, which is the only answer
        available and still better than reporting it for everything.

        This matters beyond a wrong number in the picker: the Chat tab's context counter measures
        fullness against it, so a doubled limit reads half-full at the point the window is really
        full - which is how a turn overflows with no warning.
        """
        base = ollama_native_url(base_url or self.base_url, "")
        detailed = []
        timeout = aiohttp.ClientTimeout(total=OLLAMA_DETAIL_TIMEOUT_SECONDS)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                # Only a locally loaded model can have been capped, so a catalogue with nothing
                # local in it has nothing /api/ps could override. Ollama Cloud refuses that
                # endpoint anyway - 401 with or without a key, because "loaded" is a local-server
                # idea - so asking would spend a round trip to learn nothing.
                loaded_context = {} if all(model.get("remote") for model in models) else await self._ollama_loaded_context(session, base)
                for model in models:
                    try:
                        async with session.post("{}/api/show".format(base), json={"model": model["id"]}) as response:
                            if response.status != 200:
                                detailed.append(model)
                                continue
                            body = await response.json()
                    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                        detailed.append(model)
                        continue
                    capabilities = body.get("capabilities") or []
                    if capabilities and "tools" not in capabilities:
                        continue
                    info = body.get("model_info") or {}
                    context = next((value for key, value in info.items() if key.endswith("context_length")), None)
                    context = context or model.get("context_length")
                    # The server's own figure wins where it has one - see the docstring. Falsy is
                    # deliberately "no answer" rather than an answer of zero throughout: no model
                    # has a zero-token window, so a 0 from either endpoint is a placeholder or a
                    # bug, and taking it would replace a usable figure with one that tells the user
                    # nothing. The client agrees - contextLengthForModel() returns
                    # `context_length || null` and renderContextUsage() shows the token count alone
                    # when the limit is falsy.
                    effective = loaded_context.get(model["id"])
                    detailed.append(dict(model, context_length=effective or context))
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return models
        return detailed

    @staticmethod
    async def _ollama_loaded_context(session, base):
        """Return {model id: context length} for the models the Ollama server has loaded.

        Empty when /api/ps cannot be read or nothing is loaded, which leaves every model on its
        own ceiling - the same answer as before this existed, so a server too old to serve the
        endpoint degrades to the previous behaviour rather than losing the catalogue.
        """
        try:
            async with session.get("{}/api/ps".format(base)) as response:
                if response.status != 200:
                    return {}
                body = await response.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return {}
        loaded = {}
        for entry in (body or {}).get("models") or []:
            name = entry.get("model") or entry.get("name")
            context = entry.get("context_length")
            # A zero is filtered out here rather than downstream, so it can never win over a
            # model's own figure - see the note beside that comparison.
            if name and context:
                loaded[name] = context
        return loaded

    async def _stream_chunks(self, payload):
        """Yield decoded chunk dicts from the chat-completions endpoint.

        The only network call in the component, and the seam the tests replace. The session is
        created per request because this coroutine runs on whichever event loop invoked the turn.

        The ClientTimeout is built from self.request_timeout, not self.turn_timeout: this bounds
        one completion request, while turn_timeout bounds the whole turn across every round trip -
        see initialize()'s docstring for why conflating the two caps a multi-round turn on a budget
        meant for a single request.
        """
        headers = {"Authorization": "Bearer {}".format(self.api_key), "Content-Type": "application/json", "HTTP-Referer": "https://springfall2008.github.io/batpred/", "X-Title": "Predbat"}
        timeout = aiohttp.ClientTimeout(total=self.request_timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post("{}/chat/completions".format(self.base_url), headers=headers, json=payload) as response:
                if response.status != 200:
                    body = await response.text()
                    # OpenRouter sends Retry-After with a 429 saying how long the quota window has
                    # left. Honouring it beats any fixed schedule: it is the one number that
                    # actually knows when the limit clears.
                    raise ChatRequestError(response.status, body, retry_after=parse_retry_after(response.headers.get("Retry-After")))
                async for raw in response.content:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:") :].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        yield json.loads(data)
                    except ValueError:
                        continue

    async def _run_completion(self, conversation_id, messages, model):
        """Stream one completion, returning the assistant message, its usage and any sources.

        Three things beyond plain content/tool_calls accumulation happen here, all driven by
        fields a rich OpenRouter stream can carry that the original version silently ignored:

        - A chunk can carry a top-level "error" object mid-stream (a 200 response, not the
          non-200 _stream_chunks already raises on), and a choice can finish with
          finish_reason "error". Both fail the turn immediately via ChatRequestError, carrying
          the provider's own message through friendly() rather than inventing a second error
          path - _execute_turn's existing ChatRequestError handler does the rest.
        - finish_reason "length" means openrouter_max_tokens cut the reply off. The partial
          content is kept - discarding it would be worse than a truncated answer - but a visible
          note is appended so a half-answer is never mistaken for a whole one.
        - reasoning_details fragments are streamed the same way tool_calls fragments are: one
          logical reasoning block's text arrives in pieces that share an index (or, if the
          provider omits both index and id, as fully independent blocks). Merging onto the block
          it belongs to - not appending every fragment as its own block - is required by
          OpenRouter's own replay contract: "the entire sequence of consecutive reasoning blocks
          must match the outputs generated by the model during the original request; you cannot
          rearrange or modify the sequence of these blocks." A plain delta.reasoning string is
          also captured as a fallback for providers that send that instead.
        """
        payload = {"model": model, "messages": messages, "tools": self.tool_payload(), "stream": True}
        # Two ways of asking for token counts, because providers disagree: OpenRouter reads its
        # own usage.include and ignores stream_options, everyone else is the other way round.
        # Sending only the one that applies keeps an unknown field out of the request.
        if self.provider["openrouter_ext"]:
            payload["usage"] = {"include": True}
        if self.provider["stream_usage"]:
            payload["stream_options"] = {"include_usage": True}
        if self.max_tokens:
            payload["max_tokens"] = self.max_tokens
        # web_search_enabled() decides, including whether the active provider supports it at all -
        # gating it here as well would short-circuit the warning it logs when it does not.
        if self.web_search_enabled():
            payload["plugins"] = [{"id": "web"}]

        content = ""
        usage = {}
        sources = []
        accumulator = {}
        reasoning_accumulator = {}
        reasoning_order = []
        reasoning_text = ""
        next_anon_reasoning_key = 0
        truncated = False
        async for chunk in self._stream_chunks(payload):
            # Checked per chunk, which is the only place that runs while the model is thinking.
            # Every other stop check sits between rounds or between tool calls, so pressing Stop
            # during a long completion - the longest part of a turn, and exactly when somebody
            # reaches for it - did nothing until that completion finished on its own.
            stopping = self.stop_reason()
            if stopping:
                raise TurnStopped(content, stopping)
            error = chunk.get("error")
            if error:
                message_text = (error or {}).get("message") or "The provider reported an error"
                error_type = (error.get("metadata") or {}).get("error_type")
                raise ChatRequestError(error.get("code"), json.dumps(error), provider_message=message_text, error_type=error_type)
            if chunk.get("usage"):
                usage = chunk["usage"]
            choices = chunk.get("choices") or []
            delta = (choices[0].get("delta") or {}) if choices else {}
            finish_reason = choices[0].get("finish_reason") if choices else None
            if finish_reason == "error":
                raise ChatRequestError(None, json.dumps(chunk), provider_message="The provider ended the response with an error")
            if finish_reason == "length":
                truncated = True
            if delta.get("content"):
                content += delta["content"]
                self.emit(conversation_id, "delta", {"text": delta["content"]})
            if delta.get("reasoning"):
                reasoning_text += delta["reasoning"]
            # reasoning_details fragments merge onto the block they belong to - see the docstring
            # above for why that must not become one entry per fragment. A fragment carrying an
            # "index" (the shape OpenRouter actually streams) merges with every other fragment
            # sharing that index; one carrying only an "id" merges the same way tool_calls
            # fragments merge on id; a fragment with neither is its own block, appended in
            # arrival order rather than collapsed onto whatever came before it.
            for fragment in delta.get("reasoning_details") or []:
                if "index" in fragment:
                    key = ("index", fragment["index"])
                elif fragment.get("id"):
                    key = ("id", fragment["id"])
                else:
                    key = ("anon", next_anon_reasoning_key)
                    next_anon_reasoning_key += 1
                if key not in reasoning_accumulator:
                    reasoning_accumulator[key] = {}
                    reasoning_order.append(key)
                slot = reasoning_accumulator[key]
                if fragment.get("text"):
                    slot["text"] = slot.get("text", "") + fragment["text"]
                for field, value in fragment.items():
                    if field == "text" or field in slot:
                        continue
                    slot[field] = value
            for annotation in delta.get("annotations") or []:
                citation = annotation.get("url_citation") or {}
                if citation.get("url"):
                    sources.append({"url": citation["url"], "title": citation.get("title") or citation["url"]})
            # Tool call fragments are keyed by index, not id: the id and name arrive only in the
            # first fragment and the arguments are split across the rest.
            for fragment in delta.get("tool_calls") or []:
                slot = accumulator.setdefault(fragment.get("index", 0), {"id": None, "type": "function", "function": {"name": "", "arguments": ""}})
                if fragment.get("id"):
                    slot["id"] = fragment["id"]
                function = fragment.get("function") or {}
                if function.get("name"):
                    slot["function"]["name"] += function["name"]
                if function.get("arguments"):
                    slot["function"]["arguments"] += function["arguments"]

        if truncated:
            content += "\n\n*(Reply cut short: it reached the token limit configured for this conversation.)*"

        message = {"role": "assistant", "content": content or None}
        if accumulator:
            # Real OpenAI-compatible providers always send an id, but the invariant that every
            # stored tool_call_id matches a stored call id must hold by construction, not by the
            # provider's goodwill - an id-less call would otherwise store as {"id": None, ...},
            # and downstream code would have to invent a per-call synthetic id anyway. Doing it
            # once here, keyed on the accumulator's own index, keeps two id-less calls in the same
            # message distinct instead of colliding on a single turn-wide fallback.
            for index in accumulator:
                if not accumulator[index].get("id"):
                    accumulator[index]["id"] = "call_auto_{}".format(index)
            message["tool_calls"] = [accumulator[index] for index in sorted(accumulator)]
        if reasoning_accumulator:
            message["reasoning_details"] = [reasoning_accumulator[key] for key in reasoning_order]
        if reasoning_text:
            message["reasoning"] = reasoning_text
        return message, usage, sources

    async def _run_completion_with_retry(self, conversation_id, messages, model):
        """Run one completion, retrying a transient provider failure up to COMPLETION_MAX_ATTEMPTS.

        Wraps _run_completion rather than changing it, so each attempt starts that call fresh with
        its own empty local accumulator - nothing from a failed attempt's partial content can
        survive into the next one's message. The only thing that crosses attempts is the 'retry'
        event emitted just before each wait: it tells the browser to discard whatever partial
        assistant bubble the failed attempt had already streamed (see the client's handleRetry())
        before the retried attempt's own deltas start arriving on the same conversation.
        OpenRouter's own documentation is explicit that a mid-stream error can arrive after tokens
        have already started, which is exactly the case this guards against.

        An empty completion (no content, no tool_calls) is retried the same way a provider error
        is - see is_empty_completion() - because live traffic showed that shape is almost always
        provider trouble, not the model genuinely declining to answer. Everything
        classify_completion_failure() marks as not retryable (401/402/400, or a failure this
        function does not recognise) is re-raised on the very first attempt, completely unchanged,
        exactly as it was before this wrapper existed.

        Gives up - raising a ChatRequestError whose friendly() already reads "<what would have
        been shown today> Gave up after N attempts." - once COMPLETION_MAX_ATTEMPTS is reached or
        the turn's own deadline would be blown by the next backoff, whichever comes first.
        """
        error = None
        empty_message = None
        reason = None
        rate_limited = False
        attempt = 1
        while True:
            try:
                message, usage, sources = await self._run_completion(conversation_id, messages, model)
            except (ChatRequestError, aiohttp.ClientError, asyncio.TimeoutError) as raised:
                retryable, reason, rate_limited = classify_completion_failure(raised)
                if not retryable:
                    raise
                error, empty_message = raised, None
            else:
                if not is_empty_completion(message):
                    return message, usage, sources
                error, empty_message = None, message
                reason, rate_limited = EMPTY_COMPLETION_RETRY_REASON, False

            retry_after = getattr(error, "retry_after", None)
            if attempt >= max_attempts_for(rate_limited) or not await self._wait_before_retrying(conversation_id, attempt, rate_limited, reason, retry_after):
                raise self._give_up_error(error, empty_message, attempt)
            attempt += 1

    async def _wait_before_retrying(self, conversation_id, attempt, rate_limited, reason, retry_after=None):
        """Emit a 'retry' event and back off before the next completion attempt.

        Returns False, without sleeping, when the turn's own deadline does not leave room for the
        backoff - self.deadline bounds the whole turn, and sleeping into it would only trade a
        provider-side failure for a self-inflicted timeout. The caller treats that exactly like
        running out of attempts: give up now, with the failure already in hand.
        """
        delay = retry_delay_for(attempt, rate_limited)
        # A Retry-After from the provider wins over the schedule - it is the only number that
        # knows when the window actually clears. Bounded, because a header asking for an hour
        # would otherwise park the turn until its own deadline killed it with no explanation.
        if retry_after is not None:
            delay = min(max(retry_after, delay), RETRY_AFTER_MAX_SECONDS)
        remaining = self.deadline - time.monotonic()
        if remaining < delay:
            return False
        self.emit(conversation_id, "retry", {"attempt": attempt + 1, "of": max_attempts_for(rate_limited), "reason": reason, "delay": delay})
        await self._retry_sleep(delay)
        return True

    def _give_up_error(self, error, empty_message, attempts):
        """Build the ChatRequestError raised once every attempt is used or the deadline runs out.

        Its final_message already reads "<what would have been shown today> Gave up after N
        attempts.", built once here rather than reconstructed by ChatRequestError.friendly()'s
        status branches - this always follows the same shape regardless of which of the three
        failure kinds (a real ChatRequestError, an aiohttp/timeout failure, or an empty completion)
        triggered it. error and empty_message are mutually exclusive: exactly one names the last
        attempt's outcome, the other is None.
        """
        if error is not None:
            base = error.friendly() if isinstance(error, ChatRequestError) else "Could not reach {}: {}".format(self.base_url, error)
            status = getattr(error, "status", None)
            body = getattr(error, "body", "")
        else:
            base = empty_completion_message(empty_message)
            status = None
            body = ""
        plural = "" if attempts == 1 else "s"
        final_message = "{} Gave up after {} attempt{}.".format(base, attempts, plural)
        return ChatRequestError(status, body, final_message=final_message)

    async def _dispatch(self, conversation_id, name, arguments):
        """Run one tool, trying the chat-only tools before the shared Predbat ones.

        Every property named in the tool's chat_omit_properties is stripped from arguments right
        here, before any branch below sees them - the single choke point every tool call passes
        through regardless of which one handles it. openai_tool_list() only removes the property
        from the schema offered to the model; nothing stops the model - or content it read via
        fetch_url/search_docs - from naming the property anyway (this is exactly how 'masked':
        false reaches get_apps: the tool description still says credentials are redacted "by
        default", and fetch_url's github.com allowlist can serve a page that names the argument).
        So the guarantee has to be enforced on the arguments dict actually executed, not merely
        omitted from what the model was invited to ask for. See spec section 14.1. Builds a new
        dict rather than popping in place, so this never mutates the arguments already captured
        by the tool_start/confirm event emitted just before this call.
        """
        omit = (self.tool_defs_by_name.get(name) or {}).get("chat_omit_properties") or []
        if omit and arguments:
            arguments = {key: value for key, value in arguments.items() if key not in omit}
        if name == "set_chat_title":
            title = self.store.set_title(conversation_id, arguments.get("title"))
            if title is None:
                return {"success": False, "error": "This conversation no longer exists", "data": None}
            self.titled_this_turn = True
            self.emit(conversation_id, "title", {"title": title})
            return {"success": True, "error": None, "data": {"title": title}}
        if name == "search_docs":
            return await search_docs(self.storage, arguments.get("query"), max_results=arguments.get("max_results", 5))
        if name == "read_docs":
            return await read_docs(self.storage, arguments.get("section"), offset=arguments.get("offset", 0))
        # read_source is bounded work on this loop, which is fine: the component loop's only
        # other job is a five-second tick, and the web server is a different loop in a different
        # thread. search_source is different - it runs a MODEL-SUPPLIED regular expression, and
        # Python's re engine backtracks with no timeout, so a pattern like (.*)*x can hang inside
        # a single search() call that no elapsed-time check can interrupt. Run it on a worker
        # thread so a pathological pattern burns that thread rather than killing the component's
        # only event loop: the component stays alive, other turns still run, and this turn dies on
        # its own deadline. Containment, not latency.
        #
        # Never pass `root` to either function. It is deliberately absent from both tool schemas -
        # a model that could set it would point the search at /config and walk straight past the
        # extension allowlist that keeps apps.yaml and the token cache out.
        if name == "search_source":
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, functools.partial(search_source, arguments.get("pattern"), file=arguments.get("file"), max_results=arguments.get("max_results", 20)))
        if name == "read_source":
            return read_source(arguments.get("file"), start_line=arguments.get("start_line", 1), max_lines=arguments.get("max_lines", 200))
        if name == "fetch_url":
            return await fetch_url(arguments.get("url"), allowlist=self.fetch_allowlist)
        if name == "set_apps_config":
            # Synchronous file I/O on this loop, same as read_source above - apps.yaml is small,
            # and this mirrors WebInterface.html_apps_post doing the same open()/write() inline in
            # an async handler. See set_apps_config()'s own docstring (chat_tools.py) for the
            # safety checks it runs before touching the file.
            return set_apps_config(self.base, arguments.get("key"), arguments.get("value"))
        return await self.tools.execute(name, arguments)

    def claim_turn(self, conversation_id):
        """Reserve the single turn slot and return the new turn id.

        Synchronous and lock-guarded so the web thread can decide busy-or-not without waiting on
        either loop: by the time submit_turn returns, a concurrent send is already a clean 409.
        """
        meta = self.store.get_meta(conversation_id)
        if meta is None:
            raise KeyError("Unknown conversation {}".format(conversation_id))
        with self.lock:
            if self.active is not None:
                raise ChatBusyError("A reply is already in progress in '{}'".format(self.active.get("title")))
            self.turn_counter += 1
            turn_id = self.turn_counter
            # started is what _release_stale_turn measures against; without it a stranded slot is
            # never freed, and with a tick count instead it would free live ones.
            self.active = {"conversation_id": conversation_id, "turn_id": turn_id, "title": meta.get("title"), "started": time.monotonic()}
            # Cleared per turn so a Stop aimed at a finished turn cannot kill the next one.
            self.stop_requested = None
        return turn_id

    def submit_turn(self, conversation_id, text):
        """Start a turn on this component's loop and return its id without waiting for it.

        This is the web layer's entry point. The reply is delivered through the event buffer, so
        the HTTP request that started it has nothing left to wait for.
        """
        loop = self.loop
        if loop is None:
            raise AgentNotReadyError("The chat component has not finished starting")
        turn_id = self.claim_turn(conversation_id)
        asyncio.run_coroutine_threadsafe(self._execute_turn(conversation_id, turn_id, text), loop)
        return turn_id

    async def run_turn(self, conversation_id, text):
        """Claim and run a turn inline on the current loop, returning its id when it finishes."""
        turn_id = self.claim_turn(conversation_id)
        await self._execute_turn(conversation_id, turn_id, text)
        return turn_id

    async def _execute_turn(self, conversation_id, turn_id, text):
        """Run one full agentic turn, releasing the turn slot however it ends."""
        meta = self.store.get_meta(conversation_id) or {}
        self.titled_this_turn = False
        self.deadline = time.monotonic() + self.turn_timeout
        self.emit(None, "busy", {"conversation_id": conversation_id, "title": meta.get("title"), "turn_id": turn_id})
        try:
            await self.store.append(conversation_id, {"role": "user", "content": text})
            self.emit(conversation_id, "user", {"text": text})
            await self._turn_loop(conversation_id, turn_id, text)
        except ChatRequestError as error:
            self.count_errors += 1
            self._report_turn_error(conversation_id, error.friendly(), error.detail())
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            self.count_errors += 1
            self._report_turn_error(conversation_id, "Could not reach {}: {}".format(self.base_url, error), type(error).__name__)
        except Exception as error:
            self.count_errors += 1
            self.log("Error: chat turn failed: {}".format(error))
            self._report_turn_error(conversation_id, "The chat turn failed: {}".format(error), traceback.format_exc()[-PROVIDER_DETAIL_MAX:])
        finally:
            if not self.titled_this_turn and (self.store.get_meta(conversation_id) or {}).get("title") == NEW_CONVERSATION_TITLE:
                title = self.store.set_title(conversation_id, derive_title(text))
                if title:
                    self.emit(conversation_id, "title", {"title": title})
            try:
                await self.store.flush(conversation_id)
            except Exception as error:
                self.count_errors += 1
                self.log("Warn: could not persist chat conversation {}: {}".format(conversation_id, error))
            with self.lock:
                self.pending_confirm = {key: value for key, value in self.pending_confirm.items() if value.get("turn_id") != turn_id}
                # Only clear the slot - and only announce done/idle - if this turn still owns it.
                # If _release_stale_turn already freed it and another turn has since claimed it,
                # an unconditional clear (or an unconditional emit) here would silently unlock the
                # composer everywhere while that later turn is still running.
                owns_slot = (self.active or {}).get("turn_id") == turn_id
                if owns_slot:
                    self.active = None
            if owns_slot:
                self.emit(conversation_id, "done", {"turn_id": turn_id})
                self.emit(None, "idle", {})

    async def _turn_loop(self, conversation_id, turn_id, text):
        """Alternate completions and tool calls until the model answers or the cap is reached.

        max_tool_rounds bounds model round trips (completions), not tool calls: each iteration of
        this loop is one completion, and every tool call the model requested inside it runs before
        the next one - with the cap at 32, that is up to 32 completions but an unbounded number of
        tool calls inside each. Bounding rounds rather than calls is deliberate: a round is what
        costs money (one completion), not the tool calls inside it.

        The deadline is checked both here, once per round, and again inside the tool-dispatch loop
        below, between each individual tool call - see the comment there for why the per-round
        check alone leaves a gap wide enough for one round, on its own, to run far past the
        deadline.
        """
        # Checked before anything is sent. Without this a turn ran with no provider configured at
        # all, dialling OpenRouter unauthenticated and coming back with a bare 401 - which reads
        # as a rejected key rather than as "you have not configured an endpoint". A conversation
        # that already remembers a model reaches here even on a fresh install, so resolve_model()
        # alone is not the guard.
        if not self.provider_ready():
            self.emit(conversation_id, "error", {"message": NO_PROVIDER_MESSAGE})
            return
        model = self.resolve_model(conversation_id)
        if not model:
            self.emit(conversation_id, "error", {"message": NO_MODEL_MESSAGE})
            return
        for iteration in range(self.max_tool_rounds + 1):
            ending = self.stop_reason()
            if ending:
                self.emit(conversation_id, "error", {"message": self.stop_message(ending)})
                return
            history = await self.store.get_messages(conversation_id)
            messages = await self.build_messages(conversation_id, history)
            try:
                message, usage, sources = await self._run_completion_with_retry(conversation_id, messages, model)
            except TurnStopped as stopped:
                await self._finish_stopped_turn(conversation_id, stopped)
                return
            if usage:
                self.store.add_usage(conversation_id, usage)
                total = (self.store.get_meta(conversation_id) or {}).get("usage_total", {})
                self.emit(
                    conversation_id,
                    "usage",
                    {
                        "prompt_tokens": usage.get("prompt_tokens", 0),
                        "completion_tokens": usage.get("completion_tokens", 0),
                        "cost": usage.get("cost", 0),
                        "conversation_cost": total.get("cost", 0),
                        "cached_tokens": extract_cached_tokens(usage),
                        "conversation_cached_tokens": total.get("cached_tokens", 0),
                    },
                )
            # message is never empty here: _run_completion_with_retry only returns once
            # is_empty_completion(message) is False, retrying (and eventually raising a
            # ChatRequestError caught by _execute_turn) for as long as it stays True - see that
            # function's docstring. A second check here would be unreachable dead code.
            await self.store.append(conversation_id, message)
            self.emit(conversation_id, "assistant", {"text": message.get("content") or "", "sources": sources})

            calls = message.get("tool_calls") or []
            if not calls:
                return
            if iteration >= self.max_tool_rounds:
                await self._refuse_remaining_calls(conversation_id, calls, "Not run: the {} tool round limit for one turn was reached".format(self.max_tool_rounds))
                break
            for index, call in enumerate(calls):
                # Checked before every individual tool call, not only once at the top of the
                # round: the per-round check above only runs before the completion that requested
                # these calls, and a model can put an unbounded number of tool calls in one
                # message. Twenty search_source calls at up to five seconds each can run the round
                # itself for roughly a hundred seconds with no deadline check in between - this is
                # what closes that gap, ending the turn the same way the per-round check does
                # rather than letting every remaining call in the round still run.
                #
                # Every call from this one on must still be answered before returning, for the
                # reason _refuse_remaining_calls sets out - this path is reached by the Stop
                # button (which zeroes the deadline) as much as by a genuine timeout, so it is
                # the one a user hits deliberately and often.
                ending = self.stop_reason()
                if ending:
                    await self._refuse_remaining_calls(conversation_id, calls[index:], "Not run: the turn was stopped before this tool ran")
                    self.emit(conversation_id, "error", {"message": self.stop_message(ending)})
                    return
                await self._run_one_tool(conversation_id, turn_id, call)

        note = "I stopped after {} tool rounds, which is the configured limit for one turn. Ask me to continue if you want me to keep going.".format(self.max_tool_rounds)
        await self.store.append(conversation_id, {"role": "assistant", "content": note})
        self.emit(conversation_id, "assistant", {"text": note, "sources": []})

    async def _finish_stopped_turn(self, conversation_id, stopped):
        """End a turn the user stopped mid-stream, keeping whatever the model had already said.

        The partial text is appended as an ordinary assistant message, because the browser has
        already rendered it from the deltas and dropping it here would make it vanish on the next
        reload with no explanation. Only text: any half-built tool_calls from the interrupted
        chunk stream are discarded, since an assistant message carrying tool_calls that were never
        answered makes the provider reject every later turn in the conversation.
        """
        content = (stopped.content or "").strip()
        if content:
            await self.store.append(conversation_id, {"role": "assistant", "content": content})
            self.emit(conversation_id, "assistant", {"text": content, "sources": []})
        self.emit(conversation_id, "error", {"message": self.stop_message(stopped.reason)})

    def _confirmation_card_arguments(self, name, arguments):
        """Return what a write tool's confirmation card should show the human, before they answer.

        For every write tool except set_apps_config this is just the model's own arguments,
        rendered as-is by the frontend's generic confirm card (appendConfirmCard() in
        web_chat.py) - fine for set_config/set_plan_override, whose arguments already say exactly
        what will change. set_apps_config's arguments are only 'key' and 'value' - the model's
        proposed change, with nothing said about what the key is currently set to - so a human
        approving it would be approving a change they cannot actually see. This looks the current
        value up fresh from self.base.args (the same source get_apps_config reads, and the source
        the confirmed call will overwrite), rather than trusting anything the model said about it,
        so a prompt-injected description of "the current value" cannot misrepresent the change
        being approved. The restart warning belongs here too, not only in the tool's own success
        result, because the point of the warning is to be seen before the user approves, not after.
        """
        if name != "set_apps_config":
            return arguments
        key = arguments.get("key")

        # Resolved through the same path syntax set_apps_config writes with. A plain
        # self.base.args.get() only understands top-level names, so every nested path -
        # "car_charging_exclusive[0]", "forecast_solar[0].azimuth" - showed a current value of
        # null, telling the user the setting did not exist when it did.
        try:
            current = resolve_nested_yaml_value(self.base.args, key) if isinstance(key, str) else None
        except (KeyError, ValueError, TypeError):
            current = None

        # set_apps_config refuses credential keys, but the card is built before it runs, so
        # without this the value would be shown in the transcript on the way to being refused.
        if isinstance(key, str) and any(is_secret_key(segment) for segment in parse_yaml_path(key) if not segment.startswith("[")):
            current = SECRET_MASK

        return {"key": key, "current_value": current, "proposed_value": arguments.get("value"), "warning": APPS_YAML_RESTART_WARNING}

    def select_provider(self, name):
        """Make one named provider active, or fall back to the first usable one.

        Called at start-up with None, and again whenever the Chat tab switches provider. Sets the
        same self.api_key/base_url/provider the rest of the agent already reads, so nothing
        downstream needs to know providers are named or that there is more than one.
        """
        chosen = None
        if name:
            chosen = next((entry for entry in self.providers if entry["name"] == name), None)
        if chosen is None:
            # A usable one first: a half-configured entry cannot answer a turn, and selecting it
            # silently would produce a confusing failure rather than the setup page the user needs.
            chosen = next((entry for entry in self.providers if entry["configured"]), None)
        if chosen is None:
            chosen = self.providers[0] if self.providers else None

        if chosen is None:
            self.active_provider = None
            self.api_key = None
            self.base_url = OPENROUTER_BASE_URL
            self.provider_name, self.provider = "openrouter", PROVIDERS["openrouter"]
            self.default_model = None
            return None

        self.active_provider = chosen["name"]
        self.api_key = chosen["api_key"]
        self.base_url = str(chosen["url"]).rstrip("/")
        self.provider_name = chosen["type"]
        self.provider = chosen["settings"]
        self.default_model = chosen["model"]
        return chosen["name"]

    def stop_reason(self):
        """Return 'stopped', 'timeout' or None for a turn that should end right now.

        Both are checked together because both mean the same thing to the loop and different
        things to the user: pressing Stop is a choice, and telling somebody who pressed it that
        their turn "took longer than 1800 seconds" is simply wrong.

        Read without the lock. stop_requested is a scalar and active is replaced wholesale rather
        than mutated in place, so the worst a concurrent write can do is give this the previous
        turn's answer a moment early - and it is called on every streamed chunk, where taking a
        lock hundreds of times a turn buys nothing.
        """
        if self.stop_requested is not None and self.stop_requested == (self.active or {}).get("turn_id"):
            return "stopped"
        if time.monotonic() > self.deadline:
            return "timeout"
        return None

    def stop_message(self, reason):
        """Return what to tell the user about a turn that ended early."""
        if reason == "stopped":
            return "Stopped."
        return "This turn took longer than {} seconds and was stopped".format(self.turn_timeout)

    def provider_ready(self):
        """Return whether the active provider could actually answer a turn."""
        return any(entry["configured"] and entry["name"] == self.active_provider for entry in self.providers)

    def provider_summary(self):
        """Return the configured providers for the Settings dialog, without their API keys.

        `has_key` rather than the key itself, and no way to ask for the real thing: the dialog
        needs to show whether a key is set and to let one be replaced, neither of which requires
        the browser to ever hold it. That also means an edit to some unrelated field cannot
        round-trip a credential out of apps.yaml and back in - see the save route, where a blank
        key field means "keep what is in the file".
        """
        return [
            {
                "name": entry["name"],
                "type": entry["type"],
                "url": entry["url"],
                "model": entry["model"],
                "has_key": bool(entry["api_key"]),
                "needs_key": bool(entry["settings"]["needs_key"]),
                "configured": entry["configured"],
                "active": entry["name"] == self.active_provider,
            }
            for entry in self.providers
        ]

    def reload_providers(self, block):
        """Rebuild the provider list from a freshly saved chat: block and re-select.

        Called after the Settings dialog writes apps.yaml. The restart that write triggers is what
        really applies the change; this keeps the live agent consistent with the file in the
        seconds before it, so the save's own response is not describing stale configuration. Only
        provider state is touched - the store, the event buffer and any running turn are left
        exactly as they are, which is why this exists instead of calling initialize() again.
        """
        self.providers = build_providers(extract_providers(block, self.log))
        return self.select_provider(self.store.get_selected_provider())

    async def select_active_provider(self, name):
        """Switch which provider answers, and remember it. Returns None if there is no such entry.

        Nothing is written to apps.yaml, which is the whole point: the file already lists every
        provider, so choosing between them is not a configuration change and must not pay for one
        with a restart. Only the remembered choice moves, and that lives in the conversation index
        beside the remembered model.
        """
        if not any(entry["name"] == name for entry in self.providers):
            return None
        self.store.set_selected_provider(name)
        chosen = self.select_provider(name)
        await self.store.flush()
        return chosen

    async def apply_provider_block(self, block, active):
        """Adopt a freshly saved chat: block on this component's own loop, and remember the choice.

        Saving apps.yaml restarts Predbat within a few seconds (hass.py watches the file), so this
        is not what makes a provider change take effect - it is what makes the seconds until then,
        and the response the dialog renders from, describe the configuration that was actually
        saved. Remembering `active` is the part that has to happen here and not later: the restart
        is what would otherwise discard the choice, since select_provider() falls back to the first
        usable entry when nothing is remembered.

        Run on this loop rather than the web thread because select_provider() writes the api_key,
        base_url and provider a turn reads. A turn in flight when this lands will finish against
        the new endpoint and most likely fail on a model it does not serve - but that turn is
        about to be cut short by the restart in any case.
        """
        self.store.set_selected_provider(active)
        name = self.reload_providers(block)
        await self.store.flush()
        return name

    def resolve_model(self, conversation_id):
        """Return the model this conversation should use, or None if nothing has been chosen.

        Three sources, most specific first: the model set on this conversation, the model the
        user last picked in the UI (remembered per provider across restarts by the store), then
        the active provider's own default - its 'model' in apps.yaml, or the built-in default for
        its type. All three can be empty for an endpoint with no built-in default, which is not an
        error, just a user who has not chosen yet.

        Every one of the three is per provider, including the conversation override: a model id
        only means anything to the endpoint serving it, so carrying an OpenRouter model across a
        switch to Ollama produces a turn that fails on a name that endpoint has never heard of.
        An override with no provider recorded against it predates that being tracked and is
        treated as belonging to nobody, which costs nothing - the same choice is in the store's
        per-provider memory, written at the same moment the override was.
        """
        meta = self.store.get_meta(conversation_id) or {}
        return conversation_model_for(meta, self.active_provider) or self.store.get_selected_model(self.active_provider) or self.default_model or None

    def _report_turn_error(self, conversation_id, message, detail=None):
        """Show a failed turn in the transcript and record it on the conversation.

        Recorded as conversation metadata, never as a message. A failure is not something the
        model said, and replaying it would both waste context and invite the model to treat a
        transport error as part of the conversation - so it is stored beside the messages rather
        than among them, and build_messages() never sees it.
        """
        payload = {"message": message}
        if detail:
            payload["detail"] = detail
        self.emit(conversation_id, "error", payload)
        # The message count fixes where the failure sits in the conversation, so a client can tell
        # a current failure from one a later turn has since superseded.
        self.store.set_last_error(conversation_id, message, detail, message_count=self.store.message_count(conversation_id))

    async def _refuse_remaining_calls(self, conversation_id, calls, reason):
        """Answer tool calls the turn is abandoning, so the stored conversation stays well-formed.

        Every early exit out of the tool loop must come through here. An assistant message that
        carries tool_calls with no matching tool replies is rejected by the OpenAI-compatible API
        with a 400 - and not on the turn that created it, but on the next one, which makes the
        damage look unrelated to its cause. classify_completion_failure treats 400 as
        non-retryable, so that conversation then fails on every subsequent turn.

        This used to heal itself: trim_history cut at a user-message boundary, so a broken pair
        eventually aged out of the window. With max_history now defaulting to 0 (no trimming) the
        whole conversation is replayed every turn, so a single orphaned pair breaks it for good.

        Deliberately no tool_start/tool_end events - nothing ran, and the transcript should not
        suggest otherwise.
        """
        for call in calls:
            refused = {"success": False, "error": reason, "data": None}
            # call["id"] is guaranteed by _run_completion's normalisation - every stored
            # tool_calls entry carries one, real or synthetic - so no turn-wide fallback
            # is needed (and one would collide two id-less calls in the same message).
            await self.store.append(conversation_id, {"role": "tool", "tool_call_id": call.get("id"), "name": (call.get("function") or {}).get("name") or "", "content": json.dumps(refused)})

    async def _run_one_tool(self, conversation_id, turn_id, call):
        """Execute one tool call and append its result as a tool message."""
        name = (call.get("function") or {}).get("name") or ""
        # call["id"] is guaranteed by _run_completion's normalisation, so no turn-wide fallback is
        # needed here either - see the note beside the refusal loop in _turn_loop.
        call_id = call.get("id")
        raw = (call.get("function") or {}).get("arguments") or "{}"
        try:
            arguments = json.loads(raw) if raw.strip() else {}
            if not isinstance(arguments, dict):
                raise ValueError("arguments must be a JSON object")
        except ValueError as error:
            result = {"success": False, "error": "Could not read the tool argument JSON: {}".format(error), "data": None}
            self.emit(conversation_id, "tool_end", {"call_id": call_id, "name": name, "ok": False, "elapsed": 0, "preview": result["error"]})
            await self.store.append(conversation_id, {"role": "tool", "tool_call_id": call_id, "name": name, "content": json.dumps(result)})
            return

        definition = self.tool_defs_by_name.get(name) or {}
        if definition.get("writes") and self.confirm_writes_enabled():
            with self.lock:
                # The card is kept, not just the fact that something is pending: a client that
                # reconnects or reloads has no other way to get it back, and without it the user
                # is left with a turn that waits for an answer to a question no longer on screen.
                card = {"call_id": call_id, "name": name, "arguments": self._confirmation_card_arguments(name, arguments)}
                self.pending_confirm[call_id] = {"conversation_id": conversation_id, "turn_id": turn_id, "approved": None, "card": card}
            # Persisted as a record of the decision, so it survives a reconnect or a restart and
            # the transcript shows what was asked. Never enters `messages`, so it is never
            # replayed to the model - which learns the outcome from the tool result instead.
            self.store.add_approval(conversation_id, card)
            self.emit(conversation_id, "confirm", card)
            approved = await self.await_confirmation(call_id)
            with self.lock:
                self.pending_confirm.pop(call_id, None)
            self.store.resolve_approval(conversation_id, call_id, APPROVAL_APPROVED if approved else APPROVAL_REJECTED)
            if not approved:
                # An ordinary tool result rather than aborting the turn: the model acknowledges the
                # decline and can offer an alternative, which is what a user expects from a refusal.
                result = {"success": False, "error": "User declined this change", "data": None}
                await self.store.append(conversation_id, {"role": "tool", "tool_call_id": call_id, "name": name, "content": json.dumps(result)})
                return

        started = time.monotonic()
        self.emit(conversation_id, "tool_start", {"call_id": call_id, "name": name, "arguments": arguments})
        try:
            result = await self._dispatch(conversation_id, name, arguments)
        except Exception as error:
            result = {"success": False, "error": "Tool '{}' failed: {}".format(name, error), "data": None}
        elapsed = round(time.monotonic() - started, 2)
        encoded = json.dumps(result)
        self.emit(conversation_id, "tool_end", {"call_id": call_id, "name": name, "ok": bool(result.get("success")), "elapsed": elapsed, "preview": encoded[:400]})
        await self.store.append(conversation_id, {"role": "tool", "tool_call_id": call_id, "name": name, "content": encoded})

    def confirm_writes_enabled(self):
        """Return whether a write tool must be confirmed before it runs.

        Read at the moment the tool is called rather than cached at turn start, so toggling the
        switch mid-turn takes effect on the next tool call.
        """
        value, _ = self.get_ha_config("chat_confirm_writes", True)
        return True if value is None else bool(value)

    def web_search_enabled(self):
        """Return whether OpenRouter's web search plugin should be added to the request.

        The plugin is an OpenRouter feature, so it is not sent to any other provider - it would be
        ignored at best and rejected at worst. The switch being on with a different provider
        active is worth saying once, rather than leaving the user wondering why turning it on
        changed nothing.

        The provider check lives here rather than at the call site: written as
        `provider["openrouter_ext"] and web_search_enabled()`, Python short-circuits and this
        never runs off OpenRouter, so the explanation was never logged.
        """
        value, _ = self.get_ha_config("chat_web_search", False)
        if not value:
            return False
        if not self.provider["openrouter_ext"]:
            if not self.warned_web_search_base_url:
                self.warned_web_search_base_url = True
                self.log("Warn: chat web search is enabled but the active provider is '{}' ({}), not OpenRouter - the web plugin is only sent to OpenRouter".format(self.active_provider, self.base_url))
            return False
        return True

    def confirm(self, call_id, conversation_id, approved):
        """Record a user's answer to a pending write confirmation."""
        with self.lock:
            pending = self.pending_confirm.get(call_id)
            if pending is None or pending.get("conversation_id") != conversation_id:
                return False
            pending["approved"] = bool(approved)
        self.emit(conversation_id, "confirm_result", {"call_id": call_id, "approved": bool(approved)})
        return True

    async def await_confirmation(self, call_id):
        """Wait for a confirmation answer, polling rather than blocking on a primitive.

        Polling keeps this free of loop-bound objects, so the turn runs correctly on whichever
        event loop invoked it. The time spent parked is added back to two separate clocks, not
        just one: self.deadline (the turn's own timeout, checked in _turn_loop) - a user who steps
        away should not turn their own approval into a timeout - and active["started"] (the
        stale-turn clock _release_stale_turn measures against). _release_stale_turn's own guard
        only protects this turn while its entry is still in pending_confirm; that entry is popped
        by _run_one_tool right after this returns, so a wait long enough to push the extended
        deadline past started + turn_timeout + STALE_TURN_GRACE_SECONDS would otherwise have its
        live slot released on the very next housekeeping tick after the user finally answers -
        displacing the original during-the-wait hazard to just-after-the-answer instead of closing
        it. Advancing both clocks together closes it in both places.
        """
        started = time.monotonic()
        while time.monotonic() - started < CONFIRM_TIMEOUT_SECONDS:
            with self.lock:
                pending = self.pending_confirm.get(call_id)
                if pending is None:
                    break
                # Stop must reach a turn parked here. This loop deliberately ignores self.deadline
                # - the parked time is added back precisely so a slow human does not time their
                # own approval out - but that also made the Stop button inert for up to
                # CONFIRM_TIMEOUT_SECONDS, with the timer visibly climbing and nothing able to end
                # it. An explicit stop is not a deadline, so it is checked separately.
                if self.stop_requested is not None and self.stop_requested == pending.get("turn_id"):
                    break
                if pending.get("approved") is not None:
                    elapsed = time.monotonic() - started
                    self.deadline += elapsed
                    if self.active is not None:
                        self.active["started"] += elapsed
                    return bool(pending["approved"])
            await asyncio.sleep(CONFIRM_POLL_SECONDS)
        elapsed = time.monotonic() - started
        self.deadline += elapsed
        with self.lock:
            if self.active is not None:
                self.active["started"] += elapsed
        return False
