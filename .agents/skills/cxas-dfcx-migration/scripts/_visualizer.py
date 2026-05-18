# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

"""Pre-migration tree visualizer.

Inspects a freshly loaded `DFCXAgentIR` and emits an HTML preview report with
Mermaid graphs of the agent topology. Designed to be cheap (no LLM, no API)
so the user can preview the structure of a source DFCX agent BEFORE kicking
off a 30+ minute migration.
"""

from __future__ import annotations

import html
import io
import json
import re
from collections import Counter
from typing import Any

from rich.console import Console
from rich.panel import Panel

from cxas_scrapi.migration.data_models import DFCXAgentIR, MigrationIR
from cxas_scrapi.migration.dfcx_dep_analyzer import DependencyAnalyzer
from cxas_scrapi.migration.flow_visualizer import (
    FlowDependencyResolver,
    FlowTreeVisualizer,
)
from cxas_scrapi.migration.graph_visualizer import HighLevelGraphVisualizer
from cxas_scrapi.migration.playbook_visualizer import PlaybookTreeVisualizer

# ---------------------------------------------------------------------------
# Mermaid emission
# ---------------------------------------------------------------------------


_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9]")


def _mermaid_id(prefix: str, raw: str) -> str:
    """Mermaid node IDs must be alphanumeric + underscore. We use the suffix
    of the resource name (the part after the last `/`) plus a short hash of
    the full name to guarantee uniqueness — the prefix `projects/.../agents/<uuid>/`
    is identical across all resources of an agent and would collide.
    """
    suffix = raw.rsplit("/", 1)[-1] if "/" in raw else raw
    safe = _SAFE_ID_RE.sub("_", suffix)[:40]
    digest = abs(hash(raw)) % 0xFFFFFF
    return f"{prefix}_{safe}_{digest:06x}"


def _mermaid_label(text: str, max_len: int = 60) -> str:
    """Wrap a label so Mermaid renders it without breaking syntax."""
    if not text:
        return "(unnamed)"
    cleaned = text.replace('"', "'").replace("\n", " ")
    if len(cleaned) > max_len:
        cleaned = cleaned[: max_len - 1] + "…"
    return cleaned


def build_mermaid_topology(
    agent_data: DFCXAgentIR, analyzer: DependencyAnalyzer
) -> str:
    """Render the source agent's resource graph as a Mermaid flowchart.

    Nodes: each playbook (rectangle) and each flow (rounded). Edges: dependency
    references (one resource → another). Tools are NOT exploded into the
    graph — they would multiply node counts past Mermaid's practical limit on
    large agents. Use `build_mermaid_tools_per_agent` for tool inventory.
    """
    lines = ["flowchart LR"]

    type_styles = {
        "Playbook": ":::playbook",
        "Flow": ":::flow",
    }

    # Nodes
    for full_name, display_name in analyzer.name_map.items():
        node_type = analyzer.type_map.get(full_name, "Resource")
        node_id = _mermaid_id("n", full_name)
        cls = type_styles.get(node_type, "")
        label = _mermaid_label(display_name)
        if node_type == "Flow":
            lines.append(f'  {node_id}(["{label}"]){cls}')
        else:
            lines.append(f'  {node_id}["{label}"]{cls}')

    # Edges
    for source, targets in analyzer.graph.items():
        if source not in analyzer.name_map:
            continue
        src_id = _mermaid_id("n", source)
        for tgt in targets:
            if tgt not in analyzer.name_map:
                continue
            tgt_id = _mermaid_id("n", tgt)
            lines.append(f"  {src_id} --> {tgt_id}")

    # Style classes
    lines.extend(
        [
            "  classDef playbook fill:#dbeafe,stroke:#2563eb,color:#1e3a8a",
            "  classDef flow fill:#fef3c7,stroke:#d97706,color:#7c2d12",
        ]
    )
    return "\n".join(lines)


_TOOL_REF_RE = re.compile(r"\$\{TOOL:([^}]+)\}|\{@TOOL:\s*([^}]+)\}")


def _scan_text_for_tool_refs(text: str) -> set[str]:
    refs: set[str] = set()
    if not text:
        return refs
    for m in _TOOL_REF_RE.finditer(text):
        refs.add((m.group(1) or m.group(2) or "").strip())
    return refs


def _collect_agent_tool_refs(
    agent_data: DFCXAgentIR,
) -> list[tuple[str, str, str, list[str]]]:
    """Return (kind, full_name, display_name, tool_refs) per playbook + flow.

    Looks at explicit `referencedTools` lists AND scans instruction/page bodies
    for `${TOOL:Name}` and `{@TOOL: name}` references so empty `referencedTools`
    doesn't hide a real dependency."""
    out: list[tuple[str, str, str, list[str]]] = []

    for pb in agent_data.playbooks:
        full = pb.get("name", "")
        display = pb.get("displayName", "?")
        explicit = list(pb.get("referencedTools", []) or [])
        scanned = _scan_text_for_tool_refs(
            json.dumps(pb.get("instruction", {}))
        )
        merged = list(dict.fromkeys(explicit + sorted(scanned)))
        out.append(("Playbook", full, display, merged))

    for flow_wrapper in agent_data.flows:
        flow = flow_wrapper.flow_data
        full = flow.get("name", "")
        display = flow.get("displayName", "?")
        scanned = _scan_text_for_tool_refs(json.dumps(flow))
        for page in flow_wrapper.pages:
            scanned |= _scan_text_for_tool_refs(json.dumps(page.page_data))
        out.append(("Flow", full, display, sorted(scanned)))

    return out


def build_mermaid_tools_per_agent(
    agent_data: DFCXAgentIR, max_agents: int = 25
) -> str:
    """Render an agent → tools subgraph for the playbooks/flows that
    reference the most tools. Caps at `max_agents` to keep the graph
    readable on large agents."""
    lines = ["flowchart LR"]

    refs = _collect_agent_tool_refs(agent_data)
    refs.sort(key=lambda x: len(x[3]), reverse=True)

    seen_tools: set[str] = set()
    rendered = 0
    for kind, full_name, display, tools in refs:
        if not tools:
            continue
        if rendered >= max_agents:
            break
        agent_id = _mermaid_id("a", full_name)
        cls = "flow" if kind == "Flow" else "pb"
        shape_open, shape_close = (
            ("([", "])") if kind == "Flow" else ('["', '"]')
        )
        lines.append(
            f'  {agent_id}{shape_open}"{_mermaid_label(display)}"{shape_close}:::{cls}'
            if kind == "Flow"
            else f'  {agent_id}["{_mermaid_label(display)}"]:::{cls}'
        )
        for tool in tools:
            tool_id = _mermaid_id("t", tool)
            if tool not in seen_tools:
                tool_label = tool.rsplit("/", 1)[-1] if "/" in tool else tool
                lines.append(
                    f'  {tool_id}(["{_mermaid_label(tool_label)}"]):::tool'
                )
                seen_tools.add(tool)
            lines.append(f"  {agent_id} -.-> {tool_id}")
        rendered += 1

    if len(lines) == 1:
        lines.append('  empty["No tool references found in instructions"]')

    lines.extend(
        [
            "  classDef pb fill:#dbeafe,stroke:#2563eb,color:#1e3a8a",
            "  classDef flow fill:#fef3c7,stroke:#d97706,color:#7c2d12",
            "  classDef tool fill:#dcfce7,stroke:#16a34a,color:#14532d",
        ]
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


def collect_stats(
    agent_data: DFCXAgentIR, analyzer: DependencyAnalyzer
) -> dict[str, Any]:
    """Build a stats dict the HTML template can render."""
    pb_count = len(agent_data.playbooks)
    flow_count = len(agent_data.flows)
    page_count = sum(len(f.pages) for f in agent_data.flows)
    tool_count = len(agent_data.tools)
    webhook_count = len(agent_data.webhooks)
    intent_count = len(agent_data.intents)
    entity_count = len(agent_data.entity_types)
    code_block_count = len(agent_data.code_blocks)
    routing_edge_count = sum(
        len(targets) for targets in analyzer.graph.values()
    )

    # Top connected resources (in + out)
    in_out: Counter[str] = Counter()
    for source, targets in analyzer.graph.items():
        in_out[source] += len(targets)
    for target, sources in analyzer.reverse_graph.items():
        in_out[target] += len(sources)
    top_connected = []
    for resource_id, degree in in_out.most_common(10):
        top_connected.append(
            {
                "name": analyzer.name_map.get(resource_id, resource_id),
                "type": analyzer.type_map.get(resource_id, "?"),
                "degree": degree,
            }
        )

    # Estimated migration time (rough, based on observed ~40s per flow + 10s
    # per playbook through the LLM pipeline)
    est_seconds = flow_count * 40 + pb_count * 10 + 60
    est_minutes = max(1, est_seconds // 60)

    return {
        "playbook_count": pb_count,
        "flow_count": flow_count,
        "page_count": page_count,
        "tool_count": tool_count,
        "webhook_count": webhook_count,
        "intent_count": intent_count,
        "entity_count": entity_count,
        "code_block_count": code_block_count,
        "routing_edge_count": routing_edge_count,
        "top_connected": top_connected,
        "estimated_minutes": est_minutes,
        "agent_name": agent_data.display_name,
        "default_language": agent_data.default_language_code,
        "start_flow": agent_data.start_flow,
        "start_playbook": agent_data.start_playbook,
    }


def collect_resource_rows(
    agent_data: DFCXAgentIR, analyzer: DependencyAnalyzer
) -> list[dict[str, Any]]:
    """Per-resource row data for the HTML tables."""
    rows: list[dict[str, Any]] = []
    for pb in agent_data.playbooks:
        full = pb.get("name", "")
        outgoing = sorted(
            analyzer.name_map.get(t, t) for t in analyzer.graph.get(full, set())
        )
        incoming = sorted(
            analyzer.name_map.get(s, s)
            for s in analyzer.reverse_graph.get(full, set())
        )
        rows.append(
            {
                "type": "Playbook",
                "name": pb.get("displayName", "?"),
                "id": full.split("/")[-1] if full else "",
                "tools": [
                    t.split("/")[-1] for t in pb.get("referencedTools", [])
                ],
                "playbooks": [
                    p.split("/")[-1] for p in pb.get("referencedPlaybooks", [])
                ],
                "outgoing": outgoing,
                "incoming": incoming,
                "step_count": len(
                    pb.get("instruction", {}).get("steps", []) or []
                ),
            }
        )
    for flow_wrapper in agent_data.flows:
        flow = flow_wrapper.flow_data
        full = flow.get("name", "")
        outgoing = sorted(
            analyzer.name_map.get(t, t) for t in analyzer.graph.get(full, set())
        )
        incoming = sorted(
            analyzer.name_map.get(s, s)
            for s in analyzer.reverse_graph.get(full, set())
        )
        rows.append(
            {
                "type": "Flow",
                "name": flow.get("displayName", "?"),
                "id": full.split("/")[-1] if full else "",
                "tools": [],
                "playbooks": [],
                "outgoing": outgoing,
                "incoming": incoming,
                "step_count": len(flow_wrapper.pages),
            }
        )
    rows.sort(
        key=lambda r: (-len(r["outgoing"]) - len(r["incoming"]), r["name"])
    )
    return rows


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------


_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>DFCX Source Tree Preview — {agent_name}</title>
<script type="module">
  import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs';
  mermaid.initialize({{ startOnLoad: true, theme: 'default', flowchart: {{ curve: 'basis' }} }});
</script>
<style>
  :root {{
    --bg:#f8fafc; --card:#ffffff; --border:#e2e8f0; --muted:#64748b;
    --accent:#2563eb; --pill-bg:#eff6ff; --pill-text:#1e40af;
  }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
         margin: 0; padding: 24px; background: var(--bg); color: #0f172a; }}
  h1 {{ margin: 0 0 4px 0; font-size: 24px; }}
  h2 {{ margin: 32px 0 12px 0; font-size: 18px; color: var(--accent); }}
  .subtitle {{ color: var(--muted); margin-bottom: 24px; font-size: 14px; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; }}
  .stat {{ background: var(--card); border: 1px solid var(--border); border-radius: 8px;
           padding: 14px 16px; }}
  .stat .label {{ font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em;
                  color: var(--muted); }}
  .stat .value {{ font-size: 24px; font-weight: 600; margin-top: 4px; }}
  .panel {{ background: var(--card); border: 1px solid var(--border); border-radius: 8px;
            padding: 16px; margin-top: 12px; overflow: auto; }}
  .pill {{ display: inline-block; padding: 2px 8px; border-radius: 999px;
           background: var(--pill-bg); color: var(--pill-text); font-size: 11px;
           font-family: ui-monospace, SFMono-Regular, Menlo, monospace; margin: 1px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--border);
            vertical-align: top; }}
  th {{ background: #f1f5f9; font-weight: 600; font-size: 12px; }}
  details {{ margin: 4px 0; }}
  summary {{ cursor: pointer; padding: 6px 0; font-weight: 500; }}
  summary:hover {{ color: var(--accent); }}
  .est-banner {{ background: #fef3c7; border-left: 4px solid #d97706; color: #7c2d12;
                  padding: 12px 16px; border-radius: 4px; margin-bottom: 16px; font-size: 14px; }}
  .mermaid {{ background: var(--card); border: 1px solid var(--border); border-radius: 8px;
              padding: 16px; }}
  code {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }}
</style>
</head>
<body>

<h1>{agent_name}</h1>
<div class="subtitle">DFCX source tree preview · default language <code>{default_language}</code></div>

<div class="est-banner">
  <strong>Estimated migration time:</strong> ~{estimated_minutes} minutes for a 1:1 migration of every playbook + flow.
  Optimization adds ~30-50% on top for the consolidation + Stage 1/2 passes.
</div>

<h2>Resource counts</h2>
<div class="grid">
  <div class="stat"><div class="label">Playbooks</div><div class="value">{playbook_count}</div></div>
  <div class="stat"><div class="label">Flows</div><div class="value">{flow_count}</div></div>
  <div class="stat"><div class="label">Pages</div><div class="value">{page_count}</div></div>
  <div class="stat"><div class="label">Tools</div><div class="value">{tool_count}</div></div>
  <div class="stat"><div class="label">Webhooks</div><div class="value">{webhook_count}</div></div>
  <div class="stat"><div class="label">Intents</div><div class="value">{intent_count}</div></div>
  <div class="stat"><div class="label">Entities</div><div class="value">{entity_count}</div></div>
  <div class="stat"><div class="label">Code blocks</div><div class="value">{code_block_count}</div></div>
  <div class="stat"><div class="label">Routing edges</div><div class="value">{routing_edge_count}</div></div>
</div>

<h2>Topology graph</h2>
<div class="panel"><div class="mermaid">
{mermaid_topology}
</div></div>

<h2>Tools per agent (top {tools_top_n} by tool count)</h2>
<div class="panel"><div class="mermaid">
{mermaid_tools}
</div></div>

<h2>Top {top_connected_n} most-connected resources</h2>
<div class="panel">
<table>
<tr><th>Resource</th><th>Type</th><th>Degree (in + out)</th></tr>
{top_connected_rows}
</table>
</div>

<h2>Per-resource detail</h2>
<div class="panel">
{resource_details}
</div>

<h2>Raw stats (JSON)</h2>
<div class="panel"><pre><code>{stats_json}</code></pre></div>

</body>
</html>
"""


def _render_top_connected_rows(top: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"<tr><td>{html.escape(r['name'])}</td>"
        f"<td><span class='pill'>{html.escape(r['type'])}</span></td>"
        f"<td>{r['degree']}</td></tr>"
        for r in top
    )


def _render_resource_details(rows: list[dict[str, Any]]) -> str:
    out = []
    for r in rows:
        tools_html = (
            " ".join(
                f"<span class='pill'>{html.escape(t)}</span>"
                for t in r["tools"]
            )
            or "<em style='color:#64748b'>none</em>"
        )
        outgoing_html = (
            " ".join(
                f"<span class='pill'>{html.escape(t)}</span>"
                for t in r["outgoing"]
            )
            or "<em style='color:#64748b'>none</em>"
        )
        incoming_html = (
            " ".join(
                f"<span class='pill'>{html.escape(t)}</span>"
                for t in r["incoming"]
            )
            or "<em style='color:#64748b'>none</em>"
        )
        out.append(
            f"<details><summary>"
            f"<span class='pill'>{html.escape(r['type'])}</span> "
            f"<strong>{html.escape(r['name'])}</strong> "
            f"<span style='color:#64748b'>· {r['step_count']} steps · "
            f"{len(r['outgoing'])} outgoing · {len(r['incoming'])} incoming</span>"
            f"</summary>"
            f"<div style='padding: 8px 16px;'>"
            f"<div><strong>ID:</strong> <code>{html.escape(r['id'])}</code></div>"
            f"<div style='margin-top:6px'><strong>Tools:</strong> {tools_html}</div>"
            f"<div style='margin-top:6px'><strong>Outgoing refs:</strong> {outgoing_html}</div>"
            f"<div style='margin-top:6px'><strong>Incoming refs:</strong> {incoming_html}</div>"
            f"</div></details>"
        )
    return "\n".join(out)


def generate_html_report(
    agent_data: DFCXAgentIR,
    analyzer: DependencyAnalyzer,
    output_path: str,
    tools_top_n: int = 25,
) -> str:
    """Write a self-contained HTML preview to `output_path`. Returns the path."""
    stats = collect_stats(agent_data, analyzer)
    rows = collect_resource_rows(agent_data, analyzer)
    mermaid_top = build_mermaid_topology(agent_data, analyzer)
    mermaid_tools = build_mermaid_tools_per_agent(
        agent_data, max_agents=tools_top_n
    )

    rendered = _HTML_TEMPLATE.format(
        agent_name=html.escape(stats["agent_name"] or "DFCX Agent"),
        default_language=html.escape(stats["default_language"] or "?"),
        estimated_minutes=stats["estimated_minutes"],
        playbook_count=stats["playbook_count"],
        flow_count=stats["flow_count"],
        page_count=stats["page_count"],
        tool_count=stats["tool_count"],
        webhook_count=stats["webhook_count"],
        intent_count=stats["intent_count"],
        entity_count=stats["entity_count"],
        code_block_count=stats["code_block_count"],
        routing_edge_count=stats["routing_edge_count"],
        mermaid_topology=mermaid_top,
        mermaid_tools=mermaid_tools,
        top_connected_n=len(stats["top_connected"]),
        top_connected_rows=_render_top_connected_rows(stats["top_connected"]),
        resource_details=_render_resource_details(rows),
        tools_top_n=tools_top_n,
        stats_json=html.escape(json.dumps(stats, indent=2, default=str)),
    )
    with open(output_path, "w") as f:
        f.write(rendered)
    return output_path


def write_mermaid_files(
    agent_data: DFCXAgentIR,
    analyzer: DependencyAnalyzer,
    target_name: str,
) -> tuple[str, str]:
    """Also write the raw Mermaid sources to disk so the user can paste them
    into mermaid.live or include in other docs."""
    topo_path = f"{target_name}_topology.mmd"
    tools_path = f"{target_name}_tools.mmd"
    with open(topo_path, "w") as f:
        f.write(build_mermaid_topology(agent_data, analyzer))
    with open(tools_path, "w") as f:
        f.write(build_mermaid_tools_per_agent(agent_data))
    return topo_path, tools_path


# ---------------------------------------------------------------------------
# Use existing cxas_scrapi.migration visualizers
# ---------------------------------------------------------------------------


def topology_svg(
    agent_data: DFCXAgentIR, show_code_blocks: bool = False
) -> str | None:
    """Render the source agent topology as an SVG string using
    HighLevelGraphVisualizer (graphviz). Returns None if graphviz isn't
    available."""
    try:
        dot = HighLevelGraphVisualizer(agent_data).build(
            show_code_blocks=show_code_blocks
        )
        svg_bytes = dot.pipe(format="svg")
        svg_text = svg_bytes.decode("utf-8")
        idx = svg_text.find("<svg")
        return svg_text[idx:] if idx >= 0 else svg_text
    except Exception:
        return None


def _rich_to_html(renderable, width: int = 140) -> str:
    """Capture a Rich renderable as HTML using Rich's exporter. Output is
    routed to an in-memory buffer (not stdout), so calling this is silent."""
    buf_console = Console(
        record=True,
        width=width,
        force_terminal=True,
        file=io.StringIO(),
    )
    buf_console.print(renderable)
    full = buf_console.export_html(
        inline_styles=True, code_format="<pre>{code}</pre>"
    )
    start = full.find("<body")
    if start == -1:
        return full
    body_open_end = full.find(">", start)
    body_close = full.rfind("</body>")
    return full[body_open_end + 1 : body_close] if body_close > 0 else full


def render_playbook_trees_html(agent_data: DFCXAgentIR) -> str:
    """Render every playbook as a Rich tree and concatenate as HTML."""
    chunks: list[str] = []
    for playbook_wrapper in agent_data.playbooks:
        playbook = (
            playbook_wrapper.get("playbook", playbook_wrapper)
            if isinstance(playbook_wrapper, dict)
            else playbook_wrapper
        )
        try:
            tree = PlaybookTreeVisualizer(playbook).build_tree()
            chunks.append(_rich_to_html(Panel(tree, title="Playbook")))
        except Exception as exc:
            chunks.append(
                f"<pre>Failed to render playbook: {html.escape(str(exc))}</pre>"
            )
    return "\n".join(chunks) or "<em>No playbooks.</em>"


def render_flow_trees_html(agent_data: DFCXAgentIR) -> str:
    """Render every flow as a Rich tree and concatenate as HTML."""
    chunks: list[str] = []
    try:
        resolver = FlowDependencyResolver(agent_data)
    except Exception as exc:
        return f"<pre>Failed to build resolver: {html.escape(str(exc))}</pre>"
    for flow_wrapper in agent_data.flows:
        try:
            context = resolver.resolve(flow_wrapper)
            tree = FlowTreeVisualizer(context).build_tree()
            chunks.append(_rich_to_html(Panel(tree, title="Flow")))
        except Exception as exc:
            chunks.append(
                f"<pre>Failed to render flow: {html.escape(str(exc))}</pre>"
            )
    return "\n".join(chunks) or "<em>No flows.</em>"


def render_ir_tree_html(
    ir: MigrationIR, title: str, root_key: str | None
) -> str:
    """Render an IR tree (post-compile, post-grouping, etc.) using the
    same render_ir_tree from _grouping, captured as HTML."""
    # Defer import to avoid circular dependency at module load.
    import _grouping  # noqa: PLC0415

    tree = _grouping.render_ir_tree(ir, title, root_key)
    return _rich_to_html(tree)


# ---------------------------------------------------------------------------
# StageReport — multi-stage HTML accumulator
# ---------------------------------------------------------------------------


_STAGE_HTML_HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<script type="module">
  import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs';
  mermaid.initialize({{ startOnLoad: true, theme: 'default', flowchart: {{ curve: 'basis' }} }});
</script>
<style>
  :root {{
    --bg:#f8fafc; --card:#ffffff; --border:#e2e8f0; --muted:#64748b;
    --accent:#2563eb; --pill-bg:#eff6ff; --pill-text:#1e40af;
  }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
         margin: 0; padding: 24px; background: var(--bg); color: #0f172a; }}
  h1 {{ margin: 0 0 4px 0; font-size: 24px; }}
  h2 {{ margin: 24px 0 8px 0; font-size: 18px; color: var(--accent); }}
  .subtitle {{ color: var(--muted); margin-bottom: 16px; font-size: 14px; }}
  .toc {{ background: var(--card); border: 1px solid var(--border); border-radius: 8px;
          padding: 12px 16px; margin-bottom: 16px; font-size: 14px; }}
  .toc a {{ color: var(--accent); text-decoration: none; margin-right: 12px; }}
  .toc a:hover {{ text-decoration: underline; }}
  .stage {{ background: var(--card); border: 1px solid var(--border); border-radius: 8px;
            padding: 16px; margin: 12px 0; }}
  .stage > summary {{ font-size: 16px; font-weight: 600; cursor: pointer;
                      list-style: none; }}
  .stage > summary::before {{ content: '▶ '; color: var(--muted); }}
  .stage[open] > summary::before {{ content: '▼ '; }}
  .stage-meta {{ color: var(--muted); font-size: 12px; margin-top: 4px; }}
  .panel {{ background: #f8fafc; border-left: 3px solid var(--accent);
            padding: 12px 16px; border-radius: 4px; margin: 12px 0;
            overflow: auto; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; }}
  .stat {{ background: var(--card); border: 1px solid var(--border); border-radius: 8px;
           padding: 12px 14px; }}
  .stat .label {{ font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em;
                  color: var(--muted); }}
  .stat .value {{ font-size: 22px; font-weight: 600; margin-top: 4px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--border);
            vertical-align: top; }}
  th {{ background: #f1f5f9; font-weight: 600; font-size: 12px; }}
  pre {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px;
         margin: 0; padding: 0; background: transparent; }}
  .pill {{ display: inline-block; padding: 2px 8px; border-radius: 999px;
           background: var(--pill-bg); color: var(--pill-text); font-size: 11px;
           font-family: ui-monospace, SFMono-Regular, Menlo, monospace; margin: 1px; }}
  .status-ok {{ color: #16a34a; }} .status-fail {{ color: #dc2626; }}
  .status-skipped {{ color: #d97706; }}
  .mermaid {{ background: var(--card); border: 1px solid var(--border); border-radius: 8px;
              padding: 12px; }}
  .svg-zoom {{ border: 1px solid var(--border); border-radius: 8px;
               max-height: 700px; overflow: auto; background: white; padding: 12px; }}
</style>
</head>
<body>

<h1>{title}</h1>
<div class="subtitle">{subtitle}</div>

<div class="toc"><strong>Stages:</strong> {toc}</div>
"""


_STAGE_HTML_TAIL = """
</body>
</html>
"""


class StageReport:
    """Accumulates HTML snapshots across the migration / optimization
    pipeline and writes a single multi-section HTML report at the end."""

    def __init__(self, title: str, subtitle: str = ""):
        self.title = title
        self.subtitle = subtitle
        self._stages: list[tuple[str, str, str]] = []  # (id, label, body_html)

    def add_section(
        self, label: str, body_html: str, anchor: str | None = None
    ) -> None:
        anchor = anchor or _SAFE_ID_RE.sub("_", label).lower()[:60]
        self._stages.append((anchor, label, body_html))

    # --- High-level helpers ------------------------------------------------

    def add_source_overview(
        self, agent_data: DFCXAgentIR, analyzer: DependencyAnalyzer
    ) -> None:
        stats = collect_stats(agent_data, analyzer)
        cards = "\n".join(
            f'<div class="stat"><div class="label">{k}</div>'
            f'<div class="value">{v}</div></div>'
            for k, v in [
                ("Playbooks", stats["playbook_count"]),
                ("Flows", stats["flow_count"]),
                ("Pages", stats["page_count"]),
                ("Tools", stats["tool_count"]),
                ("Webhooks", stats["webhook_count"]),
                ("Intents", stats["intent_count"]),
                ("Entities", stats["entity_count"]),
                ("Code blocks", stats["code_block_count"]),
                ("Routing edges", stats["routing_edge_count"]),
                ("Est. mins", stats["estimated_minutes"]),
            ]
        )
        self.add_section(
            "Source overview",
            f'<div class="grid">{cards}</div>',
            anchor="source-overview",
        )

    def add_topology_svg(self, agent_data: DFCXAgentIR) -> None:
        svg = topology_svg(agent_data)
        if svg:
            body = f'<div class="svg-zoom">{svg}</div>'
        else:
            analyzer = DependencyAnalyzer(agent_data)
            mermaid = build_mermaid_topology(agent_data, analyzer)
            body = (
                "<p><em>graphviz not available; falling back to Mermaid.</em></p>"
                f'<div class="mermaid">{mermaid}</div>'
            )
        self.add_section("Topology graph", body, anchor="topology")

    def add_playbook_and_flow_trees(self, agent_data: DFCXAgentIR) -> None:
        self.add_section(
            "Playbook trees",
            render_playbook_trees_html(agent_data),
            anchor="playbooks",
        )
        self.add_section(
            "Flow trees",
            render_flow_trees_html(agent_data),
            anchor="flows",
        )

    def add_ir_snapshot(
        self, label: str, ir: MigrationIR, root_key: str | None
    ) -> None:
        self.add_section(
            label,
            render_ir_tree_html(ir, label, root_key),
            anchor=label.lower().replace(" ", "-"),
        )

    def add_grouping_table(self, groupings: dict) -> None:
        rows = []
        for name, payload in groupings.items():
            members = ", ".join(payload.get("agents", []) or [])
            journey = (payload.get("journey") or "").replace("|", "\\|")
            is_root = "yes" if payload.get("is_root") else ""
            rows.append(
                f"<tr><td><code>{html.escape(name)}</code></td>"
                f"<td>{html.escape(members)}</td>"
                f"<td>{html.escape(journey)}</td>"
                f"<td>{is_root}</td></tr>"
            )
        body = (
            "<table><tr><th>Group</th><th>Members</th><th>Journey</th>"
            "<th>Root</th></tr>" + "\n".join(rows) + "</table>"
        )
        self.add_section("Grouping proposal", body, anchor="grouping")

    def add_optimizer_logs(self, label: str, logs: list[dict] | None) -> None:
        if not logs:
            return
        rows = "\n".join(
            f"<tr><td><code>{html.escape(str(e.get('stage', '?')))}</code></td>"
            f"<td><code>{html.escape(str(e.get('action', '?')))}</code></td>"
            f"<td>{html.escape(str(e.get('details', '')))}</td></tr>"
            for e in logs
        )
        body = (
            "<table><tr><th>Stage</th><th>Action</th><th>Details</th></tr>"
            + rows
            + "</table>"
        )
        self.add_section(label, body, anchor=label.lower().replace(" ", "-"))

    def add_phase_timeline(self, tracker_records: list[dict]) -> None:
        if not tracker_records:
            return
        rows = []
        for rec in tracker_records:
            status = rec.get("status", "?")
            cls = {
                "ok": "status-ok",
                "fail": "status-fail",
                "skipped": "status-skipped",
            }.get(status, "")
            duration = (
                f"{rec['duration_s']:.1f}s"
                if rec.get("duration_s") is not None
                else "—"
            )
            rows.append(
                f"<tr><td><code>{html.escape(rec.get('label', '?'))}</code></td>"
                f"<td>{html.escape(rec.get('description', ''))}</td>"
                f"<td class='{cls}'>{html.escape(status)}</td>"
                f"<td>{duration}</td>"
                f"<td>{html.escape(rec.get('note', ''))}</td></tr>"
            )
        body = (
            "<table><tr><th>Phase</th><th>Description</th><th>Status</th>"
            "<th>Duration</th><th>Note</th></tr>" + "\n".join(rows) + "</table>"
        )
        self.add_section("Pipeline timeline", body, anchor="timeline")

    def add_versions(self, versions: list[tuple[str, str]]) -> None:
        if not versions:
            return
        rows = "\n".join(
            f"<tr><td><code>{html.escape(d)}</code></td><td>{html.escape(desc)}</td></tr>"
            for d, desc in versions
        )
        body = (
            "<table><tr><th>Version</th><th>Description</th></tr>"
            + rows
            + "</table>"
        )
        self.add_section("CXAS Version checkpoints", body, anchor="versions")

    def add_app_url(self, app_url: str) -> None:
        if not app_url:
            return
        self.add_section(
            "App",
            f'<p><a href="{html.escape(app_url)}" target="_blank">{html.escape(app_url)}</a></p>',
            anchor="app",
        )

    # --- Emit --------------------------------------------------------------

    def write(self, path: str) -> str:
        toc = " · ".join(
            f'<a href="#{anchor}">{html.escape(label)}</a>'
            for anchor, label, _ in self._stages
        )
        head = _STAGE_HTML_HEAD.format(
            title=html.escape(self.title),
            subtitle=html.escape(self.subtitle),
            toc=toc,
        )
        body = "\n".join(
            f'<details class="stage" id="{anchor}" open>'
            f"<summary>{html.escape(label)}</summary>"
            f"<div>{body_html}</div></details>"
            for anchor, label, body_html in self._stages
        )
        with open(path, "w") as f:
            f.write(head + body + _STAGE_HTML_TAIL)
        return path
