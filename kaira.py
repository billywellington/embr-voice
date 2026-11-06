"""
Kaira -- Voice Dictation Tool
=============================
Personal voice-to-text tool for Billy Wellington.

Controls (defaults — configurable via tray > Preferences...):
  Ctrl+Win         = Start / stop dictation (hold to talk)
  Ctrl+Shift+Alt   = Command mode (hold to talk — runs a voice command on selected text)
  Ctrl+Shift+Q     = Quit
  Click widget     = Start / stop dictation

Floating overlay with mic-reactive waveform (Warm Ember theme).
Uses PyQt6 with QPainter for the overlay -- true per-pixel alpha
transparency via DWM compositing. Works on Intel UHD + Win11.
Qt.WindowType.Tool hides from taskbar automatically.

Runs silently on startup. All activity logged to logs/ folder.
Requires: OpenAI API key in .env file
"""

import os
import re
import sys
import json
import wave
import time
import math
import logging
import subprocess
import threading
from datetime import datetime

import numpy as np
import sounddevice as sd
import pyperclip
from pynput import keyboard as pynput_kb
from openai import OpenAI
from dotenv import load_dotenv

# v1.2 dependencies for VAD hallucination guard + retry-with-backoff.
# silero-vad-lite: ONNX model, zero Python deps, ~2MB — runs before each
# Whisper call to reject recordings that contain no speech.
# tenacity: exponential backoff wrapper around the Whisper API call.
try:
    from silero_vad_lite import SileroVAD
    _VAD_AVAILABLE = True
except Exception as e:
    SileroVAD = None
    _VAD_AVAILABLE = False
    _vad_import_error = str(e)

import openai  # for concrete exception classes used by the retry predicate
import httpx   # concrete transport exceptions + split timeout object
from tenacity import (
    retry, stop_after_attempt, wait_random_exponential,
    retry_if_exception, RetryError,
)

from PyQt6.QtWidgets import (
    QApplication, QWidget, QDialog, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QMessageBox,
    QSystemTrayIcon, QMenu, QFrame, QComboBox, QCheckBox, QScrollArea,
    QTabWidget,
)
from PyQt6.QtCore import (
    Qt, QTimer, QRectF, QPointF, pyqtSignal,
    QObject, QAbstractNativeEventFilter, QByteArray,
)
from PyQt6.QtGui import (
    QPainter, QColor, QBrush, QPen, QFont, QFontMetrics,
    QPainterPath, QMouseEvent, QGuiApplication, QCursor,
    QIcon, QPixmap, QAction,
)
import ctypes
import ctypes.wintypes
import keyring

try:
    import msvcrt
except ImportError:
    msvcrt = None

# --- Paths ---
# Code lives in SCRIPT_DIR (may be read-only when installed). User-writable
# state (logs, audio, lock) lives under %APPDATA%\Kaira so Kaira works when
# installed to Program Files, a PyInstaller onefile, or any read-only location.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
APPDATA_DIR = os.environ.get("APPDATA") or os.path.expanduser("~/AppData/Roaming")
DATA_DIR = os.path.join(APPDATA_DIR, "Kaira")
LOG_DIR = os.path.join(DATA_DIR, "logs")
AUDIO_DIR = os.path.join(LOG_DIR, "audio")
LOCK_PATH = os.path.join(DATA_DIR, "kaira.lock")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(AUDIO_DIR, exist_ok=True)

AUDIO_RETENTION_DAYS = 7

SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
JOURNAL_PATH  = os.path.join(DATA_DIR, "pending_transcriptions.json")
MODELS_DIR    = os.path.join(DATA_DIR, "models")  # faster-whisper cache
os.makedirs(MODELS_DIR, exist_ok=True)

# Local faster-whisper fallback config (v1.2.0 MVP). Triggered only when
# cloud Whisper exhausts all tenacity retries. base.en is the sweet-spot
# model for CPU-only laptops — 145 MB on disk, ~2.4x realtime on Intel
# UHD, same accuracy as medium for standard English. First use downloads
# the model to MODELS_DIR — ~1-2 min on normal net.
LOCAL_MODEL_NAME = "base.en"
LOCAL_MODEL_DEVICE = "cpu"
LOCAL_MODEL_COMPUTE_TYPE = "int8"  # quantized — 4x smaller, ~imperceptible WER delta
LOCAL_MODEL_CPU_THREADS = 4        # avoids OpenBLAS/Qt threadpool conflicts

# --- VAD hallucination guard config (v1.2) ---
# Three-layer defence against Whisper hallucinating phrases like
# "Thank you for watching" on silent recordings:
#   Layer 1 — RMS energy pre-gate: rejects true silence in 0.1ms
#   Layer 2 — Silero VAD: rejects near-silence / ambient noise in <5ms
#   Layer 3 — Hallucination phrase filter: catches anything that slips through
# Research brief: "Kaira v1.2 — Silero VAD False-Negative Risk Analysis..."
VAD_DEFAULTS = {
    "enabled":            True,
    "threshold":          0.30,   # peak-score threshold for Silero
    "min_speech_chunks":  2,      # consecutive chunks >= threshold required
    "rms_silence_floor":  0.005,  # below this = true silence, skip Silero
    "skip_below_ms":      500,    # recordings under this always go to Whisper
    "skip_above_ms":      10000,  # recordings over this always go to Whisper
}

# Top 20 Whisper hallucination phrases from the LocalLLaMA collected-135 thread.
# These match exactly (case-insensitive, full-string) — we never substring-match
# because "Thank you" is a legitimate dictation.
HALLUCINATION_PHRASES = {
    "thank you for watching",
    "thanks for watching",
    "thank you for watching!",
    "thanks for watching!",
    "please subscribe",
    "subscribe to my channel",
    "like and subscribe",
    "don't forget to subscribe",
    "don't forget to like and subscribe",
    "see you in the next video",
    "see you next time",
    "hit the like button",
    "turn on notifications",
    "[music]",
    "[applause]",
    "[laughter]",
    "[silence]",
    "♪",
    ".",   # single-period hallucination on very short silence
    "you", # single-token hallucination
}

# --- Hotkey config ---
# Kaira uses Option D (hybrid): hold-to-talk actions (dictation, command_mode)
# are modifier-only combos; fire-once actions (quit) are standard combos.
# See research brief: "Recommendation_ __Option D — Hybrid__.md"

DEFAULT_HOTKEYS = {
    # v1.2.0: default flipped to "toggle" (tap-to-toggle). Cold first-run
    # testers on two different laptops instinctively tapped Ctrl+Win and
    # got no feedback — hold-to-talk's microtransition suppression was
    # correctly cancelling those as accidental. Tap-toggle matches every
    # dictation user's instinct without onboarding help. Users who prefer
    # the walkie-talkie feel can flip it in Preferences.
    "dictation":    {"type": "mod_only", "keys": ["ctrl", "win"],          "activation": "toggle"},
    "command_mode": {"type": "mod_only", "keys": ["ctrl", "shift", "alt"], "activation": "toggle"},
    "quit":         {"type": "standard", "modifiers": ["ctrl", "shift"], "key": "q"},
}

ACTIVATION_MODES = ("toggle", "hold_to_talk")
ACTIVATION_DISPLAY = {"toggle": "Tap (toggle)", "hold_to_talk": "Hold to talk"}

# Internal key names <-> Windows VK codes. Only the four modifiers are valid
# for mod_only combos. Standard combos accept any key from STANDARD_KEYS.
MOD_NAMES = ("ctrl", "shift", "alt", "win")

MOD_VK = {
    "ctrl":  0x11,  # VK_CONTROL (generic; Raw Input reports this for L/R)
    "shift": 0x10,  # VK_SHIFT
    "alt":   0x12,  # VK_MENU
    "win":   0x5B,  # VK_LWIN (RWIN 0x5C normalised to LWIN)
}

MOD_DISPLAY = {"ctrl": "Ctrl", "shift": "Shift", "alt": "Alt", "win": "Win"}

# Canonical modifier order for display — matches Windows convention.
MOD_ORDER = ("ctrl", "shift", "alt", "win")

# Standard keys allowed as the non-modifier component of a standard combo.
# Letters A-Z, digits 0-9, F1-F24.
STANDARD_KEYS = {}
for _i, _c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
    STANDARD_KEYS[_c.lower()] = 0x41 + _i
for _i in range(10):
    STANDARD_KEYS[str(_i)] = 0x30 + _i
for _i in range(1, 25):
    STANDARD_KEYS[f"f{_i}"] = 0x6F + _i  # VK_F1 = 0x70

# Windows 11 reserved combos (standard-style: modifier+key).
# From Microsoft keyboard-shortcuts docs. Hard-block on CRITICAL (kernel/
# system-intercepted), warn on HIGH (user-visible feature collisions).
# mod_only combos below.
RESERVED_STANDARD_CRITICAL = frozenset({
    # (frozenset(modifiers), key)
    (frozenset({"ctrl", "alt"}), "delete"),  # listed for completeness — can't capture anyway
    (frozenset({"win"}), "l"),   # Lock PC
    (frozenset({"win"}), "d"),   # Show desktop
    (frozenset({"win"}), "tab"), # Task View
    (frozenset({"win"}), "e"),   # File Explorer
    (frozenset({"win"}), "h"),   # Windows Voice Typing
    (frozenset({"win"}), "space"),
    (frozenset({"win", "shift"}), "s"),  # Snipping Tool
    (frozenset({"ctrl", "shift"}), "escape"),  # Task Manager
    (frozenset({"win"}), "r"),   # Run
    (frozenset({"win"}), "i"),   # Settings
    (frozenset(), "f12"),        # System debugger
})

RESERVED_STANDARD_HIGH = frozenset({
    (frozenset({"win"}), ch) for ch in "acgkmnpqsvwxz"
} | {
    (frozenset({"win", "shift"}), "r"),
    (frozenset({"alt"}), "tab"),
    (frozenset({"alt"}), "f4"),
} | {
    (frozenset({"win"}), str(n)) for n in range(10)
})


DEFAULT_SETTINGS = {
    "hotkeys": DEFAULT_HOTKEYS,
    "paused":  False,   # True = boot into paused state (hotkeys released, overlay hidden)
    "vad":     VAD_DEFAULTS,
}


def _default_hotkeys_copy():
    """Deep-copy of DEFAULT_HOTKEYS (mutable dicts/lists inside)."""
    return {
        name: {k: (list(v) if isinstance(v, list) else v)
               for k, v in cfg.items()}
        for name, cfg in DEFAULT_HOTKEYS.items()
    }


def load_settings():
    """Load settings.json or create it with defaults on first run.
    Missing/invalid fields fall back to defaults silently."""
    if not os.path.exists(SETTINGS_PATH):
        cfg = {"hotkeys": _default_hotkeys_copy(), "paused": False, "vad": dict(VAD_DEFAULTS)}
        save_settings(cfg)
        return cfg
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        log.warning(f"[SETTINGS] Failed to read {SETTINGS_PATH}: {e}. Using defaults.")
        return {"hotkeys": _default_hotkeys_copy(), "paused": False, "vad": dict(VAD_DEFAULTS)}

    hotkeys = cfg.get("hotkeys") or {}
    for name, default in DEFAULT_HOTKEYS.items():
        if name not in hotkeys or not _is_valid_hotkey(hotkeys.get(name)):
            log.warning(f"[SETTINGS] Hotkey '{name}' missing/invalid — using default.")
            hotkeys[name] = {k: (list(v) if isinstance(v, list) else v)
                             for k, v in default.items()}
        else:
            # Migrate pre-v1.2 configs: fill missing activation field.
            # Before v1.2 defaulted to hold_to_talk (market research
            # recommendation), v1.2.0 flipped to toggle after cold-install
            # testing showed tap-to-toggle is a lower-friction default
            # for first-time users without onboarding. Pre-v1.2 configs
            # had no activation field at all — migrate them to the new
            # default so existing installs move with the product.
            hk = hotkeys[name]
            if hk.get("type") == "mod_only" and "activation" not in hk:
                hk["activation"] = "toggle"
                log.info(f"[SETTINGS] Migrated '{name}' to activation=toggle")
    cfg["hotkeys"] = hotkeys
    cfg["paused"]  = bool(cfg.get("paused", False))
    # Ensure vad block exists and is fully populated (migration for v1.1 files)
    vad = cfg.get("vad") or {}
    for key, default_val in VAD_DEFAULTS.items():
        if key not in vad:
            vad[key] = default_val
    cfg["vad"] = vad
    return cfg


def save_settings(cfg):
    """Write settings to disk. Best-effort — logs and swallows on failure."""
    try:
        tmp = SETTINGS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, SETTINGS_PATH)
    except Exception as e:
        log.error(f"[SETTINGS] Failed to write {SETTINGS_PATH}: {e}")


# --- Pending-transcriptions journal (v1.2) ---
# When Kaira records audio to disk but the Whisper API call fails, we track
# it in pending_transcriptions.json. Entries move through three states:
#   pending         — registered, not yet attempted (or first attempt in flight)
#   retry_scheduled — had 1-2 failed attempts, still eligible for retry
#   dead_letter    — hit the failure threshold (3 counted attempts) — shown
#                    to the user in "Failed recordings" but NOT re-prompted
#                    on every launch. Breaks the infinite-loop defect where
#                    the same poisoned file was retried forever.
# Research: research/Kaira v1.3 Research Recommendations.md §"Persistent
# queue and retry exhaustion".
JOURNAL_MAX_AGE_DAYS = 7             # pending entries pruned after 7 days
JOURNAL_DEAD_LETTER_THRESHOLD = 3    # counted failures before dead-letter
JOURNAL_DEAD_LETTER_RETENTION_DAYS_DEFAULT = 30  # 7 / 30 / 90 / None=never

# State constants — kept as plain strings for simple JSON serialisation.
JOURNAL_STATE_PENDING         = "pending"
JOURNAL_STATE_RETRY_SCHEDULED = "retry_scheduled"
JOURNAL_STATE_DEAD_LETTER     = "dead_letter"


def _journal_now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _journal_retention_days() -> int | None:
    """Read retention config. Returns days, or None if user chose 'never'."""
    try:
        settings = load_settings()
        val = settings.get("journal_retention_days",
                           JOURNAL_DEAD_LETTER_RETENTION_DAYS_DEFAULT)
    except Exception:
        val = JOURNAL_DEAD_LETTER_RETENTION_DAYS_DEFAULT
    if val in (7, 30, 90):
        return val
    if val in (None, -1, "never"):
        return None
    return JOURNAL_DEAD_LETTER_RETENTION_DAYS_DEFAULT


def _journal_migrate_entry(entry: dict) -> dict:
    """Upgrade v1.2 journal entries to the v1.2.0-hotfix schema. Old
    entries had a single `attempts` field and no state; new entries track
    attempt_count, last_error_*, state, retryable, next_retry_at,
    failure_class."""
    changed = False
    if "attempt_count" not in entry:
        entry["attempt_count"] = int(entry.pop("attempts", 0) or 0)
        changed = True
    for field in ("last_attempt_at", "last_error_code",
                  "last_error_message", "next_retry_at", "failure_class"):
        if field not in entry:
            entry[field] = None
            changed = True
    if "retryable" not in entry:
        entry["retryable"] = True
        changed = True
    if "state" not in entry:
        entry["state"] = JOURNAL_STATE_PENDING
        changed = True
    return entry if changed else entry


def _journal_load() -> dict:
    """Return the journal dict (wav_path -> metadata). Empty on corruption.
    Migrates any legacy entries to the current schema in-place."""
    if not os.path.exists(JOURNAL_PATH):
        return {}
    try:
        with open(JOURNAL_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        log.warning(f"[JOURNAL] Corrupt or unreadable ({e}). Resetting to empty.")
        return {}
    if not isinstance(data, dict):
        log.warning("[JOURNAL] Unexpected format — resetting to empty.")
        return {}
    for path, entry in list(data.items()):
        if isinstance(entry, dict):
            _journal_migrate_entry(entry)
    return data


def _journal_save(data: dict) -> None:
    try:
        tmp = JOURNAL_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, JOURNAL_PATH)
    except Exception as e:
        log.error(f"[JOURNAL] Failed to write: {e}")


def _journal_prune(data: dict) -> bool:
    """Drop entries whose WAV is gone, or whose age exceeds retention.
    Pending entries prune at JOURNAL_MAX_AGE_DAYS; dead-letter entries
    prune at the user-configured retention (default 30 days, or never).
    Returns True if anything was removed."""
    now = datetime.now()
    dl_retention = _journal_retention_days()
    dirty = False
    for path, meta in list(data.items()):
        if not os.path.exists(path):
            data.pop(path, None)
            dirty = True
            continue
        try:
            recorded = datetime.fromisoformat(meta.get("recorded_at", ""))
            age_days = (now - recorded).total_seconds() / 86400
        except Exception:
            age_days = 0.0
        state = meta.get("state", JOURNAL_STATE_PENDING)
        if state == JOURNAL_STATE_DEAD_LETTER:
            if dl_retention is not None and age_days > dl_retention:
                data.pop(path, None)
                dirty = True
        else:
            if age_days > JOURNAL_MAX_AGE_DAYS:
                data.pop(path, None)
                dirty = True
    return dirty


def journal_add(wav_path: str, duration: float, mode: str = "dictation") -> None:
    """Register a WAV file as pending-transcription. Called BEFORE the
    Whisper API call — if we crash between journal_add and journal_remove,
    the entry survives for next-launch recovery. Does NOT increment
    attempt_count; that happens only when an actual execution attempt
    starts (see journal_mark_attempt_started)."""
    data = _journal_load()
    data[wav_path] = {
        "recorded_at":        _journal_now_iso(),
        "duration_s":         round(float(duration), 2),
        "mode":               mode,
        "attempt_count":      0,
        "last_attempt_at":    None,
        "last_error_code":    None,
        "last_error_message": None,
        "state":              JOURNAL_STATE_PENDING,
        "retryable":          True,
        "next_retry_at":      None,
        "failure_class":      None,
    }
    _journal_save(data)


def journal_remove(wav_path: str) -> None:
    """Remove an entry after a successful transcription."""
    data = _journal_load()
    if data.pop(wav_path, None) is not None:
        _journal_save(data)


def journal_mark_attempt_started(wav_path: str) -> None:
    """Increment attempt_count at the moment a real execution attempt
    begins. Deliberately NOT called on: queue-add, user cancel, precondition
    failures, paused-state deferral, or startup-scan enumeration — those
    are not attempts. Research §"Failures that should not count"."""
    data = _journal_load()
    entry = data.get(wav_path)
    if not entry:
        return
    entry["attempt_count"] = int(entry.get("attempt_count", 0)) + 1
    entry["last_attempt_at"] = _journal_now_iso()
    # An attempt-in-flight stays "pending" until it completes or fails.
    if entry.get("state") == JOURNAL_STATE_DEAD_LETTER:
        # Manual retry from the dead-letter list — reset state so the
        # normal pipeline can recover or promote-again on failure.
        entry["state"] = JOURNAL_STATE_RETRY_SCHEDULED
    _journal_save(data)


def journal_mark_failure(wav_path: str, error_code: str, error_message: str,
                         failure_class: str = "transient_transport",
                         retryable: bool = True) -> str:
    """Record that the most recent attempt failed. Promotes the entry to
    dead_letter when either retryable=False or attempt_count reaches the
    threshold. Returns the resulting state string."""
    data = _journal_load()
    entry = data.get(wav_path)
    if not entry:
        return ""
    entry["last_error_code"]    = (str(error_code) if error_code else "")[:80]
    entry["last_error_message"] = (str(error_message) if error_message else "")[:300]
    entry["failure_class"]      = failure_class
    entry["retryable"]          = bool(retryable)
    attempts = int(entry.get("attempt_count", 0))
    if (not retryable) or attempts >= JOURNAL_DEAD_LETTER_THRESHOLD:
        entry["state"] = JOURNAL_STATE_DEAD_LETTER
        entry["next_retry_at"] = None
        log.warning(f"[JOURNAL] Promoted to dead_letter after {attempts} "
                    f"attempt(s): {wav_path}")
    else:
        entry["state"] = JOURNAL_STATE_RETRY_SCHEDULED
    _journal_save(data)
    return entry["state"]


def journal_reset_for_manual_retry(wav_path: str) -> None:
    """Reset a dead-letter entry so the user's 'Retry' click gets a fresh
    3-attempt budget. Called from the Failed Recordings dialog after the
    user has presumably fixed credentials / network / disk space."""
    data = _journal_load()
    entry = data.get(wav_path)
    if not entry:
        return
    entry["attempt_count"]      = 0
    entry["last_error_code"]    = None
    entry["last_error_message"] = None
    entry["state"]              = JOURNAL_STATE_PENDING
    entry["retryable"]          = True
    entry["next_retry_at"]      = None
    _journal_save(data)


def journal_pending() -> list[dict]:
    """Return the list of entries eligible for auto-retry on startup.
    Excludes dead_letter — those appear only in the Failed Recordings
    list, never re-prompted on every launch."""
    data = _journal_load()
    dirty = _journal_prune(data)
    out = []
    for path, meta in data.items():
        if meta.get("state") == JOURNAL_STATE_DEAD_LETTER:
            continue
        out.append({"wav_path": path, **meta})
    if dirty:
        _journal_save(data)
    return out


def journal_dead_letter() -> list[dict]:
    """Return only the dead-letter entries, newest first."""
    data = _journal_load()
    dirty = _journal_prune(data)
    out = []
    for path, meta in data.items():
        if meta.get("state") != JOURNAL_STATE_DEAD_LETTER:
            continue
        out.append({"wav_path": path, **meta})
    if dirty:
        _journal_save(data)
    out.sort(key=lambda e: e.get("recorded_at", ""), reverse=True)
    return out


def _is_valid_hotkey(hk):
    if not isinstance(hk, dict):
        return False
    t = hk.get("type")
    if t == "mod_only":
        keys = hk.get("keys")
        if not isinstance(keys, list) or len(keys) < 2:
            return False
        if not all(k in MOD_NAMES for k in keys):
            return False
        # activation field is optional; missing → fill with hold_to_talk on load
        act = hk.get("activation", "hold_to_talk")
        return act in ACTIVATION_MODES
    if t == "standard":
        mods = hk.get("modifiers") or []
        key  = hk.get("key")
        if not isinstance(mods, list):
            return False
        if not all(m in MOD_NAMES for m in mods):
            return False
        if not isinstance(key, str) or key not in STANDARD_KEYS and key not in {"escape", "tab", "space", "delete", "home", "end", "pageup", "pagedown", "insert", "up", "down", "left", "right"}:
            return False
        return True
    return False


def format_hotkey(hk, include_mode: bool = False):
    """Pretty-print a hotkey config for display in UI and tooltips.
    If include_mode is True, append '(hold to talk)' or '(tap to toggle)'
    for mod_only actions."""
    if not hk:
        return "—"
    if hk.get("type") == "mod_only":
        keys = hk.get("keys", [])
        ordered = [k for k in MOD_ORDER if k in keys]
        combo = " + ".join(MOD_DISPLAY[k] for k in ordered)
        if include_mode:
            act = hk.get("activation", "hold_to_talk")
            suffix = "hold to talk" if act == "hold_to_talk" else "tap to toggle"
            return f"{combo} ({suffix})"
        return combo
    if hk.get("type") == "standard":
        mods = hk.get("modifiers", [])
        ordered = [m for m in MOD_ORDER if m in mods]
        parts = [MOD_DISPLAY[m] for m in ordered]
        key = hk.get("key", "")
        parts.append(key.upper() if len(key) == 1 else key.capitalize())
        return " + ".join(parts)
    return "—"


def is_reserved_standard(modifiers, key):
    """Return 'critical' / 'high' / None for a standard combo."""
    mod_set = frozenset(modifiers)
    k = (key or "").lower()
    if (mod_set, k) in RESERVED_STANDARD_CRITICAL:
        return "critical"
    if (mod_set, k) in RESERVED_STANDARD_HIGH:
        return "high"
    return None


# --- Logging ---
def _next_log_file():
    today = datetime.now().strftime('%Y-%m-%d')
    n = 1
    while True:
        path = os.path.join(LOG_DIR, f"{today}_{n:03d}.log")
        if not os.path.exists(path):
            return path
        n += 1

log_file = _next_log_file()
_handlers = [logging.FileHandler(log_file, encoding="utf-8")]
if sys.stdout is not None:
    _handlers.append(logging.StreamHandler(sys.stdout))
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=_handlers,
)
log = logging.getLogger("kaira")

# --- Load API key ---
# --- API key sources (priority: env var → .env → Windows Credential Manager) ---
KEYRING_SERVICE = "Kaira"
KEYRING_USERNAME = "openai_api_key"

load_dotenv(os.path.join(SCRIPT_DIR, ".env"))


def _load_api_key_silent():
    """Return the OpenAI API key from env or keyring. None if neither has it."""
    key = os.getenv("OPENAI_API_KEY")
    if key and key != "your-key-here":
        return key
    try:
        key = keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME)
        if key:
            return key
    except Exception as e:
        log.warning(f"[KEY] keyring read failed: {e}")
    return None


class ApiKeyDialog(QDialog):
    """First-run dialog that prompts for an OpenAI API key and stores it
    in the Windows Credential Manager via the keyring library."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Kaira — OpenAI API Key Required")
        self.setModal(True)
        self.setMinimumWidth(520)
        self.setMaximumWidth(520)
        self.api_key = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 20)
        lay.setSpacing(14)

        title = QLabel("Welcome to Kaira")
        title.setStyleSheet("font-size: 16pt; font-weight: 600;")
        lay.addWidget(title)

        body = QLabel(
            "Kaira transcribes your speech using OpenAI's Whisper API. "
            "You need your own OpenAI API key. Typical cost is around $0.10 / month "
            "for regular dictation use.<br><br>"
            "Get a key at: "
            "<a href=\"https://platform.openai.com/api-keys\">"
            "platform.openai.com/api-keys</a><br><br>"
            "Your key is stored securely in Windows Credential Manager "
            "and only ever sent to OpenAI directly from your computer."
        )
        body.setTextFormat(Qt.TextFormat.RichText)
        body.setWordWrap(True)
        body.setOpenExternalLinks(True)
        body.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction
        )
        lay.addWidget(body)

        self._input = QLineEdit()
        self._input.setEchoMode(QLineEdit.EchoMode.Password)
        self._input.setPlaceholderText("sk-...")
        self._input.returnPressed.connect(self._on_save)
        lay.addWidget(self._input)

        self._error = QLabel("")
        self._error.setStyleSheet("color: #cc3333;")
        self._error.setVisible(False)
        lay.addWidget(self._error)

        btns = QHBoxLayout()
        btns.addStretch()
        quit_btn = QPushButton("Quit")
        quit_btn.clicked.connect(self.reject)
        save_btn = QPushButton("Save and Continue")
        save_btn.setDefault(True)
        save_btn.clicked.connect(self._on_save)
        btns.addWidget(quit_btn)
        btns.addWidget(save_btn)
        lay.addLayout(btns)

    def _on_save(self):
        key = self._input.text().strip()
        if not key.startswith("sk-") or len(key) < 20:
            self._error.setText("That doesn't look like a valid OpenAI key (should start with sk-).")
            self._error.setVisible(True)
            return
        try:
            keyring.set_password(KEYRING_SERVICE, KEYRING_USERNAME, key)
        except Exception as e:
            self._error.setText(f"Couldn't save to Credential Manager: {e}")
            self._error.setVisible(True)
            return
        self.api_key = key
        self.accept()


def _make_tray_icon(paused: bool = False) -> "QIcon":
    """Draw the Kaira waveform-bars tray icon programmatically. No image
    file dependency. Renders cleanly at 16px and 32px. When paused, bars
    are greyed and desaturated so users can see at a glance Kaira is off."""
    size = 32
    pm = QPixmap(size, size)
    pm.fill(QColor(0, 0, 0, 0))  # transparent
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#6b6b6b") if paused else QColor("#ff9500"))
    # Five bars — heights form a pyramid: short, medium, tall, medium, short
    heights = [10, 18, 26, 18, 10]
    bar_w = 3
    gap = 2
    total_w = 5 * bar_w + 4 * gap
    start_x = (size - total_w) // 2
    cy = size // 2
    for i, h in enumerate(heights):
        x = start_x + i * (bar_w + gap)
        y = cy - h // 2
        p.drawRoundedRect(x, y, bar_w, h, 1, 1)
    p.end()
    return QIcon(pm)


def pause_kaira():
    """Release ALL functional resources while keeping the process alive:
    unregister hotkeys, stop defense timers, hide overlay, stop any active
    recording. Tray icon goes grey. User can Resume from the tray."""
    if getattr(main, "_paused", False):
        return
    log.info("[PAUSE] Pausing Kaira...")
    widget = getattr(main, "_widget", None)
    app    = getattr(main, "_app", None)
    filt   = getattr(main, "_hotkey_filter", None)
    tray   = getattr(main, "_tray", None)

    # 1. Stop any active recording so we don't leave a mic stream open.
    if widget is not None:
        try:
            if widget._state in (RECORDING, COMMAND_RECORDING):
                widget.recorder.stop()
                widget._set_state(IDLE)
        except Exception as e:
            log.warning(f"[PAUSE] Recording stop: {e}")

    # 2. Hide the overlay (user intent flag + Qt hide + Win32 SW_HIDE).
    if widget is not None:
        widget._user_hidden = True
        widget.hide()
        try:
            ctypes.windll.user32.ShowWindow(int(widget.winId()), 0)  # SW_HIDE
        except Exception:
            pass
        # 3. Stop all defense layers (foreground hook, topmost timer, watchdog).
        try:
            widget.stop_defense()
        except Exception as e:
            log.warning(f"[PAUSE] stop_defense: {e}")

    # 4. Release hotkey registrations (Raw Input + RegisterHotKey).
    if filt is not None:
        try:
            filt.unregister()
        except Exception as e:
            log.warning(f"[PAUSE] Filter unregister: {e}")
        if app is not None:
            try:
                app.removeNativeEventFilter(filt)
            except Exception:
                pass
    main._hotkey_filter = None

    # 5. Update tray icon + menu to the paused state.
    if tray is not None:
        try:
            tray.setIcon(_make_tray_icon(paused=True))
            tray.setToolTip("Kaira — paused (click tray to resume)")
        except Exception:
            pass
    _refresh_tray_menu(paused=True)

    # 6. Persist state so boot-time honours it next launch.
    cfg = load_settings()
    cfg["paused"] = True
    save_settings(cfg)
    main._paused = True
    log.info("[PAUSE] Kaira paused — hotkeys released, overlay hidden")


def resume_kaira():
    """Re-acquire all functional resources: re-register hotkeys, restart
    defense timers, show overlay. Inverse of pause_kaira()."""
    if not getattr(main, "_paused", False):
        return
    log.info("[RESUME] Resuming Kaira...")
    widget = getattr(main, "_widget", None)
    app    = getattr(main, "_app", None)
    tray   = getattr(main, "_tray", None)
    hotkeys = load_settings()["hotkeys"]

    # 1. Re-create and install the hotkey filter.
    if app is not None and widget is not None:
        try:
            filt = setup_hotkeys(app, widget, hotkeys)
            main._hotkey_filter = filt
        except Exception as e:
            log.error(f"[RESUME] Failed to re-install hotkey filter: {e}")

    # 2. Show the overlay (clears hidden gate, Qt show, Win32 SW_SHOWNA).
    if widget is not None:
        widget._user_hidden = False
        widget.show()
        try:
            ctypes.windll.user32.ShowWindow(int(widget.winId()), 8)  # SW_SHOWNA
            _reassert_topmost(int(widget.winId()))
        except Exception:
            pass
        # 3. Restart defense layers.
        try:
            widget.start_defense()
        except Exception as e:
            log.warning(f"[RESUME] start_defense: {e}")

    # 4. Update tray icon + menu to the active state.
    if tray is not None:
        try:
            tray.setIcon(_make_tray_icon(paused=False))
            dic = load_settings()["hotkeys"]["dictation"]
            tray.setToolTip(f"Kaira — voice dictation ({format_hotkey(dic, include_mode=True)})")
        except Exception:
            pass
    _refresh_tray_menu(paused=False)

    # 5. Persist state.
    cfg = load_settings()
    cfg["paused"] = False
    save_settings(cfg)
    main._paused = False
    log.info("[RESUME] Kaira resumed")


def _refresh_tray_menu(paused: bool):
    """Toggle visibility of the dynamic Pause/Resume action based on state."""
    tray = getattr(main, "_tray", None)
    if tray is None:
        return
    pause_action  = getattr(tray, "_pause_action", None)
    resume_action = getattr(tray, "_resume_action", None)
    if pause_action is not None:
        pause_action.setVisible(not paused)
    if resume_action is not None:
        resume_action.setVisible(paused)


def _retry_pending_wav(widget, wav_path: str):
    """Re-run the transcription pipeline on an already-recorded WAV.
    Used by (a) the outage-toast click handler and (b) the startup scan
    when previous-session recordings are found in the journal.

    We reuse widget._transcribe_and_paste so the same VAD gate, retry
    logic, journal bookkeeping and pasting flow apply uniformly."""
    if not os.path.exists(wav_path):
        log.warning(f"[RETRY] WAV no longer exists: {wav_path}")
        journal_remove(wav_path)
        return
    try:
        with wave.open(wav_path, "rb") as wf:
            duration = wf.getnframes() / float(wf.getframerate())
    except Exception as e:
        log.warning(f"[RETRY] Could not read {wav_path}: {e}")
        duration = 0.0
    log.info(f"[RETRY] Retrying {wav_path} ({duration:.1f}s)")
    widget.state_changed.emit(PROCESSING)
    threading.Thread(
        target=widget._transcribe_and_paste,
        args=(wav_path, duration),
        daemon=True,
    ).start()


def _offer_startup_retries(widget, tray):
    """On boot, scan the journal for pending recordings from previous
    sessions. If any exist and their WAV files are still on disk, show a
    toast inviting the user to retry them. Clicking the toast fires all
    retries sequentially."""
    pending = journal_pending()
    if not pending:
        return
    log.info(f"[STARTUP-SCAN] Found {len(pending)} pending recording(s) "
             f"from previous session(s)")
    for entry in pending:
        log.info(f"[STARTUP-SCAN]   {entry['wav_path']} "
                 f"({entry.get('duration_s', '?')}s, "
                 f"recorded {entry.get('recorded_at', '?')})")

    if tray is None:
        return

    def _retry_all():
        log.info(f"[STARTUP-SCAN] User opted to retry {len(pending)} recording(s)")
        for entry in pending:
            _retry_pending_wav(widget, entry["wav_path"])

    # We can't reuse the same messageClicked signal handler because it's
    # already wired to the outage toast. Use a one-shot connection instead.
    def _on_startup_toast_clicked():
        try:
            tray.messageClicked.disconnect(_on_startup_toast_clicked)
        except TypeError:
            pass
        _retry_all()

    tray.messageClicked.connect(_on_startup_toast_clicked)
    tray.showMessage(
        f"Kaira — {len(pending)} unsent recording(s)",
        "Kaira found recordings from a previous session that weren't "
        "transcribed. Click this notification to retry them now.",
        QSystemTrayIcon.MessageIcon.Information,
        15000,
    )


class FailedRecordingsDialog(QDialog):
    """Dead-letter list view. One row per failed recording with per-entry
    Retry / Reveal / Delete actions, plus Close at the bottom. Replaces the
    old 'prompt every launch' behaviour so the same poisoned file never
    traps the user in an infinite-retry loop."""

    def __init__(self, widget, parent=None):
        super().__init__(parent)
        self._widget = widget
        self.setWindowTitle("Kaira — Failed recordings")
        self.setMinimumWidth(620)
        self.setMinimumHeight(340)
        self._build_ui()
        self._refresh()

    def _build_ui(self):
        outer = QVBoxLayout(self)

        intro = QLabel(
            "These recordings couldn't be transcribed after 3 attempts. "
            "Your audio is safe on disk — fix the underlying issue "
            "(network, API key, disk space), then click Retry.\n"
            "Dead-letter entries auto-delete after 30 days."
        )
        intro.setWordWrap(True)
        outer.addWidget(intro)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(self._scroll, stretch=1)

        self._list_host = QWidget()
        self._list_layout = QVBoxLayout(self._list_host)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(6)
        self._scroll.setWidget(self._list_host)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        outer.addLayout(btn_row)

    def _refresh(self):
        # Clear existing rows
        while self._list_layout.count():
            item = self._list_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        entries = journal_dead_letter()
        if not entries:
            empty = QLabel("No failed recordings. 🎉")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setStyleSheet("color: #888; padding: 40px;")
            self._list_layout.addWidget(empty)
            self._list_layout.addStretch(1)
            return

        for entry in entries:
            self._list_layout.addWidget(self._make_row(entry))
        self._list_layout.addStretch(1)

    def _make_row(self, entry: dict) -> QWidget:
        row = QFrame()
        row.setFrameShape(QFrame.Shape.StyledPanel)
        row.setStyleSheet(
            "QFrame { border: 1px solid #d0d0d0; border-radius: 6px; "
            "padding: 8px; background: #fafafa; }"
        )
        layout = QVBoxLayout(row)
        layout.setContentsMargins(8, 6, 8, 6)

        wav_path = entry.get("wav_path", "")
        filename = os.path.basename(wav_path) if wav_path else "(unknown)"
        duration_s = entry.get("duration_s", 0) or 0
        recorded_at = entry.get("recorded_at", "(unknown date)")
        attempts = entry.get("attempt_count", 0)
        err_code = entry.get("last_error_code") or "—"
        err_msg = entry.get("last_error_message") or ""
        mode = entry.get("mode", "dictation")

        header = QLabel(f"<b>{filename}</b>  "
                        f"<span style='color:#666'>"
                        f"{duration_s:.1f}s · {mode} · recorded {recorded_at} · "
                        f"{attempts} attempts"
                        f"</span>")
        header.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(header)

        err_label = QLabel(f"<span style='color:#b33'>Last error: {err_code}</span>"
                           + (f" — {err_msg}" if err_msg else ""))
        err_label.setTextFormat(Qt.TextFormat.RichText)
        err_label.setWordWrap(True)
        layout.addWidget(err_label)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)

        retry_btn = QPushButton("Retry")
        retry_btn.clicked.connect(lambda _c=False, p=wav_path: self._on_retry(p))
        btn_row.addWidget(retry_btn)

        reveal_btn = QPushButton("Reveal file")
        reveal_btn.clicked.connect(lambda _c=False, p=wav_path: self._on_reveal(p))
        btn_row.addWidget(reveal_btn)

        delete_btn = QPushButton("Delete")
        delete_btn.clicked.connect(lambda _c=False, p=wav_path: self._on_delete(p))
        btn_row.addWidget(delete_btn)

        btn_row.addStretch(1)
        layout.addLayout(btn_row)
        return row

    def _on_retry(self, wav_path: str):
        if not wav_path or not os.path.exists(wav_path):
            QMessageBox.warning(self, "Kaira",
                                "The audio file is no longer on disk. "
                                "Removing this entry.")
            journal_remove(wav_path)
            self._widget.journal_changed.emit()
            self._refresh()
            return
        log.info(f"[FAILED-LIST] Manual retry requested: {wav_path}")
        journal_reset_for_manual_retry(wav_path)
        _retry_pending_wav(self._widget, wav_path)
        self._widget.journal_changed.emit()
        self._refresh()

    def _on_reveal(self, wav_path: str):
        if not wav_path or not os.path.exists(wav_path):
            QMessageBox.warning(self, "Kaira", "The file is no longer on disk.")
            return
        try:
            # Open Explorer with the file selected.
            subprocess.Popen(["explorer", "/select,", os.path.normpath(wav_path)])
        except Exception as e:
            log.warning(f"[FAILED-LIST] Reveal failed: {e}")

    def _on_delete(self, wav_path: str):
        if QMessageBox.question(
            self, "Kaira — Delete recording",
            f"Delete {os.path.basename(wav_path)} and its journal entry?\n\n"
            "The WAV file will be removed from disk.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            if wav_path and os.path.exists(wav_path):
                os.remove(wav_path)
        except Exception as e:
            log.warning(f"[FAILED-LIST] Could not delete WAV: {e}")
        journal_remove(wav_path)
        self._widget.journal_changed.emit()
        self._refresh()


def _restart_kaira_now():
    """Relaunch Kaira: release the single-instance lock, spawn a fresh
    detached process, then os._exit. Called from tray > Restart Kaira and
    from Preferences > Save > Restart Now."""
    lock = getattr(main, "_lock", None)
    if lock is not None and hasattr(lock, "fileno") and msvcrt is not None:
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        except Exception as e:
            log.warning(f"[RESTART] Lock unlock failed (continuing): {e}")
        try:
            lock.close()
        except Exception:
            pass
    if getattr(sys, "frozen", False):
        args = [sys.executable] + sys.argv[1:]
    else:
        args = [sys.executable] + sys.argv
    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    try:
        subprocess.Popen(
            args,
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
            cwd=SCRIPT_DIR,
        )
        log.info(f"[RESTART] Spawned new Kaira: {args}")
    except Exception as e:
        log.error(f"[RESTART] Failed to spawn new Kaira: {e}")
        return
    os._exit(0)


def _setup_tray(app, widget, hotkey_filter=None, start_paused: bool = False):
    """Add a Windows notification-area tray icon. Menu items: dynamic
    Pause/Resume, Preferences, Open log folder, Restart, Quit. The Pause
    and Resume actions are mutually exclusive — setVisible toggles them
    based on current runtime state."""
    if not QSystemTrayIcon.isSystemTrayAvailable():
        log.warning("[TRAY] System tray not available on this system")
        return None
    tray = QSystemTrayIcon(_make_tray_icon(paused=start_paused), parent=app)
    try:
        if start_paused:
            tray.setToolTip("Kaira — paused (click tray to resume)")
        else:
            cfg = load_settings()["hotkeys"]["dictation"]
            tray.setToolTip(f"Kaira — voice dictation ({format_hotkey(cfg, include_mode=True)})")
    except Exception:
        tray.setToolTip("Kaira — voice dictation")

    menu = QMenu(widget)

    def _open_logs():
        try:
            os.startfile(LOG_DIR)
            log.info(f"[TRAY] Opened log folder: {LOG_DIR}")
        except Exception as e:
            log.warning(f"[TRAY] Could not open log folder: {e}")

    def _open_preferences():
        filt = getattr(main, "_hotkey_filter", None)
        if filt is None:
            QMessageBox.information(
                widget, "Kaira — Resume first",
                "Kaira is paused. Resume it before opening Preferences."
            )
            return
        log.info("[TRAY] Opening Preferences dialog")
        dlg = getattr(main, "_prefs_dialog", None)
        if dlg is None:
            dlg = PreferencesDialog(filt, parent=widget)
            main._prefs_dialog = dlg
        else:
            dlg.set_filter(filt)
            dlg.reload_from_settings()
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _restart_kaira():
        log.info("[TRAY] Restart requested")
        _restart_kaira_now()

    def _quit():
        log.info("[TRAY] Quit from tray menu")
        os._exit(0)

    def _toggle_startup_on_login(checked: bool):
        """Called when the user toggles the 'Start Kaira at Windows sign-in'
        checkbox. Writes or removes the HKCU Run registry entry. Never
        auto-re-enables — if the user unchecks, stays unchecked."""
        ok = enable_startup_on_login() if checked else disable_startup_on_login()
        if not ok:
            QMessageBox.warning(
                widget, "Kaira — couldn't update startup setting",
                "Kaira couldn't write to the Windows registry. "
                "Check the log folder for details."
            )
            startup_action.setChecked(is_startup_on_login_enabled())

    # Create the Pause/Resume actions explicitly so we can toggle visibility.
    pause_action  = menu.addAction("Pause Kaira",  pause_kaira)
    resume_action = menu.addAction("Resume Kaira", resume_kaira)
    pause_action.setVisible(not start_paused)
    resume_action.setVisible(start_paused)
    tray._pause_action  = pause_action
    tray._resume_action = resume_action

    # v1.2.0 hotfix — Failed recordings (N) badge. Only visible when
    # dead-letter entries exist. Clicking opens the FailedRecordingsDialog
    # where the user can retry / reveal / delete each one.
    def _open_failed_recordings():
        dlg = FailedRecordingsDialog(widget, parent=widget)
        dlg.exec()
        _refresh_failed_count()

    failed_action = menu.addAction("Failed recordings", _open_failed_recordings)
    tray._failed_action = failed_action

    def _refresh_failed_count():
        try:
            n = len(journal_dead_letter())
        except Exception as e:
            log.warning(f"[TRAY] Failed-count refresh error: {e}")
            n = 0
        if n > 0:
            failed_action.setText(f"Failed recordings ({n})")
            failed_action.setVisible(True)
        else:
            failed_action.setText("Failed recordings")
            failed_action.setVisible(False)

    _refresh_failed_count()
    menu.aboutToShow.connect(_refresh_failed_count)
    widget.journal_changed.connect(_refresh_failed_count)

    menu.addSeparator()
    menu.addAction("Preferences...", _open_preferences)
    startup_action = menu.addAction(
        "Start Kaira at Windows sign-in", _toggle_startup_on_login
    )
    startup_action.setCheckable(True)
    startup_action.setChecked(is_startup_on_login_enabled())
    menu.addAction("Open log folder", _open_logs)
    menu.addSeparator()
    menu.addAction("Restart Kaira", _restart_kaira)
    menu.addAction("Quit Kaira", _quit)

    def _on_activate(reason):
        log.info(f"[TRAY] activated reason={reason}")
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
            QSystemTrayIcon.ActivationReason.MiddleClick,
            QSystemTrayIcon.ActivationReason.Context,
        ):
            menu.exec(QCursor.pos())
    tray.setContextMenu(menu)
    tray.activated.connect(_on_activate)

    # --- v1.2 toast notifications via tray.showMessage ---
    # When VAD skips a recording or Whisper retries are exhausted, the
    # widget emits a signal on its transcription thread; we route it through
    # tray.showMessage which handles Windows 10 balloons and Windows 11
    # WinRT toasts automatically (Qt 6.4+).

    _pending_retry = {"wav_path": None}   # captured by closures below

    def _on_no_speech(reason: str):
        log.info(f"[TRAY] Showing 'no speech' toast (reason={reason})")
        tray.showMessage(
            "Kaira — No speech detected",
            "Your recording didn't contain any speech, so nothing was "
            "sent for transcription. Press the hotkey and speak, or "
            "disable the silence filter in Preferences.",
            QSystemTrayIcon.MessageIcon.Information,
            4000,  # 4 seconds visible
        )

    def _on_transcription_failed(wav_path: str):
        log.info(f"[TRAY] Showing 'OpenAI outage' toast for {wav_path}")
        _pending_retry["wav_path"] = wav_path
        tray.showMessage(
            "OpenAI is unavailable",
            "Your recording is saved. Click this notification to retry "
            "when OpenAI is back. Kaira will also offer to retry on next launch.",
            QSystemTrayIcon.MessageIcon.Warning,
            15000,  # 15 seconds visible
        )

    def _on_toast_clicked():
        wav = _pending_retry.get("wav_path")
        if not wav:
            return
        _pending_retry["wav_path"] = None
        log.info(f"[TRAY] User clicked outage toast — retrying {wav}")
        _retry_pending_wav(widget, wav)

    def _on_fell_back_to_local():
        log.info("[TRAY] Showing 'used local fallback' toast")
        tray.showMessage(
            "Kaira — used local fallback",
            "Cloud Whisper was unavailable, so Kaira transcribed locally "
            "on your CPU. Your text is pasted. Accuracy may differ slightly "
            "from the cloud model.",
            QSystemTrayIcon.MessageIcon.Information,
            6000,  # 6 seconds visible
        )

    tray.messageClicked.connect(_on_toast_clicked)
    widget.no_speech_detected.connect(_on_no_speech)
    widget.transcription_failed.connect(_on_transcription_failed)
    widget.fell_back_to_local.connect(_on_fell_back_to_local)

    tray.show()
    log.info("[TRAY] System tray icon installed")
    return tray


def _check_microphone_or_prompt():
    """Verify the default input device exists AND can actually open a stream.
    Distinguishes 'no hardware' from 'Windows Privacy mic toggle is off'.
    Shows an actionable dialog and returns mic_name on success, None on
    failure after user dismisses the dialog."""
    # Step 1: is there a device at all?
    try:
        default_input = sd.query_devices(kind="input")
        mic_name = default_input["name"]
    except Exception as e:
        log.error(f"[MIC] No input device found: {e}")
        QMessageBox.critical(
            None, "Kaira — No Microphone Found",
            "Kaira couldn't find any microphone on this system.\n\n"
            "Make sure a microphone is connected, then restart Kaira.",
        )
        return None

    # Step 2: can we actually open a stream? Windows 11 Privacy toggle
    # blocks desktop apps silently — query_devices still succeeds, but
    # InputStream.start() raises PortAudioError.
    try:
        with sd.InputStream(channels=1, samplerate=16000, blocksize=160):
            pass  # opening + closing is enough; no need to actually read
    except Exception as e:
        log.error(f"[MIC] Mic found ('{mic_name}') but stream failed: {e}")
        box = QMessageBox()
        box.setWindowTitle("Kaira — Microphone Access Blocked")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText("Kaira found a microphone but Windows blocked access to it.")
        box.setInformativeText(
            "This is usually because Windows Privacy settings have desktop "
            "microphone access turned off.\n\n"
            "Click 'Open Settings' below — then turn ON:\n"
            "  • 'Microphone access'\n"
            "  • 'Let desktop apps access your microphone'\n\n"
            "Then close and reopen Kaira."
        )
        open_btn = box.addButton("Open Settings", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Quit", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is open_btn:
            try:
                os.startfile("ms-settings:privacy-microphone")
                log.info("[MIC] Opened ms-settings:privacy-microphone")
            except Exception as e2:
                log.warning(f"[MIC] Failed to open settings URI: {e2}")
        return None

    return mic_name


def _startup_registry_command() -> str:
    """Build the command string Windows should run at user login to start
    Kaira. Uses the start-silent.vbs launcher if it exists (preferred — runs
    pythonw.exe via Windows Script Host so no console flashes), otherwise
    falls back to the current interpreter + script path. Handles PyInstaller
    --onefile (sys.frozen) cleanly."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    vbs_launcher = os.path.join(SCRIPT_DIR, "start-silent.vbs")
    if os.path.exists(vbs_launcher):
        # wscript.exe runs .vbs scripts without a console window
        return f'wscript.exe "{vbs_launcher}"'
    # Fallback: direct pythonw.exe with the main script
    script = os.path.join(SCRIPT_DIR, "kaira.py")
    return f'"{sys.executable}" "{script}"'


STARTUP_REGISTRY_KEY   = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_REGISTRY_VALUE = "Kaira"


def is_startup_on_login_enabled() -> bool:
    """Return True if Kaira is registered to launch at Windows sign-in."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_REGISTRY_KEY, 0,
                            winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, STARTUP_REGISTRY_VALUE)
            return bool(value)
    except FileNotFoundError:
        return False
    except OSError:
        return False


def enable_startup_on_login() -> bool:
    """Register Kaira to launch automatically at the next Windows sign-in.
    Returns True on success. Writes to HKCU\\...\\Run — user-level only,
    no admin / UAC required."""
    try:
        import winreg
        command = _startup_registry_command()
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_REGISTRY_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, STARTUP_REGISTRY_VALUE, 0,
                              winreg.REG_SZ, command)
        log.info(f"[STARTUP] Enabled startup-on-login: {command}")
        return True
    except Exception as e:
        log.error(f"[STARTUP] Failed to enable startup-on-login: {e}")
        return False


def disable_startup_on_login() -> bool:
    """Remove Kaira from Windows sign-in launch list. Returns True on
    success or if already disabled."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_REGISTRY_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            try:
                winreg.DeleteValue(key, STARTUP_REGISTRY_VALUE)
                log.info("[STARTUP] Disabled startup-on-login")
            except FileNotFoundError:
                log.info("[STARTUP] Startup-on-login already disabled")
        return True
    except Exception as e:
        log.error(f"[STARTUP] Failed to disable startup-on-login: {e}")
        return False


def _check_sticky_keys_advisory():
    """Check Windows Sticky Keys state. If enabled, log an advisory — Sticky
    Keys auto-disables itself when two keys are pressed together by default,
    which will trip on every Kaira hotkey activation. The user can turn off
    that auto-disable in Accessibility settings, or disable Sticky Keys
    entirely. This is an advisory only — Kaira continues to run normally."""
    try:
        # STICKYKEYS struct: cbSize (UINT) + dwFlags (DWORD) = 8 bytes total.
        # SKF_STICKYKEYSON = 0x00000001 — Sticky Keys is active.
        class _STICKYKEYS(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwFlags", ctypes.c_uint)]
        sk = _STICKYKEYS()
        sk.cbSize = ctypes.sizeof(_STICKYKEYS)
        SPI_GETSTICKYKEYS = 0x003A
        ok = user32.SystemParametersInfoW(
            SPI_GETSTICKYKEYS, ctypes.sizeof(_STICKYKEYS), ctypes.byref(sk), 0
        )
        if ok and (sk.dwFlags & 0x00000001):
            log.warning(
                "[STICKY-KEYS] Windows Sticky Keys is enabled. Sticky Keys "
                "will auto-disable itself whenever you press a Kaira hotkey "
                "combo, because its default setting turns Sticky Keys off "
                "when two keys are pressed together. To avoid that: open "
                "Windows Settings > Accessibility > Keyboard > Sticky Keys "
                "and either turn Sticky Keys off entirely, or uncheck 'Turn "
                "off Sticky Keys when two keys are pressed at the same time'."
            )
        else:
            log.info("[STICKY-KEYS] Not enabled — no advisory needed.")
    except Exception as e:
        log.info(f"[STICKY-KEYS] Check skipped ({e})")


def ensure_api_key(app):
    """Return a valid API key, prompting the user via dialog if none found.
    Exits the process cleanly if the user refuses to supply a key."""
    key = _load_api_key_silent()
    if key:
        log.info("[KEY] Loaded from env/keyring")
        return key
    log.info("[KEY] No key found — prompting user")
    dlg = ApiKeyDialog()
    if dlg.exec() == QDialog.DialogCode.Accepted and dlg.api_key:
        log.info("[KEY] User supplied key — saved to keyring")
        return dlg.api_key
    log.info("[KEY] User cancelled — exiting")
    return None

# Client is initialized with a placeholder key here — the real key is
# loaded from keyring / .env / user prompt in main() and set via
# client.api_key = real_key BEFORE any API call happens. We can't defer
# construction entirely because transcribe() and cleanup_text() reference
# the module-level `client` variable. The placeholder avoids the
# "api_key must be set" crash in PyInstaller builds where no .env exists.
#
# v1.2.0 hotfix — split HTTPX timeouts + disable SDK retries:
#   - connect/write/read/pool timeouts each bounded explicitly so a stalled
#     upload (Cloudflare kills at ~100s) fails fast instead of waiting on
#     the SDK's 600s default. The Apr-17 Rain-LTE outage showed stacked
#     waits burning 20 minutes of wall-clock time per recording.
#   - max_retries=0: tenacity is the only retry owner. Without this, the
#     SDK's built-in 2-retry policy multiplied against tenacity's 3 → up
#     to 9 attempts per recording.
# Reference: research/Kaira v1.3 Research Recommendations.md
client = OpenAI(
    api_key=os.getenv("OPENAI_API_KEY") or "sk-placeholder-set-in-main",
    timeout=httpx.Timeout(connect=10.0, write=45.0, read=60.0, pool=10.0),
    max_retries=0,
)

# --- Audio settings ---
SAMPLE_RATE = 16000
CHANNELS = 1


# ============================================================
# AUDIO RECORDER
# ============================================================

class Recorder:
    def __init__(self):
        self.is_recording = False
        self.audio_chunks = []
        self.stream = None
        self.current_level = 0.0

    def start(self):
        self.audio_chunks = []
        self.current_level = 0.0
        self.is_recording = True
        log.info("[RECORDER] Opening audio stream...")
        try:
            self.stream = sd.InputStream(
                samplerate=SAMPLE_RATE, channels=CHANNELS,
                dtype="int16", callback=self._callback, blocksize=1024,
            )
            self.stream.start()
            log.info("[RECORDER] Audio stream active")
        except Exception as e:
            log.error(f"[RECORDER] FAILED to open stream: {e}")
            self.is_recording = False

    def _callback(self, indata, frames, time_info, status):
        if self.is_recording:
            self.audio_chunks.append(indata.copy())
            rms = np.sqrt(np.mean(indata.astype(np.float32) ** 2))
            self.current_level = min(1.0, rms / 3000.0)

    def stop(self):
        self.is_recording = False
        log.info("[RECORDER] Stopping stream...")
        if self.stream:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception as e:
                log.error(f"[RECORDER] Error closing stream: {e}")
            self.stream = None
        if not self.audio_chunks:
            log.info("[RECORDER] No audio chunks captured")
            return None, 0.0
        audio = np.concatenate(self.audio_chunks, axis=0)
        duration = len(audio) / SAMPLE_RATE
        log.info(f"[RECORDER] Captured {len(self.audio_chunks)} chunks, {duration:.1f}s")
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        path = os.path.join(AUDIO_DIR, f"{stamp}.wav")
        with wave.open(path, "wb") as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(audio.tobytes())
        log.info(f"[RECORDER] Saved: {path}")
        return path, duration


# ============================================================
# TRANSCRIPTION & PASTE
# ============================================================

# --- Silero VAD singleton ---
# Created lazily on first use, warmed once at boot via _warmup_vad() so the
# first real user dictation isn't slow. silero-vad-lite uses an internal
# ONNX runtime — no PyTorch, no separate onnxruntime install.
_vad_instance = None


def _get_vad() -> "SileroVAD | None":
    global _vad_instance
    if not _VAD_AVAILABLE:
        return None
    if _vad_instance is None:
        try:
            _vad_instance = SileroVAD(sample_rate=16000)
            log.info("[VAD] Silero VAD loaded (sample_rate=16000)")
        except Exception as e:
            log.error(f"[VAD] Failed to load Silero VAD: {e}")
            return None
    return _vad_instance


def _warmup_vad():
    """Run one dummy inference so first real user dictation hits a warm model.
    Silero's first call is ~5-15x slower than subsequent calls. silero-vad-lite
    expects float32 PCM in [-1.0, 1.0], exactly window_size_samples long
    (512 samples = 32ms at 16kHz)."""
    vad = _get_vad()
    if vad is None:
        return
    try:
        dummy = np.zeros(512, dtype=np.float32).tobytes()
        vad.process(dummy)
        log.info("[VAD] Warmup complete")
    except Exception as e:
        log.warning(f"[VAD] Warmup skipped: {e}")


def _read_wav_as_float32(path: str) -> tuple[np.ndarray, int]:
    """Load a mono 16kHz WAV file as float32 in [-1.0, 1.0]. Returns
    (samples, sample_rate)."""
    with wave.open(path, "rb") as wf:
        sr = wf.getframerate()
        n_ch = wf.getnchannels()
        sample_width = wf.getsampwidth()
        frames = wf.readframes(wf.getnframes())
    if sample_width != 2:
        raise ValueError(f"expected 16-bit PCM, got {sample_width*8}-bit")
    pcm_int16 = np.frombuffer(frames, dtype=np.int16)
    if n_ch > 1:
        pcm_int16 = pcm_int16.reshape(-1, n_ch)[:, 0]  # take channel 0 only
    return pcm_int16.astype(np.float32) / 32768.0, sr


def vad_gate(audio_path: str, vad_cfg: dict) -> tuple[bool, str, dict]:
    """Decide whether to send a recording to Whisper.
    Returns (should_transcribe, reason, diagnostics). Pipeline ordering:
      Layer 0: user disabled VAD in Preferences → always transcribe
      Layer 1: RMS energy check (ALWAYS runs regardless of duration — it's
               ~0.1ms and catches accidental-tap silence at any length)
      Duration bypass: recordings shorter than skip_below_ms or longer than
               skip_above_ms skip Layer 2 only. They still face the RMS gate.
      Layer 2: Silero VAD peak score + consecutive-chunks check
    reason is one of: vad_disabled, silence_rms, too_short_skip_silero,
    too_long_skip_silero, no_speech_vad, speech_detected, vad_unavailable."""
    diag = {"reason": "", "duration_ms": 0.0, "rms": 0.0,
            "max_score": 0.0, "chunks_above": 0, "total_chunks": 0,
            "threshold": vad_cfg.get("threshold", 0.30)}

    if not vad_cfg.get("enabled", True):
        diag["reason"] = "vad_disabled"
        return True, "vad_disabled", diag

    try:
        pcm, sr = _read_wav_as_float32(audio_path)
    except Exception as e:
        log.warning(f"[VAD] Could not read {audio_path}: {e} — skipping gate")
        diag["reason"] = "read_error"
        return True, "read_error", diag

    duration_ms = len(pcm) / sr * 1000.0
    diag["duration_ms"] = round(duration_ms, 1)

    # Layer 1: RMS energy pre-gate — always runs. Rejects true silence at
    # any duration (including very short accidental taps).
    rms = float(np.sqrt(np.mean(pcm ** 2)))
    diag["rms"] = round(rms, 4)
    rms_floor = vad_cfg.get("rms_silence_floor", 0.005)
    if rms < rms_floor:
        diag["reason"] = "silence_rms"
        log.info(f"[VAD] No speech detected — audio is silent "
                 f"(RMS energy {rms:.4f} below floor {rms_floor:.4f}). "
                 f"Skipping Whisper call.")
        return False, "silence_rms", diag

    # Duration-based bypass for Silero (Layer 2) only. Recordings below
    # skip_below_ms don't give Silero enough chunks for reliable detection;
    # recordings above skip_above_ms were clearly intentional (hard to hold
    # a key for that long by accident). In both cases we let Whisper decide.
    if duration_ms < vad_cfg.get("skip_below_ms", 500):
        diag["reason"] = "too_short_skip_silero"
        log.info(f"[VAD] RMS passed ({rms:.4f}). Skipping Silero — recording "
                 f"only {duration_ms:.0f}ms (below "
                 f"{vad_cfg.get('skip_below_ms', 500)}ms). Sending to Whisper.")
        return True, "too_short_skip_silero", diag
    if duration_ms > vad_cfg.get("skip_above_ms", 10000):
        diag["reason"] = "too_long_skip_silero"
        log.info(f"[VAD] RMS passed ({rms:.4f}). Skipping Silero — recording "
                 f"{duration_ms:.0f}ms (above "
                 f"{vad_cfg.get('skip_above_ms', 10000)}ms, assuming "
                 f"intentional speech). Sending to Whisper.")
        return True, "too_long_skip_silero", diag

    # Layer 2: Silero VAD
    vad = _get_vad()
    if vad is None:
        log.warning(f"[VAD] Silero unavailable — passing through to Whisper")
        diag["reason"] = "vad_unavailable"
        return True, "vad_unavailable", diag

    chunk_size = 512  # 32ms at 16kHz — Silero's native chunk size
    threshold = vad_cfg.get("threshold", 0.30)
    min_chunks = vad_cfg.get("min_speech_chunks", 2)
    scores = []
    try:
        # silero-vad-lite expects float32 PCM in [-1.0, 1.0], exactly 512
        # samples per process() call. pcm is already float32 from WAV load.
        for i in range(0, len(pcm) - chunk_size + 1, chunk_size):
            chunk = np.ascontiguousarray(pcm[i:i + chunk_size], dtype=np.float32)
            score = vad.process(chunk.tobytes())
            scores.append(float(score))
    except Exception as e:
        log.warning(f"[VAD] Silero error ({e}) — passing through to Whisper")
        diag["reason"] = "vad_error"
        return True, "vad_error", diag

    if not scores:
        diag["reason"] = "no_chunks"
        return True, "no_chunks", diag

    max_score = max(scores)
    chunks_above = sum(1 for s in scores if s >= threshold)
    diag["max_score"]    = round(max_score, 3)
    diag["chunks_above"] = chunks_above
    diag["total_chunks"] = len(scores)

    # Consecutive-chunk check — a single isolated peak (like a keyboard click)
    # shouldn't trigger transcription. Need at least min_chunks in a row.
    consecutive = 0
    detected = False
    for s in scores:
        if s >= threshold:
            consecutive += 1
            if consecutive >= min_chunks:
                detected = True
                break
        else:
            consecutive = 0

    if detected:
        diag["reason"] = "speech_detected"
        log.info(f"[VAD] Speech detected — peak score {max_score:.3f}, "
                 f"{chunks_above}/{len(scores)} chunks above {threshold:.2f}. "
                 f"Sending to Whisper.")
        return True, "speech_detected", diag

    diag["reason"] = "no_speech_vad"
    log.info(f"[VAD] No speech detected — peak score {max_score:.3f}, "
             f"only {chunks_above}/{len(scores)} chunks above {threshold:.2f} "
             f"(need {min_chunks} consecutive). Skipping Whisper call.")
    return False, "no_speech_vad", diag


def is_hallucinated(text: str) -> bool:
    """Return True if Whisper's output matches a known-hallucination phrase
    exactly (case-insensitive, full string). Never substring-matches —
    'Thank you' alone is legitimate dictation."""
    if not text:
        return False
    normalised = text.strip().lower()
    return normalised in HALLUCINATION_PHRASES


# Transient HTTP status codes worth retrying — research-backed narrow set.
# 408 request timeout, 429 rate limit, 5xx server errors.
TRANSIENT_STATUS_CODES = {408, 429, 500, 502, 503, 504}


def _is_retryable_openai_error(exc: BaseException) -> bool:
    """Predicate for tenacity: retry only on transient transport or status
    failures that a second attempt can plausibly recover. Deliberately
    narrow so auth errors, bad-request errors, and permanent 4xx responses
    fail fast instead of burning the tenacity budget."""
    # Transport-level transient failures (both OpenAI SDK wrappers and the
    # underlying httpx exceptions — the SDK sometimes re-raises httpx
    # errors directly on lower-level failures).
    if isinstance(exc, (
        openai.APIConnectionError,
        openai.APITimeoutError,
        httpx.ConnectError,
        httpx.ConnectTimeout,
        httpx.ReadTimeout,
        httpx.WriteTimeout,
    )):
        return True
    # HTTP status errors — only the explicit transient set.
    # RateLimitError (429) is a subclass of APIStatusError so it's covered
    # here via status_code matching.
    if isinstance(exc, openai.APIStatusError):
        try:
            return exc.status_code in TRANSIENT_STATUS_CODES
        except Exception:
            return False
    return False


@retry(
    retry=retry_if_exception(_is_retryable_openai_error),
    wait=wait_random_exponential(multiplier=1, min=1, max=8),
    stop=stop_after_attempt(3),
    reraise=False,  # wrap in RetryError so callers can inspect e.last_attempt
)
def _call_whisper_api(audio_path: str) -> str:
    """Single Whisper API call, hardened against hallucinations via
    temperature=0 + explicit language. No instruction-style prompt — the
    Whisper `prompt` parameter is a VOCABULARY BIAS, not a directive. An
    instruction in the prompt will be echoed back as transcription when the
    audio is silent. The VAD gate (RMS + Silero) runs before this function
    to stop silent audio reaching Whisper in the first place."""
    with open(audio_path, "rb") as f:
        response = client.audio.transcriptions.create(
            model="whisper-1",
            file=f,
            language="en",
            response_format="text",
            temperature=0,
        )
    return response.strip()


# --- faster-whisper local fallback (v1.2.0) ---
# When cloud Whisper exhausts its retry budget (APIConnectionError,
# APITimeoutError, or transient 5xx), we transcribe the same WAV locally
# using faster-whisper running on CPU. Same Silero VAD gate still runs
# upstream, same AI cleanup still runs downstream — only the Whisper
# step swaps out. Model is lazily downloaded to %APPDATA%\Kaira\models\
# on first fallback use (~145 MB, ~1-2 min on normal net). Subsequent
# fallbacks reuse the cached model.
_fw_model = None                # singleton
_fw_import_failed = False       # true after the first failed import attempt
_fw_load_error = None           # last load error string for surfacing


def _local_fallback_available() -> bool:
    """Return True if faster-whisper can be imported. Cached after the
    first attempt so we don't re-try a failed import on every call."""
    global _fw_import_failed
    if _fw_import_failed:
        return False
    try:
        import faster_whisper  # noqa: F401
        import ctranslate2     # noqa: F401
        return True
    except Exception as e:
        _fw_import_failed = True
        log.warning(f"[LOCAL] faster-whisper unavailable: {e}")
        return False


def _get_local_model():
    """Lazy-load the faster-whisper WhisperModel. Downloads weights to
    MODELS_DIR on first call — subsequent calls are instant."""
    global _fw_model, _fw_load_error
    if _fw_model is not None:
        return _fw_model
    if not _local_fallback_available():
        return None
    try:
        from faster_whisper import WhisperModel
    except Exception as e:
        _fw_load_error = str(e)
        log.error(f"[LOCAL] Import failed: {e}")
        return None

    log.info(f"[LOCAL] Loading {LOCAL_MODEL_NAME} "
             f"({LOCAL_MODEL_COMPUTE_TYPE}, {LOCAL_MODEL_CPU_THREADS} threads) "
             f"from {MODELS_DIR} — may download ~145 MB on first use")
    t0 = time.time()
    try:
        _fw_model = WhisperModel(
            LOCAL_MODEL_NAME,
            device=LOCAL_MODEL_DEVICE,
            compute_type=LOCAL_MODEL_COMPUTE_TYPE,
            cpu_threads=LOCAL_MODEL_CPU_THREADS,
            download_root=MODELS_DIR,
        )
        log.info(f"[LOCAL] Model ready in {time.time()-t0:.1f}s")
        return _fw_model
    except Exception as e:
        _fw_load_error = str(e)
        log.error(f"[LOCAL] Model load failed ({time.time()-t0:.1f}s): {e}",
                  exc_info=True)
        return None


def _transcribe_locally(audio_path: str) -> str:
    """Run faster-whisper on audio_path. Raises RuntimeError if the model
    can't be loaded. The caller (transcribe) surfaces that to the user."""
    model = _get_local_model()
    if model is None:
        raise RuntimeError(f"Local Whisper unavailable ({_fw_load_error or 'not installed'})")
    log.info(f"[LOCAL] Transcribing {audio_path} on CPU...")
    t0 = time.time()
    segments, info = model.transcribe(
        audio_path,
        beam_size=5,
        language="en",
        vad_filter=False,   # Silero VAD already ran upstream
        condition_on_previous_text=False,  # reduces hallucination risk
    )
    text = " ".join(s.text.strip() for s in segments).strip()
    log.info(f"[LOCAL] Done in {time.time()-t0:.1f}s "
             f"(lang={info.language} {info.language_probability:.2f}): "
             f"{text[:80]!r}...")
    return text


def transcribe(audio_path: str) -> str:
    """Transcribe a WAV. Tries cloud Whisper first (with tenacity retry);
    on transient failure, falls back to faster-whisper running locally on
    CPU so Billy never loses a recording to a network blip or OpenAI-side
    hiccup. Raises RetryError only if BOTH cloud and local failed (or if
    local fallback is unavailable).

    Returns "" if the result is a known hallucination phrase (Layer 3)."""
    used_local = False
    try:
        text = _call_whisper_api(audio_path)
    except RetryError as cloud_err:
        log.warning(f"[WHISPER] Cloud exhausted after 3 attempts — "
                    f"falling back to local for {audio_path}")
        try:
            text = _transcribe_locally(audio_path)
            used_local = True
            log.info("[FALLBACK] Local transcription succeeded")
        except Exception as local_err:
            log.error(f"[FALLBACK] Local also failed: {local_err}")
            raise cloud_err  # propagate original RetryError so dead-letter fires
    except openai.AuthenticationError as e:
        log.error(f"[WHISPER] Authentication failed (check API key): {e}")
        raise
    except openai.BadRequestError as e:
        log.error(f"[WHISPER] Bad request (check audio file): {e}")
        return ""

    # Layer 3: catch any hallucination that slipped past VAD. Applied to
    # both cloud and local output since Whisper-family models hallucinate
    # identically on silence regardless of which backend runs them.
    if is_hallucinated(text):
        log.warning(f"[WHISPER] Output matches known hallucination phrase: "
                    f"{text!r} — discarding")
        return ""

    if used_local:
        # Mark the text so the caller can log/surface that local was used.
        # Using a function attribute instead of changing the return type
        # to avoid rippling the change through every caller.
        transcribe.last_used_local = True
    else:
        transcribe.last_used_local = False
    return text

transcribe.last_used_local = False


def cleanup_old_audio():
    cutoff = time.time() - (AUDIO_RETENTION_DAYS * 86400)
    removed = 0
    try:
        for name in os.listdir(AUDIO_DIR):
            if not name.lower().endswith(".wav"):
                continue
            path = os.path.join(AUDIO_DIR, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    removed += 1
            except OSError:
                pass
    except OSError:
        return
    if removed:
        log.info(f"[CLEANUP] Removed {removed} audio file(s) older than {AUDIO_RETENTION_DAYS}d")


def paste_text(text):
    pyperclip.copy(text)
    time.sleep(0.1)
    ctrl = pynput_kb.Controller()
    ctrl.press(pynput_kb.Key.ctrl)
    ctrl.press('v')
    ctrl.release('v')
    ctrl.release(pynput_kb.Key.ctrl)


# ============================================================
# SINGLE-INSTANCE LOCK
# ============================================================

def acquire_single_instance_lock():
    if msvcrt is None:
        return True
    try:
        lock_fd = open(LOCK_PATH, "a+")
        msvcrt.locking(lock_fd.fileno(), msvcrt.LK_NBLCK, 1)
        lock_fd.seek(0)
        lock_fd.truncate()
        lock_fd.write(str(os.getpid()))
        lock_fd.flush()
        return lock_fd
    except OSError:
        return None


# ============================================================
# PYQT6 OVERLAY WIDGET
# ============================================================

# States
IDLE = "idle"
RECORDING = "recording"
COMMAND_RECORDING = "command_recording"  # recording a voice command on highlighted text
PROCESSING = "processing"
DONE = "done"

# Theme (Warm Ember)
BG_COLOR = QColor(13, 10, 8, 220)
BORDER_IDLE = QColor(255, 149, 0, 50)
BORDER_ACTIVE = QColor(255, 149, 0, 100)
ACCENT = QColor(255, 149, 0)
ACCENT_DIM = QColor(255, 149, 0, 90)
DOT_DIM = QColor(136, 136, 136, 180)
TEXT_COLOR = QColor(230, 225, 220, 160)
GLOW_COLOR = QColor(255, 149, 0, 25)
REC_DOT = QColor(255, 68, 0)


# ============================================================
# RELIABLE CLIPBOARD CAPTURE (via SendInput)
# ============================================================

VK_CONTROL = 0x11
VK_SHIFT = 0x10
VK_LSHIFT = 0xA0
VK_RSHIFT = 0xA1
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_C = 0x43
VK_V = 0x56
KEYEVENTF_KEYUP = 0x0002
INPUT_KEYBOARD = 1

_PUL = ctypes.POINTER(ctypes.c_ulong)


class _KeyBdInput(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", _PUL),
    ]


class _Input_I(ctypes.Union):
    _fields_ = [("ki", _KeyBdInput)]


class _Input(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("ii", _Input_I)]


def _make_key(vk, flags=0):
    extra = ctypes.c_ulong(0)
    ii = _Input_I()
    ii.ki = _KeyBdInput(vk, 0, flags, 0, ctypes.pointer(extra))
    return _Input(INPUT_KEYBOARD, ii)


def _send_ctrl_c():
    """Atomically release any held modifiers, then send Ctrl+C.
    Eliminates the need to poll for modifier release before injection —
    the synthetic key-ups update the OS input state in the same batch."""
    user32_local = ctypes.windll.user32
    inputs = (_Input * 9)(
        _make_key(VK_LWIN,    KEYEVENTF_KEYUP),
        _make_key(VK_RWIN,    KEYEVENTF_KEYUP),
        _make_key(VK_LSHIFT,  KEYEVENTF_KEYUP),
        _make_key(VK_RSHIFT,  KEYEVENTF_KEYUP),
        _make_key(VK_CONTROL, KEYEVENTF_KEYUP),
        _make_key(VK_CONTROL, 0),
        _make_key(VK_C,       0),
        _make_key(VK_C,       KEYEVENTF_KEYUP),
        _make_key(VK_CONTROL, KEYEVENTF_KEYUP),
    )
    user32_local.SendInput(9, inputs, ctypes.sizeof(_Input))


def _wait_modifiers_released(timeout=2.0):
    """Wait until Ctrl, Shift, and Win keys are all physically up."""
    user32_local = ctypes.windll.user32
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ctrl = user32_local.GetAsyncKeyState(VK_CONTROL) & 0x8000
        shift = user32_local.GetAsyncKeyState(VK_SHIFT) & 0x8000
        lwin = user32_local.GetAsyncKeyState(VK_LWIN) & 0x8000
        rwin = user32_local.GetAsyncKeyState(VK_RWIN) & 0x8000
        if not (ctrl or shift or lwin or rwin):
            return True
        time.sleep(0.005)
    return False


def _restore_focus(target_hwnd):
    """Restore focus to target_hwnd using AttachThreadInput trick."""
    user32_local = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    if not target_hwnd:
        return False

    current_thread = kernel32.GetCurrentThreadId()
    fg_hwnd = user32_local.GetForegroundWindow()
    fg_thread = user32_local.GetWindowThreadProcessId(fg_hwnd, None)

    if fg_thread != current_thread:
        user32_local.AttachThreadInput(current_thread, fg_thread, True)
        user32_local.BringWindowToTop(target_hwnd)
        user32_local.SetForegroundWindow(target_hwnd)
        user32_local.AttachThreadInput(current_thread, fg_thread, False)
    else:
        user32_local.SetForegroundWindow(target_hwnd)

    time.sleep(0.05)
    return user32_local.GetForegroundWindow() == target_hwnd


def _warmup_uia():
    """Initialise UI Automation on a dedicated background thread so the first
    real command-mode call doesn't pay the ~400-700ms COM cold-start cost."""
    try:
        import uiautomation as auto
        auto.GetRootControl()
        log.info("[UIA] Warmup complete")
    except Exception as e:
        log.warning(f"[UIA] Warmup failed: {e}")


def _get_selected_text_uia():
    """Read currently-selected text via UI Automation. No clipboard, no SendInput.
    Works in Notepad, VS Code, Word, most Win32/WPF/WinForms apps.
    Returns text or None if unsupported/no selection."""
    try:
        import uiautomation as auto
        focused = auto.GetFocusedControl()
        if focused is None:
            log.info("[UIA] No focused control")
            return None
        tp = focused.GetTextPattern()
        if tp is None:
            log.info(f"[UIA] {focused.ControlTypeName} has no TextPattern")
            return None
        selections = tp.GetSelection()
        if not selections:
            log.info("[UIA] No selections in TextPattern")
            return None
        text = selections[0].GetText(-1)
        if text:
            log.info(f"[UIA] Captured {len(text)} chars from {focused.ControlTypeName}")
        return text if text else None
    except Exception as e:
        log.warning(f"[UIA] Failed: {e}")
        return None


def capture_highlighted_text(target_hwnd=None, timeout=0.5):
    """Capture highlighted text. Tries UI Automation first (doesn't care about
    modifier state, doesn't touch clipboard). Falls back to SendInput Ctrl+C
    which atomically releases held modifiers in the same batch.

    With WS_EX_NOACTIVATE the target window already has focus at hotkey-fire
    time, so no focus-restore or settle sleep is needed."""
    # --- METHOD 1: UI Automation (no clipboard, no keystrokes, modifier-agnostic) ---
    t0 = time.monotonic()
    uia_text = _get_selected_text_uia()
    if uia_text:
        log.info(f"[CAPTURE] UIA hit in {(time.monotonic()-t0)*1000:.0f}ms")
        return uia_text

    # --- METHOD 2: Clipboard via SendInput (modifier-release is part of the batch) ---
    log.info("[CAPTURE] UIA returned empty, falling back to clipboard")
    user32_local = ctypes.windll.user32
    old_seq = user32_local.GetClipboardSequenceNumber()

    _send_ctrl_c()

    result = ""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        new_seq = user32_local.GetClipboardSequenceNumber()
        if new_seq != old_seq:
            try:
                result = pyperclip.paste()
                log.info(f"[CAPTURE] Clipboard captured {len(result)} chars in {(time.monotonic()-t0)*1000:.0f}ms")
                break
            except Exception as e:
                log.error(f"[CAPTURE] pyperclip failed: {e}")
                break
        time.sleep(0.005)

    if not result:
        log.warning("[CAPTURE] Clipboard timeout — both UIA and SendInput failed")
    return result


# ============================================================
# BULLETPROOF OVERLAY — THREE-LAYER DEFENSE
# ============================================================

user32 = ctypes.windll.user32
dwmapi = ctypes.windll.dwmapi

HWND_TOPMOST = ctypes.wintypes.HWND(-1)
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
EVENT_SYSTEM_FOREGROUND = 0x0003
WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002
DWMWA_TRANSITIONS_FORCEDISABLED = 3
DWMWA_EXCLUDED_FROM_PEEK = 12

WinEventProcType = ctypes.WINFUNCTYPE(
    None,
    ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD,
    ctypes.wintypes.HWND, ctypes.wintypes.LONG,
    ctypes.wintypes.LONG, ctypes.wintypes.DWORD,
    ctypes.wintypes.DWORD,
)


def _reassert_topmost(hwnd):
    """Reassert HWND_TOPMOST without activating, moving, or un-hiding.
    SWP_SHOWWINDOW is INTENTIONALLY OMITTED — it would force-unhide the
    window, fighting against user-initiated hide. WS_VISIBLE survives
    SetWindowPos without SWP_SHOWWINDOW."""
    user32.SetWindowPos(
        hwnd, HWND_TOPMOST, 0, 0, 0, 0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
    )


class TopMostGuard:
    """Layer 1: System-wide foreground-change hook that instantly
    reasserts HWND_TOPMOST whenever another window gains focus."""

    def __init__(self, hwnd):
        self._hwnd = hwnd
        self._hook = None
        self._proc_ref = WinEventProcType(self._on_foreground_change)

    def _on_foreground_change(self, hook, event, hwnd, id_object, id_child, thread, time_ms):
        _reassert_topmost(self._hwnd)

    def install(self):
        self._hook = user32.SetWinEventHook(
            EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND,
            0, self._proc_ref, 0, 0,
            WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS,
        )
        if self._hook:
            log.info("[GUARD] Foreground hook installed")
        else:
            log.warning("[GUARD] SetWinEventHook failed")

    def uninstall(self):
        if self._hook:
            user32.UnhookWinEvent(self._hook)
            self._hook = None


# ============================================================
# AI TEXT CLEANUP
# ============================================================

CLEANUP_PROMPT = """You are a voice dictation cleanup engine. Your ONLY job is to clean up \
speech-recognition output. You NEVER rephrase, summarise, add content, change the user's \
vocabulary, or alter meaning in any way.

## What you fix (and only this):
1. FILLER WORDS — Remove: um, uh, like (when filler), you know, sort of, sort of like, \
basically, I mean, right? (when filler), actually (when filler). Remove stutters and \
false starts: repeated words ("the the"), half-sentences abandoned before completion.
2. BACKTRACK CORRECTION — When the speaker self-corrects, keep ONLY the final intended \
version. "meet Tuesday, wait, Wednesday" → "meet on Wednesday"
3. PUNCTUATION — Add contextually correct punctuation. Do not over-comma.
4. LIST FORMATTING — If the speaker enumerates items, format as a bullet list with line breaks. \
Do NOT create lists if the user is writing flowing prose.
5. PARAGRAPH BREAKS — For dictations longer than ~5 sentences, insert a blank line at clear \
topic shifts.
6. TECHNICAL CONTENT — Preserve ALL technical terms exactly: variable names, API names, URLs.
7. MULTILINGUAL — Preserve Afrikaans words and phrases exactly as transcribed.

## Critical rules:
- Output ONLY the cleaned text. No preamble, no "Here is the cleaned version:".
- NEVER add words not present in the original (except punctuation).
- NEVER change the user's vocabulary.
- If input is 4 words or fewer, return it unchanged except punctuation.
- If the user says 'quote' or 'for example' before 'um'/'uh', preserve them."""


COMMAND_PROMPT = """You are a text editing assistant. The user has highlighted text in an application \
and spoken a voice command describing what they want done to that text.

## Rules:
- Output ONLY the modified text. No explanations, no preamble.
- Follow the user's voice command exactly.
- Preserve the original meaning unless the command explicitly asks to change it.
- If the command is "translate to [language]", translate the entire text.
- If the command is about formatting (bullet list, numbered list, headings), reformat accordingly.
- If the command is about tone (make professional, make casual, make formal), rewrite in that tone.
- If the command is "summarize" or "shorten", condense while keeping key points.
- If the command is unclear, make your best interpretation and apply it.
- Preserve technical terms, names, and specific details unless told to change them.

## Selected text:
{selected_text}

## Voice command:
{command}"""


def execute_command(selected_text, voice_command):
    """Send highlighted text + voice command to GPT-4o-mini for editing."""
    try:
        t0 = time.time()
        prompt = COMMAND_PROMPT.format(
            selected_text=selected_text,
            command=voice_command,
        )
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": prompt},
            ],
            temperature=0.3,
            max_tokens=4096,
        )
        result = response.choices[0].message.content.strip()
        elapsed = time.time() - t0
        log.info(f"[COMMAND] Executed in {elapsed:.1f}s: '{voice_command}' on {len(selected_text)} chars")
        return result
    except Exception as e:
        log.error(f"[COMMAND] Failed: {e} — returning original text")
        return selected_text


# Offline rule-based cleanup. Runs when GPT-4o-mini cleanup is unreachable
# (same cloud outage that triggered local Whisper fallback will typically
# also take GPT out). Conservative on purpose — no misheard-word fixing,
# just the mechanical cleanup that always helps:
#   1. Strip classic fillers ("um", "uh", "erm", "hmm")
#   2. Collapse immediate duplicate words ("the the" → "the")
#   3. Collapse runs of whitespace
#   4. Capitalise the first letter of each sentence
#   5. Ensure terminal punctuation on the last sentence
# Legitimate speech patterns like "sort of", "basically", "like" stay —
# removing them would mangle real sentences. This is a safety net, not
# a replacement for GPT cleanup.
_FILLER_PATTERN = re.compile(
    r'(?<!\w)(?:um+|uh+|erm+|mm+|hmm+|uhm+)(?!\w)[\s,]*',
    re.IGNORECASE,
)
_DUPLICATE_WORD_PATTERN = re.compile(
    r'\b(\w+)\s+\1\b',
    re.IGNORECASE,
)
_WHITESPACE_PATTERN = re.compile(r'\s+')


def _rule_based_cleanup(raw_text: str) -> str:
    """Offline fallback cleanup. Input is raw Whisper output, output is a
    more readable string. Never raises — returns raw_text.strip() on any
    surprise so this is always safe to call."""
    try:
        text = raw_text.strip()
        if not text:
            return text

        text = _FILLER_PATTERN.sub(' ', text)
        text = _DUPLICATE_WORD_PATTERN.sub(r'\1', text)
        text = _WHITESPACE_PATTERN.sub(' ', text).strip()
        # Standalone "i" → "I" (first-person pronoun is always capitalised).
        text = re.sub(r"\bi\b", "I", text)
        text = re.sub(r"\bi'(m|ll|ve|d|t)\b", lambda m: "I'" + m.group(1), text,
                      flags=re.IGNORECASE)

        # Capitalise the first letter of each sentence (conservative:
        # only letters following ". ", "! ", "? ", or start-of-string).
        def _cap(match: re.Match) -> str:
            prefix = match.group(1) or ''
            letter = match.group(2)
            return prefix + letter.upper()

        text = re.sub(r'(^|[.!?]\s+)([a-z])', _cap, text)

        # Ensure terminal punctuation on the final sentence so pasted
        # text doesn't dangle awkwardly.
        if text and text[-1] not in '.!?':
            text += '.'

        return text
    except Exception as e:
        log.warning(f"[CLEANUP-RULES] Failed: {e} — returning raw text")
        return raw_text.strip()


def cleanup_text(raw_text):
    """Clean raw Whisper text. Tries GPT-4o-mini first; if that fails
    (cloud outage / network), falls back to offline rule-based cleanup
    so the user gets something usable instead of raw transcription."""
    words = raw_text.strip().split()
    if len(words) < 5:
        return raw_text.strip()

    try:
        t0 = time.time()
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": CLEANUP_PROMPT},
                {"role": "user", "content": raw_text},
            ],
            temperature=0,
            max_tokens=2048,
        )
        cleaned = response.choices[0].message.content.strip()
        elapsed = time.time() - t0
        log.info(f"[CLEANUP] {len(words)} words cleaned by GPT-4o-mini in {elapsed:.1f}s")
        return cleaned
    except Exception as e:
        log.warning(f"[CLEANUP] GPT unavailable ({e}) — applying offline "
                    f"rule-based cleanup to {len(words)} words")
        return _rule_based_cleanup(raw_text)


class KairaWidget(QWidget):
    """Floating pill overlay -- transparent, borderless, always-on-top, no taskbar."""

    state_changed = pyqtSignal(str)
    # v1.2 signals for user feedback from background transcription threads.
    # no_speech_detected: VAD gate rejected the recording → brief tray toast.
    # transcription_failed: all Whisper retries exhausted → outage toast with
    # retry button; wav_path is the file to retry when the user clicks.
    # journal_changed: dead-letter entry count may have changed — tray
    # refreshes the "Failed recordings (N)" menu item.
    no_speech_detected    = pyqtSignal(str)   # reason code for diagnostics
    transcription_failed  = pyqtSignal(str)   # wav_path waiting for retry
    journal_changed       = pyqtSignal()      # dead-letter count changed
    fell_back_to_local    = pyqtSignal()      # local-fallback toast trigger

    # Geometry
    PILL_H = 28
    CORNER_R = 14
    IDLE_W = 28       # perfect circle
    HOVER_W = 90
    REC_W = 180
    PROC_W = 140
    CMD_REC_W = 160
    DONE_W = 28

    def __init__(self):
        super().__init__()

        # Window flags: frameless + always-on-top + Tool (no taskbar)
        # NoDropShadowWindowHint prevents Qt from requesting DWM shadow
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.NoDropShadowWindowHint
        )
        # Per-pixel alpha via DWM (NOT color-key -- works on Intel UHD)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)

        self._state = IDLE
        self.recorder = Recorder()
        self._hovered = False
        self._drag_pos = None
        self._anim_t = 0.0
        self._spinner_angle = 0.0
        self._bar_levels = [0.0] * 20
        self._bar_targets = [0.0] * 20
        self._selected_text = ""  # text captured for command mode

        # Animated width
        self._target_w = float(self.IDLE_W)
        self._current_w = float(self.IDLE_W)

        # Position: bottom-center of primary at startup, repositioned to the
        # cursor's screen on each hotkey fire (follow-the-cursor model).
        self._widget_area_w = 300  # max possible pill width + glow margin
        screen = QApplication.primaryScreen().availableGeometry()
        self._home_x = screen.x() + (screen.width() - self._widget_area_w) // 2
        self._home_y = screen.y() + screen.height() - self.PILL_H - 25
        self.setGeometry(self._home_x, self._home_y, self._widget_area_w, self.PILL_H + 12)

        # Animation tick (~60fps)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(16)

        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self.state_changed.connect(self._set_state)

        # Defense layers (initialized in showEvent once HWND exists)
        self._topmost_guard = None
        self._topmost_timer = None
        self._watchdog_timer = None

    def showEvent(self, event):
        """Nuke DWM decorations + install three-layer topmost defense."""
        super().showEvent(event)
        hwnd_int = int(self.winId())
        hwnd = ctypes.wintypes.HWND(hwnd_int)
        log.info(f"[UI] showEvent fired, winId={hwnd_int}")

        try:
            # Attribute IDs 33 (DWMWA_WINDOW_CORNER_PREFERENCE) and
            # 34 (DWMWA_BORDER_COLOR) require Windows 11 build 22000+.
            # ID 38 (DWMWA_SYSTEMBACKDROP_TYPE) requires build 22621+.
            # On older builds they return E_INVALIDARG — harmless with try/except,
            # but guarding avoids noise in the debug output on Windows 10.
            win_build = sys.getwindowsversion().build
            is_win11 = win_build >= 22000
            is_win11_22h2 = win_build >= 22621

            # --- DWM decoration removal ---
            val = ctypes.c_int(1)
            dwmapi.DwmSetWindowAttribute(hwnd, 2, ctypes.byref(val), 4)   # NCRENDERING disabled (Vista+)
            if is_win11:
                val = ctypes.c_int(1)
                dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(val), 4)  # No rounded corners
                val = ctypes.c_uint(0xFFFFFFFE)
                dwmapi.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(val), 4)  # No border
            if is_win11_22h2:
                val = ctypes.c_int(0)
                dwmapi.DwmSetWindowAttribute(hwnd, 38, ctypes.byref(val), 4)  # No backdrop

            GCL_STYLE = -26
            CS_DROPSHADOW = 0x00020000
            style = user32.GetClassLongPtrW(hwnd, GCL_STYLE)
            if style & CS_DROPSHADOW:
                user32.SetClassLongPtrW(hwnd, GCL_STYLE, style & ~CS_DROPSHADOW)

            # --- DWM hardening ---
            TRUE = ctypes.c_int(1)
            dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_TRANSITIONS_FORCEDISABLED, ctypes.byref(TRUE), 4)
            dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_EXCLUDED_FROM_PEEK, ctypes.byref(TRUE), 4)

            # --- WS_EX_NOACTIVATE (never steal focus) ---
            ex_style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex_style | WS_EX_NOACTIVATE)

            log.info(f"[UI] DWM decorations removed + hardening applied (build {win_build}, win11={is_win11})")
        except Exception as e:
            log.warning(f"[UI] DWM fix failed: {e}")

        # Create defense objects lazily on first show — they're started/stopped
        # separately via start_defense()/stop_defense() so Pause can release
        # them fully while Resume re-installs.
        self._ensure_defense_objects(hwnd_int)
        self.start_defense()

    def _ensure_defense_objects(self, hwnd_int):
        """Create (but do not necessarily start) the three defense layers."""
        if not self._topmost_guard:
            self._topmost_guard = TopMostGuard(hwnd_int)

        if not self._topmost_timer:
            def _periodic_topmost():
                if getattr(self, "_user_hidden", False):
                    return
                _reassert_topmost(hwnd_int)
            self._topmost_timer = QTimer(self)
            self._topmost_timer.setInterval(7000)
            self._topmost_timer.timeout.connect(_periodic_topmost)

        if not self._watchdog_timer:
            def _watchdog_check():
                if getattr(self, "_user_hidden", False):
                    return
                if not self.isVisible():
                    self.show()
                    _reassert_topmost(hwnd_int)
                    log.warning("[WATCHDOG] Widget was hidden — recovered")
                    return
                geo = self.geometry()
                screens = QGuiApplication.screens()
                on_any_screen = any(s.geometry().intersects(geo) for s in screens)
                if not on_any_screen:
                    self.move(self._home_x, self._home_y)
                    log.warning("[WATCHDOG] Widget off-screen — repositioned")
                self.show()
                _reassert_topmost(hwnd_int)

            self._watchdog_timer = QTimer(self)
            self._watchdog_timer.setInterval(10000)
            self._watchdog_timer.timeout.connect(_watchdog_check)

    def start_defense(self):
        """Install foreground hook + start both defense timers. Idempotent."""
        if self._topmost_guard and self._topmost_guard._hook is None:
            self._topmost_guard.install()
        if self._topmost_timer and not self._topmost_timer.isActive():
            self._topmost_timer.start()
            log.info("[GUARD] Periodic topmost timer started (7s)")
        if self._watchdog_timer and not self._watchdog_timer.isActive():
            self._watchdog_timer.start()
            log.info("[WATCHDOG] Visibility watchdog started (10s)")

    def stop_defense(self):
        """Uninstall foreground hook + stop both timers. Idempotent."""
        if self._topmost_guard:
            self._topmost_guard.uninstall()
        if self._topmost_timer and self._topmost_timer.isActive():
            self._topmost_timer.stop()
            log.info("[GUARD] Periodic topmost timer stopped")
        if self._watchdog_timer and self._watchdog_timer.isActive():
            self._watchdog_timer.stop()
            log.info("[WATCHDOG] Visibility watchdog stopped")

    # --- State machine ---

    def _set_state(self, state):
        prev = self._state
        self._state = state
        log.info(f"[STATE] {prev} -> {state}")
        targets = {
            IDLE: self.IDLE_W,
            RECORDING: self.REC_W,
            COMMAND_RECORDING: self.CMD_REC_W,
            PROCESSING: self.PROC_W,
            DONE: self.DONE_W,
        }
        self._target_w = float(targets.get(state, self.IDLE_W))

    def reposition_to_cursor_screen(self):
        """Move the pill to the bottom-center of whichever monitor the cursor
        is currently on. Safe to call from the Qt main thread. No-op if the
        cursor's screen cannot be determined (falls back to primary)."""
        if self._state != IDLE and self._state != "hover":
            return  # don't reposition mid-recording — avoids visual jumps
        screen = QGuiApplication.screenAt(QCursor.pos())
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        geom = screen.availableGeometry()
        new_x = geom.x() + (geom.width() - self._widget_area_w) // 2
        new_y = geom.y() + geom.height() - self.PILL_H - 25
        if (new_x, new_y) == (self._home_x, self._home_y):
            return
        self._home_x = new_x
        self._home_y = new_y
        self.move(new_x, new_y)
        log.info(f"[UI] Repositioned to screen at ({new_x},{new_y})")

    def toggle_dictation(self):
        """Thread-safe toggle via signal."""
        log.info(f"[TOGGLE] Triggered (current state: {self._state})")
        if self._state in (IDLE, "hover"):
            self.state_changed.emit(RECORDING)
            self._start_recording()
        elif self._state == RECORDING:
            self._stop_recording()
        elif self._state == COMMAND_RECORDING:
            self._stop_command_recording()
        else:
            log.info(f"[TOGGLE] Ignored — busy ({self._state})")

    def start_command_mode(self, target_hwnd=None):
        """Grab highlighted text via reliable SendInput Ctrl+C, then record a voice command.
        target_hwnd: the HWND that was in foreground when the hotkey fired."""
        log.info(f"[COMMAND] Starting command mode... target_hwnd={target_hwnd}")

        # Log which app we're targeting
        if target_hwnd:
            try:
                title = ctypes.create_unicode_buffer(256)
                user32.GetWindowTextW(target_hwnd, title, 256)
                log.info(f"[COMMAND] Target window: '{title.value}'")
            except Exception:
                pass

        # Capture highlighted text — waits for modifiers, restores focus, uses SendInput
        self._selected_text = capture_highlighted_text(target_hwnd=target_hwnd, timeout=1.5)

        if not self._selected_text.strip():
            log.warning("[COMMAND] No text captured — aborting. Highlight text first, then press Ctrl+Shift+Win.")
            self._selected_text = ""
            return  # stay in idle state

        # Check selection size limits
        char_count = len(self._selected_text)
        HARD_LIMIT = 20000   # ~3,000 words
        SOFT_WARN = 5000     # ~750 words

        if char_count > HARD_LIMIT:
            log.warning(f"[COMMAND] Selection too large ({char_count} chars > {HARD_LIMIT}) — aborting")
            self._selected_text = ""
            return

        if char_count > SOFT_WARN:
            log.warning(f"[COMMAND] Large selection ({char_count} chars) — processing may be slow")

        log.info(f"[COMMAND] Captured {char_count} chars of selected text")
        # Step 2: Start recording the voice command
        self.state_changed.emit(COMMAND_RECORDING)
        self._bar_levels = [0.0] * 20
        self._bar_targets = [0.0] * 20
        self.recorder.start()

    def _stop_command_recording(self):
        """Stop recording and process the voice command on the selected text."""
        self.state_changed.emit(PROCESSING)
        log.info("[COMMAND] Processing voice command...")
        audio_path, duration = self.recorder.stop()
        if audio_path:
            log.info(f"[COMMAND] Recorded {duration:.1f}s command")
            threading.Thread(
                target=self._execute_voice_command,
                args=(audio_path, duration),
                daemon=True,
            ).start()
        else:
            log.info("[COMMAND] No audio captured")
            self.state_changed.emit(IDLE)

    def _execute_voice_command(self, audio_path, duration):
        """Transcribe voice command, apply to selected text, paste result.
        v1.2: Same VAD + retry + journal pipeline as dictation."""
        try:
            # Step 1: VAD gate — if no command was spoken, bail out.
            vad_cfg = load_settings().get("vad", VAD_DEFAULTS)
            should_transcribe, reason, diag = vad_gate(audio_path, vad_cfg)
            if not should_transcribe:
                self.no_speech_detected.emit(reason)
                log.info("[COMMAND] Aborted — no voice command was spoken.")
                return

            # Step 2: Journal the recording before the API call
            if audio_path not in _journal_load():
                journal_add(audio_path, duration, mode="command_mode")

            # Step 3: Transcribe the command
            journal_mark_attempt_started(audio_path)
            start = time.time()
            try:
                voice_command = transcribe(audio_path)
            except RetryError as e:
                log.error(f"[COMMAND] Outage — all retries failed for {audio_path}")
                root = e.last_attempt.exception() if e.last_attempt else None
                state = journal_mark_failure(
                    audio_path,
                    error_code=type(root).__name__ if root else "RetryError",
                    error_message=str(root) if root else str(e),
                    failure_class="transient_transport",
                    retryable=True,
                )
                self.transcription_failed.emit(audio_path)
                if state == JOURNAL_STATE_DEAD_LETTER:
                    self.journal_changed.emit()
                return
            except openai.AuthenticationError as e:
                log.error("[COMMAND] Authentication failed — API key is invalid")
                journal_mark_failure(
                    audio_path,
                    error_code="AuthenticationError",
                    error_message=str(e),
                    failure_class="permanent_request",
                    retryable=False,
                )
                self.journal_changed.emit()
                return
            whisper_time = time.time() - start
            if getattr(transcribe, "last_used_local", False):
                log.info("[FALLBACK] Command used local faster-whisper (cloud failed)")
                self.fell_back_to_local.emit()

            if not voice_command:
                log.info("[COMMAND] Empty command (silence sentinel or hallucination)")
                journal_remove(audio_path)
                return

            log.info(f"[COMMAND] Voice command ({whisper_time:.1f}s): {voice_command}")
            log.info(f"[COMMAND] Applying to: {self._selected_text[:80]}...")

            # Step 4: Execute command on the selected text
            result = execute_command(self._selected_text, voice_command)

            # Step 5: Paste the result (replaces the selection)
            paste_text(result)
            journal_remove(audio_path)
            total_time = time.time() - start
            log.info(f"[COMMAND] Done ({total_time:.1f}s total). Result: {result[:100]}...")

        except Exception as e:
            log.error(f"[COMMAND] Failed: {e}", exc_info=True)
        finally:
            self._selected_text = ""
            self.state_changed.emit(DONE)
            time.sleep(0.4)
            self.state_changed.emit(IDLE)

    def _start_recording(self):
        self._bar_levels = [0.0] * 20
        self._bar_targets = [0.0] * 20
        log.info("[REC] Listening... speak now")
        self.recorder.start()

    def _stop_recording(self):
        self.state_changed.emit(PROCESSING)
        log.info("[STOP] Processing...")
        audio_path, duration = self.recorder.stop()
        if audio_path:
            log.info(f"[SAVED] {audio_path} ({duration:.1f}s)")
            log.info(f"[PROCESSING] Sending {duration:.1f}s to Whisper...")
            threading.Thread(
                target=self._transcribe_and_paste,
                args=(audio_path, duration),
                daemon=True,
            ).start()
        else:
            log.info("[SKIP] No audio captured")
            self.state_changed.emit(IDLE)

    def cancel_recording(self):
        """Stop recording WITHOUT submitting to Whisper. Used by
        microtransition suppression when a hold-to-talk press+release
        happened too fast to be intentional (under 150ms). Audio is
        captured to disk as usual but we skip the transcription call."""
        if self._state not in (RECORDING, COMMAND_RECORDING):
            return
        log.info("[CANCEL] Discarding recording (microtransition suppression)")
        try:
            audio_path, duration = self.recorder.stop()
            if audio_path:
                log.info(f"[CANCEL] Discarded {duration:.1f}s at {audio_path}")
        except Exception as e:
            log.warning(f"[CANCEL] Recorder stop error: {e}")
        self.state_changed.emit(IDLE)

    def _transcribe_and_paste(self, audio_path, duration):
        """v1.2 pipeline:
          1. VAD gate — if no speech detected, skip Whisper entirely and
             show brief user feedback.
          2. Journal-add so a crash during transcription doesn't lose audio.
          3. Whisper call with exponential-backoff retry (3 attempts, ~12s).
          4. On total failure — keep journal entry, emit transcription_failed
             so tray shows outage toast; user can retry later.
          5. On success — clean up via GPT, paste, remove journal entry."""
        try:
            # Step 1: VAD gate (3-layer defence — disabled bypass, duration
            # bypass, RMS pre-gate, Silero VAD, hallucination phrase filter
            # at Whisper output).
            vad_cfg = load_settings().get("vad", VAD_DEFAULTS)
            should_transcribe, reason, diag = vad_gate(audio_path, vad_cfg)
            if not should_transcribe:
                self.no_speech_detected.emit(reason)
                # Don't journal-add — we're deliberately not submitting this.
                return

            # Step 2: Journal the recording before any API call. If we crash
            # between here and journal_remove below, next launch will offer
            # to retry automatically. Only add if not already present (a
            # manual-retry of a dead-letter entry will hit the pipeline with
            # the entry already in the journal).
            mode = "command_mode" if self._state == COMMAND_RECORDING else "dictation"
            if audio_path not in _journal_load():
                journal_add(audio_path, duration, mode)

            # Step 3: Whisper with retry-on-failure. mark_attempt_started()
            # increments attempt_count for exactly one user-visible attempt
            # (tenacity runs up to 3 sub-attempts internally — that is one
            # journal attempt). On RetryError the entry may promote to
            # dead_letter; once there it is not re-prompted on next launch.
            journal_mark_attempt_started(audio_path)
            start = time.time()
            try:
                raw_text = transcribe(audio_path)
            except RetryError as e:
                # Both cloud AND local failed (transcribe tries local
                # fallback internally when cloud's RetryError fires).
                log.error(f"[WHISPER] Cloud + local both failed for {audio_path}")
                root = e.last_attempt.exception() if e.last_attempt else None
                state = journal_mark_failure(
                    audio_path,
                    error_code=type(root).__name__ if root else "RetryError",
                    error_message=str(root) if root else str(e),
                    failure_class="transient_transport",
                    retryable=True,
                )
                self.transcription_failed.emit(audio_path)
                if state == JOURNAL_STATE_DEAD_LETTER:
                    self.journal_changed.emit()
                return
            except openai.AuthenticationError as e:
                log.error("[WHISPER] Authentication failed — API key is invalid")
                # Not retryable → straight to dead_letter so the user can
                # fix the key and retry manually from the Failed list.
                journal_mark_failure(
                    audio_path,
                    error_code="AuthenticationError",
                    error_message=str(e),
                    failure_class="permanent_request",
                    retryable=False,
                )
                self.journal_changed.emit()
                return
            whisper_time = time.time() - start
            if getattr(transcribe, "last_used_local", False):
                log.info("[FALLBACK] Used local faster-whisper (cloud failed)")
                self.fell_back_to_local.emit()

            if not raw_text:
                log.info("[SKIP] Empty transcription (silence sentinel or "
                         "hallucination phrase — see earlier log)")
                journal_remove(audio_path)
                return

            log.info(f"[WHISPER] ({duration:.1f}s audio, "
                     f"{whisper_time:.1f}s API) {raw_text}")

            # Step 4: AI cleanup via GPT-4o-mini
            cleaned_text = cleanup_text(raw_text)
            if cleaned_text != raw_text:
                log.info(f"[CLEANED] {cleaned_text}")

            # Step 5: Paste
            paste_text(cleaned_text)
            journal_remove(audio_path)
            total_time = time.time() - start
            log.info(f"[PASTED] ({total_time:.1f}s total) {cleaned_text}")

        except Exception as e:
            log.error(f"[ERROR] Unhandled exception in transcription pipeline: {e}",
                      exc_info=True)
            log.error(f"[RECOVERABLE] Audio saved at: {audio_path}. "
                     f"Journal entry kept — retry on next launch.")
        finally:
            self.state_changed.emit(DONE)
            time.sleep(0.4)
            self.state_changed.emit(IDLE)

    # --- Animation loop ---

    def _tick(self):
        self._anim_t += 0.016

        # Smooth width interpolation
        diff = self._target_w - self._current_w
        self._current_w += diff * 0.18

        # Update bar levels from recorder
        if self._state in (RECORDING, COMMAND_RECORDING) and self.recorder.is_recording:
            level = self.recorder.current_level
            for i in range(20):
                variation = 0.6 + (hash((i, int(self._anim_t * 60))) % 100) / 150.0
                self._bar_targets[i] = min(1.0, level * variation * 3.5)

        # Smooth bars (fast attack, slow decay)
        for i in range(20):
            target = self._bar_targets[i]
            current = self._bar_levels[i]
            k = 0.35 if target > current else 0.12
            self._bar_levels[i] += (target - current) * k

        # Spinner
        if self._state == PROCESSING:
            self._spinner_angle = (self._spinner_angle + 5) % 360

        self.update()

    # --- Painting ---

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = self._current_w
        h = self.PILL_H
        r = self.CORNER_R
        # Center the pill horizontally in the widget
        x = (self.width() - w) / 2
        y = 4.0  # small top margin for glow

        pill = QPainterPath()
        pill.addRoundedRect(QRectF(x, y, w, h), r, r)

        # Outer glow (recording/processing only)
        if self._state in (RECORDING, COMMAND_RECORDING, PROCESSING):
            p.setPen(Qt.PenStyle.NoPen)
            for i in range(3):
                glow = QColor(255, 149, 0, 10 - i * 3)
                p.setBrush(QBrush(glow))
                expand = (3 - i) * 2
                glow_path = QPainterPath()
                glow_path.addRoundedRect(
                    QRectF(x - expand, y - expand, w + expand * 2, h + expand * 2),
                    r + expand, r + expand
                )
                p.drawPath(glow_path)

        # Background
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(BG_COLOR))
        p.drawPath(pill)

        # Border
        border_color = BORDER_ACTIVE if self._state != IDLE else BORDER_IDLE
        if self._hovered and self._state == IDLE:
            border_color = QColor(255, 149, 0, 80)
        p.setPen(QPen(border_color, 1.2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(pill)

        # Content
        cx = x + w / 2
        cy = y + h / 2

        if self._state == IDLE:
            if self._hovered and self._current_w > 40:
                # Dot + text centered together. Text reflects the
                # actually-configured dictation hotkey so custom combos
                # surface correctly; short form (no mode suffix) keeps
                # the pill from widening.
                font = QFont("Segoe UI", 8)
                font.setWeight(QFont.Weight.Medium)
                metrics = QFontMetrics(font)
                try:
                    cfg = load_settings()["hotkeys"]["dictation"]
                    combo = format_hotkey(cfg)  # e.g. "Ctrl + Win"
                    text = combo.replace(" ", "")  # tight for the small pill
                except Exception:
                    text = "Ctrl+Win"
                tw = metrics.horizontalAdvance(text)
                dot_r = 3.5
                gap = 6
                total = dot_r * 2 + gap + tw
                start_x = cx - total / 2
                self._draw_idle_dot(p, start_x + dot_r, cy)
                self._draw_hint(p, start_x + dot_r * 2 + gap, y, h, text)
            else:
                self._draw_idle_dot(p, cx, cy)
        elif self._state == RECORDING:
            self._draw_rec_dot(p, x, cy)
            self._draw_waveform(p, x, w, y, h)
        elif self._state == COMMAND_RECORDING:
            # Command mode — purple-ish dot + waveform + "Command..." label
            pulse = 0.5 + 0.5 * math.sin(self._anim_t * 6)
            alpha = int(180 + 75 * pulse)
            color = QColor(149, 100, 255, alpha)  # purple accent for command mode
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(color))
            p.drawEllipse(QPointF(x + 12, cy), 3.5, 3.5)
            self._draw_waveform(p, x, w, y, h)
        elif self._state == PROCESSING:
            # Spinner + text centered together
            font = QFont("Segoe UI", 8)
            font.setWeight(QFont.Weight.Medium)
            metrics = QFontMetrics(font)
            text = "Transcribing..."
            tw = metrics.horizontalAdvance(text)
            spinner_d = 12
            gap = 8
            total = spinner_d + gap + tw
            start_x = cx - total / 2
            self._draw_spinner(p, start_x + 6, cy)
            self._draw_hint(p, start_x + spinner_d + gap, y, h, text)
        elif self._state == DONE:
            self._draw_done_dot(p, cx, cy)

        p.end()

    def _draw_idle_dot(self, p, cx, cy):
        color = ACCENT if self._hovered else DOT_DIM
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(color))
        p.drawEllipse(QPointF(cx, cy), 3.5, 3.5)

    def _draw_rec_dot(self, p, pill_x, cy):
        # Pulsing red dot
        pulse = 0.5 + 0.5 * math.sin(self._anim_t * 6)
        alpha = int(180 + 75 * pulse)
        color = QColor(255, 68, 0, alpha)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(color))
        p.drawEllipse(QPointF(pill_x + 14, cy), 3.5, 3.5)

    def _draw_waveform(self, p, pill_x, pill_w, pill_y, pill_h):
        bar_count = len(self._bar_levels)
        bar_area_start = pill_x + 28
        bar_area_end = pill_x + pill_w - 12
        bar_area = bar_area_end - bar_area_start
        if bar_area <= 0:
            return
        spacing = bar_area / bar_count

        for i, level in enumerate(self._bar_levels):
            bx = bar_area_start + i * spacing + spacing / 2
            bar_h = max(2, level * (pill_h - 10))
            bar_w = max(1.5, spacing * 0.45)
            by = pill_y + pill_h / 2 - bar_h / 2

            alpha = int(120 + 135 * min(1.0, level))
            color = QColor(255, 149, 0, alpha)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(color))
            p.drawRoundedRect(QRectF(bx - bar_w / 2, by, bar_w, bar_h), 1, 1)

    def _draw_spinner(self, p, cx, cy):
        sr = 6
        rect = QRectF(cx - sr, cy - sr, sr * 2, sr * 2)
        # Background arc
        p.setPen(QPen(ACCENT_DIM, 2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(rect)
        # Active arc
        pen = QPen(ACCENT, 2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawArc(rect, int(self._spinner_angle * 16), 270 * 16)

    def _draw_hint(self, p, left_x, pill_y, pill_h, text):
        """Left-aligned label, positioned right after the icon."""
        font = QFont("Segoe UI", 8)
        font.setWeight(QFont.Weight.Medium)
        p.setFont(font)
        p.setPen(QPen(TEXT_COLOR))
        metrics = QFontMetrics(font)
        ty = pill_y + pill_h / 2 + metrics.ascent() / 2 - 2
        p.drawText(QPointF(left_x, ty), text)

    def _draw_done_dot(self, p, cx, cy):
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(ACCENT))
        scale = 1.0 + 0.3 * math.sin(self._anim_t * 10)
        p.drawEllipse(QPointF(cx, cy), 4 * scale, 4 * scale)

    # --- Mouse events ---

    def enterEvent(self, event):
        self._hovered = True
        if self._state == IDLE:
            self._target_w = float(self.HOVER_W)

    def leaveEvent(self, event):
        self._hovered = False
        if self._state == IDLE:
            self._target_w = float(self.IDLE_W)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.toggle_dictation()

    def mouseMoveEvent(self, event):
        pass  # no drag for now


# ============================================================
# HOTKEY HANDLER
# ============================================================

# ============================================================
# GLOBAL HOTKEY FILTER — Raw Input API (config-driven)
# ============================================================
# Modifier-only hotkeys (dictation, command_mode) use Raw Input — they fire on
# release of the first modifier from the held set, matching peak-held against
# each configured action's required modifiers. Standard hotkeys (quit) use
# RegisterHotKey. Both paths are driven by settings.json via the `hotkeys`
# dict passed to GlobalHotkeyFilter.
#
# Raw Input runs through the Qt event loop via WM_INPUT and is not subject to
# LowLevelHooksTimeout. Injected keys (SendInput) don't appear in WM_INPUT, so
# Kaira's own Ctrl+C / Ctrl+V simulation cannot re-trigger hotkeys.

WM_INPUT         = 0x00FF
WM_HOTKEY        = 0x0312
RIM_TYPEKEYBOARD = 0x01
RIDEV_INPUTSINK  = 0x00000100
RIDEV_REMOVE     = 0x00000001
RID_INPUT        = 0x10000003
WM_KEYDOWN       = 0x0100
WM_SYSKEYDOWN    = 0x0104

# Raw Input reports generic VK for most keyboards; also handle L/R specifics.
_CTRL_VKS  = {0x11, 0xA2, 0xA3}   # VK_CONTROL, VK_LCONTROL, VK_RCONTROL
_SHIFT_VKS = {0x10, 0xA0, 0xA1}   # VK_SHIFT, VK_LSHIFT, VK_RSHIFT
_ALT_VKS   = {0x12, 0xA4, 0xA5}   # VK_MENU, VK_LMENU, VK_RMENU
_WIN_VKS   = {0x5B, 0x5C}         # VK_LWIN, VK_RWIN
_ALL_MOD_VKS = _CTRL_VKS | _SHIFT_VKS | _ALT_VKS | _WIN_VKS

# RegisterHotKey modifier flags.
_MOD_FLAGS = {
    "ctrl":  0x0002,   # MOD_CONTROL
    "shift": 0x0004,   # MOD_SHIFT
    "alt":   0x0001,   # MOD_ALT
    "win":   0x0008,   # MOD_WIN
}


def _vk_to_mod_name(vk: int):
    if vk in _CTRL_VKS:  return "ctrl"
    if vk in _SHIFT_VKS: return "shift"
    if vk in _ALT_VKS:   return "alt"
    if vk in _WIN_VKS:   return "win"
    return None


class _RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [
        ("usUsagePage", ctypes.c_ushort),
        ("usUsage",     ctypes.c_ushort),
        ("dwFlags",     ctypes.c_uint),
        ("hwndTarget",  ctypes.wintypes.HWND),
    ]


class _RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [
        ("dwType",  ctypes.c_uint),
        ("dwSize",  ctypes.c_uint),
        ("hDevice", ctypes.c_void_p),
        ("wParam",  ctypes.wintypes.WPARAM),
    ]


class _RAWKEYBOARD(ctypes.Structure):
    _fields_ = [
        ("MakeCode",         ctypes.c_ushort),
        ("Flags",            ctypes.c_ushort),
        ("Reserved",         ctypes.c_ushort),
        ("VKey",             ctypes.c_ushort),
        ("Message",          ctypes.c_uint),
        ("ExtraInformation", ctypes.c_ulong),
    ]


class _RAWINPUT(ctypes.Structure):
    _fields_ = [
        ("header",   _RAWINPUTHEADER),
        ("keyboard", _RAWKEYBOARD),
    ]


class HotkeySignals(QObject):
    # Toggle / standard hotkey fired once per activation (on release for mod_only,
    # on keydown for standard). Existing v1.1 behaviour.
    hotkey_fired = pyqtSignal(str)
    # Hold-to-talk: pressed fires when full combo becomes held; released fires
    # when the combo is broken (any required modifier released). Each fires
    # exactly once per hold cycle.
    hotkey_pressed  = pyqtSignal(str)
    hotkey_released = pyqtSignal(str)
    # Capture mode (for Preferences dialog key-capture widgets)
    mod_held_changed    = pyqtSignal(frozenset)         # current held mod set
    mod_combo_captured  = pyqtSignal(frozenset)         # peak set at first release
    std_combo_captured  = pyqtSignal(frozenset, str)    # modifiers, key name


class GlobalHotkeyFilter(QAbstractNativeEventFilter):
    """Config-driven native event filter. Modifier-only actions fire on first
    release from the held set (peak-match). Standard actions use RegisterHotKey.
    During capture mode (used by Preferences dialogs), runtime firing is
    suspended and captured combos are emitted via dedicated signals."""

    # Microtransition threshold: if a hold-to-talk action fires pressed + released
    # within this window, the release event is suppressed (not forwarded to the
    # widget), preventing accidental empty-audio submissions to Whisper.
    # 150ms chosen because intentional key-taps are ≥200ms; below 150ms is
    # almost certainly key-fumbling or a bypass of Rule A validation.
    MICROTRANSITION_MS = 150

    def __init__(self, hwnd: int, hotkeys: dict):
        super().__init__()
        self.signals = HotkeySignals()
        self._hwnd = hwnd
        self._hotkeys = hotkeys
        self._held_mods: set = set()
        self._std_hotkey_ids: dict = {}   # hotkey_id -> action_name
        self._capture_mode = None          # None | "mod_only" | "standard"
        self._htt_active: dict = {}        # action_name -> bool (currently held)
        self._htt_press_ts: dict = {}      # action_name -> float (press timestamp)
        self._register_raw_input()
        self._register_standard_hotkeys()

    def _register_raw_input(self):
        rid = _RAWINPUTDEVICE()
        rid.usUsagePage = 0x01       # HID_USAGE_PAGE_GENERIC
        rid.usUsage     = 0x06       # HID_USAGE_GENERIC_KEYBOARD
        rid.dwFlags     = RIDEV_INPUTSINK
        rid.hwndTarget  = self._hwnd
        ok = user32.RegisterRawInputDevices(
            ctypes.byref(rid), 1, ctypes.sizeof(rid)
        )
        if not ok:
            err = ctypes.get_last_error()
            log.error(f"[HOTKEY] RegisterRawInputDevices failed: {err}")
            raise RuntimeError(f"RegisterRawInputDevices failed: {err}")
        log.info("[HOTKEY] Raw Input registered (RIDEV_INPUTSINK)")

    def _register_standard_hotkeys(self):
        """Register each configured 'standard' action via RegisterHotKey."""
        next_id = 1
        for name, cfg in self._hotkeys.items():
            if cfg.get("type") != "standard":
                continue
            flags = 0
            for m in cfg.get("modifiers", []):
                flags |= _MOD_FLAGS.get(m, 0)
            key = cfg.get("key", "")
            vk = STANDARD_KEYS.get(key)
            if vk is None:
                log.warning(f"[HOTKEY] Unknown standard key '{key}' for {name}")
                continue
            ok = user32.RegisterHotKey(self._hwnd, next_id, flags, vk)
            if not ok:
                err = ctypes.get_last_error()
                log.warning(
                    f"[HOTKEY] RegisterHotKey '{name}' "
                    f"({format_hotkey(cfg)}) failed: {err}"
                )
            else:
                self._std_hotkey_ids[next_id] = name
                log.info(
                    f"[HOTKEY] '{name}' registered: {format_hotkey(cfg)} "
                    f"(id={next_id})"
                )
                next_id += 1

    def unregister(self):
        for hk_id in list(self._std_hotkey_ids):
            try:
                user32.UnregisterHotKey(self._hwnd, hk_id)
            except Exception:
                pass
        self._std_hotkey_ids.clear()
        try:
            rid = _RAWINPUTDEVICE()
            rid.usUsagePage = 0x01
            rid.usUsage     = 0x06
            rid.dwFlags     = RIDEV_REMOVE
            rid.hwndTarget  = None
            user32.RegisterRawInputDevices(
                ctypes.byref(rid), 1, ctypes.sizeof(rid)
            )
        except Exception:
            pass

    # --- Capture mode API (used by key-capture dialogs) ---

    def start_capture(self, kind: str):
        """kind ∈ {'mod_only', 'standard'}. Suspends runtime hotkey firing so
        the user can press their desired combo without triggering the old one."""
        assert kind in ("mod_only", "standard")
        self._capture_mode = kind
        log.info(f"[HOTKEY] Capture mode ON ({kind})")

    def stop_capture(self):
        self._capture_mode = None
        log.info("[HOTKEY] Capture mode OFF")

    # --- Event handling ---

    def nativeEventFilter(self, eventType, message):
        msg = ctypes.cast(
            int(message), ctypes.POINTER(ctypes.wintypes.MSG)
        ).contents

        if msg.message == WM_INPUT:
            self._handle_raw_input(msg.lParam)
            return False, 0

        if msg.message == WM_HOTKEY:
            # During capture mode, swallow registered hotkeys so the user
            # can re-bind the combo they're currently using.
            if self._capture_mode is not None:
                return True, 0
            name = self._std_hotkey_ids.get(msg.wParam)
            if name:
                log.info(f"[HOTKEY] '{name}' fired (standard)")
                self.signals.hotkey_fired.emit(name)
            return True, 0

        return False, 0

    def _handle_raw_input(self, hRawInput: int):
        dwSize = ctypes.c_uint(0)
        user32.GetRawInputData(
            hRawInput, RID_INPUT, None, ctypes.byref(dwSize),
            ctypes.sizeof(_RAWINPUTHEADER),
        )
        if dwSize.value == 0:
            return
        buf = (ctypes.c_byte * dwSize.value)()
        got = user32.GetRawInputData(
            hRawInput, RID_INPUT, buf, ctypes.byref(dwSize),
            ctypes.sizeof(_RAWINPUTHEADER),
        )
        if got != dwSize.value:
            return
        raw = ctypes.cast(buf, ctypes.POINTER(_RAWINPUT)).contents
        if raw.header.dwType != RIM_TYPEKEYBOARD:
            return

        vk = raw.keyboard.VKey
        if vk not in _ALL_MOD_VKS:
            return
        mod_name = _vk_to_mod_name(vk)
        if mod_name is None:
            return
        msg_type = raw.keyboard.Message
        is_down = msg_type in (WM_KEYDOWN, WM_SYSKEYDOWN)

        if is_down:
            if mod_name not in self._held_mods:
                self._held_mods.add(mod_name)
                if self._capture_mode in ("mod_only", "standard"):
                    self.signals.mod_held_changed.emit(frozenset(self._held_mods))
                else:
                    self._evaluate_htt_transitions()
            return

        # Key released — evaluate peak BEFORE removing
        peak = frozenset(self._held_mods)
        self._held_mods.discard(mod_name)

        if self._capture_mode == "mod_only":
            if len(peak) >= 2:
                log.info(f"[HOTKEY] Capture: mod-only combo = {sorted(peak)}")
                self.signals.mod_combo_captured.emit(peak)
            else:
                self.signals.mod_held_changed.emit(frozenset(self._held_mods))
            return

        if self._capture_mode == "standard":
            self.signals.mod_held_changed.emit(frozenset(self._held_mods))
            return

        # Normal runtime. First evaluate hold-to-talk transitions (breaks any
        # currently-active HTT action whose required combo is no longer held).
        self._evaluate_htt_transitions()

        # Then evaluate toggle-style mod_only actions: fire once on the first
        # modifier release that breaks the peak set.
        for name, cfg in self._hotkeys.items():
            if cfg.get("type") != "mod_only":
                continue
            if cfg.get("activation", "hold_to_talk") != "toggle":
                continue  # HTT handled above
            required = frozenset(cfg.get("keys", []))
            if peak == required:
                log.info(f"[HOTKEY] '{name}' fired: {format_hotkey(cfg)} (tap-toggle mode)")
                self.signals.hotkey_fired.emit(name)
                break

    def _evaluate_htt_transitions(self):
        """Called on every modifier state change. For each hold-to-talk action,
        compare the currently-held mod set against the action's required set and
        fire pressed / released transitions exactly once per activation cycle.

        Microtransition suppression: if a release happens within 150ms of the
        corresponding press, the released signal is NOT emitted — the widget
        never sees it, so it keeps recording. However, the widget DID start
        recording on press, so we also emit a synthetic cancel via the
        suppression log. To keep behaviour consistent, we instead avoid
        emitting pressed in the first place if we can, OR suppress both pressed
        and released cleanly. Chosen approach: emit pressed immediately (so
        feedback is instant), then on fast release, emit a cancel signal so
        the widget can discard the audio without submitting to Whisper."""
        current = frozenset(self._held_mods)
        now = time.monotonic()
        for name, cfg in self._hotkeys.items():
            if cfg.get("type") != "mod_only":
                continue
            if cfg.get("activation", "hold_to_talk") != "hold_to_talk":
                continue
            required = frozenset(cfg.get("keys", []))
            matches  = (current == required)
            was_active = self._htt_active.get(name, False)
            if matches and not was_active:
                self._htt_active[name] = True
                self._htt_press_ts[name] = now
                log.info(f"[HOTKEY] '{name}' pressed (hold-to-talk mode): {format_hotkey(cfg)}")
                self.signals.hotkey_pressed.emit(name)
            elif not matches and was_active:
                self._htt_active[name] = False
                press_ts = self._htt_press_ts.pop(name, None)
                held_ms  = (now - press_ts) * 1000 if press_ts else 9999
                if held_ms < self.MICROTRANSITION_MS:
                    log.info(
                        f"[HOTKEY] '{name}' released after only {held_ms:.0f}ms "
                        f"— suppressed (microtransition, under {self.MICROTRANSITION_MS}ms). "
                        f"Discarding audio, not submitting to Whisper."
                    )
                    # Fire a special signal so the widget can cancel its recording
                    # without submitting. Reuse hotkey_released with a prefix.
                    self.signals.hotkey_released.emit(f"__cancel__:{name}")
                else:
                    log.info(f"[HOTKEY] '{name}' released (hold-to-talk mode) after {held_ms:.0f}ms")
                    self.signals.hotkey_released.emit(name)


def setup_hotkeys(app, widget, hotkeys):
    """Install the Raw Input hotkey filter on the QApplication.
    Returns the filter (caller must hold a reference)."""
    filt = GlobalHotkeyFilter(hwnd=int(widget.winId()), hotkeys=hotkeys)
    app.installNativeEventFilter(filt)

    def _on_fired(name):
        """Toggle-mode actions: fire once per activation, flip state."""
        if name == "dictation":
            widget.reposition_to_cursor_screen()
            widget.toggle_dictation()
        elif name == "command_mode":
            if widget._state in (IDLE, "hover"):
                widget.reposition_to_cursor_screen()
                target_hwnd = ctypes.windll.user32.GetForegroundWindow()
                log.info(f"[HOTKEY] command_mode — captured target_hwnd={target_hwnd}")
                threading.Thread(
                    target=widget.start_command_mode,
                    args=(target_hwnd,),
                    daemon=True,
                ).start()
            elif widget._state in (COMMAND_RECORDING, RECORDING):
                log.info("[HOTKEY] command_mode — stopping recording")
                widget.toggle_dictation()
        elif name == "quit":
            log.info("[HOTKEY] quit — shutting down.")
            os._exit(0)

    def _on_pressed(name):
        """Hold-to-talk press: start recording. Ignored if already busy."""
        if name == "dictation":
            if widget._state in (IDLE, "hover"):
                widget.reposition_to_cursor_screen()
                widget.toggle_dictation()  # enters RECORDING
        elif name == "command_mode":
            if widget._state in (IDLE, "hover"):
                widget.reposition_to_cursor_screen()
                target_hwnd = ctypes.windll.user32.GetForegroundWindow()
                log.info(f"[HOTKEY] command_mode pressed (hold-to-talk) — target_hwnd={target_hwnd}")
                threading.Thread(
                    target=widget.start_command_mode,
                    args=(target_hwnd,),
                    daemon=True,
                ).start()

    def _on_released(name):
        """Hold-to-talk release: stop recording + submit. Ignored if not recording.

        A name prefixed with '__cancel__:' signals microtransition suppression —
        the press+release cycle was too fast to be intentional, so we discard
        the audio instead of submitting it to Whisper."""
        if name.startswith("__cancel__:"):
            widget.cancel_recording()
            return
        if name == "dictation":
            if widget._state == RECORDING:
                widget.toggle_dictation()  # stops + transcribes
        elif name == "command_mode":
            if widget._state == COMMAND_RECORDING:
                widget.toggle_dictation()  # stops + executes

    filt.signals.hotkey_fired.connect(_on_fired)
    filt.signals.hotkey_pressed.connect(_on_pressed)
    filt.signals.hotkey_released.connect(_on_released)
    return filt


# ============================================================
# PREFERENCES — KEY-CAPTURE + SETTINGS DIALOGS
# ============================================================
# Two capture widgets, one dialog. Each capture widget drives the live
# GlobalHotkeyFilter in "capture mode" so it sees Raw Input events while the
# dialog is open and the old hotkey doesn't fire. Validation happens on Save.


class _ModOnlyCapture(QFrame):
    """Compact single-line live-preview capture widget for modifier-only
    combos (Ctrl+Win style). User presses and holds the modifier combo; on
    first release we keep the peak as the captured combo. At least 2
    modifiers required. Guidance text lives on the widget's tooltip so the
    row can fit in ~36px of vertical space."""

    def __init__(self, initial_keys, hotkey_filter, parent=None):
        super().__init__(parent)
        self.setObjectName("KairaCaptureWidget")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setFixedHeight(34)
        self.setToolTip(
            "Click Record, then press and hold your desired modifier combo "
            "(at least two of Ctrl/Shift/Alt/Win) and release."
        )
        self._filter = hotkey_filter
        self._captured = frozenset(initial_keys)
        self._active = False

        self._label = QLabel(self._format(self._captured))
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 0, 6, 0)
        lay.setSpacing(0)
        lay.addWidget(self._label)

    def _format(self, keys):
        if not keys:
            return "—"
        ordered = [k for k in MOD_ORDER if k in keys]
        return " + ".join(MOD_DISPLAY[k] for k in ordered)

    def start(self):
        if self._active:
            return
        self._active = True
        self._filter.signals.mod_held_changed.connect(self._on_held_changed)
        self._filter.signals.mod_combo_captured.connect(self._on_captured)
        self._filter.start_capture("mod_only")
        self._label.setText("… hold your combo …")

    def stop(self):
        if not self._active:
            return
        self._active = False
        try:
            self._filter.signals.mod_held_changed.disconnect(self._on_held_changed)
            self._filter.signals.mod_combo_captured.disconnect(self._on_captured)
        except TypeError:
            pass
        self._filter.stop_capture()

    def _on_held_changed(self, held):
        if held:
            self._label.setText(self._format(held))
        else:
            self._label.setText("… hold your combo …")

    def _on_captured(self, peak):
        self._captured = peak
        self._label.setText(self._format(peak))
        self.stop()

    def captured(self):
        return self._captured


class _StandardCapture(QFrame):
    """Capture widget for standard combos (Ctrl+Shift+Q style). Uses Qt's
    key events directly — dialog has focus so keyPressEvent fires. The
    GlobalHotkeyFilter runs in 'standard' capture mode so registered hotkeys
    are suspended (lets user rebind the existing shortcut)."""

    def __init__(self, initial_mods, initial_key, hotkey_filter, parent=None):
        super().__init__(parent)
        self.setObjectName("KairaCaptureWidget")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setFixedHeight(34)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setToolTip(
            "Click Record, then press a standard shortcut like Ctrl+Shift+Q "
            "(at least one modifier plus a regular key)."
        )
        self._filter = hotkey_filter
        self._mods = frozenset(initial_mods)
        self._key = (initial_key or "").lower()
        self._active = False

        self._label = QLabel(self._format(self._mods, self._key))
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 0, 6, 0)
        lay.setSpacing(0)
        lay.addWidget(self._label)

    def _format(self, mods, key):
        if not key:
            return "—"
        parts = [MOD_DISPLAY[m] for m in MOD_ORDER if m in mods]
        parts.append(key.upper() if len(key) == 1 else key.capitalize())
        return " + ".join(parts)

    def start(self):
        if self._active:
            return
        self._active = True
        self._filter.start_capture("standard")
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        self._label.setText("… press your combo …")

    def stop(self):
        if not self._active:
            return
        self._active = False
        self._filter.stop_capture()

    def _qt_key_to_name(self, qt_key):
        """Map a Qt.Key code to our canonical key name string."""
        from PyQt6.QtCore import Qt as _Qt
        # Letters
        if _Qt.Key.Key_A <= qt_key <= _Qt.Key.Key_Z:
            return chr(ord('a') + (qt_key - _Qt.Key.Key_A))
        # Digits (top row)
        if _Qt.Key.Key_0 <= qt_key <= _Qt.Key.Key_9:
            return str(qt_key - _Qt.Key.Key_0)
        # F-keys
        if _Qt.Key.Key_F1 <= qt_key <= _Qt.Key.Key_F24:
            return f"f{qt_key - _Qt.Key.Key_F1 + 1}"
        specials = {
            _Qt.Key.Key_Escape: "escape", _Qt.Key.Key_Tab: "tab",
            _Qt.Key.Key_Space: "space", _Qt.Key.Key_Delete: "delete",
            _Qt.Key.Key_Home: "home", _Qt.Key.Key_End: "end",
            _Qt.Key.Key_PageUp: "pageup", _Qt.Key.Key_PageDown: "pagedown",
            _Qt.Key.Key_Insert: "insert",
        }
        return specials.get(qt_key)

    def keyPressEvent(self, event):
        if not self._active:
            super().keyPressEvent(event)
            return
        name = self._qt_key_to_name(event.key())
        if name is None:
            # Pure modifier press — show live preview of held mods
            mods = set()
            m = event.modifiers()
            if m & Qt.KeyboardModifier.ControlModifier: mods.add("ctrl")
            if m & Qt.KeyboardModifier.ShiftModifier:   mods.add("shift")
            if m & Qt.KeyboardModifier.AltModifier:     mods.add("alt")
            if m & Qt.KeyboardModifier.MetaModifier:    mods.add("win")
            self._label.setText(self._format(mods, "") if mods else "… press your combo …")
            return
        # Real key pressed — capture modifiers at this instant and commit
        mods = set()
        m = event.modifiers()
        if m & Qt.KeyboardModifier.ControlModifier: mods.add("ctrl")
        if m & Qt.KeyboardModifier.ShiftModifier:   mods.add("shift")
        if m & Qt.KeyboardModifier.AltModifier:     mods.add("alt")
        if m & Qt.KeyboardModifier.MetaModifier:    mods.add("win")
        self._mods = frozenset(mods)
        self._key = name
        self._label.setText(self._format(self._mods, self._key))
        self.stop()
        event.accept()

    def captured(self):
        return self._mods, self._key


class PreferencesDialog(QDialog):
    """Three-row preferences dialog — one row per hotkey action. Uses live
    capture widgets driven by the running GlobalHotkeyFilter. Save validates
    (type-appropriate rules, action-to-action conflicts, Windows-reserved
    blocks) then writes settings.json and shows a restart-required prompt."""

    def __init__(self, hotkey_filter, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Kaira — Preferences")
        self.setModal(True)
        self.setMinimumWidth(600)
        self._filter = hotkey_filter
        self._cfg = load_settings()
        hotkeys = self._cfg["hotkeys"]

        # Outer layout: QTabWidget (each section = one tab) + pinned button bar.
        # Rationale: research brief "I'm designing the Preferences dialog for
        # Kaira…" (2026-04-17) found that every mature desktop Preferences
        # uses sidebar-or-tabs; for 2 sections a lightweight QTabWidget is the
        # right fit. Long help paragraphs replaced with tooltips + "About"
        # info links — users read only 20-28% of UI text per NN/Group.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Build the capture widgets + activation-mode dropdowns up front so
        # the tab-building helpers can reference them via self.
        self._dictation_capture = _ModOnlyCapture(
            hotkeys["dictation"].get("keys", []), hotkey_filter
        )
        self._command_capture = _ModOnlyCapture(
            hotkeys["command_mode"].get("keys", []), hotkey_filter
        )
        self._quit_capture = _StandardCapture(
            hotkeys["quit"].get("modifiers", []),
            hotkeys["quit"].get("key", ""),
            hotkey_filter,
        )
        self._dictation_mode = self._make_mode_combo(
            hotkeys["dictation"].get("activation", "hold_to_talk")
        )
        self._command_mode = self._make_mode_combo(
            hotkeys["command_mode"].get("activation", "hold_to_talk")
        )
        vad_state = self._cfg.get("vad", VAD_DEFAULTS)
        self._vad_enabled = QCheckBox("Filter silent recordings (recommended)")
        self._vad_enabled.setChecked(bool(vad_state.get("enabled", True)))
        self._vad_enabled.setToolTip(
            "When enabled, Kaira only sends recordings to OpenAI Whisper "
            "if they actually contain speech. Prevents 'Thank you for watching'-"
            "style hallucinations on silent audio. Recommended."
        )

        self._active_capture = None  # only one capture at a time

        # --- Tab 1: Hotkey Shortcuts ---
        hotkeys_tab = self._build_hotkeys_tab()
        # --- Tab 2: Silence Filter ---
        silence_tab = self._build_silence_tab()

        tabs = QTabWidget()
        tabs.addTab(hotkeys_tab, "Hotkey Shortcuts")
        tabs.addTab(silence_tab, "Silence Filter")
        outer.addWidget(tabs, 1)

        # Error label — lives below the tabs, above the button bar. Always
        # visible when populated regardless of which tab is active.
        self._error = QLabel("")
        self._error.setStyleSheet(
            "color: #cc3333; font-size: 9pt; padding: 4px 20px;"
        )
        self._error.setVisible(False)
        self._error.setWordWrap(True)
        outer.addWidget(self._error)

        # Pinned button bar — always visible.
        btn_bar = QFrame()
        btn_bar.setObjectName("KairaButtonBar")
        btn_bar.setFrameShape(QFrame.Shape.NoFrame)
        btns = QHBoxLayout(btn_bar)
        btns.setContentsMargins(16, 10, 16, 14)
        btns.addStretch()
        reset_btn = QPushButton("Restore Defaults")
        reset_btn.clicked.connect(self._on_reset)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        save_btn = QPushButton("Save")
        save_btn.setDefault(True)
        save_btn.clicked.connect(self._on_save)
        btns.addWidget(reset_btn)
        btns.addWidget(cancel_btn)
        btns.addWidget(save_btn)
        outer.addWidget(btn_bar)

        # --- Single consolidated stylesheet (Fix #1) ---
        # One parse instead of 6+ per-widget calls. Type selectors target
        # capture widgets by their objectName set in __init__.
        self.setStyleSheet("""
            QFrame#KairaCaptureWidget {
                background-color: #1a1a1a;
                border: 1px solid #ff9500;
                border-radius: 4px;
            }
            QFrame#KairaCaptureWidget QLabel {
                color: #ff9500;
                font-size: 11pt;
                font-weight: 600;
                background: transparent;
                border: none;
            }
            QFrame#KairaButtonBar {
                border-top: 1px solid #dddddd;
            }
        """)

        # Fixed size — dialog never scrolls, always fits 1536x816 laptops.
        self.setFixedSize(580, 420)
        self._center_on_cursor_screen()

    def _center_on_cursor_screen(self):
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        if screen is not None:
            geom = screen.availableGeometry()
            x = geom.x() + (geom.width()  - self.width())  // 2
            y = geom.y() + (geom.height() - self.height()) // 2
            self.move(x, y)

    def showEvent(self, event):
        """Disable Windows 11 DWM fade-in animation (Fix #4) and re-center."""
        super().showEvent(event)
        try:
            hwnd = int(self.winId())
            val = ctypes.c_int(1)
            dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_TRANSITIONS_FORCEDISABLED,
                                         ctypes.byref(val), 4)
        except Exception:
            pass
        self._center_on_cursor_screen()

    def closeEvent(self, event):
        """Hide instead of destroy — keeps the singleton instance alive (Fix #3)."""
        if self._active_capture is not None:
            self._active_capture.stop()
            self._active_capture = None
        event.ignore()
        self.hide()

    def set_filter(self, hotkey_filter):
        """Update the hotkey filter reference after a Pause→Resume cycle
        which destroys and recreates the filter. Called before each show."""
        self._filter = hotkey_filter
        self._dictation_capture._filter = hotkey_filter
        self._command_capture._filter = hotkey_filter
        self._quit_capture._filter = hotkey_filter

    def reload_from_settings(self):
        """Re-read settings.json and update all fields. Called before each
        show so the dialog reflects the current saved state, not a stale
        draft from a previous open that was cancelled."""
        self._cfg = load_settings()
        hotkeys = self._cfg["hotkeys"]

        # Capture widgets
        d_keys = hotkeys["dictation"].get("keys", [])
        self._dictation_capture._captured = frozenset(d_keys)
        self._dictation_capture._label.setText(
            self._dictation_capture._format(self._dictation_capture._captured)
        )
        c_keys = hotkeys["command_mode"].get("keys", [])
        self._command_capture._captured = frozenset(c_keys)
        self._command_capture._label.setText(
            self._command_capture._format(self._command_capture._captured)
        )
        q_mods = hotkeys["quit"].get("modifiers", [])
        q_key  = hotkeys["quit"].get("key", "")
        self._quit_capture._mods = frozenset(q_mods)
        self._quit_capture._key  = (q_key or "").lower()
        self._quit_capture._label.setText(
            self._quit_capture._format(self._quit_capture._mods,
                                       self._quit_capture._key)
        )

        # Activation-mode dropdowns
        for combo, name in ((self._dictation_mode, "dictation"),
                            (self._command_mode,   "command_mode")):
            act = hotkeys[name].get("activation", "hold_to_talk")
            idx = combo.findData(act if act in ACTIVATION_MODES else "hold_to_talk")
            combo.setCurrentIndex(max(idx, 0))

        # Silence filter checkbox
        vad_state = self._cfg.get("vad", VAD_DEFAULTS)
        self._vad_enabled.setChecked(bool(vad_state.get("enabled", True)))

        # Reset UI state
        self._error.setVisible(False)
        if self._active_capture is not None:
            self._active_capture.stop()
            self._active_capture = None

    # --- Tab builders ---

    def _build_hotkeys_tab(self) -> QWidget:
        """Tab 1: three compact rows (Dictation / Command / Quit) + tip link."""
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(20, 16, 20, 12)
        lay.setSpacing(10)

        subtitle = QLabel(
            "Hold: press + speak + release.  "
            "Tap: tap to start, tap to stop."
        )
        subtitle.setStyleSheet("color: #666; font-size: 9pt;")
        lay.addWidget(subtitle)

        lay.addLayout(self._build_hotkey_row("Dictation",
                                             self._dictation_capture,
                                             self._dictation_mode))
        lay.addLayout(self._build_hotkey_row("Command mode",
                                             self._command_capture,
                                             self._command_mode))
        lay.addLayout(self._build_hotkey_row("Quit Kaira",
                                             self._quit_capture,
                                             None))

        tips_link = self._build_info_link(
            "ⓘ Tips for choosing combos",
            "Kaira — Tips for choosing combos",
            "• Neither combo can contain all the keys of the other — "
            "e.g. don't use Ctrl+Alt and Ctrl+Shift+Alt together. When you "
            "press the bigger combo you'd briefly trigger the smaller one.\n\n"
            "• Win-key combos can conflict with Windows system shortcuts "
            "(Win+L locks the PC, Win+D shows desktop, Win+H opens Voice "
            "Typing). Pick carefully.\n\n"
            "• Dictation and Command mode are hold-to-talk by default. "
            "Quit is a standard shortcut (tap once).\n\n"
            "• Changes apply after you restart Kaira."
        )
        lay.addWidget(tips_link)
        lay.addStretch()
        return page

    def _build_silence_tab(self) -> QWidget:
        """Tab 2: VAD checkbox + short subtitle + About info link."""
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(20, 16, 20, 12)
        lay.setSpacing(8)

        lay.addWidget(self._vad_enabled)
        subtitle = QLabel(
            "Skips recordings with no detected speech. Prevents Whisper "
            "from hallucinating phrases like \"Thank you for watching\" "
            "on silent audio."
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet("color: #666; font-size: 9pt; margin-left: 22px;")
        lay.addWidget(subtitle)

        about_link = self._build_info_link(
            "ⓘ About the silence filter",
            "Kaira — About the silence filter",
            "Kaira runs a three-layer voice-activity check on every recording "
            "before calling OpenAI Whisper:\n\n"
            "1. RMS energy check — rejects true silence in ~0.1ms.\n"
            "2. Silero VAD — rejects near-silence and ambient noise.\n"
            "3. Hallucination phrase filter — catches known-bad outputs "
            "  like \"Thank you for watching\" at the Whisper output stage.\n\n"
            "Recordings shorter than 0.5 seconds or longer than 10 seconds "
            "bypass the Silero layer automatically (too short to classify "
            "reliably, too long to be accidental)."
        )
        lay.addWidget(about_link)
        lay.addStretch()
        return page

    def _build_hotkey_row(self, label_text: str, capture, mode_combo):
        """A single compact row: [label | capture | mode | Record]."""
        row = QHBoxLayout()
        row.setSpacing(8)
        lbl = QLabel(label_text)
        lbl.setMinimumWidth(110)
        row.addWidget(lbl)
        row.addWidget(capture, 1)
        if mode_combo is not None:
            mode_combo.setFixedWidth(120)
            mode_combo.setToolTip(
                "Hold to talk: press + speak + release.\n"
                "Tap (toggle): tap to start, tap again to stop."
            )
            row.addWidget(mode_combo)
        else:
            # Keep vertical alignment even without a mode combo on this row.
            spacer = QLabel("")
            spacer.setFixedWidth(120)
            row.addWidget(spacer)
        rec_btn = QPushButton("Record")
        rec_btn.setFixedWidth(76)
        rec_btn.clicked.connect(lambda: self._start_capture(capture))
        row.addWidget(rec_btn)
        return row

    def _build_info_link(self, label_text: str, dialog_title: str, body: str) -> QPushButton:
        """A subtle grey 'ⓘ About…' flat button. Hover shows the text as a
        tooltip; click opens it in a QMessageBox. Uses plain text (no HTML
        QLabel) to avoid the expensive QTextDocument initialization that was
        causing the dialog to lag on first open."""
        btn = QPushButton(label_text)
        btn.setFlat(True)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setToolTip(body)
        btn.clicked.connect(
            lambda _=None, t=dialog_title, b=body:
                QMessageBox.information(self, t, b)
        )
        return btn

    def _make_mode_combo(self, current: str) -> "QComboBox":
        combo = QComboBox()
        for key in ACTIVATION_MODES:
            combo.addItem(ACTIVATION_DISPLAY[key], userData=key)
        idx = combo.findData(current if current in ACTIVATION_MODES else "hold_to_talk")
        combo.setCurrentIndex(max(idx, 0))
        combo.setFixedWidth(140)
        return combo

    def _start_capture(self, widget):
        # Already mid-capture on this widget? Don't restart (avoids a reset
        # flash if user double-clicks Record).
        if getattr(widget, "_active", False):
            return
        # Stop any OTHER capture widget that's still live.
        if self._active_capture is not None and self._active_capture is not widget:
            try:
                self._active_capture.stop()
            except Exception:
                pass
        # Clear the previously-captured value so the user sees the slot is
        # being re-recorded. Without this, the old combo stays visible until
        # they finish pressing a new one — confusing if they declined and
        # came back to change it.
        if isinstance(widget, _ModOnlyCapture):
            widget._captured = frozenset()
        elif isinstance(widget, _StandardCapture):
            widget._mods = frozenset()
            widget._key  = ""
        self._active_capture = widget
        widget.start()

    def _on_reset(self):
        cfg = _default_hotkeys_copy()
        self._dictation_capture._captured = frozenset(cfg["dictation"]["keys"])
        self._dictation_capture._label.setText(
            self._dictation_capture._format(self._dictation_capture._captured)
        )
        self._command_capture._captured = frozenset(cfg["command_mode"]["keys"])
        self._command_capture._label.setText(
            self._command_capture._format(self._command_capture._captured)
        )
        self._quit_capture._mods = frozenset(cfg["quit"]["modifiers"])
        self._quit_capture._key = cfg["quit"]["key"]
        self._quit_capture._label.setText(
            self._quit_capture._format(
                self._quit_capture._mods, self._quit_capture._key
            )
        )
        # Reset activation modes too.
        for combo, name in ((self._dictation_mode, "dictation"),
                            (self._command_mode,   "command_mode")):
            idx = combo.findData(cfg[name].get("activation", "hold_to_talk"))
            combo.setCurrentIndex(max(idx, 0))
        # Reset the silence filter toggle to its default too.
        self._vad_enabled.setChecked(bool(VAD_DEFAULTS["enabled"]))

    def _on_save(self):
        dictation = set(self._dictation_capture.captured())
        command   = set(self._command_capture.captured())
        quit_mods, quit_key = self._quit_capture.captured()

        # Per-action validation
        err = self._validate_mod_only(dictation, "Dictation")
        if err: return self._show_error(err)
        err = self._validate_mod_only(command, "Command mode")
        if err: return self._show_error(err)
        err = self._validate_standard(quit_mods, quit_key, "Quit")
        if err: return self._show_error(err)

        # Cross-action conflict: identical combos
        dict_fs = frozenset(dictation)
        cmd_fs  = frozenset(command)
        if dict_fs == cmd_fs:
            return self._show_error(
                "Dictation and Command mode can't use the same shortcut."
            )

        # Rule A: neither combo may be a subset of the other. If dictation is
        # contained inside command (or vice versa), the user will briefly
        # trigger the subset combo while assembling the larger one — accidental
        # Whisper submissions of empty audio. Mathematically proven necessary
        # and sufficient by research brief "Kaira Hotkey Validation..." (2026-04-16).
        if dict_fs < cmd_fs:
            return self._show_error(
                "These shortcuts overlap — Command mode includes all of "
                "Dictation's keys. When you reach for Command mode, "
                "Kaira will briefly trigger Dictation first. Change one "
                "shortcut so they don't share all the same keys."
            )
        if cmd_fs < dict_fs:
            return self._show_error(
                "These shortcuts overlap — Dictation includes all of "
                "Command mode's keys. When you reach for Dictation, "
                "Kaira will briefly trigger Command mode first. Change "
                "one shortcut so they don't share all the same keys."
            )

        dictation_mode = self._dictation_mode.currentData()
        command_mode   = self._command_mode.currentData()

        # Commit hotkeys
        self._cfg["hotkeys"] = {
            "dictation":    {"type": "mod_only",
                             "keys": sorted(dictation),
                             "activation": dictation_mode},
            "command_mode": {"type": "mod_only",
                             "keys": sorted(command),
                             "activation": command_mode},
            "quit":         {"type": "standard",
                             "modifiers": sorted(quit_mods),
                             "key": quit_key},
        }
        # Commit silence-filter toggle (merge into existing vad block to
        # preserve any other tuning values users may have set manually).
        existing_vad = self._cfg.get("vad") or dict(VAD_DEFAULTS)
        existing_vad["enabled"] = bool(self._vad_enabled.isChecked())
        self._cfg["vad"] = existing_vad

        save_settings(self._cfg)
        log.info(f"[SETTINGS] Saved hotkeys: {self._cfg['hotkeys']}")

        box = QMessageBox(self)
        box.setWindowTitle("Kaira — Restart required")
        box.setText("Your new hotkeys are saved.")
        box.setInformativeText("Kaira needs to restart to apply them.")
        box.setIcon(QMessageBox.Icon.Information)
        restart_btn = box.addButton("Restart Now", QMessageBox.ButtonRole.AcceptRole)
        later_btn   = box.addButton("Restart Later", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(restart_btn)
        box.exec()

        self.accept()
        if box.clickedButton() is restart_btn:
            log.info("[SETTINGS] User chose Restart Now")
            _restart_kaira_now()

    def _validate_mod_only(self, keys, label):
        if not keys:
            return f"{label}: capture a shortcut first."
        if len(keys) < 2:
            return (f"{label} needs at least 2 modifiers held together "
                    f"(e.g. Ctrl + Win). A single modifier isn't enough.")
        if keys == {"ctrl", "shift"}:
            return (f"{label}: Ctrl + Shift alone switches keyboard layouts "
                    f"on many Windows systems. Pick a different combo.")
        if not keys.issubset(set(MOD_NAMES)):
            return f"{label}: invalid modifier in {keys}."
        return None

    def _validate_standard(self, mods, key, label):
        if not key:
            return f"{label}: capture a shortcut first."
        if not mods:
            return (f"{label} needs at least one modifier "
                    f"(e.g. Ctrl + Shift + Q).")
        sev = is_reserved_standard(mods, key)
        if sev == "critical":
            return (f"{label}: that combo is reserved by Windows and won't "
                    f"work (e.g. Win+L locks your PC). Pick another.")
        if sev == "high":
            return (f"{label}: that combo conflicts with a common Windows "
                    f"shortcut. Pick another to avoid surprises.")
        return None

    def _show_error(self, text):
        self._error.setText(text)
        self._error.setVisible(True)



# ============================================================
# MAIN
# ============================================================

def main():
    log.info("[BOOT] Kaira starting...")
    log.info(f"[BOOT] Python: {sys.executable}")
    log.info(f"[BOOT] PID: {os.getpid()}")
    log.info(f"[BOOT] CWD: {os.getcwd()}")

    lock = acquire_single_instance_lock()
    if lock is None:
        log.warning("[BOOT] BLOCKED — another instance holds the lock. Exiting.")
        return
    log.info("[BOOT] Single-instance lock acquired")
    main._lock = lock

    log.info("[BOOT] Creating QApplication...")
    app = QApplication(sys.argv)

    # Set the Qt-level window icon so it appears on dialog title bars,
    # in the taskbar (for dev runs), and in Alt-Tab. PyInstaller already
    # bakes kaira.ico into the .exe metadata via the spec; this call makes
    # it visible inside Qt-owned windows too.
    # Lookup order: sys._MEIPASS (PyInstaller runtime dir — _internal/ in
    # onedir, temp dir in onefile), then _internal/ next to the exe, then
    # the exe's own folder, then SCRIPT_DIR (dev runs from source).
    try:
        candidates = []
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(os.path.join(meipass, "kaira.ico"))
        exe_dir = os.path.dirname(sys.executable)
        candidates.append(os.path.join(exe_dir, "_internal", "kaira.ico"))
        candidates.append(os.path.join(exe_dir, "kaira.ico"))
        candidates.append(os.path.join(SCRIPT_DIR, "kaira.ico"))
        for icon_path in candidates:
            if os.path.exists(icon_path):
                app.setWindowIcon(QIcon(icon_path))
                log.info(f"[BOOT] Window icon loaded: {icon_path}")
                break
        else:
            log.warning(f"[BOOT] kaira.ico not found in any candidate path: {candidates}")
    except Exception as e:
        log.warning(f"[BOOT] Could not load window icon: {e}")

    api_key = ensure_api_key(app)
    if not api_key:
        log.error("[BOOT] No API key — exiting.")
        return
    os.environ["OPENAI_API_KEY"] = api_key
    client.api_key = api_key
    log.info("[BOOT] API key ready")

    mic_name = _check_microphone_or_prompt()
    if mic_name is None:
        return
    log.info(f"[BOOT] Mic OK: {mic_name}")

    _check_sticky_keys_advisory()

    settings = load_settings()
    hotkeys  = settings["hotkeys"]
    start_paused = settings.get("paused", False)
    log.info("=" * 48)
    log.info("  Kaira -- Voice Dictation Tool (PyQt6)")
    log.info("=" * 48)
    log.info(f"  Mic: {mic_name}")
    log.info(f"  Log: {log_file}")
    log.info(f"  {format_hotkey(hotkeys['dictation'], include_mode=True):<40} = Dictation")
    log.info(f"  {format_hotkey(hotkeys['command_mode'], include_mode=True):<40} = Command mode")
    log.info(f"  {format_hotkey(hotkeys['quit']):<40} = Quit")
    log.info("  Click widget         = Start / stop dictation")
    log.info(f"  Boot state: {'PAUSED' if start_paused else 'RUNNING'}")
    log.info("=" * 48)
    cleanup_old_audio()

    threading.Thread(target=_warmup_uia, daemon=True).start()

    # Pre-warm Qt's native QTabWidget style so the first Preferences open
    # doesn't pay the one-time Windows UXTheme DLL + style-engine load
    # (~200-500ms before this pre-warm was added). This must run on the
    # main Qt thread — Qt widgets cannot be constructed from worker threads.
    try:
        _warm_tab = QTabWidget()
        _warm_tab.addTab(QWidget(), "warm")
        _warm_tab.deleteLater()
        log.info("[BOOT] QTabWidget theme pre-warmed")
    except Exception as e:
        log.warning(f"[BOOT] QTabWidget pre-warm failed: {e}")

    # Warm up Silero VAD on a background thread so first dictation is fast.
    # _VAD_AVAILABLE is False if silero-vad-lite import failed at module load.
    if _VAD_AVAILABLE:
        threading.Thread(target=_warmup_vad, daemon=True).start()
    else:
        log.warning(
            f"[VAD] silero-vad-lite not available ({_vad_import_error!r}). "
            f"Recordings will be sent to Whisper without a silence gate — "
            f"hallucination phrase filter still runs on output."
        )

    vad_cfg = settings.get("vad", VAD_DEFAULTS)
    log.info(f"[VAD] Silence gate {'enabled' if vad_cfg.get('enabled') else 'DISABLED'} "
             f"(threshold {vad_cfg.get('threshold', 0.30):.2f}, "
             f"min-chunks {vad_cfg.get('min_speech_chunks', 2)}, "
             f"bypass <{vad_cfg.get('skip_below_ms', 500)}ms and "
             f">{vad_cfg.get('skip_above_ms', 10000)}ms)")

    log.info("[BOOT] Creating widget...")
    widget = KairaWidget()
    # Gate visibility via _user_hidden BEFORE first show() so the paused-boot
    # case never flashes the overlay on-screen. In running state we show and
    # defense layers initialise via showEvent; in paused state we skip show.
    widget._user_hidden = start_paused
    if not start_paused:
        widget.show()

    screen = app.primaryScreen().availableGeometry()
    log.info(f"[BOOT] Screen: {screen.width()}x{screen.height()} at ({screen.x()},{screen.y()})")
    log.info(f"[BOOT] Widget pos: ({widget.x()}, {widget.y()}) size: {widget.width()}x{widget.height()}")
    log.info(f"[BOOT] Widget visible: {widget.isVisible()}")

    # Stash globals so pause_kaira() / resume_kaira() can coordinate.
    main._app    = app
    main._widget = widget
    main._paused = start_paused

    if start_paused:
        log.info("[BOOT] Starting in PAUSED state (no hotkey filter, overlay hidden)")
        main._hotkey_filter = None
    else:
        log.info(f"[BOOT] Ready. Waiting for {format_hotkey(hotkeys['dictation'])}...")
        main._hotkey_filter = setup_hotkeys(app, widget, hotkeys)
        log.info("[BOOT] Raw Input hotkey filter installed")

    main._tray = _setup_tray(app, widget, main._hotkey_filter, start_paused=start_paused)

    # v1.2: offer to retry any recordings left unsent from a prior session.
    # Delay slightly so the tray icon is fully registered before the toast fires.
    if not start_paused:
        QTimer.singleShot(2500, lambda: _offer_startup_retries(widget, main._tray))

    exit_code = app.exec()
    log.info(f"[SHUTDOWN] Qt event loop ended with code {exit_code}")
    sys.exit(exit_code)


if __name__ == "__main__":
    # Catch any unhandled crash so it always hits the log
    def _crash_handler(exc_type, exc_value, exc_tb):
        import traceback
        tb = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        log.error(f"[CRASH] Unhandled exception:\n{tb}")
        sys.__excepthook__(exc_type, exc_value, exc_tb)
    sys.excepthook = _crash_handler

    try:
        main()
    except Exception as e:
        log.error(f"[CRASH] Fatal: {e}", exc_info=True)
