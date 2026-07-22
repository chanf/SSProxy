"""Regression tests for the unified connection telemetry collector."""

from pathlib import Path


def _install_jsonfilter_stub(bin_dir):
    jsonfilter = Path(bin_dir) / "jsonfilter"
    jsonfilter.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "  *'.id'*) printf 'c1\\nc2\\n' ;;\n"
        "  *'.sourceIP'*) printf '192.0.2.1\\n192.0.2.2\\n' ;;\n"
        "  *'.host'*) printf 'one.example\\ntwo.example\\n' ;;\n"
        "  *'.destinationIP'*) printf '203.0.113.1\\n203.0.113.2\\n' ;;\n"
        "  *'.chains[0]'*) printf 'PROXY\\nDIRECT\\n' ;;\n"
        "  *'.rule'*) printf 'Match\\nDomain\\n' ;;\n"
        "  *'.upload'*) printf '10\\n20\\n' ;;\n"
        "  *'.download'*) printf '30\\n40\\n' ;;\n"
        "  *'.start'*) printf 's1\\ns2\\n' ;;\n"
        "esac\n"
    )
    jsonfilter.chmod(0o755)


def test_flatten_connections_batches_fields_without_per_row_sed(run_fn, bin_dir):
    _install_jsonfilter_stub(bin_dir)

    result = run_fn("flatten_connections '{}' ")

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "c1|192.0.2.1||one.example|203.0.113.1|PROXY|Match|10|30|s1",
        "c2|192.0.2.2||two.example|203.0.113.2|DIRECT|Domain|20|40|s2",
    ]


def test_collector_reuses_one_snapshot_and_init_has_one_telemetry_loop(src_files):
    helper = src_files["root/usr/share/mihomo/helper.sh"]
    init = src_files["root/etc/init.d/mihomo"]
    flatten = helper.split("flatten_connections() {", 1)[1].split("get_connections() {", 1)[0]
    loop = helper.split("collect_loop() {", 1)[1].split("# Proxy traffic stats", 1)[0]

    assert 'sed -n "${n}p"' not in flatten
    assert 'collect_traffic "$raw"' in loop
    assert 'collect_connections "$raw"' in loop
    assert init.count("/usr/share/mihomo/helper.sh collect_loop") == 1
    assert "/usr/share/mihomo/helper.sh traffic_loop" not in init
