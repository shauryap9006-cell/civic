"""Object storage interface for MinIO / S3-compatible backends."""

from functools import lru_cache

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

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
