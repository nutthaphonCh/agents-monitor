#!/usr/bin/env python3
"""Summarize `codex exec --json` events without mixing telemetry into the answer."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any


def token_usage(event: dict[str, Any]) -> dict[str, int] | None:
    if event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
        return {key: int(value or 0) for key, value in event["usage"].items() if isinstance(value, (int, float))}
    payload = event.get("payload")
    if isinstance(payload, dict) and payload.get("type") == "token_count":
        info = payload.get("info")
        if isinstance(info, dict) and isinstance(info.get("total_token_usage"), dict):
            return {key: int(value or 0) for key, value in info["total_token_usage"].items() if isinstance(value, (int, float))}
    return None


def summarize(path: str, exit_code: int) -> dict[str, Any]:
    first_time = None
    last_time = None
    thread_id = None
    usage = None
    rate_limits = None
    errors: list[str] = []
    event_count = 0

    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            event_count += 1
            timestamp = event.get("timestamp")
            if timestamp:
                first_time = first_time or timestamp
                last_time = timestamp
            if event.get("type") == "thread.started":
                thread_id = event.get("thread_id") or event.get("thread", {}).get("id")
            if event.get("type") == "session_meta":
                payload = event.get("payload") or {}
                thread_id = thread_id or payload.get("session_id") or payload.get("id")
            candidate = token_usage(event)
            if candidate:
                usage = candidate
            payload = event.get("payload")
            if isinstance(payload, dict) and payload.get("type") == "token_count":
                rate_limits = payload.get("rate_limits") or rate_limits
            if event.get("type") in {"error", "turn.failed"}:
                errors.append(str(event.get("message") or event.get("error") or event))

    duration_seconds = None
    if first_time and last_time:
        try:
            start = dt.datetime.fromisoformat(str(first_time).replace("Z", "+00:00"))
            end = dt.datetime.fromisoformat(str(last_time).replace("Z", "+00:00"))
            duration_seconds = round((end - start).total_seconds(), 3)
        except ValueError:
            pass
    return {
        "kind": "codex_exec_telemetry",
        "thread_id": thread_id,
        "started_at": first_time,
        "finished_at": last_time,
        "exit_code": exit_code,
        "duration_seconds": duration_seconds,
        "event_count": event_count,
        "usage": usage or {},
        "rate_limits": rate_limits,
        "errors": errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("events", help="JSONL emitted by codex exec --json")
    parser.add_argument("--exit-code", type=int, required=True)
    parser.add_argument("--output")
    args = parser.parse_args()
    result = summarize(args.events, args.exit_code)
    text = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
