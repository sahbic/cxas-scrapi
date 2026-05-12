"""Executor tool: complete a reservation booking."""

from typing import Any


def book_reservation(
    party_size: int,  # pylint: disable=unused-argument
    preferred_date: str,
    selected_time: str,
    guest_name: str,
    special_requests: str,  # pylint: disable=unused-argument
    large_party_phone: str = "",  # pylint: disable=unused-argument
) -> dict[str, Any]:
  """Book a reservation with the provided details.

  Args:
    party_size: Number of guests.
    preferred_date: Date in YYYY-MM-DD format.
    selected_time: Time in HH:MM 24-hour format.
    guest_name: Name for the reservation.
    special_requests: Any special requests or "none".
    large_party_phone: Contact phone for large parties (optional).

  Returns:
    Dict with confirmation_number and success flag.
  """
  hash_input = preferred_date + selected_time + guest_name
  conf = f"BN-{abs(hash(hash_input)) % 10000:04d}"
  return {"confirmation_number": conf, "success": True}
