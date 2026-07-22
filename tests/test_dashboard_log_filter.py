"""Regression coverage for the dashboard system-log level filter."""


DASHBOARD_PATH = "root/www/luci-static/resources/view/mihomo/dashboard.js"


def test_system_log_level_filter_controls_are_present(src_files):
    dashboard = src_files[DASHBOARD_PATH]

    assert "'id': 'system-log-level-filter'" in dashboard
    for value in ("all", "debug", "info", "warning", "error", "fatal", "other"):
        assert f"'value': '{value}'" in dashboard


def test_system_log_level_filter_handles_core_and_syslog_formats(src_files):
    dashboard = src_files[DASHBOARD_PATH]

    assert "function detectLogLevel(line)" in dashboard
    assert "level\\s*=\\s*" in dashboard
    assert "(?:daemon|user|kern|local[0-7])" in dashboard
    assert "log_level_filter !== 'all' && level !== log_level_filter" in dashboard
    assert "renderLogs(log_text)" in dashboard
    assert "暂无匹配该级别的日志" in dashboard
