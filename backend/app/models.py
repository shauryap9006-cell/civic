"""SQLAlchemy models mirroring db/schema.sql."""

import enum
import struct
import uuid
from datetime import datetime
from typing import Any

from geoalchemy2 import Geography
from geoalchemy2.elements import WKBElement, WKTElement
from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.logic.state_machine import PotholeStatus


class SeverityBand(enum.StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ReportKind(enum.StrEnum):
    INITIAL = "initial"
    REMINDER = "reminder"
    ESCALATION = "escalation"
    REOPENED = "reopened"


class ReportDeliveryStatus(enum.StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class DevicePlatform(enum.StrEnum):
    ANDROID = "android"
    LINUX = "linux"
    REPLAY = "replay"
    SEED = "seed"


def pg_enum(enum_cls: type[enum.Enum], name: str) -> Enum:
    """Helper for PostgreSQL ENUM using enum values (lowercase) without recreating type."""
    return Enum(
        enum_cls,
        name=name,
        create_type=False,
        values_callable=lambda x: [e.value for e in x],
    )


def parse_point_wkb(
    data: bytes | memoryview | str | WKBElement | WKTElement,
) -> tuple[float, float]:
    """Parse WKB (or hex string / WKBElement / WKTElement) of a 2D Point into (lat, lon)."""
    if isinstance(data, WKTElement):
        text_val = str(data.data)
        coords_part = text_val[text_val.find("(") + 1 : text_val.find(")")].strip()
        lon_s, lat_s = coords_part.split()
        return float(lat_s), float(lon_s)
    if isinstance(data, WKBElement):
        data = data.data
    if isinstance(data, str):
        if data.startswith("POINT") or "(" in data:
            coords_part = data[data.find("(") + 1 : data.find(")")].strip()
            lon_s, lat_s = coords_part.split()
            return float(lat_s), float(lon_s)
        data = bytes.fromhex(data)
    raw = bytes(data)
    endian = "<" if raw[0] == 1 else ">"
    geom_type = struct.unpack(f"{endian}I", raw[1:5])[0]
    has_srid = bool(geom_type & 0x20000000)
    offset = 9 if has_srid else 5
    lon, lat = struct.unpack(f"{endian}dd", raw[offset : offset + 16])
    return lat, lon


def make_point_geography(lat: float, lon: float) -> WKTElement:
    """Create a WKTElement for PostGIS Geography(Point, 4326)."""
    return WKTElement(f"POINT({lon} {lat})", srid=4326)


class Base(DeclarativeBase):
    pass


class Authority(Base):
    __tablename__ = "authorities"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    city: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    escalation_email: Mapped[str | None] = mapped_column(Text, nullable=True)
    boundary = mapped_column(Geography(geometry_type="MULTIPOLYGON", srid=4326), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # Relationships
    potholes: Mapped[list["Pothole"]] = relationship("Pothole", back_populates="authority")
    reports: Mapped[list["Report"]] = relationship("Report", back_populates="authority")


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    public_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    app_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    events: Mapped[list["Event"]] = relationship("Event", back_populates="device")
    passes: Mapped[list["Pass"]] = relationship("Pass", back_populates="device")


class Pothole(Base):
    __tablename__ = "potholes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    location = mapped_column(Geography(geometry_type="POINT", srid=4326), nullable=False)
    status: Mapped[PotholeStatus] = mapped_column(
        pg_enum(PotholeStatus, "pothole_status"),
        nullable=False,
        default=PotholeStatus.OPEN,
    )
    severity: Mapped[SeverityBand] = mapped_column(
        pg_enum(SeverityBand, "severity_band"),
        nullable=False,
        default=SeverityBand.LOW,
    )
    event_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    authority_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("authorities.id"), nullable=True
    )
    before_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("events.id", name="potholes_before_event_fk", use_alter=True),
        nullable=True,
    )
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_event_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reported_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_reported_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    claimed_fixed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reopened_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    fixed_token_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fixed_token_hash: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    authority: Mapped[Authority | None] = relationship("Authority", back_populates="potholes")
    events: Mapped[list["Event"]] = relationship(
        "Event", back_populates="pothole", foreign_keys="[Event.pothole_id]"
    )
    before_event: Mapped["Event | None"] = relationship(
        "Event", foreign_keys=[before_event_id], post_update=True
    )
    reports: Mapped[list["Report"]] = relationship("Report", back_populates="pothole")
    status_history: Mapped[list["StatusHistory"]] = relationship(
        "StatusHistory", back_populates="pothole", cascade="all, delete-orphan"
    )
    pass_coverages: Mapped[list["PassCoverage"]] = relationship(
        "PassCoverage", back_populates="pothole", cascade="all, delete-orphan"
    )

    @property
    def coordinates(self) -> tuple[float, float]:
        """Return (lat, lon) parsed from location WKB."""
        return parse_point_wkb(self.location)

    @property
    def lat(self) -> float:
        return self.coordinates[0]

    @property
    def lon(self) -> float:
        return self.coordinates[1]


class Event(Base):
    __tablename__ = "events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True
    )  # device generated UUID
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id"), nullable=False
    )
    pass_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    pothole_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("potholes.id"), nullable=True
    )
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    location = mapped_column(Geography(geometry_type="POINT", srid=4326), nullable=False)
    gps_accuracy_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    heading_deg: Mapped[float | None] = mapped_column(Float, nullable=True)
    speed_mps: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    track_frames: Mapped[int] = mapped_column(Integer, nullable=False)
    area_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    severity: Mapped[SeverityBand | None] = mapped_column(
        pg_enum(SeverityBand, "severity_band"), nullable=True
    )
    crop_key: Mapped[str] = mapped_column(Text, nullable=False)
    crop_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    sharpness: Mapped[float | None] = mapped_column(Float, nullable=True)
    mean_luma: Mapped[float | None] = mapped_column(Float, nullable=True)
    model_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    app_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    signature: Mapped[str] = mapped_column(Text, nullable=False)

    # Relationships
    device: Mapped[Device] = relationship("Device", back_populates="events")
    pothole: Mapped[Pothole | None] = relationship(
        "Pothole", back_populates="events", foreign_keys=[pothole_id]
    )

    @property
    def coordinates(self) -> tuple[float, float]:
        """Return (lat, lon) parsed from location WKB."""
        return parse_point_wkb(self.location)

    @property
    def lat(self) -> float:
        return self.coordinates[0]

    @property
    def lon(self) -> float:
        return self.coordinates[1]


class Pass(Base):
    __tablename__ = "passes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True
    )  # device generated UUID
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id"), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    path = mapped_column(Geography(geometry_type="LINESTRING", srid=4326), nullable=True)
    samples: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    model_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    app_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    signature: Mapped[str] = mapped_column(Text, nullable=False)

    # Relationships
    device: Mapped[Device] = relationship("Device", back_populates="passes")
    pass_coverages: Mapped[list["PassCoverage"]] = relationship(
        "PassCoverage", back_populates="pass_obj", cascade="all, delete-orphan"
    )


class PassCoverage(Base):
    __tablename__ = "pass_coverage"

    pass_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("passes.id", ondelete="CASCADE"),
        primary_key=True,
    )
    pothole_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("potholes.id", ondelete="CASCADE"),
        primary_key=True,
    )
    crossing_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    clean: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    window_samples: Mapped[int] = mapped_column(Integer, nullable=False)
    ok_fraction: Mapped[float | None] = mapped_column(Float, nullable=True)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # Relationships
    pass_obj: Mapped[Pass] = relationship("Pass", back_populates="pass_coverages")
    pothole: Mapped[Pothole] = relationship("Pothole", back_populates="pass_coverages")


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    pothole_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("potholes.id"), nullable=False
    )
    authority_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("authorities.id"), nullable=False
    )
    kind: Mapped[ReportKind] = mapped_column(
        pg_enum(ReportKind, "report_kind"), nullable=False
    )
    to_address: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[ReportDeliveryStatus] = mapped_column(
        pg_enum(ReportDeliveryStatus, "report_delivery_status"),
        nullable=False,
        default=ReportDeliveryStatus.PENDING,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    provider_message_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    pothole: Mapped[Pothole] = relationship("Pothole", back_populates="reports")
    authority: Mapped[Authority] = relationship("Authority", back_populates="reports")


class StatusHistory(Base):
    __tablename__ = "status_history"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    pothole_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("potholes.id", ondelete="CASCADE"),
        nullable=False,
    )
    from_status: Mapped[PotholeStatus | None] = mapped_column(
        pg_enum(PotholeStatus, "pothole_status"), nullable=True
    )
    to_status: Mapped[PotholeStatus] = mapped_column(
        pg_enum(PotholeStatus, "pothole_status"), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Relationships
    pothole: Mapped[Pothole] = relationship("Pothole", back_populates="status_history")


class AppState(Base):
    __tablename__ = "app_state"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
