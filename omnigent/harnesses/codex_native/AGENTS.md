# Codex remote resume

Apply persisted permission settings through `preload_codex_thread_for_resume`.
Codex 0.154 rejects explicit permission overrides on `resume --remote`, so
`build_codex_remote_args` omits settings handled by preload from resumed terminal
arguments. Preserve provider/model overrides and fresh-thread permission defaults.
Do not silently strip additional policy settings unless the app-server applies them.
Codex ignores resume overrides for already-loaded threads; test cold resume with
a restarted app-server, and use `thread/settings/update` for live changes.

Regression coverage is in `tests/test_codex_native.py`; app-server startup and
provider configuration tests are in `tests/test_codex_native_app_server.py`.
