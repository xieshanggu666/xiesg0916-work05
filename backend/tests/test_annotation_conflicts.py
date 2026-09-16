"""Concurrent edits to the same region must surface conflicts — never silently overwrite."""

from app.models import Annotation, AnnotationVersion
from app.db import SessionLocal
from sqlalchemy import select

from .conftest import create_annotation, make_mask_bytes, save_mask, upload_image


def test_disjoint_edits_both_succeed(client):
    img = upload_image(client)
    ann = create_annotation(client, img["id"])

    # alice draws top-left, bob draws bottom-right, both from base v0
    r1 = save_mask(client, ann["id"], make_mask_bytes(rects=[(0, 0, 10, 10)]), "alice", 0)
    assert r1.status_code == 200, r1.text
    r2 = save_mask(client, ann["id"], make_mask_bytes(rects=[(40, 30, 10, 10)]), "bob", 0)
    assert r2.status_code == 200, r2.text
    assert r2.json()["version"] == 2


def test_overlapping_edits_conflict_and_nothing_written(client):
    img = upload_image(client)
    ann = create_annotation(client, img["id"])

    r1 = save_mask(client, ann["id"], make_mask_bytes(rects=[(10, 10, 20, 20)]), "alice", 0)
    assert r1.status_code == 200

    # bob started from the same base v0 and painted over alice's region
    r2 = save_mask(client, ann["id"], make_mask_bytes(rects=[(15, 15, 20, 20)]), "bob", 0)
    assert r2.status_code == 409
    detail = r2.json()["detail"]
    assert detail["current_version"] == 1
    assert len(detail["conflicts"]) == 1
    conflict = detail["conflicts"][0]
    assert conflict["rival_author"] == "alice"
    assert conflict["rival_version"] == 1
    assert conflict["overlap_pixels"] > 0
    assert conflict["regions"], "conflict must describe where the clash is"

    # no silent overwrite: still at v1, no v2 row exists
    with SessionLocal() as db:
        a = db.get(Annotation, ann["id"])
        assert a.current_version == 1
        versions = db.execute(
            select(AnnotationVersion).where(AnnotationVersion.annotation_id == ann["id"])
        ).scalars().all()
        assert {v.version for v in versions} == {0, 1}


def test_explicit_overwrite_resolution_succeeds(client):
    img = upload_image(client)
    ann = create_annotation(client, img["id"])
    save_mask(client, ann["id"], make_mask_bytes(rects=[(10, 10, 20, 20)]), "alice", 0)

    r = save_mask(
        client,
        ann["id"],
        make_mask_bytes(rects=[(15, 15, 20, 20)]),
        "bob",
        0,
        resolution="overwrite",
    )
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 2


def test_stale_base_without_intervening_edits_is_fine(client):
    img = upload_image(client)
    ann = create_annotation(client, img["id"])
    save_mask(client, ann["id"], make_mask_bytes(rects=[(0, 0, 5, 5)]), "alice", 0)
    # alice saves again from her own latest version — no conflict with herself
    r = save_mask(client, ann["id"], make_mask_bytes(rects=[(0, 0, 8, 8)]), "alice", 1)
    assert r.status_code == 200
