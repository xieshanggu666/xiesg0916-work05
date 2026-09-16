"""Review workflow: region rejection, resolution tracking, concurrent reviewers."""

import threading

from app.db import SessionLocal
from app.services import review as rev_svc

from .conftest import create_annotation, make_mask_bytes, save_mask, upload_image


def _submit(client, ann_id, actor, version):
    return client.post(
        f"/annotations/{ann_id}/submit", json={"actor": actor, "expected_version": version}
    )


def _approve(client, ann_id, actor, version):
    return client.post(
        f"/annotations/{ann_id}/review/approve",
        json={"actor": actor, "expected_version": version},
    )


def _reject(client, ann_id, actor, version, regions):
    return client.post(
        f"/annotations/{ann_id}/review/reject",
        json={"actor": actor, "expected_version": version, "regions": regions},
    )


def test_full_review_cycle_with_region_rejection(client):
    img = upload_image(client)
    ann = create_annotation(client, img["id"])
    save_mask(client, ann["id"], make_mask_bytes(rects=[(10, 10, 20, 20)]), "alice", 0)

    r = _submit(client, ann["id"], "alice", 1)
    assert r.status_code == 200 and r.json()["status"] == "in_review"

    # reviewer kicks back the top-left corner of the mask
    r = _reject(
        client,
        ann["id"],
        "carol",
        1,
        [{"polygon": [[10, 10], [20, 10], [20, 20], [10, 20]], "comment": "edge is rough"}],
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "changes_requested"
    assert len(body["open_regions"]) == 1
    assert body["open_regions"][0]["comment"] == "edge is rough"

    # alice fixes exactly that region (shrinks the mask there) and resubmits
    r = save_mask(client, ann["id"], make_mask_bytes(rects=[(20, 10, 10, 20)]), "alice", 1)
    assert r.status_code == 200
    r = _submit(client, ann["id"], "alice", 2)
    assert r.status_code == 200
    # the region was touched -> resolved
    assert r.json()["open_regions"] == []

    r = _approve(client, ann["id"], "carol", 2)
    assert r.status_code == 200 and r.json()["status"] == "approved"


def test_untouched_region_stays_open_on_resubmit(client):
    img = upload_image(client)
    ann = create_annotation(client, img["id"])
    save_mask(client, ann["id"], make_mask_bytes(rects=[(10, 10, 20, 20)]), "alice", 0)
    _submit(client, ann["id"], "alice", 1)
    _reject(
        client,
        ann["id"],
        "carol",
        1,
        [{"polygon": [[10, 10], [20, 10], [20, 20], [10, 20]]}],
    )

    # alice edits a *different* area only — the rejected region is untouched
    save_mask(client, ann["id"], make_mask_bytes(rects=[(10, 10, 20, 20), (40, 35, 8, 8)]), "alice", 1)
    r = _submit(client, ann["id"], "alice", 2)
    assert len(r.json()["open_regions"]) == 1, "untouched rejected region must stay open"


def test_concurrent_approve_exactly_one_wins(client):
    img = upload_image(client)
    ann = create_annotation(client, img["id"])
    save_mask(client, ann["id"], make_mask_bytes(rects=[(10, 10, 20, 20)]), "alice", 0)
    _submit(client, ann["id"], "alice", 1)

    results = {}

    def do_approve(name):
        with SessionLocal() as db:
            try:
                rev_svc.approve(db, ann["id"], name, expected_version=1)
                results[name] = "ok"
            except rev_svc.ReviewConflict as e:
                results[name] = f"conflict: {e}"

    threads = [threading.Thread(target=do_approve, args=(f"reviewer{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    oks = [k for k, v in results.items() if v == "ok"]
    assert len(oks) == 1, f"exactly one reviewer may win, got {results}"
    assert all(v.startswith("conflict") for k, v in results.items() if k not in oks)

    r = client.get(f"/annotations/{ann['id']}")
    assert r.json()["status"] == "approved"


def test_concurrent_approve_vs_reject_exactly_one_wins(client):
    img = upload_image(client)
    ann = create_annotation(client, img["id"])
    save_mask(client, ann["id"], make_mask_bytes(rects=[(10, 10, 20, 20)]), "alice", 0)
    _submit(client, ann["id"], "alice", 1)

    outcomes = {}

    def approve():
        with SessionLocal() as db:
            try:
                rev_svc.approve(db, ann["id"], "carol", 1)
                outcomes["approve"] = "ok"
            except rev_svc.ReviewConflict:
                outcomes["approve"] = "conflict"

    def reject():
        with SessionLocal() as db:
            try:
                rev_svc.reject(
                    db, ann["id"], "dave", 1,
                    regions=[type("R", (), {"polygon": [[0, 0], [5, 0], [5, 5]], "comment": ""})()],
                )
                outcomes["reject"] = "ok"
            except rev_svc.ReviewConflict:
                outcomes["reject"] = "conflict"

    t1, t2 = threading.Thread(target=approve), threading.Thread(target=reject)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert sorted(outcomes.values()) == ["conflict", "ok"], outcomes
    final = client.get(f"/annotations/{ann['id']}").json()["status"]
    assert final in ("approved", "changes_requested")


def test_stale_expected_version_rejected(client):
    img = upload_image(client)
    ann = create_annotation(client, img["id"])
    save_mask(client, ann["id"], make_mask_bytes(rects=[(10, 10, 20, 20)]), "alice", 0)
    _submit(client, ann["id"], "alice", 1)
    # reviewer looks at v1, meanwhile alice's teammate saves v2 — approve must fail
    save_mask(client, ann["id"], make_mask_bytes(rects=[(10, 10, 25, 25)]), "alice", 1)
    r = _approve(client, ann["id"], "carol", 1)
    assert r.status_code == 409
