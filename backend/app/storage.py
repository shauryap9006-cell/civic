"""Object storage interface for MinIO / S3-compatible backends.

Provides presigned PUT/GET URL generation, object existence and SHA-256 verification.
Follows EVT-02 and openapi.yaml specifications.
"""

import hashlib
import uuid
from datetime import datetime, timedelta
from functools import lru_cache

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from app.clock import clock
from app.config import Settings


@lru_cache(maxsize=1)
def get_s3_client(
    endpoint_url: str,
    access_key: str,
    secret_key: str,
    region: str = "auto",
):
    """Create and cache boto3 S3 client configured for MinIO / S3."""
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
        config=Config(signature_version="s3v4"),
    )


def check_storage_health(settings: Settings) -> bool:
    """Verify that object storage is reachable and the configured bucket exists."""
    try:
        s3 = get_s3_client(
            settings.STORAGE_ENDPOINT,
            settings.STORAGE_ACCESS_KEY,
            settings.STORAGE_SECRET_KEY,
            settings.STORAGE_REGION,
        )
        s3.head_bucket(Bucket=settings.STORAGE_BUCKET)
        return True
    except ClientError:
        return False
    except Exception:
        return False


def generate_crop_key(device_id: uuid.UUID | str | None = None) -> str:
    """Generate an unguessable object key for a road crop image."""
    crop_id = uuid.uuid4()
    if device_id:
        return f"crops/{device_id}/{crop_id}.jpg"
    return f"crops/{crop_id}.jpg"


def generate_presigned_put(
    settings: Settings,
    crop_key: str,
    content_type: str = "image/jpeg",
    expires_in_s: int | None = None,
) -> tuple[str, dict[str, str], datetime]:
    """Generate a presigned PUT URL for uploading a crop image.

    Returns:
        (upload_url, headers_dict, expires_at)
    """
    ttl = expires_in_s or settings.STORAGE_PRESIGN_TTL_S
    s3 = get_s3_client(
        settings.STORAGE_ENDPOINT,
        settings.STORAGE_ACCESS_KEY,
        settings.STORAGE_SECRET_KEY,
        settings.STORAGE_REGION,
    )
    url = s3.generate_presigned_url(
        "put_object",
        Params={
            "Bucket": settings.STORAGE_BUCKET,
            "Key": crop_key,
            "ContentType": content_type,
        },
        ExpiresIn=ttl,
        HttpMethod="PUT",
    )
    headers = {"Content-Type": content_type}
    expires_at = clock.real_now() + timedelta(seconds=ttl)
    return url, headers, expires_at


def generate_presigned_get(
    settings: Settings,
    key: str,
    expires_in_s: int = 3600,
) -> str:
    """Generate a presigned GET URL for downloading an image."""
    s3 = get_s3_client(
        settings.STORAGE_ENDPOINT,
        settings.STORAGE_ACCESS_KEY,
        settings.STORAGE_SECRET_KEY,
        settings.STORAGE_REGION,
    )
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.STORAGE_BUCKET, "Key": key},
        ExpiresIn=expires_in_s,
    )


def check_object_exists(settings: Settings, key: str) -> bool:
    """Check if an object exists in storage."""
    s3 = get_s3_client(
        settings.STORAGE_ENDPOINT,
        settings.STORAGE_ACCESS_KEY,
        settings.STORAGE_SECRET_KEY,
        settings.STORAGE_REGION,
    )
    try:
        s3.head_object(Bucket=settings.STORAGE_BUCKET, Key=key)
        return True
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code")
        if error_code in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def get_object_bytes(settings: Settings, key: str) -> bytes:
    """Retrieve raw bytes of an object from storage."""
    s3 = get_s3_client(
        settings.STORAGE_ENDPOINT,
        settings.STORAGE_ACCESS_KEY,
        settings.STORAGE_SECRET_KEY,
        settings.STORAGE_REGION,
    )
    response = s3.get_object(Bucket=settings.STORAGE_BUCKET, Key=key)
    return response["Body"].read()


def get_object_sha256(settings: Settings, key: str) -> str:
    """Compute lowercase hex SHA-256 of an object in storage."""
    body = get_object_bytes(settings, key)
    return hashlib.sha256(body).hexdigest()


def verify_crop(
    settings: Settings,
    crop_key: str,
    expected_sha256: str,
) -> tuple[bool, str]:
    """Verify that a crop exists in storage and its content matches expected_sha256.

    Returns:
        (True, "ok") on match
        (False, "crop_missing") if object doesn't exist
        (False, "crop_hash_mismatch") if SHA-256 doesn't match
    """
    if not check_object_exists(settings, crop_key):
        return False, "crop_missing"

    actual_sha256 = get_object_sha256(settings, crop_key)
    if actual_sha256.lower() != expected_sha256.lower():
        return False, "crop_hash_mismatch"

    return True, "ok"
