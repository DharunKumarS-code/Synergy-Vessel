from __future__ import annotations

from pathlib import Path

import yaml
from dotenv import load_dotenv

AUTOMATION_ROOT = Path(__file__).resolve().parent.parent


class ConfigError(ValueError):
    """Phase P2 remediation: settings.yaml is missing, unreadable, not valid YAML, or lacks a
    required setting. run_pipeline.main() reports it as one clean "Configuration error: ..."
    line with exit code 1, before any processing, client creation or output. Messages name the
    file and the setting, never a value from .env."""


def env_file_path() -> Path:
    return AUTOMATION_ROOT / ".env"


def load_provider_environment() -> None:
    """Load automation/.env into os.environ (existing variables win; a missing file is a no-op).
    The ONLY place this project reads .env. Call it only for an explicitly authorized live
    provider operation -- never for settings loading, validation, --quota-plan, discovery, a
    dry run, or tests."""
    load_dotenv(env_file_path())


def load_settings(settings_path: Path | None = None) -> dict:
    """Read settings.yaml. Does not touch .env (see load_provider_environment)."""
    path = settings_path or (AUTOMATION_ROOT / "config" / "settings.yaml")
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_validated_settings(settings_path: Path | None = None) -> dict:
    """load_settings() + validate_settings(), with every expected failure (missing/unreadable
    file, invalid YAML, empty or non-mapping file, missing/invalid required setting) raised as
    ConfigError. load_settings() itself is unchanged for direct callers."""
    path = settings_path or (AUTOMATION_ROOT / "config" / "settings.yaml")
    try:
        settings = load_settings(settings_path) if settings_path is not None else load_settings()
    except FileNotFoundError:
        raise ConfigError(f"settings file not found: {path}") from None
    except OSError as exc:
        raise ConfigError(f"settings file could not be read: {path} ({type(exc).__name__}: "
                          f"{exc.strerror or exc})") from None
    except yaml.YAMLError as exc:
        where = getattr(exc, "problem_mark", None)
        at = f" at line {where.line + 1}, column {where.column + 1}" if where is not None else ""
        problem = getattr(exc, "problem", None) or type(exc).__name__
        raise ConfigError(f"settings file is not valid YAML: {path}{at} ({problem})") from None
    return validate_settings(settings, path)



REQUIRED_SETTINGS = [
    (None, "provider", "str"),
    (None, "model", "str"),
    ("paths", "project_root", "str"),
    ("paths", "automation_root", "str"),
    ("paths", "state_dir", "str"),
    ("paths", "logs_dir", "str"),
    ("report", "output_filename", "str"),
    ("report", "extracted_images_dirname", "str"),
    ("report", "image_cell_max_width_px", "int"),
    ("report", "image_cell_max_height_px", "int"),
    ("processing", "heading_min_merge_width", "int"),
]


def validate_settings(settings, path: Path | str = "settings.yaml") -> dict:
    """Phase P2 remediation: check the structure and required settings up front, so a missing
    or mistyped one is a single configuration error instead of a KeyError inside every week.
    Lists every problem at once. Returns settings unchanged when valid."""
    if settings is None:
        raise ConfigError(f"settings file is empty: {path}")
    if not isinstance(settings, dict):
        raise ConfigError(f"settings file must contain a mapping of settings, got "
                          f"{type(settings).__name__}: {path}")
    problems = []
    for section in ("paths", "report", "processing"):
        if section not in settings:
            problems.append(f"missing required section '{section}'")
        elif not isinstance(settings[section], dict):
            problems.append(f"'{section}' must be a mapping, got {type(settings[section]).__name__}")
    for section, key, kind in REQUIRED_SETTINGS:
        container = settings if section is None else settings.get(section)
        if not isinstance(container, dict):
            continue  # already reported above
        name = key if section is None else f"{section}.{key}"
        if key not in container:
            problems.append(f"missing required setting '{name}'")
            continue
        value = container[key]
        if kind == "str" and not (isinstance(value, str) and value.strip()):
            problems.append(f"'{name}' must be a non-empty string, got {type(value).__name__}")
        elif kind == "int" and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
            problems.append(f"'{name}' must be an integer >= 1, got {value!r}")
    if problems:
        raise ConfigError(f"{path}: " + "; ".join(problems))
    return settings
