#!/usr/bin/env python3
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

"""Standalone CLI script for the llm-lint skill.

This script runs AI-driven semantic and style reviews against a single
GECX sub-agent's instruction.txt file using Gemini.
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional, Tuple

# Ensure the root directory of cxas-scrapi is in the Python path if run directly
script_dir = Path(__file__).resolve().parent
repo_root = script_dir.parents[3]
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

# pylint: disable=wrong-import-position
from cxas_scrapi.utils.gemini import GeminiGenerate  # noqa: E402


def parse_args() -> argparse.Namespace:
    """Parses command line arguments.

    Returns:
        An argparse.Namespace containing parsed arguments.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Run AI-driven semantic linter on GECX sub-agent instructions."
        )
    )
    parser.add_argument(
        "--agent-dir",
        required=True,
        help="Path to the sub-agent directory containing instruction.txt.",
    )
    parser.add_argument(
        "--project-id",
        help="GCP Project ID (auto-detected if omitted).",
    )
    parser.add_argument(
        "--location",
        default="us-central1",
        help="GCP location for Vertex AI queries (default: us-central1).",
    )
    parser.add_argument(
        "--model",
        default="gemini-2.5-flash",
        help="Gemini model name to use (default: gemini-2.5-flash).",
    )
    parser.add_argument(
        "--output",
        help="Optional path to write the markdown lint report.",
    )
    return parser.parse_args()


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
    # 1. Check CLI args
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


def main() -> None:
    """Executes the main linter flow."""
    args = parse_args()
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

    # Build Prompts
    system_prompt = """You are an expert conversational AI designer and \
reviewer specializing in Google Customer Engagement Suite (GECX) agent design.
Your task is to analyze the sub-agent instructions (`instruction.txt`) \
and point out any errors, style issues, and ambiguities.

Please evaluate the instruction text according to the following Criteria:

1. BASIC ERRORS:
   - Typos: spelling errors or typos.
   - Grammar Errors: grammatical issues that may cause user or model \
confusion.

2. INSTRUCTION STYLE:
   - Length: Identify overly long, verbose, or repetitive instructions. \
Suggest ways to condense them without losing key constraints or details.
   - Task Decomposition: Ensure complex workflows are broken down into \
sequential, numbered steps. Crucially, check that steps use ordered \
numbering with nesting (e.g., 1., 1.1., 1.2.) rather than flat lists or \
paragraphs.
   - Completeness & Edge Cases: Identify underspecified instructions, such \
as conditional `if-then` statements without a clear fallback `else` or \
fallback action when a condition isn't met.
   - Clarity & Ambiguity: Identify abbreviations, specialized jargon, or \
slang that lacks a clear, singular meaning.
   - Contradictions: Identify directives that contradict each other.

3. EXAMPLES:
   - Redundant Examples: Sample conversations or user logs that repeat \
standard instructions without demonstrating unique edge cases.
   - Conflicting Examples: Examples that contradict rules defined in the \
instructions.

Provide your response as a structured markdown report containing these \
sections:
- SUMMARY: A high-level score (e.g., out of 100) and a brief 2-3 sentence \
assessment of instruction quality.
- BASIC ERRORS: Table or list of typos, misspellings, and grammar bugs, \
with exact line or text snippets and recommended fixes. If none, state \
"No issues found."
- INSTRUCTION STYLE: Detailed review of length, task decomposition, \
completeness, ambiguity, and contradictions, pointing out specific \
instructions and explaining how to correct them. Provide a concrete \
rewrite suggestion for the problematic sections using proper nested \
numbering.
- EXAMPLES: Review of any examples provided, flagging redundancies or \
conflicts.
"""

    user_prompt = f"""Please lint the following GECX sub-agent instructions:

--- BEGIN INSTRUCTION.TXT ---
{instruction_content}
--- END INSTRUCTION.TXT ---
"""

    print(
        "Running semantic review using Gemini (this may take a few seconds)..."
    )
    report = gemini_client.generate(
        prompt=user_prompt,
        system_prompt=system_prompt,
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


if __name__ == "__main__":
    main()
