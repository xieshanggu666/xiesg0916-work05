"""Export state consistency: crash recovery, lease takeover, zombie runners,
idempotent reprocessing, and unexpected mid-run failures."""

import json
import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import ExportItemStatus, ExportJob, ExportJobItem, ExportStatus
from app.services import exports as exp_svc

from .test_export_retry import _approved_annotation, _make_job, _three_approved


def test_orphaned_running_job_recovered_on_startup(client):
    _three_approved(client)
    job_id, _ = _make_job("orphan")
    with SessionLocal() as db:
        exp_svc.claim_job(db, job_id)  # runner "starts", then the process dies

    recovered = exp_svc.recover_interrupted_jobs()
    assert recovered == 1
    with SessionLocal() as db:
        job = db.get(ExportJob, job_id)
        assert job.status == ExportStatus.FAILED
        assert "retry" in job.error

    job = exp_svc.run_export(job_id)  # retryable after recovery
    assert job.status == ExportStatus.COMPLETED
    assert job.done_items == 3


def test_fresh_lease_blocks_second_claim(client):
    _three_approved(client)
    job_id, _ = _make_job("fresh-lease")
    with SessionLocal() as db:
        exp_svc.claim_job(db, job_id)
    with pytest.raises(exp_svc.ClaimConflict, match="already running"):
        with SessionLocal() as db:
            exp_svc.claim_job(db, job_id)


def test_stale_lease_is_reclaimed(client):
    _three_approved(client)
    job_id, _ = _make_job("stale-lease")
    with SessionLocal() as db:
        token1 = exp_svc.claim_job(db, job_id)
    with SessionLocal() as db:  # simulate a dead runner: heartbeat far in the past
        job = db.get(ExportJob, job_id)
        job.heartbeat_at = datetime.now(timezone.utc) - timedelta(hours=1)
        db.commit()
    with SessionLocal() as db:
        token2 = exp_svc.claim_job(db, job_id)
    assert token1 != token2
    with SessionLocal() as db:
        assert db.get(ExportJob, job_id).attempt == 2


def test_zombie_runner_cannot_write_after_reclaim(client):
    _three_approved(client)
    job_id, _ = _make_job("zombie")
    with SessionLocal() as db:
        token1 = exp_svc.claim_job(db, job_id)
    with SessionLocal() as db:
        job = db.get(ExportJob, job_id)
        job.heartbeat_at = datetime.now(timezone.utc) - timedelta(hours=1)
        db.commit()
    with SessionLocal() as db:
        token2 = exp_svc.claim_job(db, job_id)

    # the zombie wakes up and tries to run with its old token
    exp_svc.run_claimed(job_id, token1)

    with SessionLocal() as db:
        job = db.get(ExportJob, job_id)
        assert job.status == ExportStatus.RUNNING, "zombie must not change status"
        assert job.claim_token == token2
        assert job.done_items == 0, "zombie must not move progress"
        items = db.execute(
            select(ExportJobItem).where(ExportJobItem.job_id == job_id)
        ).scalars().all()
        assert all(i.status == ExportItemStatus.PENDING for i in items)

    job = exp_svc.run_claimed(job_id, token2)  # the legitimate runner works fine
    assert job.status == ExportStatus.COMPLETED


def test_reprocessing_item_never_duplicates_manifest(client):
    _three_approved(client)
    job_id, _ = _make_job("idempotent")
    job = exp_svc.run_export(job_id)
    assert job.status == ExportStatus.COMPLETED

    # simulate a lost commit: files were written but the DB state rolled back
    with SessionLocal() as db:
        item = db.execute(
            select(ExportJobItem)
            .where(ExportJobItem.job_id == job_id)
            .order_by(ExportJobItem.id)
            .limit(1)
        ).scalar_one()
        item.status = ExportItemStatus.PENDING
        item.output_path = ""
        job = db.get(ExportJob, job_id)
        job.status = ExportStatus.FAILED
        job.done_items = 3  # stale cache, ahead of ground truth
        db.commit()

    job = exp_svc.run_export(job_id)
    assert job.status == ExportStatus.COMPLETED
    assert job.done_items == 3

    with open(os.path.join(job.output_dir, "manifest.jsonl")) as f:
        lines = [json.loads(x) for x in f.read().strip().split("\n")]
    assert len(lines) == 3, "reprocessing must not append duplicate manifest lines"
    assert len({l["annotation_id"] for l in lines}) == 3


def test_unexpected_runner_failure_marks_failed_and_retry_resumes(client):
    _three_approved(client)
    job_id, _ = _make_job("crash-mid-run")

    calls = {"n": 0}

    def flaky_factory():
        # claim (1) and first item (2) succeed; the next session dies
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("db connection lost")
        return SessionLocal()

    job = exp_svc.run_export(job_id, session_factory=flaky_factory)
    assert job.status == ExportStatus.FAILED, "crash must not leave the job RUNNING"
    assert "interrupted" in job.error

    with SessionLocal() as db:
        states = [
            i.status
            for i in db.execute(
                select(ExportJobItem).where(ExportJobItem.job_id == job_id)
            ).scalars().all()
        ]
    assert states.count(ExportItemStatus.DONE) == 1, "committed progress must survive"
    assert states.count(ExportItemStatus.PENDING) == 2

    job = exp_svc.run_export(job_id)  # healthy retry resumes from item 2
    assert job.status == ExportStatus.COMPLETED
    assert job.done_items == 3
    with open(os.path.join(job.output_dir, "manifest.jsonl")) as f:
        lines = f.read().strip().split("\n")
    assert len(lines) == 3, "no lost progress, no duplicate output"


def test_finalize_regenerates_lost_records(client):
    """Record files lost after items committed (or legacy jobs) must not break
    finalization — they are rebuilt from the frozen DB state."""
    _three_approved(client)
    job_id, _ = _make_job("lost-records")
    job = exp_svc.run_export(job_id)
    assert job.status == ExportStatus.COMPLETED

    import shutil

    shutil.rmtree(os.path.join(job.output_dir, "manifest.d"))
    os.remove(os.path.join(job.output_dir, "manifest.jsonl"))
    with SessionLocal() as db:  # force a re-run of the finalize pass
        j = db.get(ExportJob, job_id)
        j.status = ExportStatus.FAILED
        db.commit()

    job = exp_svc.run_export(job_id)
    assert job.status == ExportStatus.COMPLETED
    with open(os.path.join(job.output_dir, "manifest.jsonl")) as f:
        lines = [json.loads(x) for x in f.read().strip().split("\n")]
    assert len(lines) == 3
    assert len({l["annotation_id"] for l in lines}) == 3


def test_done_items_resynced_from_items_at_claim(client):
    _three_approved(client)
    job_id, _ = _make_job("resync")
    with SessionLocal() as db:
        exp_svc.claim_job(db, job_id)
    # corrupt the cached counter, then let the lease expire
    with SessionLocal() as db:
        job = db.get(ExportJob, job_id)
        job.done_items = 99
        job.heartbeat_at = datetime.now(timezone.utc) - timedelta(hours=1)
        db.commit()
    job = exp_svc.run_export(job_id)
    assert job.status == ExportStatus.COMPLETED
    assert job.done_items == 3, "counter must come from item rows, not the stale cache"
