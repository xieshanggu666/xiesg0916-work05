"""Export: frozen versions, mid-export failure, resumable retry, no double-run."""

import json
import os
import threading
import time

import pytest

from app.db import SessionLocal
from app.models import ExportItemStatus, ExportJobItem
from app.services import exports as exp_svc

from .conftest import create_annotation, make_mask_bytes, save_mask, upload_image


def _approved_annotation(client, image_id, label, rects):
    ann = create_annotation(client, image_id, label)
    save_mask(client, ann["id"], make_mask_bytes(rects=rects), "alice", 0)
    client.post(f"/annotations/{ann['id']}/submit", json={"actor": "alice", "expected_version": 1})
    client.post(
        f"/annotations/{ann['id']}/review/approve",
        json={"actor": "carol", "expected_version": 1},
    )
    return ann


def _three_approved(client):
    img = upload_image(client)
    return [
        _approved_annotation(client, img["id"], f"label{i}", [(i * 15, i * 10, 10, 10)])
        for i in range(3)
    ]


def _make_job(name):
    with SessionLocal() as db:
        job = exp_svc.create_export(db, name)
        items = [(i.id, i.annotation_id, i.annotation_version) for i in job.items]
        return job.id, items


def test_export_freezes_versions(client):
    anns = _three_approved(client)
    job_id, items = _make_job("freeze-test")
    frozen_version = items[0][2]

    # annotator keeps editing AFTER the export was frozen
    save_mask(client, anns[0]["id"], make_mask_bytes(rects=[(0, 0, 30, 30)]), "alice", 1)
    assert client.get(f"/annotations/{anns[0]['id']}").json()["current_version"] == 2

    job = exp_svc.run_export(job_id)
    assert job.status.value == "completed"

    with open(os.path.join(job.output_dir, "manifest.jsonl")) as f:
        lines = [json.loads(x) for x in f]
    assert len(lines) == 3
    assert all(l["version"] == frozen_version for l in lines), "export must use frozen versions"


def test_failed_export_retries_from_where_it_stopped(client):
    _three_approved(client)
    job_id, _ = _make_job("retry-test")

    def boom_on_second(item, processed):
        if processed == 1:
            raise RuntimeError("disk full (injected)")

    job = exp_svc.run_export(job_id, fault_hook=boom_on_second)
    assert job.status.value == "failed"
    assert job.done_items == 1
    assert "disk full" in job.error

    with SessionLocal() as db:
        states = [i.status for i in db.query(ExportJobItem).filter_by(job_id=job_id).all()]
    assert states.count(ExportItemStatus.DONE) == 1
    assert states.count(ExportItemStatus.FAILED) == 1
    assert states.count(ExportItemStatus.PENDING) == 1

    # retry with the fault cleared — resumes, does not restart
    job = exp_svc.retry_export(job_id)
    assert job.status.value == "completed"
    assert job.done_items == 3
    assert job.attempt == 2

    # manifest has exactly 3 lines: done items were NOT re-exported (no duplicates)
    with open(os.path.join(job.output_dir, "manifest.jsonl")) as f:
        lines = f.read().strip().split("\n")
    assert len(lines) == 3
    ids = [json.loads(l)["annotation_id"] for l in lines]
    assert len(set(ids)) == 3

    # every file referenced by the manifest exists on disk
    for l in lines:
        line = json.loads(l)
        assert os.path.exists(os.path.join(job.output_dir, line["mask"]))
        assert os.path.exists(os.path.join(job.output_dir, line["image"]))


def test_concurrent_export_runs_are_rejected(client):
    _three_approved(client)
    job_id, _ = _make_job("race-test")

    def slow_hook(item, processed):
        time.sleep(0.4)

    outcome = {}

    def runner():
        outcome["first"] = exp_svc.run_export(job_id, fault_hook=slow_hook).status

    t = threading.Thread(target=runner)
    t.start()
    time.sleep(0.1)  # first runner holds the claim by now
    with pytest.raises(ValueError, match="already running"):
        exp_svc.run_export(job_id)
    t.join()
    assert outcome["first"].value == "completed"


def test_export_only_approved_by_default(client):
    img = upload_image(client)
    _approved_annotation(client, img["id"], "ok", [(0, 0, 10, 10)])
    draft = create_annotation(client, img["id"], "not-ready")
    save_mask(client, draft["id"], make_mask_bytes(rects=[(20, 20, 5, 5)]), "alice", 0)

    job_id, items = _make_job("approved-only")
    assert len(items) == 1
    assert items[0][1] != draft["id"]
