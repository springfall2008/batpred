# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init


"""Cached credential-redaction pattern for log(), as a self-contained mixin.

Holds the compiled pattern Hass.log() applies to every line it writes (GH#4770), kept
here rather than on Hass itself so that whatever class provides log() only has to
inherit LogRedaction to get working redaction - with no __init__ of its own to
remember (GH#5169).
"""

import threading

from utils import collect_log_secret_values, compile_log_secret_pattern


class LogRedaction:
    """Mixin providing log()'s cached credential-redaction pattern (GH#4770).

    Inheritable on its own terms: all of its state is established on the class rather
    than in an __init__, so a class that inherits this mixin has working redaction
    whatever its own constructor does - including not calling this one's, since there
    isn't one. That matters because the members below are called from files that do not
    define the class holding them (userinterface.py, web.py, chat_tools.py all call
    _invalidate_log_secret_pattern() on the engine object), so "has the methods but not
    the state" was a reachable state as long as exactly one __init__ established it, and
    an engine composed from a different hass.py crash-looped on precisely that (GH#5169).
    """

    # Sentinel distinct from None: compile_log_secret_pattern() legitimately returns None when
    # there are no secrets configured to redact, so None alone in the cache slot can't tell
    # "not built yet" from "built, and there is nothing to redact" - the latter would otherwise
    # rebuild (recompute the value set, recompile) on every single log() call instead of caching.
    _LOG_SECRET_PATTERN_UNSET = object()

    # Class attributes, not __init__ assignments: an instance that has never built a pattern
    # reads these, and the first build/invalidation shadows them with instance attributes of its
    # own, so the cache is per-instance from the moment it holds anything instance-specific
    # while still needing no constructor to exist (GH#5169).
    _log_secret_pattern_cache = _LOG_SECRET_PATTERN_UNSET
    _log_secret_pattern_fingerprint = None

    # The lock, unlike the cache, cannot be created on first use: _log_secret_pattern() takes it
    # on entry precisely to serialise concurrent first uses, and two threads lazily creating one
    # each would each proceed inside their own - the exact interleaving it exists to prevent. A
    # class attribute is therefore shared by every instance, which is sound because all it guards
    # is the atomicity of one instance's "read sentinel, build, store"; making that atomic
    # process-wide is strictly stronger than making it per-instance, and it is only ever held for
    # the duration of a rebuild, which happens on a config change rather than per log line.
    _log_secret_pattern_lock = threading.Lock()

    def _invalidate_log_secret_pattern(self):
        """
        Mark the cached redaction pattern stale so the next log() call rebuilds it from the
        current args/secrets (GH#4770). Every call site that mutates self.args or self.secrets
        after startup must call this - see _log_secret_pattern()'s docstring for why a missed
        site is a real leak, not just a staleness bug.
        """
        with self._log_secret_pattern_lock:
            self._log_secret_pattern_cache = self._LOG_SECRET_PATTERN_UNSET

    def _log_secret_fingerprint(self):
        """
        A cheap value that changes whenever the set of credentials in args/secrets could have.

        Deliberately not a hash of every value: this runs under the lock on the way to each
        rebuild decision, so it stays O(number of top-level keys). Identity of the args and
        secrets mappings plus their sizes catches the shapes a config mutation takes - a key
        added or removed (size), and the whole mapping being replaced or rebound (identity), as
        web.py's batch editor does via clear()/update() and web_chat.py does by assigning a new
        block.

        An in-place edit of an existing key that keeps the size the same is NOT caught here, so
        the explicit _invalidate_log_secret_pattern() call sites remain load-bearing. This is a
        safety net under them, not a replacement: with it, a missed or mis-ordered call site
        degrades to "redacted from the next line" instead of "leaks until the next restart",
        which is the failure mode successive reviews of #5053 kept finding one call site at a
        time. GH#5063 tracks removing the contract itself by routing every args mutation through
        one setter (#5053 review).
        """
        args = getattr(self, "args", None)
        secrets = getattr(self, "secrets", None)
        return (id(args), len(args) if isinstance(args, dict) else -1, id(secrets), len(secrets) if isinstance(secrets, dict) else -1)

    def _log_secret_pattern(self):
        """
        Return the cached compiled redaction pattern log() must apply, rebuilding it the first
        time it is needed and whenever load_secrets()/apps.yaml load invalidate it (GH#4770).

        Cached rather than recomputed on every log() call: log() runs on every log line, while
        args/secrets only change on startup and on a config reload, so rebuilding the value set
        and recompiling the pattern that rarely - rather than on every call - keeps the
        redaction check to a single compiled-regex scan per line on the hot path.

        Guarded by a lock, not just the sentinel check: log() runs from component threads as well
        as the main thread (create_task()), so two threads can both observe the sentinel and race
        to rebuild. Without the lock, a thread that started building from stale args right before
        another thread invalidates the cache (a credential just added via set_arg()) can finish
        second and overwrite the fresh invalidation with its stale, already-out-of-date pattern -
        silently keeping the just-added credential unredacted until something invalidates the
        cache again. The lock makes "read sentinel, build, store" one atomic step so a build that
        started before an invalidation can never win a race against it.
        """
        with self._log_secret_pattern_lock:
            fingerprint = self._log_secret_fingerprint()
            if self._log_secret_pattern_cache is self._LOG_SECRET_PATTERN_UNSET or fingerprint != self._log_secret_pattern_fingerprint:
                args = getattr(self, "args", None)
                redact_strings = args.get("redact_strings") if args else None
                redact_strings_labelled = args.get("redact_strings_labelled") if args else None
                values = collect_log_secret_values(args, getattr(self, "secrets", None), redact_strings, redact_strings_labelled)
                self._log_secret_pattern_cache = compile_log_secret_pattern(values)
                self._log_secret_pattern_fingerprint = fingerprint
            return self._log_secret_pattern_cache
