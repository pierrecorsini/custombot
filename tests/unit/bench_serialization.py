"""
bench_serialization.py — Benchmark: orjson vs msgpack for tool-call result payloads.

Evaluates whether msgpack's binary format offers serialization or memory
advantages over orjson for the hot-path tool-call result payloads in the
ReAct loop.

Tool results flow through the pipeline as:
    str → dict("role":"tool", "content": str) → JSONL (JSON format)

Since the final format MUST be JSON (for LLM API calls and JSONL persistence),
msgpack would require double serialization (msgpack → deserialize → JSON),
which adds overhead.  This benchmark validates that hypothesis.

Run:
    python -m pytest tests/unit/bench_serialization.py -v -s
"""

from __future__ import annotations

import statistics
import time
from typing import Any

import orjson
import pytest


# ── Payload generators ──────────────────────────────────────────────────


def _make_tool_result(content: str) -> dict[str, Any]:
    """Mimic a ChatCompletionToolMessageParam persisted via buffered_persist."""
    return {"role": "tool", "content": content, "name": "read_file"}


def _small_payload() -> dict[str, Any]:
    """Typical short tool result (e.g., 'file not found')."""
    return _make_tool_result("File not found: /workspace/data.txt")


def _medium_payload() -> dict[str, Any]:
    """Typical tool result (e.g., a short file read, ~2KB)."""
    text = "Line {}\n".join(f"Sample content row {i} with typical data."
                            for i in range(30))
    return _make_tool_result(text)


def _large_payload() -> dict[str, Any]:
    """Large tool result (e.g., a full file read, ~50KB)."""
    lines = [f"row_{i}: " + "x" * 80 for i in range(500)]
    return _make_tool_result("\n".join(lines))


def _payload_suite() -> list[tuple[str, dict[str, Any]]]:
    return [
        ("small", _small_payload()),
        ("medium", _medium_payload()),
        ("large", _large_payload()),
    ]


# ── Benchmark helper ────────────────────────────────────────────────────


def _bench(fn, iterations: int = 500) -> list[float]:
    """Run *fn* *iterations* times, return latencies in microseconds."""
    times: list[float] = []
    for _ in range(iterations):
        start = time.perf_counter_ns()
        fn()
        elapsed = (time.perf_counter_ns() - start) / 1000  # µs
        times.append(elapsed)
    return times


def _summary(times: list[float]) -> dict[str, float]:
    return {
        "median": statistics.median(times),
        "p95": sorted(times)[int(len(times) * 0.95)],
    }


# ── Tests ───────────────────────────────────────────────────────────────


class TestSerializationBenchmark:
    """Benchmark orjson vs msgpack for tool-call result payloads."""

    @pytest.fixture(autouse=True)
    def _check_msgpack(self):
        pytest.importorskip("msgpack")

    def test_direct_serialization(self) -> None:
        """orjson→bytes vs msgpack→bytes for tool result dicts."""
        try:
            import msgpack
        except ImportError:
            pytest.skip("msgpack not installed")

        print("\n" + "=" * 70)
        print("BENCHMARK: Direct serialization (dict → bytes)")
        print("=" * 70)

        for name, payload in _payload_suite():
            orjson_times = _bench(lambda p=payload: orjson.dumps(p))
            msgpack_times = _bench(lambda p=payload: msgpack.packb(p, use_bin_type=True))

            o = _summary(orjson_times)
            m = _summary(msgpack_times)

            orjson_size = len(orjson.dumps(payload))
            msgpack_size = len(msgpack.packb(payload, use_bin_type=True))

            print(f"\n  {name.upper()} payload:")
            print(f"    orjson  — median: {o['median']:.1f}µs, p95: {o['p95']:.1f}µs, size: {orjson_size}B")
            print(f"    msgpack — median: {m['median']:.1f}µs, p95: {m['p95']:.1f}µs, size: {msgpack_size}B")
            print(f"    size ratio (msgpack/orjson): {msgpack_size / orjson_size:.2f}x")

            # Verify both produce round-trippable data
            assert orjson.loads(orjson.dumps(payload)) == payload
            assert msgpack.unpackb(msgpack.packb(payload, use_bin_type=True), raw=False) == payload

    def test_full_pipeline(self) -> None:
        """Full pipeline: serialize → store → load → re-serialize to JSON.

        This models the actual data flow where tool results must end up as
        JSON for the LLM API and JSONL persistence.
        """
        try:
            import msgpack
        except ImportError:
            pytest.skip("msgpack not installed")

        print("\n" + "=" * 70)
        print("BENCHMARK: Full pipeline (serialize → store → load → JSON)")
        print("=" * 70)

        for name, payload in _payload_suite():
            # orjson path: dict → JSON bytes → dict (single step)
            def orjson_pipeline(p=payload):
                data = orjson.dumps(p)
                return orjson.loads(data)

            # msgpack path: dict → msgpack → dict → orjson → JSON bytes (double)
            def msgpack_pipeline(p=payload):
                binary = msgpack.packb(p, use_bin_type=True)
                restored = msgpack.unpackb(binary, raw=False)
                return orjson.dumps(restored)

            o_times = _bench(orjson_pipeline)
            m_times = _bench(msgpack_pipeline)

            o = _summary(o_times)
            m = _summary(m_times)

            print(f"\n  {name.upper()} pipeline:")
            print(f"    orjson path  — median: {o['median']:.1f}µs, p95: {o['p95']:.1f}µs")
            print(f"    msgpack path — median: {m['median']:.1f}µs, p95: {m['p95']:.1f}µs")
            ratio = m['median'] / o['median'] if o['median'] > 0 else float('inf')
            print(f"    msgpack/orjson ratio: {ratio:.2f}x (lower is better)")

            # Verify both paths produce equivalent final output
            orjson_result = orjson_pipeline()
            msgpack_result = msgpack_pipeline()
            assert orjson_result == orjson.loads(msgpack_result)

    def test_text_heavy_payload(self) -> None:
        """Validate that orjson wins specifically on text-heavy payloads."""
        try:
            import msgpack
        except ImportError:
            pytest.skip("msgpack not installed")

        print("\n" + "=" * 70)
        print("BENCHMARK: Text-heavy payload (file read simulation)")
        print("=" * 70)

        # Simulate a tool result that reads a source file (~100KB)
        code_lines = [
            f"def function_{i}(x: int, y: str) -> dict[str, Any]:\n"
            f'    """Docstring for function {i}."""\n'
            f'    return {{"result": x + len(y), "name": "func_{i}"}}\n'
            for i in range(300)
        ]
        payload = _make_tool_result("\n".join(code_lines))

        orjson_times = _bench(lambda: orjson.dumps(payload))
        msgpack_times = _bench(lambda: msgpack.packb(payload, use_bin_type=True))

        o = _summary(orjson_times)
        m = _summary(msgpack_times)

        orjson_size = len(orjson.dumps(payload))
        msgpack_size = len(msgpack.packb(payload, use_bin_type=True))

        print(f"\n  Text-heavy (~{len(orjson.dumps(payload)) // 1024}KB):")
        print(f"    orjson  — median: {o['median']:.1f}µs, size: {orjson_size}B")
        print(f"    msgpack — median: {m['median']:.1f}µs, size: {msgpack_size}B")
        print(f"    size ratio: {msgpack_size / orjson_size:.2f}x")
        print(f"    speed ratio: {m['median'] / o['median']:.2f}x (msgpack/orjson)")

        # orjson should be faster or comparable for text-heavy data
        # msgpack adds overhead for text-heavy dicts (string encoding is not its strength)
        print(f"\n  Conclusion: orjson {'is' if o['median'] <= m['median'] else 'is NOT'} "
              f"faster for text-heavy tool results.")
