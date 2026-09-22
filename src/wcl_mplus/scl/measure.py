"""Size and token measurement for Track A candidates.

**Token counts are never estimated.** A measurement whose instrument is unnamed
is not a measurement, and a number that looks like a token count but is really a
word count would decide the format question wrongly and invisibly.

So the tokenizer is resolved explicitly and reports three things: its name,
whether it is the target model's tokenizer, and whether its output is an
estimate. When no real tokenizer is installed the fallback is used, but its
output is called `symbols` rather than `tokens` and every renderer refuses to
present it as a token count.

`tiktoken` is an OpenAI tokenizer. Even when installed it is a **proxy** for any
other model's, and it needs to fetch its vocabulary on first use -- which fails
on an offline machine. Both facts travel with the numbers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from .candidates import CANDIDATES, Encoded
from .dictionaries import Dictionaries
from .model import TimelineEvent


class Tokenizer(Protocol):
    name: str
    is_target_model: bool
    is_estimate: bool

    def count(self, text: str) -> int: ...


@dataclass
class HeuristicTokenizer:
    """Declared fallback. Splits on whitespace and punctuation runs.

    This is **not** a tokenizer and its output is not a token count. It exists
    so the harness runs offline and produces comparable relative numbers, and
    every consumer is told, loudly, not to publish them.
    """

    name: str = "proxy:whitespace-punct"
    is_target_model: bool = False
    is_estimate: bool = True

    def count(self, text: str) -> int:
        total = 0
        current = ""
        for char in text:
            if char.isalnum():
                current += char
                continue
            if current:
                # Long alphanumeric runs split further in real tokenizers.
                total += max(1, (len(current) + 3) // 4)
                current = ""
            if not char.isspace():
                total += 1
        if current:
            total += max(1, (len(current) + 3) // 4)
        return total


@dataclass
class TiktokenTokenizer:
    """A real BPE tokenizer, still a proxy for any non-OpenAI model."""

    encoding: Any
    name: str
    is_target_model: bool = False
    is_estimate: bool = False

    def count(self, text: str) -> int:
        return len(self.encoding.encode(text))


def resolve_tokenizer(preferred: str = "cl100k_base") -> Tokenizer:
    """The best available tokenizer, never a silent downgrade.

    A caller that gets the heuristic back can see it in `name`, `is_estimate`
    and every rendered table. There is no path where an estimate is presented
    as a measurement.
    """
    try:
        import tiktoken
    except ImportError:
        return HeuristicTokenizer()
    try:
        encoding = tiktoken.get_encoding(preferred)
    except Exception:  # pragma: no cover - needs network on first use
        # The vocabulary download failed. An offline machine is the normal case
        # for this project, so this is a fallback rather than an error.
        return HeuristicTokenizer()
    return TiktokenTokenizer(encoding=encoding, name=f"tiktoken/{preferred}")


@dataclass
class Measurement:
    """What one candidate cost on one fixture."""

    candidate: str
    events: int
    bytes_total: int
    legend_bytes: int
    lines: int
    tokens_total: int
    legend_tokens: int
    tokenizer: str
    tokens_are_estimates: bool
    reversible: bool
    decode_error: str | None = None

    @property
    def bytes_per_event(self) -> float | None:
        return round(self.bytes_total / self.events, 2) if self.events else None

    @property
    def tokens_per_event(self) -> float | None:
        return round(self.tokens_total / self.events, 3) if self.events else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate": self.candidate,
            "events": self.events,
            "bytes": self.bytes_total,
            "legend_bytes": self.legend_bytes,
            "bytes_per_event": self.bytes_per_event,
            "lines": self.lines,
            # Named for what it is. `tokens` only when a real tokenizer ran.
            ("tokens" if not self.tokens_are_estimates else "symbols_ESTIMATE"): self.tokens_total,
            "legend_tokens": self.legend_tokens,
            "per_event": self.tokens_per_event,
            "tokenizer": self.tokenizer,
            "tokens_are_estimates": self.tokens_are_estimates,
            "reversible": self.reversible,
            "decode_error": self.decode_error,
        }


@dataclass
class ScaleResult:
    """One candidate measured at several timeline lengths.

    Legend overhead is fixed and per-event cost is not, so a format that is
    terrible at ten events can win at a thousand. The crossover is the number
    that decides which format a retrieved chunk should use versus a whole run.
    """

    candidate: str
    by_size: dict[int, Measurement] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate": self.candidate,
            "by_size": {str(n): m.as_dict() for n, m in sorted(self.by_size.items())},
        }


def measure(
    events: list[TimelineEvent],
    dictionaries: Dictionaries,
    candidate: str,
    tokenizer: Tokenizer,
) -> Measurement:
    """Encode, measure, and prove the decoder reproduces the timeline."""
    if candidate not in CANDIDATES:
        raise ValueError(f"Unknown candidate {candidate!r}; expected {sorted(CANDIDATES)}.")
    encode, decode = CANDIDATES[candidate]
    encoded: Encoded = encode(events, dictionaries)

    reversible = False
    decode_error: str | None = None
    try:
        reversible = decode(encoded, dictionaries) == events
    except Exception as exc:  # a decoder that raises is a finding, not a crash
        decode_error = str(exc)

    text = encoded.full_text
    return Measurement(
        candidate=candidate,
        events=len(events),
        bytes_total=len(text.encode("utf-8")),
        legend_bytes=len(encoded.legend_text.encode("utf-8")),
        lines=len(text.splitlines()),
        tokens_total=tokenizer.count(text),
        legend_tokens=tokenizer.count(encoded.legend_text) if encoded.legend_text else 0,
        tokenizer=tokenizer.name,
        tokens_are_estimates=tokenizer.is_estimate,
        reversible=reversible,
        decode_error=decode_error,
    )


def measure_at_scales(
    events: list[TimelineEvent],
    dictionaries: Dictionaries,
    candidate: str,
    tokenizer: Tokenizer,
    scales: tuple[int, ...] = (10, 100, 1000),
) -> ScaleResult:
    result = ScaleResult(candidate=candidate)
    for size in scales:
        if size > len(events):
            continue
        result.by_size[size] = measure(events[:size], dictionaries, candidate, tokenizer)
    if events:
        result.by_size[len(events)] = measure(events, dictionaries, candidate, tokenizer)
    return result


def render_benchmark(
    results: list[ScaleResult], *, tokenizer: Tokenizer, fixture_label: str
) -> str:
    """Track A's table. Refuses to call an estimate a token count."""
    baseline = next((r for r in results if r.candidate == "A"), None)
    lines = [
        "# SCL benchmark — Track A (exact / forensic)",
        "",
        f"Fixture: `{fixture_label}`",
        f"Tokenizer: `{tokenizer.name}`",
        "",
    ]
    if tokenizer.is_estimate:
        lines += [
            "> **These are not token counts.** No tokenizer was available, so the",
            "> figures below come from a declared heuristic and are labelled",
            "> `symbols`. They are comparable to each other and to nothing else.",
            "> Install `tiktoken` to obtain real counts — and note that even those",
            "> are a proxy, since tiktoken is an OpenAI tokenizer.",
            "",
        ]
    elif not tokenizer.is_target_model:
        lines += [
            "> Counts come from a real tokenizer, but **not the target model's**.",
            "> Treat ratios as sound and absolute numbers as approximate.",
            "",
        ]

    unit = "symbols" if tokenizer.is_estimate else "tokens"
    lines += [
        f"## Cost by timeline length ({unit})",
        "",
        "| Candidate | 10 events | 100 events | 1,000 events | per event (full) | Reversible |",
        "| --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for result in results:
        full = max(result.by_size) if result.by_size else 0
        row = result.by_size.get(full)
        lines.append(
            "| {} | {} | {} | {} | {} | {} |".format(
                result.candidate,
                _cell(result, 10),
                _cell(result, 100),
                _cell(result, 1000),
                "-" if row is None else row.tokens_per_event,
                "yes" if row and row.reversible else "**NO**",
            )
        )

    if baseline is not None and baseline.by_size:
        full = max(baseline.by_size)
        base = baseline.by_size[full].tokens_total
        lines += [
            "",
            "## Ratio against candidate A (verbose JSON)",
            "",
            f"| Candidate | {unit} | vs A |",
            "| --- | ---: | ---: |",
        ]
        for result in results:
            row = result.by_size.get(full)
            if row is None or not base:
                continue
            lines.append(
                f"| {result.candidate} | {row.tokens_total:,} | "
                f"{round(row.tokens_total / base, 3)} |"
            )

    broken = [r.candidate for r in results if any(not m.reversible for m in r.by_size.values())]
    if broken:
        lines += [
            "",
            "## Not reversible",
            "",
            "A candidate whose decoder does not reproduce the timeline is not a "
            "candidate, whatever it costs: " + ", ".join(broken),
        ]
    return "\n".join(lines) + "\n"


def _cell(result: ScaleResult, size: int) -> str:
    row = result.by_size.get(size)
    return "-" if row is None else f"{row.tokens_total:,}"


def render_benchmark_json(
    results: list[ScaleResult], *, tokenizer: Tokenizer, fixture_label: str
) -> str:
    return json.dumps(
        {
            "track": "A",
            "fixture": fixture_label,
            "tokenizer": {
                "name": tokenizer.name,
                "is_target_model": tokenizer.is_target_model,
                "is_estimate": tokenizer.is_estimate,
            },
            "results": [r.as_dict() for r in results],
        },
        indent=2,
    )
