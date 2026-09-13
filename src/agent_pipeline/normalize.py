"""Input normalisation for task_key derivation (ADR-0002).

`task_key = hash(source_id, normalised_input)` is only stable if this function
is stable, so what it does and does not do is deliberately narrow:

Does:
- Strips leading/trailing whitespace from string values, recursively through
  nested dicts and lists.
- Produces a deterministic ordering of JSON object keys (via `json.dumps`'s
  `sort_keys`), so the same logical input normalises to the same string
  regardless of the key order it arrived in.

Deliberately does not:
- Change value types (e.g. coerce `"1"` to `1`). Type is part of the input's
  meaning; guessing a coercion here could silently merge task keys that were
  never meant to be the same, or split ones that were.
- Reorder list elements. List order is frequently semantically meaningful
  (an ordered list of steps, a conversation history); collapsing two
  differently-ordered lists to the same key would be a correctness bug, not
  a normalisation.
- Perform any semantic normalisation (e.g. case folding). Case can carry
  meaning in task content (an identifier, a URL, quoted text); folding it away
  risks treating genuinely different inputs as the same task.

Anything not listed above is out of scope on purpose. If a normalisation rule
needs to change, it changes here — nowhere else re-implements any part of it.
"""

import hashlib
import json
from typing import Any


def _strip_strings(value: Any) -> Any:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return {key: _strip_strings(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_strip_strings(item) for item in value]
    return value


def normalise(raw_input: dict[str, Any]) -> str:
    stripped = _strip_strings(raw_input)
    return json.dumps(stripped, sort_keys=True, separators=(",", ":"))


def derive_task_key(raw_input: dict[str, Any]) -> str:
    """task_key = sha256(normalised_input), hex digest, no prefix.

    ADR-0002 defines task_key as hash(source_id, normalised_input) — a source
    identifier combined with the content hash. There is no source_id concept
    anywhere in the pipeline yet (no batch/source model exists), so this
    hashes normalised_input alone for now. Deliberate simplification, not a
    forgotten half of the ADR: revisit once a real source_id exists.
    """
    return hashlib.sha256(normalise(raw_input).encode("utf-8")).hexdigest()
