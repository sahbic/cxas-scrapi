# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

"""Shared helpers for the cxas-dfcx-migration skill scripts.

Where possible we DELEGATE to existing methods on
`cxas_scrapi.cli.migration_cli.MigrationCLI` instead of reimplementing them:

  * `auth_check`              → MigrationCLI.check_auth
  * `display_status_table`    → MigrationCLI.display_status
  * `run_dependency_analysis` → MigrationCLI.run_dependency_analysis
  * `select_resources`        → MigrationCLI.select_resources
  * `show_visualizations`     → MigrationCLI.show_visualizations

What we KEEP skill-local because the CLI version has materially different
behaviour we don't want:

  * `load_source_agent_inquirer` — CLI's source-load flow lives inline inside
    `MigrationCLI.run` (not exposed as a method) and uses rich.Prompt; our
    version uses InquirerPy.
  * `collect_migration_inputs` — `MigrationCLI.compose_config` doesn't ask
    for `location` (silently hardcodes `us` deeper in the call), uses
    rich.Prompt instead of InquirerPy, and conflates --gen-* CLI flags with
    interactive prompts.
"""

from __future__ import annotations

import functools
import os
import sys
from datetime import datetime
from typing import Any

import _prompts  # InquirerPy-backed prompts (skill-local module)
from rich.console import Console
from rich.prompt import Prompt

from cxas_scrapi.cli.migration_cli import MigrationCLI
from cxas_scrapi.migration.config import AGENT_MODELS, DEFAULT_MODEL
from cxas_scrapi.migration.data_models import DFCXAgentIR, MigrationIR
from cxas_scrapi.migration.dfcx_dep_analyzer import DependencyAnalyzer
from cxas_scrapi.migration.dfcx_exporter import ConversationalAgentsAPI


@functools.lru_cache(maxsize=1)
def _cli() -> MigrationCLI:
    """Lazy-instantiate MigrationCLI once per process. Used to delegate the
    presentation methods (auth, status table, dep analysis, resource picker,
    visualization) instead of reimplementing them in this skill.

    `MigrationCLI.__init__` calls `logging.basicConfig` which is idempotent
    after the first call, so this is safe even when our scripts have already
    set up logging."""
    return MigrationCLI()


def load_source_agent(
    args, console: Console
) -> tuple[DFCXAgentIR, str, ConversationalAgentsAPI]:
    """Load source agent data from --source-agent-id, --zip-file, or prompt.

    Returns (agent_data, agent_id, exporter).
    """
    cx_api = ConversationalAgentsAPI()
    zip_path = getattr(args, "zip_file", None)
    agent_id = getattr(args, "source_agent_id", None)

    if not zip_path and not agent_id:
        choice = Prompt.ask(
            "Load source agent from",
            choices=["ID", "Zip File"],
            default="ID",
        )
        if choice == "ID":
            agent_id = Prompt.ask("Enter Source Agent ID")
        else:
            zip_path = Prompt.ask("Enter path to local agent export (.zip)")

    if zip_path:
        path = os.path.expanduser(zip_path)
        console.print(f"Loading agent from zip: {path}")
        with open(path, "rb") as f:
            agent_data = cx_api.process_local_agent_zip(f.read())
        agent_id = "uploaded-agent"
    else:
        console.print(f"Loading Agent ID: {agent_id}")
        agent_data = cx_api.fetch_full_agent_details(agent_id, use_export=True)

    if not agent_data:
        console.print("[red]Failed to load source agent.[/]")
        sys.exit(1)

    console.print("[green]Agent data loaded successfully.[/]")
    return agent_data, agent_id, cx_api


def collect_common_inputs(
    args, console: Console, default_target_prefix: str = "migrated_agent"
) -> dict[str, str]:
    """Prompt for project_id / target_name / env / model, honoring CLI args."""
    default_target = (
        f"{default_target_prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    )
    project_id = args.project_id or Prompt.ask("Enter Google Cloud Project ID")
    target_name = args.target_name or Prompt.ask(
        "Enter Target Agent Name", default=default_target
    )
    env = args.env or Prompt.ask(
        "Enter Environment", choices=["PROD", "AUTOPUSH"], default="PROD"
    )
    model = args.model or Prompt.ask(
        "Enter Global App Model", choices=AGENT_MODELS, default=DEFAULT_MODEL
    )
    return {
        "project_id": project_id,
        "target_name": target_name,
        "env": env,
        "model": model,
    }


def display_status_table(
    ir: MigrationIR, console: Console, title: str = "Resources Status"
) -> None:
    """Delegate to MigrationCLI.display_status (`title` is currently fixed
    inside the CLI helper; preserved here for callsite ergonomics)."""
    cli = _cli()
    cli.console = console
    cli.display_status(ir)


def run_dependency_analysis(
    full_data: DFCXAgentIR,
    filtered_data: DFCXAgentIR,
    console: Console,
) -> tuple[DependencyAnalyzer, list[str], list[str]]:
    """Delegate the printing to MigrationCLI.run_dependency_analysis, then
    also build + return the analyzer + outgoing/incoming lists so downstream
    callers (e.g. build_dep_summary) don't have to recompute them.

    The CLI method only prints; it doesn't return the structured result.
    """
    cli = _cli()
    cli.console = console
    cli.run_dependency_analysis(full_data, filtered_data)

    analyzer = DependencyAnalyzer(full_data)
    selected_ids: list[str] = [pb.get("name") for pb in filtered_data.playbooks]
    selected_ids += [f.flow_data.get("name") for f in filtered_data.flows]
    outgoing, incoming = analyzer.get_impact(selected_ids)
    return analyzer, outgoing, incoming


def select_resources(agent_data: DFCXAgentIR, console: Console) -> DFCXAgentIR:
    """Delegate to MigrationCLI.select_resources for the include/exclude
    picker. (rich.Prompt-based; consistent with how `cxas migrate dfcx-cxas`
    behaves.)"""
    cli = _cli()
    cli.console = console
    return cli.select_resources(agent_data)


def show_visualizations(prefix: str, console: Console) -> None:
    """Delegate to MigrationCLI.show_visualizations — prints the SVG /
    markdown paths that MainVisualizer.export_visualizations just wrote."""
    cli = _cli()
    cli.console = console
    cli.show_visualizations(prefix)


def build_dep_summary(
    analyzer: DependencyAnalyzer, ir: MigrationIR
) -> dict[str, Any]:
    """Build a JSON-serializable summary of the dependency graph keyed by IR
    agent display name. Used to enrich the Gemini grouping prompt so it can
    reason about which source resources actually reference each other.
    """
    name_to_resource = {}
    for resource_id, display_name in analyzer.name_map.items():
        name_to_resource[display_name] = resource_id

    summary: dict[str, dict[str, list[str]]] = {}
    for ir_key in ir.agents:
        resource_id = name_to_resource.get(ir_key)
        if not resource_id:
            continue
        outgoing = sorted(
            analyzer.name_map.get(t, t)
            for t in analyzer.graph.get(resource_id, set())
        )
        incoming = sorted(
            analyzer.name_map.get(s, s)
            for s in analyzer.reverse_graph.get(resource_id, set())
        )
        if outgoing or incoming:
            summary[ir_key] = {
                "references": outgoing,
                "referenced_by": incoming,
            }
    return summary


# ---------------------------------------------------------------------------
# InquirerPy-based helpers (used by migrate / stage1 / stage2)
# ---------------------------------------------------------------------------


def prompt_project_and_location(args, console: Console) -> tuple[str, str]:
    """Ask for project_id + location upfront — the missing piece in the
    legacy skill. CLI flags (`--project-id`, `--location`) are honored as
    defaults / overrides. Default location is `us`
    (matches `MigrationCLI.run`)."""
    project_id = getattr(args, "project_id", None)
    location = getattr(args, "location", None) or _prompts.DEFAULT_LOCATION

    if not project_id:
        project_id = _prompts.prompt_project_id()
    if not getattr(args, "location", None):
        # Only prompt for location interactively if it wasn't on the CLI;
        # else trust the flag (lets non-interactive runs work).
        if _prompts.is_interactive():
            location = _prompts.prompt_location(default=location)

    console.print(
        f"[dim]Using[/] [cyan]project[/]=[bold]{project_id}[/] "
        f"[cyan]location[/]=[bold]{location}[/]"
    )
    return project_id, location


def load_source_agent_inquirer(
    args, console: Console
) -> tuple[DFCXAgentIR, str, ConversationalAgentsAPI]:
    """InquirerPy version of load_source_agent. Honors --source-agent-id /
    --zip-file CLI flags as overrides; otherwise prompts."""
    cx_api = ConversationalAgentsAPI()
    zip_path = getattr(args, "zip_file", None)
    agent_id = getattr(args, "source_agent_id", None)

    if not zip_path and not agent_id:
        mode = _prompts.prompt_source_load_mode()
        if mode == "ID":
            agent_id = _prompts.prompt_source_agent_id()
        else:
            zip_path = _prompts.prompt_zip_path()

    if zip_path:
        path = os.path.expanduser(zip_path)
        console.print(f"Loading agent from zip: {path}")
        with open(path, "rb") as f:
            agent_data = cx_api.process_local_agent_zip(f.read())
        agent_id = "uploaded-agent"
    else:
        console.print(f"Loading Agent ID: {agent_id}")
        agent_data = cx_api.fetch_full_agent_details(agent_id, use_export=True)

    if not agent_data:
        console.print("[red]Failed to load source agent.[/]")
        sys.exit(1)

    console.print("[green]Agent data loaded successfully.[/]")
    return agent_data, agent_id, cx_api


def collect_migration_inputs(
    args,
    console: Console,
    *,
    default_target_prefix: str = "migrated_agent",
) -> dict[str, str]:
    """InquirerPy version of collect_common_inputs that honors CLI overrides
    and defaults logic version + env. Returns project / target_name / env /
    model / location / migration_version."""
    default_target = (
        f"{default_target_prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    )

    project_id, location = prompt_project_and_location(args, console)

    target_name = getattr(args, "target_name", None) or (
        _prompts.prompt_target_name(default_target)
        if _prompts.is_interactive()
        else default_target
    )
    env = getattr(args, "env", None) or (
        _prompts.prompt_env() if _prompts.is_interactive() else "PROD"
    )
    model = getattr(args, "model", None) or (
        _prompts.prompt_model(AGENT_MODELS, DEFAULT_MODEL)
        if _prompts.is_interactive()
        else DEFAULT_MODEL
    )
    migration_version = getattr(args, "migration_version", None) or (
        _prompts.prompt_logic_version() if _prompts.is_interactive() else "2.0"
    )
    return {
        "project_id": project_id,
        "location": location,
        "target_name": target_name,
        "env": env,
        "model": model,
        "migration_version": migration_version,
    }


def auth_check(console: Console) -> bool:
    """Delegate to MigrationCLI.check_auth (gcloud + DFCX client init)."""
    cli = _cli()
    cli.console = console
    return cli.check_auth()
