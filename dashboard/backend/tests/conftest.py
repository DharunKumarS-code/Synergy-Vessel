"""Synthetic data root mirroring the real layout (no real data is touched)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import openpyxl
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

REPORT = "Final Official vs AI Location Report.json"
IMG_DIR = "Extracted Images - New"
XLSX_A = "Nord Kudu  Deck Photos -02 AUG 2026 Part 1 (1).xlsx"
XLSX_B = "Nord Kudu  Deck Photos - SEPT 06 2026 Part 1 (1).xlsx"


def jpeg_bytes(seed: int, size=(320, 240), fmt="JPEG") -> bytes:
    """Small, real, decodable photograph substitute: random rectangles; each seed looks different."""
    import io
    import random

    from PIL import Image, ImageDraw

    rnd = random.Random(seed)
    im = Image.new("RGB", size, tuple(rnd.randrange(256) for _ in range(3)))
    d = ImageDraw.Draw(im)
    w, h = size
    for _ in range(14):
        x0, y0 = rnd.randrange(w), rnd.randrange(h)
        d.rectangle([x0, y0, x0 + rnd.randrange(20, w // 2), y0 + rnd.randrange(20, h // 2)],
                    fill=tuple(rnd.randrange(256) for _ in range(3)))
    buf = io.BytesIO()
    im.save(buf, fmt, **({"quality": 90} if fmt == "JPEG" else {}))
    return buf.getvalue()


def _img(folder: Path, name: str, payload: bytes) -> str:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def _rec(serial, fname, date, xlsx, ws, loc, ai, sha):
    return {
        "serial_number": serial, "image_filename": fname, "report_date": date,
        "original_excel_filename": xlsx, "worksheet_name": ws, "official_location": loc,
        "ai_suggested_location": ai,
        "extracted_photograph": {"embedded": True, "format": "jpeg", "sha256": sha,
                                 "size_bytes": 10, "width_px": 850, "height_px": 637},
        "activity_identified": "No activity observed", "location_confidence": "Medium" if ai else None,
    }


def _report(folder: Path, week: str, records, record_count=None):
    data = {"schema_version": 1, "report_title": "Final Official vs AI Location Report", "week": week,
            "source_workbook": "x.xlsx", "source_sha256": "0", "sheet_name": "Image Register", "columns": [],
            "record_count": len(records) if record_count is None else record_count, "records": records}
    (folder / REPORT).write_text(json.dumps(data), encoding="utf-8")


def _register(folder: Path, num: int, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Image Register"
    ws.append(["Serial Number", "Image Filename", "Report Date", "Original Excel Filename", "Worksheet Name",
               "Official Location", "Extracted Photograph", "Activity Identified", "Location Confidence"])
    for r in rows:
        ws.append(list(r) + [None, None, None])
    wb.save(folder / f"Week_{num:02d}_Image_Register.xlsx")


@pytest.fixture(scope="session")
def data_root(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("synergy")
    (root / "secret.txt").write_text("outside approved folders")
    (root / "automation").mkdir()  # not a week folder: no report

    # Week 30: a photo repeated within the week (P1 at W30-0001 and W30-0004)
    a = root / "Week 30"
    imgs = a / IMG_DIR
    p1 = _img(imgs, "W30-0001.jpg", jpeg_bytes(1))
    p2 = _img(imgs, "W30-0002.jpg", jpeg_bytes(2))
    p3 = _img(imgs, "W30-0003.jpg", jpeg_bytes(3))
    _img(imgs, "W30-0004.jpg", jpeg_bytes(1))
    _report(a, "Week 30", [
        _rec(1, "2026-08-02_001.jpg", "8/2/2026", XLSX_A, "AUG 02 2026", "Not Labelled", "Main Deck", p1),
        _rec(2, "2026-08-02_002.jpg", "8/2/2026", XLSX_A, "AUG 02 2026", "Main Deck", None, p2),
        _rec(3, "2026-08-02_003.jpg", "8/2/2026", XLSX_A, "AUG 02 2026", "CARGO HOLD NO. 1", None, p3),
    ])
    _register(a, 30, [
        (1, "W30-0001.jpg", "8/2/2026", XLSX_A, "AUG 02 2026 ", None),
        (2, "W30-0002.jpg", "8/2/2026", XLSX_A, "AUG 02 2026 ", "Main Deck"),
        (3, "W30-0003.jpg", "8/2/2026", XLSX_A, "AUG 02 2026 ", "CARGO HOLD NO. 1"),
        (4, "W30-0004.jpg", "8/2/2026", XLSX_A, "AUG 02 2026 ", "PORT AND STARBOARD VIEW"),
    ])

    # week 34: pending date, a missing image, an invalid record, wrong record_count
    b = root / "week 34"
    imgs = b / IMG_DIR
    q1 = _img(imgs, "W34-0001.jpg", jpeg_bytes(2))  # same photo as W30-0002
    _report(b, "week 34", [
        _rec(1, "Timestamp_Pending_001.jpg", "AMBIGUOUS/PENDING", XLSX_B, "SEPT 06 2026", "main deck", None, q1),
        _rec(2, "Timestamp_Pending_002.jpg", "AMBIGUOUS/PENDING", XLSX_B, "SEPT 06 2026", "Main Deck", None, "f" * 64),
        "not-a-record",
    ], record_count=5)
    _register(b, 34, [(1, "W34-0001.jpg", "AMBIGUOUS/PENDING", XLSX_B, "SEPT 06 2026", "main deck")])

    # week 35: corrupt JSON
    c = root / "week 35"
    c.mkdir()
    (c / REPORT).write_text("{not json", encoding="utf-8")
    return root


@pytest.fixture(scope="session", autouse=True)
def _isolated_auth(tmp_path_factory):
    """Phase 8.2: no test may ever touch dashboard/data/auth. Settings without an explicit auth_dir resolve
    to this temporary folder; password hashing uses few iterations to keep the suite fast (tests only)."""
    import app.config as config
    from app.auth import passwords

    old_dir, old_backup, old_iter = config.DEFAULT_AUTH_DIR, config.DEFAULT_BACKUP_DIR, passwords.ITERATIONS
    config.DEFAULT_AUTH_DIR = tmp_path_factory.mktemp("auth-default")
    config.DEFAULT_BACKUP_DIR = tmp_path_factory.mktemp("backup-default")  # Phase 8.3: never the real backup folder
    passwords.ITERATIONS = 1_000
    yield
    config.DEFAULT_AUTH_DIR, config.DEFAULT_BACKUP_DIR, passwords.ITERATIONS = old_dir, old_backup, old_iter


TEST_PASSWORD = "correct-horse-battery-1"


def sign_in(client, role: str = "admin", username: str | None = None) -> dict:
    """Create an isolated test account directly in the client's user store, sign in through the API and attach
    the bearer token to the client. Returns the /login response (user + token)."""
    import uuid

    name = username or f"t-{role}-{uuid.uuid4().hex[:8]}"
    users = client.app.state.auth.users
    if users.by_username(name) is None:
        users.create(name, TEST_PASSWORD, role, f"Test {role.title()} {name[-4:]}")
    r = client.post("/api/auth/login", json={"username": name, "password": TEST_PASSWORD})
    assert r.status_code == 200, r.text
    client.headers["Authorization"] = f"Bearer {r.json()['token']}"
    return r.json()


@pytest.fixture(scope="session")
def settings(data_root) -> Settings:
    return Settings(data_root=data_root, analysis_dir=data_root / "_analyses", import_dir=data_root / "_imports")


@pytest.fixture(scope="session")
def client(settings):
    with TestClient(create_app(settings)) as c:
        yield c


# SV-02: application data needs a signed-in account. A test that uses the shared client while it carries no
# token starts as a viewer (least privilege). A token set by a test or a module fixture (sign_in) is kept, and
# tests of anonymous behaviour remove the header themselves.
_VIEWER = {}


@pytest.fixture(autouse=True)
def _shared_client_signed_in_as_viewer(request):
    if "client" not in request.fixturenames:
        yield
        return
    c = request.getfixturevalue("client")
    if "Authorization" in c.headers:
        yield
        return
    token = _VIEWER.get(id(c))
    if token is None or c.app.state.auth.authenticate(token) is None:
        users = c.app.state.auth.users
        if users.by_username("t-shared-viewer") is None:
            users.create("t-shared-viewer", TEST_PASSWORD, "viewer", "Shared Test Viewer")
        r = c.post("/api/auth/login", json={"username": "t-shared-viewer", "password": TEST_PASSWORD})
        assert r.status_code == 200, r.text
        token = _VIEWER[id(c)] = r.json()["token"]
    c.headers["Authorization"] = f"Bearer {token}"
    yield


@pytest.fixture(scope="session")
def store(client):
    return client.app.state.store
