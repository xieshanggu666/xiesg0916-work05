"""Pydantic schemas for the API."""

from pydantic import BaseModel, Field


class AnnotationCreate(BaseModel):
    label: str
    assignee: str = ""


class MaskSave(BaseModel):
    author: str
    base_version: int = Field(description="version the client started editing from")
    resolution: str | None = Field(
        default=None,
        description="required to save over conflicts: 'overwrite' (explicit, never silent)",
    )


class SubmitRequest(BaseModel):
    actor: str
    expected_version: int


class ApproveRequest(BaseModel):
    actor: str
    expected_version: int


class RejectRegionIn(BaseModel):
    polygon: list[list[float]]
    comment: str = ""


class RejectRequest(BaseModel):
    actor: str
    expected_version: int
    regions: list[RejectRegionIn]


class MigrateRequest(BaseModel):
    actor: str


class ArbitrationCreate(BaseModel):
    image_id: int
    label: str
    initiator: str = Field(description="负责人 who starts the double-blind task")
    annotator_a: str
    annotator_b: str
    arbitrator: str


class ArbitrationSubmitRequest(BaseModel):
    actor: str = Field(description="must be one of the two assigned annotators")


class AdjudicationDecisionIn(BaseModel):
    region_index: int
    pick: str = Field(description="'a' or 'b' — which side wins this region")


class AdjudicateRequest(BaseModel):
    actor: str = Field(description="must be the arbitrator")
    decisions: list[AdjudicationDecisionIn]


class ExportCreate(BaseModel):
    name: str
    annotation_ids: list[int] | None = None  # default: all approved annotations


class ConflictRegion(BaseModel):
    x: int
    y: int
    w: int
    h: int
    pixels: int


class ConflictInfo(BaseModel):
    rival_version: int
    rival_author: str
    regions: list[ConflictRegion]
    overlap_pixels: int
