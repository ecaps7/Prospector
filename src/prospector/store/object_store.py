"""S3-compatible object store thin wrapper (MinIO)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import boto3
from botocore.client import BaseClient

from prospector.config import Settings, get_settings


@dataclass(frozen=True)
class StorageRef:
    bucket: str
    key: str

    def as_uri(self) -> str:
        return f"s3://{self.bucket}/{self.key}"


def workspace_key(workspace_id: UUID | str, *parts: str) -> str:
    """Build an object key with workspace prefix (design §10)."""
    clean = [str(workspace_id).strip("/")]
    clean.extend(p.strip("/") for p in parts if p)
    return "/".join(clean)


class ObjectStore:
    def __init__(self, settings: Settings | None = None, client: BaseClient | None = None) -> None:
        self._settings = settings or get_settings()
        self._client = client or self._build_client(self._settings)

    @staticmethod
    def _build_client(settings: Settings) -> BaseClient:
        return boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            region_name="us-east-1",
        )

    @property
    def bucket(self) -> str:
        return self._settings.s3_bucket

    def ensure_bucket(self) -> None:
        try:
            self._client.head_bucket(Bucket=self.bucket)
        except Exception:
            self._client.create_bucket(Bucket=self.bucket)

    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
    ) -> StorageRef:
        self._client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
        )
        return StorageRef(bucket=self.bucket, key=key)

    def get_bytes(self, key: str) -> bytes:
        resp: dict[str, Any] = self._client.get_object(Bucket=self.bucket, Key=key)
        body = resp["Body"]
        return bytes(body.read())

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:
            return False
