"""Before-model callback — DAG engine with config loaded from dag_config tool.

CES calls this before EACH model invocation, including after tool
results within the same turn. Contains the full slot-filling framework
inline. Config is loaded from the dag_config tool (the only agent-specific
component).

Config and SM are loaded from state (set by before_agent_callback).
"""

import datetime
import random
import re
from typing import Any, Optional


# ═════════════════════════════════════════════════════════════════════
# HELPERS
# ═════════════════════════════════════════════════════════════════════


def _normalize_sources(source) -> list[str]:
  """Normalize slot source to a list."""
  if isinstance(source, list):
    return source
  return [source] if source else ["user"]


# ── Built-in readback formatters ──────────────────────────────────


def _format_date(v: str) -> str:
  """Format date as 'on Month Nth'."""
  try:
    dt = datetime.datetime.strptime(str(v), "%Y-%m-%d")
    day = dt.day
    suffix = (
        "th"
        if 11 <= day <= 13
        else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    )
    return f"on {dt.strftime('%B')} {day}{suffix}"
  except (ValueError, TypeError):
    return f"on {v}"


def _format_time(v: str) -> str:
  """Format time as 'at H:MM AM/PM'."""
  try:
    parts = str(v).split(":")
    h, m = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    period = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"at {h12}:{m:02d} {period}"
  except (ValueError, TypeError, IndexError):
    return f"at {v}"


def _format_prefix(v, text: str = "") -> str:
  """Format value with a text prefix."""
  return f"{text} {v}"


def _format_plural(v, one: str = "", other: str = "") -> str:
  """Format value with singular/plural unit."""
  n = int(v)
  return f"{n} {one if n == 1 else other}"


def _format_none_sub(v, default: str = "") -> str:
  """Substitute a default for none-like values."""
  if str(v).lower() in ("none", "no", "nothing"):
    return default
  return str(v)


_BUILTIN_FORMATTERS = {
    "date": _format_date,
    "time": _format_time,
}


# ── Config compilation ────────────────────────────────────────────

_SAFE_EVAL_GLOBALS = {
    "__builtins__": {
        "int": int, "str": str, "len": len,
        "float": float, "bool": bool,
    },
}

_COMPILED_CONFIG = None


def _compile_formatter(fmt):
  """Resolve a format spec to a callable."""
  if fmt is None:
    return None
  if callable(fmt):
    return fmt
  if isinstance(fmt, str):
    if fmt in _BUILTIN_FORMATTERS:
      return _BUILTIN_FORMATTERS[fmt]
    return eval(fmt, _SAFE_EVAL_GLOBALS)  # pylint: disable=eval-used
  if isinstance(fmt, dict):
    fmt_type = fmt.get("type", "")
    if fmt_type == "prefix":
      text = fmt["text"]
      return lambda v, _t=text: _format_prefix(v, text=_t)
    if fmt_type == "plural":
      one, other = fmt["one"], fmt["other"]
      return lambda v, _o=one, _p=other: _format_plural(v, one=_o, other=_p)
    if fmt_type == "none_sub":
      default = fmt["default"]
      return lambda v, _d=default: _format_none_sub(v, default=_d)
    if fmt_type in _BUILTIN_FORMATTERS:
      return _BUILTIN_FORMATTERS[fmt_type]
    raise ValueError(f"Unknown readback_fmt type: {fmt_type!r}")
  return None


def _compile_config(config: dict[str, Any]) -> dict[str, Any]:
  """Compile string conditions and formatters to callables."""
  compiled_slots = []
  for slot_def in config["slots"]:
    slot = dict(slot_def)
    cond = slot.get("condition")
    if isinstance(cond, str):
      slot["condition"] = eval(  # pylint: disable=eval-used
          cond, _SAFE_EVAL_GLOBALS,
      )
    slot["readback_fmt"] = _compile_formatter(slot.get("readback_fmt"))
    compiled_slots.append(slot)
  compiled_tasks = []
  for task_def in config["tasks"]:
    task = dict(task_def)
    cond = task.get("condition")
    if isinstance(cond, str):
      task["condition"] = eval(  # pylint: disable=eval-used
          cond, _SAFE_EVAL_GLOBALS,
      )
    compiled_tasks.append(task)
  compiled = dict(config)
  compiled["slots"] = compiled_slots
  compiled["tasks"] = compiled_tasks
  return compiled


def _validate_config(config):
  """Validate config structure and references."""
  slots = config["slots"]
  tasks = config["tasks"]
  slot_map = {s["name"]: s for s in slots}

  slot_names = [s["name"] for s in slots]
  slot_set = set(slot_names)
  task_names = {t["name"] for t in tasks}

  if len(slot_names) != len(slot_set):
    dupes = [n for n in slot_names if slot_names.count(n) > 1]
    raise ValueError(f"Duplicate slot names: {set(dupes)}")

  for task in tasks:
    for inp in task["inputs"]:
      if inp not in slot_set:
        raise ValueError(
            f"Task '{task['name']}' input '{inp}' not in slots"
        )
    for slot_name in task.get("outputs", {}).values():
      if slot_name not in slot_set:
        raise ValueError(
            f"Task '{task['name']}' output '{slot_name}'"
            " not in slots"
        )
    if not task.get("tool"):
      raise ValueError(
          f"Task '{task['name']}' has no 'tool' key"
      )
    for req in task.get("requires", []):
      if req not in slot_set:
        raise ValueError(
            f"Task '{task['name']}' requires '{req}'"
            " not in slots"
        )
    condition = task.get("condition")
    if (condition is not None
        and not callable(condition)
        and not isinstance(condition, str)):
      raise ValueError(
          f"Task '{task['name']}' condition must be callable or"
          f" string, got {type(condition)}"
      )

  for slot in slots:
    sources = _normalize_sources(slot.get("source", "user"))
    for source in sources:
      if source.startswith("task:"):
        src_task = source[5:]
        if src_task not in task_names:
          raise ValueError(
              f"Slot '{slot['name']}' references unknown task"
              f" '{src_task}'"
          )
    condition = slot.get("condition")
    if (condition is not None
        and not callable(condition)
        and not isinstance(condition, str)):
      raise ValueError(
          f"Slot '{slot['name']}' condition must be callable or"
          f" string, got {type(condition)}"
      )
    if "announce" in sources:
      if not slot.get("message"):
        raise ValueError(
            f"Announce slot '{slot['name']}'"
            " requires 'message'"
        )
      if slot.get("setter"):
        raise ValueError(
            f"Announce slot '{slot['name']}'"
            " must not have 'setter'"
        )
    for req in slot.get("requires", []):
      if req not in slot_set:
        raise ValueError(
            f"Slot '{slot['name']}' requires unknown"
            f" '{req}'"
        )

  def _has_cycle(name, visited, stack):
    visited.add(name)
    stack.add(name)
    slot_def = slot_map.get(name)
    if slot_def:
      for req in slot_def.get("requires", []):
        if req not in visited:
          if _has_cycle(req, visited, stack):
            return True
        elif req in stack:
          return True
    stack.discard(name)
    return False

  visited, stack = set(), set()
  for name in slot_names:
    if name not in visited:
      if _has_cycle(name, visited, stack):
        raise ValueError(
            f"Circular requires involving '{name}'"
        )


# ═════════════════════════════════════════════════════════════════════
# SLOT STATE HELPERS
# ═════════════════════════════════════════════════════════════════════


def _is_slot_active(slot_def, filled):
  """Check if a conditional slot is active."""
  condition = slot_def.get("condition")
  if condition is None:
    return True
  try:
    return bool(condition(filled))
  except Exception:  # pylint: disable=broad-except
    return True


def _is_task_active(task_def, filled):
  """Check if a conditional task is active."""
  condition = task_def.get("condition")
  if condition is None:
    return True
  try:
    return bool(condition(filled))
  except Exception:  # pylint: disable=broad-except
    return True


def _resolve_formatter(fmt):
  """Resolve a compiled formatter to a callable."""
  if fmt is None:
    return None
  if callable(fmt):
    return fmt
  if isinstance(fmt, str):
    return _BUILTIN_FORMATTERS.get(fmt)
  return None


# ═════════════════════════════════════════════════════════════════════
# AFFIRMATIVE DETECTION
# ═════════════════════════════════════════════════════════════════════


_AFFIRMATIVES = frozenset({
    "yes", "yeah", "yep", "yup", "yah", "ya",
    "correct", "right",
    "sure", "sounds good", "looks good",
    "ok", "okay", "perfect", "great", "exactly",
    "confirmed", "confirm",
    "absolutely", "definitely", "certainly",
    "that's right", "that is right",
    "that's correct", "that is correct",
    "looks right", "that looks right", "that sounds right",
})

_CORRECTION_SIGNALS = frozenset({
    "but", "actually", "wait", "change", "different",
    "instead", "not", "no", "wrong", "except", "however",
    "although", "though",
})

_STRIP_PUNCT = str.maketrans("", "", ".,;:!?\"'")


def _is_affirmative(text: str) -> bool:
  """Detect affirmative user responses."""
  if not text:
    return False
  normalized = text.lower().strip().rstrip(".,!? ")
  if normalized in _AFFIRMATIVES:
    return True
  words = [w.translate(_STRIP_PUNCT) for w in normalized.split()]
  if len(words) <= 5 and words and words[0] in _AFFIRMATIVES:
    return not any(w in _CORRECTION_SIGNALS for w in words[1:])
  return False


# ═════════════════════════════════════════════════════════════════════
# LOGGING
# ═════════════════════════════════════════════════════════════════════


def _log(tag, **data):
  """Emit a structured log line."""
  parts = " ".join(f"{k}={v!r}" for k, v in data.items())
  print(f"[slot-filling:{tag}]" + (f" {parts}" if parts else ""))


def _log_progress(filled, pending, last_state):
  """Log slot state changes since last turn."""
  last_filled = last_state.get("filled", {})
  last_pending = last_state.get("pending", {})
  confirmed = sorted(
      k for k in set(last_pending) if k in filled and k not in last_filled
  )
  task_out = {
      k: filled[k]
      for k in set(filled) - set(last_filled) - set(confirmed)
  }
  new_pending = {k: pending[k] for k in set(pending) - set(last_pending)}
  rejected = sorted(
      k for k in set(last_pending) if k not in pending and k not in filled
  )
  if not (confirmed or task_out or new_pending or rejected):
    return
  _log("progress",
       **({} if not new_pending else {"pending+": new_pending}),
       **({} if not confirmed else {"confirmed": confirmed}),
       **({} if not task_out else {"task+": task_out}),
       **({} if not rejected else {"rejected": rejected}))


def _log_invoke(n, phase, filled, pending, fresh_pending, hidden, *,
                asking=None, reading_back=None, fired=None, done=False,
                preempted=None, deferred=None):
  """Log a DAG invocation with current state."""
  _log("invoke",
       n=n, phase=phase,
       **({} if not filled else {"filled": sorted(filled)}),
       **({} if not pending else {"pending": sorted(pending)}),
       **({} if not deferred else {"deferred": sorted(deferred)}),
       **({} if not fresh_pending else {"fresh": True}),
       **({} if not hidden else {"hidden": sorted(hidden)}),
       **({} if asking is None else {"asking": asking[:80]}),
       **({} if reading_back is None else {"rb": reading_back[:80]}),
       **({} if fired is None else {"fired": fired}),
       **({} if not done else {"done": True}),
       **({} if preempted is None else {"preempted": preempted[:80]}))


# ═════════════════════════════════════════════════════════════════════
# DAG ENGINE COMPONENTS
# ═════════════════════════════════════════════════════════════════════


def _handle_state_change(
    sm: dict[str, Any],
    filled: dict[str, Any], pending: dict[str, Any],
    last_state: dict[str, Any],
) -> None:
  """Reset stall counters on state changes."""
  current_state = {
      "filled": filled, "pending": pending,
      "deferred": sm.get("deferred", {}),
  }
  if current_state == last_state:
    return

  if not sm.pop("_auto_confirm_pending", False):
    sm["_progress_turns"] = 0
  sm.pop("_readback_stall", None)

  retries = sm.get("_retries", {})
  last_filled = last_state.get("filled", {})
  last_pending = last_state.get("pending", {})
  for name in set(filled) - set(last_filled):
    retries.pop(f"slot:{name}", None)
  for name in set(pending) - set(last_pending):
    retries.pop(f"slot:{name}", None)
  if last_pending and not pending:
    retries.pop("readback", None)

  _log_progress(filled, pending, last_state)


def _auto_promote_and_route(
    slots, tasks, task_results, slot_map,
    filled: dict[str, Any], pending: dict[str, Any],
    deferred: dict[str, Any],
) -> None:
  """Promote non-readback pending slots and route deferred."""
  readback_set = {
      s["name"] for s in slots if s.get("requires_readback")
  }
  for name in [k for k in pending if k not in readback_set]:
    filled[name] = pending.pop(name)

  deferred_eligible = _compute_deferred_eligible(
      slots, tasks, task_results, slot_map,
  )
  for name in [k for k in pending if k in deferred_eligible]:
    deferred[name] = pending.pop(name)
  _check_deferred_groups(
      tasks, filled, pending, deferred, task_results, slot_map,
  )


def _deactivate_conditional_slots(
    slots, filled: dict[str, Any], pending: dict[str, Any],
    deferred: dict[str, Any], retries: dict[str, Any],
) -> None:
  """Remove slots whose conditions are no longer met."""
  for slot_def in slots:
    if "condition" not in slot_def:
      continue
    name = slot_def["name"]
    if not _is_slot_active(slot_def, filled):
      if name in filled:
        filled.pop(name)
        retries.pop(f"slot:{name}", None)
        _log("slot_deactivated", slot=name, source="filled")
      if name in pending:
        pending.pop(name)
        _log("slot_deactivated", slot=name, source="pending")
      if name in deferred:
        deferred.pop(name)
        _log("slot_deactivated", slot=name, source="deferred")


def fill_slots(
    sm: dict[str, Any],
    config: dict[str, Any],
    values: dict[str, Any],
    skip_readback: bool = True,
) -> dict[str, list[str]]:
  """Fill slots programmatically.

  Writes values into the state machine so the DAG engine
  advances on the next before_model_callback invocation.

  Args:
    sm: The state machine dict (callback_context.state["sm"]).
    config: The compiled DAG config from _compile_config().
    values: Dict of {slot_name: value} to fill.
    skip_readback: If True (default), write directly to
      filled (no user confirmation). If False, write to
      pending (triggers readback/confirm flow).

  Returns:
    {"filled": [names written], "skipped": [names skipped]}.
    Slots are skipped if unknown, already filled, or their
    condition is not met.
  """
  slot_map = {s["name"]: s for s in config["slots"]}
  filled = sm.setdefault("filled", {})
  result = {"filled": [], "skipped": []}
  for name, value in values.items():
    slot_def = slot_map.get(name)
    if not slot_def:
      result["skipped"].append(name)
      continue
    if name in filled:
      result["skipped"].append(name)
      continue
    if not _is_slot_active(slot_def, filled):
      result["skipped"].append(name)
      continue
    if skip_readback:
      filled[name] = value
    else:
      sm.setdefault("pending", {})[name] = value
    _log("fill_slots", slot=name, value=value)
    result["filled"].append(name)
  return result


def _try_auto_confirm(
    phase: str, last_user_text: str, sm: dict[str, Any],
) -> Optional[dict[str, Any]]:
  """Auto-confirm pending slots on affirmative reply."""
  if (phase != "awaiting_confirmation"
      or not last_user_text
      or not _is_affirmative(last_user_text)):
    return None
  _log("auto_confirm", user_msg=last_user_text)
  sm["_progress_turns"] = sm.get("_progress_turns", 0) + 1
  sm["_auto_confirm_pending"] = True
  return {
      "hide_tools": [],
      "preempt": True,
      "force_preempt": True,
      "function_call": {"name": "confirm_pending", "args": {}},
      "message": "",
  }


def _handle_readback_stall(
    sm: dict[str, Any], pending: dict[str, Any],
    readback_transition: bool,
    readback_retry: dict[str, Any],
    slots, filled: dict[str, Any], slot_map: dict[str, Any],
    readback_tools, executor_tool_names: list[str],
    deferred: dict[str, Any],
    inv_n: int,
    hide_tools: list[str],
) -> Optional[dict[str, Any]]:
  """Detect and handle stalled readback confirmations."""
  if not pending or readback_transition or "_rejection_snapshot" in sm:
    return None

  stall = sm.get("_readback_stall", 0) + 1
  sm["_readback_stall"] = stall
  if stall < 3:
    return None

  sm["pending"] = {}
  pending.clear()
  sm.pop("_readback_stall", None)
  retries = sm.setdefault("_retries", {})
  retries["readback"] = retries.get("readback", 0) + 1
  _log("readback_stall", retries=retries["readback"])
  max_rb = readback_retry.get("max_retries", 2)

  if retries["readback"] >= max_rb:
    exhaust = readback_retry.get("on_exhaust", {})
    if exhaust.get("then") == "escalate":
      sm["status"] = "escalated"
    msg = exhaust.get("say", "Please call us for help.")
    _log_invoke(inv_n, "readback_stall", filled, {}, False, hide_tools,
                preempted=msg, deferred=deferred)
    return {"hide_tools": hide_tools, "preempt": True, "message": msg}

  hide_tools = _compute_hidden_tools(
      slots, filled, {}, readback_tools, slot_map,
      fresh_pending=False, executor_tools=executor_tool_names,
  )
  next_q = _find_next_question(
      slots, filled, {}, slot_map, deferred=deferred,
  )
  msg = next_q.get("system_message", "")
  _log_invoke(inv_n, "readback_stall_retry", filled, {}, False, hide_tools,
              asking=msg, deferred=deferred)
  return {
      "hide_tools": hide_tools,
      "preempt": True,
      "force_preempt": True,
      "message": msg,
  }


def _handle_progress_stall(
    sm: dict[str, Any], last_user_text: str,
    progress_stall_cfg: dict[str, Any],
    filled: dict[str, Any], pending: dict[str, Any],
    deferred: dict[str, Any],
    fresh_pending: bool, hide_tools: list[str],
    inv_n: int,
) -> Optional[dict[str, Any]]:
  """Escalate after too many turns without progress."""
  if last_user_text:
    progress = sm.get("_progress_turns", 0) + 1
    sm["_progress_turns"] = progress
  else:
    progress = sm.get("_progress_turns", 0)
  max_turns = progress_stall_cfg.get("max_turns", 8)
  if progress < max_turns:
    return None

  _log("progress_stall", turns=progress)
  exhaust = progress_stall_cfg.get("on_exhaust", {})
  if exhaust.get("then") == "escalate":
    sm["status"] = "escalated"
  msg = exhaust.get("say", "Please call us for help.")
  _log_invoke(inv_n, "progress_stall", filled, pending, fresh_pending,
              hide_tools, preempted=msg, deferred=deferred)
  return {"hide_tools": hide_tools, "preempt": True, "message": msg}


def _handle_post_executor(
    sm: dict[str, Any], tasks: list[dict[str, Any]],
    task_results: dict[str, Any],
    filled: dict[str, Any], pending: dict[str, Any],
    deferred: dict[str, Any],
    retries: dict[str, Any], confirm_transition_prefix: str,
    inv_n: int, phase: str, fresh_pending: bool,
    hide_tools: list[str],
) -> tuple[Optional[dict[str, Any]], str]:
  """Handle task executor results and retries."""
  task_just = sm.pop("_task_just_completed", None)
  if not task_just:
    return None, ""

  task_def = next(t for t in tasks if t["name"] == task_just)
  success_key = task_def.get("success_check", "success")
  result = task_results.get(task_just, {})

  if result.get(success_key):
    _log("task", name=task_just, ok=True)
    retries.pop(task_just, None)
    task_msg = ""
    msg_template = task_def.get("then_say", "")
    if msg_template:
      task_msg = msg_template.format(**filled)
    deferred_transition = sm.pop("_deferred_transition", False)
    if deferred_transition and task_msg and confirm_transition_prefix:
      task_msg = f"{confirm_transition_prefix} {task_msg}"
    if task_def.get("terminal"):
      sm["status"] = "complete"
      _log_invoke(inv_n, phase, filled, pending, fresh_pending,
                  hide_tools, fired=task_just, preempted=task_msg,
                  deferred=deferred)
      return {"hide_tools": [], "preempt": True, "message": task_msg}, task_msg
    return None, task_msg

  _log("task", name=task_just, ok=False)
  on_failure = task_def.get("on_failure", {})
  max_retries = on_failure.get("max_retries", 0)
  retries[task_just] = retries.get(task_just, 0) + 1
  for sn in on_failure.get("clear_slots", []):
    filled.pop(sn, None)
  if retries[task_just] >= max_retries:
    exhaust = on_failure.get("on_exhaust", {})
    if exhaust.get("then") == "escalate":
      sm["status"] = "escalated"
    _log("task_exhaust", name=task_just)
    exhaust_msg = exhaust.get("say", "An error occurred.")
    _log_invoke(inv_n, phase, filled, pending, fresh_pending,
                hide_tools, preempted=exhaust_msg, deferred=deferred)
    return {
        "hide_tools": hide_tools, "preempt": True, "message": exhaust_msg,
    }, ""
  retry_msg = on_failure.get("retry_say", "Let me try again.")
  _log_invoke(inv_n, phase, filled, pending, fresh_pending,
              hide_tools, preempted=retry_msg, deferred=deferred)
  return {
      "hide_tools": hide_tools, "preempt": True, "message": retry_msg,
  }, ""


def _build_readback_hint(
    slots, pending: dict[str, Any], filled: dict[str, Any],
    fresh_pending: bool, promoted_from_deferred: bool = False,
) -> str:
  """Build readback hint for system instruction."""
  if not fresh_pending or not pending:
    return ""
  hint_parts = []
  for slot_def in slots:
    name = slot_def["name"]
    if name not in pending:
      continue
    if not _is_slot_active(slot_def, filled):
      continue
    formatter = _resolve_formatter(slot_def.get("readback_fmt"))
    val = formatter(pending[name]) if formatter else str(pending[name])
    if promoted_from_deferred:
      hint_parts.append(f"{name}: {val}")
    else:
      hint_parts.append(val)
  if not hint_parts:
    return ""
  if promoted_from_deferred:
    return "\n".join(f"  - {p}" for p in hint_parts)
  return ", ".join(hint_parts)


# ═════════════════════════════════════════════════════════════════════
# DAG EVALUATION
# ═════════════════════════════════════════════════════════════════════


def _build_readback(slots, pending, filled):
  """Build readback confirmation prompt."""
  fragments = []
  for slot_def in slots:
    name = slot_def["name"]
    if name not in pending:
      continue
    if not _is_slot_active(slot_def, filled):
      continue
    formatter = _resolve_formatter(slot_def.get("readback_fmt"))
    if formatter:
      fragments.append(formatter(pending[name]))
    else:
      fragments.append(f"{name}: {pending[name]}")
  if not fragments:
    return None
  summary = ", ".join(fragments)
  return {
      "action": "awaiting_readback",
      "system_message": f"Just to confirm — {summary}. Is that correct?",
  }


def _find_next_question(
    slots, filled, pending, slot_map, deferred=None,
):
  """Find the next unfilled slot to ask about."""
  deferred = deferred or {}
  for slot_def in slots:
    name = slot_def["name"]
    if not _is_slot_active(slot_def, filled):
      continue
    if name in filled or name in pending or name in deferred:
      continue
    if "user" not in _normalize_sources(slot_def.get("source", "user")):
      continue
    requires = slot_def.get("requires", [])
    if not all(
        req in filled
        or not _is_slot_active(slot_map[req], filled)
        for req in requires
    ):
      continue
    ask_template = slot_def.get("ask", f"Please provide {name}.")
    ask = ask_template.format(**filled)
    return {"action": "next_question", "system_message": ask}
  return {
      "action": "all_done",
      "system_message": "All information collected!",
  }


def _find_next_slot_action(
    slots, filled, pending, slot_map, deferred=None,
):
  """Find the next slot action (announce or user question).

  Walks slots in declaration order. Returns the first
  eligible announce or user slot. Announce slots are filled
  by the framework; user slots produce a question prompt.

  Args:
    slots: Ordered list of slot definitions.
    filled: Dict of filled slot values.
    pending: Dict of pending slot values.
    slot_map: Dict mapping slot name to slot definition.
    deferred: Optional dict of deferred slot values.

  Returns:
    Action dict with 'action' key ('announce',
    'next_question', or 'all_done').
  """
  deferred = deferred or {}
  for slot_def in slots:
    name = slot_def["name"]
    if not _is_slot_active(slot_def, filled):
      continue
    if name in filled or name in pending or name in deferred:
      continue
    sources = _normalize_sources(
        slot_def.get("source", "user"),
    )
    requires = slot_def.get("requires", [])
    if not all(
        req in filled
        or not _is_slot_active(slot_map[req], filled)
        for req in requires
    ):
      continue
    if "announce" in sources:
      return {
          "action": "announce",
          "slot_def": slot_def,
      }
    if "user" in sources:
      ask = slot_def.get(
          "ask", f"Please provide {name}.",
      )
      try:
        ask = ask.format(**filled)
      except KeyError:
        pass
      return {
          "action": "next_question",
          "system_message": ask,
      }
  return {
      "action": "all_done",
      "system_message": "All information collected!",
  }


def _compute_dag_state(
    tasks, slots, filled, pending, task_results, slot_map,
    deferred=None,
):
  """Evaluate the DAG to determine the next action."""
  if pending:
    rb = _build_readback(slots, pending, filled)
    if rb is not None:
      return rb

  for task in tasks:
    task_name = task["name"]
    success_key = task.get("success_check", "success")

    if (task_name in task_results
        and task_results[task_name].get(success_key)):
      continue

    if not _is_task_active(task, filled):
      continue

    active_inputs = [
        s for s in task["inputs"]
        if _is_slot_active(slot_map[s], filled)
    ]
    if not all(s in filled for s in active_inputs):
      continue
    task_reqs = [
        r for r in task.get("requires", [])
        if _is_slot_active(slot_map[r], filled)
    ]
    if not all(r in filled for r in task_reqs):
      continue

    return {
        "action": "fire",
        "task_name": task_name,
        "task_def": task,
    }

  return _find_next_slot_action(
      slots, filled, pending, slot_map, deferred=deferred,
  )


def _handle_slot_errors(sm, slots):
  """Process validation errors and manage retries."""
  errors = sm.pop("_slot_errors", [])
  if not errors:
    return None, False

  retries = sm.setdefault("_retries", {})
  filled = sm.get("filled", {})
  messages = []

  for err in errors:
    slot_name = err["slot"]
    error_code = err["code"]
    retry_key = f"slot:{slot_name}"

    slot_def = next(
        (s for s in slots if s["name"] == slot_name), None,
    )
    if not slot_def:
      continue

    validation = slot_def.get("validation", {})
    max_retries = validation.get("max_retries", 3)

    retries[retry_key] = retries.get(retry_key, 0) + 1
    _log("slot_error", slot=slot_name,
         code=error_code, retries=retries[retry_key])

    if retries[retry_key] >= max_retries:
      exhaust = validation.get("on_exhaust", {})
      if exhaust.get("then") == "escalate":
        sm["status"] = "escalated"
      msg = exhaust.get(
          "say", "An error occurred. Please call us for help.",
      )
      try:
        msg = msg.format(**filled)
      except KeyError:
        pass
      _log("slot_error_exhaust", slot=slot_name)
      return msg, True

    error_messages = validation.get("errors", {})
    msg = error_messages.get(
        error_code, "Could you try that again?",
    )
    try:
      msg = msg.format(**filled)
    except KeyError:
      pass
    messages.append(msg)

  if not messages:
    return None, False

  combined = " ".join(messages)
  return combined, False


def _compute_deferred_eligible(slots, tasks, task_results, slot_map):
  """Identify slots eligible for deferred confirmation."""
  eligible = set()
  for slot_def in slots:
    name = slot_def["name"]
    if not slot_def.get("requires_readback"):
      continue
    has_task_requires = False
    for req in slot_def.get("requires", []):
      req_def = slot_map.get(req)
      if req_def and any(
          s.startswith("task:")
          for s in _normalize_sources(req_def.get("source", "user"))
      ):
        has_task_requires = True
        break
    if has_task_requires:
      continue
    is_deferred_input = False
    blocked = False
    for task in tasks:
      if name not in task["inputs"]:
        continue
      if task.get("readback_inputs"):
        is_deferred_input = True
      else:
        sk = task.get("success_check", "success")
        if not (task["name"] in task_results
                and task_results[task["name"]].get(sk)):
          blocked = True
          break
    if is_deferred_input and not blocked:
      eligible.add(name)
  return eligible


def _check_deferred_groups(
    tasks, filled, pending, deferred, task_results, slot_map,
):
  """Promote deferred slots when all group inputs are ready."""
  for task_def in tasks:
    if not task_def.get("readback_inputs"):
      continue
    sk = task_def.get("success_check", "success")
    if (task_def["name"] in task_results
        and task_results[task_def["name"]].get(sk)):
      continue
    deferred_inputs = []
    all_ready = True
    for inp in task_def["inputs"]:
      sd = slot_map.get(inp)
      if not sd:
        continue
      if "user" not in _normalize_sources(sd.get("source", "user")):
        continue
      if not _is_slot_active(sd, filled):
        continue
      if inp in filled or inp in pending:
        continue
      if inp in deferred:
        deferred_inputs.append(inp)
      else:
        all_ready = False
        break
    if all_ready and deferred_inputs:
      for inp in deferred_inputs:
        pending[inp] = deferred.pop(inp)


def _compute_hidden_tools(
    slots, filled, pending, readback_tools, slot_map,
    *, fresh_pending=False, executor_tools=None,
):
  """Determine which tools to hide from the LLM."""
  hidden = []
  if pending:
    if fresh_pending:
      hidden.extend(readback_tools)
  else:
    hidden.extend(readback_tools)
  for slot_def in slots:
    setter = slot_def.get("setter")
    if not setter:
      continue
    name = slot_def["name"]
    if not _is_slot_active(slot_def, filled):
      hidden.append(setter)
    elif name in filled:
      hidden.append(setter)
    elif name in pending and fresh_pending:
      hidden.append(setter)
    elif not all(
        r in filled
        or not _is_slot_active(slot_map[r], filled)
        for r in slot_def.get("requires", [])
    ):
      hidden.append(setter)
  if executor_tools:
    hidden.extend(executor_tools)
  return hidden


# ═════════════════════════════════════════════════════════════════════
# MAIN ORCHESTRATOR
# ═════════════════════════════════════════════════════════════════════


def _run_slot_filling(
    config: dict[str, Any],
    sm: dict[str, Any],
    last_user_text: str = "",
) -> dict[str, Any]:
  """Run one turn of the slot-filling DAG engine."""
  slots = config["slots"]
  tasks = config["tasks"]
  executors = {t["name"]: t["tool"] for t in tasks}
  readback_tools = ["confirm_pending", "reject_pending"]
  readback_retry = config["readback_retry"]
  progress_stall = config["progress_stall"]
  _prefix_cfg = config.get("confirm_transition_prefix", "")
  if isinstance(_prefix_cfg, list):
    confirm_transition_prefix = (
        random.choice(_prefix_cfg) if _prefix_cfg else ""
    )
  else:
    confirm_transition_prefix = _prefix_cfg

  slot_map = {s["name"]: s for s in slots}
  executor_tool_names = list(executors.values())

  sm["_invoke_n"] = sm.get("_invoke_n", 0) + 1
  inv_n = sm["_invoke_n"]

  filled = sm.get("filled", {})
  pending = sm.get("pending", {})
  deferred = sm.setdefault("deferred", {})
  task_results = sm.get("task_results", {})
  last_state = sm.get("_last_state", {})

  _handle_state_change(sm, filled, pending, last_state)

  _auto_promote_and_route(
      slots, tasks, task_results, slot_map,
      filled, pending, deferred,
  )

  retries = sm.setdefault("_retries", {})
  _deactivate_conditional_slots(
      slots, filled, pending, deferred, retries,
  )

  sm["_last_state"] = {
      "filled": dict(filled),
      "pending": dict(pending),
      "deferred": dict(deferred),
  }
  last_pending = last_state.get("pending", {})
  fresh_pending = bool(set(pending) - set(last_pending))
  last_deferred = last_state.get("deferred", {})
  fresh_deferred = bool(set(deferred) - set(last_deferred))
  promoted_from_deferred = bool(
      set(pending) & (set(last_deferred) - set(last_pending))
  )
  if pending:
    phase = "fresh_readback" if fresh_pending else "awaiting_confirmation"
  else:
    phase = "collection"

  result = _try_auto_confirm(phase, last_user_text, sm)
  if result:
    return result

  hide_tools = _compute_hidden_tools(
      slots, filled, pending, readback_tools, slot_map,
      fresh_pending=fresh_pending, executor_tools=executor_tool_names,
  )

  error_msg, _ = _handle_slot_errors(sm, slots)
  if error_msg:
    sm["_progress_turns"] = 0
    _log_invoke(inv_n, phase, filled, pending, fresh_pending, hide_tools,
                preempted=error_msg, deferred=deferred)
    return {"hide_tools": hide_tools, "preempt": True, "message": error_msg}

  readback_transition = sm.pop("_readback_transition", False)

  result = _handle_readback_stall(
      sm, pending, readback_transition, readback_retry,
      slots, filled, slot_map,
      readback_tools, executor_tool_names,
      deferred, inv_n, hide_tools,
  )
  if result:
    return result

  result = _handle_progress_stall(
      sm, last_user_text, progress_stall,
      filled, pending, deferred,
      fresh_pending, hide_tools, inv_n,
  )
  if result:
    return result

  result, task_msg = _handle_post_executor(
      sm, tasks, task_results, filled, pending, deferred,
      retries, confirm_transition_prefix,
      inv_n, phase, fresh_pending, hide_tools,
  )
  if result:
    return result

  # ── Announce slots (cascade through consecutive) ────────
  announce_msgs = []
  any_announce_preempt = False
  dag_result = _compute_dag_state(
      tasks, slots, filled, pending, task_results, slot_map,
      deferred=deferred,
  )
  while dag_result["action"] == "announce":
    slot_def_a = dag_result["slot_def"]
    name_a = slot_def_a["name"]
    msg_a = slot_def_a["message"]
    try:
      msg_a = msg_a.format(**filled)
    except KeyError:
      pass
    filled[name_a] = True
    announce_msgs.append(msg_a)
    if slot_def_a.get("preempt", True):
      any_announce_preempt = True
    _log("announce", slot=name_a)
    dag_result = _compute_dag_state(
        tasks, slots, filled, pending, task_results,
        slot_map, deferred=deferred,
    )

  if dag_result["action"] == "fire":
    task_def_f = dag_result["task_def"]
    task_name_f = task_def_f["name"]
    tool_name = task_def_f["tool"]
    active_inputs = [
        s for s in task_def_f["inputs"]
        if _is_slot_active(slot_map[s], filled)
    ]
    args = {k: filled[k] for k in active_inputs if k in filled}
    if readback_transition:
      sm["_deferred_transition"] = True
    sm["_last_state"] = {
        "filled": dict(filled),
        "pending": dict(pending),
        "deferred": dict(deferred),
    }
    combined_msg = task_msg
    if announce_msgs:
      announce_text = " ".join(announce_msgs)
      combined_msg = (
          f"{announce_text} {task_msg}"
          if task_msg else announce_text
      )
    _log_invoke(inv_n, phase, filled, pending, fresh_pending,
                hide_tools, fired=task_name_f, deferred=deferred)
    fire_hide = [t for t in hide_tools if t != tool_name]
    return {
        "hide_tools": fire_hide,
        "preempt": True,
        "force_preempt": any_announce_preempt,
        "function_call": {"name": tool_name, "args": args},
        "message": combined_msg,
    }

  msg = task_msg or dag_result.get("system_message", "")
  if announce_msgs:
    announce_text = " ".join(announce_msgs)
    msg = (
        f"{announce_text} {msg}" if msg
        else announce_text
    )
  sm["_last_state"] = {
      "filled": dict(filled), "pending": dict(pending),
      "deferred": dict(deferred),
  }
  if dag_result["action"] == "awaiting_readback" and not fresh_pending:
    msg = ""

  preempt = bool(task_msg) or any_announce_preempt
  if readback_transition and msg and confirm_transition_prefix:
    if not msg.lower().startswith(confirm_transition_prefix.lower()):
      msg = f"{confirm_transition_prefix} {msg}"
    preempt = True

  action = dag_result["action"]
  if preempt:
    _log_invoke(inv_n, phase, filled, pending, fresh_pending, hide_tools,
                preempted=msg, deferred=deferred)
  elif action == "next_question":
    _log_invoke(inv_n, phase, filled, pending, fresh_pending, hide_tools,
                asking=msg or dag_result.get("system_message", ""),
                deferred=deferred)
  elif action == "awaiting_readback" and fresh_pending:
    _log_invoke(inv_n, phase, filled, pending, fresh_pending, hide_tools,
                reading_back=dag_result.get("system_message", ""),
                deferred=deferred)
  else:
    _log_invoke(inv_n, phase, filled, pending, fresh_pending, hide_tools,
                done=(action == "all_done"), deferred=deferred)

  deferred_hint = ""
  if fresh_deferred and not pending:
    next_q = _find_next_question(
        slots, filled, pending, slot_map, deferred=deferred,
    )
    next_msg = next_q.get("system_message", "")
    if next_msg:
      deferred_names = sorted(set(deferred) - set(last_deferred))
      deferred_hint = (
          f"The value(s) just collected ({', '.join(deferred_names)})"
          f" are noted and will be confirmed later together with"
          f" related information. Do NOT read them back or ask for"
          f" confirmation now. Instead, proceed to ask: {next_msg}"
      )

  return {
      "hide_tools": hide_tools,
      "preempt": preempt,
      "force_preempt": any_announce_preempt,
      "message": msg,
      "readback_hint": _build_readback_hint(
          slots, pending, filled, fresh_pending,
          promoted_from_deferred=promoted_from_deferred,
      ),
      "deferred_hint": deferred_hint,
      "promoted_from_deferred": promoted_from_deferred,
  }


# ═════════════════════════════════════════════════════════════════════
# CES CALLBACK ENTRY POINT
# ═════════════════════════════════════════════════════════════════════


def before_model_callback(  # pylint: disable=undefined-variable
    callback_context: CallbackContext,
    llm_request: LlmRequest,
) -> Optional[LlmResponse]:
  """CES entry point: run DAG engine before each LLM call."""
  global _COMPILED_CONFIG  # pylint: disable=global-statement

  # ── Hide dag_config from model ────────────────────────────────
  llm_request.config.hide_tool("dag_config")

  sm = callback_context.state.get("sm", {})

  if sm.get("status") in ("complete", "escalated"):
    return {"decision": "OK"}

  # ── Load and compile config (cached after first call) ─────────
  if _COMPILED_CONFIG is None:
    raw_config = tools.dag_config(  # pylint: disable=undefined-variable
        {},
    ).json()["result"]
    _validate_config(raw_config)
    _COMPILED_CONFIG = _compile_config(raw_config)
  config = _COMPILED_CONFIG

  # ── Extract last user text (CES-specific) ────────────────────
  last_user_text = ""
  if llm_request.contents:
    last_content = llm_request.contents[-1]
    if getattr(last_content, "role", "") == "user":
      for part in getattr(last_content, "parts", []):
        txt = getattr(part, "text", "")
        if txt:
          last_user_text = txt
          break

  # ── Event pre-fill ───────────────────────────────────────────
  if not sm.get("_events_checked"):
    event_data = callback_context.state.get(
        "event_data", {},
    )
    if event_data:
      event_values = {}
      for slot_def in config["slots"]:
        if "event" not in _normalize_sources(
            slot_def.get("source", "user")
        ):
          continue
        key = slot_def.get("event_key", slot_def["name"])
        value = event_data.get(key)
        if value is not None:
          event_values[slot_def["name"]] = value
      if event_values:
        result = fill_slots(sm, config, event_values)
        if result["filled"]:
          sm["_event_prefilled_this_turn"] = True
    sm["_events_checked"] = True

  # ── Derive mappings for after_tool_callback ──────────────────
  if "_setter_slots" not in sm:
    setter_slots = {}
    slot_requires = {}
    slot_validates = {}
    for slot_def in config["slots"]:
      setter = slot_def.get("setter")
      if setter:
        setter_slots[setter] = slot_def["name"]
      if slot_def.get("requires"):
        slot_requires[slot_def["name"]] = slot_def["requires"]
      if slot_def.get("validate_against"):
        slot_validates[slot_def["name"]] = slot_def["validate_against"]
    sm["_setter_slots"] = setter_slots
    sm["_slot_requires"] = slot_requires
    sm["_slot_validates"] = slot_validates
    executor_tasks = {}
    for task_def in config["tasks"]:
      tool_name = task_def.get("tool")
      if tool_name:
        executor_tasks[tool_name] = {
            "task_name": task_def["name"],
            "outputs": task_def.get("outputs", {}),
            "success_check": task_def.get("success_check", "success"),
            "terminal": task_def.get("terminal", False),
        }
    sm["_executor_tasks"] = executor_tasks

  # ── Generate tool selection for before_agent_callback ────────
  filled_for_ts = sm.get("filled", {})
  ts_lines = []
  ordering_parts = []
  prereq_parts = []
  for slot_def in config["slots"]:
    sources = _normalize_sources(slot_def.get("source", "user"))
    if "user" not in sources:
      continue
    name = slot_def["name"]
    hint = slot_def.get("hint", "")
    setter = slot_def.get("setter", "")
    if name in filled_for_ts:
      continue
    if hint and setter and _is_slot_active(slot_def, filled_for_ts):
      ts_lines.append(f"   - {hint} → {setter}")
    if not slot_def.get("condition") and hint and setter:
      ordering_parts.append(name)
    if slot_def.get("requires") and setter:
      prereq_parts.append(
          f"Never call {setter} before"
          f" {', '.join(slot_def['requires'])} are presented."
      )
  sm["_tool_selection"] = "\n".join(ts_lines)
  sm["_slot_ordering"] = " → ".join(ordering_parts)
  sm["_prereq_note"] = " ".join(prereq_parts)

  # ── Run the DAG engine ──────────────────────────────────────
  result = _run_slot_filling(config, sm, last_user_text=last_user_text)

  callback_context.state["sm"] = sm

  # ── Persist directive for next before_agent_callback ─────────
  msg = result.get("message", "")
  if msg:
    sm["_next_directive"] = msg

  # ── Clear prompt variables on completion ─────────────────────
  status = sm.get("status", "in_progress")
  if status in ("complete", "escalated"):
    callback_context.state["slot_filling_protocol"] = ""
    callback_context.state["readback_protocol"] = ""
    callback_context.state["system_directive"] = ""

  # ── Hide tools ───────────────────────────────────────────────
  for tool_name in result.get("hide_tools", []):
    llm_request.config.hide_tool(tool_name)

  # ── System instruction suffix ────────────────────────────────
  readback_hint = result.get("readback_hint", "")
  deferred_hint = result.get("deferred_hint", "")
  si_suffix = ""
  promoted = result.get("promoted_from_deferred", False)
  if readback_hint:
    if promoted:
      si_suffix += (
          f"\n\n<readback_scope>"
          f"\nYou MUST confirm ALL of the following values together"
          f" in a single readback — do NOT omit any:\n{readback_hint}"
          f"\n</readback_scope>"
      )
    else:
      si_suffix += (
          f"\n\n<readback_scope>"
          f"\nThe only values pending confirmation right now: {readback_hint}."
          f" Do not re-read any previously confirmed values."
          f"\n</readback_scope>"
      )
  if deferred_hint:
    si_suffix += (
        f"\n\n<deferred_collection>"
        f"\n{deferred_hint}"
        f"\n</deferred_collection>"
    )

  event_prefilled = sm.pop("_event_prefilled_this_turn", False)
  if event_prefilled and msg:
    si_suffix += (
        f"\n\n<system_directive>\n{msg}\n</system_directive>"
    )

  if llm_request.config.system_instruction:
    si = llm_request.config.system_instruction
    if hasattr(si, "parts") and si.parts:
      text = si.parts[0].text or ""
      text = re.sub(
          r"\n\n<readback_scope>.*?</readback_scope>",
          "", text, flags=re.DOTALL,
      )
      text = re.sub(
          r"\n\n<deferred_collection>.*?</deferred_collection>",
          "", text, flags=re.DOTALL,
      )
      text = re.sub(
          r"\n\n<system_directive>.*?</system_directive>",
          "", text, flags=re.DOTALL,
      )
      if event_prefilled:
        text = re.sub(
            r"<slot_filling_protocol>.*?</slot_filling_protocol>",
            "", text, flags=re.DOTALL,
        )
      si.parts[0].text = text + si_suffix
    elif isinstance(si, str):
      text = re.sub(
          r"\n\n<readback_scope>.*?</readback_scope>", "", si, flags=re.DOTALL,
      )
      text = re.sub(
          r"\n\n<deferred_collection>.*?</deferred_collection>",
          "", text, flags=re.DOTALL,
      )
      text = re.sub(
          r"\n\n<system_directive>.*?</system_directive>",
          "", text, flags=re.DOTALL,
      )
      if event_prefilled:
        text = re.sub(
            r"<slot_filling_protocol>.*?</slot_filling_protocol>",
            "", text, flags=re.DOTALL,
        )
      llm_request.config.system_instruction = text + si_suffix

  # ── Preemption ───────────────────────────────────────────────
  if (result.get("preempt")
      and llm_request.contents
      and (len(llm_request.contents) > 1 or result.get("force_preempt"))):
    parts = []
    if result.get("message"):
      parts.append(Part.from_text(  # pylint: disable=undefined-variable
          text=result["message"],
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
