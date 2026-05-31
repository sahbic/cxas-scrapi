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
"""CLI subcommand for AI-driven semantic linter on GECX instructions."""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional, Tuple

from cxas_scrapi.prompts import LLM_LINT_SYSTEM_PROMPT, LLM_LINT_USER_PROMPT
from cxas_scrapi.utils.gemini import GeminiGenerate


def resolve_gcp_credentials(
    agent_dir: Path,
    cli_project_id: Optional[str] = None,
    cli_location: Optional[str] = None,
) -> Tuple[str, str]:
    """Resolves the GCP project ID and location using multiple fallback methods.

    Checks:
    1. Explicit CLI arguments.
    2. Standard environment variables.
    3. Walk up from agent directory to locate gecx-config.json.
    4. Root level gecx-config.json.

    Args:
        agent_dir: Path to the agent directory.
        cli_project_id: Project ID from CLI args if provided.
        cli_location: Location/region from CLI args if provided.

    Returns:
        A tuple containing (project_id, location).
    """
    project_id = cli_project_id
    location = cli_location

    # 2. Check environment variables
    if not project_id:
        project_id = os.environ.get("PROJECT_ID") or os.environ.get(
            "GOOGLE_CLOUD_PROJECT"
        )
    if not location:
        location = os.environ.get("LOCATION") or os.environ.get("REGION")

    # 3. Search for gecx-config.json by walking up from the agent directory
    current_dir = agent_dir.resolve()
    while current_dir != current_dir.parent:
        config_path = current_dir / "gecx-config.json"
        if config_path.exists():
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    config_data = json.load(f)
                    if not project_id:
                        project_id = config_data.get("gcp_project_id")
                    if not location:
                        location = config_data.get("location")
            except (json.JSONDecodeError, OSError) as e:
                print(
                    f"Warning: Failed to parse config at {config_path}: {e}",
                    file=sys.stderr,
                )
            break
        current_dir = current_dir.parent

    # 4. Try default fallback paths if still not resolved
    if not project_id or not location:
        # Walk up from current working directory to find root level config
        repo_root = Path.cwd()
        while repo_root != repo_root.parent:
            repo_config = repo_root / "gecx-config.json"
            if repo_config.exists():
                try:
                    with open(repo_config, "r", encoding="utf-8") as f:
                        config_data = json.load(f)
                        if not project_id:
                            project_id = config_data.get("gcp_project_id")
                        if not location:
                            location = config_data.get("location")
                except (json.JSONDecodeError, OSError):
                    pass
                break
            repo_root = repo_root.parent

    # Standard GECX default location if still None
    if not location or location == "<YOUR_GCP_REGION>":
        location = "us-central1"

    if not project_id or project_id == "<YOUR_GCP_PROJECT_ID>":
        print(
            "Error: GCP Project ID could not be resolved. Please provide "
            "either --project-id, set PROJECT_ID environment variable, "
            "or configure gecx-config.json in your project directory.",
            file=sys.stderr,
        )
        sys.exit(1)

    return project_id, location


def llm_lint(args: argparse.Namespace) -> None:
    """Executes the main linter flow."""
    agent_path = Path(args.agent_dir)

    if not agent_path.exists():
        print(
            f"Error: Agent directory '{args.agent_dir}' does not exist.",
            file=sys.stderr,
        )
        sys.exit(1)

    instruction_file = agent_path / "instruction.txt"
    if not instruction_file.exists():
        print(
            f"Error: Could not find instruction.txt in '{args.agent_dir}'.",
            file=sys.stderr,
        )
        sys.exit(1)

    print("------------------------------------------------------------")
    print(f"LLM LINTER — Starting analysis for agent: {agent_path.name}")
    print("------------------------------------------------------------")

    # Resolve Credentials
    project_id, location = resolve_gcp_credentials(
        agent_path, args.project_id, args.location
    )
    print(f"GCP Project : {project_id}")
    print(f"GCP Location: {location}")
    print(f"Gemini Model: {args.model}")

    # Load instruction content
    try:
        with open(instruction_file, "r", encoding="utf-8") as f:
            instruction_content = f.read()
    except OSError as e:
        print(f"Error reading instruction.txt: {e}", file=sys.stderr)
        sys.exit(1)

    if not instruction_content.strip():
        print(
            f"Warning: instruction.txt in '{args.agent_dir}' is empty.",
            file=sys.stderr,
        )
        sys.exit(0)

    # Initialize Gemini
    print("Initializing Gemini Client...")
    gemini_client = GeminiGenerate(
        project_id=project_id,
        location=location,
        model_name=args.model,
    )

    user_prompt = LLM_LINT_USER_PROMPT.format(
        instruction_content=instruction_content
    )

    print(
        "Running semantic review using Gemini (this may take a few seconds)..."
    )
    report = gemini_client.generate(
        prompt=user_prompt,
        system_prompt=LLM_LINT_SYSTEM_PROMPT,
    )

    if not report:
        print("Error: Failed to generate report from Gemini.", file=sys.stderr)
        sys.exit(1)

    print("\n============================================================")
    print("LINT REPORT GENERATED")
    print("============================================================\n")
    print(report)

    # Optionally save to file
    if args.output:
        output_path = Path(args.output)
    else:
        output_path = agent_path / "llm_lint_report.md"

    try:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(report)
        print("\n------------------------------------------------------------")
        print(f"Successfully saved report to: {output_path}")
        print("------------------------------------------------------------")
    except OSError as e:
        print(
            f"Warning: Failed to save report file to {output_path}: {e}",
            file=sys.stderr,
        )
