# Architecture

`monitoring.py` remains the executable and compatibility surface. Runtime code is split
by responsibility under `agent_monitor/`:

- `models.py` — domain dataclasses and token/consumption accounting.
- `parsers.py` — Claude/Codex transcript normalization and the persistent parser cache.
- `views.py` — terminal formatting and offline HTML report rendering.
- `dashboard.py` — loopback HTTP dashboard lifecycle and API routing.
- `tui.py` — curses state, navigation, and rendering.
- `updater.py` — release discovery, checksum verification, safe extraction, and install.

The executable re-exports the established Python symbols so existing scripts that import
`monitoring` continue to work. UI modules receive the core service namespace through a
small binding boundary; this keeps stateful front ends separate while preserving the
project's patchable compatibility surface during the migration.

## Dependency direction

Domain models do not import parser or UI code. The parser depends on domain and core
discovery/formatting services. Views depend on domain/report services. Dashboard and TUI
sit at the outer edge and consume those services. The updater is independent of session
parsing and UI code.

## Tests

Run every unit and integration test with:

```sh
python3 -m unittest discover -s tests
```

`tests/test_monitoring.py` protects end-to-end compatibility and terminal/dashboard
behavior. `tests/test_refactored_modules.py` covers module boundaries, isolated domain
behavior, updater security checks, release contents, and installed-command viability.

The release manifest test must fail whenever a new runtime module is omitted from the
archive. The structural line-limit test prevents `monitoring.py` from growing back into
the previous 5,000-line monolith.
