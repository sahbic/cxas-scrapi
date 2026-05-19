#!/usr/bin/env python3
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

"""Stage 1: CXASOptimizer variable dedup + optional Gemini consolidation.

Loads the <target>_ir.json bundle written by migrate.py (does NOT re-fetch or
re-compile the source agent), then:

  1. Runs CXASOptimizer.optimize_stage1() — global variable deduplication.
  2. (Optional) Runs the StructuralConsolidator (Gemini grouping + per-group
     PIF XML synthesis) to collapse N agents into M capability-rich groups.
  3. Pushes the resulting IR via update-pass deploys.
  4. Creates CXAS Version `0.0.1`.
  5. Persists the updated IR back to <target>_ir.json.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime

from rich.console import Console
from rich.logging import RichHandler

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bundle  # noqa: E402
import _grouping  # noqa: E402
import _optimizer_runner  # noqa: E402
import _phase_tracker  # noqa: E402
import _prompts  # noqa: E402
import _shared  # noqa: E402
import _synthesis  # noqa: E402

from cxas_scrapi.core.agents import Agents
from cxas_scrapi.core.versions import Versions
from cxas_scrapi.migration.dfcx_dep_analyzer import DependencyAnalyzer
from cxas_scrapi.migration.integrity_checks import (
    check_consolidation_integrity as _check_consolidation_integrity,
)
from cxas_scrapi.migration.service import MigrationService
from cxas_scrapi.migration.structural_consolidator import (
    StructuralConsolidator,
    detect_root_key,
    load_grouping,
    persist_grouping,
    root_group_name,
    validate_groupings,
)
from cxas_scrapi.utils.gemini import GeminiGenerate

logger = logging.getLogger(__name__)
console = Console()


def _short_id(resource_name: str) -> str:
    return (
        resource_name.rsplit("/", maxsplit=1)[-1]
        if "/" in resource_name
        else resource_name
    )


def _delete_orphan_agents(
    app_resource_name: str,
    keep_resources: set[str],
    console: Console,
    *,
    max_passes: int = 5,
) -> tuple[int, int]:
    """Delete every CXAS agent under `app_resource_name` whose resource name
    is NOT in `keep_resources`.

    CXAS rejects deleting an agent that is still listed as a child of another
    agent. We do up to `max_passes` iterations: each pass deletes whatever
    is currently a leaf (no longer referenced as a child by any remaining
    orphan), which then exposes new leaves on the next pass. Stops early
    once a pass makes no progress.

    Returns (total_deleted, remaining_undeletable).
    """
    try:
        agents_client = Agents(app_name=app_resource_name)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[yellow]Could not init Agents client: {exc}[/]")
        return 0, 0

    total_deleted = 0
    pass_num = 0
    while pass_num < max_passes:
        pass_num += 1
        try:
            live_agents = agents_client.list_agents()
        except Exception as exc:  # noqa: BLE001
            console.print(f"[yellow]list_agents failed mid-cleanup: {exc}[/]")
            break

        orphans = [a for a in live_agents if a.name not in keep_resources]
        if not orphans:
            if pass_num == 1:
                console.print("[green]No orphan agents to delete.[/]")
            break

        if pass_num == 1:
            console.print(
                f"[cyan]Deleting up to {len(orphans)} orphan agents from the "
                "1:1 migration…[/]"
            )

        deleted_this_pass = 0
        failed_this_pass: list[tuple[str, str, str]] = []
        for agent in orphans:
            short = _short_id(agent.name)
            try:
                agents_client.delete_agent(agent_name=agent.name)
                console.print(
                    f"  [dim]pass {pass_num}: deleted[/] "
                    f"{agent.display_name!r} ({short})"
                )
                deleted_this_pass += 1
                total_deleted += 1
            except Exception as exc:  # noqa: BLE001
                msg = str(exc).splitlines()[0][:80]
                failed_this_pass.append((agent.display_name, short, msg))

        if deleted_this_pass == 0:
            # No progress — remaining orphans likely cycle-linked. Surface
            # the unique failure messages and stop.
            n_failed = len(failed_this_pass)
            console.print(
                f"[yellow]Pass {pass_num}: no progress; {n_failed} "
                "orphans remain undeletable.[/]"
            )
            unique_msgs = {msg for _, _, msg in failed_this_pass}
            for msg in unique_msgs:
                console.print(f"  [dim]reason:[/] {msg}")
            return total_deleted, len(failed_this_pass)

    # Re-list to count whatever's left over after max_passes.
    try:
        live_agents = agents_client.list_agents()
        remaining = sum(1 for a in live_agents if a.name not in keep_resources)
    except Exception:  # noqa: BLE001
        remaining = 0
    return total_deleted, remaining


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Stage 1: variable dedup + optional Gemini consolidation. "
            "Loads <target>_ir.json."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    src = p.add_mutually_exclusive_group()
    src.add_argument(
        "--ir-bundle",
        help="Path to <target>_ir.json (defaults to newest in cwd)",
    )
    src.add_argument(
        "--target-name",
        help="Resolves to <target>_ir.json in the cwd",
    )
    p.add_argument("--project-id", help="Override bundle project ID")
    p.add_argument("--location", help="Override bundle location")

    p.add_argument(
        "--no-consolidate",
        action="store_true",
        help="Skip the Gemini consolidation; only run CXASOptimizer Stage 1.",
    )
    p.add_argument(
        "--no-instruction-review",
        action="store_true",
        help="Skip the per-group instruction review (view/edit/re-synthesize).",
    )
    p.add_argument(
        "--gemini-model",
        default="gemini-3.1-pro-preview",
        help=(
            "Model for the grouping proposal (default: gemini-3.1-pro-preview)"
        ),
    )
    p.add_argument(
        "--grouping-json",
        default=None,
        help="Load a previously persisted grouping instead of asking Gemini.",
    )
    p.add_argument("--yes", "-y", action="store_true", help="Non-interactive.")
    return p


def _resolve_bundle_path(args) -> str:
    if args.ir_bundle:
        return args.ir_bundle
    path = _bundle.find_default_bundle(args.target_name)
    if not path:
        console.print(
            "[red]No IR bundle found.[/] Run migrate.py first, or pass "
            "--ir-bundle / --target-name."
        )
        sys.exit(1)
    return path


def _restore_service(bundle: _bundle.IRBundle, args) -> MigrationService:
    """Delegate to `MigrationService.restore_from_bundle`, honoring the
    skill's `--project-id` / `--location` CLI overrides."""
    return MigrationService.restore_from_bundle(
        bundle,
        project_id=getattr(args, "project_id", None),
        location=getattr(args, "location", None),
    )


def _resolve_location_from_bundle(bundle: _bundle.IRBundle) -> str:
    return bundle.resolve_location(default=_prompts.DEFAULT_LOCATION)


async def _run(args) -> None:
    tracker = _phase_tracker.PhaseTracker(console)

    if not _shared.auth_check(console):
        if not args.yes and not _prompts.prompt_yes_no(
            "Proceed anyway?", default=False
        ):
            sys.exit(1)

    bundle_path = _resolve_bundle_path(args)
    console.print(f"[cyan]Loading IR bundle:[/] {bundle_path}")
    bundle = _bundle.load(bundle_path)

    target_name = bundle.config.target_name
    service = _restore_service(bundle, args)
    started_at = datetime.now()

    # ---- Stage 1A: variable dedup --------------------------------------
    with tracker.phase(
        "Stage 1 — variable dedup", "CXASOptimizer.optimize_stage1"
    ):
        try:
            stage1_optimizer = await _optimizer_runner.run_stage_with_redeploy(
                service, stage=1, console=console
            )
            _optimizer_runner.merge_optimizer_logs_into_ir(
                service.ir, stage1_optimizer, "stage1"
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Stage 1 failed: %s", exc)
            _bundle.append_stage(bundle, "stage1", "fail", started_at, str(exc))
            _bundle.save(bundle, bundle_path)
            console.print(f"[red]Stage 1 failed: {exc}[/]")
            sys.exit(2)

    # ---- Stage 1B (optional): consolidation --------------------------------
    if not args.no_consolidate:
        with tracker.phase(
            "Consolidation", "Gemini grouping + per-group PIF XML synthesis"
        ):
            try:
                gemini = GeminiGenerate(
                    project_id=service.project_id,
                    location="global",
                    model_name=args.gemini_model,
                    max_concurrent_requests=10,
                )
                consolidator = StructuralConsolidator(
                    service.ir, gemini, service.source_agent_data
                )

                root_key = detect_root_key(
                    service.ir, service.source_agent_data
                )
                analyzer = DependencyAnalyzer(service.source_agent_data)
                dep_summary = _shared.build_dep_summary(analyzer, service.ir)

                if args.grouping_json:
                    console.print(f"Loading grouping from {args.grouping_json}")
                    groupings = load_grouping(args.grouping_json)
                    validate_groupings(service.ir, groupings, root_key)
                else:
                    groupings = await consolidator.propose_groupings(
                        root_key, dep_summary
                    )

                if args.yes:
                    optimized_ir = consolidator.consolidate(groupings)
                else:
                    result = await _grouping.interactive_review(
                        service.ir,
                        groupings,
                        consolidator,
                        root_key,
                        dep_summary,
                        console,
                    )
                    if result is None:
                        console.print(
                            "[yellow]Consolidation aborted; "
                            "keeping post-Stage-1 IR.[/]"
                        )
                        optimized_ir = None
                    else:
                        optimized_ir, groupings = result

                if optimized_ir is not None:
                    grouping_path = persist_grouping(
                        groupings, f"{target_name}_grouping.json"
                    )
                    console.print(
                        f"[green]Grouping persisted → {grouping_path}[/]"
                    )
                    _bundle.attach_grouping(bundle, groupings)

                    # Snapshot the original 1:1 IR before mutation. Persisted
                    # in the bundle so the post-migrate state survives even
                    # after consolidation collapses N agents into M groups.
                    bundle.pre_consolidation_ir = service.ir.model_copy(
                        deep=True
                    )

                    await _synthesis.synthesize_instructions_for_ir(
                        optimized_ir, service, groupings, console
                    )

                    if not (args.yes or args.no_instruction_review):
                        await _synthesis.interactive_synthesis_review(
                            optimized_ir, service, groupings, console
                        )

                    # Pre-deploy integrity checks on the consolidated IR.
                    blocking, warnings_ = _check_consolidation_integrity(
                        optimized_ir, service.ir
                    )
                    if warnings_:
                        console.print(
                            "[yellow]Integrity warnings (non-blocking):[/]"
                        )
                        for w in warnings_:
                            console.print(f"  • {w}")
                    if blocking:
                        console.print("[red]Integrity errors (blocking):[/]")
                        for b in blocking:
                            console.print(f"  • {b}")
                        if not args.yes and not _prompts.prompt_yes_no(
                            "Proceed with deploy anyway?", default=False
                        ):
                            console.print(
                                "[yellow]Aborting consolidation. "
                                "Pre-consolidation IR is preserved on disk.[/]"
                            )
                            raise RuntimeError("integrity check failed")

                    # Push the consolidated agents
                    service.ir.agents = optimized_ir.agents
                    console.print(
                        "[cyan]Pushing consolidated agents to CXAS…[/]"
                    )
                    await service._deploy_base_resources(is_update_pass=True)
                    await service._deploy_pending_agents(is_update_pass=True)

                    # Wire parent → child topology by scanning the deployed
                    # agents' instructions for `{@AGENT: GroupName}` refs.
                    # Without this step the root group has no `child_agents`
                    # and can't transfer to the other groups at runtime.
                    console.print("[cyan]Linking agent topology…[/]")
                    try:
                        service.topology_linker.link_and_finalize_topology(
                            service.ir, service.source_agent_data
                        )
                    except Exception as exc:  # noqa: BLE001
                        console.print(
                            f"[yellow]Topology linking failed: {exc}[/]"
                        )

                    if root_group_name(groupings, root_key):
                        # Update app's start agent to the root group's resource.
                        rg = root_group_name(groupings, root_key)
                        agent = service.ir.agents.get(rg)
                        if agent and agent.resource_name:
                            try:
                                service.ps_apps.update_app(
                                    service.ir.metadata.app_resource_name,
                                    root_agent=agent.resource_name,
                                )
                                console.print(
                                    f"[green]Set app start agent → {rg} "
                                    f"({_short_id(agent.resource_name)})[/]"
                                )
                            except Exception as exc:  # noqa: BLE001
                                console.print(
                                    f"[yellow]Failed to set app root agent "
                                    f"{rg}: {exc}[/]"
                                )

                    # Delete orphans — original 1:1 agents that the
                    # consolidation didn't replace in place. "Keep" set is
                    # the resource names of the agents we just deployed;
                    # everything else under the app is an orphan.
                    keep = {
                        a.resource_name
                        for a in service.ir.agents.values()
                        if a.resource_name
                    }
                    deleted, failed = _delete_orphan_agents(
                        service.ir.metadata.app_resource_name,
                        keep_resources=keep,
                        console=console,
                    )
                    if deleted or failed:
                        console.print(
                            f"[cyan]Orphan cleanup: {deleted} deleted, "
                            f"{failed} failed.[/]"
                        )
            except Exception as exc:  # noqa: BLE001
                logger.error("Consolidation failed: %s", exc)
                console.print(
                    f"[yellow]Consolidation failed; Stage 1 changes are still "
                    f"deployed. Reason: {exc}[/]"
                )

    # ---- CXAS Version 0.0.1 ----------------------------------------------
    if service.ir.metadata.app_resource_name:
        with tracker.phase("Version 0.0.1", "CXAS pre-Stage-2 checkpoint"):
            try:
                Versions(service.ir.metadata.app_resource_name).create_version(
                    display_name="0.0.1",
                    description=(
                        "Stage 1: variable dedup"
                        + (
                            " + consolidation"
                            if not args.no_consolidate and bundle.grouping
                            else ""
                        )
                    ),
                )
                _bundle.attach_version(
                    bundle,
                    "0.0.1",
                    "Stage 1: variable dedup"
                    + (
                        " + consolidation"
                        if not args.no_consolidate and bundle.grouping
                        else ""
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Version 0.0.1 checkpoint failed: %s", exc)

    # ---- Persist updated bundle ------------------------------------------
    bundle.ir = service.ir
    _bundle.append_stage(
        bundle,
        "stage1",
        "ok",
        started_at,
        notes=(
            f"{len(service.ir.agents)} agents after Stage 1"
            + (" (consolidated)" if bundle.grouping else "")
        ),
    )
    _bundle.save(bundle, bundle_path)

    console.print()
    console.print(tracker.summary_table())
    console.print("\n[bold green]Stage 1 complete.[/]")
    console.print(f"  • IR bundle:        {bundle_path}")
    if bundle.grouping:
        console.print(f"  • Grouping JSON:    {target_name}_grouping.json")
    if bundle.app_url:
        console.print(f"  • App console:      {bundle.app_url}")
    console.print(
        f"\n[dim]Next:[/] [cyan]stage2.py --target-name {target_name}[/]"
        " for instruction state machines + tool mocks + lint + report."
    )


def main() -> None:
    logging.basicConfig(
        level="INFO",
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True)],
    )
    args = _build_parser().parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
