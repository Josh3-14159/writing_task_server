#!/usr/bin/env python3
"""
watchdog.py — Keep server.py alive
====================================
Spawns server.py as a subprocess and restarts it if it exits for any reason.
Respects PORT / DATABASE_URL from the environment, or accepts --port on the
command line (mirrors server.py's own flag).

Usage
-----
  python3 watchdog.py                  # default port 8080
  python3 watchdog.py --port 3000
  python3 watchdog.py --port 3000 --server /path/to/server.py

Signals
-------
  SIGTERM / SIGINT  — gracefully stop the child then exit the watchdog.
  SIGHUP            — restart the child immediately (useful for config reload).

Environment variables
---------------------
  DATABASE_URL           Required by server.py.
  READONLY_DATABASE_URL  Recommended for server.py's export endpoints.
  PORT                   Fallback port (overridden by --port).
  WATCHDOG_MAX_RESTARTS  Max consecutive fast restarts before backing off
                         (default: 5).
  WATCHDOG_BACKOFF_SEC   Seconds to wait after hitting the restart cap
                         (default: 30).
  WATCHDOG_MIN_UPTIME_SEC
                         Seconds a process must run to be considered "healthy"
                         and reset the consecutive-restart counter (default: 10).
"""

import argparse
import os
import signal
import subprocess
import sys
import time

# ---------------------------------------------------------------------------
# Configuration (env-overridable)
# ---------------------------------------------------------------------------

MAX_RESTARTS    = int(os.environ.get('WATCHDOG_MAX_RESTARTS',   5))
BACKOFF_SEC     = int(os.environ.get('WATCHDOG_BACKOFF_SEC',   30))
MIN_UPTIME_SEC  = int(os.environ.get('WATCHDOG_MIN_UPTIME_SEC', 10))

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------

_child: subprocess.Popen | None = None
_stop  = False   # set by SIGTERM / SIGINT
_restart_now = False  # set by SIGHUP


def _ts():
    return time.strftime('%Y-%m-%d %H:%M:%S')


def _log(msg: str):
    print(f'[watchdog {_ts()}]  {msg}', flush=True)


# ---------------------------------------------------------------------------
# Signal handlers
# ---------------------------------------------------------------------------

def _handle_stop(signum, _frame):
    global _stop
    sig_name = signal.Signals(signum).name
    _log(f'Received {sig_name} — stopping.')
    _stop = True
    if _child and _child.poll() is None:
        _log(f'Sending SIGTERM to child PID {_child.pid}')
        _child.terminate()


def _handle_hup(_signum, _frame):
    global _restart_now
    _log('Received SIGHUP — scheduling child restart.')
    _restart_now = True
    if _child and _child.poll() is None:
        _child.terminate()


signal.signal(signal.SIGTERM, _handle_stop)
signal.signal(signal.SIGINT,  _handle_stop)
try:
    signal.signal(signal.SIGHUP, _handle_hup)
except AttributeError:
    pass  # Windows doesn't have SIGHUP


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run(server_script: str, port: int):
    global _child, _stop, _restart_now

    cmd = [sys.executable, server_script, '--port', str(port)]
    _log(f'Starting: {" ".join(cmd)}')

    consecutive_fast_restarts = 0

    while not _stop:
        _restart_now = False
        start_time = time.monotonic()

        try:
            _child = subprocess.Popen(cmd)
        except FileNotFoundError:
            _log(f'ERROR: server script not found: {server_script}')
            sys.exit(1)

        _log(f'Child started  PID={_child.pid}  port={port}')

        # Wait for child to exit (poll so we can react to _stop / _restart_now)
        while True:
            ret = _child.poll()
            if ret is not None:
                break
            if _stop:
                # Watchdog is shutting down; wait for child to finish.
                try:
                    _child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    _log('Child did not exit in time — sending SIGKILL')
                    _child.kill()
                    _child.wait()
                return
            time.sleep(0.5)

        uptime = time.monotonic() - start_time
        _log(f'Child exited  PID={_child.pid}  code={ret}  uptime={uptime:.1f}s')

        if _stop:
            return

        # ── restart logic ───────────────────────────────────────────────────
        if uptime >= MIN_UPTIME_SEC:
            # Process was healthy long enough — reset the fast-restart counter.
            consecutive_fast_restarts = 0
        else:
            consecutive_fast_restarts += 1
            _log(
                f'Fast exit #{consecutive_fast_restarts} '
                f'(threshold={MIN_UPTIME_SEC}s).'
            )

        if not _restart_now and consecutive_fast_restarts >= MAX_RESTARTS:
            _log(
                f'Hit {MAX_RESTARTS} consecutive fast restarts. '
                f'Backing off for {BACKOFF_SEC}s before next attempt.'
            )
            # Sleep in small increments so SIGTERM can still interrupt us.
            for _ in range(BACKOFF_SEC * 2):
                if _stop:
                    return
                time.sleep(0.5)
            consecutive_fast_restarts = 0

        _log('Restarting child…')


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Watchdog daemon for server.py')
    parser.add_argument(
        '--port', '-p', type=int,
        default=int(os.environ.get('PORT', 8080)),
        help='Port to pass to server.py (default: 8080)',
    )
    parser.add_argument(
        '--server', '-s',
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'server.py'),
        help='Path to server.py (default: same directory as this script)',
    )
    args = parser.parse_args()

    if not os.environ.get('DATABASE_URL'):
        sys.exit(
            'ERROR: DATABASE_URL is not set.\n'
            'Example: export DATABASE_URL="postgresql://user:pass@localhost/handwriting"'
        )

    _log(f'Watchdog started  PID={os.getpid()}  server={args.server}  port={args.port}')
    run(args.server, args.port)
    _log('Watchdog exiting.')
