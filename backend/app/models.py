import enum
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow():
    return datetime.now(timezone.utc)


class AnnotationStatus(str, enum.Enum):
    DRAFT = "draft"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    STALE = "stale"  # image was replaced; must be migrated or invalidated
    INVALIDATED = "invalidated"


class RegionStatus(str, enum.Enum):
    OPEN = "open"
    RESOLVED = "resolved"


class ExportStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    FAILED = "failed"
    COMPLETED = "completed"


class ExportItemStatus(str, enum.Enum):
    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"


class Image(Base):
    __tablename__ = "images"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    current_revision: Mapped[int] = mapped_column(Integer, default=1)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    revisions: Mapped[list["ImageRevision"]] = relationship(
        back_populates="image", cascade="all, delete-orphan"
    )
    annotations: Mapped[list["Annotation"]] = relationship(back_populates="image")


class ImageRevision(Base):
    """Every upload/replace of the raw image is an immutable revision."""

    __tablename__ = "image_revisions"
    __table_args__ = (UniqueConstraint("image_id", "revision"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    image_id: Mapped[int] = mapped_column(ForeignKey("images.id"))
    revision: Mapped[int] = mapped_column(Integer)
    storage_path: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    image: Mapped[Image] = relationship(back_populates="revisions")


class Annotation(Base):
    __tablename__ = "annotations"

    id: Mapped[int] = mapped_column(primary_key=True)
    image_id: Mapped[int] = mapped_column(ForeignKey("images.id"))
    image_revision: Mapped[int] = mapped_column(Integer)  # revision the mask is drawn against
    label: Mapped[str] = mapped_column(String(128))
    status: Mapped[AnnotationStatus] = mapped_column(
        Enum(AnnotationStatus), default=AnnotationStatus.DRAFT
    )
    current_version: Mapped[int] = mapped_column(Integer, default=0)
    assignee: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    image: Mapped[Image] = relationship(back_populates="annotations")
    versions: Mapped[list["AnnotationVersion"]] = relationship(
        back_populates="annotation", cascade="all, delete-orphan"
    )
    rejection_regions: Mapped[list["RejectionRegion"]] = relationship(
        back_populates="annotation", cascade="all, delete-orphan"
    )


class AnnotationVersion(Base):
    """Immutable mask version. Never updated in place — a save is a new row."""

    __tablename__ = "annotation_versions"
    __table_args__ = (UniqueConstraint("annotation_id", "version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    annotation_id: Mapped[int] = mapped_column(ForeignKey("annotations.id"))
    version: Mapped[int] = mapped_column(Integer)
    image_revision: Mapped[int] = mapped_column(Integer)
    mask_path: Mapped[str] = mapped_column(Text)
    mask_hash: Mapped[str] = mapped_column(String(64))
    author: Mapped[str] = mapped_column(String(64))
    parent_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="draw")  # draw | migrate
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    annotation: Mapped[Annotation] = relationship(back_populates="versions")


class ReviewEvent(Base):
    __tablename__ = "review_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    annotation_id: Mapped[int] = mapped_column(ForeignKey("annotations.id"))
    version: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(16))  # submit | approve | reject
    actor: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RejectionRegion(Base):
    """A region the reviewer kicked back. Geometry stored as polygon + raster mask."""

    __tablename__ = "rejection_regions"

    id: Mapped[int] = mapped_column(primary_key=True)
    annotation_id: Mapped[int] = mapped_column(ForeignKey("annotations.id"))
    review_event_id: Mapped[int] = mapped_column(ForeignKey("review_events.id"))
    rejected_version: Mapped[int] = mapped_column(Integer)
    polygon: Mapped[list] = mapped_column(JSON)  # [[x, y], ...]
    mask_path: Mapped[str] = mapped_column(Text)
    status: Mapped[RegionStatus] = mapped_column(Enum(RegionStatus), default=RegionStatus.OPEN)
    resolved_in_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    comment: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    annotation: Mapped[Annotation] = relationship(back_populates="rejection_regions")


class ExportJob(Base):
    __tablename__ = "export_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    status: Mapped[ExportStatus] = mapped_column(Enum(ExportStatus), default=ExportStatus.PENDING)
    spec: Mapped[dict] = mapped_column(JSON, default=dict)
    frozen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    total_items: Mapped[int] = mapped_column(Integer, default=0)
    done_items: Mapped[int] = mapped_column(Integer, default=0)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    # runner ownership: exactly one live runner holds the claim; the heartbeat
    # lease lets a crashed runner's job be reclaimed instead of stuck RUNNING
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    output_dir: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    items: Mapped[list["ExportJobItem"]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )


class ExportJobItem(Base):
    """One frozen (annotation, version) pair. Per-item status makes retry resumable."""

    __tablename__ = "export_job_items"
    __table_args__ = (UniqueConstraint("job_id", "annotation_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("export_jobs.id"))
    annotation_id: Mapped[int] = mapped_column(ForeignKey("annotations.id"))
    annotation_version: Mapped[int] = mapped_column(Integer)  # frozen at export creation
    status: Mapped[ExportItemStatus] = mapped_column(
        Enum(ExportItemStatus), default=ExportItemStatus.PENDING
    )
    error: Mapped[str] = mapped_column(Text, default="")
    output_path: Mapped[str] = mapped_column(Text, default="")

    job: Mapped[ExportJob] = relationship(back_populates="items")
