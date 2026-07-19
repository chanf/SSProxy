"""Table-driven tests for the pure :func:`build_ipk._compute_file_mode` helper."""
import pytest

import build_ipk


@pytest.mark.parametrize(
    "rel_path, basename, is_control, expected",
    [
        # control tarball: only maintainer scripts are executable
        ("CONTROL/postinst", "postinst", True, 0o755),
        ("CONTROL/postrm", "postrm", True, 0o755),
        ("CONTROL/preinst", "preinst", True, 0o755),
        ("CONTROL/prerm", "prerm", True, 0o755),
        ("CONTROL/control", "control", True, 0o644),
        ("CONTROL/conffiles", "conffiles", True, 0o644),
        # data tarball: init.d scripts + helper.sh are executable
        ("etc/init.d/mihomo", "mihomo", False, 0o755),
        ("usr/share/mihomo/helper.sh", "helper.sh", False, 0o755),
        ("etc/config/mihomo", "mihomo", False, 0o644),
        ("www/luci-static/resources/view/mihomo/dashboard.js", "dashboard.js", False, 0o644),
        ("usr/share/luci/menu.d/luci-app-ssproxy.json", "luci-app-ssproxy.json", False, 0o644),
        ("usr/share/rpcd/acl.d/luci-app-ssproxy.json", "luci-app-ssproxy.json", False, 0o644),
    ],
)
def test_compute_file_mode(builder, rel_path, basename, is_control, expected):
    assert builder._compute_file_mode(rel_path, basename, is_control) == expected
