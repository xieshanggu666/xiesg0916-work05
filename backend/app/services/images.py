"""Image upload / replacement and the explicit stale-annotation workflow.

Replacing an image never silently keeps or drops annotations: every annotation
on the old revision becomes STALE and must be explicitly migrated (mask carried
onto the new revision, resized if dimensions changed) or invalidated.
"""

import os

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import IMAGE_DIR
from ..models import (
    Annotation,
    AnnotationStatus,
    AnnotationVersion,
    Image,
    ImageRevision,
)
from . import masks
from .annotations import _mask_path


def _store_image(data: bytes, image_id: int, revision: int) -> str:
    path = os.path.join(IMAGE_DIR, f"image_{image_id}_r{revision}.png")
    img = masks.decode_image(data)
    # normalize to png on disk so serving is format-stable
    import cv2

    cv2.imwrite(path, img)
    return path


def create_image(db: Session, name: str, data: bytes) -> Image:
    img = masks.decode_image(data)
    h, w = img.shape[:2]
    image = Image(name=name, current_revision=1, width=w, height=h)
    db.add(image)
    db.flush()
    path = _store_image(data, image.id, 1)
    db.add(
        ImageRevision(
            image_id=image.id,
            revision=1,
            storage_path=path,
            content_hash=masks.sha256(data),
            width=w,
            height=h,
        )
    )
    db.commit()
    db.refresh(image)
    return image


def replace_image(db: Session, image_id: int, data: bytes) -> tuple[Image, list[Annotation]]:
    """Replace the raw image. Returns the image and the annotations that went stale."""
    image = db.execute(
        select(Image).where(Image.id == image_id).with_for_update()
    ).scalar_one_or_none()
    if image is None:
        raise KeyError(f"image {image_id} not found")

    img = masks.decode_image(data)
    h, w = img.shape[:2]
    revision = image.current_revision + 1
    path = _store_image(data, image.id, revision)
    db.add(
        ImageRevision(
            image_id=image.id,
            revision=revision,
            storage_path=path,
            content_hash=masks.sha256(data),
            width=w,
            height=h,
        )
    )
    image.current_revision = revision
    image.width = w
    image.height = h

    stale = (
        db.execute(
            select(Annotation).where(
                Annotation.image_id == image_id,
                Annotation.image_revision < revision,
                Annotation.status.notin_([AnnotationStatus.INVALIDATED]),
            )
        )
        .scalars()
        .all()
    )
    for ann in stale:
        ann.status = AnnotationStatus.STALE

    db.commit()
    db.refresh(image)
    return image, stale


def migrate_annotation(db: Session, annotation_id: int, actor: str) -> AnnotationVersion:
    """Explicitly carry a stale annotation onto the current image revision."""
    ann = db.execute(
        select(Annotation).where(Annotation.id == annotation_id).with_for_update()
    ).scalar_one_or_none()
    if ann is None:
        raise KeyError(f"annotation {annotation_id} not found")
    if ann.status != AnnotationStatus.STALE:
        raise ValueError(f"annotation is {ann.status.value}, not stale")

    image = ann.image
    latest = db.execute(
        select(AnnotationVersion)
        .where(AnnotationVersion.annotation_id == ann.id)
        .order_by(AnnotationVersion.version.desc())
    ).scalars().first()
    with open(latest.mask_path, "rb") as f:
        mask = masks.decode_mask(f.read())
    if mask.shape != (image.height, image.width):
        mask = masks.resize_mask(mask, image.width, image.height)

    version = ann.current_version + 1
    data = masks.encode_mask(mask)
    path = _mask_path(ann.id, version)
    with open(path, "wb") as f:
        f.write(data)
    row = AnnotationVersion(
        annotation_id=ann.id,
        version=version,
        image_revision=image.current_revision,
        mask_path=path,
        mask_hash=masks.sha256(data),
        author=actor,
        parent_version=ann.current_version,
        source="migrate",
    )
    ann.current_version = version
    ann.image_revision = image.current_revision
    ann.status = AnnotationStatus.DRAFT
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def invalidate_annotation(db: Session, annotation_id: int) -> Annotation:
    ann = db.execute(
        select(Annotation).where(Annotation.id == annotation_id).with_for_update()
    ).scalar_one_or_none()
    if ann is None:
        raise KeyError(f"annotation {annotation_id} not found")
    if ann.status != AnnotationStatus.STALE:
        raise ValueError(f"annotation is {ann.status.value}, not stale")
    ann.status = AnnotationStatus.INVALIDATED
    db.commit()
    db.refresh(ann)
    return ann
