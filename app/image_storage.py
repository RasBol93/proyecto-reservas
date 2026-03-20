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
    """
    Prioridad:
    1) image_storage_provider en Tenants
    2) env IMAGE_STORAGE_PROVIDER
    3) si hay product_photos_drive_folder_id => google_drive
    4) fallback => cloudinary
    """
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
    """
    Busca credenciales en este orden:
    1) columnas Tenants
    2) variables de entorno
    """
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
        raise RuntimeError(
            "Cloudinary no configurado. Faltan cloud_name/api_key/api_secret "
            "en Tenants o en variables de entorno."
        )

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
        raise RuntimeError("Cloudinary upload sin secure_url")

    return _normalize_cloudinary_url(secure_url)


# =========================================================
# GOOGLE DRIVE
# =========================================================

def _get_google_credentials_from_env():
    """
    Carga credenciales SOLO cuando realmente se usa Google Drive.
    Así no rompe el arranque del proyecto si todavía estás en Cloudinary.
    """
    raw = _safe_str(os.getenv("GCP_CREDENTIALS_JSON"))
    if not raw:
        raise RuntimeError("Missing env var GCP_CREDENTIALS_JSON")

    try:
        info = json.loads(raw)
    except Exception as e:
        raise RuntimeError(f"GCP_CREDENTIALS_JSON is not valid JSON: {e}")

    return service_account.Credentials.from_service_account_info(
        info,
        scopes=GOOGLE_SCOPES,
    )


def get_drive_service():
    creds = _get_google_credentials_from_env()
    return build("drive", "v3", credentials=creds)


def upload_product_photo_to_drive(
    folder_id: str,
    tenant_id: str,
    sku: str,
    file_bytes: bytes,
    mime_type: str = "image/jpeg",
) -> str:
    if not _safe_str(folder_id):
        raise RuntimeError("folder_id vacío para Google Drive")

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


# =========================================================
# FUNCIÓN ÚNICA PARA EL WEBHOOK
# =========================================================

def upload_product_photo_for_tenant(
    tenant: Dict[str, Any],
    tenant_id: str,
    sku: str,
    file_bytes: bytes,
    mime_type: str = "image/jpeg",
) -> str:
    provider = _get_storage_provider(tenant)

    if provider == "google_drive":
        folder_id = _safe_str(tenant.get("product_photos_drive_folder_id"))
        if not folder_id:
            raise RuntimeError(
                "Proveedor google_drive seleccionado pero falta product_photos_drive_folder_id"
            )
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
