"""after_model_callback — Inject pending payloads into the LLM's response.

FRAMEWORK CODE — shared across all agents using the slot-filling engine.
Do not add agent-specific logic here; customize behavior via dag_config.

The engine stashes two kinds of payloads for non-preempted turns:
  - sm["_pending_payloads"]: announce-slot payloads (welcome cards)
  - sm["_pending_question_payloads"]: question-slot payloads (chips),
    with the slot name so we can skip injection if the slot was filled.

Announce payloads are always injected on the first model call.
Question payloads are only injected when:
  1. There are no announce payloads in the same turn, AND
  2. The target slot is still unfilled (the LLM didn't call
     the setter in this response).
"""

import json as json_lib
from typing import Optional


def _extract_payload_parts(response_parts):
  """Extract only payload-type parts as CES Parts."""
  parts = []
  for rp in response_parts:
    if rp.get("type") == "payload":
      parts.append(Part.from_json(  # pylint: disable=undefined-variable
          json_lib.dumps(rp["data"]),
      ))
  return parts


def after_model_callback(callback_context: CallbackContext, llm_response: LlmResponse) -> Optional[LlmResponse]:  # pylint: disable=undefined-variable
  """Inject stashed payloads into the LLM response if present."""
  sm = callback_context.state.get("sm", {})
  debug = sm.get("_debug_mode", False)
  debug_log = sm.get("_debug_log", [])

  announce = sm.pop("_pending_payloads", None)
  question = sm.pop("_pending_question_payloads", None)

  if debug:
    debug_log.append(
        f"after_model: announce={bool(announce)}"
        f" question={bool(question)}"
        f" llm_parts={len(llm_response.content.parts)}"
    )

  if not announce and not question:
    return None

  # Guard: only inject payloads on the first model call per turn.
  # Check for ANY prior agent output (text, tool calls, or payloads).
  for event in reversed(callback_context.events):
    if event.is_user():
      break
    if event.is_agent():
      if event.parts():
        if debug:
          debug_log.append("after_model: GUARD_SKIP prior_agent_output")
        return None

  extra_parts = []

  if announce:
    extra_parts.extend(_extract_payload_parts(announce))

  if question and not announce:
    slot_name = question.get("slot")
    filled = sm.get("filled", {})
    pending = sm.get("pending", {})
    if slot_name and slot_name not in filled and slot_name not in pending:
      extra_parts.extend(_extract_payload_parts(question.get("parts", [])))
    elif debug:
      debug_log.append(
          f"after_model: SKIP_QUESTION slot={slot_name}"
          f" in_filled={slot_name in filled}"
          f" in_pending={slot_name in pending}"
      )

  if debug:
    debug_log.append(f"after_model: injecting={len(extra_parts)}")

  if not extra_parts:
    return None

  combined = list(llm_response.content.parts) + extra_parts
  return LlmResponse.from_parts(parts=combined)  # pylint: disable=undefined-variable
