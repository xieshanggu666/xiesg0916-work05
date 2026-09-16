"""Image replacement: old annotations go stale and need explicit migrate/invalidate."""

from .conftest import (
    create_annotation,
    make_image_bytes,
    make_mask_bytes,
    save_mask,
    upload_image,
)


def _replace(client, image_id, w=64, h=48):
    return client.post(
        f"/images/{image_id}/replace",
        files={"file": ("new.png", make_image_bytes(w, h, color=(10, 90, 30)), "image/png")},
    )


def test_replace_makes_annotations_stale_and_blocks_saves(client):
    img = upload_image(client)
    ann = create_annotation(client, img["id"])
    save_mask(client, ann["id"], make_mask_bytes(rects=[(10, 10, 20, 20)]), "alice", 0)

    r = _replace(client, img["id"])
    assert r.status_code == 200
    body = r.json()
    assert body["image"]["current_revision"] == 2
    assert body["stale_annotation_ids"] == [ann["id"]]

    # old annotation is stale; editing it is refused until explicitly migrated
    r = save_mask(client, ann["id"], make_mask_bytes(rects=[(5, 5, 5, 5)]), "alice", 1)
    assert r.status_code == 409
    assert r.json()["detail"]["kind"] == "stale"


def test_migrate_then_edit(client):
    img = upload_image(client)
    ann = create_annotation(client, img["id"])
    save_mask(client, ann["id"], make_mask_bytes(rects=[(10, 10, 20, 20)]), "alice", 0)
    _replace(client, img["id"])

    r = client.post(f"/annotations/{ann['id']}/migrate", json={"actor": "alice"})
    assert r.status_code == 201
    assert r.json()["source"] == "migrate"

    got = client.get(f"/annotations/{ann['id']}").json()
    assert got["status"] == "draft"
    assert got["image_revision"] == 2
    assert got["current_version"] == 2

    # editable again after migration
    r = save_mask(client, ann["id"], make_mask_bytes(rects=[(10, 10, 22, 22)]), "alice", 2)
    assert r.status_code == 200


def test_migrate_resizes_mask_when_dimensions_change(client):
    img = upload_image(client, w=64, h=48)
    ann = create_annotation(client, img["id"])
    save_mask(client, ann["id"], make_mask_bytes(64, 48, rects=[(10, 10, 20, 20)]), "alice", 0)
    _replace(client, img["id"], w=128, h=96)  # doubled

    client.post(f"/annotations/{ann['id']}/migrate", json={"actor": "alice"})
    r = client.get(f"/annotations/{ann['id']}/versions/2/mask.png")
    assert r.status_code == 200
    import cv2
    import numpy as np

    arr = cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_GRAYSCALE)
    assert arr.shape == (96, 128), "migrated mask must match the new image size"
    assert arr[20, 20] == 255 and arr.sum() > 0


def test_invalidate_is_explicit_and_terminal(client):
    img = upload_image(client)
    ann = create_annotation(client, img["id"])
    save_mask(client, ann["id"], make_mask_bytes(rects=[(10, 10, 20, 20)]), "alice", 0)
    _replace(client, img["id"])

    r = client.post(f"/annotations/{ann['id']}/invalidate")
    assert r.status_code == 200
    assert r.json()["status"] == "invalidated"

    # cannot migrate or edit an invalidated annotation
    assert client.post(f"/annotations/{ann['id']}/migrate", json={"actor": "a"}).status_code == 409
    assert save_mask(client, ann["id"], make_mask_bytes(rects=[(1, 1, 2, 2)]), "a", 1).status_code == 409


def test_no_implicit_action_on_replace(client):
    """Replacing twice still leaves every annotation explicitly resolvable."""
    img = upload_image(client)
    ann = create_annotation(client, img["id"])
    save_mask(client, ann["id"], make_mask_bytes(rects=[(10, 10, 20, 20)]), "alice", 0)
    _replace(client, img["id"])
    client.post(f"/annotations/{ann['id']}/migrate", json={"actor": "alice"})
    _replace(client, img["id"])  # replaced again -> stale again
    got = client.get(f"/annotations/{ann['id']}").json()
    assert got["status"] == "stale"
    assert got["image_revision"] == 2  # still points at the revision it was drawn on
