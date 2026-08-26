"""Document image storage.

A Cloud Run container's filesystem is ephemeral and per-instance, so an uploaded
prescription cannot live there: the next request may land on a different
instance, and a scale-to-zero event takes the file with it. Images therefore go
to Cloud Storage in asia-south1, alongside Firestore, so BR-19 holds for the
images as well as for the extracted text.

The local implementation exists so the test suite and `run_local.sh` need no
cloud account at all.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Protocol

#: Cloud Storage object names are flat; a patient prefix keeps BR-20's
#: "delete everything for this patient" a prefix listing rather than a scan.
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def blob_key(patient_id: str, document_id: str, filename: str) -> str:
    suffix = Path(_SAFE.sub("_", filename or "")).suffix.lower() or ".bin"
    return f"patients/{_SAFE.sub('_', patient_id)}/{_SAFE.sub('_', document_id)}{suffix}"


def content_digest(data: bytes) -> str:
    """Used to spot the same document uploaded twice in one batch."""
    return hashlib.sha256(data).hexdigest()[:16]


class BlobStore(Protocol):
    def put(self, key: str, data: bytes, content_type: str) -> str: ...
    def get(self, key: str) -> bytes: ...
    def delete_prefix(self, prefix: str) -> int: ...


class LocalBlobStore:
    """Filesystem-backed. Used by tests and local runs."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # Keys are generated, never taken from a request, but resolve anyway so
        # a traversal cannot escape the root.
        path = (self.root / key).resolve()
        if not str(path).startswith(str(self.root.resolve())):
            raise ValueError(f"blob key escapes the root: {key!r}")
        return path

    def put(self, key: str, data: bytes, content_type: str) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return str(path)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def delete_prefix(self, prefix: str) -> int:
        base = self._path(prefix)
        removed = 0
        if base.is_dir():
            for child in sorted(base.rglob("*")):
                if child.is_file():
                    child.unlink()
                    removed += 1
        return removed


class GcsBlobStore:
    """Cloud Storage, same region as Firestore."""

    def __init__(self, bucket: str, *, project: str | None = None):
        from google.cloud import storage  # lazily: tests use LocalBlobStore

        self._client = storage.Client(project=project)
        self._bucket = self._client.bucket(bucket)
        self.bucket_name = bucket

    def put(self, key: str, data: bytes, content_type: str) -> str:
        blob = self._bucket.blob(key)
        blob.upload_from_string(data, content_type=content_type)
        return f"gs://{self.bucket_name}/{key}"

    def get(self, key: str) -> bytes:
        return self._bucket.blob(key).download_as_bytes()

    def delete_prefix(self, prefix: str) -> int:
        removed = 0
        for blob in self._client.list_blobs(self._bucket, prefix=prefix):
            blob.delete()
            removed += 1
        return removed
