"""Utility class for interacting with Google Cloud Storage."""

# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from typing import Any

from google.cloud import storage

from cxas_scrapi.core.common import Common


class GCSUtils(Common):
  """Utility class for Google Cloud Storage integrations."""

  def __init__(
      self,
      creds_path: str | None = None,
      creds_dict: dict[str, str] | None = None,
      creds: Any = None,
      scope: list[str] | None = None,
  ):
    """Initializes GCSUtils with common auth logic.

        Args:
            creds_path: Path to service account JSON file.
            creds_dict: Service account credentials as a dictionary.
            creds: Service account credentials object.
            scope: List of scopes for the credentials.
        """
    super().__init__(
        creds_path=creds_path,
        creds_dict=creds_dict,
        creds=creds,
        scope=scope,
    )
    self.client = storage.Client(credentials=self.creds,
                                 project=self.project_id)

  def upload_string(
      self,
      gcs_uri: str,
      content: str,
      content_type: str = "text/html; charset=utf-8",
  ) -> str:
    """Uploads a string to a GCS URI and returns the mtls URL.

        Args:
            gcs_uri: The full GCS URI (e.g., gs://bucket/path/to/file).
            content: The string content to upload.
            content_type: The MIME type of the content.

        Returns:
            The authenticated URL for the uploaded file.

        Raises:
            ValueError: If the GCS URI is invalid.
        """
    if not gcs_uri.startswith("gs://"):
      raise ValueError(f"Invalid GCS URI: {gcs_uri}")

    # Remove gs:// and split into bucket and blob path
    parts = gcs_uri[5:].split("/", 1)
    if len(parts) < 2:
      raise ValueError(f"Invalid GCS URI: {gcs_uri}")

    bucket_name, blob_path = parts
    bucket = self.client.get_bucket(bucket_name)
    blob = bucket.blob(blob_path)
    blob.upload_from_string(content, content_type=content_type)

    return f"https://storage.mtls.cloud.google.com/{bucket_name}/{blob_path}"
