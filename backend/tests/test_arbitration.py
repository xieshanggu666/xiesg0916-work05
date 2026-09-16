"""Double-blind arbitration: isolation, diff, adjudication, image-replace voiding."""

import threading

import cv2
import numpy as np

from app.db import SessionLocal
from app.services import arbitration as arb_svc

from .conftest import (
    create_annotation,
    make_image_bytes,
    make_mask_bytes,
    save_mask,
    upload_image,
)

W, H = 64, 48


def _initiate(client, image_id, label="cat", a="alice", b="bob", arb="carol", init="boss"):
    r = client.post(
        "/arbitrations",
        json={
            "image_id": image_id,
            "label": label,
            "initiator": init,
            "annotator_a": a,
            "annotator_b": b,
            "arbitrator": arb,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def _submit(client, arb_id, actor, token=""):
    return client.post(
        f"/arbitrations/{arb_id}/submit", json={"actor": actor, "token": token}
    )


def _adjudicate(client, arb_id, actor, decisions):
    return client.post(
        f"/arbitrations/{arb_id}/adjudicate",
        json={"actor": actor, "decisions": decisions},
    )


def _get(client, arb_id):
    r = client.get(f"/arbitrations/{arb_id}")
    assert r.status_code == 200, r.text
    return r.json()


def _mask_pixels(client, ann_id, version):
    r = client.get(f"/annotations/{ann_id}/versions/{version}/mask.png")
    assert r.status_code == 200, r.text
    return cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_GRAYSCALE)


def _setup_arbitrating(client):
    """An arbitration where both sides drew disjoint rects and submitted."""
    img = upload_image(client, w=W, h=H)
    arb = _initiate(client, img["id"])
    ann_a = arb["side_a"]["annotation_id"]
    ann_b = arb["side_b"]["annotation_id"]
    tok_a, tok_b = arb["side_a"]["token"], arb["side_b"]["token"]
    save_mask(client, ann_a, make_mask_bytes(W, H, rects=[(5, 5, 8, 8)]), "alice", 0, token=tok_a)
    save_mask(client, ann_b, make_mask_bytes(W, H, rects=[(30, 30, 8, 8)]), "bob", 0, token=tok_b)
    assert _submit(client, arb["id"], "alice", tok_a).status_code == 200
    assert _submit(client, arb["id"], "bob", tok_b).status_code == 200
    return arb["id"], ann_a, ann_b


# ---------- full flow & merge correctness ----------


def test_full_double_blind_flow(client):
    arb_id, ann_a, ann_b = _setup_arbitrating(client)
    arb = _get(client, arb_id)
    assert arb["status"] == "arbitrating"
    assert arb["side_a"]["submitted"] and arb["side_b"]["submitted"]
    assert arb["side_a"]["submitted_version"] == 1
    # two disjoint rects -> two diff regions
    assert len(arb["diff_regions"]) == 2
    assert arb["diff_pixels"] == 2 * 8 * 8

    # pick A for the top-left region, B for the bottom-right one
    decisions = []
    for r in arb["diff_regions"]:
        pick = "a" if (r["x"], r["y"]) == (5, 5) else "b"
        decisions.append({"region_index": r["index"], "pick": pick})
    r = _adjudicate(client, arb_id, "carol", decisions)
    assert r.status_code == 200, r.text
    arb = r.json()
    assert arb["status"] == "completed"
    assert arb["result_annotation_id"]

    # merged mask: A's rect + B's rect, nothing else
    merged = _mask_pixels(client, arb["result_annotation_id"], 1)
    assert merged[7, 7] == 255 and merged[32, 32] == 255
    assert np.count_nonzero(merged) == 2 * 8 * 8

    # official version went straight into review with an audit trail
    result = client.get(f"/annotations/{arb['result_annotation_id']}").json()
    assert result["status"] == "in_review"
    assert result["arbitration"] is None  # result is an ordinary annotation
    assert result["versions"][1]["source"] == "arbitration"
    assert result["versions"][1]["author"] == "carol"

    # decision records kept, one per region
    assert len(arb["decisions"]) == 2
    by_pick = {d["pick"]: d for d in arb["decisions"]}
    assert by_pick["a"]["picked_author"] == "alice"
    assert by_pick["b"]["picked_author"] == "bob"

    # ...and the result flows through the ordinary review
    r = client.post(
        f"/annotations/{arb['result_annotation_id']}/review/approve",
        json={"actor": "dave", "expected_version": 1},
    )
    assert r.status_code == 200 and r.json()["status"] == "approved"


def test_merge_all_one_side(client):
    arb_id, _, _ = _setup_arbitrating(client)
    arb = _get(client, arb_id)
    decisions = [{"region_index": r["index"], "pick": "a"} for r in arb["diff_regions"]]
    arb = _adjudicate(client, arb_id, "carol", decisions).json()
    merged = _mask_pixels(client, arb["result_annotation_id"], 1)
    assert merged[7, 7] == 255
    assert np.count_nonzero(merged) == 8 * 8  # only A's rect survives


def test_identical_masks_zero_diff_regions(client):
    img = upload_image(client, w=W, h=H)
    arb = _initiate(client, img["id"])
    for side in ("side_a", "side_b"):
        ann_id = arb[side]["annotation_id"]
        author = arb[side]["assignee"]
        token = arb[side]["token"]
        save_mask(client, ann_id, make_mask_bytes(W, H, rects=[(10, 10, 12, 12)]), author, 0, token=token)
        assert _submit(client, arb["id"], author, token).status_code == 200
    arb = _get(client, arb["id"])
    assert arb["status"] == "arbitrating"
    assert arb["diff_regions"] == [] and arb["diff_pixels"] == 0

    arb = _adjudicate(client, arb["id"], "carol", []).json()
    assert arb["status"] == "completed"
    merged = _mask_pixels(client, arb["result_annotation_id"], 1)
    assert np.count_nonzero(merged) == 12 * 12


# ---------- isolation ----------


def test_isolation_requires_tokens_not_names(client):
    """The reported bug: a forged name must NOT unlock the other side's mask.
    Only the per-side token issued at initiation does."""
    img = upload_image(client, w=W, h=H)
    arb = _initiate(client, img["id"])
    ann_a = arb["side_a"]["annotation_id"]
    tok_a, tok_b = arb["side_a"]["token"], arb["side_b"]["token"]

    # no token at all -> denied, even when claiming to be alice
    assert client.get(f"/annotations/{ann_a}").status_code == 403
    assert client.get(f"/annotations/{ann_a}?viewer=alice").status_code == 403
    assert client.get(f"/annotations/{ann_a}?token=alice").status_code == 403
    assert client.get(f"/annotations/{ann_a}/versions/0/mask.png").status_code == 403
    assert client.get(f"/arbitrations/{arb['id']}/mask?side=a").status_code == 403

    # the OTHER side's token -> denied
    assert client.get(f"/annotations/{ann_a}?token={tok_b}").status_code == 403
    assert client.get(f"/arbitrations/{arb['id']}/mask?side=a&token={tok_b}").status_code == 403

    # the side's own token -> allowed
    assert client.get(f"/annotations/{ann_a}?token={tok_a}").status_code == 200
    assert client.get(f"/annotations/{ann_a}/versions/0/mask.png?token={tok_a}").status_code == 200
    assert client.get(f"/arbitrations/{arb['id']}/mask?side=a&token={tok_a}").status_code == 200

    # ordinary annotations are unaffected
    other = create_annotation(client, img["id"], "dog")
    assert client.get(f"/annotations/{other['id']}").status_code == 200

    # after both sides submit, isolation lifts and masks become public
    save_mask(client, ann_a, make_mask_bytes(W, H, rects=[(5, 5, 8, 8)]), "alice", 0, token=tok_a)
    save_mask(client, arb["side_b"]["annotation_id"], make_mask_bytes(W, H), "bob", 0, token=tok_b)
    _submit(client, arb["id"], "alice", tok_a)
    _submit(client, arb["id"], "bob", tok_b)
    assert client.get(f"/annotations/{ann_a}").status_code == 200
    assert client.get(f"/arbitrations/{arb['id']}/mask?side=a").status_code == 200


def test_tokens_are_not_exposed_after_creation(client):
    img = upload_image(client, w=W, h=H)
    arb = _initiate(client, img["id"])
    assert arb["side_a"]["token"] and arb["side_b"]["token"]
    assert arb["side_a"]["token"] != arb["side_b"]["token"]

    for r in (client.get(f"/arbitrations/{arb['id']}"), client.get("/arbitrations")):
        assert r.status_code == 200
        assert "token" not in r.text, "tokens must never appear in later responses"


def test_blind_annotation_save_restrictions(client):
    img = upload_image(client, w=W, h=H)
    arb = _initiate(client, img["id"])
    ann_a = arb["side_a"]["annotation_id"]
    tok_a, tok_b = arb["side_a"]["token"], arb["side_b"]["token"]

    # the right name alone is not enough
    r = save_mask(client, ann_a, make_mask_bytes(W, H, rects=[(1, 1, 3, 3)]), "alice", 0)
    assert r.status_code == 403
    # the other side's token is not enough either
    r = save_mask(client, ann_a, make_mask_bytes(W, H, rects=[(1, 1, 3, 3)]), "alice", 0, token=tok_b)
    assert r.status_code == 403
    # token + matching author works
    r = save_mask(client, ann_a, make_mask_bytes(W, H, rects=[(1, 1, 3, 3)]), "alice", 0, token=tok_a)
    assert r.status_code == 200
    # token but misattributed author is rejected
    r = save_mask(client, ann_a, make_mask_bytes(W, H, rects=[(2, 2, 3, 3)]), "mallory", 1, token=tok_a)
    assert r.status_code == 403

    # the ordinary review flow is closed for blind annotations
    r = client.post(f"/annotations/{ann_a}/submit", json={"actor": "alice", "expected_version": 1})
    assert r.status_code == 409
    r = client.post(
        f"/annotations/{ann_a}/review/approve", json={"actor": "carol", "expected_version": 1}
    )
    assert r.status_code == 409

    # after submitting, the side is frozen even for the token holder
    assert _submit(client, arb["id"], "alice", tok_a).status_code == 200
    r = save_mask(client, ann_a, make_mask_bytes(W, H, rects=[(2, 2, 3, 3)]), "alice", 1, token=tok_a)
    assert r.status_code == 403
    assert _submit(client, arb["id"], "alice", tok_a).status_code == 409  # no double submit


def test_submit_requires_side_token(client):
    img = upload_image(client, w=W, h=H)
    arb = _initiate(client, img["id"])
    tok_a, tok_b = arb["side_a"]["token"], arb["side_b"]["token"]

    assert _submit(client, arb["id"], "mallory").status_code == 403  # not an annotator
    assert _submit(client, arb["id"], "carol").status_code == 403  # arbitrator != annotator
    # a forged name without the token cannot freeze the other side's work
    assert _submit(client, arb["id"], "alice").status_code == 403
    assert _submit(client, arb["id"], "alice", tok_b).status_code == 403
    assert _submit(client, arb["id"], "alice", tok_a).status_code == 200
    arb = _get(client, arb["id"])
    assert arb["side_a"]["submitted"] and not arb["side_b"]["submitted"]


def test_open_blind_annotation_cannot_be_exported(client):
    img = upload_image(client, w=W, h=H)
    arb = _initiate(client, img["id"])
    ann_a = arb["side_a"]["annotation_id"]
    r = client.post("/exports", json={"name": "leak", "annotation_ids": [ann_a]})
    assert r.status_code == 400
    assert "isolation" in r.json()["detail"]


# ---------- adjudication rules ----------


def test_adjudicate_requires_arbitrator_and_full_coverage(client):
    arb_id, _, _ = _setup_arbitrating(client)
    arb = _get(client, arb_id)
    n = len(arb["diff_regions"])

    # not the arbitrator
    decisions = [{"region_index": i, "pick": "a"} for i in range(n)]
    assert _adjudicate(client, arb_id, "alice", decisions).status_code == 403

    # missing a region
    assert _adjudicate(client, arb_id, "carol", decisions[:-1]).status_code == 400
    # duplicate region
    dup = decisions + [{"region_index": 0, "pick": "b"}]
    assert _adjudicate(client, arb_id, "carol", dup).status_code == 400
    # bad pick value
    bad = [{"region_index": i, "pick": "x"} for i in range(n)]
    assert _adjudicate(client, arb_id, "carol", bad).status_code == 400

    assert _get(client, arb_id)["status"] == "arbitrating"  # nothing stuck


def test_cannot_adjudicate_before_both_submit(client):
    img = upload_image(client, w=W, h=H)
    arb = _initiate(client, img["id"])
    assert _adjudicate(client, arb["id"], "carol", []).status_code == 409
    _submit(client, arb["id"], "alice", arb["side_a"]["token"])
    assert _adjudicate(client, arb["id"], "carol", []).status_code == 409


def test_concurrent_adjudicate_exactly_one_wins(client):
    arb_id, _, _ = _setup_arbitrating(client)
    n = len(_get(client, arb_id)["diff_regions"])
    decisions = [
        type("D", (), {"region_index": i, "pick": "a"})() for i in range(n)
    ]

    outcomes = {}

    def act(idx):
        with SessionLocal() as db:
            try:
                arb_svc.adjudicate(db, arb_id, "carol", decisions)
                outcomes[idx] = "ok"
            except arb_svc.ArbitrationConflict:
                outcomes[idx] = "conflict"

    threads = [threading.Thread(target=act, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(outcomes.values()) == ["conflict", "conflict", "ok"], outcomes
    assert _get(client, arb_id)["status"] == "completed"


def test_concurrent_side_submits_compute_diff_once(client):
    img = upload_image(client, w=W, h=H)
    arb = _initiate(client, img["id"])
    tok_a, tok_b = arb["side_a"]["token"], arb["side_b"]["token"]
    save_mask(client, arb["side_a"]["annotation_id"], make_mask_bytes(W, H, rects=[(5, 5, 8, 8)]), "alice", 0, token=tok_a)
    save_mask(client, arb["side_b"]["annotation_id"], make_mask_bytes(W, H, rects=[(30, 30, 8, 8)]), "bob", 0, token=tok_b)

    errors = []

    def act(actor, token):
        with SessionLocal() as db:
            try:
                arb_svc.submit_side(db, arb["id"], actor, token)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

    threads = [
        threading.Thread(target=act, args=("alice", tok_a)),
        threading.Thread(target=act, args=("bob", tok_b)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    arb = _get(client, arb["id"])
    assert arb["status"] == "arbitrating"
    assert len(arb["diff_regions"]) == 2


# ---------- initiation validation ----------


def test_initiate_validation(client):
    img = upload_image(client, w=W, h=H)
    base = {
        "image_id": img["id"], "label": "cat", "initiator": "boss",
        "annotator_a": "alice", "annotator_b": "bob", "arbitrator": "carol",
    }
    assert client.post("/arbitrations", json=base | {"annotator_b": "alice"}).status_code == 400
    assert client.post("/arbitrations", json=base | {"arbitrator": "alice"}).status_code == 400
    assert client.post("/arbitrations", json=base | {"label": "  "}).status_code == 400
    assert client.post("/arbitrations", json=base | {"image_id": 9999}).status_code == 404


# ---------- image replacement ----------


def _replace(client, image_id, w=W, h=H):
    return client.post(
        f"/images/{image_id}/replace",
        files={"file": ("new.png", make_image_bytes(w, h, color=(10, 90, 30)), "image/png")},
    )


def test_replace_voids_incomplete_arbitration(client):
    img = upload_image(client, w=W, h=H)
    arb_open = _initiate(client, img["id"], label="cat")
    arb_mid = _initiate(client, img["id"], label="dog")
    # second arbitration gets one side submitted before the replacement
    save_mask(
        client, arb_mid["side_a"]["annotation_id"],
        make_mask_bytes(W, H, rects=[(5, 5, 8, 8)]), "alice", 0,
        token=arb_mid["side_a"]["token"],
    )
    _submit(client, arb_mid["id"], "alice", arb_mid["side_a"]["token"])

    r = _replace(client, img["id"])
    assert r.status_code == 200
    assert sorted(r.json()["voided_arbitration_ids"]) == sorted([arb_open["id"], arb_mid["id"]])

    for arb_id in (arb_open["id"], arb_mid["id"]):
        arb = _get(client, arb_id)
        assert arb["status"] == "void"
        assert arb["voided_at"]
        # the blind annotations went stale through the ordinary mechanism
        for side in ("side_a", "side_b"):
            ann = client.get(f"/annotations/{arb[side]['annotation_id']}").json()
            assert ann["status"] == "stale"

    # a voided arbitration accepts no further action
    assert _submit(client, arb_mid["id"], "bob").status_code == 409
    assert _adjudicate(client, arb_mid["id"], "carol", []).status_code == 409


def test_replace_keeps_completed_arbitration_and_records(client):
    arb_id, _, _ = _setup_arbitrating(client)
    arb = _get(client, arb_id)
    decisions = [{"region_index": r["index"], "pick": "a"} for r in arb["diff_regions"]]
    arb = _adjudicate(client, arb_id, "carol", decisions).json()
    img_id = arb["image_id"]
    result_id = arb["result_annotation_id"]

    r = _replace(client, img_id)
    assert arb_id not in r.json()["voided_arbitration_ids"]

    after = _get(client, arb_id)
    assert after["status"] == "completed"  # completed arbitrations survive
    assert len(after["decisions"]) == 2  # 裁定记录保留
    assert after["decisions"][0]["actor"] == "carol"

    # the official annotation follows the ordinary stale rules
    assert result_id in r.json()["stale_annotation_ids"]
    result = client.get(f"/annotations/{result_id}").json()
    assert result["status"] == "stale"
    r = client.post(f"/annotations/{result_id}/migrate", json={"actor": "boss"})
    assert r.status_code == 201


def test_blind_annotation_cannot_migrate_after_void(client):
    img = upload_image(client, w=W, h=H)
    arb = _initiate(client, img["id"])
    _replace(client, img["id"])
    ann_a = arb["side_a"]["annotation_id"]
    r = client.post(f"/annotations/{ann_a}/migrate", json={"actor": "alice"})
    assert r.status_code == 409
    # invalidation is the supported cleanup
    r = client.post(f"/annotations/{ann_a}/invalidate")
    assert r.status_code == 200 and r.json()["status"] == "invalidated"
