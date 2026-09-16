import asyncio
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import Base, engine, get_db
from .models import (
    Annotation,
    AnnotationVersion,
    Arbitration,
    ExportJob,
    Image,
    ImageRevision,
    RegionStatus,
)
from .schemas import (
    AdjudicateRequest,
    AnnotationCreate,
    ApproveRequest,
    ArbitrationCreate,
    ArbitrationSubmitRequest,
    ExportCreate,
    MigrateRequest,
    RejectRequest,
    SubmitRequest,
)
from .services import annotations as ann_svc
from .services import arbitration as arb_svc
from .services import exports as exp_svc
from .services import images as img_svc
from .services import review as rev_svc


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)
    # runners are in-process: any job still RUNNING here is an orphan
    recovered = exp_svc.recover_interrupted_jobs()
    if recovered:
        print(f"recovered {recovered} interrupted export job(s) -> failed (retryable)")
    yield


app = FastAPI(title="Segmentation Annotation Workbench", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- images ----------


@app.post("/images", status_code=201)
async def upload_image(name: str, file: UploadFile = File(...), db: Session = Depends(get_db)):
    data = await file.read()
    try:
        image = img_svc.create_image(db, name, data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _image_json(db, image)


@app.get("/images")
def list_images(db: Session = Depends(get_db)):
    return [_image_json(db, i) for i in db.execute(select(Image)).scalars().all()]


def _image_json(db: Session, image: Image):
    stale_count = (
        db.execute(select(Annotation).where(Annotation.image_id == image.id))
        .scalars()
        .all()
    )
    return {
        "id": image.id,
        "name": image.name,
        "current_revision": image.current_revision,
        "width": image.width,
        "height": image.height,
        "stale_annotations": sum(
            1 for a in stale_count if a.status.value == "stale"
        ),
    }


@app.get("/images/{image_id}/file")
def image_file(image_id: int, revision: int | None = None, db: Session = Depends(get_db)):
    image = db.get(Image, image_id)
    if image is None:
        raise HTTPException(404, "image not found")
    rev = revision or image.current_revision
    row = db.execute(
        select(ImageRevision).where(
            ImageRevision.image_id == image_id, ImageRevision.revision == rev
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "revision not found")
    return FileResponse(row.storage_path, media_type="image/png")


@app.post("/images/{image_id}/replace")
async def replace_image(image_id: int, file: UploadFile = File(...), db: Session = Depends(get_db)):
    data = await file.read()
    try:
        image, stale, voided = img_svc.replace_image(db, image_id, data)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {
        "image": _image_json(db, image),
        "stale_annotation_ids": [a.id for a in stale],
        "voided_arbitration_ids": [a.id for a in voided],
        "message": "annotations on the old revision are now stale; migrate or invalidate each explicitly",
    }


# ---------- annotations ----------


@app.post("/images/{image_id}/annotations", status_code=201)
def create_annotation(image_id: int, body: AnnotationCreate, db: Session = Depends(get_db)):
    try:
        ann = ann_svc.create_annotation(db, image_id, body.label, body.assignee)
    except KeyError as e:
        raise HTTPException(404, str(e))
    return _ann_json(ann)


@app.get("/annotations")
def list_annotations(
    image_id: int | None = None, status: str | None = None, db: Session = Depends(get_db)
):
    q = select(Annotation)
    if image_id is not None:
        q = q.where(Annotation.image_id == image_id)
    if status is not None:
        q = q.where(Annotation.status == status)
    return [_ann_json(a) for a in db.execute(q).scalars().all()]


def _ann_json(ann: Annotation):
    return {
        "id": ann.id,
        "image_id": ann.image_id,
        "image_revision": ann.image_revision,
        "label": ann.label,
        "status": ann.status.value,
        "current_version": ann.current_version,
        "assignee": ann.assignee,
        "arbitration": (
            {
                "id": ann.arbitration_id,
                "side": ann.arbitration_side,
                "submitted": ann.arbitration_submitted,
            }
            if ann.arbitration_id is not None
            else None
        ),
        "open_regions": [
            {
                "id": r.id,
                "polygon": r.polygon,
                "comment": r.comment,
                "rejected_version": r.rejected_version,
            }
            for r in ann.rejection_regions
            if r.status == RegionStatus.OPEN
        ],
    }


@app.get("/annotations/{annotation_id}")
def get_annotation(
    annotation_id: int, viewer: str = "", db: Session = Depends(get_db)
):
    ann = db.get(Annotation, annotation_id)
    if ann is None:
        raise HTTPException(404, "annotation not found")
    try:
        arb_svc.check_blind_read(ann, viewer)
    except arb_svc.ArbitrationPermission as e:
        raise HTTPException(403, str(e))
    out = _ann_json(ann)
    out["versions"] = [
        {
            "version": v.version,
            "author": v.author,
            "source": v.source,
            "image_revision": v.image_revision,
            "created_at": v.created_at.isoformat(),
        }
        for v in sorted(ann.versions, key=lambda x: x.version)
    ]
    return out


@app.get("/annotations/{annotation_id}/versions/{version}/mask.png")
def get_mask(
    annotation_id: int, version: int, viewer: str = "", db: Session = Depends(get_db)
):
    ann = db.get(Annotation, annotation_id)
    if ann is None:
        raise HTTPException(404, "annotation not found")
    try:
        arb_svc.check_blind_read(ann, viewer)
    except arb_svc.ArbitrationPermission as e:
        raise HTTPException(403, str(e))
    row = db.execute(
        select(AnnotationVersion).where(
            AnnotationVersion.annotation_id == annotation_id,
            AnnotationVersion.version == version,
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "version not found")
    return FileResponse(row.mask_path, media_type="image/png")


@app.put("/annotations/{annotation_id}/mask")
async def save_mask(
    annotation_id: int,
    author: str,
    base_version: int,
    resolution: str | None = None,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    data = await file.read()
    try:
        row = ann_svc.save_mask(db, annotation_id, data, author, base_version, resolution)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ann_svc.ConflictError as e:
        # 409 with full conflict detail — the client must show this, never overwrite silently
        raise HTTPException(
            409,
            detail={
                "message": "concurrent edits overlap your changes",
                "current_version": e.current_version,
                "conflicts": [c.model_dump() for c in e.conflicts],
            },
        )
    except ann_svc.StaleAnnotationError as e:
        raise HTTPException(409, {"message": str(e), "kind": "stale"})
    except ann_svc.BlindIsolationError as e:
        raise HTTPException(403, str(e))
    except ann_svc.InvalidStateError as e:
        raise HTTPException(400, str(e))
    return {"annotation_id": annotation_id, "version": row.version, "author": row.author}


# ---------- review ----------


@app.post("/annotations/{annotation_id}/submit")
def submit(annotation_id: int, body: SubmitRequest, db: Session = Depends(get_db)):
    return _review_call(db, rev_svc.submit, annotation_id, body.actor, body.expected_version)


@app.post("/annotations/{annotation_id}/review/approve")
def approve(annotation_id: int, body: ApproveRequest, db: Session = Depends(get_db)):
    return _review_call(db, rev_svc.approve, annotation_id, body.actor, body.expected_version)


@app.post("/annotations/{annotation_id}/review/reject")
def reject(annotation_id: int, body: RejectRequest, db: Session = Depends(get_db)):
    try:
        ann = rev_svc.reject(
            db, annotation_id, body.actor, body.expected_version, body.regions
        )
    except KeyError as e:
        raise HTTPException(404, str(e))
    except rev_svc.ReviewConflict as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _ann_json(ann)


def _review_call(db, fn, annotation_id, actor, expected_version):
    try:
        ann = fn(db, annotation_id, actor, expected_version)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except rev_svc.ReviewConflict as e:
        raise HTTPException(409, str(e))
    return _ann_json(ann)


# ---------- stale migration ----------


@app.post("/annotations/{annotation_id}/migrate", status_code=201)
def migrate(annotation_id: int, body: MigrateRequest, db: Session = Depends(get_db)):
    try:
        row = img_svc.migrate_annotation(db, annotation_id, body.actor)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"annotation_id": annotation_id, "version": row.version, "source": row.source}


@app.post("/annotations/{annotation_id}/invalidate")
def invalidate(annotation_id: int, db: Session = Depends(get_db)):
    try:
        ann = img_svc.invalidate_annotation(db, annotation_id)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(409, str(e))
    return _ann_json(ann)


# ---------- double-blind arbitration ----------


@app.post("/arbitrations", status_code=201)
def create_arbitration(body: ArbitrationCreate, db: Session = Depends(get_db)):
    try:
        arb = arb_svc.initiate(
            db,
            image_id=body.image_id,
            label=body.label,
            initiator=body.initiator,
            annotator_a=body.annotator_a,
            annotator_b=body.annotator_b,
            arbitrator=body.arbitrator,
        )
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return arb_svc.serialize(arb)


@app.get("/arbitrations")
def list_arbitrations(image_id: int | None = None, db: Session = Depends(get_db)):
    q = select(Arbitration).order_by(Arbitration.id)
    if image_id is not None:
        q = q.where(Arbitration.image_id == image_id)
    return [arb_svc.serialize(a) for a in db.execute(q).scalars().all()]


@app.get("/arbitrations/{arbitration_id}")
def get_arbitration(arbitration_id: int, db: Session = Depends(get_db)):
    try:
        arb = arb_svc.get_arbitration(db, arbitration_id)
    except KeyError as e:
        raise HTTPException(404, str(e))
    return arb_svc.serialize(arb)


@app.get("/arbitrations/{arbitration_id}/mask")
def arbitration_side_mask(
    arbitration_id: int, side: str, viewer: str = "", db: Session = Depends(get_db)
):
    try:
        arb = arb_svc.get_arbitration(db, arbitration_id)
        ann = arb_svc.check_side_mask_access(arb, side, viewer)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except arb_svc.ArbitrationPermission as e:
        raise HTTPException(403, str(e))
    row = db.execute(
        select(AnnotationVersion).where(
            AnnotationVersion.annotation_id == ann.id,
            AnnotationVersion.version == ann.current_version,
        )
    ).scalar_one()
    return FileResponse(row.mask_path, media_type="image/png")


@app.post("/arbitrations/{arbitration_id}/submit")
def submit_arbitration_side(
    arbitration_id: int, body: ArbitrationSubmitRequest, db: Session = Depends(get_db)
):
    try:
        arb = arb_svc.submit_side(db, arbitration_id, body.actor)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except arb_svc.ArbitrationPermission as e:
        raise HTTPException(403, str(e))
    except arb_svc.ArbitrationConflict as e:
        raise HTTPException(409, str(e))
    return arb_svc.serialize(arb)


@app.post("/arbitrations/{arbitration_id}/adjudicate")
def adjudicate_arbitration(
    arbitration_id: int, body: AdjudicateRequest, db: Session = Depends(get_db)
):
    try:
        arb = arb_svc.adjudicate(db, arbitration_id, body.actor, body.decisions)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except arb_svc.ArbitrationPermission as e:
        raise HTTPException(403, str(e))
    except arb_svc.ArbitrationConflict as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return arb_svc.serialize(arb)


# ---------- exports ----------


@app.post("/exports", status_code=201)
def create_export(body: ExportCreate, background: BackgroundTasks, db: Session = Depends(get_db)):
    try:
        job = exp_svc.create_export(db, body.name, body.annotation_ids)
        token = exp_svc.claim_job(db, job.id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    background.add_task(_run_export_async, job.id, token)
    return _job_json(db.get(ExportJob, job.id))


def _run_export_async(job_id: int, token: str):
    asyncio.run(asyncio.to_thread(exp_svc.run_claimed, job_id, token))


@app.get("/exports")
def list_exports(db: Session = Depends(get_db)):
    return [_job_json(j) for j in db.execute(select(ExportJob)).scalars().all()]


@app.get("/exports/{job_id}")
def get_export(job_id: int, db: Session = Depends(get_db)):
    job = db.get(ExportJob, job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    out = _job_json(job)
    out["items"] = [
        {
            "id": i.id,
            "annotation_id": i.annotation_id,
            "annotation_version": i.annotation_version,
            "status": i.status.value,
            "error": i.error,
        }
        for i in job.items
    ]
    return out


@app.post("/exports/{job_id}/retry")
def retry_export(job_id: int, background: BackgroundTasks, db: Session = Depends(get_db)):
    # claim synchronously so a losing concurrent retry gets an immediate 409
    try:
        token = exp_svc.claim_job(db, job_id)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except exp_svc.ClaimConflict as e:
        raise HTTPException(409, str(e))
    background.add_task(_run_export_async, job_id, token)
    return _job_json(db.get(ExportJob, job_id))


def _job_json(job: ExportJob):
    return {
        "id": job.id,
        "name": job.name,
        "status": job.status.value if hasattr(job.status, "value") else job.status,
        "total_items": job.total_items,
        "done_items": job.done_items,
        "attempt": job.attempt,
        "error": job.error,
        "output_dir": job.output_dir,
        "frozen_at": job.frozen_at.isoformat() if job.frozen_at else None,
    }
