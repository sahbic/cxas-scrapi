---
name: cxas-dfcx-migration
description: >-
  Migrate Dialogflow CX (DFCX) agents to CXAS (Customer Experience Agent Studio) agents.
  Use this skill when the user mentions DFCX migration, migrating agents, converting DFCX to CXAS,
  porting agents, agent migration, or post-migration optimization/consolidation. Four independently
  runnable scripts: migrate.py (1:1), stage1.py (variable dedup + consolidation), stage2.py
  (instruction state machines + tool mocks + lint + report), stage3.py (rewires consolidated
  topology from source dep graph; only needed when stage1 ran consolidation). State persists
  between scripts via <target>_ir.json so each can run / re-run / resume independently.
---

# DFCX to CXAS Migration

Four small scripts, one persistent IR bundle:

| Script | What it does | Runtime | Output |
|---|---|---|---|
| `migrate.py` | 1:1 conversion of every selected playbook/flow into a CXAS agent. | ~30 min for ~40 flows | `<target>_ir.json`, `<target>_migration_report.md`, `<target>_unit_tests.json` |
| `stage1.py` | Loads the IR bundle, runs `CXASOptimizer.optimize_stage1` (variable dedup) + optional Gemini consolidation (N→M agent grouping). Pushes via update-pass deploys. Deletes original orphans (iterative). CXAS Version `0.0.1`. | ~5 min (no consolidate) / ~15 min (with consolidate) | Updated `<target>_ir.json` (with `pre_consolidation_ir` snapshot for rollback), `<target>_grouping.json` |
| `stage2.py` | Loads the IR bundle, runs `CXASOptimizer.optimize_stage2` (instruction state machines + tool mocks). Pushes via update-pass deploys. CXAS Version `0.0.2`. Re-generates unit tests. Lints. Writes the audit report. | ~10 min | Updated `<target>_ir.json`, `<target>_optimization_report.md`, regenerated `<target>_unit_tests.json` |
| `stage3.py` | **Only after `stage1.py` ran consolidation.** Rewires the consolidated agents' parent → children topology by mapping the SOURCE DFCX dep graph onto the new groups (rather than relying on what the synthesized PIF XML happened to reference). Sets app `root_agent` to the `is_root` group. Idempotent — safe to re-run. `--dry-run` to preview. | ~10 sec | Updated `<target>_ir.json` stage history; CXAS app's `child_agents` set per group |

State flows through `<target>_ir.json` (a Pydantic `IRBundle` containing the `MigrationConfig`, source `DFCXAgentIR`, target `MigrationIR`, stage history, and version checkpoints). Each stage loads it from disk, mutates it, and writes it back. **No re-fetching or re-compiling between stages.**

## Prerequisites

```bash
# Ensure cxas_scrapi is installed editable (so this skill picks up local changes)
pip install -e .

# Auth
gcloud auth application-default login
gcloud auth list   # confirm the account has read on source + admin on target
```

InquirerPy is required for the interactive prompts (matches the agent-foundry skill):

```bash
pip install InquirerPy
```

## Driving the flow interactively (from Claude)

When invoked through Claude, lead the user through one question at a time. The scripts will prompt for missing inputs via InquirerPy, but you should pre-collect:

1. **GCP project ID** (target — where the new CXAS app will live).
2. **Location** — default `us`. **Do NOT default to `global` — it does not work for CXAS apps in most projects.**
3. **Source agent** — DFCX agent ID (full resource name) or path to a local `.zip` export.
4. **Target name** — display name for the new CXAS app.

Optional follow-ups: `--env` (PROD/AUTOPUSH), `--model` (Gemini), `--migration-version` (1.0/2.0).

## Quick Reference

```bash
# 1:1 migration (interactive — InquirerPy will prompt for project + location)
python .agents/skills/cxas-dfcx-migration/scripts/migrate.py

# Fully scripted
python .agents/skills/cxas-dfcx-migration/scripts/migrate.py \
  --source-agent-id "projects/<src_proj>/locations/us/agents/<uuid>" \
  --project-id <target_proj> --location us \
  --target-name my_cxas_app --yes

# Pre-flight HTML preview only (no migration)
python .agents/skills/cxas-dfcx-migration/scripts/migrate.py \
  --source-agent-id "<id>" --project-id <proj> --target-name preview_only \
  --preview-only --yes

# Stage 1 — variable dedup + Gemini consolidation
python .agents/skills/cxas-dfcx-migration/scripts/stage1.py --target-name my_cxas_app

# Stage 1 — variable dedup ONLY (skip consolidation)
python .agents/skills/cxas-dfcx-migration/scripts/stage1.py \
  --target-name my_cxas_app --no-consolidate --yes

# Stage 1 — replay a saved grouping JSON
python .agents/skills/cxas-dfcx-migration/scripts/stage1.py \
  --target-name my_cxas_app --grouping-json my_cxas_app_grouping.json --yes

# Stage 2 — instruction state machines + tool mocks + lint + report
python .agents/skills/cxas-dfcx-migration/scripts/stage2.py --target-name my_cxas_app

# Stage 3 — rewire consolidated agent topology from source dep graph
# (only run after stage1.py with consolidation; idempotent)
python .agents/skills/cxas-dfcx-migration/scripts/stage3.py --target-name my_cxas_app

# Stage 3 — preview the proposed parent → children mapping without applying
python .agents/skills/cxas-dfcx-migration/scripts/stage3.py --target-name my_cxas_app --dry-run
```

## What lives in the skill vs. in `cxas_scrapi`

The skill is a thin orchestrator. All migration / optimization logic lives in `src/cxas_scrapi/migration/`:

| Operation | src/ entry point |
|---|---|
| Source agent fetch / zip parse | `migration/dfcx_exporter.py:ConversationalAgentsAPI` |
| Full migration pipeline | `migration/service.py:MigrationService.run_migration` |
| Variable dedup (Stage 1) | `migration/optimizer.py:CXASOptimizer.optimize_stage1` |
| Instruction restructuring + tool mocks (Stage 2) | `migration/optimizer.py:CXASOptimizer.optimize_stage2` |
| Update-pass redeploys | `migration/service.py:MigrationService._deploy_base_resources(is_update_pass=True)` + `_deploy_pending_agents(is_update_pass=True)` |
| Topology link | `migration/cxas_topology_linker.py` |
| Version checkpoints | `core/versions.py:Versions.create_version` |
| Topology SVG | `migration/graph_visualizer.py:HighLevelGraphVisualizer` |
| Per-resource Rich trees | `migration/playbook_visualizer.py` + `migration/flow_visualizer.py` |

| Deterministic unit tests | `migration/eval_generator.py:DeterministicEvalGenerator` |
| Migration report | `migration/dfcx_migration_reporter.py` |
| Gemini N→M grouping + per-group PIF XML synthesis | `migration/structural_consolidator.py:StructuralConsolidator` *(new in src/, promoted out of the skill)* |

Skill-local helpers (TUI / persistence / formatting only):

- `_prompts.py` — InquirerPy prompt library (matches agent-foundry).
- `_bundle.py` — `<target>_ir.json` persistence (Pydantic roundtrip).
- `_phase_tracker.py` — timed phase markers for the terminal.
- `_visualizer.py` — pre-flight HTML preview + multi-stage HTML report aggregator.
- `_grouping.py` — TUI wrapper around `StructuralConsolidator` (interactive accept / re-propose / merge / split / rename).
- `_synthesis.py` — TUI wrapper for view / edit-in-`$EDITOR` / re-synthesize per group.
- `_optimizer_runner.py` — `run_stage_with_redeploy(service, stage)` helper (Stage N + update-pass redeploys).
- `_lint.py` — `cxas pull` + `cxas lint` post-deploy.
- `_reporter.py` — `OptimizationReporter` audit markdown.
- `_shared.py` — **delegates to `MigrationCLI`** for `check_auth`, `display_status`, `run_dependency_analysis`, `select_resources`, `show_visualizations`. Skill-specific reimplementation only for: project + location prompts upfront (CLI doesn't ask), source loading via InquirerPy (CLI version is inline + uses rich.Prompt), `MigrationConfig` assembly that interleaves InquirerPy prompts with CLI flag overrides.

## IR bundle (`<target>_ir.json`)

The unit of state shared across the three scripts. Pydantic `IRBundle` model:

```jsonc
{
  "schema_version": "1",
  "created_at": "2026-05-14T15:30:00",
  "config": { /* MigrationConfig */ },
  "source_agent_data": { /* DFCXAgentIR — needed for tool-mock context */ },
  "ir": { /* MigrationIR — mutated by each stage */ },
  "stage_history": [
    {"phase": "migrate", "status": "ok", ...},
    {"phase": "stage1",  "status": "ok", ...}
  ],
  "app_url": "https://ces.cloud.google.com/...",
  "version_checkpoints": [["0.0.1", "Stage 1: ..."]],
  "grouping": { /* present if Stage 1 ran consolidation */ }
}
```

Killing a stage script mid-run leaves the bundle untouched (only persisted on success). Re-running picks up where the last successful stage left off.

## Pre-flight HTML preview

`migrate.py` generates `<target>_tree_preview.html` in ~5 seconds after source loading. Open it in any browser to see:

- Source overview (resource counts, estimated migration time).
- Topology graph (graphviz SVG when `dot` is on PATH; Mermaid fallback otherwise).
- Per-playbook and per-flow Rich trees.

`migrate.py --preview-only` exits after the preview without running the migration.

## Troubleshooting

- **`create_app` returns `404 / 501 / MethodNotImplemented`** — your `--location` is wrong. CXAS apps in most projects live in `us`, not `global`. Pass `--location us`.
- **`AlreadyExists: App with same display name`** — pick a different `--target-name`. Old runs leave deployed apps behind even on partial failure.
- **Stage 1 / Stage 2 fail with `No IR bundle found`** — run `migrate.py` first to produce `<target>_ir.json`, or pass `--ir-bundle <path>` explicitly.
- **Synthesis (Stage 1 consolidation) hangs on Gemini** — fixed with per-group `asyncio.wait_for(timeout=600s)` in `structural_consolidator.synthesize_instructions`. Override via `SYNTHESIS_TIMEOUT_S` env var. Hung groups fall back to the concatenated instruction.
- **`gemini-2.5-flash-001 not found`** during AI augment — the Gemini call uses `locations/global` for the model; sometimes that endpoint is project-restricted. The migration continues with empty AI descriptions. Pick a different `--model` if you need them.

## Detailed reference

See [references/migration-options.md](references/migration-options.md) for full parameter / flag descriptions and the IR bundle schema.
