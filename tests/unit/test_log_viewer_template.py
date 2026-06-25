from app.core.log_viewer_template import LOG_VIEWER_HTML


def test_template_has_parameterised_slots():
    # The deploy-specific hardcoded URL must be gone; slots must exist.
    assert "{trace_url}" in LOG_VIEWER_HTML
    assert "{terminal_statuses_json}" in LOG_VIEWER_HTML
    assert "{title}" in LOG_VIEWER_HTML
    assert "{heading}" in LOG_VIEWER_HTML
    assert "/api/v1/deploy/jobs/{job_id}/trace/ui" not in LOG_VIEWER_HTML


def test_template_formats_for_command_viewer():
    html = LOG_VIEWER_HTML.format(
        title="Command Log | c1",
        heading="Command: c1",
        trace_url="/api/v1/command/execution/c1/trace/ui",
        terminal_statuses_json="['success','failed','killed']",
        meta_html="<div>Command ID c1</div>",
    )
    assert "/api/v1/command/execution/c1/trace/ui" in html
    assert "killed" in html


def test_too_large_panel_renders_log_location():
    # On the hard-cap bail-out, the panel must tell the user where to read the
    # full log (host/port/user/path) — ideally as a copy-pasteable ssh + tail.
    # The template's JS must reference the trace response's location fields.
    for field in ("log_host", "log_port", "log_user", "log_file_path"):
        assert field in LOG_VIEWER_HTML, f"template must surface {field}"
    # And it should compose them into a usable command hint.
    assert "ssh" in LOG_VIEWER_HTML.lower()
