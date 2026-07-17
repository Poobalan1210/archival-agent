"""Amazon S3 helpers for report artifacts (plan section 20).

Thin wrapper around boto3 S3 used by the runtime to store artifacts, generate
presigned links, read previous reports, and implement the idempotency lock.
The client is injectable so the storage layer can be tested without AWS.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


class ReportStorage:
    def __init__(self, bucket: str, region: str, client: Any | None = None):
        self.bucket = bucket
        self.client = client or boto3.client("s3", region_name=region)

    # -- writes ------------------------------------------------------------

    def put_json(self, key: str, data: Any) -> str:
        body = json.dumps(data, indent=2, default=str).encode("utf-8")
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=body,
            ContentType="application/json",
            ServerSideEncryption="AES256",
        )
        return self.s3_uri(key)

    def put_html(self, key: str, html: str) -> str:
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=html.encode("utf-8"),
            ContentType="text/html; charset=utf-8",
            ServerSideEncryption="AES256",
        )
        return self.s3_uri(key)

    def put_if_absent(self, key: str, data: Any) -> bool:
        """Atomically write only if the key does not exist (lock primitive).

        Uses S3 conditional writes (``IfNoneMatch='*'``). Returns True if the
        object was created, False if it already existed.
        """
        body = json.dumps(data, default=str).encode("utf-8")
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=body,
                ContentType="application/json",
                ServerSideEncryption="AES256",
                IfNoneMatch="*",
            )
            return True
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in {"PreconditionFailed", "412"}:
                return False
            raise

    # -- reads -------------------------------------------------------------

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise

    def get_json(self, key: str) -> dict | None:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in {"404", "NoSuchKey"}:
                return None
            raise
        return json.loads(response["Body"].read())

    def list_keys(self, prefix: str) -> list[str]:
        keys: list[str] = []
        token: str | None = None
        while True:
            kwargs = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            response = self.client.list_objects_v2(**kwargs)
            keys.extend(obj["Key"] for obj in response.get("Contents", []))
            if not response.get("IsTruncated"):
                break
            token = response.get("NextContinuationToken")
        return keys

    def delete(self, key: str) -> None:
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
        except ClientError:
            logger.warning("Failed to delete %s", key, exc_info=True)

    # -- links -------------------------------------------------------------

    def presigned_url(self, key: str, ttl_seconds: int) -> str:
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=ttl_seconds,
        )

    def s3_uri(self, key: str) -> str:
        return f"s3://{self.bucket}/{key}"
