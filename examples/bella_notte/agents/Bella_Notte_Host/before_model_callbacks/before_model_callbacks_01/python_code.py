"""Before-model callback — thin CES adapter for the slot-filling engine.

FRAMEWORK CODE — shared across all agents using the slot-filling engine.
Do not add agent-specific logic here; customize behavior via dag_config.

Calls the slot_filling_engine tool to compute the next action, then
applies CES-specific operations: tool visibility, system instruction
manipulation, and preemption.

Config is loaded from dag_config; engine logic lives in
slot_filling_engine. This callback is pure CES glue.
"""

import json as json_lib
import re
from typing import Optional


_RAW_CONFIG = None

_STALE_TAG_PATTERNS = [
    re.compile(r"\n\n<readback_scope>.*?</readback_scope>", re.DOTALL),
    re.compile(
        r"\n\n<deferred_collection>.*?</deferred_collection>", re.DOTALL,
    ),
    re.compile(
        r"\n\n<system_directive>.*?</system_directive>", re.DOTALL,
    ),
]
_EVENT_PREFILL_PATTERN = re.compile(
    r"<slot_filling_protocol>.*?</slot_filling_protocol>", re.DOTALL,
)
_READBACK_PROTOCOL_PATTERN = re.compile(
    r"<readback_protocol>.*?</readback_protocol>", re.DOTALL,
)
_EVENT_TAG_PATTERN = re.compile(r"<event>(.*?)</event>")


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


def _strip_stale_tags(text: str, event_prefilled: bool) -> str:
  """Remove stale SI tags before appending fresh suffix."""
  for pattern in _STALE_TAG_PATTERNS:
    text = pattern.sub("", text)
  if event_prefilled:
    text = _EVENT_PREFILL_PATTERN.sub("", text)
  return text


def before_model_callback(  # pylint: disable=undefined-variable
    callback_context: CallbackContext,
    llm_request: LlmRequest,
) -> Optional[LlmResponse]:
  """CES entry point: run DAG engine before each LLM call."""
  global _RAW_CONFIG  # pylint: disable=global-statement

  # ── Hide internal tools from model ──────────────────────────
  llm_request.config.hide_tool("dag_config")
  llm_request.config.hide_tool("slot_filling_engine")

  sm = callback_context.state.get("sm", {})

  if sm.get("status") in ("complete", "escalated"):
    return {"decision": "OK"}

  # ── Load raw config (cached after first call) ───────────────
  if _RAW_CONFIG is None:
    _RAW_CONFIG = tools.dag_config(  # pylint: disable=undefined-variable
        {},
    ).json()["result"]

  # ── Debug mode (opt-in via sm._debug_mode state variable) ──
  debug = sm.get("_debug_mode", False)

  # ── Extract last user text ──────────────────────────────────
  last_user_text = ""
  if llm_request.contents:
    last_content = llm_request.contents[-1]
    if getattr(last_content, "role", "") == "user":
      for part in getattr(last_content, "parts", []):
        txt = getattr(part, "text", "")
        if txt:
          last_user_text = txt
          break

  # ── Call the engine ─────────────────────────────────────────
  event_data = callback_context.state.get("event_data", {})
  ia_event = callback_context.state.get("ia_event_name")
  if not ia_event and last_user_text:
    m = _EVENT_TAG_PATTERN.search(last_user_text)
    if m:
      ia_event = m.group(1)
  if ia_event:
    event_data["ia_event_name"] = ia_event
  engine_result = tools.slot_filling_engine(  # pylint: disable=undefined-variable
      {"input_data": {
          "raw_config": _RAW_CONFIG,
          "sm": sm,
          "last_user_text": last_user_text,
          "event_data": event_data,
          **({"debug": True} if debug else {}),
      }},
  ).json()["result"]

  result = engine_result["action"]
  sm = engine_result["sm"]

  if debug:
    sm["_debug_log"] = engine_result.get("_debug_log", [])

  callback_context.state["sm"] = sm

  # ── Clear prompt variables on completion ────────────────────
  status = sm.get("status", "in_progress")
  if status in ("complete", "escalated"):
    callback_context.state["slot_filling_protocol"] = ""
    callback_context.state["readback_protocol"] = ""
    callback_context.state["system_directive"] = ""

  # ── Hide tools ──────────────────────────────────────────────
  for tool_name in result.get("hide_tools", []):
    llm_request.config.hide_tool(tool_name)

  # ── Inline confirm: swap readback → collection in SI ────────
  # When the user confirms AND provides new info in the same
  # message ("Yea, also shellfish allergy"), the engine silently
  # confirmed pending slots and is letting the LLM run to call
  # setters for the new content. But before_agent_callback
  # already baked readback-only instructions into the SI
  # (because pending was non-empty at that point). We need to
  # replace those with collection instructions so the LLM knows
  # to call setters instead of reading back values.
  inline_confirmed = result.get("inline_confirmed", False)
  if inline_confirmed:
    collection_block = _make_collection_block(
        sm.get("_tool_selection", ""),
        sm.get("_slot_ordering", ""),
        sm.get("_prereq_note", ""),
    )
    si = llm_request.config.system_instruction
    if si:
      if hasattr(si, "parts") and si.parts:
        text = si.parts[0].text or ""
        text = _READBACK_PROTOCOL_PATTERN.sub("", text)
        text = _strip_stale_tags(text, False)
        si.parts[0].text = text + "\n\n" + collection_block
      elif isinstance(si, str):
        text = _READBACK_PROTOCOL_PATTERN.sub("", si)
        text = _strip_stale_tags(text, False)
        llm_request.config.system_instruction = (
            text + "\n\n" + collection_block
        )

  # ── Apply system instruction suffix ─────────────────────────
  si_suffix = result.get("si_suffix", "")
  event_prefilled = result.get("event_prefilled", False)
  if si_suffix or event_prefilled:
    si = llm_request.config.system_instruction
    if si:
      if hasattr(si, "parts") and si.parts:
        text = _strip_stale_tags(si.parts[0].text or "", event_prefilled)
        si.parts[0].text = text + si_suffix
      elif isinstance(si, str):
        text = _strip_stale_tags(si, event_prefilled)
        llm_request.config.system_instruction = text + si_suffix

  # ── Preemption ──────────────────────────────────────────────
  if (result.get("preempt")
      and llm_request.contents
      and (len(llm_request.contents) > 1 or result.get("force_preempt"))):
    parts = []
    if result.get("message"):
      parts.append(Part.from_text(  # pylint: disable=undefined-variable
          text=result["message"],
      ))
    response_parts = result.get("response")
    if response_parts:
      for rp in response_parts:
        rp_type = rp.get("type", "text")
        if rp_type == "text":
          parts.append(Part.from_text(  # pylint: disable=undefined-variable
              text=rp.get("text", ""),
          ))
        elif rp_type == "payload":
          parts.append(Part.from_json(  # pylint: disable=undefined-variable
              json_lib.dumps(rp["data"]),
          ))
        elif rp_type == "audio":
          parts.append(Part.from_audio(  # pylint: disable=undefined-variable
              audio_uri=rp["uri"],
              cancellable=rp.get("cancellable", False),
              interruptible=rp.get("interruptible", True),
          ))
        elif rp_type == "end_session":
          parts.append(Part.from_end_session(  # pylint: disable=undefined-variable
              reason=rp.get("reason", "completed"),
              escalated=rp.get("escalated", False),
          ))
        elif rp_type == "transfer":
          parts.append(Part.from_agent_transfer(  # pylint: disable=undefined-variable
              agent=rp["agent"],
          ))
    fc = result.get("function_call")
    if fc:
      fn_call = Part.from_function_call(  # pylint: disable=undefined-variable
          name=fc["name"],
          args=fc.get("args", {}),
      )
      parts.append(fn_call)
    if parts:
      return LlmResponse.from_parts(  # pylint: disable=undefined-variable
          parts=parts,
      )

  return {"decision": "OK"}
