"""Session-wide safety guard (safety remediation): offline tests must never open or load the real
automation/.env (it holds the real API keys).

Installed at import time -- before any test module or project module is imported -- rather than
through a monkeypatch fixture, because some tests call monkeypatch.undo(), which would silently
remove a fixture-based guard. Two layers:

1. config.load_dotenv (the only loader the project uses) is wrapped: a call for the real .env, or
   with no path (python-dotenv would then SEARCH for a .env, finding the real one), is refused --
   nothing is read -- and recorded; a call for a temporary test .env is delegated normally.
2. builtins.open / io.open refuse the real .env path outright (PermissionError), so even an
   unexpected direct read fails loudly instead of happening.

The guard never reads .env -- it compares paths only. The env_load_attempts fixture exposes the
refused attempts so tests can assert exactly when the project TRIED to load the environment.
"""

import builtins
import io
import os
import sys
from pathlib import Path

import pytest

AUTOMATION_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AUTOMATION_ROOT))

REAL_ENV = os.path.normcase(os.path.abspath(AUTOMATION_ROOT / ".env"))
REFUSED_ENV_LOADS: list = []   # per test (reset by the env_load_attempts fixture)
SESSION_COUNTS = {"refused_loads": 0, "blocked_opens": 0}  # whole session, never reset


def _is_real_env(file) -> bool:
    if isinstance(file, int):
        return False
    try:
        return os.path.normcase(os.path.abspath(os.fspath(file))) == REAL_ENV
    except TypeError:
        return False


def _guard_open(real_open):
    def guarded(file, *args, **kwargs):
        if _is_real_env(file):
            SESSION_COUNTS["blocked_opens"] += 1
            raise PermissionError("offline tests must never open the real automation/.env")
        return real_open(file, *args, **kwargs)

    guarded.__wrapped__ = real_open
    return guarded


builtins.open = _guard_open(builtins.open)
io.open = _guard_open(io.open)

from src import config  # noqa: E402 -- after the open guard, before any test module

_REAL_LOAD_DOTENV = config.load_dotenv


def _guarded_load_dotenv(dotenv_path=None, *args, **kwargs):
    if dotenv_path is None or _is_real_env(dotenv_path):
        REFUSED_ENV_LOADS.append(dotenv_path)
        SESSION_COUNTS["refused_loads"] += 1
        return False  # refused: nothing read, nothing set
    return _REAL_LOAD_DOTENV(dotenv_path, *args, **kwargs)


config.load_dotenv = _guarded_load_dotenv


def pytest_sessionfinish(session, exitstatus):
    # Any blocked open of the real .env (outside the guard's own self-test, which resets the
    # count) fails the whole run, even if the PermissionError was swallowed somewhere.
    if SESSION_COUNTS["blocked_opens"]:
        session.exitstatus = 1


def pytest_terminal_summary(terminalreporter):
    terminalreporter.write_line(
        f".env guard: {SESSION_COUNTS['blocked_opens']} blocked open(s) of the real automation/.env; "
        f"{SESSION_COUNTS['refused_loads']} refused load attempt(s) (the real .env was never read)")


@pytest.fixture(autouse=True)
def env_load_attempts():
    """Refused attempts to load the real .env during the current test (normally empty)."""
    REFUSED_ENV_LOADS.clear()
    yield REFUSED_ENV_LOADS
    REFUSED_ENV_LOADS.clear()
