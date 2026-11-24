"""Pydantic response models. Field names mirror the source JSON keys where a
source field exists; derived fields are documented as such."""

from __future__ import annotations

import datetime as dt
from typing import Literal, Optional

from pydantic import BaseModel, Field


class Photograph(BaseModel):
    """Copied verbatim from the JSON ``extracted_photograph`` object."""

    embedded: Optional[bool] = None
    format: Optional[str] = None
    sha256: Optional[str] = None
    size_bytes: Optional[int] = None
    width_px: Optional[int] = None
    height_px: Optional[int] = None


class InspectionRecord(BaseModel):
    # identity (derived)
    record_id: str
    source: Literal["json_report", "image_register", "excel_import"]
    week: str = Field(description="Week label as written in the source (JSON 'week' or folder name)")
    week_folder: str
    week_number: Optional[int]
    vessel_id: str
    vessel_name: str

    # original fields (preserved as-is)
    serial_number: Optional[int] = None
    original_image_filename: Optional[str] = Field(
        None, description="JSON 'image_filename' (pre-2026-11-16 name; the file no longer exists)"
    )
    report_date: Optional[str] = None
    original_excel_filename: Optional[str] = None
    worksheet_name: Optional[str] = None
    official_location: Optional[str] = None
    ai_suggested_location: Optional[str] = None
    activity_identified: Optional[str] = None
    location_confidence: Optional[str] = None
    extracted_photograph: Optional[Photograph] = None

    # derived
    official_location_labelled: bool
    # Phase 9 (imported records only): the Excel text, and how the shown official_location was reached
    original_location: Optional[str] = None
    location_method: Optional[str] = None
    location_review: Optional[str] = None
    inspection_date: Optional[dt.date] = None
    date_source: Optional[Literal["report_date", "worksheet_name"]] = None
    register_image_filename: Optional[str] = None
    register_serial_number: Optional[int] = None
    image_id: Optional[str] = None
    image_url: Optional[str] = None
    image_status: Literal["resolved", "ambiguous", "missing"]
    image_match_method: Optional[str] = None
    same_photo_appearances: int = Field(
        1, description="Appearances across all weeks sharing this photo's SHA-256"
    )
    issues: list[str] = Field(default_factory=list)
    import_id: Optional[str] = Field(None, description="Phase 7: the import that added this record (excel_import only)")


class Page(BaseModel):
    total: int
    count: int
    offset: int
    limit: int
    items: list[InspectionRecord]


LocationField = Literal["official_location", "ai_suggested_location"]


class SearchItem(InspectionRecord):
    """An inspection record plus which location field(s) matched the query."""

    matched_on: list[LocationField] = Field(default_factory=list)


class SearchResult(BaseModel):
    location: str
    match: str
    include_ai_location: bool
    count: int
    items: list[SearchItem]


class Vessel(BaseModel):
    vessel_id: str
    vessel_name: str
    name_source: str
    record_count: int
    weeks: list[str]
    first_inspection_date: Optional[dt.date]
    last_inspection_date: Optional[dt.date]


class Location(BaseModel):
    location: str
    variants: list[str]
    is_placeholder: bool
    record_count: int
    date_count: int
    vessel_ids: list[str]


class DateEntry(BaseModel):
    date: dt.date
    weeks: list[str]
    record_count: int
    resolved_image_count: int


class DatesResult(BaseModel):
    location: str
    dates: list[DateEntry]
    undated_record_count: int


class ImagesResult(BaseModel):
    location: str
    date: dt.date
    count: int
    items: list[InspectionRecord]
