"""
log_config.py
=============
Centralised logging for the Momentum Trading Platform.

Handlers
--------
1. StreamHandler        — stdout  (Docker / gunicorn captures this)
2. RotatingFileHandler  — /app/logs/angel_margin_trading_app.log
                          (50 MB cap, 1 backup so disk never fills)
3. S3SyncThread         — daemon thread; reads the full local log file and
                          overwrites the single S3 key every 3 minutes.

S3 key  — one fixed file, grows continuously
--------------------------------------------
    logs/angel_margin_trading_app.log

Sync behaviour
--------------
Every SYNC_SECS seconds the thread reads /app/logs/angel_margin_trading_app.log
from byte 0 to EOF and PUT-overwrites the S3 key.  Because S3 has no native
append, overwriting the complete local file is the only way to maintain a
single, always-current log object.  The atexit hook guarantees a final sync
on gunicorn/SIGTERM shutdown so no lines are lost.

Usage
-----
    # very top of app.py, right after ssm_config.bootstrap()
    from log_config import setup_logging
    setup_logging()

Environment variables
---------------------
    S3_BUCKET        — reused from ssm_config  (default dhan-trading-data)
    AWS_REGION                                  (default ap-south-1)
    LOG_LEVEL        DEBUG / INFO / WARNING     (default INFO)
    LOG_S3_PREFIX    S3 folder prefix           (default logs)
    LOG_SYNC_SECS    upload interval in seconds (default 180 = 3 min)
    LOG_LOCAL_DIR    local log directory        (default /app/logs)
    LOG_S3_ENABLED   "false" to disable S3      (default true)
"""

from __future__ import annotations

import atexit
import logging
import logging.handlers
import os
import sys
import threading
from aws_s3 import S3_BUCKET as ACTIVE_S3_BUCKET

# ── tunables ─────────────────────────────────────────────────────────────────
LOG_LEVEL     = os.getenv("LOG_LEVEL",      "INFO").upper()
S3_BUCKET = ACTIVE_S3_BUCKET
S3_PREFIX     = os.getenv("LOG_S3_PREFIX",  "trading-bot/logs")
SYNC_SECS     = int(os.getenv("LOG_SYNC_SECS",  "60"))   # 1 minutes
LOCAL_LOG_DIR = os.getenv("LOG_LOCAL_DIR",  "/app/logs")
S3_ENABLED    = os.getenv("LOG_S3_ENABLED", "true").lower() == "true"
AWS_REGION    = os.getenv("AWS_REGION",     "ap-south-1")

LOG_FILENAME = "angel_margin_trading_app.log"
S3_KEY       = f"{S3_PREFIX}/{LOG_FILENAME}"        # logs/angel_margin_trading_app.log
LOCAL_PATH   = os.path.join(LOCAL_LOG_DIR, LOG_FILENAME)

_FMT = "%(asctime)s [%(levelname)s] %(name)s — %(message)s"
_DFT = "%Y-%m-%d %H:%M:%S"

_log = logging.getLogger(__name__)


# ── S3 sync thread ───────────────────────────────────────────────────────────
class S3SyncThread(threading.Thread):
    """
    Daemon thread: wakes every SYNC_SECS seconds, reads the entire local
    log file, and PUT-overwrites the single fixed S3 key.

    Thread-safety  : open() on the log path; Python's RotatingFileHandler
                     holds its own lock so concurrent reads are safe.
    Fault-tolerance: every S3 error is printed to stderr and swallowed —
                     a log failure never interrupts the trading app.
    Graceful exit  : atexit hook calls sync_now() for the final flush.
    """

    def __init__(self) -> None:
        super().__init__(name="s3-log-sync", daemon=True)
        self._stop = threading.Event()
        self._client = None
        self._last_sig = None          # (size, mtime) of last uploaded file

    def stop(self) -> None:
        self._stop.set()

    def sync_now(self, label: str = "sync") -> None:
        """Read local log file and overwrite the S3 key."""
        if not os.path.exists(LOCAL_PATH):
            return
        try:
            st  = os.stat(LOCAL_PATH)
            sig = (st.st_size, st.st_mtime)
            if sig == self._last_sig:
                return                  # nothing new since last upload
            if self._client is None:
                import boto3
                self._client = boto3.client("s3", region_name=AWS_REGION)
            with open(LOCAL_PATH, "rb") as fh:
                data = fh.read()

            self._client.put_object(
                Bucket      = S3_BUCKET,
                Key         = S3_KEY,
                Body        = data,
                ContentType = "text/plain",
            )
            self._last_sig = sig
            _log.debug(
                "[LogSync] %s → s3://%s/%s  (%d bytes)",
                label, S3_BUCKET, S3_KEY, len(data),
            )
        except Exception as exc:
            print(f"[LogSync] {label} failed — {exc}", file=sys.stderr)

    def run(self) -> None:
        _log.info(
            "[LogSync] Started — s3://%s/%s  every %ds",
            S3_BUCKET, S3_KEY, SYNC_SECS,
        )
        while not self._stop.wait(SYNC_SECS):
            self.sync_now()


# ── module singleton ──────────────────────────────────────────────────────────
_sync_thread: S3SyncThread | None = None


# ── public entry point ────────────────────────────────────────────────────────
def setup_logging():
    global _sync_thread

    root = logging.getLogger()

    # Remove existing handlers installed by Flask/Gunicorn/etc.
    for h in root.handlers[:]:
        root.removeHandler(h)

    root.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

    fmt = logging.Formatter(_FMT, datefmt=_DFT)

    #
    # STDOUT
    #
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    #
    # FILE
    #
    os.makedirs(LOCAL_LOG_DIR, exist_ok=True)

    fh = logging.handlers.RotatingFileHandler(
        filename=LOCAL_PATH,
        maxBytes=50 * 1024 * 1024,
        backupCount=1,
        encoding="utf-8"
    )

    fh.setFormatter(fmt)
    root.addHandler(fh)

    #
    # Silence AWS noise
    #
    for lib in ("boto3", "botocore", "urllib3", "s3transfer"):
        logging.getLogger(lib).setLevel(logging.WARNING)

    #
    # S3 Sync
    #
    if S3_ENABLED:
        _sync_thread = S3SyncThread()
        _sync_thread.start()
        atexit.register(_final_sync)

    #
    # TEST
    #
    logging.info("=" * 80)
    logging.info("Logging Initialized")
    logging.info("Root Handlers : %s", root.handlers)
    logging.info("Log File      : %s", LOCAL_PATH)
    logging.info("S3 Bucket     : %s", S3_BUCKET)
    logging.info("=" * 80)


def _final_sync() -> None:
    """atexit — push the last log lines to S3 before the process exits."""
    if _sync_thread:
        _sync_thread.sync_now(label="final-sync")