#!/usr/bin/env python3
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

"""Stage 2: instruction state machines + tool mocks + lint + report.

Loads the <target>_ir.json bundle written by migrate.py / stage1.py, then:

  1. Runs CXASOptimizer.optimize_stage2() — Playbook XML state machine
     restructuring (parallel) + Python tool `mock_mode` injection (parallel).
  2. Pushes via update-pass deploys.
  3. Creates CXAS Version `0.0.2`.
  4. Re-generates deterministic unit tests against the final agents.
  5. Runs `cxas pull` + `cxas lint`.
  6. Writes the final OptimizationReporter audit markdown.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime

from rich.console import Console
from rich.logging import RichHandler

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bundle  # noqa: E402
import _lint  # noqa: E402
import _optimizer_runner  # noqa: E402
import _phase_tracker  # noqa: E402
import _prompts  # noqa: E402
import _reporter  # noqa: E402
import _shared  # noqa: E402

from cxas_scrapi.core.agents import Agents
from cxas_scrapi.core.tools import Tools
from cxas_scrapi.core.versions import Versions
from cxas_scrapi.migration.eval_generator import DeterministicEvalGenerator
from cxas_scrapi.migration.service import MigrationService

logger = logging.getLogger(__name__)
console = Console()


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Stage 2: instruction state machines + tool mocks + lint + report."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    src = p.add_mutually_exclusive_group()
    src.add_argument("--ir-bundle", help="Path to <target>_ir.json")
    src.add_argument(
        "--target-name", help="Resolves to <target>_ir.json in cwd"
    )
    p.add_argument("--project-id", help="Override bundle project ID")
    p.add_argument("--location", help="Override bundle location")

    p.add_argument(
        "--no-unit-tests",
        action="store_true",
        help="Skip deterministic unit test regeneration.",
    )
    p.add_argument(
        "--no-lint",
        action="store_true",
        help="Skip the post-deploy `cxas pull` + `cxas lint`.",
    )
    p.add_argument(
        "--no-report",
        action="store_true",
        help="Skip the OptimizationReporter audit markdown.",
    )
    p.add_argument("--yes", "-y", action="store_true", help="Non-interactive.")
    return p


def _resolve_bundle_path(args) -> str:
    if args.ir_bundle:
        return args.ir_bundle
    path = _bundle.find_default_bundle(args.target_name)
    if not path:
        console.print(
            "[red]No IR bundle found.[/] Run migrate.py / stage1.py first."
        )
        sys.exit(1)
    return path


def _restore_service(bundle: _bundle.IRBundle, args) -> MigrationService:
    """Recreate a MigrationService from a persisted IR bundle. See stage1.py
    `_restore_service` for the attributes initialized here and why."""
    project_id = args.project_id or bundle.config.project_id
    location = args.location or _resolve_location_from_bundle(bundle)
    service = MigrationService(
        project_id=project_id,
        location=location,
        default_model=bundle.config.model,
    )
    service.ir = bundle.ir
    service.source_agent_data = bundle.source_agent_data
    service.deployment_state = {
        "app_created": True,
        "vars_deployed": True,
        "app_timeout_configured": True,
        "app_model_configured": True,
    }
    service.eval_generator = DeterministicEvalGenerator(service.ir)

    app_resource = service.ir.metadata.app_resource_name
    if app_resource:
        service.ps_agents = Agents(app_name=app_resource)
        service.ps_tools = Tools(app_name=app_resource)
        if hasattr(service, "topology_linker"):
            service.topology_linker.ps_agents = service.ps_agents
        if hasattr(service, "code_block_migrator"):
            service.code_block_migrator.ps_tools = service.ps_tools

    return service


def _resolve_location_from_bundle(bundle: _bundle.IRBundle) -> str:
    if bundle.app_url and "/locations/" in bundle.app_url:
        return bundle.app_url.split("/locations/")[1].split("/")[0]
    return _prompts.DEFAULT_LOCATION


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

    # ---- Stage 2: instruction state machines + tool mocks ----------------
    stage2_optimizer = None
    with tracker.phase(
        "Stage 2", "instruction state machines + tool mocks + redeploy"
    ):
        try:
            stage2_optimizer = await _optimizer_runner.run_stage_with_redeploy(
                service, stage=2, console=console
            )
            _optimizer_runner.merge_optimizer_logs_into_ir(
                service.ir, stage2_optimizer, "stage2"
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Stage 2 failed: %s", exc)
            _bundle.append_stage(bundle, "stage2", "fail", started_at, str(exc))
            _bundle.save(bundle, bundle_path)
            console.print(f"[red]Stage 2 failed: {exc}[/]")
            sys.exit(2)

    # ---- CXAS Version 0.0.2 ----------------------------------------------
    if service.ir.metadata.app_resource_name:
        with tracker.phase("Version 0.0.2", "CXAS post-Stage-2 checkpoint"):
            try:
                Versions(service.ir.metadata.app_resource_name).create_version(
                    display_name="0.0.2",
                    description=(
                        "Stage 2: instruction state machines + tool mocks"
                    ),
                )
                _bundle.attach_version(
                    bundle,
                    "0.0.2",
                    "Stage 2: instruction state machines + tool mocks",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Version 0.0.2 checkpoint failed: %s", exc)

    # ---- Re-generate deterministic unit tests ---------------------------
    test_path = ""
    test_counts: dict[str, int] = {}
    if not args.no_unit_tests:
        with tracker.phase("Unit tests", "DeterministicEvalGenerator"):
            try:
                gen = DeterministicEvalGenerator(service.ir)
                by_agent: dict[str, list] = {}
                for agent_name in service.ir.agents:
                    cases = gen.generate_tests_for_agent(agent_name)
                    if cases:
                        by_agent[agent_name] = [
                            tc.model_dump(mode="json") for tc in cases
                        ]
                test_path = f"{target_name}_unit_tests.json"
                with open(test_path, "w") as f:
                    json.dump(by_agent, f, indent=2, default=str)
                test_counts = {n: len(v) for n, v in by_agent.items()}
                console.print(
                    f"[green]Regenerated {sum(test_counts.values())} tests for "
                    f"{len(test_counts)} agents → {test_path}[/]"
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Unit test regeneration failed: %s", exc)

    # ---- Lint ------------------------------------------------------------
    lint_passed: bool | None = None
    lint_output = ""
    if not args.no_lint:
        with tracker.phase("Lint", "cxas pull + cxas lint"):
            try:
                lint_passed, lint_output = await _lint.run_post_deploy_lint(
                    service, console
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Lint failed to run: %s", exc)
                console.print(f"[yellow]Lint did not run: {exc}[/]")

    # ---- OptimizationReporter audit markdown -----------------------------
    report_path = ""
    if not args.no_report:
        with tracker.phase("Audit report", "OptimizationReporter markdown"):
            try:
                stage1_logs = bundle.ir.optimization_logs.get("stages", {}).get(
                    "stage1"
                )
                stage2_logs = bundle.ir.optimization_logs.get("stages", {}).get(
                    "stage2"
                )
                reporter = _reporter.OptimizationReporter()
                reporter.set_app_info(
                    "(see bundle)",
                    target_name,
                    service.ir.metadata.app_resource_name or "",
                    bundle.app_url or "",
                )
                reporter.set_grouping(
                    bundle.grouping or {},
                    before_count=len(bundle.source_agent_data.playbooks)
                    + len(bundle.source_agent_data.flows),
                    after_count=len(service.ir.agents),
                    path=f"{target_name}_grouping.json"
                    if bundle.grouping
                    else "",
                )
                reporter.set_optimizer_logs(stage1_logs, stage2_logs)
                reporter.set_version_checkpoints(bundle.version_checkpoints)
                if test_counts:
                    reporter.set_unit_test_summary(test_counts, test_path)
                if lint_passed is not None:
                    reporter.set_lint_result(lint_passed, lint_output)
                report_path = reporter.export(
                    f"{target_name}_optimization_report.md"
                )
                console.print(f"[green]Optimization report → {report_path}[/]")
            except Exception as exc:  # noqa: BLE001
                logger.warning("Report generation failed: %s", exc)

    # ---- Persist updated bundle -----------------------------------------
    bundle.ir = service.ir
    _bundle.append_stage(
        bundle,
        "stage2",
        "ok",
        started_at,
        notes=(
            "Stage 2 complete; "
            + (
                "lint passed"
                if lint_passed
                else "lint had issues"
                if lint_passed is False
                else "lint skipped"
            )
        ),
    )
    _bundle.save(bundle, bundle_path)

    console.print()
    console.print(tracker.summary_table())
    console.print("\n[bold green]Stage 2 complete.[/]")
    console.print(f"  • IR bundle:        {bundle_path}")
    if test_path:
        console.print(f"  • Unit tests:       {test_path}")
    if report_path:
        console.print(f"  • Audit report:     {report_path}")
    if bundle.app_url:
        console.print(f"  • App console:      {bundle.app_url}")


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
