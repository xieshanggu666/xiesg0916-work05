"""Double-blind annotation arbitration.

Flow:
1. initiate  — the manager (负责人) picks two annotators + an arbitrator; the
   system creates one isolated draft annotation per side, each with a random
   access token. The tokens are returned ONCE in the creation response (for
   the manager to distribute) and never exposed by the API again.
2. isolation — while the arbitration is OPEN, every access to a side's mask
   (read, save, submit) requires its token. A name is never a credential:
   knowing the other side's assignee name proves nothing.
3. submit_side — a side freezes its mask. When the second side submits, the
   XOR of the two masks is computed and connected components become the
   difference regions; status moves to ARBITRATING and isolation lifts.
4. adjudicate — the arbitrator picks side A or B for every difference region.
   The merged mask (agreed pixels + per-region picks) is written as the
   official annotation's new version (source="arbitration") and goes straight
   to IN_REVIEW. Every pick is stored as an ArbitrationDecision — kept forever,
   even if the arbitration is later voided.
5. image replacement — OPEN/ARBITRATING arbitrations on the image become VOID
   (see images.replace_image); COMPLETED ones are unaffected and their result
   annotation follows the ordinary stale/migrate/invalidate rules.
"""

import os
import secrets

import cv2
import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import MASK_DIR
from ..models import (
    Annotation,
    AnnotationStatus,
    AnnotationVersion,
    Arbitration,
    ArbitrationDecision,
    ArbitrationStatus,
    Image,
    ReviewEvent,
    utcnow,
)
from . import masks
from .annotations import _mask_path, load_version_mask


class ArbitrationConflict(Exception):
    """State moved on (already submitted / adjudicated / voided) — 409."""


class ArbitrationPermission(Exception):
    """Actor is not the assignee/arbitrator required for this action — 403."""


def _locked_arbitration(db: Session, arbitration_id: int) -> Arbitration:
    arb = db.execute(
        select(Arbitration).where(Arbitration.id == arbitration_id).with_for_update()
    ).scalar_one_or_none()
    if arb is None:
        raise KeyError(f"arbitration {arbitration_id} not found")
    return arb


def get_arbitration(db: Session, arbitration_id: int) -> Arbitration:
    arb = db.get(Arbitration, arbitration_id)
    if arb is None:
        raise KeyError(f"arbitration {arbitration_id} not found")
    return arb


def _token_ok(provided: str | None, expected: str | None) -> bool:
    """Constant-time token check; a missing/legacy token never matches."""
    if not provided or not expected:
        return False
    return secrets.compare_digest(provided, expected)


def check_blind_read(ann: Annotation, token: str = ""):
    """While the arbitration is OPEN, a blind annotation is readable only with
    its side's access token. Once both sides are in (ARBITRATING/COMPLETED/
    VOID) the isolation is lifted and the records become public."""
    if ann.arbitration_id is None:
        return
    arb = ann.arbitration
    if arb is not None and arb.status == ArbitrationStatus.OPEN:
        if not _token_ok(token, ann.blind_token):
            raise ArbitrationPermission(
                "double-blind isolation: this annotation requires its side's "
                "access token until both sides submit"
            )


def _new_blind_annotation(
    db: Session, image: Image, label: str, assignee: str, arb_id: int, side: str
) -> Annotation:
    ann = Annotation(
        image_id=image.id,
        image_revision=image.current_revision,
        label=label,
        assignee=assignee,
        status=AnnotationStatus.DRAFT,
        current_version=0,
        arbitration_id=arb_id,
        arbitration_side=side,
        blind_token=secrets.token_urlsafe(32),
    )
    db.add(ann)
    db.flush()

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
    return ann


def initiate(
    db: Session,
    image_id: int,
    label: str,
    initiator: str,
    annotator_a: str,
    annotator_b: str,
    arbitrator: str,
) -> Arbitration:
    image = db.get(Image, image_id)
    if image is None:
        raise KeyError(f"image {image_id} not found")
    if not label.strip():
        raise ValueError("label is required")
    if not annotator_a or not annotator_b or not arbitrator or not initiator:
        raise ValueError("initiator, both annotators and arbitrator are required")
    if annotator_a == annotator_b:
        raise ValueError("double-blind requires two different annotators")
    if arbitrator in (annotator_a, annotator_b):
        raise ValueError("arbitrator must be a third party, not one of the annotators")

    arb = Arbitration(
        image_id=image.id,
        image_revision=image.current_revision,
        label=label.strip(),
        initiator=initiator,
        arbitrator=arbitrator,
        status=ArbitrationStatus.OPEN,
        # sides are linked right below, once their annotations exist
        ann_a_id=None,
        ann_b_id=None,
    )
    db.add(arb)
    db.flush()
    ann_a = _new_blind_annotation(db, image, arb.label, annotator_a, arb.id, "a")
    ann_b = _new_blind_annotation(db, image, arb.label, annotator_b, arb.id, "b")
    arb.ann_a_id = ann_a.id
    arb.ann_b_id = ann_b.id
    db.commit()
    db.refresh(arb)
    return arb


def _diff_regions(mask_a: np.ndarray, mask_b: np.ndarray):
    """Connected components of the XOR. Returns (regions, labels) where
    labels[y, x] == i + 1 marks pixels of regions[i]."""
    diff = masks.changed_pixels(mask_a, mask_b)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(
        diff.astype(np.uint8), connectivity=8
    )
    regions = []
    for i in range(1, n):
        x, y, w, h, area = (int(v) for v in stats[i])
        regions.append({"index": i - 1, "x": x, "y": y, "w": w, "h": h, "pixels": area})
    return regions, labels


def submit_side(db: Session, arbitration_id: int, actor: str, token: str = "") -> Arbitration:
    arb = _locked_arbitration(db, arbitration_id)
    if arb.status != ArbitrationStatus.OPEN:
        raise ArbitrationConflict(f"cannot submit to a {arb.status.value} arbitration")

    side_ann = None
    for ann in (arb.ann_a, arb.ann_b):
        if ann.assignee == actor:
            side_ann = ann
            break
    if side_ann is None:
        raise ArbitrationPermission(
            f"{actor!r} is not an annotator of arbitration {arbitration_id}"
        )
    # the name only selects the side; the token proves the caller may act for it
    if not _token_ok(token, side_ann.blind_token):
        raise ArbitrationPermission(
            "submitting a side requires that side's access token"
        )
    if side_ann.arbitration_submitted:
        raise ArbitrationConflict(f"side {side_ann.arbitration_side} already submitted")

    side_ann.arbitration_submitted = True
    side_ann.arbitration_submitted_version = side_ann.current_version

    if arb.ann_a.arbitration_submitted and arb.ann_b.arbitration_submitted:
        # both in — freeze the comparison against the submitted versions
        mask_a = load_version_mask(db, arb.ann_a_id, arb.ann_a.arbitration_submitted_version)
        mask_b = load_version_mask(db, arb.ann_b_id, arb.ann_b.arbitration_submitted_version)
        regions, _ = _diff_regions(mask_a, mask_b)
        arb.diff_regions = regions
        arb.diff_pixels = sum(r["pixels"] for r in regions)
        arb.status = ArbitrationStatus.ARBITRATING

    db.commit()
    db.refresh(arb)
    return arb


def adjudicate(
    db: Session, arbitration_id: int, actor: str, decisions: list
) -> Arbitration:
    """decisions: [{region_index, pick}] — must cover every diff region once."""
    arb = _locked_arbitration(db, arbitration_id)
    if arb.status != ArbitrationStatus.ARBITRATING:
        raise ArbitrationConflict(f"cannot adjudicate a {arb.status.value} arbitration")
    if actor != arb.arbitrator:
        raise ArbitrationPermission(
            f"only the arbitrator ({arb.arbitrator}) may adjudicate"
        )

    regions = arb.diff_regions or []
    picks: dict[int, str] = {}
    for d in decisions:
        idx, pick = d.region_index, d.pick
        if pick not in ("a", "b"):
            raise ValueError(f"pick must be 'a' or 'b', got {pick!r}")
        if idx in picks:
            raise ValueError(f"region {idx} decided twice")
        picks[idx] = pick
    if set(picks) != set(range(len(regions))):
        raise ValueError(
            f"decisions must cover every difference region exactly once "
            f"(need {len(regions)}, got {len(picks)})"
        )

    mask_a = load_version_mask(db, arb.ann_a_id, arb.ann_a.arbitration_submitted_version)
    mask_b = load_version_mask(db, arb.ann_b_id, arb.ann_b.arbitration_submitted_version)
    recomputed, labels = _diff_regions(mask_a, mask_b)
    if len(recomputed) != len(regions):
        raise ArbitrationConflict(
            "submitted masks changed after the diff was computed; cannot adjudicate"
        )

    # merge: agreed pixels + the picked side's pixels inside each diff region
    merged = cv2.bitwise_and(mask_a, mask_b)
    for i, region in enumerate(regions):
        src = mask_a if picks[i] == "a" else mask_b
        component = labels == i + 1
        merged[component] = src[component]

    # official annotation: v0 empty base + v1 merged, straight into review
    image = db.get(Image, arb.image_id)
    result = Annotation(
        image_id=arb.image_id,
        image_revision=arb.image_revision,
        label=arb.label,
        assignee="",
        status=AnnotationStatus.IN_REVIEW,
        current_version=0,
    )
    db.add(result)
    db.flush()

    empty = masks.empty_mask(image.height, image.width)
    data = masks.encode_mask(empty)
    path = _mask_path(result.id, 0)
    with open(path, "wb") as f:
        f.write(data)
    db.add(
        AnnotationVersion(
            annotation_id=result.id,
            version=0,
            image_revision=arb.image_revision,
            mask_path=path,
            mask_hash=masks.sha256(data),
            author="system",
            parent_version=None,
            source="draw",
        )
    )

    data = masks.encode_mask(merged)
    path = _mask_path(result.id, 1)
    with open(path, "wb") as f:
        f.write(data)
    db.add(
        AnnotationVersion(
            annotation_id=result.id,
            version=1,
            image_revision=arb.image_revision,
            mask_path=path,
            mask_hash=masks.sha256(data),
            author=actor,
            parent_version=0,
            source="arbitration",
        )
    )
    result.current_version = 1
    db.add(
        ReviewEvent(annotation_id=result.id, version=1, action="submit", actor=actor)
    )

    for i, region in enumerate(regions):
        pick = picks[i]
        db.add(
            ArbitrationDecision(
                arbitration_id=arb.id,
                region_index=i,
                region=region,
                pick=pick,
                picked_author=(arb.ann_a if pick == "a" else arb.ann_b).assignee,
                actor=actor,
            )
        )

    arb.status = ArbitrationStatus.COMPLETED
    arb.result_annotation_id = result.id
    arb.completed_at = utcnow()
    db.commit()
    db.refresh(arb)
    return arb


def void_active_for_image(db: Session, image_id: int) -> list[Arbitration]:
    """Image was replaced: every incomplete arbitration on it becomes VOID.
    Completed arbitrations (and their decision records) are untouched."""
    active = (
        db.execute(
            select(Arbitration).where(
                Arbitration.image_id == image_id,
                Arbitration.status.in_(
                    [ArbitrationStatus.OPEN, ArbitrationStatus.ARBITRATING]
                ),
            )
        )
        .scalars()
        .all()
    )
    for arb in active:
        arb.status = ArbitrationStatus.VOID
        arb.voided_at = utcnow()
    return active


def check_side_mask_access(arb: Arbitration, side: str, token: str = "") -> Annotation:
    """Return the side's annotation if the caller may see its mask right now."""
    if side not in ("a", "b"):
        raise ValueError("side must be 'a' or 'b'")
    ann = arb.ann_a if side == "a" else arb.ann_b
    if arb.status == ArbitrationStatus.OPEN and not _token_ok(token, ann.blind_token):
        raise ArbitrationPermission(
            "double-blind isolation: masks require their side's access token "
            "until both sides submit"
        )
    return ann


def serialize(arb: Arbitration, reveal_tokens: bool = False) -> dict:
    """Tokens are included only when reveal_tokens=True — used solely by the
    creation response, so the manager can distribute them. Every other
    endpoint must use the default."""

    def side(ann: Annotation):
        s = {
            "annotation_id": ann.id,
            "assignee": ann.assignee,
            "submitted": ann.arbitration_submitted,
            "submitted_version": ann.arbitration_submitted_version,
        }
        if reveal_tokens:
            s["token"] = ann.blind_token
        return s

    return {
        "id": arb.id,
        "image_id": arb.image_id,
        "image_revision": arb.image_revision,
        "label": arb.label,
        "status": arb.status.value,
        "initiator": arb.initiator,
        "arbitrator": arb.arbitrator,
        "side_a": side(arb.ann_a),
        "side_b": side(arb.ann_b),
        "diff_regions": arb.diff_regions,
        "diff_pixels": arb.diff_pixels,
        "result_annotation_id": arb.result_annotation_id,
        "decisions": [
            {
                "region_index": d.region_index,
                "region": d.region,
                "pick": d.pick,
                "picked_author": d.picked_author,
                "actor": d.actor,
                "created_at": d.created_at.isoformat(),
            }
            for d in sorted(arb.decisions, key=lambda d: d.region_index)
        ],
        "created_at": arb.created_at.isoformat(),
        "completed_at": arb.completed_at.isoformat() if arb.completed_at else None,
        "voided_at": arb.voided_at.isoformat() if arb.voided_at else None,
    }
