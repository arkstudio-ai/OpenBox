"""Use the configured blob backend; original uploads never become public URLs."""
from contextlib import asynccontextmanager
import inspect

from core import config as runtime_config
from memory.documents.parser import DocumentError, MAX_BYTES


@asynccontextmanager
async def document_storage():
    config = runtime_config.get_config()
    if config.blob_provider == "local":
        from blob.local_blob import LocalBlobStorage
        store = LocalBlobStorage(config.blob_local_path)
    elif config.blob_provider == "azure" and config.blob_azure_connection_string:
        from blob.azure_blob import AzureBlobStorage
        store = AzureBlobStorage(config.blob_azure_connection_string, config.blob_azure_container)
    elif config.blob_provider == "gcs" and config.gcs_bucket:
        from blob.gcs_blob import GCSBlobStorage
        store = GCSBlobStorage(config.gcs_bucket)
    else:
        raise DocumentError("document_storage_unavailable")
    try:
        yield store
    finally:
        if hasattr(store, "close"):
            await store.close()


async def download_bytes(store, key):
    value = store.download(key)
    if inspect.isawaitable(value):
        data = await value
    else:
        parts, size = [], 0
        async for piece in value:
            size += len(piece)
            if size > MAX_BYTES:
                raise DocumentError("document_size_limit")
            parts.append(piece)
        data = b"".join(parts)
    if len(data) > MAX_BYTES:
        raise DocumentError("document_size_limit")
    return data
