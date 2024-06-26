from datetime import datetime, timedelta
import mimetypes
import os
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Iterable, Optional, Tuple, Union


from ..client import Client, register_client_class
from ..cloudpath import implementation_registry
from ..enums import FileCacheMode
from ..exceptions import MissingCredentialsError
from .azblobpath import AzureBlobPath


try:
    from azure.core.exceptions import ResourceNotFoundError
    from azure.storage.blob import (
        BlobSasPermissions,
        BlobServiceClient,
        BlobProperties,
        ContentSettings,
        generate_blob_sas,
    )
except ModuleNotFoundError:
    implementation_registry["azure"].dependencies_loaded = False


@register_client_class("azure")
class AzureBlobClient(Client):
    """Client class for Azure Blob Storage which handles authentication with Azure for
    [`AzureBlobPath`](../azblobpath/) instances. See documentation for the
    [`__init__` method][cloudpathlib.azure.azblobclient.AzureBlobClient.__init__] for detailed
    authentication options.
    """

    def __init__(
        self,
        account_url: Optional[str] = None,
        credential: Optional[Any] = None,
        connection_string: Optional[str] = None,
        blob_service_client: Optional["BlobServiceClient"] = None,
        file_cache_mode: Optional[Union[str, FileCacheMode]] = None,
        local_cache_dir: Optional[Union[str, os.PathLike]] = None,
        content_type_method: Optional[Callable] = mimetypes.guess_type,
    ):
        """Class constructor. Sets up a [`BlobServiceClient`](
        https://docs.microsoft.com/en-us/python/api/azure-storage-blob/azure.storage.blob.blobserviceclient?view=azure-python).
        Supports the following authentication methods of `BlobServiceClient`.

        - Environment variable `""AZURE_STORAGE_CONNECTION_STRING"` containing connecting string
        with account credentials. See [Azure Storage SDK documentation](
        https://docs.microsoft.com/en-us/azure/storage/blobs/storage-quickstart-blobs-python#copy-your-credentials-from-the-azure-portal).
        - Account URL via `account_url`, authenticated either with an embedded SAS token, or with
        credentials passed to `credentials`.
        - Connection string via `connection_string`, authenticated either with an embedded SAS
        token or with credentials passed to `credentials`.
        - Instantiated and already authenticated [`BlobServiceClient`](
        https://docs.microsoft.com/en-us/python/api/azure-storage-blob/azure.storage.blob.blobserviceclient?view=azure-python).

        If multiple methods are used, priority order is reverse of list above (later in list takes
        priority). If no methods are used, a [`MissingCredentialsError`][cloudpathlib.exceptions.MissingCredentialsError]
        exception will be raised raised.

        Args:
            account_url (Optional[str]): The URL to the blob storage account, optionally
                authenticated with a SAS token. See documentation for [`BlobServiceClient`](
                https://docs.microsoft.com/en-us/python/api/azure-storage-blob/azure.storage.blob.blobserviceclient?view=azure-python).
            credential (Optional[Any]): Credentials with which to authenticate. Can be used with
                `account_url` or `connection_string`, but is unnecessary if the other already has
                an SAS token. See documentation for [`BlobServiceClient`](
                https://docs.microsoft.com/en-us/python/api/azure-storage-blob/azure.storage.blob.blobserviceclient?view=azure-python)
                or [`BlobServiceClient.from_connection_string`](
                https://docs.microsoft.com/en-us/python/api/azure-storage-blob/azure.storage.blob.blobserviceclient?view=azure-python#from-connection-string-conn-str--credential-none----kwargs-).
            connection_string (Optional[str]): A connection string to an Azure Storage account. See
                [Azure Storage SDK documentation](
                https://docs.microsoft.com/en-us/azure/storage/blobs/storage-quickstart-blobs-python#copy-your-credentials-from-the-azure-portal).
            blob_service_client (Optional[BlobServiceClient]): Instantiated [`BlobServiceClient`](
                https://docs.microsoft.com/en-us/python/api/azure-storage-blob/azure.storage.blob.blobserviceclient?view=azure-python).
            file_cache_mode (Optional[Union[str, FileCacheMode]]): How often to clear the file cache; see
                [the caching docs](https://cloudpathlib.drivendata.org/stable/caching/) for more information
                about the options in cloudpathlib.eums.FileCacheMode.
            local_cache_dir (Optional[Union[str, os.PathLike]]): Path to directory to use as cache
                for downloaded files. If None, will use a temporary directory. Default can be set with
                the `CLOUDPATHLIB_LOCAL_CACHE_DIR` environment variable.
            content_type_method (Optional[Callable]): Function to call to guess media type (mimetype) when
                writing a file to the cloud. Defaults to `mimetypes.guess_type`. Must return a tuple (content type, content encoding).
        """
        super().__init__(
            local_cache_dir=local_cache_dir,
            content_type_method=content_type_method,
            file_cache_mode=file_cache_mode,
        )

        if connection_string is None:
            connection_string = os.getenv("AZURE_STORAGE_CONNECTION_STRING", None)

        if blob_service_client is not None:
            self.service_client = blob_service_client
        elif connection_string is not None:
            self.service_client = BlobServiceClient.from_connection_string(
                conn_str=connection_string, credential=credential
            )
        elif account_url is not None:
            self.service_client = BlobServiceClient(account_url=account_url, credential=credential)
        else:
            raise MissingCredentialsError(
                "AzureBlobClient does not support anonymous instantiation. "
                "Credentials are required; see docs for options."
            )

    def _get_metadata(self, cloud_path: AzureBlobPath) -> Union["BlobProperties", Dict[str, Any]]:
        blob = self.service_client.get_blob_client(
            container=cloud_path.container, blob=cloud_path.blob
        )
        properties = blob.get_blob_properties()

        properties["content_type"] = properties.content_settings.content_type

        return properties

    @staticmethod
    def _partial_filename(local_path) -> Path:
        return Path(str(local_path) + ".part")

    def _download_file(
        self, cloud_path: AzureBlobPath, local_path: Union[str, os.PathLike]
    ) -> Path:
        blob = self.service_client.get_blob_client(
            container=cloud_path.container, blob=cloud_path.blob
        )

        download_stream = blob.download_blob()

        local_path = Path(local_path)

        local_path.parent.mkdir(exist_ok=True, parents=True)

        try:
            partial_local_path = self._partial_filename(local_path)
            with partial_local_path.open("wb") as data:
                download_stream.readinto(data)

            partial_local_path.replace(local_path)
        except:  # noqa: E722
            # remove any partial download
            if partial_local_path.exists():
                partial_local_path.unlink()
            raise

        return local_path

    def _is_file_or_dir(self, cloud_path: AzureBlobPath) -> Optional[str]:
        # short-circuit the root-level container
        if not cloud_path.blob:
            return "dir"

        try:
            metadata = self._get_metadata(cloud_path)
            assert isinstance(metadata, BlobProperties)

            is_folder = metadata.content_settings.content_type is None and metadata.content_settings.content_md5 is None

            if is_folder:
                return "dir"

            return "file"
        except ResourceNotFoundError:
            prefix = cloud_path.blob
            if prefix and not prefix.endswith("/"):
                prefix += "/"

            # not a file, see if it is a directory
            container_client = self.service_client.get_container_client(cloud_path.container)

            try:
                next(container_client.list_blobs(name_starts_with=prefix))
                return "dir"
            except StopIteration:
                return None

    def _exists(self, cloud_path: AzureBlobPath) -> bool:
        # short circuit when only the container
        if not cloud_path.blob:
            return self.service_client.get_container_client(cloud_path.container).exists()

        return self._is_file_or_dir(cloud_path) in ["file", "dir"]

    def _list_dir(
        self, cloud_path: AzureBlobPath, recursive: bool = False
    ) -> Iterable[Tuple[AzureBlobPath, bool]]:
        container_client = self.service_client.get_container_client(cloud_path.container)

        prefix = cloud_path.blob
        if prefix and not prefix.endswith("/"):
            prefix += "/"

        if not recursive:
            blobs = container_client.walk_blobs(name_starts_with=prefix)
        else:
            blobs = container_client.list_blobs(name_starts_with=prefix)

        for blob in blobs:
            # walk_blobs returns folders with a trailing slash
            blob_path = blob.name.rstrip("/")
            blob_cloud_path = self.CloudPath(f"az://{cloud_path.container}/{blob_path}")
            yield blob_cloud_path, blob_cloud_path.is_dir()

    def _move_file(
        self, src: AzureBlobPath, dst: AzureBlobPath, remove_src: bool = True
    ) -> AzureBlobPath:
        # just a touch, so "REPLACE" metadata
        if src == dst:
            blob_client = self.service_client.get_blob_client(
                container=src.container, blob=src.blob
            )

            blob_client.set_blob_metadata(
                metadata=dict(last_modified=str(datetime.utcnow().timestamp()))
            )

        else:
            target = self.service_client.get_blob_client(container=dst.container, blob=dst.blob)

            source = self.service_client.get_blob_client(container=src.container, blob=src.blob)

            target.start_copy_from_url(source.url)

            if remove_src:
                self._remove(src)

        return dst

    def _remove(self, cloud_path: AzureBlobPath, missing_ok: bool = True) -> None:  # type: ignore
        container_client = self.service_client.get_container_client(cloud_path.container)
        file_or_dir = self._is_file_or_dir(cloud_path)

        if not file_or_dir:
            if missing_ok:
                return

            raise FileNotFoundError(f"File does not exist: {cloud_path}")

        if file_or_dir == "dir":
            blobs = [(blob.blob, is_dir) for blob, is_dir in self._list_dir(cloud_path, recursive=True)]

            # need to delete files first to allow deleting the folders
            files = [blob for blob, is_dir in blobs if not is_dir]
            container_client.delete_blobs(*files)

            # folders need to be deleted from the deepest to the shallowest
            folders = sorted((blob for blob, is_dir in blobs if is_dir), reverse=True)
            for folder in folders:
                container_client.delete_blob(folder)

        # delete the cloud_path itself
        container_client.delete_blob(cloud_path.blob)

    def _upload_file(
        self, local_path: Union[str, os.PathLike], cloud_path: AzureBlobPath
    ) -> AzureBlobPath:
        blob = self.service_client.get_blob_client(
            container=cloud_path.container, blob=cloud_path.blob
        )

        extra_args = {}
        if self.content_type_method is not None:
            content_type, content_encoding = self.content_type_method(str(local_path))

            if content_type is not None:
                extra_args["content_type"] = content_type
            if content_encoding is not None:
                extra_args["content_encoding"] = content_encoding

        content_settings = ContentSettings(**extra_args)

        with Path(local_path).open("rb") as data:
            blob.upload_blob(data, overwrite=True, content_settings=content_settings)  # type: ignore

        return cloud_path

    def _get_public_url(self, cloud_path: AzureBlobPath) -> str:
        blob_client = self.service_client.get_blob_client(
            container=cloud_path.container, blob=cloud_path.blob
        )
        return blob_client.url

    def _generate_presigned_url(
        self, cloud_path: AzureBlobPath, expire_seconds: int = 60 * 60
    ) -> str:
        sas_token = generate_blob_sas(
            self.service_client.account_name,
            container_name=cloud_path.container,
            blob_name=cloud_path.blob,
            account_key=self.service_client.credential.account_key,
            permission=BlobSasPermissions(read=True),
            expiry=datetime.utcnow() + timedelta(seconds=expire_seconds),
        )
        url = f"{self._get_public_url(cloud_path)}?{sas_token}"
        return url


AzureBlobClient.AzureBlobPath = AzureBlobClient.CloudPath  # type: ignore
