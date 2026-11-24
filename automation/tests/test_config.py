"""Phase P2-4: offline tests for configuration loading (src/config.py) and its validation, from a
settings.yaml file through to the CLI's exit code.

Safety: since the .env safety remediation, config.load_settings() never loads .env; only
config.load_provider_environment() does, on an explicit live run. Here config.load_dotenv is also a
recorder by default, real dotenv parsing is exercised only on a temporary .env (AUTOMATION_ROOT
re-pointed at tmp_path), and tests/conftest.py refuses any open/load of the real automation/.env
session-wide. Only fake key strings are used, and no test prints a key.
No client ever sends a request: live-mode CLI tests stop at configuration validation, and the
successful runs are dry runs (NullVisionClient).

The six defects P2-3/P2-4 recorded as xfail(strict=True) were fixed in the P2 remediation phase;
their tests now pass as ordinary tests with their original assertions, plus regression tests.
"""

import hashlib
import io
import logging
import math
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
import yaml
from PIL import Image

import run_pipeline
from src import config, extraction, vision_client as vc

FAKE_KEY = "fake-key-p24-never-real-0123456789"
REAL_SETTINGS = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"
KEY_VARS = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GROQ_API_KEY")


@pytest.fixture(autouse=True)
def _never_read_real_env(monkeypatch):
    """Default for every test: load_dotenv is a recorder, so the real .env is never read. Tests
    that exercise real dotenv parsing re-point AUTOMATION_ROOT at tmp_path first."""
    calls = []
    monkeypatch.setattr(config, "load_dotenv", lambda path, *a, **kw: calls.append(Path(path)))
    for var in KEY_VARS:
        monkeypatch.delenv(var, raising=False)
    return calls


def _valid_settings(tmp_path: Path) -> dict:
    project = tmp_path / "project"
    (project / "Week 1").mkdir(parents=True, exist_ok=True)
    (project / "Week 1" / "Report (1).xlsx").write_bytes(b"placeholder; extract_week is patched")
    auto = tmp_path / "automation"
    return {
        "provider": "gemini", "model": "gemini-3.8-flash",
        "rate_limit": {"max_retries": 4, "backoff_seconds": 4, "max_backoff_seconds": 60, "jitter_seconds": 4,
                       "circuit_breaker_threshold": 2, "request_timeout_seconds": 300,
                       "requests_per_minute": 15},
        "paths": {"project_root": str(project), "automation_root": str(auto),
                  "state_dir": str(auto / ".state"), "logs_dir": str(auto / "logs")},
        "report": {"output_filename": "report.xlsx", "extracted_images_dirname": "Extracted Images",
                   "image_cell_max_width_px": 420, "image_cell_max_height_px": 320},
        "processing": {"heading_min_merge_width": 10},
    }


def _write_yaml(tmp_path: Path, data, text: str | None = None) -> Path:
    """Write tmp_path/config/settings.yaml and point config.AUTOMATION_ROOT's default at it."""
    path = tmp_path / "config" / "settings.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text is not None else yaml.safe_dump(data), encoding="utf-8")
    return path


def _jpeg(tag: str) -> bytes:
    d = hashlib.sha256(tag.encode()).digest()
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color=(d[0], d[1], d[2])).save(buf, format="JPEG")
    return buf.getvalue()


def _patch_extraction(monkeypatch):
    images = [extraction.ExtractedImage(anchor_row=1, anchor_col=1, official_location=loc,
                                        image_bytes=_jpeg(tag), image_ext="jpg",
                                        source_workbook="Report (1).xlsx", worksheet_name="Sheet1")
              for tag, loc in (("a", "Not Labelled"), ("b", "MAIN DECK"))]
    monkeypatch.setattr(extraction, "extract_week", lambda *a, **kw: extraction.ExtractionResult(
        images=images, report_date=None, warnings=[]))


def _run_cli(tmp_path, monkeypatch, caplog, settings_data, argv=(), text=None):
    """Real main() reading a real temp settings.yaml through the real config.load_settings().
    Returns (exit_code or None, pipeline log messages, run_weeks_called)."""
    monkeypatch.setattr(config, "AUTOMATION_ROOT", tmp_path)
    _write_yaml(tmp_path, settings_data, text)
    monkeypatch.setattr(sys, "argv", ["run_pipeline.py", *argv])
    called = {"run_weeks": False}
    real_run_weeks = run_pipeline.run_weeks

    def _spy(*a, **kw):
        called["run_weeks"] = True
        return real_run_weeks(*a, **kw)

    monkeypatch.setattr(run_pipeline, "run_weeks", _spy)
    code = None
    with caplog.at_level(logging.INFO, logger="pipeline"):
        try:
            run_pipeline.main()
        except SystemExit as exc:
            code = exc.code
    return code, [r.message for r in caplog.records if r.name == "pipeline"], called["run_weeks"]


# --- 1. valid configuration loading ----------------------------------------------------------


def test_load_settings_reads_the_given_file_and_never_loads_dotenv(tmp_path, _never_read_real_env,
                                                                   env_load_attempts):
    data = _valid_settings(tmp_path)
    path = _write_yaml(tmp_path, data)
    assert config.load_settings(path) == data
    assert config.load_validated_settings(path) == data
    assert _never_read_real_env == [] and env_load_attempts == []  # safety remediation


def test_load_provider_environment_targets_only_automation_env(_never_read_real_env):
    config.load_provider_environment()  # the recorder stands in for load_dotenv: nothing is read
    assert _never_read_real_env == [config.AUTOMATION_ROOT / ".env"] == [config.env_file_path()]


def test_load_settings_default_path_is_automation_root_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOMATION_ROOT", tmp_path)
    data = _valid_settings(tmp_path)
    _write_yaml(tmp_path, data)
    assert config.load_settings() == data


def test_real_project_settings_yaml_is_complete_and_passes_every_validator(_never_read_real_env):
    settings = config.load_settings(REAL_SETTINGS)  # .env NOT read: load_dotenv is the recorder
    assert set(settings) >= {"provider", "model", "rate_limit", "paths", "report", "processing"}
    assert settings["provider"] in ("gemini", "groq") and settings["model"]
    assert set(settings["paths"]) >= {"project_root", "automation_root", "state_dir", "logs_dir"}
    assert set(settings["report"]) >= {"output_filename", "extracted_images_dirname",
                                       "image_cell_max_width_px", "image_cell_max_height_px"}
    assert isinstance(settings["processing"]["heading_min_merge_width"], int)
    assert "force_reprocess" not in settings["processing"]  # P2-12: removed (only --force reprocesses)
    rl = settings["rate_limit"]
    assert vc.RetryPolicy.from_settings(rl).max_attempts == 5
    assert vc.requests_per_minute_from_settings(rl) == 15
    assert run_pipeline._circuit_breaker_threshold(settings) == 2
    assert isinstance(vc.build_vision_client(settings["provider"], settings["model"], dry_run=True,
                                             rate_limit=rl), vc.NullVisionClient)


# --- 2. missing configuration / 3. invalid YAML ---------------------------------------------


def test_missing_settings_file_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        config.load_settings(tmp_path / "nope.yaml")


@pytest.mark.parametrize("text", ["provider: [unclosed", "a: b: c", "\tprovider: gemini", "key: 'open"])
def test_invalid_yaml_raises_yaml_error(tmp_path, text):
    with pytest.raises(yaml.YAMLError):
        config.load_settings(_write_yaml(tmp_path, None, text))


def test_empty_settings_file_loads_as_none(tmp_path):
    assert config.load_settings(_write_yaml(tmp_path, None, "")) is None
    assert config.load_settings(_write_yaml(tmp_path, None, "# only a comment\n")) is None


def test_yaml_is_loaded_safely(tmp_path):
    with pytest.raises(yaml.YAMLError):  # safe_load refuses arbitrary Python objects
        config.load_settings(_write_yaml(tmp_path, None, "x: !!python/object/apply:os.getcwd []"))


def test_cli_missing_settings_file_is_a_clean_configuration_error(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(config, "AUTOMATION_ROOT", tmp_path)  # no config/settings.yaml here
    monkeypatch.setattr(sys, "argv", ["run_pipeline.py", "--dry-run"])
    with pytest.raises(SystemExit) as exc_info:
        run_pipeline.main()
    assert exc_info.value.code == 1


@pytest.fixture
def _stderr(capsys):
    return lambda: capsys.readouterr().err


def _expect_load_config_error(tmp_path, monkeypatch, caplog, data, argv=(), text=None, stderr=None) -> str:
    """Run the CLI expecting a configuration error from loading/validating settings.yaml.
    Asserts the full contract (exit 1, one stderr message, nothing run, built or written) and
    returns the message."""
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_KEY)
    monkeypatch.setattr(vc, "GeminiVisionClient", lambda *a, **kw: pytest.fail("no client may be built"))
    monkeypatch.setattr(vc, "GroqVisionClient", lambda *a, **kw: pytest.fail("no client may be built"))
    monkeypatch.setattr(run_pipeline.logging_utils, "configure_logging",
                        lambda *a, **kw: pytest.fail("logging/output must not start"))
    code, msgs, ran = _run_cli(tmp_path, monkeypatch, caplog, data, list(argv), text=text)
    message = stderr().strip()
    assert code == 1
    assert not ran and msgs == []  # no pipeline log line at all
    assert message.startswith("Configuration error: ") and message.count("Configuration error") == 1
    assert "\n" not in message and "Traceback" not in message
    assert message.endswith("No images were processed and no API requests were made.")
    assert FAKE_KEY not in message
    for d in ("automation", "project/Week 1/Extracted Images"):  # no logs, state, output, dry_run
        assert not (tmp_path / d).exists()
    return message


def test_cli_missing_settings_file_message(tmp_path, monkeypatch, caplog, _stderr):
    monkeypatch.setattr(config, "AUTOMATION_ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["run_pipeline.py"])
    with pytest.raises(SystemExit) as exc_info:
        run_pipeline.main()
    message = _stderr()
    assert exc_info.value.code == 1
    assert message.startswith("Configuration error: settings file not found: ")
    assert str(tmp_path / "config" / "settings.yaml") in message


@pytest.mark.parametrize("argv", [[], ["--dry-run"], ["--strict-exit"], ["--quota-plan"]])
@pytest.mark.parametrize("text, fragment", [
    ("provider: [unclosed", "settings file is not valid YAML"),
    ("a: b: c", "settings file is not valid YAML"),
    ("", "settings file is empty"),
    ("# only a comment\n", "settings file is empty"),
    ("- provider\n- model\n", "settings file must contain a mapping of settings, got list"),
    ("just a string", "settings file must contain a mapping of settings, got str"),
])
def test_cli_invalid_or_empty_yaml_is_a_clean_configuration_error(tmp_path, monkeypatch, caplog, _stderr, argv,
                                                                   text, fragment):
    message = _expect_load_config_error(tmp_path, monkeypatch, caplog, None, argv, text=text, stderr=_stderr)
    assert fragment in message
    if "YAML" in fragment:
        assert " at line " in message and "column" in message  # where, not a raw traceback


def test_cli_unreadable_settings_file_is_a_clean_configuration_error(tmp_path, monkeypatch, caplog, _stderr):
    def _denied(*a, **kw):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(config, "load_settings", _denied)
    monkeypatch.setattr(sys, "argv", ["run_pipeline.py"])
    with pytest.raises(SystemExit) as exc_info:
        run_pipeline.main()
    message = _stderr()
    assert exc_info.value.code == 1
    assert message.startswith("Configuration error: ") and "settings file could not be read" in message
    assert "PermissionError: Permission denied" in message


# --- 4. missing required settings -----------------------------------------------------------


@pytest.mark.parametrize("argv", [[], ["--dry-run"], ["--strict-exit"], ["--dry-run", "--strict-exit"]])
@pytest.mark.parametrize("drop, fragment", [
    ("paths", "missing required section 'paths'"),
    ("report", "missing required section 'report'"),
    ("processing", "missing required section 'processing'"),
    ("provider", "missing required setting 'provider'"),
    ("model", "missing required setting 'model'"),
])
def test_cli_missing_required_setting_is_a_clean_configuration_error(tmp_path, monkeypatch, caplog, _stderr,
                                                                      drop, fragment, argv):
    # P2 remediation (defect 6): previously a raw KeyError, or -- for processing / report --
    # every week failing on its own while the run exited 0. Now: exit 1 before any processing,
    # with or without --strict-exit (a configuration error is never exit 3).
    data = _valid_settings(tmp_path)
    del data[drop]
    _patch_extraction(monkeypatch)
    assert fragment in _expect_load_config_error(tmp_path, monkeypatch, caplog, data, argv, stderr=_stderr)


@pytest.mark.parametrize("section, key", [(s, k) for s, k, _ in config.REQUIRED_SETTINGS if s])
def test_cli_missing_required_nested_setting_is_named(tmp_path, monkeypatch, caplog, _stderr, section, key):
    data = _valid_settings(tmp_path)
    del data[section][key]
    message = _expect_load_config_error(tmp_path, monkeypatch, caplog, data, ["--dry-run"], stderr=_stderr)
    assert f"missing required setting '{section}.{key}'" in message


@pytest.mark.parametrize("mutate, fragment", [
    (lambda d: d.update(provider=""), "'provider' must be a non-empty string"),
    (lambda d: d.update(model=None), "'model' must be a non-empty string, got NoneType"),
    (lambda d: d.update(report=["x"]), "'report' must be a mapping, got list"),
    (lambda d: d["paths"].update(logs_dir=5), "'paths.logs_dir' must be a non-empty string, got int"),
    (lambda d: d["report"].update(image_cell_max_width_px=0), "'report.image_cell_max_width_px' must be an integer >= 1, got 0"),
    (lambda d: d["report"].update(image_cell_max_height_px="320"), "'report.image_cell_max_height_px' must be an integer >= 1"),
    (lambda d: d["processing"].update(heading_min_merge_width=True), "'processing.heading_min_merge_width' must be an integer >= 1, got True"),
])
def test_cli_mistyped_required_setting_is_a_clean_configuration_error(tmp_path, monkeypatch, caplog, _stderr,
                                                                      mutate, fragment):
    data = _valid_settings(tmp_path)
    mutate(data)
    assert fragment in _expect_load_config_error(tmp_path, monkeypatch, caplog, data, stderr=_stderr)


def test_all_required_problems_are_reported_together(tmp_path, monkeypatch, caplog, _stderr):
    data = _valid_settings(tmp_path)
    del data["model"], data["processing"], data["paths"]["state_dir"]
    message = _expect_load_config_error(tmp_path, monkeypatch, caplog, data, stderr=_stderr)
    for fragment in ("missing required section 'processing'", "missing required setting 'model'",
                     "missing required setting 'paths.state_dir'"):
        assert fragment in message


def test_optional_settings_are_not_required(tmp_path):
    # L1: tmp_path -- _valid_settings() creates a placeholder workbook, so it must never be
    # given a path relative to the working directory (it used to write automation/unused/).
    data = _valid_settings(tmp_path)
    del data["rate_limit"]  # P2-12: processing.force_reprocess no longer exists at all
    assert data["processing"] == {"heading_min_merge_width": 10}
    assert config.validate_settings(data) is data


def test_load_validated_settings_on_valid_file_returns_same_dict(tmp_path):
    data = _valid_settings(tmp_path)
    assert config.load_validated_settings(_write_yaml(tmp_path, data)) == data == \
        config.load_settings(tmp_path / "config" / "settings.yaml")


# --- 5-9, 14. numeric / retry / timeout / provider / rpm validation through to the CLI ------


INVALID_RATE_LIMITS = [
    ({"backoff_seconds": "4"}, "rate_limit.backoff_seconds"),           # quoted in YAML -> str
    ({"max_retries": 2.5}, "rate_limit.max_retries"),
    ({"max_retries": -1}, "rate_limit.max_retries"),
    ({"jitter_seconds": -1}, "rate_limit.jitter_seconds"),
    ({"backoff_seconds": 10, "max_backoff_seconds": 5}, "rate_limit.max_backoff_seconds"),
    ({"request_timeout_seconds": 29}, "rate_limit.request_timeout_seconds"),
    ({"request_timeout_seconds": "300"}, "rate_limit.request_timeout_seconds"),
    ({"request_timeout_seconds": 300.5}, "rate_limit.request_timeout_seconds"),
    ({"requests_per_minute": 0}, "rate_limit.requests_per_minute"),
    ({"requests_per_minute": 15.0}, "rate_limit.requests_per_minute"),
    ({"requests_per_minute": True}, "rate_limit.requests_per_minute"),
    ({"requests_per_minute": "15"}, "rate_limit.requests_per_minute"),
    ({"circuit_breaker_threshold": 0}, "rate_limit.circuit_breaker_threshold"),
    ({"circuit_breaker_threshold": 1.5}, "rate_limit.circuit_breaker_threshold"),
]


@pytest.mark.parametrize("argv", [[], ["--dry-run"]])
@pytest.mark.parametrize("override, fragment", INVALID_RATE_LIMITS)
def test_cli_invalid_rate_limit_is_a_clean_configuration_error(tmp_path, monkeypatch, caplog,
                                                                override, fragment, argv):
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_KEY)
    data = _valid_settings(tmp_path)
    data["rate_limit"].update(override)
    code, msgs, ran = _run_cli(tmp_path, monkeypatch, caplog, data, argv)
    errors = [m for m in msgs if m.startswith("Configuration error: ")]
    assert code == 1 and not ran and len(errors) == 1
    assert fragment in errors[0] and "No images were processed and no API requests were made" in errors[0]
    assert all(FAKE_KEY not in m for m in msgs)  # the key never reaches any log record
    # (The run_*.log FILE cannot be asserted in-process: logging.basicConfig is a no-op once
    # pytest's capture handler is on the root logger. The log records above are what it writes.)


@pytest.mark.parametrize("bad", [[1, 2], "fast"])
def test_cli_non_mapping_rate_limit_is_a_clean_configuration_error(tmp_path, monkeypatch, caplog, bad):
    data = _valid_settings(tmp_path)
    data["rate_limit"] = bad
    code, msgs, _ = _run_cli(tmp_path, monkeypatch, caplog, data, ["--dry-run"])
    assert code == 1 and any("rate_limit must be a mapping" in m for m in msgs)


@pytest.mark.parametrize("field, value", [("backoff_seconds", math.nan), ("max_backoff_seconds", math.inf),
                                          ("jitter_seconds", math.nan)])
def test_non_finite_backoff_values_are_rejected(field, value):
    with pytest.raises(vc.VisionAPIError):
        vc.RetryPolicy.from_settings({field: value})


@pytest.mark.parametrize("field", ["backoff_seconds", "max_backoff_seconds", "jitter_seconds"])
@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_every_non_finite_value_is_rejected_for_every_float_setting(field, value):
    with pytest.raises(vc.VisionAPIError, match=f"rate_limit.{field} must be a finite, non-negative number"):
        vc.RetryPolicy.from_settings({field: value})


@pytest.mark.parametrize("field", ["max_retries", "request_timeout_seconds"])
@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_integer_settings_already_reject_non_finite_values(field, value):
    with pytest.raises(vc.VisionAPIError, match=f"rate_limit.{field}"):
        vc.RetryPolicy.from_settings({field: value})
    with pytest.raises(vc.VisionAPIError, match="requests_per_minute"):
        vc.requests_per_minute_from_settings({"requests_per_minute": value})
    with pytest.raises(ValueError, match="circuit_breaker_threshold"):
        run_pipeline._circuit_breaker_threshold({"rate_limit": {"circuit_breaker_threshold": value}})


@pytest.mark.parametrize("text", [".nan", ".inf", "-.inf", ".NaN", ".Inf"])
def test_cli_yaml_non_finite_backoff_is_a_clean_configuration_error(tmp_path, monkeypatch, caplog, text):
    data = _valid_settings(tmp_path)
    yaml_text = yaml.safe_dump(data).replace("backoff_seconds: 4", f"backoff_seconds: {text}", 1)
    assert f"backoff_seconds: {text}" in yaml_text
    code, msgs, ran = _run_cli(tmp_path, monkeypatch, caplog, None, ["--dry-run"], text=yaml_text)
    assert code == 1 and not ran
    assert any("rate_limit.backoff_seconds must be a finite, non-negative number" in m for m in msgs)


def test_valid_numeric_ranges_unchanged():
    assert vc.RetryPolicy.from_settings({"backoff_seconds": 0, "max_backoff_seconds": 0, "jitter_seconds": 0})
    assert vc.RetryPolicy.from_settings({"backoff_seconds": 0.5, "max_backoff_seconds": 1e6, "jitter_seconds": 2.5})
    assert vc.RetryPolicy() == vc.RetryPolicy(max_retries=4, backoff_seconds=4, max_backoff_seconds=60,
                                              jitter_seconds=4, request_timeout_seconds=300)


@pytest.mark.parametrize("bad", [[], "", 0, ("a",)])
def test_falsy_or_other_non_mapping_rate_limit_is_rejected_too(tmp_path, monkeypatch, caplog, bad):
    with pytest.raises(ValueError, match="rate_limit must be a mapping"):
        run_pipeline._circuit_breaker_threshold({"rate_limit": bad})
    with pytest.raises(vc.VisionAPIError, match="rate_limit must be a mapping"):
        vc.RetryPolicy.from_settings(bad)


def test_null_rate_limit_still_means_defaults():
    assert run_pipeline._circuit_breaker_threshold({"rate_limit": None}) == 2
    assert vc.RetryPolicy.from_settings(None) == vc.RetryPolicy()


def test_cli_unknown_provider_is_rejected_live(tmp_path, monkeypatch, caplog):
    data = {**_valid_settings(tmp_path), "provider": "openai"}
    code, msgs, ran = _run_cli(tmp_path, monkeypatch, caplog, data)
    assert code == 1 and not ran
    assert any("Unsupported provider 'openai'. Supported: gemini, groq." in m for m in msgs)


def test_cli_unknown_provider_is_accepted_in_dry_run(tmp_path, monkeypatch, caplog):
    # pinned, intentional (see test_build_vision_client_dry_run_returns_null_client_regardless_of_provider):
    # a dry run never builds a provider client, so the provider name is not checked
    _patch_extraction(monkeypatch)
    code, _, ran = _run_cli(tmp_path, monkeypatch, caplog, {**_valid_settings(tmp_path), "provider": "openai"},
                            ["--dry-run"])
    assert code is None and ran


@pytest.mark.parametrize("provider, var, message", [
    ("gemini", "GEMINI_API_KEY", "No Gemini API key found"),
    ("groq", "GROQ_API_KEY", "No Groq API key found"),
])
def test_cli_missing_key_is_a_clean_configuration_error(tmp_path, monkeypatch, caplog, provider, var, message):
    data = {**_valid_settings(tmp_path), "provider": provider}
    # P2-9: a live Groq run also requires its own pacing cap; supplied so the KEY check is reached
    data["rate_limit"] = {**data["rate_limit"], "groq_requests_per_minute": 2}
    code, msgs, ran = _run_cli(tmp_path, monkeypatch, caplog, data)
    assert code == 1 and not ran
    assert [m for m in msgs if m.startswith("Configuration error: ")][0].count(message) == 1


# --- 10. null requests_per_minute / 12. missing optional settings ---------------------------


def test_null_requests_per_minute_disables_pacing(monkeypatch):
    assert vc.requests_per_minute_from_settings({"requests_per_minute": None}) is None
    assert vc.requests_per_minute_from_settings({}) is None and vc.requests_per_minute_from_settings(None) is None
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_KEY)
    client = vc.build_vision_client("gemini", "gemini-3.8-flash", dry_run=False,
                                    rate_limit={"requests_per_minute": None})
    assert client._min_request_interval == 0.0  # constructing the client sends nothing


@pytest.mark.parametrize("rate_limit", [{"requests_per_minute": None}, "MISSING", None, {}])
def test_cli_runs_with_null_or_missing_optional_rate_limit(tmp_path, monkeypatch, caplog, rate_limit):
    data = _valid_settings(tmp_path)
    if rate_limit == "MISSING":
        del data["rate_limit"]
    else:
        data["rate_limit"] = rate_limit
    _patch_extraction(monkeypatch)
    code, msgs, ran = _run_cli(tmp_path, monkeypatch, caplog, data, ["--dry-run", "--strict-exit"])
    assert code is None and ran
    assert "--strict-exit: processing completed successfully; exit code 0." in msgs


def test_missing_optional_settings_fall_back_to_documented_defaults():
    assert vc.RetryPolicy.from_settings(None) == vc.RetryPolicy(
        max_retries=4, backoff_seconds=4, max_backoff_seconds=60, jitter_seconds=4, request_timeout_seconds=300)
    assert run_pipeline._circuit_breaker_threshold({}) == 2
    assert run_pipeline._circuit_breaker_threshold({"rate_limit": None}) == 2
    assert vc.RetryPolicy.from_settings({"max_retries": 1}).request_timeout_seconds == 300  # partial block


def test_unused_processing_force_reprocess_setting_is_ignored(tmp_path, monkeypatch, caplog):
    # pinned (reported): processing.force_reprocess is read by nothing; only --force re-classifies.
    # P2-12 removed it from settings.yaml; an older settings file that still has it must keep
    # loading (no new configuration error) and the leftover key must still change nothing.
    data = _valid_settings(tmp_path)
    data["processing"]["force_reprocess"] = True
    _patch_extraction(monkeypatch)
    _run_cli(tmp_path, monkeypatch, caplog, data, ["--dry-run"])
    assert "force_reprocess" not in Path(run_pipeline.__file__).read_text(encoding="utf-8")


# --- 11. environment-variable handling / 13. secret redaction -------------------------------


def test_dotenv_file_is_loaded_without_overriding_existing_environment(tmp_path, monkeypatch):
    monkeypatch.undo()  # drop the autouse recorder: use the REAL load_dotenv, on a TEMP .env only
    monkeypatch.setattr(config, "AUTOMATION_ROOT", tmp_path)
    (tmp_path / ".env").write_text("P24_FROM_DOTENV=from-file\nP24_ALREADY_SET=from-file\n", encoding="utf-8")
    _write_yaml(tmp_path, {"provider": "gemini"})
    monkeypatch.setenv("P24_ALREADY_SET", "from-environment")
    monkeypatch.delenv("P24_FROM_DOTENV", raising=False)
    try:
        assert config.load_settings() == {"provider": "gemini"}
        assert "P24_FROM_DOTENV" not in os.environ  # settings loading never loads .env
        config.load_provider_environment()  # the explicit loader, on the TEMP .env
        assert os.environ["P24_FROM_DOTENV"] == "from-file"
        assert os.environ["P24_ALREADY_SET"] == "from-environment"  # real env wins (override=False)
    finally:
        os.environ.pop("P24_FROM_DOTENV", None)


def test_missing_dotenv_file_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(config, "AUTOMATION_ROOT", tmp_path)  # no .env here
    _write_yaml(tmp_path, {"provider": "gemini"})
    assert config.load_settings() == {"provider": "gemini"}
    config.load_provider_environment()  # missing file: a no-op, not an error


def test_gemini_key_precedence_and_google_fallback(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-google-key")
    assert vc.build_vision_client("gemini", "m", dry_run=False)._api_key == "fake-google-key"
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_KEY)
    assert vc.build_vision_client("gemini", "m", dry_run=False)._api_key == FAKE_KEY  # GEMINI wins


def test_dry_run_needs_no_api_key(monkeypatch):
    assert isinstance(vc.build_vision_client("gemini", "m", dry_run=True), vc.NullVisionClient)
    assert isinstance(vc.build_vision_client("groq", "m", dry_run=True), vc.NullVisionClient)


def test_api_key_is_never_in_client_repr_or_configuration_errors(tmp_path, monkeypatch, caplog, capsys):
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_KEY)
    client = vc.build_vision_client("gemini", "m", dry_run=False)
    assert FAKE_KEY not in repr(client) and FAKE_KEY not in str(vars(client).get("_retry_policy", ""))
    data = _valid_settings(tmp_path)
    data["rate_limit"]["max_retries"] = -1
    code, msgs, _ = _run_cli(tmp_path, monkeypatch, caplog, data)
    out = capsys.readouterr()
    assert code == 1
    assert FAKE_KEY not in "\n".join(msgs) + out.out + out.err


def test_sdk_error_text_containing_the_key_is_redacted(monkeypatch):
    from google.genai import errors

    client = vc.GeminiVisionClient(model="m", api_key=FAKE_KEY, max_image_dimension=0)
    leak = errors.ClientError(code=400, response_json={"error": {
        "code": 400, "status": "INVALID_ARGUMENT", "message": f"bad request for key={FAKE_KEY}"}})

    def _raise(*a, **kw):
        raise leak

    monkeypatch.setattr(client._client.models, "generate_content", _raise)
    with pytest.raises(RuntimeError) as exc_info:
        client._call_and_handle_errors(b"img")
    assert FAKE_KEY not in str(exc_info.value) and "***REDACTED***" in str(exc_info.value)


# --- Phase P2-9: rate_limit.groq_requests_per_minute through the real CLI ----------------------


def _p29_groq_data(tmp_path, groq_rpm="MISSING"):
    data = {**_valid_settings(tmp_path), "provider": "groq", "model": "qwen/qwen3.8-27b"}
    data["rate_limit"] = dict(data["rate_limit"])
    if groq_rpm != "MISSING":
        data["rate_limit"]["groq_requests_per_minute"] = groq_rpm
    return data


@pytest.mark.parametrize("groq_rpm", ["MISSING", None])
def test_p29_cli_live_groq_without_pacing_cap_is_a_config_error_before_env(tmp_path, monkeypatch, caplog,
                                                                           groq_rpm):
    loads = []
    monkeypatch.setattr(config, "load_provider_environment", lambda: loads.append("env"))
    monkeypatch.setattr(vc, "GroqVisionClient", lambda *a, **kw: pytest.fail("no Groq client"))
    code, msgs, ran = _run_cli(tmp_path, monkeypatch, caplog, _p29_groq_data(tmp_path, groq_rpm))
    assert code == 1 and not ran and loads == []  # stopped before .env, client or any request
    errors = [m for m in msgs if m.startswith("Configuration error: ")]
    assert len(errors) == 1
    assert "rate_limit.groq_requests_per_minute is required when provider is groq" in errors[0]
    assert "no default is assumed" in errors[0] and "No images were processed" in errors[0]


@pytest.mark.parametrize("argv", [(), ("--dry-run",)])
@pytest.mark.parametrize("bad", [0, -3, 1.5, "10", True])
def test_p29_cli_invalid_groq_cap_rejected_live_and_dry_run(tmp_path, monkeypatch, caplog, argv, bad):
    loads = []
    monkeypatch.setattr(config, "load_provider_environment", lambda: loads.append("env"))
    _patch_extraction(monkeypatch)
    code, msgs, ran = _run_cli(tmp_path, monkeypatch, caplog, _p29_groq_data(tmp_path, bad), argv)
    assert code == 1 and not ran and loads == []
    assert any("groq_requests_per_minute must be an integer >= 1" in m for m in msgs)


def test_p29_cli_dry_run_groq_needs_no_cap_and_builds_no_client(tmp_path, monkeypatch, caplog):
    loads = []
    monkeypatch.setattr(config, "load_provider_environment", lambda: loads.append("env"))
    monkeypatch.setattr(vc, "GroqVisionClient", lambda *a, **kw: pytest.fail("dry run builds no client"))
    _patch_extraction(monkeypatch)
    code, _, ran = _run_cli(tmp_path, monkeypatch, caplog, _p29_groq_data(tmp_path), ["--dry-run"])
    assert code is None and ran and loads == []


def test_p29_cli_gemini_run_does_not_require_the_groq_cap(tmp_path, monkeypatch, caplog):
    # Gemini configuration is unchanged: no groq_requests_per_minute needed (the run then stops
    # only at the missing-key check, exactly as before P2-9).
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setattr(config, "load_provider_environment", lambda: None)
    code, msgs, ran = _run_cli(tmp_path, monkeypatch, caplog, _valid_settings(tmp_path))
    assert code == 1 and not ran
    errors = [m for m in msgs if m.startswith("Configuration error: ")]
    assert "No Gemini API key found" in errors[0] and "groq_requests_per_minute" not in errors[0]
