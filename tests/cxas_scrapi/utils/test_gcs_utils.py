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

from unittest.mock import Mock, patch

import pytest

from cxas_scrapi.utils.gcs_utils import GCSUtils


@patch("cxas_scrapi.utils.gcs_utils.storage.Client")
def test_upload_string_success(mock_client_cls):
  mock_client = mock_client_cls.return_value
  mock_bucket = Mock()
  mock_blob = Mock()
  mock_client.get_bucket.return_value = mock_bucket
  mock_bucket.blob.return_value = mock_blob

  gcs = GCSUtils()
  gcs_uri = "gs://test-bucket/reports/report.html"
  content = "<html><body>Test</body></html>"

  res_url = gcs.upload_string(gcs_uri, content)

  mock_client.get_bucket.assert_called_once_with("test-bucket")
  mock_bucket.blob.assert_called_once_with("reports/report.html")
  mock_blob.upload_from_string.assert_called_once_with(
      content, content_type="text/html; charset=utf-8")
  assert "test-bucket/reports/report.html" in res_url


@patch("cxas_scrapi.utils.gcs_utils.storage.Client")
def test_upload_string_invalid_scheme(mock_client_cls):
  gcs = GCSUtils()
  with pytest.raises(ValueError, match="Invalid GCS URI"):
    gcs.upload_string("https://storage.com/file", "content")


@patch("cxas_scrapi.utils.gcs_utils.storage.Client")
def test_upload_string_no_path(mock_client_cls):
  gcs = GCSUtils()
  with pytest.raises(ValueError, match="Invalid GCS URI"):
    gcs.upload_string("gs://bucket", "content")
