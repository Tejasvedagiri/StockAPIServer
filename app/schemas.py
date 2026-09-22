"""Pydantic response models for the harvest endpoint."""

from pydantic import BaseModel


class FileHarvestEntry(BaseModel):
    """Per-file result: which file was processed and how many rows it loaded."""

    file: str
    rows_loaded: int


class HarvestSummary(BaseModel):
    """Top-level response of GET /harvest_csv.

    Mirrors exactly the dict built by app.harvest.harvest() — same keys,
    same nesting — so FastAPI can validate and serialize that return value
    directly against this model.
    """

    files: list[FileHarvestEntry]
    total_rows_loaded: int
    files_processed: int
