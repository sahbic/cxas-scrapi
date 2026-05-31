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
"""Tests for the llm-lint CLI command."""

import argparse
import json
from pathlib import Path
from unittest import mock

from cxas_scrapi.cli.llm_lint import llm_lint, resolve_gcp_credentials
from cxas_scrapi.prompts import LLM_LINT_SYSTEM_PROMPT, LLM_LINT_USER_PROMPT


def test_resolve_gcp_credentials_from_cli():
    """Test resolving credentials directly from CLI arguments."""
    agent_dir = Path("/dummy/agent")
    proj, loc = resolve_gcp_credentials(
        agent_dir,
        cli_project_id="cli-proj",
        cli_location="cli-loc",
    )
    assert proj == "cli-proj"
    assert loc == "cli-loc"


def test_resolve_gcp_credentials_from_env(monkeypatch):
    """Test resolving credentials from environment variables."""
    monkeypatch.setenv("PROJECT_ID", "env-proj")
    monkeypatch.setenv("LOCATION", "env-loc")
    agent_dir = Path("/dummy/agent")
    proj, loc = resolve_gcp_credentials(agent_dir)
    assert proj == "env-proj"
    assert loc == "env-loc"


def test_resolve_gcp_credentials_from_config(tmp_path):
    """Test resolving credentials from walking up to gecx-config.json."""
    agent_dir = tmp_path / "agents" / "my-agent"
    agent_dir.mkdir(parents=True)

    config_path = tmp_path / "gecx-config.json"
    config_data = {
        "gcp_project_id": "config-proj",
        "location": "config-loc",
    }
    config_path.write_text(json.dumps(config_data), encoding="utf-8")

    proj, loc = resolve_gcp_credentials(agent_dir)
    assert proj == "config-proj"
    assert loc == "config-loc"


@mock.patch("cxas_scrapi.cli.llm_lint.GeminiGenerate", autospec=True)
def test_llm_lint_success(mock_gemini_cls, tmp_path):
    """Test successful execution of llm_lint with mocking."""
    agent_dir = tmp_path / "agents" / "my-agent"
    agent_dir.mkdir(parents=True)
    instruction_file = agent_dir / "instruction.txt"
    instruction_file.write_text(
        "Don't use generic responses.", encoding="utf-8"
    )

    # Create active project gecx-config.json
    config_path = tmp_path / "gecx-config.json"
    config_data = {
        "gcp_project_id": "test-project",
        "location": "us-central1",
    }
    config_path.write_text(json.dumps(config_data), encoding="utf-8")

    # Stub GeminiGenerate instance
    mock_gemini_inst = mock_gemini_cls.return_value
    mock_gemini_inst.generate.return_value = "## SUMMARY\nAll good."

    args = argparse.Namespace(
        agent_dir=str(agent_dir),
        project_id=None,
        location=None,
        model="gemini-2.5-flash",
        output=None,
    )

    # Change directory to tmp_path so CWD resolution works
    with mock.patch("pathlib.Path.cwd", return_value=tmp_path):
        llm_lint(args)

    # Assertions
    mock_gemini_cls.assert_called_once_with(
        project_id="test-project",
        location="us-central1",
        model_name="gemini-2.5-flash",
    )

    expected_user_prompt = LLM_LINT_USER_PROMPT.format(
        instruction_content="Don't use generic responses."
    )
    mock_gemini_inst.generate.assert_called_once_with(
        prompt=expected_user_prompt,
        system_prompt=LLM_LINT_SYSTEM_PROMPT,
    )

    # Check if the report is written to file
    report_file = agent_dir / "llm_lint_report.md"
    assert report_file.exists()
    assert report_file.read_text(encoding="utf-8") == "## SUMMARY\nAll good."
