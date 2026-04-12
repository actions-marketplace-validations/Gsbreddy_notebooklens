"""SQLAlchemy models for the managed API skeleton."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
import uuid

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Enum as SqlEnum,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


JSONVariant = JSON().with_variant(JSONB(astext_type=Text()), "postgresql")

_NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Base declarative model."""

    metadata = MetaData(naming_convention=_NAMING_CONVENTION)


class TimestampMixin:
    """Created/updated timestamps."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
    )


class InstallationAccountType(str, Enum):
    USER = "user"
    ORGANIZATION = "organization"


class ManagedReviewStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"
    CLOSED = "closed"


class SnapshotBuildJobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    RETRYABLE_FAILED = "retryable_failed"
    FAILED = "failed"
    SUCCEEDED = "succeeded"


class ReviewSnapshotStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"


class ReviewThreadStatus(str, Enum):
    OPEN = "open"
    RESOLVED = "resolved"
    OUTDATED = "outdated"


class NotificationEventType(str, Enum):
    THREAD_CREATED = "thread_created"
    REPLY_ADDED = "reply_added"
    THREAD_RESOLVED = "thread_resolved"
    THREAD_REOPENED = "thread_reopened"


class NotificationDeliveryState(str, Enum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class GitHubInstallation(TimestampMixin, Base):
    __tablename__ = "github_installations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    github_installation_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    account_login: Mapped[str] = mapped_column(String(255), nullable=False)
    account_type: Mapped[InstallationAccountType] = mapped_column(
        SqlEnum(InstallationAccountType, native_enum=False),
        nullable=False,
    )

    repositories: Mapped[list["InstallationRepository"]] = relationship(
        back_populates="installation",
        cascade="all, delete-orphan",
    )


class InstallationRepository(TimestampMixin, Base):
    __tablename__ = "installation_repositories"
    __table_args__ = (
        UniqueConstraint("installation_id", "full_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    installation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("github_installations.id", ondelete="CASCADE"),
        nullable=False,
    )
    owner: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    private: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    installation: Mapped[GitHubInstallation] = relationship(back_populates="repositories")
    managed_reviews: Mapped[list["ManagedReview"]] = relationship(
        back_populates="installation_repository",
        cascade="all, delete-orphan",
    )


class ManagedReview(TimestampMixin, Base):
    __tablename__ = "managed_reviews"
    __table_args__ = (
        UniqueConstraint("installation_repository_id", "pull_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    installation_repository_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("installation_repositories.id", ondelete="CASCADE"),
        nullable=False,
    )
    owner: Mapped[str] = mapped_column(String(255), nullable=False)
    repo: Mapped[str] = mapped_column(String(255), nullable=False)
    pull_number: Mapped[int] = mapped_column(Integer, nullable=False)
    base_branch: Mapped[str] = mapped_column(String(255), nullable=False)
    latest_base_sha: Mapped[str] = mapped_column(String(255), nullable=False)
    latest_head_sha: Mapped[str] = mapped_column(String(255), nullable=False)
    pull_author_github_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    pull_author_login: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[ManagedReviewStatus] = mapped_column(
        SqlEnum(ManagedReviewStatus, native_enum=False),
        default=ManagedReviewStatus.PENDING,
        nullable=False,
    )
    latest_check_run_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    latest_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        nullable=True,
    )

    installation_repository: Mapped[InstallationRepository] = relationship(
        back_populates="managed_reviews"
    )
    snapshot_jobs: Mapped[list["SnapshotBuildJob"]] = relationship(
        back_populates="managed_review",
        cascade="all, delete-orphan",
    )
    review_snapshots: Mapped[list["ReviewSnapshot"]] = relationship(
        back_populates="managed_review",
        cascade="all, delete-orphan",
    )
    review_threads: Mapped[list["ReviewThread"]] = relationship(
        back_populates="managed_review",
        cascade="all, delete-orphan",
    )


class SnapshotBuildJob(Base):
    __tablename__ = "snapshot_build_jobs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    managed_review_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("managed_reviews.id", ondelete="CASCADE"),
        nullable=False,
    )
    base_sha: Mapped[str] = mapped_column(String(255), nullable=False)
    head_sha: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[SnapshotBuildJobStatus] = mapped_column(
        SqlEnum(SnapshotBuildJobStatus, native_enum=False),
        default=SnapshotBuildJobStatus.QUEUED,
        nullable=False,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    managed_review: Mapped[ManagedReview] = relationship(back_populates="snapshot_jobs")


Index(
    "ix_snapshot_build_jobs_status_scheduled_at",
    SnapshotBuildJob.status,
    SnapshotBuildJob.scheduled_at,
)


class ReviewSnapshot(Base):
    __tablename__ = "review_snapshots"
    __table_args__ = (
        UniqueConstraint("managed_review_id", "snapshot_index"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    managed_review_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("managed_reviews.id", ondelete="CASCADE"),
        nullable=False,
    )
    base_sha: Mapped[str] = mapped_column(String(255), nullable=False)
    head_sha: Mapped[str] = mapped_column(String(255), nullable=False)
    snapshot_index: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[ReviewSnapshotStatus] = mapped_column(
        SqlEnum(ReviewSnapshotStatus, native_enum=False),
        default=ReviewSnapshotStatus.PENDING,
        nullable=False,
    )
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    summary_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    flagged_findings_json: Mapped[list] = mapped_column(JSONVariant, default=list, nullable=False)
    reviewer_guidance_json: Mapped[list] = mapped_column(JSONVariant, default=list, nullable=False)
    snapshot_payload_json: Mapped[dict] = mapped_column(JSONVariant, default=dict, nullable=False)
    notebook_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    changed_cell_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    managed_review: Mapped[ManagedReview] = relationship(back_populates="review_snapshots")
    origin_threads: Mapped[list["ReviewThread"]] = relationship(
        back_populates="origin_snapshot",
        foreign_keys="ReviewThread.origin_snapshot_id",
    )
    current_threads: Mapped[list["ReviewThread"]] = relationship(
        back_populates="current_snapshot",
        foreign_keys="ReviewThread.current_snapshot_id",
    )


class ReviewThread(TimestampMixin, Base):
    __tablename__ = "review_threads"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    managed_review_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("managed_reviews.id", ondelete="CASCADE"),
        nullable=False,
    )
    origin_snapshot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("review_snapshots.id", ondelete="CASCADE"),
        nullable=False,
    )
    current_snapshot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("review_snapshots.id", ondelete="CASCADE"),
        nullable=False,
    )
    anchor_json: Mapped[dict] = mapped_column(JSONVariant, default=dict, nullable=False)
    status: Mapped[ReviewThreadStatus] = mapped_column(
        SqlEnum(ReviewThreadStatus, native_enum=False),
        default=ReviewThreadStatus.OPEN,
        nullable=False,
    )
    carried_forward: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_by_github_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by_github_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    managed_review: Mapped[ManagedReview] = relationship(back_populates="review_threads")
    origin_snapshot: Mapped[ReviewSnapshot] = relationship(
        back_populates="origin_threads",
        foreign_keys=[origin_snapshot_id],
    )
    current_snapshot: Mapped[ReviewSnapshot] = relationship(
        back_populates="current_threads",
        foreign_keys=[current_snapshot_id],
    )
    messages: Mapped[list["ThreadMessage"]] = relationship(
        back_populates="thread",
        cascade="all, delete-orphan",
        order_by="ThreadMessage.created_at",
    )
    notifications: Mapped[list["NotificationOutbox"]] = relationship(
        back_populates="thread",
        cascade="all, delete-orphan",
        order_by="NotificationOutbox.created_at",
    )


Index(
    "ix_review_threads_managed_review_id_status",
    ReviewThread.managed_review_id,
    ReviewThread.status,
)
Index(
    "ix_review_threads_current_snapshot_id_status",
    ReviewThread.current_snapshot_id,
    ReviewThread.status,
)


class ThreadMessage(Base):
    __tablename__ = "thread_messages"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    thread_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("review_threads.id", ondelete="CASCADE"),
        nullable=False,
    )
    author_github_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    author_login: Mapped[str] = mapped_column(String(255), nullable=False)
    body_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    thread: Mapped[ReviewThread] = relationship(back_populates="messages")


class NotificationOutbox(Base):
    __tablename__ = "notification_outbox"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    thread_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("review_threads.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_type: Mapped[NotificationEventType] = mapped_column(
        SqlEnum(NotificationEventType, native_enum=False),
        nullable=False,
    )
    recipient_github_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    recipient_email: Mapped[str] = mapped_column(String(320), nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSONVariant, default=dict, nullable=False)
    delivery_state: Mapped[NotificationDeliveryState] = mapped_column(
        SqlEnum(NotificationDeliveryState, native_enum=False),
        default=NotificationDeliveryState.PENDING,
        nullable=False,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    thread: Mapped[ReviewThread] = relationship(back_populates="notifications")


class UserSession(Base):
    __tablename__ = "user_sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    github_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    github_login: Mapped[str] = mapped_column(String(255), nullable=False)
    access_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


__all__ = [
    "ApiConfigurationError",
    "Base",
    "GitHubInstallation",
    "InstallationAccountType",
    "InstallationRepository",
    "ManagedReview",
    "ManagedReviewStatus",
    "NotificationDeliveryState",
    "NotificationEventType",
    "NotificationOutbox",
    "ReviewThread",
    "ReviewThreadStatus",
    "ReviewSnapshot",
    "ReviewSnapshotStatus",
    "SnapshotBuildJob",
    "SnapshotBuildJobStatus",
    "ThreadMessage",
    "UserSession",
    "utcnow",
]
