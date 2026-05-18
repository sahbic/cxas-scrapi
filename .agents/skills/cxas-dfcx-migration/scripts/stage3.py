#!/usr/bin/env python3
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

"""Stage 3: parent-child topology wiring for consolidated CXAS agents.

After Stage 1's consolidation collapses N source agents into M groups, the
parent-child topology of the deployed CXAS app is only as connected as
whatever the synthesized PIF XML happened to reference. In practice the LLM
often misses sibling routes (e.g. the root group only mentions one of the
sub-groups), leaving siblings unreachable.

CXAS topology rule: a transfer from sub-agent A to sub-agent B does NOT
require A to have B as a child — the conversation goes A → root → B. So
the right default is **hub-and-spoke**: the root group has every other
group as a direct child, and non-root groups have NO children. This
trivially avoids the A↔B cycles you get when you naively mirror a
bidirectional source dep graph onto CXAS child-agent links.

Two modes:

  --hub-and-spoke (default)
    Root.children = every non-root group.
    Non-root.children = [] (peer transfers route via root).

  --preserve-hierarchy
    Derive children from the source DFCX dep graph, with cycle breaking:
    iterate edges in (source-fanout) priority order and skip any edge that
    would create a cycle when added. Use this only if the source agent has
    a genuine hierarchy you want to preserve.

Idempotent: safe to re-run. Doesn't touch instructions, tools, or variables.
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
from rich.table import Table

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bundle  # noqa: E402
import _phase_tracker  # noqa: E402
import _prompts  # noqa: E402
import _shared  # noqa: E402
import stage1 as _stage1  # noqa: E402  (reuses _restore_service)

from cxas_scrapi.core.agents import Agents
from cxas_scrapi.core.apps import Apps
from cxas_scrapi.migration.dfcx_dep_analyzer import DependencyAnalyzer
from cxas_scrapi.migration.structural_consolidator import (
    member_to_group_map,
    root_group_name,
)

logger = logging.getLogger(__name__)
console = Console()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Stage 3: rewire consolidated agent parent-child topology from the "
            "source DFCX dep graph. Idempotent."
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
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--hub-and-spoke",
        dest="mode",
        action="store_const",
        const="hub",
        help=(
            "(default) Root has every non-root group as a direct child; "
            "non-root groups have no children. Peer transfers route via root."
        ),
    )
    mode.add_argument(
        "--preserve-hierarchy",
        dest="mode",
        action="store_const",
        const="hierarchy",
        help=(
            "Derive children from the source DFCX dep graph, breaking cycles. "
            "Use only when the source has a true hierarchy worth preserving."
        ),
    )
    p.set_defaults(mode="hub")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help=("Print the proposed parent → children mapping without applying."),
    )
    p.add_argument(
        "--no-set-root",
        action="store_true",
        help=("Skip resetting the app's root_agent (keep whatever is set)."),
    )
    p.add_argument("--yes", "-y", action="store_true", help="Non-interactive.")
    return p


def _resolve_bundle_path(args) -> str:
    if args.ir_bundle:
        return args.ir_bundle
    path = _bundle.find_default_bundle(args.target_name)
    if not path:
        console.print(
            "[red]No IR bundle found.[/] Run migrate.py + stage1.py first."
        )
        sys.exit(1)
    return path


# ---------------------------------------------------------------------------
# Topology computation
# ---------------------------------------------------------------------------


def _short(resource_name: str) -> str:
    return (
        resource_name.rsplit("/", maxsplit=1)[-1]
        if "/" in resource_name
        else resource_name
    )


def compute_group_children_hub_and_spoke(
    bundle: _bundle.IRBundle,
) -> dict[str, set[str]]:
    """Hub-and-spoke topology: root has every non-root group as a direct
    child; non-root groups have no children. Peer transfers route via root.

    This is the safe default — no cycles possible because the only edges
    in the graph are root → leaf, never the reverse.
    """
    if not bundle.grouping:
        raise RuntimeError(
            "Bundle has no `grouping` field — was Stage 1 consolidation run?"
        )

    root = root_group_name(bundle.grouping, root_key=None)
    if not root:
        raise RuntimeError(
            "Could not determine the root group from the grouping JSON. "
            "Set `is_root: true` on exactly one group."
        )

    children: dict[str, set[str]] = {g: set() for g in bundle.grouping}
    for g in bundle.grouping:
        if g != root:
            children[root].add(g)
    return children


def _build_raw_source_edges(
    bundle: _bundle.IRBundle,
) -> dict[str, set[str]]:
    """Walk the source DFCX dep graph and project every cross-group edge
    onto the consolidated groups. Returns parent_group → set(child_group)."""
    pre_ir = bundle.pre_consolidation_ir or bundle.ir
    m2g_keys = member_to_group_map(bundle.grouping)
    member_to_group: dict[str, str] = {}
    for ir_key, group in m2g_keys.items():
        member_to_group[ir_key] = group
        agent = pre_ir.agents.get(ir_key)
        if agent and agent.display_name:
            member_to_group[agent.display_name] = group

    analyzer = DependencyAnalyzer(bundle.source_agent_data)
    display_to_resource = {v: k for k, v in analyzer.name_map.items()}

    edges: dict[str, set[str]] = {g: set() for g in bundle.grouping}
    for group_name, payload in bundle.grouping.items():
        for member_key in payload.get("agents", []) or []:
            member_resource = display_to_resource.get(member_key)
            if not member_resource:
                agent = pre_ir.agents.get(member_key)
                if agent:
                    member_resource = display_to_resource.get(
                        agent.display_name
                    )
            if not member_resource:
                continue

            for target_resource in analyzer.graph.get(member_resource, set()):
                target_display = analyzer.name_map.get(target_resource)
                if not target_display:
                    continue
                target_group = member_to_group.get(target_display)
                if not target_group or target_group == group_name:
                    continue
                edges[group_name].add(target_group)

    return edges


def _has_path(edges: dict[str, set[str]], src: str, dst: str) -> bool:
    """Reachability check (BFS) — used by cycle breaker to test if adding
    src → dst would create a cycle (i.e. dst can already reach src)."""
    if src == dst:
        return True
    visited = {dst}
    queue = [dst]
    while queue:
        node = queue.pop(0)
        for neighbor in edges.get(node, set()):
            if neighbor == src:
                return True
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append(neighbor)
    return False


def compute_group_children_preserve_hierarchy(
    bundle: _bundle.IRBundle,
) -> dict[str, set[str]]:
    """Derive children from the source DFCX dep graph, breaking cycles.

    Strategy: collect all candidate edges from the source graph. Process
    them in priority order (edges OUT OF the root first, then edges with
    higher source-fanout) and skip any edge that would close a cycle when
    added to the accepted set. The accepted set is therefore guaranteed
    to be a DAG.

    The root agent is also forced to never appear as a child of any agent
    (so it's always reachable as the entry point and never as a sink).
    """
    if not bundle.grouping:
        raise RuntimeError(
            "Bundle has no `grouping` field — was Stage 1 consolidation run?"
        )

    root = root_group_name(bundle.grouping, root_key=None)
    raw_edges = _build_raw_source_edges(bundle)

    # Flatten edges and sort: root-out edges first, then by source group's
    # total fanout (high fanout = "router-like" agents go earlier).
    fanout = {g: len(targets) for g, targets in raw_edges.items()}
    candidates: list[tuple[str, str]] = []
    for src, targets in raw_edges.items():
        for tgt in targets:
            if tgt == root:
                # Never wire anyone to point at the root — that's how cycles
                # form, and CXAS routes peer transfers through root anyway.
                continue
            candidates.append((src, tgt))

    def _priority(edge: tuple[str, str]) -> tuple[int, int, str]:
        src, tgt = edge
        # Lower = higher priority. Root-out edges first, then by source fanout.
        return (0 if src == root else 1, -fanout.get(src, 0), src + tgt)

    candidates.sort(key=_priority)

    accepted: dict[str, set[str]] = {g: set() for g in bundle.grouping}
    for src, tgt in candidates:
        if tgt in accepted[src]:
            continue
        # Would adding src → tgt create a cycle? It would iff tgt can already
        # reach src in the current accepted graph.
        if _has_path(accepted, tgt, src):
            continue
        accepted[src].add(tgt)
    return accepted


def compute_group_children(
    bundle: _bundle.IRBundle, mode: str
) -> dict[str, set[str]]:
    """Dispatch on `--hub-and-spoke` (default) vs `--preserve-hierarchy`."""
    if mode == "hierarchy":
        return compute_group_children_preserve_hierarchy(bundle)
    return compute_group_children_hub_and_spoke(bundle)


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def apply_topology(
    bundle: _bundle.IRBundle,
    children: dict[str, set[str]],
    dry_run: bool,
    console: Console,
) -> tuple[int, int, int]:
    """Push child_agents to CXAS. Returns (updated, skipped, failed)."""
    app_resource = bundle.ir.metadata.app_resource_name
    if not app_resource:
        raise RuntimeError("Bundle has no app_resource_name; cannot push.")

    # Build group_name → resource_name map from the deployed IR.
    group_resources = {
        name: agent.resource_name
        for name, agent in bundle.ir.agents.items()
        if agent.resource_name
    }
    missing = set(children.keys()) - set(group_resources.keys())
    if missing:
        console.print(
            f"[yellow]These groups have no resource_name in the bundle "
            f"(skipped): {sorted(missing)}[/]"
        )

    # Render the proposed parent → children table.
    table = Table(title="Proposed parent → children (from source dep graph)")
    table.add_column("Parent group", style="cyan")
    table.add_column("Children", style="magenta")
    table.add_column("Count", justify="right")
    for parent, child_groups in children.items():
        names = sorted(child_groups)
        table.add_row(
            parent,
            ", ".join(names) if names else "[dim](none)[/]",
            str(len(names)),
        )
    console.print(table)

    if dry_run:
        console.print("[yellow]--dry-run set; not applying.[/]")
        return 0, 0, 0

    agents_client = Agents(app_name=app_resource)
    updated = 0
    skipped = 0
    failed = 0

    for parent, child_groups in children.items():
        parent_resource = group_resources.get(parent)
        if not parent_resource:
            skipped += 1
            continue
        child_resources = [
            group_resources[c] for c in child_groups if c in group_resources
        ]
        try:
            agents_client.update_agent(
                agent_name=parent_resource,
                child_agents=child_resources,
            )
            console.print(
                f"  [green]updated[/] {parent} → "
                f"{len(child_resources)} children"
            )
            updated += 1
        except Exception as exc:  # noqa: BLE001
            console.print(
                f"  [red]failed[/] {parent}: {str(exc).splitlines()[0][:120]}"
            )
            failed += 1

    return updated, skipped, failed


def maybe_set_root(
    bundle: _bundle.IRBundle,
    console: Console,
) -> None:
    """Set the app's root_agent to whichever group is marked is_root in
    bundle.grouping."""
    if not bundle.grouping:
        return
    rg = root_group_name(bundle.grouping, root_key=None)
    if not rg:
        return
    agent = bundle.ir.agents.get(rg)
    if not agent or not agent.resource_name:
        console.print(f"[yellow]Root group {rg!r} has no resource_name.[/]")
        return

    try:
        apps_client = Apps(
            project_id=bundle.config.project_id,
            location=_stage1._resolve_location_from_bundle(bundle),
        )
        apps_client.update_app(
            bundle.ir.metadata.app_resource_name,
            root_agent=agent.resource_name,
        )
        console.print(
            f"[green]Set app start agent → {rg} "
            f"({_short(agent.resource_name)})[/]"
        )
    except Exception as exc:  # noqa: BLE001
        console.print(f"[yellow]Failed to set app root agent: {exc}[/]")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


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

    if not bundle.grouping:
        console.print(
            "[red]Bundle has no `grouping` field.[/] Stage 3 only runs "
            "after Stage 1 consolidation. If you ran stage1.py with "
            "--no-consolidate, the original 1:1 topology from migrate.py "
            "is still in effect and Stage 3 isn't needed."
        )
        sys.exit(1)

    started_at = datetime.now()

    # Phase A: compute the topology
    mode_label = (
        "hub-and-spoke (root has all groups as direct children)"
        if args.mode == "hub"
        else "preserve-hierarchy (source dep graph, cycles broken)"
    )
    with tracker.phase("Compute topology", mode_label):
        children = compute_group_children(bundle, mode=args.mode)

    # Phase B: apply
    with tracker.phase("Apply topology", "update_agent per consolidated group"):
        updated, skipped, failed = apply_topology(
            bundle, children, args.dry_run, console
        )

    # Phase C: optionally set root
    if not args.no_set_root and not args.dry_run:
        with tracker.phase("Root agent", "set app.root_agent to is_root group"):
            maybe_set_root(bundle, console)

    # Persist (no IR mutation; just stage history).
    if not args.dry_run:
        _bundle.append_stage(
            bundle,
            "stage3",
            "ok" if failed == 0 else "partial",
            started_at,
            notes=(f"updated={updated} skipped={skipped} failed={failed}"),
        )
        _bundle.save(bundle, bundle_path)

    console.print()
    console.print(tracker.summary_table())
    console.print("\n[bold green]Stage 3 complete.[/]")
    console.print(f"  • updated={updated}, skipped={skipped}, failed={failed}")


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
