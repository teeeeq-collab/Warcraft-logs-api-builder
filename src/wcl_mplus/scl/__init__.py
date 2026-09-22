"""Compression experiments for LLM-facing timelines.

Nothing here is standardized. The name `SCL/1` is reserved and must not be used
until the gate in `SCL_EXPERIMENT_PLAN.md` closes; everything produced now is
`experimental-<n>` and every decoder rejects a version it does not recognise.

Two tracks, deliberately not competing:

* **Track A** -- exact or explicitly lossy event timelines (candidates A-E, G).
  Measured on tokens, reversibility, chronological comprehension.
* **Track B** -- semantic state/action/outcome (candidate F). Measured on
  teaching information per token. It encodes different information, so
  comparing it to Track A on tokens-per-event is a category error.

Track B is downstream of the gameplay-state engine and is not built here.
"""

from __future__ import annotations

#: Experimental encoding version. Every encoded document carries it and every
#: decoder refuses a version it does not know: a format mismatch must fail
#: loudly rather than decode into plausible nonsense.
SCL_VERSION = "experimental-1"

__all__ = ["SCL_VERSION"]
