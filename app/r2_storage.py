import secrets
import time
from pathlib import Path
from typing import BinaryIO, Dict

import boto3
from botocore.config import Config

from app.config import (
    ENV_R2_ACCOUNT_ID,
    ENV_R2_ACCESS_KEY_ID,
    ENV_R2_SECRET_ACCESS_KEY,
    ENV_R2_BUCKET_NAME,
    ENV_R2_PUBLIC_BASE_URL,
    env_required,
)
from app.utils import log_event


def _clean_ext(filename: str, content_type: str) -> str:
    raw_ext = Path(str(filename or "")).suffix.strip().lower()
    if raw_ext and len(raw_ext) <= 10 and raw_ext.replace(".", "").isalnum():
        return raw_ext

    content_map = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "application/pdf": ".pdf",
    }
    return content_map.get(str(content_type or "").strip().lower(), ".bin")


def _public_base_url() -> str:
    configured = env_required(ENV_R2_PUBLIC_BASE_URL).rstrip("/")
    if not configured:
        raise RuntimeError(f"Missing env var: {ENV_R2_PUBLIC_BASE_URL}")
    return configured


def _build_r2_config() -> Dict[str, str]:
    account_id = env_required(ENV_R2_ACCOUNT_ID)
    access_key_id = env_required(ENV_R2_ACCESS_KEY_ID)
    secret_access_key = env_required(ENV_R2_SECRET_ACCESS_KEY)
    bucket_name = env_required(ENV_R2_BUCKET_NAME)
    public_base_url = _public_base_url()

    return {
        "account_id": account_id,
        "access_key_id": access_key_id,
        "secret_access_key": secret_access_key,
        "bucket_name": bucket_name,
        "endpoint_url": f"https://{account_id}.r2.cloudflarestorage.com",
        "public_base_url": public_base_url,
    }


def _get_r2_client(cfg: Dict[str, str]):
    return boto3.client(
        "s3",
        endpoint_url=cfg["endpoint_url"],
        aws_access_key_id=cfg["access_key_id"],
        aws_secret_access_key=cfg["secret_access_key"],
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )


def upload_payment_proof_fileobj(fileobj: BinaryIO, filename: str, content_type: str = "") -> Dict[str, str]:
    cfg = _build_r2_config()
    ext = _clean_ext(filename=filename, content_type=content_type)
    key = f"payment_proofs/{int(time.time())}_{secrets.token_hex(8)}{ext}"
    clean_content_type = str(content_type or "").strip() or "application/octet-stream"

    try:
        client = _get_r2_client(cfg)
        client.upload_fileobj(
            Fileobj=fileobj,
            Bucket=cfg["bucket_name"],
            Key=key,
            ExtraArgs={"ContentType": clean_content_type},
        )
    except Exception as e:
        log_event(
            "r2_payment_proof_upload_failed",
            key=key,
            bucket_name=cfg["bucket_name"],
            error_type=type(e).__name__,
            error=str(e),
        )
        raise RuntimeError(f"R2 upload failed: {e}") from e

    url = f"{cfg['public_base_url']}/{key}"
    log_event(
        "r2_payment_proof_uploaded",
        key=key,
        bucket_name=cfg["bucket_name"],
        content_type=clean_content_type,
        url=url,
    )
    return {
        "key": key,
        "url": url,
    }
