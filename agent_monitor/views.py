from __future__ import annotations


def bind(namespace: dict[str, object]) -> None:
    """Bind domain and report services used by presentation helpers."""
    globals().update(namespace)


def timestamp_hm(value: str | None) -> str:
    if value and "T" in value:
        return value.split("T", 1)[1][:5]
    return "     "


def live_feed(analysis: Analysis) -> list[tuple[int, str]]:
    """Human-readable execution events mapped back to their prompt."""
    items: list[tuple[int, str]] = []
    session_is_live = time.time() - os.path.getmtime(analysis.path) < 2
    index_width = max((len(str(prompt.index)) for prompt in analysis.prompts), default=1)
    for prompt in analysis.prompts:
        clock = timestamp_hm(prompt.timestamp)
        quoted_prompt = json.dumps(prompt.prompt, ensure_ascii=False)
        items.append((prompt.index, f"{prompt.index:>{index_width}} | {clock} · {quoted_prompt}"))
        for event in prompt.events:
            items.append((prompt.index, f"       {event}"))
        usage = prompt.total_usage
        if prompt is analysis.prompts[-1]:
            state = "running" if session_is_live else "last observed"
        else:
            state = "completed"
        subagents = f" · {len(prompt.sub_sessions)} sub-agents" if prompt.sub_sessions else ""
        items.append((prompt.index, (
            f"       {state} · {fmt_tokens(latest_context_size(prompt))} context "
            f"({latest_context_usage(prompt).cache_hit_rate:.0f}% cached) · "
            f"{fmt_tokens(usage.output)} out · "
            f"{usage.requests} thinking rounds{subagents}"
        )))
    return items


def history_feed(analysis: Analysis) -> list[tuple[int, str]]:
    items: list[tuple[int, str]] = []
    index_width = max((len(str(prompt.index)) for prompt in analysis.prompts), default=1)
    for prompt in reversed(analysis.prompts):
        usage = prompt.total_usage
        quoted_prompt = json.dumps(prompt.prompt, ensure_ascii=False)
        text = f"{prompt.index:>{index_width}} | {timestamp_hm(prompt.timestamp)} · {quoted_prompt}"
        subagents = f" · {len(prompt.sub_sessions)} sub-agents" if prompt.sub_sessions else ""
        items.append((prompt.index, (
            f"{text}  ·  {fmt_tokens(latest_context_size(prompt))} context "
            f"({latest_context_usage(prompt).cache_hit_rate:.0f}% cached) · "
            f"{fmt_tokens(usage.output)} out · "
            f"{usage.requests} thinking rounds{subagents}"
        )))
    return items


def pct(part: float, whole: float) -> float:
    return (part / whole * 100) if whole else 0.0


OVERALL_SECTIONS = (
    "Scope", "Consumption", "Provider / source split", "Top projects",
    "Model split", "Consumption basis", "Time trend",
)
OVERALL_MAX_MODELS_SHOWN = 8
OVERALL_TREND_DAYS = 14


def overall_lines(report: OverallReport, width: int) -> list[str]:
    """Shallow, scannable summary for the Overall page — one screen, no drill-down."""
    total = report.total
    lines: list[str] = []

    lines.append("Scope")
    lines.append(f"  Sessions active in the last {report.window_days} days")
    scope = f"  {report.session_count} of {report.discovered_count} discovered sessions analyzed"
    if report.unreadable:
        scope += f" · {report.unreadable} unreadable, excluded"
    if report.excluded_old:
        scope += f" · {report.excluded_old} older than {report.window_days} days, excluded"
    lines.append(scope)
    lines.append("")

    lines.append("Consumption")
    if total.consumption:
        lines.append(
            f"  {fmt_consumption(total.consumption)} score  ·  "
            f"{fmt_consumption(total.fresh_consumption)} fresh contribution  ·  "
            f"{fmt_consumption(total.cache_consumption)} cache contribution"
        )
        lines.append(
            f"  {report.session_count} sessions  ·  {report.prompt_count} prompts  ·  "
            f"{total.requests} model requests"
        )
    else:
        lines.append("  Not measured — no consumption found in scope")
    lines.append("")

    lines.append("Provider / source split")
    provider_lines = [
        f"  {provider.title():<7} {pct(usage.consumption, total.consumption):>5.1f}%  "
        f"{fmt_consumption(usage.consumption):>8}  ·  {report.provider_sessions.get(provider, 0)} sessions"
        for provider, usage in report.provider_usage.items()
        if usage.consumption or report.provider_sessions.get(provider, 0)
    ]
    lines.extend(provider_lines or ["  Not measured"])
    lines.append("")

    lines.append("Top projects")
    if report.projects:
        for index, project in enumerate(report.projects, 1):
            lines.append(
                f"  {index:>2}. {truncate(project.name, 26):<26}  "
                f"{pct(project.usage.consumption, total.consumption):>5.1f}%  "
                f"{fmt_consumption(project.usage.consumption):>8}  ·  {project.sessions} sessions"
            )
    else:
        lines.append("  No projects discovered")
    lines.append("")

    lines.append("Model split")
    model_consumption = total.model_consumption
    if model_consumption:
        ranked = model_consumption.most_common(OVERALL_MAX_MODELS_SHOWN)
        for model, score in ranked:
            lines.append(
                f"  {short_model(model, 22):<22} "
                f"{pct(score, total.consumption):>5.1f}%  {fmt_consumption(score):>8}"
            )
        if len(model_consumption) > OVERALL_MAX_MODELS_SHOWN:
            other = total.consumption - sum(score for _, score in ranked)
            lines.append(
                f"  {'Other models':<22} {pct(other, total.consumption):>5.1f}%  "
                f"{fmt_consumption(other):>8}"
            )
    else:
        lines.append("  Not measured")
    lines.append("")

    lines.append("Consumption basis")
    if total.consumption:
        lines.append(
            f"  Fresh   {pct(total.fresh_consumption, total.consumption):>5.1f}%  "
            f"{fmt_consumption(total.fresh_consumption):>8}  ×{CONSUMPTION_CONFIG.fresh_weight:g}"
        )
        lines.append(
            f"  Cache   {pct(total.cache_consumption, total.consumption):>5.1f}%  "
            f"{fmt_consumption(total.cache_consumption):>8}  ×{CONSUMPTION_CONFIG.cache_weight:g}"
            f"  ({total.cache_hit_rate:.0f}% cached across all context)"
        )
    else:
        lines.append("  Not measured")
    lines.append("")

    lines.append("Time trend")
    recent_days = report.days[-OVERALL_TREND_DAYS:]
    if recent_days:
        peak = max((day.usage.consumption for day in recent_days), default=0)
        bar_width = max(4, min(24, width - 30))
        for day in recent_days:
            score = day.usage.consumption
            filled = max(1, round(score / peak * bar_width)) if peak and score else 0
            lines.append(f"  {day.day}  {'█' * filled:<{bar_width}}  {fmt_consumption(score):>8}")
    else:
        lines.append("  Not measured — no timestamps found in scope")

    return lines


def overall_project_line_indices(lines: list[str]) -> list[int]:
    return [index for index, line in enumerate(lines) if re.match(r"^\s+\d+\.\s", line)]


PROJECT_DETAIL_SECTIONS = ("Model mix", "Top 5 sessions by consumption · last 30 days", "Top 5 files")
PROJECT_DETAIL_CONTENT_WIDTH = 120


def project_detail_lines(project: ProjectUsage, width: int) -> list[str]:
    """Measured model/task usage plus observed file activity for one project."""
    content_width = max(24, min(width, PROJECT_DETAIL_CONTENT_WIDTH))
    lines = [
        project.name,
        (
            f"{fmt_consumption(project.usage.consumption)} consumption · {project.sessions} sessions · "
            f"{project.prompts} prompts · {project.usage.requests} model requests"
        ),
        "",
        "Model mix",
    ]
    model_consumption = project.usage.model_consumption
    if model_consumption:
        for model, score in model_consumption.most_common():
            lines.append(
                f"  {short_model(model, 24):<24} "
                f"{pct(score, project.usage.consumption):>5.1f}%  {fmt_consumption(score):>9}"
            )
    else:
        lines.append("  Not measured")

    lines.extend(["", "Top 5 sessions by consumption · last 30 days"])
    sessions = sorted(
        project.recent_sessions, key=lambda item: item.usage.consumption, reverse=True,
    )[:5]
    if sessions:
        for index, session in enumerate(sessions, 1):
            day = (session.timestamp or "unknown date")[:10]
            model = short_model(session.usage.primary_model, 14)
            lines.append(space_between(
                f"  {index}. {session.label}", fmt_consumption(session.usage.consumption), content_width,
            ))
            context = (
                f"ctx {fmt_tokens(session.latest_context)} latest / {fmt_tokens(session.peak_context)} peak · "
                f"{session.cache_hit_rate:.0f}% cached overall"
                if session.latest_context else "ctx not measured"
            )
            if session.provider == "codex" and session.rollout_id:
                identity = truncate(display_rollout_id(session.rollout_id), 64)
                if session.rollout_count > 1:
                    identity += f" ({session.rollout_count} shards)"
            else:
                identity = f"session {truncate(session.session_id, 22)}"
            lines.append(
                f"     {identity} · {model} · {day} · "
                f"{session.prompt_count} prompts · {context}"
            )
    else:
        lines.append("  No measured sessions in the last 30 days")

    lines.extend(["", "Top 5 files", "  Ranked by observed file operations, not consumption attribution."])
    if project.files:
        for index, (path, operations) in enumerate(project.files.most_common(5), 1):
            lines.append(f"  {index}. {truncate(path, max(12, width - 22))} · {operations} operations")
    else:
        lines.append("  No structured file activity observed")
    return lines


def project_session_line_indices(lines: list[str]) -> list[int]:
    """Return the metadata rows that act as session links in project detail."""
    return [
        index for index, line in enumerate(lines)
        if line.startswith("     session ") or line.startswith("     rollout-")
    ]


DETAIL_SECTIONS = (
    "Actors", "Timeline", "Files", "Requests", "Context attribution",
    "Usage observation", "Sub-agents",
)


def context_attribution(prompt: PromptTurn) -> list[tuple[str, int]]:
    """Locally attribute observed evidence, reconciled to the exact provider context total."""
    total = latest_context_size(prompt)
    categories = (
        ("System", ("system", "instructions")),
        ("Skills", ("skills",)),
        ("Instructions", ("agents",)),
        ("Repo", ("repository",)),
        ("Conversation", ("conversation",)),
        ("Tool output", ("tool output", "worker results")),
    )
    weights = [sum(prompt.evidence_chars.get(key, 0) for key in keys) for _, keys in categories]
    evidence_total = sum(weights)
    if not evidence_total:
        return []
    values = [round(total * weight / evidence_total) for weight in weights]
    if values:
        target = max(range(len(weights)), key=weights.__getitem__)
        values[target] += total - sum(values)
    allocated = [
        (categories[index][0], value)
        for index, value in enumerate(values)
        if weights[index] > 0 and value > 0
    ]
    return allocated


def main_actor_name(prompt: PromptTurn) -> str:
    model = prompt.main.primary_model.lower()
    return "Codex" if model.startswith(("gpt-", "codex")) else "Claude"


def latest_context_size(prompt: PromptTurn) -> int:
    return latest_context_usage(prompt).context_total


def latest_context_usage(prompt: PromptTurn) -> Usage:
    return prompt.requests[-1].usage if prompt.requests else prompt.total_usage


def prompt_overview_lines(prompt: PromptTurn, width: int) -> list[str]:
    usage = prompt.total_usage
    completed = any(item.label == "Prompt completed" for item in prompt.timeline)
    interrupted = "Interrupted by user" in prompt.events
    status = "interrupted" if interrupted else ("completed" if completed else "last observed")
    timestamps = [parse_iso_timestamp(item.timestamp) for item in prompt.timeline]
    timestamps = [value for value in timestamps if value]
    span = ""
    if timestamps:
        duration = max(0, round((timestamps[-1] - timestamps[0]).total_seconds()))
        span = f" · {timestamp_hm(prompt.timestamp)}–{timestamps[-1].strftime('%H:%M')} · {duration}s"

    actor_parts = []
    if prompt.main.total:
        actor_parts.append(main_actor_name(prompt))
    actor_parts.extend(f"{truncate(actor.label, 30)} ({actor.status})" for actor in prompt.actors[:2])
    if len(prompt.actors) > 2:
        actor_parts.append(f"+{len(prompt.actors) - 2} more")
    actor_summary = " · ".join(actor_parts) or "none observed"

    return [
        f"Prompt {prompt.index}",
        f'“{truncate(prompt.prompt, max(10, width - 4))}”',
        f"{status}{span}",
        "",
        "Overview",
        f"  Actors    {truncate(actor_summary, max(10, width - 14))}",
        (
            f"  I/O       {fmt_tokens(latest_context_size(prompt))} context "
            f"({latest_context_usage(prompt).cache_hit_rate:.0f}% cached) · "
            f"{fmt_tokens(usage.output)} out"
        ),
        (
            f"  Work      {len(prompt.requests)} thinking rounds · "
            f"{len(prompt.timeline)} events · {len(prompt.files)} file ops"
            + (f" · {len(prompt.sub_sessions)} sub-agents" if prompt.sub_sessions else "")
        ),
        "",
        f"Actors                 {len(prompt.actors) + (1 if prompt.main.total else 0)}",
        f"Timeline               {len(prompt.timeline)} events",
        f"Files                  {len(prompt.files)} operations",
        f"Requests               {len(prompt.requests)}",
        f"Context attribution    {fmt_tokens(latest_context_size(prompt))} current",
        f"Usage observation      {len(prompt.usage_observations)} captured",
        f"Sub-agents             {len(prompt.sub_sessions)}",
    ]


def section_line_indices(lines: list[str]) -> list[int]:
    return [
        index for index, line in enumerate(lines)
        if any(line.startswith(section) for section in DETAIL_SECTIONS)
    ]


def request_line_indices(lines: list[str]) -> list[int]:
    return [index for index, line in enumerate(lines) if re.match(r"^\s*\d+\s+\|", line)]


def sub_session_usage(sub: SubSession) -> Usage:
    return sub.analysis.total_usage if sub.analysis is not None else sub.usage


def subagent_detail_lines(sub: SubSession, width: int) -> list[str]:
    usage = sub_session_usage(sub)
    lines = [
        sub.label, "",
        f"Status       {sub.status}",
        f"Thread       {sub.thread_id or sub.key}",
        f"Context      {fmt_tokens(usage.context_total)} ({usage.cache_hit_rate:.0f}% cached)",
        f"Output       {fmt_tokens(usage.output)}",
        f"Rounds       {usage.requests}",
    ]
    if sub.analysis is not None:
        lines.extend(["", f"Prompts      {len(sub.analysis.prompts)}"])
        for prompt in sub.analysis.prompts:
            lines.append(f"  {prompt.index} | {timestamp_hm(prompt.timestamp)} · {truncate(prompt.prompt, max(10, width - 18))}")
    return lines


def wrapped_prefixed_lines(
    value: str, width: int, first_prefix: str, continuation_prefix: str,
) -> list[str]:
    available = max(1, width - len(first_prefix) - 1)
    lines: list[str] = []
    first = True
    for physical_line in (value.splitlines() or [""]):
        wrapped = textwrap.wrap(
            physical_line, width=available, replace_whitespace=False,
            drop_whitespace=False, break_long_words=True, break_on_hyphens=False,
        ) or [""]
        for part in wrapped:
            prefix = first_prefix if first else continuation_prefix
            lines.append(prefix + part)
            first = False
    return lines


def output_box_lines(value: str, width: int) -> list[str]:
    indent = "    "
    rule_width = max(8, min(56, width - len(indent) - 1))
    lines = [indent + "┌" + "─" * (rule_width - 1)]
    content = value or "No output captured"
    lines.extend(wrapped_prefixed_lines(content, width, indent + "│ ", indent + "│ "))
    lines.append(indent + "└" + "─" * (rule_width - 1))
    return lines


def request_commands(request: RequestInfo) -> str:
    commands = [
        request.action_details[index]
        for index, action in enumerate(request.actions)
        if action.startswith("Bash")
        and index < len(request.action_details)
        and request.action_details[index]
    ]
    return "\n".join(commands)


def copy_to_clipboard(value: str) -> bool:
    if not value:
        return False
    try:
        subprocess.run(("pbcopy",), input=value, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return False
    return True


def default_overall_report_path() -> Path:
    return Path.home() / "Library" / "Caches" / "execution-profiler" / "overall-report.html"


# Fixed categorical order — colors are assigned by rank/entity, never reused ad hoc,
# so a re-export with the same data always paints the same series the same color.
OVERALL_CATEGORICAL_COLORS = (
    "#2a78d6", "#eb6834", "#1baf7a", "#eda100",
    "#e87ba4", "#008300", "#4a3aa7", "#e34948",
)
OVERALL_HTML_MAX_MODELS = 30
OVERALL_HTML_MAX_TREND_DAYS = 90


def render_overall_html(report: OverallReport, generated_at: str) -> str:
    """Render a single, offline, self-contained HTML usage report — inline CSS, no JS or external assets."""
    esc = html.escape
    total = report.total

    def bar_row(label: str, value: float, whole: float, color: str, sub: str = "") -> str:
        return (
            '<div class="bar-row">'
            f'<div class="bar-label" title="{esc(label)}">{esc(label)}</div>'
            '<div class="bar-track">'
            f'<div class="bar-fill" style="width:{pct(value, whole):.2f}%;background:{color}"></div>'
            "</div>"
            f'<div class="bar-value">{pct(value, whole):.1f}% · {esc(fmt_consumption(value))}{esc(sub)}</div>'
            "</div>"
        )

    scope_note = (
        f"last {report.window_days} days · "
        f"{report.session_count} of {report.discovered_count} discovered sessions analyzed"
    )
    if report.unreadable:
        scope_note += f" · {report.unreadable} unreadable, excluded"
    if report.excluded_old:
        scope_note += f" · {report.excluded_old} older than {report.window_days} days, excluded"

    kpi_tiles = "".join(
        f'<div class="tile"><div class="tile-value">{esc(value)}</div><div class="tile-label">{esc(label)}</div></div>'
        for label, value in (
            ("Consumption score", fmt_consumption(total.consumption) if total.consumption else "—"),
            ("Sessions", str(report.session_count)),
            ("Prompts", str(report.prompt_count)),
            ("Model requests", str(total.requests)),
        )
    )

    provider_colors = {"claude": "var(--series-1)", "codex": "var(--series-2)"}
    provider_rows = "".join(
        bar_row(
            provider.title(), usage.consumption, total.consumption,
            provider_colors.get(provider, "var(--muted)"),
            sub=f" · {report.provider_sessions.get(provider, 0)} sessions",
        )
        for provider, usage in report.provider_usage.items()
        if usage.consumption or report.provider_sessions.get(provider, 0)
    ) or '<p class="muted">Not measured.</p>'

    project_details = []
    for project in report.projects:
        project_models = project.usage.model_consumption
        model_mix = "".join(
            bar_row(
                short_model(model, 40), score, project.usage.consumption,
                OVERALL_CATEGORICAL_COLORS[index % len(OVERALL_CATEGORICAL_COLORS)],
            )
            for index, (model, score) in enumerate(project_models.most_common())
        ) or '<p class="muted">Not measured.</p>'
        sessions = sorted(
            project.recent_sessions, key=lambda item: item.usage.consumption, reverse=True,
        )[:5]
        session_items = "".join(
            '<div class="session-row">'
            '<div class="session-main">'
            f'<span class="session-rank">{index}.</span>'
            f'<span class="session-title">{esc(session.label)}</span>'
            f'<strong class="session-score">{esc(fmt_consumption(session.usage.consumption))}</strong>'
            '</div>'
            '<div class="session-meta">'
            + (
                f'<code title="thread {esc(session.session_id)} · full rollout {esc(session.rollout_id)}">'
                f'{esc(display_rollout_id(session.rollout_id))}</code>'
                f'<span>{session.rollout_count} shard(s)</span>'
                if session.provider == "codex" and session.rollout_id
                else f'<code title="{esc(session.session_id)}">session {esc(session.session_id)}</code>'
            )
            +
            f'<span>{esc(short_model(session.usage.primary_model, 32))}</span>'
            f'<span>{esc((session.timestamp or "unknown date")[:10])}</span>'
            f'<span>{session.prompt_count} prompts</span>'
            f'<span>ctx {esc(fmt_tokens(session.latest_context))} latest / '
            f'{esc(fmt_tokens(session.peak_context))} peak · {session.cache_hit_rate:.0f}% cached overall</span>'
            '</div></div>'
            for index, session in enumerate(sessions, 1)
        ) or '<p class="muted">No measured sessions in the last 30 days.</p>'
        file_rows = "".join(
            f"<tr><td>{index}</td><td><code>{esc(path)}</code></td><td>{operations}</td></tr>"
            for index, (path, operations) in enumerate(project.files.most_common(5), 1)
        ) or '<tr><td colspan="3" class="muted">No structured file activity observed.</td></tr>'
        project_details.append(f"""
        <details class="project-detail">
          <summary>
            <span class="project-name" title="{esc(project.root)}">{esc(project.name)}</span>
            <span class="bar-track"><span class="bar-fill" style="width:{pct(project.usage.consumption, total.consumption):.2f}%;background:var(--series-seq)"></span></span>
            <span class="summary-value">{pct(project.usage.consumption, total.consumption):.1f}% · {esc(fmt_consumption(project.usage.consumption))} · {project.sessions} session(s)</span>
          </summary>
          <div class="detail-body">
            <h3>Model mix within project</h3>{model_mix}
            <h3>Top 5 sessions by consumption · last 30 days</h3>
            <div class="session-list">{session_items}</div>
            <h3>Top 5 files by observed activity</h3>
            <p class="muted small">File activity is ranked by observed operations; consumption is not attributed to individual files.</p>
            <div class="table-scroll"><table><thead><tr><th>#</th><th>File</th><th>Operations</th></tr></thead><tbody>{file_rows}</tbody></table></div>
          </div>
        </details>""")
    project_detail_sections = "".join(project_details) or '<p class="muted">No projects discovered.</p>'

    model_consumption = total.model_consumption
    model_items = model_consumption.most_common(OVERALL_HTML_MAX_MODELS)
    model_rows = "".join(
        bar_row(
            short_model(model, 40), score, total.consumption,
            OVERALL_CATEGORICAL_COLORS[index % len(OVERALL_CATEGORICAL_COLORS)],
        )
        for index, (model, score) in enumerate(model_items)
    )
    if len(model_consumption) > OVERALL_HTML_MAX_MODELS:
        other = total.consumption - sum(score for _, score in model_items)
        model_rows += bar_row("Other models", other, total.consumption, "var(--muted)")
    model_rows = model_rows or '<p class="muted">Not measured.</p>'

    consumption_parts = (
        (f"Fresh ×{CONSUMPTION_CONFIG.fresh_weight:g}", total.fresh_consumption, "var(--series-1)"),
        (f"Cache ×{CONSUMPTION_CONFIG.cache_weight:g}", total.cache_consumption, "var(--series-3)"),
    )
    io_segments = "".join(
        f'<div class="stack-seg" style="width:{pct(value, total.consumption):.2f}%;background:{color}" '
        f'title="{esc(name)} · {pct(value, total.consumption):.1f}%"></div>'
        for name, value, color in consumption_parts if value
    )
    io_legend = "".join(
        f'<div class="legend-item"><span class="swatch" style="background:{color}"></span>'
        f'{esc(name)} · {pct(value, total.consumption):.1f}% · {esc(fmt_consumption(value))}</div>'
        for name, value, color in consumption_parts
    )

    trend_days = report.days[-OVERALL_HTML_MAX_TREND_DAYS:]
    peak = max((day.usage.consumption for day in trend_days), default=0)
    if trend_days:
        trend_bars = "".join(
            f'<div class="trend-bar" style="height:{(day.usage.consumption / peak * 100) if peak else 0:.1f}%" '
            f'title="{esc(day.day)} · {esc(fmt_consumption(day.usage.consumption))}"></div>'
            for day in trend_days
        )
        trend_section = (
            f'<div class="trend-chart">{trend_bars}</div>'
            f'<div class="trend-range muted">{esc(trend_days[0].day)} → {esc(trend_days[-1].day)}'
            f'{" (most recent " + str(OVERALL_HTML_MAX_TREND_DAYS) + " days)" if len(report.days) > OVERALL_HTML_MAX_TREND_DAYS else ""}'
            "</div>"
        )
    else:
        trend_section = '<p class="muted">Not measured — no timestamps found in scope.</p>'

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agent Monitor — Overall consumption report</title>
<style>
  :root {{
    color-scheme: light;
    --surface: #fcfcfb; --page: #f9f9f7; --text: #0b0b0b; --text-2: #52514e;
    --muted: #898781; --grid: #e1e0d9; --border: rgba(11,11,11,0.10);
    --series-1: #2a78d6; --series-2: #eb6834; --series-3: #1baf7a; --series-seq: #2a78d6;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      color-scheme: dark;
      --surface: #1a1a19; --page: #0d0d0d; --text: #ffffff; --text-2: #c3c2b7;
      --muted: #898781; --grid: #2c2c2a; --border: rgba(255,255,255,0.10);
      --series-1: #3987e5; --series-2: #d95926; --series-3: #199e70; --series-seq: #3987e5;
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 32px 16px 64px; background: var(--page); color: var(--text);
    font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
  }}
  main {{ max-width: 880px; margin: 0 auto; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  h2 {{ font-size: 13px; text-transform: uppercase; letter-spacing: .04em; color: var(--text-2); margin: 0 0 12px; }}
  h3 {{ font-size: 13px; margin: 20px 0 10px; }}
  .meta {{ color: var(--muted); font-size: 12px; margin-bottom: 28px; }}
  section {{
    background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
    padding: 20px; margin-bottom: 16px;
  }}
  .tiles {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; padding: 0; background: none; border: none; }}
  .tile {{ background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 16px; }}
  .tile-value {{ font-size: 26px; font-weight: 600; font-variant-numeric: tabular-nums; }}
  .tile-label {{ color: var(--text-2); font-size: 12px; margin-top: 4px; }}
  .bar-row {{ display: grid; grid-template-columns: 160px 1fr 220px; align-items: center; gap: 10px; padding: 5px 0; }}
  .bar-label {{ font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .bar-track {{ height: 10px; background: var(--grid); border-radius: 4px; overflow: hidden; }}
  .bar-fill {{ display: block; height: 100%; border-radius: 4px; }}
  .bar-value {{ font-size: 12px; color: var(--text-2); font-variant-numeric: tabular-nums; white-space: nowrap; }}
  .stack {{ display: flex; height: 14px; border-radius: 4px; overflow: hidden; background: var(--grid); gap: 2px; }}
  .stack-seg {{ height: 100%; }}
  .legend {{ display: flex; flex-wrap: wrap; gap: 16px; margin-top: 12px; font-size: 12px; color: var(--text-2); }}
  .legend-item {{ display: flex; align-items: center; gap: 6px; }}
  .swatch {{ width: 10px; height: 10px; border-radius: 2px; display: inline-block; }}
  .trend-chart {{ display: flex; align-items: flex-end; gap: 2px; height: 90px; border-bottom: 1px solid var(--grid); }}
  .trend-bar {{ flex: 1; background: var(--series-1); border-radius: 2px 2px 0 0; min-height: 1px; }}
  .trend-range {{ margin-top: 6px; font-size: 11px; }}
  .project-detail {{ border-top: 1px solid var(--grid); }}
  .project-detail:first-of-type {{ border-top: 0; }}
  .project-detail summary {{ display: grid; grid-template-columns: 14px minmax(120px, 180px) 1fr minmax(180px, auto); align-items: center; gap: 10px; padding: 12px 0; cursor: pointer; font-weight: 600; list-style: none; }}
  .project-detail summary::-webkit-details-marker {{ display: none; }}
  .project-detail summary::before {{ content: "▸"; color: var(--muted); }}
  .project-detail[open] summary::before {{ content: "▾"; }}
  .project-detail summary .bar-track {{ width: 100%; }}
  .project-name {{ overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .summary-value {{ color: var(--text-2); font-weight: 400; white-space: nowrap; }}
  .detail-body {{ padding: 0 0 18px 16px; }}
  .session-list {{ border-top: 1px solid var(--grid); }}
  .session-row {{ padding: 13px 0; border-bottom: 1px solid var(--grid); }}
  .session-main {{ display: grid; grid-template-columns: 24px minmax(0, 1fr) auto; align-items: baseline; gap: 8px; }}
  .session-rank {{ color: var(--muted); font-variant-numeric: tabular-nums; }}
  .session-title {{ min-width: 0; font-weight: 600; overflow-wrap: anywhere; }}
  .session-score {{ color: var(--series-1); font-size: 14px; font-variant-numeric: tabular-nums; white-space: nowrap; }}
  .session-meta {{ display: flex; flex-wrap: wrap; gap: 4px 0; margin: 5px 0 0 32px; color: var(--muted); font-size: 11px; line-height: 1.45; }}
  .session-meta > * {{ display: inline-flex; align-items: baseline; }}
  .session-meta > * + *::before {{ content: "·"; margin: 0 7px; color: var(--grid); }}
  .session-meta code {{ color: var(--text-2); overflow-wrap: anywhere; }}
  .table-scroll {{ overflow-x: auto; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
  th, td {{ padding: 7px 8px; border-bottom: 1px solid var(--grid); text-align: left; vertical-align: top; }}
  th {{ color: var(--muted); font-weight: 500; }}
  td:last-child, th:last-child {{ text-align: right; white-space: nowrap; }}
  code {{ color: var(--text-2); }}
  .small {{ font-size: 11px; }}
  .muted {{ color: var(--muted); }}
  .unmeasured {{ font-size: 12px; color: var(--muted); border-top: 1px solid var(--grid); margin-top: 16px; padding-top: 12px; }}
  footer {{ color: var(--muted); font-size: 11px; margin-top: 24px; }}
  @media (max-width: 680px) {{
    .bar-row {{ grid-template-columns: 110px 1fr; }}
    .bar-row .bar-value {{ grid-column: 2; }}
    .project-detail summary {{ grid-template-columns: 14px 1fr auto; }}
    .project-detail summary .bar-track {{ display: none; }}
    .detail-body {{ padding-left: 0; }}
    .session-main {{ grid-template-columns: 20px minmax(0, 1fr); }}
    .session-score {{ grid-column: 2; margin-top: 3px; }}
    .session-meta {{ margin-left: 28px; }}
  }}
</style>
</head>
<body>
<main>
  <h1>Overall consumption report</h1>
  <div class="meta">Generated {esc(generated_at)} · {esc(scope_note)}</div>

  <section class="tiles">{kpi_tiles}</section>

  <section>
    <h2>Provider / source split</h2>
    {provider_rows}
  </section>

  <section>
    <h2>Projects</h2>
    {project_detail_sections}
  </section>

  <section>
    <h2>Model split</h2>
    {model_rows}
  </section>

  <section>
    <h2>Consumption basis</h2>
    <div class="stack">{io_segments}</div>
    <div class="legend">{io_legend}</div>
    <div class="muted" style="margin-top:8px;font-size:12px;">
      {total.cache_hit_rate:.0f}% cached across all observed session context
    </div>
  </section>

  <section>
    <h2>Time trend</h2>
    {trend_section}
  </section>

  <div class="unmeasured">
    Consumption is a configurable heuristic: fresh × {CONSUMPTION_CONFIG.fresh_weight:g} plus
    cache-read × {CONSUMPTION_CONFIG.cache_weight:g}. It is not a price, billed amount, or direct
    compute measurement. Sessions this tool could not parse are excluded from every score above and
    counted separately in the scope line.
  </div>

  <footer>agent-monitor · offline, self-contained report · no external assets or network requests</footer>
</main>
</body>
</html>
"""


def write_overall_report(report: OverallReport) -> tuple[bool, str]:
    path = default_overall_report_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        generated_at = dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        path.write_text(render_overall_html(report, generated_at), encoding="utf-8")
    except OSError as exc:
        return False, f"export failed: {exc}"
    return True, f"saved {path}"


def open_in_browser(target: str) -> tuple[bool, str]:
    """Hand a file path or URL to the default browser without blocking the curses event loop.

    Returns ``(ok, reason)``; ``reason`` is empty when the browser was actually asked to open it.
    """
    if os.environ.get("AGENT_MONITOR_NO_BROWSER"):
        return True, "browser opening disabled"
    try:
        subprocess.Popen(
            ("open", target), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        return False, f"could not open browser: {exc}"
    return True, ""


def open_overall_report(path: Path | None = None) -> tuple[bool, str]:
    """Open an exported report without blocking the curses event loop."""
    target = path or default_overall_report_path()
    opened, reason = open_in_browser(str(target))
    if reason:
        return opened, f"saved {target} · {reason}"
    return True, f"opened in browser · {target}"
