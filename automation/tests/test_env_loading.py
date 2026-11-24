""".env safety remediation: the real automation/.env may be loaded ONLY by an explicitly requested
live provider run (no --dry-run, no --quota-plan), after every argument and configuration check
passes, immediately before the client is built.

Every test here spies on config.load_provider_environment (the project's only loader) AND on
config.load_dotenv, and forbids constructing a real provider client. Settings come from temporary
files; tests/conftest.py additionally refuses any open/load of the real .env session-wide. No test
reads, prints or inspects .env contents or key values.
"""

import hashlib
import io
import os
import subprocess
import sys
from pathlib import Path

AUTOMATION_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AUTOMATION_ROOT))

import pytest
import yaml
from PIL import Image

import run_pipeline
from src import config, extraction, vision_client as vc

KEY_VARS = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GROQ_API_KEY")


@pytest.fixture
def spy(monkeypatch):
    """events: ordered record of env loads ("env" / "dotenv") and client construction attempts."""
    events = []
    monkeypatch.setattr(config, "load_provider_environment", lambda: events.append("env"))
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **kw: events.append("dotenv"))

    def _no_client(name):
        def _fail(*a, **kw):
            events.append(f"client:{name}")
            raise AssertionError(f"{name} must not be constructed on this path")
        return _fail

    monkeypatch.setattr(vc, "GeminiVisionClient", _no_client("gemini"))
    monkeypatch.setattr(vc, "GroqVisionClient", _no_client("groq"))
    for var in KEY_VARS:
        monkeypatch.delenv(var, raising=False)
    return events


def _settings(tmp_path: Path) -> dict:
    project = tmp_path / "project"
    for week in ("Week 1", "Week 2"):
        (project / week).mkdir(parents=True, exist_ok=True)
        (project / week / "Report (1).xlsx").write_bytes(b"placeholder; extract_week is patched")
    auto = tmp_path / "automation"
    return {
        "provider": "gemini", "model": "gemini-3.8-flash",
        "rate_limit": {"max_retries": 4, "backoff_seconds": 4, "max_backoff_seconds": 60, "jitter_seconds": 4,
                       "circuit_breaker_threshold": 2, "request_timeout_seconds": 300, "requests_per_minute": 15},
        "paths": {"project_root": str(project), "automation_root": str(auto),
                  "state_dir": str(auto / ".state"), "logs_dir": str(auto / "logs")},
        "report": {"output_filename": "report.xlsx", "extracted_images_dirname": "Extracted Images",
                   "image_cell_max_width_px": 420, "image_cell_max_height_px": 320},
        "processing": {"heading_min_merge_width": 10},
    }


def _jpeg(tag):
    d = hashlib.sha256(tag.encode()).digest()
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color=(d[0], d[1], d[2])).save(buf, format="JPEG")
    return buf.getvalue()


def _patch_extraction(monkeypatch):
    images = [extraction.ExtractedImage(anchor_row=1, anchor_col=1, official_location=loc, image_bytes=_jpeg(t),
                                        image_ext="jpg", source_workbook="Report (1).xlsx", worksheet_name="S")
              for t, loc in (("a", "Not Labelled"), ("b", "MAIN DECK"))]
    monkeypatch.setattr(extraction, "extract_week", lambda *a, **kw: extraction.ExtractionResult(
        images=images, report_date=None, warnings=[]))


def _main(tmp_path, monkeypatch, argv, data="VALID", text=None):
    """Real main() reading a temporary settings.yaml through the real config loader.
    Returns the exit code (None = normal return)."""
    monkeypatch.setattr(config, "AUTOMATION_ROOT", tmp_path)
    path = tmp_path / "config" / "settings.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    if text is None:
        text = yaml.safe_dump(_settings(tmp_path) if data == "VALID" else data)
    path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["run_pipeline.py", *argv])
    try:
        run_pipeline.main()
    except SystemExit as exc:
        return exc.code
    return None


# --- --quota-plan -----------------------------------------------------------------------------


@pytest.mark.parametrize("argv", [["--quota-plan"], ["--quota-plan", "--strict-exit"],
                                  ["--quota-plan", "--weeks", "week 2", "--daily-request-limit", "20"]])
def test_quota_plan_never_loads_env_or_builds_a_client(tmp_path, monkeypatch, capsys, spy, argv):
    _patch_extraction(monkeypatch)
    assert _main(tmp_path, monkeypatch, argv) is None
    assert spy == []
    assert "QUOTA PLAN (read-only: no API calls, nothing written)" in capsys.readouterr().out
    assert not (tmp_path / "automation").exists()  # still read-only: no log, state or output


# --- invalid CLI arguments (exit 2) -----------------------------------------------------------


@pytest.mark.parametrize("argv", [["--sample", "0"], ["--sample", "-1"], ["--sample"], ["--weeks"],
                                  ["--bogus-flag"], ["--daily-request-limit", "5"],
                                  ["--quota-plan", "--dry-run"], ["--quota-plan", "--sample", "1"]])
def test_invalid_arguments_never_load_env_or_settings(monkeypatch, spy, argv):
    monkeypatch.setattr(sys, "argv", ["run_pipeline.py", *argv])
    monkeypatch.setattr(config, "load_settings", lambda *a, **kw: spy.append("settings"))
    with pytest.raises(SystemExit) as exc_info:
        run_pipeline.main()
    assert exc_info.value.code == 2
    assert spy == []  # not even settings.yaml was read


@pytest.mark.parametrize("argv", [["--weeks", "Week 99"], ["--weeks", "Week 1", "Nope", "--strict-exit"]])
def test_unknown_week_selection_never_loads_env(tmp_path, monkeypatch, spy, argv):
    assert _main(tmp_path, monkeypatch, argv) == 2
    assert spy == []


# --- configuration errors (exit 1) ------------------------------------------------------------


def _broken(tmp_path, mutate):
    data = _settings(tmp_path)
    mutate(data)
    return data


CONFIG_ERRORS = [
    ("invalid yaml", None, "provider: [unclosed"),
    ("empty yaml", None, ""),
    ("missing processing", lambda d: d.pop("processing"), None),
    ("missing provider", lambda d: d.pop("provider"), None),
    ("rate_limit list", lambda d: d.update(rate_limit=[1]), None),
    ("negative retries", lambda d: d["rate_limit"].update(max_retries=-1), None),
    ("short timeout", lambda d: d["rate_limit"].update(request_timeout_seconds=5), None),
    ("rpm zero", lambda d: d["rate_limit"].update(requests_per_minute=0), None),
    ("nan backoff", lambda d: d["rate_limit"].update(backoff_seconds=float("nan")), None),
    ("breaker zero", lambda d: d["rate_limit"].update(circuit_breaker_threshold=0), None),
    ("unsupported provider", lambda d: d.update(provider="openai"), None),
]


# (a dry run deliberately does not check the provider name -- existing behaviour -- so that one
# combination is not a configuration error and is not generated)
CONFIG_ERROR_CASES = [pytest.param(argv, *case, id=f"{case[0]}-{' '.join(argv) or 'live'}")
                      for case in CONFIG_ERRORS for argv in ([], ["--strict-exit"], ["--dry-run"])
                      if not (case[0] == "unsupported provider" and argv == ["--dry-run"])]


@pytest.mark.parametrize("argv, label, mutate, text", CONFIG_ERROR_CASES)
def test_configuration_errors_never_load_env_or_build_a_client(tmp_path, monkeypatch, spy, argv, label,
                                                                mutate, text):
    data = _broken(tmp_path, mutate) if mutate else None
    code = _main(tmp_path, monkeypatch, argv, data=data, text=text)
    assert code == 1  # configuration errors still exit 1 (never 3 with --strict-exit)
    assert spy == []  # live mode included: validation runs BEFORE the only .env load


def test_validate_client_settings_is_pure(spy):
    names_before = set(os.environ)
    policy, rpm = vc.validate_client_settings("gemini", {"requests_per_minute": 15}, dry_run=False)
    assert policy.max_attempts == 5 and rpm == 15
    with pytest.raises(vc.VisionAPIError, match="Unsupported provider"):
        vc.validate_client_settings("openai", None, dry_run=False)
    assert vc.validate_client_settings("openai", None, dry_run=True)[1] is None  # dry run: unchanged
    assert spy == [] and set(os.environ) == names_before


# --- dry runs -----------------------------------------------------------------------------------


@pytest.mark.parametrize("argv", [["--dry-run"], ["--dry-run", "--strict-exit"], ["--dry-run", "--sample", "1"],
                                  ["--dry-run", "--weeks", "Week 1", "--force"]])
def test_dry_run_never_loads_env(tmp_path, monkeypatch, spy, argv):
    _patch_extraction(monkeypatch)
    assert _main(tmp_path, monkeypatch, argv) is None  # completes; --strict-exit still 0
    assert spy == []
    assert (tmp_path / "automation" / "dry_run" / "output").is_dir()  # P0-1: isolated output only
    assert not (tmp_path / "automation" / "output").exists()


# --- the one authorized path: an explicit live run --------------------------------------------


class _Stub(vc.VisionClient):
    def __init__(self, events):
        self.events, self.calls = events, 0

    def classify(self, image_bytes):
        self.calls += 1
        return {"person_visible": "No", "activity_identified": "No activity observed", "activity_confidence": "High",
                "ai_suggested_location": "Main Deck", "location_confidence": "High"}


@pytest.mark.parametrize("argv", [[], ["--strict-exit"], ["--sample", "1"], ["--weeks", "Week 2", "--force"]])
def test_live_run_loads_env_exactly_once_immediately_before_the_client(tmp_path, monkeypatch, spy, argv):
    _patch_extraction(monkeypatch)
    stub = _Stub(spy)

    def _build(*a, **kw):
        spy.append("client")
        return stub

    monkeypatch.setattr(vc, "build_vision_client", _build)
    assert _main(tmp_path, monkeypatch, argv) is None
    assert spy == ["env", "client"]  # exactly one load, then the client, nothing else
    assert stub.calls >= 1


def test_live_run_loads_env_after_all_validation(tmp_path, monkeypatch, spy):
    order = []
    real_threshold, real_validate = run_pipeline._circuit_breaker_threshold, vc.validate_client_settings
    monkeypatch.setattr(run_pipeline, "_circuit_breaker_threshold",
                        lambda s: order.append("breaker") or real_threshold(s))
    monkeypatch.setattr(vc, "validate_client_settings",
                        lambda *a, **kw: order.append("validate") or real_validate(*a, **kw))
    monkeypatch.setattr(config, "load_provider_environment", lambda: order.append("env"))
    monkeypatch.setattr(vc, "build_vision_client", lambda *a, **kw: order.append("client") or _Stub([]))
    _patch_extraction(monkeypatch)
    assert _main(tmp_path, monkeypatch, []) is None
    # (run_weeks() later reads the breaker threshold again to build the breaker itself)
    assert order[:4] == ["breaker", "validate", "env", "client"] and order.count("env") == 1


def test_live_run_missing_key_is_still_exit_1_after_the_authorized_load(tmp_path, monkeypatch, spy):
    monkeypatch.undo()  # real GeminiVisionClient (raises before any network: no key), spies re-added
    events = []
    monkeypatch.setattr(config, "load_provider_environment", lambda: events.append("env"))
    for var in KEY_VARS:
        monkeypatch.delenv(var, raising=False)
    assert _main(tmp_path, monkeypatch, []) == 1
    assert events == ["env"]  # the authorized live path ran its single load, then failed cleanly


def test_real_load_provider_environment_is_refused_by_the_test_guard(env_load_attempts):
    # The unpatched project loader, called directly: conftest's guard refuses the real path, so
    # nothing is read; the attempt is only recorded.
    config.load_provider_environment()
    assert env_load_attempts == [config.env_file_path()]


# --- importing modules -------------------------------------------------------------------------


IMPORT_PROBE = r"""
import builtins, io, os, sys
root = sys.argv[1]
real = os.path.normcase(os.path.abspath(os.path.join(root, ".env")))
calls = {"dotenv": 0, "open": 0}
import dotenv, dotenv.main
def no_load(*a, **kw):
    calls["dotenv"] += 1
    return False
dotenv.load_dotenv = dotenv.main.load_dotenv = no_load
for mod, name in ((builtins, "open"), (io, "open")):
    orig = getattr(mod, name)
    def guarded(f, *a, _orig=orig, **kw):
        if not isinstance(f, int) and os.path.normcase(os.path.abspath(os.fspath(f))) == real:
            calls["open"] += 1
            raise PermissionError("blocked")
        return _orig(f, *a, **kw)
    setattr(mod, name, guarded)
names_before = set(os.environ)
sys.path.insert(0, root)
import run_pipeline
from src import config, vision_client, discovery, extraction, classifier, state, report_builder
from src import logging_utils, image_prep
print(calls["dotenv"], calls["open"], int(set(os.environ) == names_before))
"""


def test_importing_project_modules_never_loads_env(tmp_path):
    # A fresh interpreter (not this already-imported test process): load_dotenv is replaced and
    # opening the real .env is blocked BEFORE any project import; nothing may be attempted.
    env = {k: v for k, v in os.environ.items() if k not in KEY_VARS}
    result = subprocess.run([sys.executable, "-c", IMPORT_PROBE, str(AUTOMATION_ROOT)], cwd=str(tmp_path),
                            env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.split() == ["0", "0", "1"]  # 0 loads, 0 opens, environment names unchanged


def test_config_module_has_no_import_time_side_effects():
    source = (AUTOMATION_ROOT / "src" / "config.py").read_text(encoding="utf-8")
    top_level_calls = [line for line in source.splitlines()
                       if line and not line[0].isspace() and "load_dotenv(" in line]
    assert top_level_calls == []
    assert source.count("load_dotenv(") == 1  # only inside load_provider_environment()


def test_only_one_production_call_site_loads_the_environment():
    sources = {p: p.read_text(encoding="utf-8") for p in
               [AUTOMATION_ROOT / "run_pipeline.py", *(AUTOMATION_ROOT / "src").glob("*.py")]}
    callers = {p.name: s.count("load_provider_environment()") for p, s in sources.items()
               if "load_provider_environment()" in s and p.name != "config.py"}
    assert callers == {"run_pipeline.py": 1}
    assert all("load_dotenv(" not in s for p, s in sources.items() if p.name != "config.py")
