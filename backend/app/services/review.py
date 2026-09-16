"""Review workflow: submit / approve / reject-regions, with optimistic checks.

Every review action carries expected_version. Two reviewers acting on the same
annotation concurrently: the row lock serializes them, the second sees a version
or status mismatch and gets 409 — no double-approve, no lost rejection.
"""

import os

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import MASK_DIR
from ..models import (
    Annotation,
    AnnotationStatus,
    RejectionRegion,
    RegionStatus,
    ReviewEvent,
)
from ..schemas import RejectRegionIn
from . import masks
from .annotations import load_version_mask


class ReviewConflict(Exception):
    """Concurrent review action lost the race."""


def _locked_annotation(db: Session, annotation_id: int) -> Annotation:
    ann = db.execute(
        select(Annotation).where(Annotation.id == annotation_id).with_for_update()
    ).scalar_one_or_none()
    if ann is None:
        raise KeyError(f"annotation {annotation_id} not found")
    if ann.arbitration_id is not None:
        raise ReviewConflict(
            "double-blind annotation is managed by its arbitration; "
            "use the arbitration submit/adjudicate flow instead"
        )
    return ann


def _check_expected(ann: Annotation, expected_version: int):
    if ann.current_version != expected_version:
        raise ReviewConflict(
            f"annotation moved to version {ann.current_version}, expected {expected_version}; "
            "someone else acted first — refresh and retry"
        )


def submit(db: Session, annotation_id: int, actor: str, expected_version: int) -> Annotation:
    ann = _locked_annotation(db, annotation_id)
    _check_expected(ann, expected_version)
    if ann.status not in (AnnotationStatus.DRAFT, AnnotationStatus.CHANGES_REQUESTED):
        raise ReviewConflict(f"cannot submit from status {ann.status.value}")

    # Regions the reviewer kicked back are resolved only if the annotator
    # actually touched those pixels since the rejected version.
    for region in ann.rejection_regions:
        if region.status != RegionStatus.OPEN:
            continue
        rejected_mask = load_version_mask(db, ann.id, region.rejected_version)
        current_mask = load_version_mask(db, ann.id, ann.current_version)
        with open(region.mask_path, "rb") as f:
            region_mask = masks.decode_mask(f.read())
        touched = masks.overlap(
            masks.changed_pixels(rejected_mask, current_mask), region_mask > 0
        )
        if masks.count_pixels(touched):
            region.status = RegionStatus.RESOLVED
            region.resolved_in_version = ann.current_version

    ann.status = AnnotationStatus.IN_REVIEW
    db.add(
        ReviewEvent(
            annotation_id=ann.id, version=ann.current_version, action="submit", actor=actor
        )
    )
    db.commit()
    db.refresh(ann)
    return ann


def approve(db: Session, annotation_id: int, actor: str, expected_version: int) -> Annotation:
    ann = _locked_annotation(db, annotation_id)
    _check_expected(ann, expected_version)
    if ann.status != AnnotationStatus.IN_REVIEW:
        raise ReviewConflict(f"cannot approve from status {ann.status.value}")
    ann.status = AnnotationStatus.APPROVED
    db.add(
        ReviewEvent(
            annotation_id=ann.id, version=ann.current_version, action="approve", actor=actor
        )
    )
    db.commit()
    db.refresh(ann)
    return ann


def reject(
    db: Session,
    annotation_id: int,
    actor: str,
    expected_version: int,
    regions: list[RejectRegionIn],
) -> Annotation:
    ann = _locked_annotation(db, annotation_id)
    _check_expected(ann, expected_version)
    if ann.status != AnnotationStatus.IN_REVIEW:
        raise ReviewConflict(f"cannot reject from status {ann.status.value}")
    if not regions:
        raise ValueError("reject requires at least one region")

    event = ReviewEvent(
        annotation_id=ann.id, version=ann.current_version, action="reject", actor=actor
    )
    db.add(event)
    db.flush()

    image = ann.image
    for i, r in enumerate(regions):
        region_mask = masks.rasterize_polygon(r.polygon, image.height, image.width)
        path = os.path.join(MASK_DIR, f"reject_{event.id}_{i}.png")
        with open(path, "wb") as f:
            f.write(masks.encode_mask(region_mask))
        db.add(
            RejectionRegion(
                annotation_id=ann.id,
                review_event_id=event.id,
                rejected_version=ann.current_version,
                polygon=r.polygon,
                mask_path=path,
                status=RegionStatus.OPEN,
                comment=r.comment,
            )
        )

    ann.status = AnnotationStatus.CHANGES_REQUESTED
    db.commit()
    db.refresh(ann)
    return ann
