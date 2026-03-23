# app/image_storage.py

import io
import json
import os
import time
from typing import Any, Dict

import cloudinary
import cloudinary.uploader
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

from app.alerts import alert_system_error, alert_tenant_error


GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]


def _safe_str(v: Any) -> str:
    return str(v or "").strip()


def _guess_ext_from_mime(mime_type: str) -> str:
    mt = _safe_str(mime_type).lower()
    if mt == "image/png":
        return "png"
    if mt == "image/webp":
        return "webp"
    return "jpg"


def _normalize_cloudinary_url(url: str) -> str:
    return _safe_str(url)


def _get_storage_provider(tenant: Dict[str, Any]) -> str:
    provider = _safe_str(tenant.get("image_storage_provider")).lower()
    if provider in {"cloudinary", "google_drive"}:
        return provider

    env_provider = _safe_str(os.getenv("IMAGE_STORAGE_PROVIDER")).lower()
    if env_provider in {"cloudinary", "google_drive"}:
        return env_provider

    if _safe_str(tenant.get("product_photos_drive_folder_id")):
        return "google_drive"

    return "cloudinary"


# =========================================================
# CLOUDINARY
# =========================================================

def _configure_cloudinary_from_tenant_or_env(tenant: Dict[str, Any]) -> None:
    cloud_name = (
        _safe_str(tenant.get("cloudinary_cloud_name"))
        or _safe_str(os.getenv("CLOUDINARY_CLOUD_NAME"))
    )
    api_key = (
        _safe_str(tenant.get("cloudinary_api_key"))
        or _safe_str(os.getenv("CLOUDINARY_API_KEY"))
    )
    api_secret = (
        _safe_str(tenant.get("cloudinary_api_secret"))
        or _safe_str(os.getenv("CLOUDINARY_API_SECRET"))
    )

    if not cloud_name or not api_key or not api_secret:
        alert_tenant_error(
            tenant_id=tenant.get("tenant_id"),
            error="Cloudinary config missing (cloud_name/api_key/api_secret)",
        )
        raise RuntimeError("Cloudinary no configurado")

    cloudinary.config(
        cloud_name=cloud_name,
        api_key=api_key,
        api_secret=api_secret,
        secure=True,
    )


def upload_product_photo_to_cloudinary(
    tenant: Dict[str, Any],
    tenant_id: str,
    sku: str,
    file_bytes: bytes,
    mime_type: str = "image/jpeg",
) -> str:
    try:
        _configure_cloudinary_from_tenant_or_env(tenant)

        ext = _guess_ext_from_mime(mime_type)
        public_id = f"{tenant_id}_{sku}_{int(time.time())}"

        folder = (
            _safe_str(tenant.get("cloudinary_folder"))
            or _safe_str(os.getenv("CLOUDINARY_FOLDER"))
            or "product_photos"
        )

        result = cloudinary.uploader.upload(
            io.BytesIO(file_bytes),
            folder=folder,
            public_id=public_id,
            resource_type="image",
            overwrite=True,
            format=ext,
        )

        secure_url = _safe_str(result.get("secure_url"))
        if not secure_url:
            alert_system_error(
                error="Cloudinary upload sin secure_url",
                module="image_storage.cloudinary",
            )
            raise RuntimeError("Cloudinary upload sin secure_url")

        return _normalize_cloudinary_url(secure_url)

    except Exception as e:
        alert_system_error(
            error=str(e),
            module="image_storage.cloudinary.upload",
        )
        raise


# =========================================================
# GOOGLE DRIVE
# =========================================================

def _get_google_credentials_from_env():
    raw = _safe_str(os.getenv("GCP_CREDENTIALS_JSON"))
    if not raw:
        alert_system_error(
            error="Missing GCP_CREDENTIALS_JSON",
            module="image_storage.drive",
        )
        raise RuntimeError("Missing env var GCP_CREDENTIALS_JSON")

    try:
        info = json.loads(raw)
    except Exception as e:
        alert_system_error(
            error=f"Invalid GCP JSON: {e}",
            module="image_storage.drive",
        )
        raise RuntimeError("Invalid GCP JSON")

    return service_account.Credentials.from_service_account_info(
        info,
        scopes=GOOGLE_SCOPES,
    )


def get_drive_service():
    try:
        creds = _get_google_credentials_from_env()
        return build("drive", "v3", credentials=creds)
    except Exception as e:
        alert_system_error(
            error=str(e),
            module="image_storage.drive.service",
        )
        raise


def upload_product_photo_to_drive(
    folder_id: str,
    tenant_id: str,
    sku: str,
    file_bytes: bytes,
    mime_type: str = "image/jpeg",
) -> str:
    try:
        if not _safe_str(folder_id):
            alert_tenant_error(
                tenant_id=tenant_id,
                error="Missing product_photos_drive_folder_id",
            )
            raise RuntimeError("folder_id vacío")

        service = get_drive_service()

        ext = _guess_ext_from_mime(mime_type)
        file_name = f"{tenant_id}_{sku}_{int(time.time())}.{ext}"

        media = MediaIoBaseUpload(
            io.BytesIO(file_bytes),
            mimetype=mime_type,
            resumable=False,
        )

        created = service.files().create(
            body={
                "name": file_name,
                "parents": [folder_id],
            },
            media_body=media,
            fields="id",
            supportsAllDrives=True,
        ).execute()

        file_id = created["id"]

        service.permissions().create(
            fileId=file_id,
            body={"type": "anyone", "role": "reader"},
            supportsAllDrives=True,
        ).execute()

        return f"https://drive.google.com/uc?export=view&id={file_id}"

    except Exception as e:
        alert_system_error(
            error=str(e),
            module="image_storage.drive.upload",
        )
        raise


# =========================================================
# FUNCIÓN PRINCIPAL
# =========================================================

def upload_product_photo_for_tenant(
    tenant: Dict[str, Any],
    tenant_id: str,
    sku: str,
    file_bytes: bytes,
    mime_type: str = "image/jpeg",
) -> str:
    try:
        provider = _get_storage_provider(tenant)

        if provider == "google_drive":
            folder_id = _safe_str(tenant.get("product_photos_drive_folder_id"))
            if not folder_id:
                alert_tenant_error(
                    tenant_id=tenant_id,
                    error="Google Drive seleccionado pero falta folder_id",
                )
                raise RuntimeError("Missing Drive folder")

            return upload_product_photo_to_drive(
                folder_id=folder_id,
                tenant_id=tenant_id,
                sku=sku,
                file_bytes=file_bytes,
                mime_type=mime_type,
            )

        return upload_product_photo_to_cloudinary(
            tenant=tenant,
            tenant_id=tenant_id,
            sku=sku,
            file_bytes=file_bytes,
            mime_type=mime_type,
        )

    except Exception as e:
        alert_system_error(
            error=str(e),
            module="image_storage.main",
        )
        raise
