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
