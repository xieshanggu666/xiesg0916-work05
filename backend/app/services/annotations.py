"""Annotation versioning with pixel-level conflict detection.

Rule: a save carries the base_version the editor started from. If other authors
published versions after that base, we compute — per intervening version — the
pixels *they* changed and the pixels *we* changed relative to the common base.
Any overlap is a conflict: HTTP 409, nothing is written. The client must either
rebase onto the newest version or explicitly save with resolution='overwrite'.
"""

import os

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import MASK_DIR
from ..models import (
    Annotation,
    AnnotationStatus,
    AnnotationVersion,
    ArbitrationStatus,
)
from ..schemas import ConflictInfo
from . import masks


class ConflictError(Exception):
    def __init__(self, current_version: int, conflicts: list[ConflictInfo]):
        self.current_version = current_version
        self.conflicts = conflicts
        super().__init__("conflicting concurrent edits")


class StaleAnnotationError(Exception):
    pass


class InvalidStateError(Exception):
    pass


class BlindIsolationError(Exception):
    """A double-blind rule was violated (wrong author / frozen side)."""


def _mask_path(annotation_id: int, version: int) -> str:
    return os.path.join(MASK_DIR, f"annotation_{annotation_id}_v{version}.png")


def load_version_mask(db: Session, annotation_id: int, version: int):
    row = db.execute(
        select(AnnotationVersion).where(
            AnnotationVersion.annotation_id == annotation_id,
            AnnotationVersion.version == version,
        )
    ).scalar_one()
    with open(row.mask_path, "rb") as f:
        return masks.decode_mask(f.read())


def get_annotation(db: Session, annotation_id: int) -> Annotation:
    ann = db.get(Annotation, annotation_id)
    if ann is None:
        raise KeyError(f"annotation {annotation_id} not found")
    return ann


def create_annotation(db: Session, image_id: int, label: str, assignee: str = "") -> Annotation:
    from ..models import Image

    image = db.get(Image, image_id)
    if image is None:
        raise KeyError(f"image {image_id} not found")
    ann = Annotation(
        image_id=image_id,
        image_revision=image.current_revision,
        label=label,
        assignee=assignee,
        status=AnnotationStatus.DRAFT,
        current_version=0,
    )
    db.add(ann)
    db.flush()

    # version 0 = empty mask, so the first real save has a base to diff against
    empty = masks.empty_mask(image.height, image.width)
    data = masks.encode_mask(empty)
    path = _mask_path(ann.id, 0)
    with open(path, "wb") as f:
        f.write(data)
    db.add(
        AnnotationVersion(
            annotation_id=ann.id,
            version=0,
            image_revision=image.current_revision,
            mask_path=path,
            mask_hash=masks.sha256(data),
            author="system",
            parent_version=None,
            source="draw",
        )
    )
    ann.current_version = 0
    db.commit()
    db.refresh(ann)
    return ann


def detect_conflicts(
    db: Session, ann: Annotation, base_version: int, new_mask
) -> list[ConflictInfo]:
    """Pixel overlap between my changes and other authors' changes since base."""
    base_mask = load_version_mask(db, ann.id, base_version)
    my_diff = masks.changed_pixels(base_mask, new_mask)
    if not masks.count_pixels(my_diff):
        return []

    rows = (
        db.execute(
            select(AnnotationVersion)
            .where(
                AnnotationVersion.annotation_id == ann.id,
                AnnotationVersion.version > base_version,
            )
            .order_by(AnnotationVersion.version)
        )
        .scalars()
        .all()
    )
    conflicts: list[ConflictInfo] = []
    for row in rows:
        with open(row.mask_path, "rb") as f:
            their_mask = masks.decode_mask(f.read())
        their_diff = masks.changed_pixels(base_mask, their_mask)
        overlap = masks.overlap(my_diff, their_diff)
        n = masks.count_pixels(overlap)
        if n:
            conflicts.append(
                ConflictInfo(
                    rival_version=row.version,
                    rival_author=row.author,
                    regions=masks.region_bboxes(overlap),
                    overlap_pixels=n,
                )
            )
    return conflicts


def save_mask(
    db: Session,
    annotation_id: int,
    mask_bytes: bytes,
    author: str,
    base_version: int,
    resolution: str | None = None,
) -> AnnotationVersion:
    # Lock the annotation row so concurrent saves serialize here.
    ann = db.execute(
        select(Annotation).where(Annotation.id == annotation_id).with_for_update()
    ).scalar_one()

    if ann.status in (AnnotationStatus.STALE, AnnotationStatus.INVALIDATED):
        raise StaleAnnotationError(
            "annotation is stale/invalidated after image replacement; "
            "migrate or invalidate it explicitly first"
        )
    # Editing an approved annotation demotes it back to draft: the approved
    # content changed, so it must go through review again.

    if ann.arbitration_id is not None:
        # double-blind side: only its own assignee may draw, only while the
        # arbitration is open, and never again after this side submitted
        if ann.arbitration.status != ArbitrationStatus.OPEN:
            raise BlindIsolationError(
                "arbitration is no longer open; this blind annotation is frozen"
            )
        if ann.arbitration_submitted:
            raise BlindIsolationError(
                "this side already submitted; the mask is frozen for adjudication"
            )
        if author != ann.assignee:
            raise BlindIsolationError(
                "double-blind isolation: only the assigned annotator "
                f"({ann.assignee}) may edit this annotation"
            )

    new_mask = masks.decode_mask(mask_bytes)
    image = ann.image
    if new_mask.shape != (image.height, image.width):
        raise InvalidStateError(
            f"mask shape {new_mask.shape} does not match image {(image.height, image.width)}"
        )

    if base_version < 0 or base_version > ann.current_version:
        raise InvalidStateError(
            f"base_version {base_version} out of range (current {ann.current_version})"
        )

    if base_version < ann.current_version:
        conflicts = detect_conflicts(db, ann, base_version, new_mask)
        if conflicts and resolution != "overwrite":
            db.rollback()  # nothing written — never silently overwrite
            raise ConflictError(ann.current_version, conflicts)

    version = ann.current_version + 1
    data = masks.encode_mask(new_mask)
    path = _mask_path(ann.id, version)
    with open(path, "wb") as f:
        f.write(data)

    row = AnnotationVersion(
        annotation_id=ann.id,
        version=version,
        image_revision=ann.image_revision,
        mask_path=path,
        mask_hash=masks.sha256(data),
        author=author,
        parent_version=ann.current_version,
        source="draw",
    )
    ann.current_version = version
    if ann.status == AnnotationStatus.APPROVED:
        ann.status = AnnotationStatus.DRAFT
    db.add(row)
    db.commit()
    db.refresh(row)
    return row
