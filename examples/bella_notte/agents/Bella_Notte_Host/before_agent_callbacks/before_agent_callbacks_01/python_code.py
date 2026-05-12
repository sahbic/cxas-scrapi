"""Before-agent callback — pure framework, no agent-specific content.

Runs ONCE per user turn, BEFORE static variable substitution.

Sets three static variables that instruction.txt references:
  {{slot_filling_protocol}}  — collection rules (empty during readback phase)
  {{readback_protocol}}   — readback/confirm rules (always shown except done)
  {{system_directive}}    — next question or readback prompt from the DAG

State is read from sm (written by before_model_callback on the previous turn):
  sm["_tool_selection"]  — slot-to-setter mapping for the collection block
  sm["_slot_ordering"]   — natural slot order for the collection block
  sm["_prereq_note"]     — prerequisite warnings (e.g. available_times first)
  sm["_next_directive"]  — next question or readback prompt

Note: `tools` global is NOT available in before_agent_callbacks. Config
loading and SM initialization from config happen in before_model_callback
via tools.dag_config().
"""

from typing import Any, Optional


# ═════════════════════════════════════════════════════════════════════
# SM INITIALIZATION — Basic structure (config-derived values are
# populated by before_model_callback on the first invocation)
# ═════════════════════════════════════════════════════════════════════

_SM_DEFAULTS = {
    "filled": {},
    "pending": {},
    "status": "in_progress",
    "task_results": {},
}


def _ensure_sm_initialized(sm: dict[str, Any]) -> None:
  """Initialize sm with defaults on first use. Idempotent."""
  if sm.get("_initialized"):
    return
  for key, value in _SM_DEFAULTS.items():
    sm.setdefault(key, value)
  sm["_initialized"] = True


# ═════════════════════════════════════════════════════════════════════
# FRAMEWORK PROMPTS — Protocol blocks injected into the instruction
# ═════════════════════════════════════════════════════════════════════


def _make_collection_block(
    tool_selection: str, slot_ordering: str, prereq_note: str = "",
) -> str:
  ts = tool_selection or (
      "   (Determine the correct setter from tool names and descriptions.)"
  )
  ordering = slot_ordering or "natural order"
  prereq = f" {prereq_note}" if prereq_note else ""
  return f"""\
<slot_filling_protocol>
You are operating in SLOT FILLING mode. Follow these rules strictly:

1. TOOL-DRIVEN CONVERSATION: After each user message, identify EVERY piece
   of information the user provided and call ALL corresponding setter tools
   in the SAME response. Never defer a setter call to a later turn when
   the user already gave the information.

2. FOLLOW THE SYSTEM'S NEXT-STEP GUIDANCE: The system provides a directive
   below. Relay it to the user naturally — include exact values or
   confirmation numbers. Do NOT substitute generic information.

3. TOOL SELECTION — call ONLY the setter that matches:
{ts}

4. ALWAYS CALL TOOLS — NEVER SKIP: Call the setter for EVERY piece of
   information, even if out of range or invalid. The system validates all
   inputs and handles errors automatically.

5. NATURAL CONVERSATION: Answer off-topic questions helpfully, then return
   to the main flow.

6. ORDERING: Natural flow is {ordering}.
   Accept info out of order.{prereq}

7. HANDLING UNAVAILABLE REQUESTS: If no matching tool is visible, guide
   the user to provide information for one of the available slots instead.
</slot_filling_protocol>"""


_READBACK_BLOCK = """\
<readback_protocol>
After calling setter tools, values enter a "pending" state requiring user
confirmation. Follow these rules:

1. READBACK: After calling setters, read back ONLY the new values in one
   natural sentence and ask "Is that correct?" Use the tool response's
   `value` field — always use digits for numbers, never spell them out.
   Do NOT include previously confirmed values. Then STOP and WAIT.

2. CONFIRM / REJECT: On "yes" → call confirm_pending. On bare "no" with
   no correction → call reject_pending. If the user corrects or provides
   new info (with or without "yes"/"no"), call the appropriate setter.

3. CAPTURE EVERYTHING: Call setter tools for ANY information the user
   provides, even alongside a confirmation. Never ignore new information.
</readback_protocol>"""


# ═════════════════════════════════════════════════════════════════════
# FRAMEWORK CALLBACK — Entry point (do not modify per project)
# ═════════════════════════════════════════════════════════════════════


def before_agent_callback(  # pylint: disable=undefined-variable
    callback_context: CallbackContext,
) -> Optional[Content]:
  """Runs once per user turn before static variable substitution."""
  sm = callback_context.state.get("sm", {})
  _ensure_sm_initialized(sm)

  # ── Deferred rejection ───────────────────────────────────────
  if "_rejection_snapshot" in sm:
    snapshot = sm.pop("_rejection_snapshot")
    sm.pop("_rejection_requested", None)
    sm["_progress_turns"] = 0
    sm.pop("_readback_stall", None)
    sm.pop("_active_readback", None)
    pending = sm.get("pending", {})
    for k in snapshot:
      pending.pop(k, None)
    sm["pending"] = pending

  # ── Prompt variables ──────────────────────────────────────────
  directive = sm.get("_next_directive", "")
  directive_block = (
      f"<system_directive>\n{directive}\n</system_directive>"
      if directive else ""
  )
  status = sm.get("status", "in_progress")

  if status in ("complete", "escalated"):
    callback_context.state["slot_filling_protocol"] = ""
    callback_context.state["readback_protocol"] = ""
    callback_context.state["system_directive"] = ""
  elif sm.get("pending"):
    callback_context.state["slot_filling_protocol"] = ""
    callback_context.state["readback_protocol"] = _READBACK_BLOCK
    callback_context.state["system_directive"] = directive_block
  else:
    ts = sm.get("_tool_selection", "")
    ordering = sm.get("_slot_ordering", "")
    prereq = sm.get("_prereq_note", "")
    collection_block = _make_collection_block(ts, ordering, prereq)
    callback_context.state["slot_filling_protocol"] = collection_block
    callback_context.state["readback_protocol"] = _READBACK_BLOCK
    callback_context.state["system_directive"] = directive_block

  callback_context.state["sm"] = sm

  return None
