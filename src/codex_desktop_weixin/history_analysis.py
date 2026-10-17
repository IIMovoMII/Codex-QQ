from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from errors import normalize_error


def analyze_history(session_root: str | Path, *, include_examples: bool = False) -> dict[str, Any]:
    root = Path(session_root)
    files = 0
    lines = 0
    parse_errors = 0
    failed_completions = 0
    standalone_errors = 0
    categories: Counter[str] = Counter()
    structured_kinds: Counter[str] = Counter()
    shapes: Counter[str] = Counter()
    fingerprints: Counter[str] = Counter()
    examples: dict[str, dict[str, str]] = {}

    for path in root.rglob("*.jsonl"):
        files += 1
        try:
            stream = path.open("r", encoding="utf-8")
        except OSError:
            continue
        with stream:
            for line in stream:
                lines += 1
                if "error" not in line and "codex_error_info" not in line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    parse_errors += 1
                    continue
                if not isinstance(record, dict) or record.get("type") != "event_msg":
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue
                error: Any = None
                if payload.get("type") == "task_complete" and payload.get("error") is not None:
                    failed_completions += 1
                    error = payload.get("error")
                elif payload.get("type") == "error":
                    standalone_errors += 1
                    error = payload
                if error is None:
                    continue
                summary = normalize_error(error)
                categories[summary.category.value] += 1
                structured_kinds[str(summary.structured_kind or "<none>")] += 1
                fingerprints[summary.fingerprint] += 1
                if include_examples:
                    bucket = examples.setdefault(summary.category.value, {})
                    if len(bucket) < 5 and summary.fingerprint not in bucket:
                        bucket[summary.fingerprint] = summary.public_message
                if isinstance(error, dict):
                    shape = ",".join(sorted(str(key) for key in error.keys()))
                else:
                    shape = type(error).__name__
                shapes[shape] += 1

    result = {
        "files": files,
        "lines": lines,
        "parse_errors": parse_errors,
        "failed_completions": failed_completions,
        "standalone_errors": standalone_errors,
        "categories": dict(categories.most_common()),
        "structured_kinds": dict(structured_kinds.most_common()),
        "error_shapes": dict(shapes.most_common()),
        "unique_error_fingerprints": len(fingerprints),
    }
    if include_examples:
        result["redacted_examples"] = examples
    return result
