from __future__ import annotations


def bind(namespace: dict[str, object]) -> None:
    """Bind the core services used by the TUI compatibility layer."""
    globals().update(namespace)


class TTYApp:
    def __init__(self, screen: Any, path: str, cache: ProfilerCache | None = None) -> None:
        self.screen = screen
        self.cache = cache
        self.action_attrs: dict[str, int] = {}
        self.path = path
        self.all_session_paths = find_all_sessions()
        if path not in self.all_session_paths:
            self.all_session_paths.insert(0, path)
        self.profiles = discover_session_profiles()
        active_profile = session_profile_id(path, self.profiles)
        if not any(profile.id == active_profile for profile in self.profiles):
            provider = session_provider(path)
            self.profiles.append(SessionProfile(
                active_profile, provider.title(), provider, Path(path).parent,
            ))
        self.session_paths = [
            item for item in self.all_session_paths
            if session_profile_id(item, self.profiles) == active_profile
        ]
        self.session_index = self.session_paths.index(path)
        self.profile_positions: dict[str, str] = {active_profile: path}
        self.view = 1
        self.mode = "list"
        self.follow = True
        self.new_events = 0
        self.cursor = {1: 0, 2: 0}
        self.offset = {1: 0, 2: 0}
        self.detail_offset = 0
        self.detail_page = "prompt"
        self.detail_cursor = 0
        self.clipboard_notice = ""
        self.process_cursor = 0
        self.process_offset = 0
        self.process_tab = "response"
        self.process_follow = True
        self.selected_prompt = 1
        self.search_query = ""
        self.search_matches: list[int] = []
        self.search_position = -1
        self.search_return_mode = "list"
        self.overall_report: OverallReport | None = None
        self.overall_offset = 0
        self.transient_status = ""
        self.transient_status_until = 0.0
        self.overall_load_state = "idle"
        self.overall_load_progress = 0
        self.overall_load_total = 0
        self.overall_load_error = ""
        self.overall_thread: threading.Thread | None = None
        self.dashboard: DashboardServer | None = None
        self.overall_project_cursor = 0
        self.project_session_cursor = 0
        self.selected_project = ""
        self.jump_return: tuple[str, str] | None = None
        self.last_mtime = -1.0
        self.last_refresh = 0.0
        self.analyzer = cache.analyzer(path) if cache else create_analyzer(path)
        self.analysis = merged_session_analysis(path, cache, self.analyzer)
        self.last_record_count = self.analysis.record_count
        self.feed: list[tuple[int, str]] = []
        self.history: list[tuple[int, str]] = []
        self.previous_frame: list[tuple[str, int]] = []
        self.status = "ready"

    def refresh_session_catalog(self) -> None:
        current_profile = session_profile_id(self.path, self.profiles)
        previous_paths = self.session_paths
        refreshed_profiles = discover_session_profiles()
        if any(profile.id == current_profile for profile in refreshed_profiles):
            self.profiles = refreshed_profiles
        all_paths = find_all_sessions()
        candidates = [
            path for path in all_paths
            if session_profile_id(path, self.profiles) == current_profile
        ]
        if self.path not in candidates:
            candidates.insert(min(self.session_index, len(candidates)), self.path)
            all_paths.append(self.path)
        self.all_session_paths = all_paths
        self.session_paths = candidates
        self.session_index = candidates.index(self.path)
        if candidates != previous_paths:
            self.previous_frame = []

    def switch_session(self, delta: int) -> None:
        self.refresh_session_catalog()
        index = max(0, min(len(self.session_paths) - 1, self.session_index + delta))
        if index == self.session_index:
            return
        self.session_index = index
        self.path = self.session_paths[index]
        self.profile_positions[session_profile_id(self.path, self.profiles)] = self.path
        self.analyzer = self.cache.analyzer(self.path) if self.cache else create_analyzer(self.path)
        self.analysis = merged_session_analysis(self.path, self.cache, self.analyzer)
        self.last_record_count = self.analysis.record_count
        self.mode, self.follow, self.new_events = "list", True, 0
        self.cursor, self.offset = {1: 0, 2: 0}, {1: 0, 2: 0}
        self.last_mtime = -1.0
        self.refresh(force=True)
        if self.search_query:
            self.update_search()

    def session_navigation_availability(self) -> tuple[bool, bool]:
        return self.session_index > 0, self.session_index < len(self.session_paths) - 1

    def session_header(self, width: int) -> str:
        profiles = getattr(self, "profiles", [])
        profile_id = session_profile_id(self.path, profiles)
        profile = next((item for item in profiles if item.id == profile_id), None)
        label = profile.label if profile else self.analysis.provider.title()
        prefix = f"< · {label} · "
        suffix = f" · {self.status} · >"
        stem_width = max(1, width - len(prefix) - len(suffix))
        identity = Path(self.path).stem
        if self.analysis.provider == "codex":
            identity = display_rollout_id(identity)
        return truncate_layout(prefix + truncate(identity, stem_width) + suffix, width)

    def select_profile(self, target_profile: str) -> None:
        current_profile = session_profile_id(self.path, self.profiles)
        if target_profile == current_profile:
            return
        candidates = [
            path for path in self.all_session_paths
            if session_profile_id(path, self.profiles) == target_profile
        ]
        if not candidates:
            profile = next((item for item in self.profiles if item.id == target_profile), None)
            label = profile.label if profile else target_profile
            self.status = f"no {label} sessions"
            self.set_status_area(f"No {label} sessions found")
            return
        self.profile_positions[current_profile] = self.path
        target = self.profile_positions.get(target_profile, candidates[0])
        if target not in candidates:
            target = candidates[0]
        self.session_paths = candidates
        self.session_index = candidates.index(target)
        self.path = target
        self.profile_positions[target_profile] = target
        self.analyzer = self.cache.analyzer(self.path) if self.cache else create_analyzer(self.path)
        self.analysis = merged_session_analysis(self.path, self.cache, self.analyzer)
        self.last_record_count = self.analysis.record_count
        self.mode, self.follow, self.new_events = "list", True, 0
        self.cursor, self.offset = {1: 0, 2: 0}, {1: 0, 2: 0}
        self.last_mtime = -1.0
        self.refresh(force=True)
        if self.search_query:
            self.update_search()

    def activate_session_path(
        self, path: str, open_detail: bool = False, return_mode: str | None = None,
    ) -> bool:
        """Switch to a discovered session, optionally opening its first prompt detail."""
        if not path or not Path(path).is_file():
            self.status = "session file unavailable"
            self.set_status_area("Session file unavailable")
            return False
        if return_mode is not None:
            self.jump_return = (self.path, return_mode)
        current_profile = session_profile_id(self.path, self.profiles)
        target_profile = session_profile_id(path, self.profiles)
        self.profile_positions[current_profile] = self.path
        discovered = find_all_sessions()
        if path not in discovered:
            discovered.insert(0, path)
        self.all_session_paths = discovered
        self.session_paths = [
            item for item in discovered
            if session_profile_id(item, self.profiles) == target_profile
        ]
        self.session_index = self.session_paths.index(path)
        self.path = path
        self.profile_positions[target_profile] = path
        self.analyzer = self.cache.analyzer(path) if self.cache else create_analyzer(path)
        self.analysis = merged_session_analysis(path, self.cache, self.analyzer)
        self.last_record_count = self.analysis.record_count
        self.view, self.follow, self.new_events = 1, True, 0
        self.mode = "list"
        self.cursor, self.offset = {1: 0, 2: 0}, {1: 0, 2: 0}
        self.last_mtime = -1.0
        self.refresh(force=True)
        if open_detail and self.analysis.prompts:
            prompt_index = self.analysis.prompts[0].index
            self.cursor[1] = self.prompt_anchor(prompt_index)
            self.selected_prompt = prompt_index
            self.detail_page, self.detail_cursor, self.detail_offset = "prompt", 0, 0
            self.follow = False
            self.mode = "detail"
        return True

    def return_from_jump(self) -> bool:
        """Restore the session and page that initiated a cross-session jump."""
        if self.jump_return is None:
            return False
        path, mode = self.jump_return
        self.jump_return = None
        if not self.activate_session_path(path):
            return False
        self.mode = mode
        return True

    def find_session_path(self, reference: str) -> str | None:
        """Resolve a full logical session ID or rollout filename from the catalog."""
        return resolve_session_path(reference)

    def selected_project_usage(self) -> ProjectUsage | None:
        if not self.overall_report:
            return None
        return next(
            (item for item in self.overall_report.projects if item.root == self.selected_project), None,
        )

    def begin_search(self) -> None:
        self.search_return_mode = self.mode
        self.search_query = ""
        self.mode = "search"

    def set_status_area(self, message: str, duration: float = 10.0) -> None:
        self.transient_status = message
        self.transient_status_until = time.monotonic() + duration if duration else 0.0
        self.previous_frame = []

    def status_area(self) -> str:
        """Return the global bottom-right status, independent of the active page."""
        if self.overall_load_state == "loading":
            spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[int(time.monotonic() * 10) % 10]
            done, total = self.overall_load_progress, self.overall_load_total
            progress = f" {done}/{total} ({done / total:.0%})" if total else ""
            return f"{spinner} Processing overall…{progress}"
        if self.transient_status and time.monotonic() >= self.transient_status_until:
            self.transient_status = ""
            self.transient_status_until = 0.0
        return self.transient_status

    def refresh(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self.last_refresh < REFRESH_INTERVAL:
            return
        self.last_refresh = now
        try:
            mtime = os.path.getmtime(self.path)
            if force or mtime != self.last_mtime:
                old_record_count = self.last_record_count
                if self.analyzer.path != self.path:
                    self.analyzer = self.cache.analyzer(self.path) if self.cache else create_analyzer(self.path)
                else:
                    changed = self.analyzer.poll()
                    if changed and self.cache:
                        self.cache.save(self.analyzer)
                self.analysis = merged_session_analysis(self.path, self.cache, self.analyzer)
                self.feed = live_feed(self.analysis)
                self.history = history_feed(self.analysis)
                if self.search_query:
                    self.update_search()
                added = max(0, self.analysis.record_count - old_record_count)
                if self.follow:
                    self.cursor[1] = self.prompt_anchor(len(self.analysis.prompts))
                    self.new_events = 0
                elif old_record_count:
                    self.new_events += added
                self.cursor[2] = min(self.cursor[2], max(0, len(self.history) - 1))
                self.last_record_count = self.analysis.record_count
                self.last_mtime = mtime
                self.status = dt.datetime.fromtimestamp(mtime).strftime("%H:%M:%S")
        except OSError as exc:
            self.status = f"error: {exc}"

    def current_items(self) -> list[tuple[int, str]]:
        return self.feed if self.view == 1 else self.history

    def prompt_anchor(self, prompt_index: int) -> int:
        """Return the selectable heading row for a prompt in the live feed."""
        for index, (item_prompt, text) in enumerate(self.feed):
            if item_prompt == prompt_index and text.lstrip().startswith(tuple("0123456789")):
                return index
        return next(
            (index for index, (item_prompt, _) in enumerate(self.feed) if item_prompt == prompt_index),
            0,
        )

    def move(self, delta: int) -> None:
        items = self.current_items()
        if not items:
            return
        if self.view == 1:
            prompt_ids = [prompt.index for prompt in self.analysis.prompts]
            current_prompt = items[self.cursor[1]][0]
            try:
                position = prompt_ids.index(current_prompt)
            except ValueError:
                position = 0
            position = max(0, min(len(prompt_ids) - 1, position + delta))
            self.cursor[1] = self.prompt_anchor(prompt_ids[position])
        else:
            self.cursor[2] = max(0, min(len(items) - 1, self.cursor[2] + delta))
        if self.view == 1 and delta < 0:
            self.follow = False

    def follow_latest(self) -> None:
        self.follow, self.new_events = True, 0
        self.cursor[1] = self.prompt_anchor(len(self.analysis.prompts))

    def inspect(self) -> None:
        items = self.current_items()
        if items:
            index = max(0, min(self.cursor[self.view], len(items) - 1))
            self.cursor[self.view] = index
            self.selected_prompt = items[index][0]
            self.detail_offset = 0
            self.detail_page = "prompt"
            self.detail_cursor = 0
            self.mode = "detail"

    def selected_prompt_turn(self) -> PromptTurn | None:
        return next((p for p in self.analysis.prompts if p.index == self.selected_prompt), None)

    def selected_process(self) -> tuple[PromptTurn, Actor] | None:
        processes = session_processes(self.analysis, self.selected_prompt)
        if not processes:
            return None
        self.process_cursor = max(0, min(self.process_cursor, len(processes) - 1))
        return processes[self.process_cursor]

    def open_detail_selection(self) -> None:
        prompt = self.selected_prompt_turn()
        if not prompt:
            return
        if self.detail_page == "prompt":
            self.detail_page = (
                "actors", "timeline", "files", "requests", "context", "usage", "subagents",
            )[self.detail_cursor]
            self.detail_cursor = 0
            self.detail_offset = 0
        elif self.detail_page == "actors" and (prompt.actors or prompt.main.total):
            if prompt.main.total and self.detail_cursor == 0:
                self.detail_page = "actor:main"
                self.detail_offset = 0
                return
            actor_index = self.detail_cursor - (1 if prompt.main.total else 0)
            if actor_index >= 0:
                self.detail_page = f"actor:{actor_index}"
                self.detail_offset = 0
        elif self.detail_page == "requests" and prompt.requests:
            self.detail_cursor = min(self.detail_cursor, len(prompt.requests) - 1)
            self.detail_page = f"request:{self.detail_cursor}"
            self.detail_offset = 0
            self.clipboard_notice = ""
        elif self.detail_page == "subagents" and prompt.sub_sessions:
            self.detail_cursor = min(self.detail_cursor, len(prompt.sub_sessions) - 1)
            self.detail_page = f"subagent:{self.detail_cursor}"
            self.detail_offset = 0

    def back_detail(self) -> None:
        if self.detail_page.startswith("actor:"):
            self.detail_page = "actors"
        elif self.detail_page.startswith("request:"):
            self.detail_page = "requests"
        elif self.detail_page.startswith("subagent:"):
            self.detail_page = "subagents"
        elif self.detail_page != "prompt":
            self.detail_page = "prompt"
            self.detail_cursor = 0
        else:
            self.mode = "list"

    def update_search(self) -> None:
        query = self.search_query.casefold()
        if self.view == 1:
            matching_prompts = {
                prompt_index for prompt_index, text in self.feed
                if query and query in text.casefold()
            }
            self.search_matches = [
                self.prompt_anchor(prompt.index) for prompt in self.analysis.prompts
                if prompt.index in matching_prompts
            ]
        else:
            self.search_matches = [
                index for index, (_, text) in enumerate(self.history)
                if query and query in text.casefold()
            ]
        self.search_position = 0 if self.search_matches else -1

    def search_next(self, direction: int) -> None:
        if not self.search_matches:
            return
        items = self.current_items()
        self.search_matches = [index for index in self.search_matches if 0 <= index < len(items)]
        if not self.search_matches:
            self.search_position = -1
            return
        self.search_position = (self.search_position + direction) % len(self.search_matches)
        match = self.search_matches[self.search_position]
        if self.view == 1:
            prompt_index = self.feed[match][0]
            self.cursor[1] = self.prompt_anchor(prompt_index)
            self.follow = False
        else:
            self.cursor[2] = min(match, max(0, len(self.history) - 1))

    def open_overall(self, force: bool = False) -> None:
        self.mode = "overall"
        self.overall_offset = 0
        if self.overall_load_state == "loading":
            return
        if force or self.overall_load_state in {"idle", "error"}:
            self.overall_report = None
            self.overall_project_cursor = 0
            self.overall_load_state = "loading"
            self.overall_load_progress = 0
            self.overall_load_total = 0
            self.overall_load_error = ""
            cache_path = str(self.cache.path) if self.cache else None

            def update_progress(done: int, total: int) -> None:
                self.overall_load_progress = done
                self.overall_load_total = total

            def load() -> None:
                background_cache: ProfilerCache | None = None
                try:
                    if cache_path:
                        background_cache = ProfilerCache(cache_path)
                    self.overall_report = build_overall_report(background_cache, progress=update_progress)
                    self.overall_load_state = "ready"
                    self.set_status_area(
                        f"Overall ready · {self.overall_report.session_count} sessions",
                    )
                except Exception as exc:  # keep background failures visible without killing the TUI
                    self.overall_load_error = str(exc)
                    self.overall_load_state = "error"
                    self.set_status_area("Overall processing failed")
                finally:
                    if background_cache is not None:
                        background_cache.db.close()
                    self.previous_frame = []

            self.overall_thread = threading.Thread(
                target=load, name="agent-monitor-overall", daemon=True,
            )
            self.overall_thread.start()

    def export_overall_report(self) -> None:
        if self.overall_load_state == "loading":
            done, total = self.overall_load_progress, self.overall_load_total
            progress = f" ({done}/{total})" if total else ""
            self.set_status_area(f"Overall is still processing{progress}")
            return
        if self.overall_report is None:
            self.set_status_area("Report unavailable · press r to retry")
            return
        ok, message = write_overall_report(self.overall_report)
        if not ok:
            self.set_status_area(message)
            return
        opened, open_message = open_overall_report()
        self.set_status_area(message if opened else f"{message} · {open_message}")

    def detach_route(self) -> str:
        """Hash route of the view on screen, so ``d`` opens the browser at the same place."""
        if self.mode in {"overall", "project"}:
            return "#/projects" if self.mode == "project" else "#/overview"
        session = session_identifier(self.path)
        route = f"#/session/{session}"
        if self.mode in {"detail", "processes", "process_detail"} or (
            self.mode == "list" and not self.follow
        ):
            route += f"/prompt/{self.selected_prompt}"
            if self.mode == "detail" and self.detail_page.startswith("request:"):
                route += f"/request/{int(self.detail_page.split(':', 1)[1]) + 1}"
            if session_provider(self.path) == "codex":
                route += f"?shard={Path(self.path).stem}"
        return route

    def detach(self) -> None:
        """Open the current view in the browser, starting the loopback dashboard if needed."""
        started = False
        if self.dashboard is None:
            def announce_ready(version: int) -> None:
                if version == 1 and self.dashboard is not None:
                    self.set_status_area(f"Dashboard ready · {self.dashboard.url} · D stops", duration=0)

            server = DashboardServer(
                str(self.cache.path) if self.cache else None, initial=self.overall_report,
                on_publish=announce_ready,
            )
            try:
                server.start()
            except OSError as exc:
                self.set_status_area(f"detach failed: {exc}")
                return
            self.dashboard = server
            started = True
        url = self.dashboard.url + self.detach_route()
        opened, reason = open_in_browser(url)
        suffix = f" · {reason}" if reason else ""
        building = started and self.dashboard.version == 0
        state = "detached · building report in the background" if building else "detached"
        self.set_status_area(f"{state} · {url} · D stops{suffix}", duration=0 if opened else 10.0)

    def stop_dashboard(self, announce: bool = False) -> None:
        if self.dashboard is not None:
            self.dashboard.stop()
            self.dashboard = None
            if announce:
                self.set_status_area("Dashboard stopped")
        elif announce:
            self.set_status_area("No dashboard running · d to detach")

    def handle_key(self, key: int | str) -> bool:
        text_key = key if isinstance(key, str) else ""
        if isinstance(key, str):
            key = ord(key) if len(key) == 1 else -1
        if self.mode == "search":
            if key == 27:
                self.mode = self.search_return_mode
            elif key in (10, 13, curses.KEY_ENTER):
                if self.search_query.startswith("#"):
                    path = self.find_session_path(self.search_query)
                    if path:
                        self.activate_session_path(
                            path, open_detail=True, return_mode=self.search_return_mode,
                        )
                    else:
                        self.mode = self.search_return_mode
                        self.status = f"session not found: {self.search_query[1:]}"
                        self.set_status_area(f"Session not found: {self.search_query[1:]}")
                else:
                    self.update_search()
                    self.mode = "list"
                    if self.search_matches:
                        self.search_position = -1
                        self.search_next(1)
            elif key in (curses.KEY_BACKSPACE, 127, 8):
                self.search_query = self.search_query[:-1]
            elif text_key and text_key.isprintable():
                self.search_query += text_key
            elif 32 <= key <= 126:
                self.search_query += chr(key)
            return True
        if key == ord("d"):
            self.detach()
            return True
        if key == ord("D"):
            self.stop_dashboard(announce=True)
            return True
        if key == ord("/") and self.mode in {"list", "overall", "project"}:
            self.begin_search()
            return True
        if self.mode == "help":
            if key in (27, curses.KEY_LEFT, ord("?"), ord("q")):
                self.mode = "list"
            return True
        if self.mode in {"processes", "process_detail"}:
            if key == ord("q"):
                return False
            if key == ord("p"):
                self.mode = "list"
            elif key in (27, curses.KEY_LEFT):
                self.mode = "processes" if self.mode == "process_detail" else "list"
            elif self.mode == "processes":
                count = len(session_processes(self.analysis, self.selected_prompt))
                if key == curses.KEY_DOWN:
                    self.process_cursor = min(max(0, count - 1), self.process_cursor + 1)
                elif key == curses.KEY_UP:
                    self.process_cursor = max(0, self.process_cursor - 1)
                elif key in (10, 13, curses.KEY_ENTER, curses.KEY_RIGHT) and count:
                    self.mode, self.process_tab, self.process_offset = "process_detail", "response", 0
            else:
                if key == 9:
                    tabs = ("response", "logs", "metadata")
                    self.process_tab = tabs[(tabs.index(self.process_tab) + 1) % len(tabs)]
                    self.process_offset = 0
                elif key == ord("f"):
                    self.process_follow = not self.process_follow
                elif key == curses.KEY_DOWN:
                    self.process_follow = False
                    self.process_offset += 1
                elif key == curses.KEY_UP:
                    self.process_follow = False
                    self.process_offset = max(0, self.process_offset - 1)
                elif key == curses.KEY_HOME:
                    self.process_follow, self.process_offset = False, 0
                elif key == curses.KEY_END:
                    self.process_follow = True
            return True
        if self.mode == "overall":
            if key == ord("q"):
                return False
            if key in (ord("p"), ord("P")):
                self.export_overall_report()
            elif key in (27, curses.KEY_LEFT, ord("o"), ord("O")):
                self.mode = "list"
            elif key == curses.KEY_DOWN:
                if self.overall_report and self.overall_report.projects:
                    self.overall_project_cursor = min(
                        len(self.overall_report.projects) - 1, self.overall_project_cursor + 1,
                    )
            elif key == curses.KEY_UP:
                self.overall_project_cursor = max(0, self.overall_project_cursor - 1)
            elif key == curses.KEY_NPAGE:
                self.overall_offset += max(1, self.screen.getmaxyx()[0] - 5)
            elif key == curses.KEY_PPAGE:
                self.overall_offset = max(0, self.overall_offset - max(1, self.screen.getmaxyx()[0] - 5))
            elif key == curses.KEY_HOME:
                self.overall_project_cursor = 0
                self.overall_offset = 0
            elif key in (10, 13, curses.KEY_ENTER, curses.KEY_RIGHT):
                if self.overall_report and self.overall_report.projects:
                    project = self.overall_report.projects[self.overall_project_cursor]
                    self.selected_project = project.root
                    self.detail_offset = 0
                    self.project_session_cursor = 0
                    self.mode = "project"
            elif key == ord("r"):
                self.open_overall(force=True)
            return True
        if self.mode == "project":
            if key == ord("q"):
                return False
            if key in (27, curses.KEY_LEFT):
                self.mode = "overall"
            elif key in (ord("p"), ord("P")):
                self.export_overall_report()
            elif key == curses.KEY_DOWN:
                project = self.selected_project_usage()
                sessions = sorted(
                    project.recent_sessions, key=lambda item: item.usage.consumption, reverse=True,
                )[:5] if project else []
                if sessions:
                    self.project_session_cursor = min(
                        len(sessions) - 1, self.project_session_cursor + 1,
                    )
                else:
                    self.detail_offset += 1
            elif key == curses.KEY_UP:
                self.project_session_cursor = max(0, self.project_session_cursor - 1)
            elif key == curses.KEY_NPAGE:
                self.detail_offset += max(1, self.screen.getmaxyx()[0] - 5)
            elif key == curses.KEY_PPAGE:
                self.detail_offset = max(0, self.detail_offset - max(1, self.screen.getmaxyx()[0] - 5))
            elif key == curses.KEY_HOME:
                self.detail_offset = 0
                self.project_session_cursor = 0
            elif key in (10, 13, curses.KEY_ENTER, curses.KEY_RIGHT):
                project = self.selected_project_usage()
                sessions = sorted(
                    project.recent_sessions, key=lambda item: item.usage.consumption, reverse=True,
                )[:5] if project else []
                if sessions:
                    session = sessions[min(self.project_session_cursor, len(sessions) - 1)]
                    self.activate_session_path(session.path, return_mode="project")
            return True
        if key in (27, curses.KEY_LEFT) and self.mode in {"list", "detail"}:
            if self.return_from_jump():
                return True
        if key == ord("q"):
            return False
        for index, hotkey in enumerate(PROFILE_HOTKEYS):
            # ``n`` keeps its established next-search-result meaning while a
            # search is active; otherwise it can address the sixth profile.
            if key == ord(hotkey) and index < len(self.profiles) and not (
                hotkey == "n" and self.search_query
            ):
                self.select_profile(self.profiles[index].id)
                return True
        if key == ord("p") and self.mode == "list":
            items = self.current_items()
            if items:
                index = max(0, min(self.cursor[self.view], len(items) - 1))
                self.selected_prompt = items[index][0]
            self.mode, self.process_cursor, self.process_offset = "processes", 0, 0
            return True
        if key in (ord("o"), ord("O")) and self.mode == "list":
            self.open_overall()
            return True
        if key in (27, curses.KEY_LEFT):
            if self.mode == "detail":
                self.back_detail()
            return True
        if self.mode == "detail":
            prompt = self.selected_prompt_turn()
            if key == ord("y") and prompt and self.detail_page.startswith("request:"):
                try:
                    request_index = int(self.detail_page.split(":", 1)[1])
                    command_text = request_commands(prompt.requests[request_index])
                except (ValueError, IndexError):
                    command_text = ""
                self.clipboard_notice = "copied" if copy_to_clipboard(command_text) else "nothing to copy"
                self.set_status_area(self.clipboard_notice.capitalize())
                return True
            if self.detail_page == "prompt":
                if key == curses.KEY_DOWN: self.detail_cursor = min(6, self.detail_cursor + 1)
                elif key == curses.KEY_UP: self.detail_cursor = max(0, self.detail_cursor - 1)
            elif self.detail_page == "actors" and prompt:
                actor_count = len(prompt.actors) + (1 if prompt.main.total else 0)
                if key == curses.KEY_DOWN: self.detail_cursor = min(max(0, actor_count - 1), self.detail_cursor + 1)
                elif key == curses.KEY_UP: self.detail_cursor = max(0, self.detail_cursor - 1)
            elif self.detail_page == "requests" and prompt:
                if key == curses.KEY_DOWN: self.detail_cursor = min(max(0, len(prompt.requests) - 1), self.detail_cursor + 1)
                elif key == curses.KEY_UP: self.detail_cursor = max(0, self.detail_cursor - 1)
            elif self.detail_page == "subagents" and prompt:
                if key == curses.KEY_DOWN: self.detail_cursor = min(max(0, len(prompt.sub_sessions) - 1), self.detail_cursor + 1)
                elif key == curses.KEY_UP: self.detail_cursor = max(0, self.detail_cursor - 1)
            else:
                if key == curses.KEY_DOWN: self.detail_offset += 1
                elif key == curses.KEY_UP: self.detail_offset = max(0, self.detail_offset - 1)
            if key in (10, 13, curses.KEY_ENTER, curses.KEY_RIGHT):
                self.open_detail_selection()
            return True
        if key == ord("?"):
            self.mode = "help"
        elif key == ord("n"):
            self.search_next(1)
        elif key == ord("N"):
            self.search_next(-1)
        elif key == ord("1"):
            self.view = 1
            if self.search_query:
                self.update_search()
        elif key == ord("2"):
            self.view, self.follow = 2, False
            if self.search_query:
                self.update_search()
        elif key == ord("["):
            self.switch_session(-1)
        elif key == ord("]"):
            self.switch_session(1)
        elif key == curses.KEY_DOWN:
            if self.view == 1 and self.cursor[1] == self.prompt_anchor(len(self.analysis.prompts)):
                self.follow_latest()
            else:
                self.move(1)
        elif key == curses.KEY_UP:
            self.move(-1)
        elif key == curses.KEY_NPAGE:
            self.move(max(1, self.screen.getmaxyx()[0] - 5))
        elif key == curses.KEY_PPAGE:
            self.move(-max(1, self.screen.getmaxyx()[0] - 5))
        elif key == curses.KEY_HOME:
            if self.view == 1 and self.analysis.prompts:
                self.cursor[1] = self.prompt_anchor(self.analysis.prompts[0].index)
                self.follow = False
            else:
                self.cursor[2] = 0
        elif key == curses.KEY_END and self.view == 1:
            self.follow_latest()
        elif key in (10, 13, curses.KEY_ENTER, curses.KEY_RIGHT):
            self.inspect()
        elif key == ord("r"):
            self.refresh(force=True)
        elif key == curses.KEY_RESIZE:
            self.previous_frame = []
            self.screen.clear()
        return True

    def frame(self) -> list[tuple[str, int]]:
        height, width = self.screen.getmaxyx()
        body_height = max(1, height - 3)
        active_live = "[1] Live" if self.view == 1 else " 1  Live"
        active_history = "[2] History" if self.view == 2 else " 2  History"
        active_profile = session_profile_id(self.path, self.profiles)
        source_tabs = "  ".join(
            f"[{key}] {profile.label}"
            if profile.id == active_profile else f" {key}  {profile.label}"
            for key, profile in zip(PROFILE_HOTKEYS, self.profiles)
        )
        header_left = f" Execution Profiler   {active_live}  {active_history}"
        gap = max(2, width - 1 - len(header_left) - len(source_tabs))
        header = truncate_layout(header_left + " " * gap + source_tabs, width - 1)
        rows: list[tuple[str, int]] = [(header, curses.A_BOLD)]
        rows.append((self.session_header(width - 1), 0))

        if self.mode == "processes":
            processes = session_processes(self.analysis, self.selected_prompt)
            active = sum(actor.status in ACTIVE_PROCESS_STATES for _, actor in processes)
            rows.append((f" Prompt {self.selected_prompt} background agents · {active} active · {len(processes) - active} finished", curses.A_BOLD))
            rows.append(("", 0))
            rendered: list[tuple[int, str]] = []
            previous_group = ""
            for index, (prompt, actor) in enumerate(processes):
                group = "ACTIVE" if actor.status in ACTIVE_PROCESS_STATES else "FINISHED"
                if group != previous_group:
                    rendered.append((-1, f"{group:<8}  ENGINE   STATUS      STARTED   FINISHED  ELAPSED"))
                    previous_group = group
                engine = (actor.engine or "process")[:8]
                rendered.append((index, (
                    f"{engine:<8} {actor.status:<10} {process_clock(actor.started_at)} "
                    f"{process_clock(actor.finished_at)} {process_elapsed(actor):>8}  "
                    f"{actor.label}  · prompt {prompt.index}"
                )))
            visible_height = max(1, body_height - 2)
            selected_row = next((i for i, (index, _) in enumerate(rendered) if index == self.process_cursor), 0)
            if selected_row < self.process_offset:
                self.process_offset = selected_row
            elif selected_row >= self.process_offset + visible_height:
                self.process_offset = selected_row - visible_height + 1
            for index, line in rendered[self.process_offset:self.process_offset + visible_height]:
                rows.append((
                    ("> " if index >= 0 else "  ") + truncate_layout(line, width - 3),
                    curses.A_REVERSE if index == self.process_cursor else (curses.A_BOLD if index < 0 else 0),
                ))
            if not processes:
                rows.append((f"  No spawned background agents observed for prompt {self.selected_prompt}.", 0))
            footer = fit_action_bar(
                " PROCESSES │ ↑/↓ select │ → detail │ p/← back │ q quit",
                " PROCESSES │ ↑/↓ │ → detail │ p back",
                width,
            )
        elif self.mode == "process_detail":
            selected = self.selected_process()
            if selected:
                prompt, actor = selected
                tabs = "  ".join(f"[{name}]" if name == self.process_tab else name for name in ("response", "logs", "metadata"))
                rows.append((truncate_layout(f" {actor.engine or 'process'} · {actor.label} · {actor.status} · {process_elapsed(actor)}", width - 1), curses.A_BOLD))
                rows.append((f" {tabs}", 0))
                if self.process_tab == "response":
                    lines = actor_response_lines(actor)
                elif self.process_tab == "logs":
                    lines = actor_log_lines(actor)
                else:
                    lines = actor_detail_lines(actor) + ["", f"Prompt       {prompt.index}", f"Output       {actor.output_path or '-'}", f"Events       {actor.events_path or '-'}"]
                visible_height = max(1, body_height - 2)
                max_offset = max(0, len(lines) - visible_height)
                if self.process_follow and self.process_tab in {"response", "logs"}:
                    self.process_offset = max_offset
                else:
                    self.process_offset = min(self.process_offset, max_offset)
                for line in lines[self.process_offset:self.process_offset + visible_height]:
                    rows.append(("  " + truncate_layout(line, width - 3), 0))
            else:
                rows.append(("  Process not found", 0))
            follow = "follow" if self.process_follow else "paused"
            footer = fit_action_bar(
                f" PROCESS DETAIL │ Tab view │ f {follow} │ ↑/↓ scroll │ ← back",
                f" DETAIL │ Tab │ f {follow} │ ← back",
                width,
            )
        elif self.mode == "overall":
            rows.append((" Overall consumption across discovered sessions", curses.A_BOLD))
            rows.append(("", 0))
            if self.overall_load_state == "loading":
                pass
            elif self.overall_load_state == "error":
                rows.extend([
                    ("  Could not build the overall report", curses.A_BOLD),
                    (f"  {truncate_layout(self.overall_load_error, width - 3)}", 0),
                    ("  Press r to try again.", 0),
                ])
            elif self.overall_report is None:
                rows.append(("  No overall report loaded. Press r to load it.", 0))
            else:
                lines = overall_lines(self.overall_report, width - 3)
                visible_height = max(1, body_height - 2)
                max_offset = max(0, len(lines) - visible_height)
                self.overall_offset = min(self.overall_offset, max_offset)
                project_lines = overall_project_line_indices(lines)
                if project_lines:
                    self.overall_project_cursor = min(self.overall_project_cursor, len(project_lines) - 1)
                    selected_line = project_lines[self.overall_project_cursor]
                    if selected_line < self.overall_offset:
                        self.overall_offset = selected_line
                    elif selected_line >= self.overall_offset + visible_height:
                        self.overall_offset = selected_line - visible_height + 1
                for line_index, line in enumerate(
                    lines[self.overall_offset:self.overall_offset + visible_height], self.overall_offset,
                ):
                    selected = bool(project_lines) and line_index == project_lines[self.overall_project_cursor]
                    attr = curses.A_REVERSE if selected else (curses.A_BOLD if line in OVERALL_SECTIONS else 0)
                    prefix = "> " if selected else "  "
                    rows.append((prefix + truncate_layout(line, width - len(prefix) - 1), attr))
            export_action = "p/P export + open" if self.overall_load_state == "ready" else "p/P after loading"
            footer = fit_action_bar(
                f" OVERALL │ ↑/↓ project │ → open │ PgUp/PgDn scroll │ {export_action} │ d detach"
                " │ r refresh │ ← back │ q quit",
                f" OVERALL │ ↑/↓ project │ → open │ {export_action} │ d detach │ ← back",
                width,
            )
        elif self.mode == "project":
            project = self.selected_project_usage()
            if project is None:
                lines = ["Project not found"]
            else:
                lines = project_detail_lines(project, width - 3)
            visible_height = max(1, body_height)
            max_offset = max(0, len(lines) - visible_height)
            self.detail_offset = min(self.detail_offset, max_offset)
            session_lines = project_session_line_indices(lines)
            if session_lines:
                self.project_session_cursor = min(self.project_session_cursor, len(session_lines) - 1)
                selected_line = session_lines[self.project_session_cursor]
                if selected_line < self.detail_offset:
                    self.detail_offset = selected_line
                elif selected_line >= self.detail_offset + visible_height:
                    self.detail_offset = selected_line - visible_height + 1
            for line_index, line in enumerate(
                lines[self.detail_offset:self.detail_offset + visible_height], self.detail_offset,
            ):
                selected = bool(session_lines) and line_index == session_lines[self.project_session_cursor]
                attr = (
                    curses.A_REVERSE if selected
                    else curses.A_BOLD if line in PROJECT_DETAIL_SECTIONS or (project and line == project.name)
                    else 0
                )
                rows.append((("> " if selected else "  ") + truncate_terminal_layout(line, width - 3), attr))
            footer = fit_action_bar(
                " PROJECT │ ↑/↓ session │ → jump │ / search or #id │ PgUp/PgDn scroll │ p/P export + open"
                " │ d detach │ ← overall │ q quit",
                " PROJECT │ ↑/↓ session │ → jump │ / search │ p export │ d detach │ ← overall",
                width,
            )
        elif self.mode == "detail":
            prompt = self.selected_prompt_turn()
            if prompt and self.detail_page.startswith("actor:"):
                actor_key = self.detail_page.split(":", 1)[1]
                if actor_key == "main":
                    is_last = prompt is self.analysis.prompts[-1]
                    is_live = time.time() - os.path.getmtime(self.analysis.path) < 2
                    main_status = "running" if is_last and is_live else ("last observed" if is_last else "completed")
                    lines = [
                        f"{main_actor_name(prompt)} · {short_model(prompt.main.primary_model, 28)}", "",
                        f"Status       {main_status}", f"Requests     {prompt.main.requests}",
                        f"Context      {fmt_tokens(prompt.main.context_total)}",
                        f"Output       {fmt_tokens(prompt.main.output)}",
                    ]
                else:
                    actor_index = int(actor_key)
                    lines = actor_detail_lines(prompt.actors[actor_index]) if actor_index < len(prompt.actors) else ["Actor not found"]
            else:
                lines = detail_page_lines(prompt, self.detail_page, width - 2) if prompt else ["Prompt not found"]
            prompt_sections = section_line_indices(lines) if self.detail_page == "prompt" else []
            request_lines = request_line_indices(lines) if self.detail_page == "requests" else []
            subagent_lines = request_line_indices(lines) if self.detail_page == "subagents" else []
            max_offset = max(0, len(lines) - body_height)
            self.detail_offset = min(self.detail_offset, max_offset)
            selected_line = None
            if self.detail_page == "prompt" and prompt_sections:
                selected_line = prompt_sections[min(self.detail_cursor, len(prompt_sections) - 1)]
            elif self.detail_page == "actors":
                selected_line = self.detail_cursor + 2
            elif self.detail_page == "requests" and request_lines:
                selected_line = request_lines[min(self.detail_cursor, len(request_lines) - 1)]
            elif self.detail_page == "subagents" and subagent_lines:
                selected_line = subagent_lines[min(self.detail_cursor, len(subagent_lines) - 1)]
            if selected_line is not None:
                if selected_line < self.detail_offset:
                    self.detail_offset = selected_line
                elif selected_line >= self.detail_offset + body_height:
                    self.detail_offset = selected_line - body_height + 1
            for line_index, line in enumerate(lines[self.detail_offset:self.detail_offset + body_height], start=self.detail_offset):
                selectable = False
                if self.detail_page == "prompt" and line_index in prompt_sections:
                    selectable = prompt_sections.index(line_index) == self.detail_cursor
                elif self.detail_page == "actors" and line_index >= 2:
                    selectable = line_index - 2 == self.detail_cursor
                elif self.detail_page == "requests" and line_index in request_lines:
                    selectable = request_lines.index(line_index) == self.detail_cursor
                elif self.detail_page == "subagents" and line_index in subagent_lines:
                    selectable = subagent_lines.index(line_index) == self.detail_cursor
                attr = curses.A_REVERSE if selectable else (curses.A_BOLD if line_index == 0 else 0)
                enterable = (
                    (self.detail_page == "prompt" and line_index in prompt_sections)
                    or (self.detail_page == "actors" and line_index >= 2)
                    or (self.detail_page == "requests" and line_index in request_lines)
                    or (self.detail_page == "subagents" and line_index in subagent_lines)
                )
                prefix = "> " if enterable else "  "
                rows.append((prefix + truncate_layout(line, width - len(prefix) - 1), attr))
            if self.detail_page.startswith("request:"):
                footer = fit_action_bar(
                    " REQUEST DETAIL │ ↑/↓ scroll │ y copy command │ ← back │ q quit",
                    " DETAIL │ ↑/↓ │ y copy │ ← back",
                    width,
                )
            else:
                footer = fit_action_bar(
                    " INSPECT │ ↑/↓ select/scroll │ → open │ ← back │ q quit",
                    " INSPECT │ ↑/↓ │ → open │ ← back │ q",
                    width,
                )
        elif self.mode == "help":
            profile_lines = [
                f"{key:<11} {profile.label} sessions"
                for key, profile in zip(PROFILE_HOTKEYS, self.profiles)
            ]
            lines = [
                "Help",
                "",
                "1 / 2       Live / History",
                "↑ / ↓       Previous / next prompt",
                "Home / End  First prompt / follow latest",
                "→ / Enter   Open selected item",
                "← / Esc     Back one level",
                "[           Previous session",
                "]           Next session",
                *profile_lines,
                "p           Spawned background agents for this session",
                "o / O       Overall consumption page (all discovered sessions)",
                "d / D       Detach this view to a loopback browser dashboard / stop it",
                "/           Search current view",
                "n / N       Next / previous search result",
                "q           Quit",
            ]
            for line in lines[:body_height]:
                rows.append((" " + line, curses.A_BOLD if line == "Help" else 0))
            footer = fit_action_bar(" HELP │ esc back │ ? close │ q back", " HELP │ esc/? back", width)
        elif self.mode == "search":
            rows.append((" Search prompts, or enter #full-session-id", curses.A_BOLD))
            rows.append(("", 0))
            rows.append((f" /{self.search_query}", 0))
            footer = fit_action_bar(" SEARCH │ Unicode supported │ #id jumps to detail │ ↵ open/apply │ esc cancel", " SEARCH │ #id jump │ ↵ apply │ esc", width)
        else:
            items = self.current_items()
            cursor = self.cursor[self.view]
            if self.view == 1 and self.follow:
                self.offset[1] = max(0, len(items) - body_height)
            else:
                if cursor < self.offset[self.view]: self.offset[self.view] = cursor
                if cursor >= self.offset[self.view] + body_height: self.offset[self.view] = cursor - body_height + 1
            start = self.offset[self.view]
            for index, (prompt_index, text) in enumerate(items[start:start + body_height], start=start):
                enterable = self.view == 2 or index == self.prompt_anchor(prompt_index)
                prefix = "> " if enterable else "  "
                rows.append((
                    prefix + truncate(text, width - len(prefix) - 1),
                    curses.A_REVERSE if index == cursor else 0,
                ))
            if self.view == 1 and self.follow:
                footer = fit_action_bar(
                    " FOLLOW │ ↑/↓ prompt │ → open │ p processes │ o overall │ d detach │ / search │ ? help │ q quit",
                    " FOLLOW │ ↑/↓ │ → open │ p processes │ o overall │ / │ ? │ q",
                    width,
                )
            elif self.view == 1:
                updates = f"{self.new_events} updates │ " if self.new_events else ""
                footer = fit_action_bar(
                    f" PAUSED │ {updates}End follow │ ↑/↓ prompt │ → open │ p processes │ o overall │ d detach │ / search │ ? help │ q quit",
                    f" PAUSED │ {updates}End follow │ → open │ p processes │ o overall │ / │ ? │ q",
                    width,
                )
            else:
                footer = fit_action_bar(
                    " HISTORY │ ↑/↓ prompt │ → open │ p processes │ o overall │ d detach │ / search │ ? help │ q quit",
                    " HISTORY │ ↑/↓ │ → open │ p processes │ o overall │ / │ ? │ q",
                    width,
                )

        while len(rows) < height - 1:
            rows.append(("", 0))
        rows = rows[:height - 1]
        rows.append((fit_status_bar(footer, self.status_area(), width), curses.A_REVERSE))
        return rows

    def draw(self) -> None:
        height, width = self.screen.getmaxyx()
        frame = self.frame()
        for row in range(height):
            current = frame[row] if row < len(frame) else ("", 0)
            previous = self.previous_frame[row] if row < len(self.previous_frame) else None
            if current == previous:
                continue
            try:
                self.screen.move(row, 0)
                self.screen.clrtoeol()
                self.screen.addnstr(row, 0, current[0], max(1, width - 1), current[1])
                if row == 1:
                    previous_available, next_available = self.session_navigation_availability()
                    if not previous_available:
                        self.screen.addnstr(row, 0, "<", 1, current[1] | curses.A_DIM)
                    next_position = current[0].rfind(">")
                    if not next_available and next_position >= 0:
                        self.screen.addnstr(
                            row, next_position, ">", 1, current[1] | curses.A_DIM,
                        )
                tool_span = action_span(current[0])
                if tool_span:
                    start, end = tool_span
                    action_attr = self.action_attrs.get(current[0][start:end].lower(), 0)
                    if action_attr:
                        self.screen.addnstr(
                            row, start, current[0][start:end],
                            min(end - start, max(1, width - start - 1)),
                            current[1] | action_attr,
                        )
                path_span = file_path_span(current[0])
                if path_span:
                    start, end = path_span
                    self.screen.addnstr(
                        row, start, current[0][start:end],
                        min(end - start, max(1, width - start - 1)),
                        current[1] | curses.A_DIM,
                    )
            except curses.error:
                pass
        self.previous_frame = frame
        self.screen.noutrefresh()
        curses.doupdate()

    def run(self) -> None:
        curses.set_escdelay(ESCAPE_DELAY_MS)
        curses.curs_set(0)
        if curses.has_colors():
            try:
                curses.start_color()
                curses.use_default_colors()
                for action, (pair, foreground, background) in ACTION_PALETTES.items():
                    curses.init_pair(pair, foreground, background)
                    self.action_attrs[action] = curses.color_pair(pair)
            except curses.error:
                self.action_attrs = {}
        self.screen.timeout(100)
        self.screen.keypad(True)
        self.refresh(force=True)
        try:
            while True:
                self.refresh()
                self.draw()
                try:
                    key = self.screen.get_wch()
                except curses.error:
                    continue
                if not self.handle_key(key):
                    break
        finally:
            self.stop_dashboard()
