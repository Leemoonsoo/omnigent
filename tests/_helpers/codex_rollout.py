"""Build Codex rollout JSONL files in the paginated format for import tests."""

from __future__ import annotations

import json
from pathlib import Path


def codex_message(role: str, text: str) -> dict[str, object]:
    """A Codex ``message`` response item, as a rollout's ``response_item`` payload."""
    content_type = "input_text" if role == "user" else "output_text"
    return {"type": "message", "role": role, "content": [{"type": content_type, "text": text}]}


class CodexRollout:
    """One Codex rollout whose records carry consecutive ordinals.

    Numbering starts at ``start_ordinal``, else at the ``history_base`` cutoff (where Codex
    numbers a fork's records from), else at 0. ``meta`` fills the ``session_meta`` payload.
    """

    def __init__(
        self, thread_id: str, *, start_ordinal: int | None = None, **meta: object
    ) -> None:
        if start_ordinal is None:
            base = meta.get("history_base")
            cutoff = base.get("end_ordinal_exclusive") if isinstance(base, dict) else None
            start_ordinal = (
                cutoff if isinstance(cutoff, int) and not isinstance(cutoff, bool) else 0
            )
        self.next_ordinal = start_ordinal
        self.records: list[dict[str, object]] = []
        self.append("session_meta", {"id": thread_id, **meta})

    def append(self, kind: str, payload: dict[str, object]) -> None:
        """Append one record with the next ordinal."""
        self.records.append({"ordinal": self.next_ordinal, "type": kind, "payload": payload})
        self.next_ordinal += 1

    def turn(self, index: int, user_text: str, assistant_text: str) -> None:
        """Append a turn: its ``turn_context`` plus a user and an assistant message."""
        self.append("turn_context", {"turn_id": f"turn_{index}"})
        self.append("response_item", codex_message("user", user_text))
        self.append("response_item", codex_message("assistant", assistant_text))

    def write(self, path: Path) -> None:
        """Write the rollout as JSONL, creating parent directories."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(f"{json.dumps(r)}\n" for r in self.records), encoding="utf-8")
