from __future__ import annotations


def bind(namespace: dict[str, object]) -> None:
    """Bind report and parser services used by the loopback dashboard."""
    globals().update(namespace)


class DashboardServer:
    """Serve the detached dashboard on loopback and keep its report fresh in the background.

    ``d`` in the TUI detaches the current view into a browser. The page is ``dashboard.html``
    next to this file: one self-contained page that renders ``/api/report`` (the same
    ``OverallReport`` the TUI shows) and browses sessions through ``/api/session/<id>`` and
    ``/api/session/<id>/prompt/<n>`` exactly the way the terminal does. Nothing leaves the
    host: the socket binds 127.0.0.1 only and the page requests nothing but that socket.
    """
    def __init__(
        self, cache_path: str | None, initial: OverallReport | None = None,
        refresh_seconds: float | None = None,
        build: Callable[..., OverallReport] | None = None,
        html_path: Path | None = None,
        on_publish: Callable[[int], None] | None = None,
    ) -> None:
        self.cache_path = cache_path
        self.refresh_seconds = refresh_seconds if refresh_seconds is not None else dashboard_refresh_seconds()
        self.build = build or build_overall_report
        self.html_path = html_path or DASHBOARD_HTML_PATH
        self.on_publish = on_publish
        self.progress = (0, 0)
        self.lock = threading.Lock()
        self.version = 0
        self.generated_at = ""
        self.report: dict[str, Any] | None = None
        self.session_paths: dict[str, str] = {}
        self.session_count = 0
        self.refreshing = False
        self.error = ""
        self.stop_event = threading.Event()
        self.server: ThreadingHTTPServer | None = None
        self.threads: list[threading.Thread] = []
        if initial is not None:
            self.publish(initial)

    @property
    def url(self) -> str:
        if self.server is None:
            return ""
        return f"http://{DASHBOARD_HOST}:{self.server.server_address[1]}/"

    @property
    def running(self) -> bool:
        return self.server is not None and not self.stop_event.is_set()

    def page_html(self) -> str:
        try:
            return self.html_path.read_text(encoding="utf-8")
        except OSError:
            return (
                "<!doctype html><meta charset=\"utf-8\"><title>agent-monitor</title>"
                f"<p>dashboard.html is missing next to {html.escape(str(Path(__file__).resolve()))}; "
                "the JSON API at /api/report is still available.</p>"
            )

    def publish(self, report: OverallReport) -> int:
        generated_at = dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        with self.lock:
            self.version += 1
            self.generated_at = generated_at
            self.session_count = report.session_count
            self.report = report_payload(report, generated_at, self.version)
            self.session_paths = {item.session_id: item.path for item in report.sessions}
            for item in report.sessions:
                if item.rollout_id:
                    self.session_paths.setdefault(item.rollout_id, item.path)
                    self.session_paths.setdefault(display_rollout_id(item.rollout_id), item.path)
            version = self.version
        if self.on_publish is not None:
            self.on_publish(version)
        return version

    def status(self) -> dict[str, Any]:
        with self.lock:
            return {
                "version": self.version, "ready": self.version > 0, "generated_at": self.generated_at,
                "sessions": self.session_count, "refreshing": self.refreshing,
                "progress": list(self.progress),
                "refresh_seconds": self.refresh_seconds, "error": self.error,
            }

    def rebuild(self) -> bool:
        self.refreshing = True
        self.progress = (0, 0)
        background: ProfilerCache | None = None

        def update_progress(done: int, total: int) -> None:
            self.progress = (done, total)

        try:
            if self.cache_path:
                background = ProfilerCache(self.cache_path)
            report = self.build(background, progress=update_progress)
        except Exception as exc:  # keep the previous report; surface the failure in /api/status
            self.error = str(exc)
            return False
        finally:
            if background is not None:
                background.db.close()
            self.refreshing = False
        self.error = ""
        self.publish(report)
        return True

    def _refresh_loop(self) -> None:
        if self.version == 0 and not self.stop_event.is_set():
            self.rebuild()
        while not self.stop_event.wait(self.refresh_seconds):
            self.rebuild()

    def resolve_session(self, reference: str) -> str | None:
        with self.lock:
            known = self.session_paths.get(reference)
        if known and Path(known).is_file():
            return known
        return resolve_session_path(reference)

    def session_payload(self, reference: str, prompt_position: int | None = None) -> dict[str, Any] | None:
        path = self.resolve_session(reference)
        if not path:
            return None
        cache: ProfilerCache | None = None
        try:
            if self.cache_path:
                cache = ProfilerCache(self.cache_path)
            return session_detail_payload(path, cache, prompt_position)
        finally:
            if cache is not None:
                cache.db.close()

    def _handler_class(self) -> type[BaseHTTPRequestHandler]:
        dashboard = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: Any) -> None:
                pass  # curses owns the terminal; never write request logs to it

            def send_json(self, payload: Any, status: int = 200) -> None:
                self.send_body(json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json", status)

            def send_body(self, body: bytes, content_type: str, status: int = 200) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                route = self.path.split("?", 1)[0]
                if route in ("/", "/index.html"):
                    self.send_body(dashboard.page_html().encode("utf-8"), "text/html; charset=utf-8")
                    return
                if route == "/api/status":
                    self.send_json(dashboard.status())
                    return
                if route == "/api/report":
                    with dashboard.lock:
                        report = dashboard.report
                    if report is None:
                        self.send_json({"error": "report not built yet", **dashboard.status()}, 503)
                    else:
                        self.send_json(report)
                    return
                parts = [part for part in route.split("/") if part]
                if len(parts) in (3, 5) and parts[:2] == ["api", "session"] and (len(parts) == 3 or parts[3] == "prompt"):
                    from urllib.parse import unquote
                    reference = unquote(parts[2])
                    position: int | None = None
                    if len(parts) == 5:
                        try:
                            position = int(parts[4])
                        except ValueError:
                            self.send_json({"error": "bad prompt position"}, 400)
                            return
                    try:
                        payload = dashboard.session_payload(reference, position)
                    except KeyError:
                        self.send_json({"error": f"prompt {position} not found"}, 404)
                        return
                    except (OSError, sqlite3.Error) as exc:
                        self.send_json({"error": str(exc)}, 500)
                        return
                    if payload is None:
                        self.send_json({"error": f"session {reference} not found"}, 404)
                    else:
                        self.send_json(payload)
                    return
                self.send_json({"error": "not found"}, 404)

        return Handler

    def start(self) -> str:
        self.server = ThreadingHTTPServer((DASHBOARD_HOST, 0), self._handler_class())
        self.server.daemon_threads = True
        self.threads = [
            threading.Thread(target=self.server.serve_forever, name="agent-monitor-dashboard", daemon=True),
            threading.Thread(target=self._refresh_loop, name="agent-monitor-dashboard-refresh", daemon=True),
        ]
        for thread in self.threads:
            thread.start()
        return self.url

    def stop(self) -> None:
        self.stop_event.set()
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        # The refresh thread may be mid-build; it is a daemon and checks the stop flag afterwards.
        if self.threads:
            self.threads[0].join(timeout=2.0)
