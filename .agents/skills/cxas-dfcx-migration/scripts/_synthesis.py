# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

"""TUI wrapper around `StructuralConsolidator.synthesize_instructions`.

Per-group synthesis logic (Step 2A blueprint + Step 2B PIF XML + topology
rewrite + per-group `asyncio.wait_for` timeout) lives in
`cxas_scrapi.migration.structural_consolidator`. This module provides the
interactive review TUI that lets the user view / edit-in-`$EDITOR` /
re-synthesize per-group instructions before deploy.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile

from InquirerPy import inquirer
from InquirerPy.base.control import Choice
from rich.console import Console

from cxas_scrapi.migration.data_models import MigrationIR
from cxas_scrapi.migration.service import MigrationService
from cxas_scrapi.migration.structural_consolidator import (
    StructuralConsolidator,
    member_to_group_map,
    rewrite_agent_refs,
)

__all__ = [
    "synthesize_instructions_for_ir",
    "interactive_synthesis_review",
]

logger = logging.getLogger(__name__)


async def synthesize_instructions_for_ir(
    ir: MigrationIR,
    service: MigrationService,
    groupings: dict,
    console: Console,
    *,
    per_group_timeout_s: int | None = None,
) -> dict[str, str]:
    """Wrapper around `StructuralConsolidator.synthesize_instructions`.

    Returns the per-group status dict so the caller can surface failures.
    """
    console.print("\n[bold cyan]Synthesizing consolidated instructions…[/]")
    timeout = per_group_timeout_s or int(
        os.environ.get("SYNTHESIS_TIMEOUT_S", "600")
    )
    consolidator = StructuralConsolidator(
        service.ir, service.gemini_client, service.source_agent_data
    )
    statuses = await consolidator.synthesize_instructions(
        ir, groupings, per_group_timeout_s=timeout
    )
    for group, status in statuses.items():
        if status == "ok":
            console.print(f"  [green]✅[/] {group}")
        elif status == "timeout":
            console.print(
                f"  [yellow]⌛[/] {group} — timed out after {timeout}s "
                "(kept fallback)"
            )
        elif status == "no-context":
            console.print(
                f"  [yellow]∅[/] {group} — no source tree context "
                "(kept fallback)"
            )
        else:
            console.print(f"  [red]✗[/] {group} — {status} (kept fallback)")
    return statuses


# ---------------------------------------------------------------------------
# Interactive review (TUI only)
# ---------------------------------------------------------------------------


def _truncate(text: str, max_lines: int = 80) -> str:
    lines = (text or "").splitlines()
    if len(lines) <= max_lines:
        return text or ""
    return (
        "\n".join(lines[:max_lines])
        + f"\n… ({len(lines) - max_lines} more lines)"
    )


def _open_in_editor(initial_text: str) -> str:
    """Open $EDITOR (or vi) on a temp file. Returns the saved text."""
    editor = os.environ.get("EDITOR", "vi")
    with tempfile.NamedTemporaryFile(
        mode="w+", suffix=".xml", delete=False
    ) as tmp:
        tmp.write(initial_text or "")
        tmp_path = tmp.name
    try:
        subprocess.call([editor, tmp_path])
        with open(tmp_path) as f:
            return f.read()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


async def _resynthesize_one(
    group_name: str,
    groupings: dict,
    ir: MigrationIR,
    service: MigrationService,
) -> str:
    consolidator = StructuralConsolidator(
        service.ir, service.gemini_client, service.source_agent_data
    )
    statuses = await consolidator.synthesize_instructions(
        ir,
        {group_name: groupings[group_name]},
        per_group_timeout_s=int(os.environ.get("SYNTHESIS_TIMEOUT_S", "600")),
    )
    return statuses.get(group_name, "unknown")


async def interactive_synthesis_review(
    ir: MigrationIR,
    service: MigrationService,
    groupings: dict,
    console: Console,
) -> None:
    """Per-group view / edit / re-synthesize loop."""
    m2g = member_to_group_map(groupings)
    member_display_to_group = {
        service.ir.agents[k].display_name: g
        for k, g in m2g.items()
        if k in service.ir.agents
    }

    while True:
        names = list(ir.agents.keys())
        console.print("\n[bold]Synthesized instructions per group:[/]")
        for i, name in enumerate(names, 1):
            agent = ir.agents[name]
            console.print(
                f"  {i}. [cyan]{name}[/] "
                f"([dim]{len(agent.instruction or '')} bytes[/])"
            )

        action = inquirer.select(
            message="Action:",
            choices=[
                Choice(value="accept", name="[a]ccept all and continue"),
                Choice(value="view", name="[v]iew a group's instruction"),
                Choice(
                    value="edit", name="[e]dit a group's instruction in $EDITOR"
                ),
                Choice(
                    value="resynth", name="[r]e-synthesize a group via Gemini"
                ),
            ],
            default="accept",
        ).execute()

        if action == "accept":
            return

        target = inquirer.select(
            message="Group:",
            choices=names,
        ).execute()

        if action == "view":
            console.print()
            console.print(f"[bold]{target}[/] (truncated):")
            console.print(_truncate(ir.agents[target].instruction or ""))
        elif action == "edit":
            new_text = _open_in_editor(ir.agents[target].instruction or "")
            ir.agents[target].instruction = rewrite_agent_refs(
                new_text, m2g, member_display_to_group, target
            )
            console.print(f"[green]Updated {target} from editor.[/]")
        elif action == "resynth":
            console.print(f"Re-synthesizing {target}…")
            try:
                status = await _resynthesize_one(target, groupings, ir, service)
                if status == "ok":
                    console.print(f"[green]Re-synthesized {target}.[/]")
                else:
                    console.print(
                        f"[yellow]Re-synthesis status for {target}: {status}[/]"
                    )
            except Exception as exc:  # noqa: BLE001
                console.print(f"[red]Re-synthesis failed: {exc}[/]")
