"""Tool to discard pending slot values after user rejects readback."""

from typing import Any


def reject_pending() -> dict[str, Any]:
  """Discard all pending slot values.

  Call when guest says the readback is wrong.

  Returns:
    Dict with discarded slot names and stored=True.
  """
  sm = context.state['sm']  # pylint: disable=undefined-variable
  sm['_rejection_requested'] = True
  sm['_rejection_snapshot'] = dict(sm.get('pending', {}))
  return {'stored': True}
