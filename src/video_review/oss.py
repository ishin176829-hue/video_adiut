from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import oss2
from alibabacloud_sts20150401.client import Client as StsClient
from alibabacloud_sts20150401 import models as sts_models
from alibabacloud_credentials.client import Client as CredentialClient
from alibabacloud_tea_openapi import models as open_api_models

from .config import settings
from .utils import safe_filename


def _endpoint_url(endpoint: str) -> str:
    if endpoint.startswith(("http://", "https://")):
        return endpoint
    return f"https://{endpoint}"


def build_oss_object_key(*, prefix: str, video_id: str, filename: str) -> str:
    safe_name = safe_filename(Path(filename or "upload.mp4").name, "upload.mp4")
    suffix = Path(safe_name).suffix or ".mp4"
    stem = safe_filename(Path(safe_name).stem, video_id)
    normalized_prefix = prefix.strip("/")
    return f"{normalized_prefix}/uploads/{video_id}/original/{stem}{suffix}"


def build_oss_url(*, bucket: str, endpoint: str, object_key: str, public_host: str | None = None) -> str:
    if public_host:
        host = public_host.removeprefix("https://").removeprefix("http://").rstrip("/")
        return f"https://{host}/{object_key}"
    endpoint_url = _endpoint_url(endpoint).removesuffix("/")
    return f"{endpoint_url}/{bucket}/{object_key}"


def _resolve_aliyun_credentials() -> tuple[str, str, str | None]:
    if settings.aliyun_access_key_id and settings.aliyun_access_key_secret:
        return settings.aliyun_access_key_id, settings.aliyun_access_key_secret, None
    try:
        credentials = CredentialClient().get_credential()
    except Exception as exc:
        raise RuntimeError(
            "未配置阿里云签名凭证；请为服务器绑定 RAM/ECS 实例角色，"
            "或通过安全密钥系统注入 ALIYUN_ACCESS_KEY_ID/ALIYUN_ACCESS_KEY_SECRET"
        ) from exc
    access_key_id = credentials.access_key_id
    access_key_secret = credentials.access_key_secret
    if not access_key_id or not access_key_secret:
        raise RuntimeError(
            "阿里云默认凭证链未返回 access_key_id/access_key_secret，无法调用阿里云 STS/OSS"
        )
    return access_key_id, access_key_secret, credentials.security_token


def _upload_policy(*, bucket: str, object_key: str) -> str:
    key_prefix = str(Path(object_key).parent).replace("\\", "/").rstrip("/") + "/*"
    policy = {
        "Version": "1",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "oss:PutObject",
                    "oss:InitiateMultipartUpload",
                    "oss:UploadPart",
                    "oss:CompleteMultipartUpload",
                    "oss:AbortMultipartUpload",
                    "oss:ListParts",
                ],
                "Resource": [
                    f"acs:oss:*:*:{bucket}/{object_key}",
                    f"acs:oss:*:*:{bucket}/{key_prefix}",
                ],
            }
        ],
    }
    return json.dumps(policy, ensure_ascii=False, separators=(",", ":"))


def _assume_role_sync(*, upload_id: str, object_key: str) -> dict[str, str]:
    access_key_id, access_key_secret, security_token = _resolve_aliyun_credentials()
    if not settings.aliyun_sts_role_arn:
        raise RuntimeError("未配置 ALIYUN_STS_ROLE_ARN，无法签发 OSS 临时上传凭证")
    if not settings.oss_bucket:
        raise RuntimeError("未配置 ALIYUN_OSS_BUCKET，无法签发 OSS 临时上传凭证")

    client = StsClient(
        open_api_models.Config(
            access_key_id=access_key_id,
            access_key_secret=access_key_secret,
            security_token=security_token,
            endpoint=settings.aliyun_sts_endpoint,
        )
    )
    role_session_name = f"{settings.aliyun_sts_session_name_prefix}-{upload_id}"[:64]
    response = client.assume_role(
        sts_models.AssumeRoleRequest(
            role_arn=settings.aliyun_sts_role_arn,
            role_session_name=role_session_name,
            duration_seconds=settings.oss_sts_duration_seconds,
            policy=_upload_policy(bucket=settings.oss_bucket, object_key=object_key),
        )
    )
    credentials = response.body.credentials
    return {
        "access_key_id": credentials.access_key_id,
        "access_key_secret": credentials.access_key_secret,
        "security_token": credentials.security_token,
        "expiration": credentials.expiration,
    }


async def create_upload_credentials(
    *,
    upload_id: str,
    object_key: str,
    filename: str,
    size: int,
) -> dict[str, str]:
    _ = filename, size
    return await asyncio.to_thread(_assume_role_sync, upload_id=upload_id, object_key=object_key)


def _bucket(bucket_name: str, *, endpoint: str | None = None) -> oss2.Bucket:
    access_key_id, access_key_secret, security_token = _resolve_aliyun_credentials()
    if security_token:
        auth = oss2.StsAuth(access_key_id, access_key_secret, security_token)
    else:
        auth = oss2.Auth(access_key_id, access_key_secret)
    return oss2.Bucket(auth, _endpoint_url(endpoint or settings.oss_internal_endpoint or settings.oss_endpoint), bucket_name)


def _head_sync(bucket_name: str, object_key: str) -> dict[str, Any]:
    headers = _bucket(bucket_name).head_object(object_key)
    return {
        "etag": str(getattr(headers, "etag", "") or "").strip('"') or None,
        "content_length": int(getattr(headers, "content_length", 0) or 0),
        "content_type": getattr(headers, "content_type", None),
        "last_modified": getattr(headers, "last_modified", None),
    }


async def head_oss_object(bucket: str, object_key: str) -> dict[str, Any]:
    return await asyncio.to_thread(_head_sync, bucket, object_key)


def _download_sync(bucket_name: str, object_key: str, target: Path) -> dict[str, Any]:
    target.parent.mkdir(parents=True, exist_ok=True)
    bucket = _bucket(bucket_name)
    bucket.get_object_to_file(object_key, str(target))
    headers = bucket.head_object(object_key)
    return {
        "etag": str(getattr(headers, "etag", "") or "").strip('"') or None,
        "content_length": int(getattr(headers, "content_length", 0) or target.stat().st_size),
        "content_type": getattr(headers, "content_type", None),
    }


async def download_oss_object(bucket: str, object_key: str, target: Path) -> dict[str, Any]:
    return await asyncio.to_thread(_download_sync, bucket, object_key, target)
