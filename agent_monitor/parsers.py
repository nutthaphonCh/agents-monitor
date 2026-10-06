from __future__ import annotations

from agent_monitor.models import MODEL_TYPES


def bind(namespace: dict[str, object]) -> None:
    """Bind discovery, domain, and formatting services used by parsers."""
    globals().update(namespace)


def message_content(rec: dict[str, Any]) -> Any:
    msg = rec.get("message")
    return msg.get("content") if isinstance(msg, dict) else None


def text_blocks(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    return [
        block.get("text", "")
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    ]


def content_text(content: Any) -> str:
    return normalize_text(" ".join(text_blocks(content)))


def has_tool_result(content: Any) -> bool:
    return isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") == "tool_result"
        for block in content
    )


def is_synthetic(rec: dict[str, Any]) -> bool:
    if rec.get("isSynthetic") is True:
        return True
    subtype = str(rec.get("subtype") or rec.get("eventType") or "").lower()
    if subtype in {"synthetic", "task_notification", "task-notification", "interrupt", "interrupted"}:
        return True
    text = content_text(message_content(rec)).lower()
    return any(marker in text for marker in (
        "[request interrupted by user]",
        "<task-notification>",
        "<task_notification>",
    ))


def parse_usage_observation(text: str) -> UsageObservation | None:
    normalized = normalize_text(text)
    if not normalized.lower().startswith("usage observation"):
        return None
    match = re.search(
        r"Before\s+(\d{1,2}:\d{2}:\d{2})\s+5h\s+([\d.]+)%\s+weekly\s+([\d.]+)%\s+"
        r"After\s+(\d{1,2}:\d{2}:\d{2})\s+5h\s+([\d.]+)%\s+weekly\s+([\d.]+)%.*?"
        r"Confidence\s+([^·]+?)(?:\s*·\s*(.*))?$",
        normalized,
        re.I,
    )
    if not match:
        return None
    return UsageObservation(
        before_time=match.group(1), before_5h=float(match.group(2)), before_weekly=float(match.group(3)),
        after_time=match.group(4), after_5h=float(match.group(5)), after_weekly=float(match.group(6)),
        confidence=match.group(7).strip(), confidence_reason=(match.group(8) or "").strip(),
    )


def local_command_payload(text: str, tag: str) -> str | None:
    match = re.search(rf"<{re.escape(tag)}>(.*?)</{re.escape(tag)}>", text, re.I | re.S)
    return normalize_text(match.group(1)) if match else None


def is_real_user_prompt(rec: dict[str, Any]) -> bool:
    if rec.get("type") != "user" or rec.get("isSidechain") or is_synthetic(rec):
        return False
    content = message_content(rec)
    text = content_text(content)
    if local_command_payload(text, "local-command-stdout") is not None:
        return False
    if "<local-command-caveat>" in text.lower():
        return False
    if text.startswith("Base directory for this skill:") or parse_usage_observation(text):
        return False
    if rec.get("isCompactSummary"):
        return False
    if isinstance(content, str):
        return bool(content.strip())
    return bool(text_blocks(content)) and not has_tool_result(content)


def prompt_text(rec: dict[str, Any]) -> str:
    text = content_text(message_content(rec))
    command_match = re.search(r"<command-name>\s*([^<]+?)\s*</command-name>", text, re.I)
    if command_match:
        command = command_match.group(1).strip()
        command = command if command.startswith("/") else f"/{command}"
        args_match = re.search(r"<command-args>\s*(.*?)\s*</command-args>", text, re.I)
        args = args_match.group(1).strip() if args_match else ""
        return truncate(f"{command} {args}".strip(), 120)
    return truncate(text or "(empty prompt)", 120)


def extract_usage(rec: dict[str, Any]) -> tuple[str, int, int, int, int] | None:
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return None
    usage = msg.get("usage")
    if not isinstance(usage, dict):
        return None
    return (
        str(msg.get("model") or rec.get("model") or "unknown"),
        int(usage.get("input_tokens", 0) or 0),
        int(usage.get("output_tokens", 0) or 0),
        int(usage.get("cache_creation_input_tokens", 0) or 0),
        int(usage.get("cache_read_input_tokens", 0) or 0),
    )


def usage_identity(rec: dict[str, Any]) -> str:
    msg = rec.get("message")
    message_id = msg.get("id") if isinstance(msg, dict) else None
    return str(rec.get("requestId") or rec.get("request_id") or message_id or rec.get("uuid") or rec.get("id") or "")


def explicit_sub_key(rec: dict[str, Any]) -> str | None:
    for key in (
        "agentId", "agent_id", "sidechainId", "sidechain_id",
        "subagentId", "subagent_id", "taskId", "task_id",
    ):
        value = rec.get(key)
        if value:
            return f"{key}:{value}"
    metadata = rec.get("metadata")
    if isinstance(metadata, dict):
        for key in ("agentId", "agent_id", "sidechainId", "taskId"):
            value = metadata.get(key)
            if value:
                return f"{key}:{value}"
    return None


def sub_session_key(rec: dict[str, Any], prompt_index: int, sequence: int) -> str:
    explicit = explicit_sub_key(rec)
    if explicit:
        return explicit
    parent = rec.get("parentUuid") or rec.get("parent_uuid")
    if parent:
        return f"branch:{parent}"
    uuid = rec.get("uuid") or rec.get("id")
    if uuid:
        return f"branch:{uuid}"
    return f"sidechain:{prompt_index}:{sequence}"


def event_label(rec: dict[str, Any]) -> str | None:
    text = content_text(message_content(rec))
    lowered = text.lower()

    stdout = local_command_payload(text, "local-command-stdout")
    if stdout is not None:
        return truncate(f"Local command · {stdout}", 120)
    if "<local-command-caveat>" in lowered:
        return None
    if not is_synthetic(rec):
        return None

    if "[request interrupted by user]" in lowered:
        return "Interrupted by user"

    if "<task-notification>" in lowered or "<task_notification>" in lowered:
        task_match = re.search(r"<task[-_]id>\s*([^<]+?)\s*</task[-_]id>", text, re.I)
        status_match = re.search(r"<status>\s*([^<]+?)\s*</status>", text, re.I)
        summary_match = re.search(r"<summary>\s*([^<]+?)\s*</summary>", text, re.I)
        task_id = task_match.group(1).strip() if task_match else "unknown"
        status = status_match.group(1).strip() if status_match else "updated"
        label = f"Subtask {task_id}"
        if summary_match:
            summary = summary_match.group(1).strip()
            summary = re.sub(r'^Background command\s+["“](.*)["”]\s+', r"\1 · ", summary)
            label += f" · {summary}"
        else:
            label += f" · {status}"
        return truncate(label, 120)

    return truncate(text or str(rec.get("subtype") or "Synthetic event"), 120)


def analyze(path: str) -> Analysis:
    prompts: list[PromptTurn] = []
    current: PromptTurn | None = None
    preamble = Usage()
    preamble_usage_keys: set[str] = set()
    malformed = 0
    record_count = 0

    with open(path, "r", encoding="utf-8") as fh:
        for sequence, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            if not isinstance(rec, dict):
                continue

            record_count += 1

            if is_real_user_prompt(rec):
                current = PromptTurn(
                    index=len(prompts) + 1,
                    prompt=prompt_text(rec),
                    start_sequence=sequence,
                    timestamp=rec.get("timestamp"),
                    prompt_id=rec.get("promptId") or None,
                )
                prompts.append(current)
                continue

            label = event_label(rec)
            if label:
                if current:
                    current.events.append(label)
                continue

            usage_tuple = extract_usage(rec)
            if not usage_tuple:
                continue

            usage_key = usage_identity(rec)
            seen_keys = current.accounted_usage_keys if current else preamble_usage_keys
            if usage_key and usage_key in seen_keys:
                continue
            if usage_key:
                seen_keys.add(usage_key)

            model, inp, out, cache_create, cache_read = usage_tuple
            usage = Usage()
            usage.add(model, inp, out, cache_create, cache_read)

            if current is None:
                preamble.merge(usage)
                continue

            if rec.get("isSidechain"):
                key = sub_session_key(rec, current.index, sequence)
                sub = current.sub_sessions.get(key)
                if not sub:
                    sub = SubSession(
                        key=key,
                        label=f"{short_model(model)} sub-session",
                        first_sequence=sequence,
                    )
                    current.sub_sessions[key] = sub
                sub.usage.merge(usage)
            else:
                current.main.merge(usage)

    return Analysis(
        path=path,
        prompts=prompts,
        preamble=preamble,
        malformed=malformed,
        record_count=record_count,
    )


FILE_TOOL_ACTIONS = {
    "Read": "read", "Write": "write", "Edit": "edit",
    "Glob": "search", "Grep": "search",
}

# Claude Code injects these into the model context as their own record types, so they
# carry real context weight even though they never appear as user/assistant messages.
ATTACHMENT_EVIDENCE = {
    "skill_listing": "skills",
    "dynamic_skill": "skills",
    "edited_text_file": "repository",
}

AGENT_INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md")


def content_blocks(content: Any, block_type: str) -> list[dict[str, Any]]:
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict) and block.get("type") == block_type]


def actor_for_tool(prompt: PromptTurn, tool_use_id: str) -> Actor | None:
    return next((actor for actor in prompt.actors if actor.tool_use_id == tool_use_id), None)


def tool_activity_label(name: str, inputs: dict[str, Any]) -> str:
    if name in {"Read", "Write", "Edit"}:
        target = str(inputs.get("file_path") or inputs.get("path") or "file")
        return f"{name} · {target}"
    if name in {"Glob", "Grep"}:
        target = str(inputs.get("pattern") or inputs.get("query") or "repository")
        return f"{name} · {target}"
    if name == "Bash":
        purpose = str(inputs.get("description") or inputs.get("command") or "command")
        return f"Bash · {truncate(purpose, 72)}"
    if name == "Skill":
        return f"Skill · {inputs.get('skill') or 'unknown'}"
    if name in {"WebSearch", "WebFetch"}:
        target = str(inputs.get("query") or inputs.get("url") or "web")
        return f"{name} · {truncate(target, 72)}"
    return name


def tool_action_detail(name: str, inputs: dict[str, Any]) -> str:
    """Return the unabridged input that is most useful when inspecting an action."""
    if name == "Bash":
        return str(inputs.get("command") or "")
    if name in {"Read", "Write", "Edit"}:
        return str(inputs.get("file_path") or inputs.get("path") or "")
    if name in {"Glob", "Grep"}:
        return str(inputs.get("pattern") or inputs.get("query") or "")
    if name in {"WebSearch", "WebFetch"}:
        return str(inputs.get("query") or inputs.get("url") or "")
    return json.dumps(inputs, ensure_ascii=False, indent=2) if inputs else ""


def compact_action_output(value: str, max_lines: int = 24, max_chars: int = 12_000) -> str:
    """Bound stored tool output while retaining useful evidence from both ends."""
    value = re.sub(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))", "", value)
    if len(value) > max_chars:
        tail_chars = max_chars // 4
        value = (
            value[:max_chars - tail_chars]
            + "\n… output truncated …\n"
            + value[-tail_chars:]
        )
    lines = value.splitlines()
    if len(lines) <= max_lines:
        return value
    head_count = max_lines - 6
    omitted = len(lines) - max_lines
    return "\n".join(lines[:head_count] + [f"… {omitted} lines omitted …"] + lines[-6:])


def attach_action_output(prompt: PromptTurn, inputs: dict[str, Any], output: str) -> None:
    request_index = inputs.get("__request_index")
    action_index = inputs.get("__action_index")
    if not isinstance(request_index, int) or not isinstance(action_index, int):
        return
    if not 0 <= request_index < len(prompt.requests):
        return
    request = prompt.requests[request_index]
    while len(request.action_outputs) < len(request.actions):
        request.action_outputs.append("")
    if action_index < len(request.action_outputs):
        request.action_outputs[action_index] = compact_action_output(output)


def tool_result_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(
            str(item.get("text") or item.get("content") or "")
            for item in value if isinstance(item, dict)
        )
    return json.dumps(value, ensure_ascii=False) if value is not None else ""


def embedded_json(text: str) -> dict[str, Any] | None:
    cleaned = "\n".join(re.sub(r"^\s*\d+[→\t]", "", line) for line in text.splitlines())
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        value = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def attach_codex_telemetry(
    path: str,
    result_text: str,
    tools_by_id: dict[str, tuple[PromptTurn, str, dict[str, Any]]],
) -> None:
    telemetry = embedded_json(result_text)
    if not telemetry or telemetry.get("kind") != "codex_exec_telemetry":
        return
    prompts = list(dict.fromkeys(id(owner) for owner, _, _ in tools_by_id.values()))
    owners = []
    for owner, _, _ in tools_by_id.values():
        if id(owner) in prompts:
            owners.append(owner)
            prompts.remove(id(owner))
    candidates = [actor for owner in owners for actor in owner.actors]
    actor = next(
        (item for item in reversed(candidates) if item.telemetry_path and path.endswith(item.telemetry_path)),
        next((item for item in reversed(candidates) if not item.token_usage), None),
    )
    if not actor:
        return
    actor.thread_id = telemetry.get("thread_id")
    actor.duration_seconds = telemetry.get("duration_seconds")
    actor.token_usage = {
        key: int(value) for key, value in (telemetry.get("usage") or {}).items()
        if isinstance(value, (int, float))
    }
    actor.rate_limits = telemetry.get("rate_limits") if isinstance(telemetry.get("rate_limits"), dict) else None


def parse_iso_timestamp(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def load_actor_telemetry(actor: Actor) -> None:
    """Load a matching sidecar directly; never route telemetry through Claude text."""
    if not actor.working_dir:
        return
    candidates: list[Path] = []
    if actor.telemetry_path and "$" not in actor.telemetry_path:
        path = Path(actor.telemetry_path).expanduser()
        candidates.append(path if path.is_absolute() else Path(actor.working_dir) / path)
    candidates.extend(Path(actor.working_dir).glob("logs/codex-*.telemetry.json"))
    finished = parse_iso_timestamp(actor.finished_at)
    best: tuple[float, dict[str, Any]] | None = None
    for path in set(candidates):
        try:
            telemetry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(telemetry, dict) or telemetry.get("kind") != "codex_exec_telemetry":
            continue
        telemetry_finished = parse_iso_timestamp(telemetry.get("finished_at"))
        distance = abs((finished - telemetry_finished).total_seconds()) if finished and telemetry_finished else float("inf")
        if distance <= 300 and (best is None or distance < best[0]):
            best = (distance, telemetry)
    if not best:
        return
    telemetry = best[1]
    actor.thread_id = telemetry.get("thread_id")
    actor.duration_seconds = telemetry.get("duration_seconds")
    actor.token_usage = {
        key: int(value) for key, value in (telemetry.get("usage") or {}).items()
        if isinstance(value, (int, float))
    }
    actor.rate_limits = telemetry.get("rate_limits") if isinstance(telemetry.get("rate_limits"), dict) else None


def is_agy_command(command: str) -> bool:
    for segment in re.split(r"&&|\|\||[;|\n]", command):
        try:
            tokens = shlex.split(segment)
        except ValueError:
            continue
        executable = next((token for token in tokens if not re.match(r"^[A-Za-z_]\w*=", token)), "")
        executable_name = Path(executable).name
        if executable_name == "agy" and any(arg in {"-p", "--print", "--prompt"} for arg in tokens[1:]):
            return True
        if executable_name == "spawn-agy.sh" and any(
            arg == "--prompt-file" or arg.startswith("--prompt-file=") for arg in tokens[1:]
        ):
            return True
    return False


def spawned_agent_engine(command: str) -> str | None:
    """Return the managed agent engine, excluding ordinary yielded shell commands."""
    if is_agy_command(command):
        return "agy"
    script_engines = {
        "spawn-claude.sh": "claude",
        "claude-background-bridge.py": "claude",
        "claude-background-bridge.mjs": "claude",
        "spawn-codex.sh": "codex",
        "spawn-advise.sh": "codex",
    }
    for segment in re.split(r"&&|\|\||[;|\n]", command):
        try:
            tokens = shlex.split(segment)
        except ValueError:
            continue
        tokens = [token for token in tokens if not re.match(r"^[A-Za-z_]\w*=", token)]
        if not tokens:
            continue
        executable = Path(tokens[0]).name
        candidate = executable
        candidate_index = 0
        if executable in {"bash", "sh", "zsh"}:
            if "-n" in tokens[1:]:
                continue
            candidate_index = next((index for index, token in enumerate(tokens[1:], 1) if not token.startswith("-")), -1)
            candidate = Path(tokens[candidate_index]).name if candidate_index >= 0 else ""
        elif executable.startswith("python"):
            candidate_index = next((index for index, token in enumerate(tokens[1:], 1) if not token.startswith("-")), -1)
            candidate = Path(tokens[candidate_index]).name if candidate_index >= 0 else ""
        engine = script_engines.get(candidate)
        invocation_args = tokens[candidate_index + 1:] if candidate_index >= 0 else []
        if engine and not any(flag in invocation_args for flag in ("--help", "-h", "--dry-run")):
            return engine
    return None


def command_option(command: str, option: str) -> str | None:
    match = re.search(
        rf"{re.escape(option)}(?:=|\s+)(?:\"([^\"]+)\"|'([^']+)'|([^\s;&|]+))",
        command,
    )
    return next((value for value in match.groups() if value), None) if match else None


def agy_model(command: str) -> str:
    return command_option(command, "--model") or "default"


def load_agy_decision(actor: Actor) -> None:
    if actor.engine != "agy" or not actor.working_dir:
        return
    path = Path(actor.working_dir) / "logs" / "agy-decisions.jsonl"
    if not path.is_file():
        return
    started = parse_iso_timestamp(actor.started_at)
    finished = parse_iso_timestamp(actor.finished_at)
    best: tuple[float, dict[str, Any]] | None = None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict) or item.get("engine") != "agy":
            continue
        timestamp = parse_iso_timestamp(item.get("ts"))
        reference = finished or started
        distance = abs((reference - timestamp).total_seconds()) if reference and timestamp else float("inf")
        if distance <= 600 and (best is None or distance < best[0]):
            best = (distance, item)
    if not best:
        return
    item = best[1]
    actor.model = str(item.get("model") or actor.model or "default")
    actor.mode = str(item.get("mode") or "") or None
    actor.lane = str(item.get("lane") or "") or None
    actor.verdict = str(item.get("verdict") or "") or None
    actor.decision = str(item.get("decision") or "") or None
    actor.rationale = str(item.get("rationale") or "") or None


def enrich_record(
    prompt: PromptTurn,
    rec: dict[str, Any],
    tools_by_id: dict[str, tuple[PromptTurn, str, dict[str, Any]]],
) -> None:
    """Extract profiler evidence without changing token accounting."""
    timestamp = rec.get("timestamp")
    content = message_content(rec)
    text = content_text(content)
    rec_type = str(rec.get("type") or "")

    observation = parse_usage_observation(text) if rec_type == "user" else None
    if observation:
        prompt.usage_observations.append(observation)
        prompt.timeline.append(TimelineItem(timestamp, "Usage observation recorded", "usage"))
        return

    if rec.get("isCompactSummary") or str(rec.get("subtype") or "") == "compact_boundary":
        # Compaction resets the window, so accumulated evidence no longer describes it.
        prompt.evidence_chars.clear()
        prompt.evidence_chars["conversation"] += local_token_weight(text)
        prompt.events.append("Context compacted")
        prompt.timeline.append(TimelineItem(timestamp, "Context compacted", "context"))
        return

    if rec_type == "attachment":
        attachment = rec.get("attachment") if isinstance(rec.get("attachment"), dict) else {}
        category = ATTACHMENT_EVIDENCE.get(str(attachment.get("type") or ""), "system")
        prompt.evidence_chars[category] += local_token_weight(json.dumps(attachment, ensure_ascii=False))
        return

    if rec_type == "system":
        payload = rec.get("content")
        if not isinstance(payload, str):
            payload = json.dumps(payload, ensure_ascii=False) if payload else ""
        prompt.evidence_chars["system"] += local_token_weight(payload)
        return

    if rec_type == "user" and not is_synthetic(rec) and not has_tool_result(content):
        category = "skills" if text.startswith("Base directory for this skill:") else "conversation"
        prompt.evidence_chars[category] += local_token_weight(text)
    elif rec_type == "assistant":
        prompt.evidence_chars["conversation"] += local_token_weight(text)

    usage_tuple = extract_usage(rec)
    request_info: RequestInfo | None = None
    if usage_tuple:
        model, inp, out, cache_create, cache_read = usage_tuple
        request_key = usage_identity(rec)
        request_info = next((item for item in prompt.requests if item.request_id == request_key), None)
        if not request_info:
            request_usage = Usage()
            request_usage.add(model, inp, out, cache_create, cache_read)
            msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
            request_info = RequestInfo(
                timestamp, model, request_usage, request_id=request_key,
                stop_reason=msg.get("stop_reason"), last_timestamp=timestamp,
            )
            prompt.requests.append(request_info)
            if request_key:
                prompt.request_info_keys.add(request_key)
        else:
            request_info.last_timestamp = timestamp or request_info.last_timestamp

    for block in content_blocks(content, "tool_use"):
        tool_id = str(block.get("id") or "")
        name = str(block.get("name") or "Tool")
        inputs = block.get("input") if isinstance(block.get("input"), dict) else {}
        activity = tool_activity_label(name, inputs)
        prompt.timeline.append(TimelineItem(timestamp, f"{activity} — started", "tool"))
        tool_inputs = dict(inputs)
        if request_info:
            existing = tools_by_id.get(tool_id)
            existing_inputs = existing[2] if existing and existing[0] is prompt else {}
            if isinstance(existing_inputs.get("__action_index"), int):
                action_index = existing_inputs["__action_index"]
            else:
                action_index = len(request_info.actions)
                request_info.actions.append(activity)
                request_info.action_details.append(tool_action_detail(name, inputs))
                request_info.action_outputs.append("")
            tool_inputs["__request_index"] = prompt.requests.index(request_info)
            tool_inputs["__action_index"] = action_index
        tools_by_id[tool_id] = (prompt, name, tool_inputs)

        action = FILE_TOOL_ACTIONS.get(name)
        if action:
            path = inputs.get("file_path") or inputs.get("path") or inputs.get("pattern")
            if path:
                prompt.files.append(FileActivity(str(path), action, timestamp))

        if name == "Skill":
            prompt.evidence_chars["skills"] += local_token_weight(json.dumps(inputs, ensure_ascii=False))

        command = str(inputs.get("command") or "") if name == "Bash" else ""
        agy_run = name == "Bash" and is_agy_command(command)
        if name == "Bash" and (inputs.get("run_in_background") or agy_run):
            description = str(inputs.get("description") or ("Antigravity review" if agy_run else "Background command"))
            label = f"Gemini · {description}" if agy_run else description
            telemetry_match = re.search(r"\bTELEMETRY=(?:\"([^\"]+)\"|'([^']+)'|(\S+))", command)
            telemetry_path = next((value for value in telemetry_match.groups() if value), None) if telemetry_match else None
            prompt.actors.append(Actor(
                tool_id, label, "running", timestamp,
                tool_use_id=tool_id, telemetry_path=telemetry_path,
                working_dir=(command_option(command, "--project") if agy_run else None)
                or str(rec.get("cwd") or ""),
                engine="agy" if agy_run else None,
                model=agy_model(command) if agy_run else None,
            ))
            if agy_run:
                prompt.timeline.append(TimelineItem(timestamp, f"Actor · {label} — started", "actor"))

    for block in content_blocks(content, "tool_result"):
        tool_id = str(block.get("tool_use_id") or "")
        linked = tools_by_id.get(tool_id)
        result_text = tool_result_text(block.get("content"))
        if linked:
            owner, name, inputs = linked
            activity = tool_activity_label(name, inputs)
            background_running = name == "Bash" and "running in background with ID" in result_text
            state = "failed" if block.get("is_error") else ("running" if background_running else "completed")
            owner.timeline.append(TimelineItem(timestamp, f"{activity} — {state}", "tool"))
            attach_action_output(owner, inputs, result_text)
            target_path = str(inputs.get("file_path") or inputs.get("path") or "")
            if target_path.endswith(AGENT_INSTRUCTION_FILES):
                owner.evidence_chars["agents"] += local_token_weight(result_text)
            elif name in {"Read", "Glob", "Grep"}:
                owner.evidence_chars["repository"] += local_token_weight(result_text)
            elif name == "Skill":
                owner.evidence_chars["skills"] += local_token_weight(result_text)
            else:
                owner.evidence_chars["tool output"] += local_token_weight(result_text)
            if name == "Read":
                telemetry_path = str(inputs.get("file_path") or "")
                attach_codex_telemetry(telemetry_path, result_text, tools_by_id)
            actor = actor_for_tool(owner, tool_id)
            if actor:
                if result_text.strip():
                    actor.log_chunks.append(result_text)
                task_match = re.search(r"background with ID:\s*([\w-]+)", result_text, re.I)
                wrapper_exit = re.search(r"(?:^|\n)exit_code=(-?\d+)(?:\n|$)", result_text)
                for field_name, attr_name in (("output", "output_path"), ("events", "events_path"), ("telemetry", "telemetry_path")):
                    match = re.search(rf"(?:^|\n){field_name}=([^\n]+)", result_text)
                    if match:
                        setattr(actor, attr_name, match.group(1).strip())
                if wrapper_exit:
                    actor.exit_code = int(wrapper_exit.group(1))
                if task_match:
                    actor.task_id = task_match.group(1)
                elif block.get("is_error") or (actor.exit_code is not None and actor.exit_code != 0):
                    actor.status, actor.finished_at = "failed", timestamp
                elif not background_running:
                    actor.status, actor.finished_at = "completed", timestamp
                if actor.engine == "agy" and actor.finished_at:
                    load_agy_decision(actor)
                    owner.timeline.append(
                        TimelineItem(timestamp, f"Actor · {actor.label} — {actor.status}", "actor")
                    )

    if "<task-notification>" in text.lower() or "<task_notification>" in text.lower():
        tool_match = re.search(r"<tool-use-id>\s*([^<]+)", text, re.I)
        task_match = re.search(r"<task-id>\s*([^<]+)", text, re.I)
        status_match = re.search(r"<status>\s*([^<]+)", text, re.I)
        summary_match = re.search(r"<summary>\s*([^<]+)", text, re.I)
        exit_match = re.search(r"exit code\s+(-?\d+)", text, re.I)
        tool_id = tool_match.group(1).strip() if tool_match else ""
        linked = tools_by_id.get(tool_id)
        owner = linked[0] if linked else prompt
        actor = actor_for_tool(owner, tool_id)
        if not actor:
            label = summary_match.group(1).strip() if summary_match else "Background task"
            actor = Actor(tool_id or f"task:{len(owner.actors)}", label, tool_use_id=tool_id or None)
            owner.actors.append(actor)
        actor.status = status_match.group(1).strip() if status_match else "completed"
        actor.finished_at = timestamp
        actor.task_id = task_match.group(1).strip() if task_match else actor.task_id
        actor.exit_code = int(exit_match.group(1)) if exit_match else None
        actor.log_chunks.append(text)
        load_actor_telemetry(actor)
        owner.timeline.append(TimelineItem(timestamp, f"Actor · {actor.label} — {actor.status}", "actor"))
        owner.evidence_chars["tool output"] += local_token_weight(text)


def enrich_analysis(path: str, analysis: Analysis) -> dict[str, tuple[PromptTurn, str, dict[str, Any]]]:
    tools: dict[str, tuple[PromptTurn, str, dict[str, Any]]] = {}
    current: PromptTurn | None = None
    # Session preamble (system prompt, skill/tool listings) lands before the first prompt;
    # collect it here so prompt 1 inherits it instead of dropping it on the floor.
    base = PromptTurn(index=0, prompt="", start_sequence=0, timestamp=None)
    prompt_position = 0
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            if is_real_user_prompt(rec):
                if prompt_position < len(analysis.prompts):
                    previous = current
                    if current:
                        current.timeline.append(TimelineItem(rec.get("timestamp"), "Prompt completed", "prompt"))
                    current = analysis.prompts[prompt_position]
                    prompt_position += 1
                    current.evidence_chars.update(
                        previous.evidence_chars if previous else base.evidence_chars
                    )
                    current.timeline.append(TimelineItem(rec.get("timestamp"), "Prompt started", "prompt"))
                    prompt_value = content_text(message_content(rec))
                    category = "instructions" if prompt_value.startswith("Base directory for this skill:") else "conversation"
                    current.evidence_chars[category] += local_token_weight(prompt_value)
                continue
            enrich_record(current or base, rec, tools)
    return tools


SUBAGENT_DIR_NAME = "subagents"


def subagent_directory(path: str) -> Path:
    """Claude Code keeps the agents a session spawned under ``<session>/subagents/``.

    Their transcripts are separate JSONL files (``agent-<id>.jsonl``, possibly nested under
    ``workflows/``), so the parent transcript itself no longer carries ``isSidechain`` usage.
    """
    return Path(path).with_suffix("") / SUBAGENT_DIR_NAME


def subagent_label(transcript_path: str, model: str) -> str:
    """Prefer the sidecar ``agent-<id>.meta.json`` description over a model-only label."""
    try:
        meta = json.loads(Path(transcript_path).with_suffix(".meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = None
    if isinstance(meta, dict):
        parts = [str(meta.get(key) or "").strip() for key in ("description", "agentType")]
        label = " · ".join(part for part in parts if part)
        if label:
            return truncate(label, 80)
    return f"{short_model(model)} sub-session"


class IncrementalSessionAnalyzer:
    """Parse an existing session once, then consume only newly appended JSONL bytes."""
    def __init__(self, path: str) -> None:
        self.path = path
        self.analysis = analyze(path)
        self.tools = enrich_analysis(path, self.analysis)
        self.offset = os.path.getsize(path)
        self.sequence = self.analysis.record_count
        self.partial = ""
        # Per subagent transcript: consumed byte offset, trailing partial line, and the
        # index of the prompt it was attributed to once its first record was read.
        self.subagents: dict[str, dict[str, Any]] = {}
        self._poll_subagents()

    def poll(self) -> bool:
        changed = self._poll_main()
        return self._poll_subagents() or changed

    def _poll_main(self) -> bool:
        size = os.path.getsize(self.path)
        if size < self.offset:
            self.__init__(self.path)
            return True
        if size == self.offset:
            return False
        with open(self.path, "r", encoding="utf-8") as fh:
            fh.seek(self.offset)
            chunk = fh.read()
            self.offset = fh.tell()
        data = self.partial + chunk
        lines = data.splitlines(keepends=True)
        self.partial = ""
        if lines and not lines[-1].endswith(("\n", "\r")):
            self.partial = lines.pop()
        changed = False
        for line in lines:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                self.analysis.malformed += 1
                continue
            if not isinstance(rec, dict):
                continue
            self.sequence += 1
            self.analysis.record_count += 1
            self._append_record(rec)
            changed = True
        return changed

    def _poll_subagents(self) -> bool:
        directory = subagent_directory(self.path)
        if not directory.is_dir():
            return False
        changed = False
        for transcript in sorted(directory.rglob("*.jsonl")):
            key = str(transcript)
            state = self.subagents.get(key)
            if state is None:
                state = self.subagents[key] = {
                    "offset": 0, "partial": "", "prompt_index": None,
                    "agent_id": transcript.stem.removeprefix("agent-"),
                }
            try:
                size = os.path.getsize(key)
            except OSError:
                continue
            if size < state["offset"]:
                # Rewritten transcript: re-read it; request-id dedup keeps totals stable.
                state["offset"], state["partial"] = 0, ""
            if size == state["offset"]:
                continue
            with open(key, "r", encoding="utf-8") as fh:
                fh.seek(state["offset"])
                chunk = fh.read()
                state["offset"] = fh.tell()
            lines = (state["partial"] + chunk).splitlines(keepends=True)
            state["partial"] = ""
            if lines and not lines[-1].endswith(("\n", "\r")):
                state["partial"] = lines.pop()
            for line in lines:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    self.analysis.malformed += 1
                    continue
                if isinstance(rec, dict) and self._append_subagent_record(rec, key, state):
                    changed = True
        return changed

    def _subagent_prompt(self, rec: dict[str, Any], state: dict[str, Any]) -> PromptTurn | None:
        if state["prompt_index"] is not None:
            return next((p for p in self.analysis.prompts if p.index == state["prompt_index"]), None)
        prompts = self.analysis.prompts
        if not prompts:
            return None
        prompt_id = rec.get("promptId")
        match = next((p for p in prompts if prompt_id and p.prompt_id == prompt_id), None)
        if match is None:
            stamp = parse_iso_timestamp(rec.get("timestamp"))
            if stamp:
                started = [(parse_iso_timestamp(p.timestamp), p) for p in prompts]
                match = next((p for at, p in reversed(started) if at and at <= stamp), None)
        match = match or prompts[-1]
        state["prompt_index"] = match.index
        return match

    def _append_subagent_record(self, rec: dict[str, Any], transcript: str, state: dict[str, Any]) -> bool:
        prompt = self._subagent_prompt(rec, state)
        usage_tuple = extract_usage(rec)
        if not usage_tuple:
            return False
        model, inp, out, cache_create, cache_read = usage_tuple
        usage = Usage()
        usage.add(model, inp, out, cache_create, cache_read)
        if prompt is None:
            self.analysis.preamble.merge(usage)
            return True
        usage_key = usage_identity(rec)
        if usage_key and usage_key in prompt.accounted_usage_keys:
            return False
        if usage_key:
            prompt.accounted_usage_keys.add(usage_key)
        key = f"agentId:{state['agent_id']}"
        sub = prompt.sub_sessions.get(key)
        if sub is None:
            sub = prompt.sub_sessions[key] = SubSession(key, subagent_label(transcript, model), prompt.start_sequence)
        sub.usage.merge(usage)
        return True

    def _append_record(self, rec: dict[str, Any]) -> None:
        if is_real_user_prompt(rec):
            previous = self.analysis.prompts[-1] if self.analysis.prompts else None
            if self.analysis.prompts:
                self.analysis.prompts[-1].timeline.append(TimelineItem(rec.get("timestamp"), "Prompt completed", "prompt"))
                for actor in self.analysis.prompts[-1].actors:
                    if actor.key == "main" and actor.status == "running":
                        actor.status = "completed"
            prompt = PromptTurn(
                len(self.analysis.prompts) + 1, prompt_text(rec), self.sequence, rec.get("timestamp"),
                prompt_id=rec.get("promptId") or None,
            )
            if previous:
                prompt.evidence_chars.update(previous.evidence_chars)
            prompt.timeline.append(TimelineItem(rec.get("timestamp"), "Prompt started", "prompt"))
            prompt.evidence_chars["conversation"] += local_token_weight(content_text(message_content(rec)))
            self.analysis.prompts.append(prompt)
            return
        current = self.analysis.prompts[-1] if self.analysis.prompts else None
        if not current:
            return
        label = event_label(rec)
        if label:
            current.events.append(label)
        usage_tuple = extract_usage(rec)
        if usage_tuple:
            usage_key = usage_identity(rec)
            if not usage_key or usage_key not in current.accounted_usage_keys:
                if usage_key:
                    current.accounted_usage_keys.add(usage_key)
                model, inp, out, cache_create, cache_read = usage_tuple
                target = Usage()
                target.add(model, inp, out, cache_create, cache_read)
                if rec.get("isSidechain"):
                    key = sub_session_key(rec, current.index, self.sequence)
                    sub = current.sub_sessions.setdefault(key, SubSession(key, f"{short_model(model)} sub-session", self.sequence))
                    sub.usage.merge(target)
                else:
                    current.main.merge(target)
        enrich_record(current, rec, self.tools)


CODEX_TOOL_ACTIONS = {
    "exec_command": "Bash",
    "apply_patch": "Edit",
    "view_image": "Read",
    "web__run": "WebSearch",
    "image_gen__imagegen": "Write",
    "read_mcp_resource": "Read",
}


def codex_tool_activity(payload: dict[str, Any]) -> tuple[str, str]:
    raw = str(payload.get("input") or "")
    method_match = re.search(r"\btools\.([A-Za-z0-9_]+)\s*\(", raw)
    method = method_match.group(1) if method_match else str(payload.get("name") or "Tool")
    action = CODEX_TOOL_ACTIONS.get(method, method.replace("__", " ").replace("_", " ").title())
    description = ""
    if method == "exec_command":
        command = codex_exec_command(payload)
        description = command[:100].replace("\\n", " ") if command else "Run command"
    elif method == "apply_patch":
        description = "Apply patch"
    elif method == "web__run":
        description = "Search the web"
    elif method:
        description = method.replace("__", " ").replace("_", " ")
    return action, truncate(description, 90)


def codex_exec_command(payload: dict[str, Any]) -> str:
    raw_input = payload.get("input")
    if isinstance(raw_input, dict):
        return str(raw_input.get("cmd") or "")
    raw = str(raw_input or "")
    match = re.search(r'''(?:["']cmd["']|\bcmd)\s*:\s*(["'])(.*?)\1(?:\s*[,}])''', raw, re.S)
    return match.group(2).replace("\\n", "\n") if match else ""


def codex_tool_detail(payload: dict[str, Any]) -> str:
    """Keep complete tool input for the request detail page."""
    command = codex_exec_command(payload)
    if command:
        return command
    raw_input = payload.get("input")
    if isinstance(raw_input, dict):
        return json.dumps(raw_input, ensure_ascii=False, indent=2)
    return str(raw_input or "")


def codex_message_text(payload: dict[str, Any]) -> str:
    content = payload.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return " ".join(
        str(block.get("text") or block.get("input_text") or block.get("output_text") or "")
        for block in content if isinstance(block, dict)
    )


def codex_function_output_text(payload: dict[str, Any]) -> str:
    output = payload.get("output")
    if isinstance(output, str):
        return output
    if not isinstance(output, list):
        return ""
    return "\n".join(
        str(block.get("text") or "") for block in output if isinstance(block, dict)
    )


class CodexSessionAnalyzer:
    """Incrementally normalize Codex rollout JSONL into the shared profiler model."""
    def __init__(self, path: str) -> None:
        self.path = path
        self.analysis = Analysis(path, [], Usage(), 0, 0, provider="codex")
        self.tools: dict[str, tuple[PromptTurn, str, dict[str, Any]]] = {}
        self.offset = 0
        self.sequence = 0
        self.partial = ""
        self.model = "codex"
        self.thread_source = ""
        self.pending_actions: list[str] = []
        self.pending_action_details: list[str] = []
        self.pending_action_outputs: list[str] = []
        self.pending_cells: dict[str, Actor] = {}
        self.base_evidence: Counter[str] = Counter()
        # Cumulative usage of the last accepted token_count; Codex replays a snapshot
        # verbatim (e.g. when only rate limits refresh), and that replay is not a request.
        self.previous_total: dict[str, Any] | None = None
        self.poll()

    def poll(self) -> bool:
        size = os.path.getsize(self.path)
        if size < self.offset:
            self.__init__(self.path)
            return True
        if size == self.offset:
            return False
        with open(self.path, "r", encoding="utf-8") as fh:
            fh.seek(self.offset)
            chunk = fh.read()
            self.offset = fh.tell()
        data = self.partial + chunk
        lines = data.splitlines(keepends=True)
        self.partial = ""
        if lines and not lines[-1].endswith(("\n", "\r")):
            self.partial = lines.pop()
        changed = False
        for line in lines:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                self.analysis.malformed += 1
                continue
            if not isinstance(rec, dict):
                continue
            self.sequence += 1
            self.analysis.record_count += 1
            self._append_record(rec)
            changed = True
        return changed

    def _append_record(self, rec: dict[str, Any]) -> None:
        payload = rec.get("payload")
        if not isinstance(payload, dict):
            return
        kind = str(payload.get("type") or "")
        timestamp = rec.get("timestamp")
        if rec.get("type") == "session_meta":
            source = str(payload.get("thread_source") or "")
            if source == "subagent" or not self.thread_source:
                self.thread_source = source
            system_payload = str(payload.get("base_instructions") or "")
            dynamic_tools = payload.get("dynamic_tools")
            if dynamic_tools:
                system_payload += json.dumps(dynamic_tools, ensure_ascii=False)
            self.base_evidence["system"] += local_token_weight(system_payload)
            self.base_evidence["agents"] += local_token_weight(str(payload.get("user_instructions") or ""))
            return
        if rec.get("type") == "turn_context":
            self.model = str(payload.get("model") or self.model)
            return
        if kind == "item_completed":
            item = payload.get("item")
            item_type = str(item.get("type") or "").lower() if isinstance(item, dict) else ""
            if item_type == "subagentactivity":
                thread_id = str(item.get("agent_thread_id") or "")
                prompt = next(
                    (candidate for candidate in self.analysis.prompts
                     if thread_id in candidate.sub_sessions),
                    self.analysis.prompts[-1] if self.analysis.prompts else None,
                )
                if not prompt or not thread_id:
                    return
                activity = str(item.get("kind") or "observed").lower()
                agent_path = str(item.get("agent_path") or "")
                label = agent_path.rsplit("/", 1)[-1] or thread_id
                sub = prompt.sub_sessions.get(thread_id)
                if sub is None:
                    sub = SubSession(
                        thread_id, label, self.sequence,
                        status="running" if activity == "started" else activity,
                        thread_id=thread_id,
                    )
                    prompt.sub_sessions[thread_id] = sub
                elif activity == "completed":
                    sub.status = "completed"
                elif activity == "started":
                    sub.status = "running"
                if activity in {"started", "completed"}:
                    prompt.timeline.append(TimelineItem(
                        timestamp, f"Sub-agent · {sub.label} — {sub.status}", "subagent",
                    ))
                return
            if item_type != "usermessage":
                return
            payload = {"type": "user_message", "message": codex_message_text(item)}
            kind = "user_message"
        if (
            kind == "message" and str(payload.get("role") or "") == "user"
            and self.thread_source == "subagent"
        ):
            message = codex_message_text(payload)
            if message.lstrip().startswith("# AGENTS.md instructions"):
                # The actual NEW_TASK payload is encrypted; this plaintext user-role
                # message is repository guidance, not the delegated request itself.
                message = "Sub-agent task (encrypted)"
            payload = {"type": "user_message", "message": message}
            kind = "user_message"
        if kind == "message" and str(payload.get("role") or "") == "developer":
            text = codex_message_text(payload)
            category = "agents" if "AGENTS.md" in text or "<INSTRUCTIONS>" in text else "system"
            self.base_evidence[category] += local_token_weight(text)
            return
        if kind == "user_message":
            if self.analysis.prompts:
                previous = self.analysis.prompts[-1]
                if not any(item.label == "Prompt completed" for item in previous.timeline):
                    previous.timeline.append(TimelineItem(timestamp, "Prompt completed", "prompt"))
            text = truncate(str(payload.get("message") or "(empty prompt)"), 120)
            prompt = PromptTurn(len(self.analysis.prompts) + 1, text, self.sequence, timestamp)
            if self.analysis.prompts:
                prompt.evidence_chars.update(self.analysis.prompts[-1].evidence_chars)
            else:
                prompt.evidence_chars.update(self.base_evidence)
            prompt.timeline.append(TimelineItem(timestamp, "Prompt started", "prompt"))
            prompt.evidence_chars["conversation"] += local_token_weight(str(payload.get("message") or ""))
            self.analysis.prompts.append(prompt)
            self.pending_actions = []
            self.pending_action_details = []
            self.pending_action_outputs = []
            return
        prompt = self.analysis.prompts[-1] if self.analysis.prompts else None
        if not prompt:
            return
        if kind == "custom_tool_call":
            action, description = codex_tool_activity(payload)
            activity = f"{action} · {description}" if description else action
            call_id = str(payload.get("call_id") or payload.get("id") or self.sequence)
            prompt.timeline.append(TimelineItem(timestamp, f"{activity} — started", "tool"))
            self.pending_actions.append(activity)
            self.pending_action_details.append(codex_tool_detail(payload))
            self.pending_action_outputs.append("")
            command = codex_exec_command(payload) if action == "Bash" else ""
            self.tools[call_id] = (
                prompt, activity,
                {"command": command, "__action_index": len(self.pending_actions) - 1},
            )
            engine = spawned_agent_engine(command)
            if engine:
                display_name = {"agy": "Gemini", "claude": "Claude", "codex": "Codex"}[engine]
                label = f"{display_name} · {description or 'Background agent'}"
                prompt.actors.append(Actor(
                    call_id, label, "running", timestamp,
                    tool_use_id=call_id,
                    working_dir=command_option(command, "--project") or "",
                    engine=engine, model=agy_model(command) if engine == "agy" else None,
                ))
                prompt.timeline.append(TimelineItem(timestamp, f"Actor · {label} — started", "actor"))
            path_match = re.search(r"(?:/Users/|/private/|/tmp/)[^\s\"']+", str(payload.get("input") or ""))
            if path_match and action.lower() in {"write", "edit", "read"}:
                prompt.files.append(FileActivity(path_match.group(0), action.lower(), timestamp))
            return
        if kind == "custom_tool_call_output":
            call_id = str(payload.get("call_id") or "")
            linked = self.tools.get(call_id)
            if linked:
                owner, activity, inputs = linked
                owner.timeline.append(TimelineItem(timestamp, f"{activity} — completed", "tool"))
                result_text = codex_function_output_text(payload)
                action_index = inputs.get("__action_index")
                if isinstance(action_index, int) and action_index < len(self.pending_action_outputs):
                    self.pending_action_outputs[action_index] = compact_action_output(result_text)
                attach_action_output(owner, inputs, result_text)
                category = "repository" if activity.startswith(("Read ·", "Glob ·", "Grep ·")) else "tool output"
                owner.evidence_chars[category] += local_token_weight(result_text)
                actor = actor_for_tool(owner, call_id)
                cell = re.search(r"Script running with cell ID\s+([\w-]+)", result_text)
                if actor and result_text.strip():
                    actor.log_chunks.append(result_text)
                if actor:
                    if cell:
                        actor.task_id = cell.group(1)
                        self.pending_cells[cell.group(1)] = actor
                    else:
                        self._complete_background_actor(actor, result_text, timestamp, owner)
            return
        if kind == "function_call" and payload.get("name") == "wait":
            call_id = str(payload.get("call_id") or payload.get("id") or self.sequence)
            try:
                arguments = json.loads(str(payload.get("arguments") or "{}"))
            except json.JSONDecodeError:
                arguments = {}
            cell_id = str(arguments.get("cell_id") or "")
            self.tools[call_id] = (prompt, "Wait · process", {"cell_id": cell_id})
            return
        if kind == "function_call_output":
            call_id = str(payload.get("call_id") or "")
            linked = self.tools.get(call_id)
            if linked and linked[1] == "Wait · process":
                owner, _, inputs = linked
                cell_id = str(inputs.get("cell_id") or "")
                actor = self.pending_cells.get(cell_id)
                if actor:
                    output = codex_function_output_text(payload)
                    actor.log_chunks.append(output)
                    if "Script running with cell ID" not in output:
                        self.pending_cells.pop(cell_id, None)
                        self._complete_background_actor(actor, output, timestamp, owner)
            return
        if kind == "agent_message":
            prompt.evidence_chars["conversation"] += local_token_weight(str(payload.get("message") or ""))
            return
        if kind == "token_count":
            info = payload.get("info")
            last = info.get("last_token_usage") if isinstance(info, dict) else None
            if not isinstance(last, dict):
                return
            total = info.get("total_token_usage")
            if isinstance(total, dict):
                if total == self.previous_total:
                    return
                self.previous_total = total
            total_input = int(last.get("input_tokens", 0) or 0)
            cached = int(last.get("cached_input_tokens", 0) or 0)
            output = int(last.get("output_tokens", 0) or 0)
            usage = Usage()
            usage.add(self.model, max(0, total_input - cached), output, 0, cached)
            prompt.main.merge(usage)
            request_index = len(prompt.requests)
            prompt.requests.append(RequestInfo(
                timestamp, self.model, usage, request_id=f"codex:{self.sequence}",
                stop_reason="turn", actions=self.pending_actions,
                action_details=self.pending_action_details,
                action_outputs=self.pending_action_outputs,
            ))
            for owner, _, inputs in self.tools.values():
                if owner is prompt and "__action_index" in inputs and "__request_index" not in inputs:
                    inputs["__request_index"] = request_index
            self.pending_actions = []
            self.pending_action_details = []
            self.pending_action_outputs = []
            return
        if kind == "task_complete":
            prompt.timeline.append(TimelineItem(timestamp, "Prompt completed", "prompt"))
            return
        if kind == "turn_aborted":
            prompt.events.append("Interrupted by user")
            prompt.timeline.append(TimelineItem(timestamp, "Prompt interrupted", "prompt"))
            return
        if kind == "context_compacted":
            prompt.events.append("Context compacted")
            prompt.timeline.append(TimelineItem(timestamp, "Context compacted", "context"))
            return

    def _complete_agy_actor(
        self, actor: Actor, result_text: str, timestamp: str | None, owner: PromptTurn,
    ) -> None:
        wrapper_exit = re.search(r"(?:exit_code=|\"exit_code\"\s*:\s*)(-?\d+)", result_text)
        actor.exit_code = int(wrapper_exit.group(1)) if wrapper_exit else None
        actor.status = "failed" if actor.exit_code not in {None, 0} else "completed"
        actor.finished_at = timestamp
        load_agy_decision(actor)
        owner.timeline.append(TimelineItem(timestamp, f"Actor · {actor.label} — {actor.status}", "actor"))

    def _complete_background_actor(
        self, actor: Actor, result_text: str, timestamp: str | None, owner: PromptTurn,
    ) -> None:
        wrapper_exit = re.search(r"(?:exit_code=|\"exit_code\"\s*:\s*)(-?\d+)", result_text)
        actor.exit_code = int(wrapper_exit.group(1)) if wrapper_exit else None
        actor.status = "failed" if actor.exit_code not in {None, 0} else "completed"
        actor.finished_at = timestamp
        for field_name, attr_name in (("output", "output_path"), ("events", "events_path"), ("telemetry", "telemetry_path")):
            match = re.search(rf"(?:^|\n){field_name}=([^\n]+)", result_text)
            if match:
                setattr(actor, attr_name, match.group(1).strip())
        if actor.engine == "agy":
            load_agy_decision(actor)
        owner.timeline.append(TimelineItem(timestamp, f"Actor · {actor.label} — {actor.status}", "actor"))


def create_analyzer(path: str) -> IncrementalSessionAnalyzer | CodexSessionAnalyzer:
    return CodexSessionAnalyzer(path) if session_provider(path) == "codex" else IncrementalSessionAnalyzer(path)


CACHE_TYPES = {cls.__name__: cls for cls in MODEL_TYPES}


def cache_encode(value: Any) -> Any:
    if is_dataclass(value):
        return {
            "__type__": type(value).__name__,
            **{item.name: cache_encode(getattr(value, item.name)) for item in fields(value)},
        }
    if isinstance(value, Counter):
        return {"__counter__": dict(value)}
    if isinstance(value, set):
        return {"__set__": sorted(value)}
    if isinstance(value, dict):
        return {str(key): cache_encode(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [cache_encode(item) for item in value]
    return value


def cache_decode(value: Any) -> Any:
    if isinstance(value, list):
        return [cache_decode(item) for item in value]
    if not isinstance(value, dict):
        return value
    if "__counter__" in value:
        return Counter(value["__counter__"])
    if "__set__" in value:
        return set(value["__set__"])
    type_name = value.get("__type__")
    if type_name in CACHE_TYPES:
        cls = CACHE_TYPES[type_name]
        return cls(**{
            key: cache_decode(item) for key, item in value.items() if key != "__type__"
        })
    return {key: cache_decode(item) for key, item in value.items()}


class ProfilerCache:
    """Persistent materialized parser state, resumed from the source byte offset."""
    def __init__(self, path: str | None = None) -> None:
        default = Path.home() / "Library" / "Caches" / "execution-profiler" / "profiler.sqlite3"
        self.path = Path(path or os.environ.get("EXECUTION_PROFILER_CACHE") or default)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS materialized_sessions (
                source_path TEXT PRIMARY KEY,
                source_inode INTEGER NOT NULL,
                byte_offset INTEGER NOT NULL,
                source_size INTEGER NOT NULL,
                source_mtime_ns INTEGER NOT NULL,
                schema_version INTEGER NOT NULL,
                state_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        self.db.commit()

    def load(self, path: str) -> IncrementalSessionAnalyzer | CodexSessionAnalyzer | None:
        stat = os.stat(path)
        row = self.db.execute(
            "SELECT source_inode, byte_offset, schema_version, state_json "
            "FROM materialized_sessions WHERE source_path = ?", (str(Path(path).resolve()),),
        ).fetchone()
        if not row or row[0] != stat.st_ino or row[1] > stat.st_size or row[2] != CACHE_SCHEMA_VERSION:
            return None
        try:
            state = json.loads(row[3])
            analysis = cache_decode(state["analysis"])
            if not isinstance(analysis, Analysis):
                return None
            analyzer_class = CodexSessionAnalyzer if analysis.provider == "codex" else IncrementalSessionAnalyzer
            analyzer = analyzer_class.__new__(analyzer_class)
            analyzer.path = path
            analyzer.analysis = analysis
            analyzer.offset = int(state["offset"])
            analyzer.sequence = int(state["sequence"])
            analyzer.partial = str(state.get("partial") or "")
            if isinstance(analyzer, CodexSessionAnalyzer):
                analyzer.model = str(state.get("model") or (
                    analysis.prompts[-1].main.primary_model if analysis.prompts else "codex"
                ))
                analyzer.thread_source = str(state.get("thread_source") or "")
                analyzer.pending_actions = list(state.get("pending_actions") or [])
                analyzer.pending_action_details = list(state.get("pending_action_details") or [])
                analyzer.pending_action_outputs = list(state.get("pending_action_outputs") or [])
                analyzer.pending_cells = {
                    actor.task_id: actor
                    for prompt in analysis.prompts
                    for actor in prompt.actors
                    if actor.status == "running" and actor.task_id
                }
                analyzer.base_evidence = Counter(state.get("base_evidence") or {})
                previous_total = state.get("previous_total")
                analyzer.previous_total = previous_total if isinstance(previous_total, dict) else None
            else:
                analyzer.subagents = {
                    str(key): dict(item) for key, item in (state.get("subagents") or {}).items()
                    if isinstance(item, dict)
                }
            analyzer.tools = {}
            for tool_id, tool in state.get("tools", {}).items():
                prompt = next((item for item in analysis.prompts if item.index == tool[0]), None)
                if prompt:
                    analyzer.tools[tool_id] = (prompt, tool[1], tool[2])
            return analyzer
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def save(self, analyzer: IncrementalSessionAnalyzer | CodexSessionAnalyzer) -> bool:
        stat = os.stat(analyzer.path)
        tools = {
            tool_id: [prompt.index, name, inputs]
            for tool_id, (prompt, name, inputs) in analyzer.tools.items()
        }
        state = json.dumps({
            "analysis": cache_encode(analyzer.analysis),
            "offset": analyzer.offset,
            "sequence": analyzer.sequence,
            "partial": analyzer.partial,
            "tools": tools,
            "model": getattr(analyzer, "model", None),
            "thread_source": getattr(analyzer, "thread_source", None),
            "pending_actions": getattr(analyzer, "pending_actions", []),
            "pending_action_details": getattr(analyzer, "pending_action_details", []),
            "pending_action_outputs": getattr(analyzer, "pending_action_outputs", []),
            "base_evidence": dict(getattr(analyzer, "base_evidence", {})),
            "previous_total": getattr(analyzer, "previous_total", None),
            "subagents": getattr(analyzer, "subagents", {}),
        }, ensure_ascii=False, separators=(",", ":"))
        try:
            self.db.execute("""
                INSERT INTO materialized_sessions (
                    source_path, source_inode, byte_offset, source_size, source_mtime_ns,
                    schema_version, state_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_path) DO UPDATE SET
                    source_inode=excluded.source_inode, byte_offset=excluded.byte_offset,
                    source_size=excluded.source_size, source_mtime_ns=excluded.source_mtime_ns,
                    schema_version=excluded.schema_version, state_json=excluded.state_json,
                    updated_at=excluded.updated_at
            """, (
                str(Path(analyzer.path).resolve()), stat.st_ino, analyzer.offset, stat.st_size,
                stat.st_mtime_ns, CACHE_SCHEMA_VERSION, state, dt.datetime.now(dt.timezone.utc).isoformat(),
            ))
            self.db.commit()
            return True
        except sqlite3.Error:
            self.db.rollback()
            return False

    def analyzer(self, path: str) -> IncrementalSessionAnalyzer | CodexSessionAnalyzer:
        analyzer = self.load(path)
        if analyzer is None:
            analyzer = create_analyzer(path)
        else:
            analyzer.poll()
        self.save(analyzer)
        return analyzer
