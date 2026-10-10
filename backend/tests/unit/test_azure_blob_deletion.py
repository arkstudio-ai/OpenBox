"""Storage absence is distinct from unavailable authorization or transport."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

from azure.core.exceptions import ClientAuthenticationError, ResourceNotFoundError, ServiceRequestError
import pytest

from blob.azure_blob import AzureBlobStorage


def storage(error):
    client = SimpleNamespace(delete_blob=AsyncMock(side_effect=error),
                             get_blob_properties=AsyncMock(side_effect=error))
    store = AzureBlobStorage("unused-test-connection")
    store._container_client = SimpleNamespace(get_blob_client=lambda key: client)
    return store


@pytest.mark.parametrize("operation", ["delete", "exists"])
@pytest.mark.parametrize("error", [ClientAuthenticationError("test auth failure"), ServiceRequestError("test network failure")])
async def test_storage_failures_propagate_instead_of_reporting_absence(operation, error):
    with pytest.raises(type(error)):
        await getattr(storage(error), operation)("synthetic-file")


async def test_only_explicit_not_found_is_a_successful_absence():
    store = storage(ResourceNotFoundError("synthetic missing object"))
    assert await store.delete("synthetic-file") is None
    assert await store.exists("synthetic-file") is False
