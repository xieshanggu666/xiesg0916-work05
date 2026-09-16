"""Dataset export against frozen versions, with crash-safe idempotent retry.

Consistency rules:
1. Single owner — a job is claimed by exactly one live runner (row lock +
   claim token + heartbeat lease). A RUNNING job whose lease expired is
   reclaimed, never stuck; a zombie runner that lost its claim detects the
   token mismatch and stops without writing anything.
2. Atomic progress — every item commits together with the job's heartbeat and
   progress counter in ONE transaction, so item state and job state cannot
   drift apart. The counter is additionally resynced from ground truth (item
   rows) at every claim and at finalization.
3. Idempotent output — item files use deterministic names; each item writes a
   per-item manifest record atomically (tmp + rename). The final manifest.jsonl
   is assembled from those records at completion. Reprocessing an item
   overwrites its own record — retry never duplicates output.
4. No stuck jobs — any unexpected runner failure marks the job FAILED
   (best-effort, token-guarded) so it stays retryable; at process start,
   RUNNING jobs are orphans (runners are in-process) and are recovered to
   FAILED automatically.
"""

import json
import os
import shutil
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..config import EXPORT_DIR
from ..db import SessionLocal
from ..models import (
    Annotation,
    AnnotationStatus,
    AnnotationVersion,
    ExportItemStatus,
    ExportJob,
    ExportJobItem,
    ExportStatus,
    ImageRevision,
)

# A RUNNING job with no heartbeat for this long is considered orphaned.
LEASE = timedelta(seconds=30)


class ClaimConflict(ValueError):
    """Job is owned by a live runner, or in a non-runnable state."""


class ClaimLost(Exception):
    """This runner's claim was superseded (lease expired, someone took over)."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def create_export(
    db: Session, name: str, annotation_ids: list[int] | None = None
) -> ExportJob:
    if annotation_ids is not None:
        # IN() dedupes; order keeps item ids deterministic
        q = select(Annotation).where(Annotation.id.in_(annotation_ids))
    else:
        q = select(Annotation).where(Annotation.status == AnnotationStatus.APPROVED)
    anns = db.execute(q.order_by(Annotation.id)).scalars().all()
    if not anns:
        raise ValueError("no annotations match the export spec")

    job = ExportJob(
        name=name,
        status=ExportStatus.PENDING,
        spec={"annotation_ids": [a.id for a in anns]},
        total_items=len(anns),
    )
    db.add(job)
    db.flush()
    job.output_dir = os.path.join(EXPORT_DIR, f"job_{job.id}")
    for ann in anns:
        db.add(
            ExportJobItem(
                job_id=job.id,
                annotation_id=ann.id,
                annotation_version=ann.current_version,  # frozen here
            )
        )
    db.commit()
    db.refresh(job)
    return job


def claim_job(db: Session, job_id: int) -> str:
    """Atomically take ownership of a job; returns the runner's claim token.

    Succeeds from PENDING/FAILED, and from RUNNING when the previous runner's
    lease expired (crash takeover). Raises ClaimConflict while a live runner
    owns the job or the job is completed.
    """
    token = uuid.uuid4().hex
    job = db.execute(
        select(ExportJob).where(ExportJob.id == job_id).with_for_update()
    ).scalar_one_or_none()
    if job is None:
        raise KeyError(f"export job {job_id} not found")

    now = _utcnow()
    if job.status == ExportStatus.COMPLETED:
        raise ClaimConflict("job already completed")
    if job.status == ExportStatus.RUNNING:
        lease_fresh = job.heartbeat_at is not None and now - job.heartbeat_at <= LEASE
        if lease_fresh:
            raise ClaimConflict("job is already running")
        # stale lease: previous runner died — take over, don't leave it stuck

    job.status = ExportStatus.RUNNING
    job.attempt += 1
    job.claim_token = token
    job.heartbeat_at = now
    job.error = ""
    # Resync cached progress with ground truth: a crash between commits may
    # have left done_items ahead of/behind the actual item rows.
    job.done_items = db.execute(
        select(func.count())
        .select_from(ExportJobItem)
        .where(
            ExportJobItem.job_id == job_id,
            ExportJobItem.status == ExportItemStatus.DONE,
        )
    ).scalar_one()
    db.commit()
    return token


def _export_one_item(session: Session, item: ExportJobItem, out_dir: str) -> str:
    """Write image+mask copies and the item's manifest record. Idempotent:
    re-running overwrites the same deterministic paths."""
    ann = session.get(Annotation, item.annotation_id)
    if ann is None:
        raise RuntimeError(f"annotation {item.annotation_id} disappeared")
    version = session.execute(
        select(AnnotationVersion).where(
            AnnotationVersion.annotation_id == item.annotation_id,
            AnnotationVersion.version == item.annotation_version,
        )
    ).scalar_one()
    revision = session.execute(
        select(ImageRevision).where(
            ImageRevision.image_id == ann.image_id,
            ImageRevision.revision == version.image_revision,
        )
    ).scalar_one()

    stem = f"ann{item.annotation_id}_v{item.annotation_version}"
    img_out = os.path.join(out_dir, "images", f"{stem}.png")
    mask_out = os.path.join(out_dir, "masks", f"{stem}.png")
    os.makedirs(os.path.dirname(img_out), exist_ok=True)
    os.makedirs(os.path.dirname(mask_out), exist_ok=True)
    shutil.copyfile(revision.storage_path, img_out)
    shutil.copyfile(version.mask_path, mask_out)

    record = {
        "annotation_id": item.annotation_id,
        "version": item.annotation_version,
        "label": ann.label,
        "image": os.path.relpath(img_out, out_dir),
        "mask": os.path.relpath(mask_out, out_dir),
        "mask_sha256": version.mask_hash,
    }
    # per-item record, atomic write — reprocessing overwrites, never appends
    rec_dir = os.path.join(out_dir, "manifest.d")
    os.makedirs(rec_dir, exist_ok=True)
    rec = os.path.join(rec_dir, f"{item.id}.json")
    tmp = rec + ".tmp"
    with open(tmp, "w") as f:
        json.dump(record, f)
    os.replace(tmp, rec)
    return mask_out


def _finalize(session: Session, job: ExportJob, out_dir: str):
    """Assemble manifest.jsonl from per-item records (atomic), then complete.
    Missing records (crash between file write and commit, or pre-migration
    jobs) are regenerated idempotently from the frozen DB state."""
    items = session.execute(
        select(ExportJobItem)
        .where(
            ExportJobItem.job_id == job.id,
            ExportJobItem.status == ExportItemStatus.DONE,
        )
        .order_by(ExportJobItem.id)
    ).scalars().all()
    lines = []
    for it in items:
        rec = os.path.join(out_dir, "manifest.d", f"{it.id}.json")
        if not os.path.exists(rec):
            _export_one_item(session, it, out_dir)
        with open(rec) as f:
            lines.append(f.read().strip())
    tmp = os.path.join(out_dir, "manifest.jsonl.tmp")
    with open(tmp, "w") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))
    os.replace(tmp, os.path.join(out_dir, "manifest.jsonl"))

    job.done_items = len(items)  # ground truth, not an increment
    job.status = ExportStatus.COMPLETED
    job.heartbeat_at = _utcnow()


def run_claimed(
    job_id: int,
    token: str,
    fault_hook: Callable[[ExportJobItem, int], None] | None = None,
    session_factory=SessionLocal,
) -> ExportJob:
    """Item loop for a job already claimed with `token`. Every iteration
    re-verifies ownership before writing; item state, heartbeat and progress
    commit in one transaction."""
    try:
        processed = 0
        while True:
            with session_factory() as session:
                job = session.execute(
                    select(ExportJob).where(ExportJob.id == job_id).with_for_update()
                ).scalar_one()
                if job.claim_token != token:
                    raise ClaimLost(f"job {job_id} is owned by another runner")
                item = session.execute(
                    select(ExportJobItem)
                    .where(
                        ExportJobItem.job_id == job_id,
                        ExportJobItem.status.in_(
                            [ExportItemStatus.PENDING, ExportItemStatus.FAILED]
                        ),
                    )
                    .order_by(ExportJobItem.id)
                    .limit(1)
                ).scalars().first()

                if item is None:
                    _finalize(session, job, job.output_dir)
                    session.commit()
                    break
                try:
                    if fault_hook is not None:
                        fault_hook(item, processed)
                    item.output_path = _export_one_item(session, item, job.output_dir)
                    item.status = ExportItemStatus.DONE
                    item.error = ""
                    job.done_items += 1
                    processed += 1
                except Exception as exc:  # item-level failure: mark and stop, retry resumes
                    item.status = ExportItemStatus.FAILED
                    item.error = str(exc)
                    job.status = ExportStatus.FAILED
                    job.error = f"item {item.id}: {exc}"
                    job.heartbeat_at = _utcnow()
                    session.commit()
                    return _reload(job_id, session_factory)
                job.heartbeat_at = _utcnow()
                session.commit()
    except ClaimLost:
        pass  # a newer runner owns the job — leave its state untouched
    except Exception:
        # unexpected runner failure (e.g. DB dropped): never leave the job
        # stuck RUNNING — mark FAILED so it can be retried
        _fail_interrupted(job_id, token, session_factory)
    return _reload(job_id, session_factory)


def _fail_interrupted(job_id: int, token: str, session_factory):
    """Best-effort FAILED marking, guarded by the claim token so a zombie
    runner can never clobber a newer run. If the DB itself is down, the
    expired lease still lets the next claim recover the job."""
    try:
        with session_factory() as session:
            job = session.execute(
                select(ExportJob).where(ExportJob.id == job_id).with_for_update()
            ).scalar_one_or_none()
            if (
                job is not None
                and job.claim_token == token
                and job.status == ExportStatus.RUNNING
            ):
                job.status = ExportStatus.FAILED
                job.error = "runner interrupted; safe to retry"
                session.commit()
    except Exception:
        pass


def recover_interrupted_jobs(session_factory=SessionLocal) -> int:
    """Runners are in-process: any RUNNING job at startup is an orphan.
    Recover it to FAILED so the user can retry instead of staring at a
    permanently 'running' job."""
    with session_factory() as session:
        result = session.execute(
            update(ExportJob)
            .where(ExportJob.status == ExportStatus.RUNNING)
            .values(
                status=ExportStatus.FAILED,
                error="runner lost on process restart; safe to retry",
            )
        )
        session.commit()
        return result.rowcount


def run_export(
    job_id: int,
    fault_hook: Callable[[ExportJobItem, int], None] | None = None,
    session_factory=SessionLocal,
) -> ExportJob:
    """Claim + run in one call (convenience used by tests and workers)."""
    with session_factory() as session:
        token = claim_job(session, job_id)
    return run_claimed(job_id, token, fault_hook=fault_hook, session_factory=session_factory)


def retry_export(job_id: int, **kwargs) -> ExportJob:
    """Retry a failed (or crash-orphaned) job. Done items are skipped and
    reprocessed items never duplicate output."""
    return run_export(job_id, **kwargs)


def _reload(job_id: int, session_factory) -> ExportJob:
    with session_factory() as session:
        job = session.get(ExportJob, job_id)
        session.expunge(job)
        return job
