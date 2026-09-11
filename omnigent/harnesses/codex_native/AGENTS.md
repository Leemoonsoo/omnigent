# Codex remote resume

Apply persisted permission settings through `preload_codex_thread_for_resume`.
Codex 0.154 rejects explicit permission overrides on `resume --remote`, so
`build_codex_remote_args` omits settings handled by preload from resumed terminal
arguments. Preserve provider/model overrides and fresh-thread permission defaults.
Do not silently strip additional policy settings unless the app-server applies them.
`config_overrides` also includes bypass permissions applied at app-server startup;
persisted `terminal_launch_args` supply the explicit thread-resume overrides.
Codex can ignore resume overrides while other clients remain subscribed; test
cold and unsubscribed resume, and use `thread/settings/update` for live changes.
Preserve terminal permission arguments before Codex 0.154: older remote TUIs can
reload an idle thread with their own settings when no clients are subscribed.
For Codex 0.154+ or an unknown version, retain the preload subscription and pass
that same client to the forwarder. Closing it before attachment lets the TUI
reload the idle thread with different permissions, even on 0.154. Close retained
clients on startup failure, cancellation, and forwarder teardown. Smoke tests
must follow production ownership without adding an independent observer that
could hide a missing subscription.

Regression coverage is in `tests/test_codex_native.py`; app-server startup and
provider configuration tests are in `tests/test_codex_native_app_server.py`.
Runner-owned launch coverage is in
`tests/runner/test_app_sessions_native_terminals_runtime.py`.
Use a temporary `OMNIGENT_CONFIG_HOME` for runner launch tests so local harness
command overrides do not alter the captured terminal arguments.
