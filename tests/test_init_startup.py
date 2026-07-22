"""Regression tests for procd startup ordering and interception safety."""


def test_controller_wait_runs_in_separate_procd_instance(src_files):
    init = src_files["root/etc/init.d/mihomo"]
    apply_network = init.split("apply_network() {", 1)[1].split("start_service() {", 1)[0]
    start_service = init.split("start_service() {", 1)[1].split("stop_service() {", 1)[0]

    assert "wait_controller 30" in apply_network
    assert "wait_controller 30" not in start_service
    assert "procd_open_instance network_setup" in start_service
    assert "procd_set_param command /etc/init.d/mihomo apply_network" in start_service


def test_config_is_validated_before_core_registration(src_files):
    init = src_files["root/etc/init.d/mihomo"]
    start_service = init.split("start_service() {", 1)[1].split("stop_service() {", 1)[0]

    validation = '"$core_path" -t -d "$work_dir" -f /tmp/mihomo_run.yaml'
    assert validation in start_service
    assert start_service.index(validation) < start_service.index("procd_open_instance")
