"""Typed configuration loader for CivicLens.

Loads environment variables (.env) and tunable thresholds (config/thresholds.yaml).
Thresholds are validated at startup; missing or invalid values fail fast.
"""

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class DetectionROI(BaseModel):
    x0: float = Field(ge=0.0, le=1.0)
    y0: float = Field(ge=0.0, le=1.0)
    x1: float = Field(ge=0.0, le=1.0)
    y1: float = Field(ge=0.0, le=1.0)

    @field_validator("x1")
    @classmethod
    def check_x(cls, v: float, info: Any) -> float:
        if "x0" in info.data and v <= info.data["x0"]:
            raise ValueError("roi x1 must be > x0")
        return v

    @field_validator("y1")
    @classmethod
    def check_y(cls, v: float, info: Any) -> float:
        if "y0" in info.data and v <= info.data["y0"]:
            raise ValueError("roi y1 must be > y0")
        return v


class DetectionThresholds(BaseModel):
    model_input_px: int = Field(gt=0)
    detector_conf_min: float = Field(ge=0.0, le=1.0)
    nms_iou: float = Field(ge=0.0, le=1.0)
    roi: DetectionROI
    target_fps: float = Field(gt=0)
    min_fps: float = Field(gt=0)


class TrackingThresholds(BaseModel):
    tracker: Literal["bytetrack", "botsort"]
    min_track_frames: int = Field(ge=1)
    min_mean_confidence: float = Field(ge=0.0, le=1.0)
    lost_track_buffer_frames: int = Field(ge=0)


class EventThresholds(BaseModel):
    crop_padding_ratio: float = Field(ge=0.0)
    crop_max_side_px: int = Field(gt=0)
    jpeg_quality: int = Field(ge=1, le=100)
    best_view_score: Literal["confidence_x_area"]
    store_wide_frame: bool = Field(default=False)

    @field_validator("store_wide_frame")
    @classmethod
    def check_privacy(cls, v: bool) -> bool:
        if v is not False:
            raise ValueError("store_wide_frame must be false (privacy rule NFR-06)")
        return v


class MergeThresholds(BaseModel):
    radius_m: float = Field(gt=0.0)
    location_estimate: Literal["confidence_weighted_mean"]


class SeverityThresholdsConfig(BaseModel):
    area_ratio_saturation: float = Field(gt=0.0)
    repeat_saturation: int = Field(ge=1)
    weight_area: float = Field(ge=0.0)
    weight_confidence: float = Field(ge=0.0)
    weight_repeat: float = Field(ge=0.0)
    band_medium_min: float = Field(ge=0.0, le=1.0)
    band_high_min: float = Field(ge=0.0, le=1.0)


class PassQualityThresholdsConfig(BaseModel):
    sample_interval_s: float = Field(gt=0.0)
    window_min_m: float = Field(ge=0.0)
    window_max_m: float = Field(gt=0.0)
    max_lateral_offset_m: float = Field(gt=0.0)
    max_heading_diff_deg: float = Field(ge=0.0, le=180.0)
    min_window_samples: int = Field(ge=1)
    min_window_ok_fraction: float = Field(ge=0.0, le=1.0)
    min_sharpness: float = Field(gt=0.0)
    max_occlusion_ratio: float = Field(ge=0.0, le=1.0)
    occlusion_check_enabled: bool = True
    min_mean_luma: float = Field(ge=0.0, le=255.0)
    max_gps_accuracy_m: float = Field(gt=0.0)
    max_speed_mps: float = Field(gt=0.0)
    min_inference_fps: float = Field(ge=0.0)
    require_detector_active: bool = True


class VerificationThresholds(BaseModel):
    required_clean_passes: int = Field(ge=1)
    require_distinct_passes: bool = True
    reopen_min_event_confidence: float = Field(ge=0.0, le=1.0)
    reopen_min_events: int = Field(ge=1)


class EscalationThresholds(BaseModel):
    reminder_after_days: int = Field(ge=1)
    escalate_after_days: int = Field(ge=1)
    check_interval_minutes: int = Field(ge=1)


class EmailThresholds(BaseModel):
    initial_per_pothole: int = Field(ge=1)
    max_retries: int = Field(ge=0)


class OfflineThresholds(BaseModel):
    backoff_base_s: float = Field(gt=0.0)
    backoff_max_s: float = Field(gt=0.0)
    jitter: bool = True
    max_batch_events: int = Field(ge=1)
    max_batch_bytes: int = Field(gt=0)


class ThermalThresholds(BaseModel):
    reduce_at_status: str
    stop_at_status: str


class AuthThresholds(BaseModel):
    request_signature_tolerance_s: int = Field(ge=0)


class DemoThresholds(BaseModel):
    skip_time_enabled: bool = True


class Thresholds(BaseModel):
    version: int
    detection: DetectionThresholds
    tracking: TrackingThresholds
    event: EventThresholds
    merge: MergeThresholds
    severity: SeverityThresholdsConfig
    pass_quality: PassQualityThresholdsConfig
    verification: VerificationThresholds
    escalation: EscalationThresholds
    email: EmailThresholds
    offline: OfflineThresholds
    thermal: ThermalThresholds
    auth: AuthThresholds
    demo: DemoThresholds


def load_thresholds(file_path: Path | str) -> Thresholds:
    """Load and validate thresholds YAML file."""
    path = Path(file_path)
    if not path.is_file():
        # Fallback to project root if running from backend/ or tests/
        root_path = Path(__file__).parents[2] / file_path
        if root_path.is_file():
            path = root_path
        else:
            raise FileNotFoundError(f"Thresholds file not found at: {path}")

    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        raise ValueError("Thresholds YAML must be a mapping")

    return Thresholds.model_validate(data)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    APP_ENV: Literal["development", "production"] = "development"
    LOG_LEVEL: str = "INFO"
    THRESHOLDS_PATH: str = "config/thresholds.yaml"

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://civiclens:civiclens@localhost:5432/civiclens"

    # Storage
    STORAGE_ENDPOINT: str = "http://localhost:9000"
    STORAGE_REGION: str = "auto"
    STORAGE_BUCKET: str = "civiclens-crops"
    STORAGE_ACCESS_KEY: str = "minioadmin"
    STORAGE_SECRET_KEY: str = "minioadmin"
    STORAGE_PRESIGN_TTL_S: int = 3600

    # URLs
    API_BASE_URL: str = "http://localhost:8000"
    PUBLIC_BASE_URL: str = "http://localhost:3000"

    # Email
    EMAIL_PROVIDER: Literal["resend", "console"] = "console"
    RESEND_API_KEY: str = ""
    EMAIL_FROM: str = "CivicLens <onboarding@resend.dev>"
    EMAIL_MODE: Literal["test", "production"] = "test"
    TEST_INBOX: str = "test@example.com"

    # Secrets
    ADMIN_SECRET: str = "change-me"
    FIXED_TOKEN_SECRET: str = "change-me"

    # Demo
    DEMO_MODE: bool = True
