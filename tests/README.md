# Tests

Unit + shell-integration tests for `build_ipk.py` (the Python builder) and the
embedded `helper.sh` backend.

## Run

```bash
# one-time: create the venv and install pytest
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt

# run everything (104 tests)
.venv/bin/python -m pytest tests/ -v
```

## Layout

```
tests/
  conftest.py                  # puts repo root on sys.path; src_files fixtures
  test_create_source_tree.py   # file-write + version/placeholder subst + chmod
  test_make_tar_gz.py          # reproducible tarball: ownership/mtime/./prefix/modes
  test_write_tar_gz_outer_archive.py
  test_increment_version.py    # version bump (revision/dotted/ValueError/no-match)
  test_version_bump.py         # table-driven _bump_version_string
  test_file_mode.py            # table-driven _compute_file_mode
  test_main_e2e.py             # full build in an isolated temp workspace
  test_reproducibility.py      # byte-identical output across runs (gzip header fix)
  fixtures/
    stubs/                     # fake uci/logger/sed/uname/opkg/curl for shell tests
  shell/
    conftest.py                # writes helper.sh to tmp; run/run_fn/uci_env fixtures
    test_get_proxies.py        # awk YAML parser: 5 error paths + block/flow/CRLF
    test_prepare_config.py     # controlled config merge + rule injection
    test_urlencode.py
    test_access_rules.py       # emit/get/add/del/import
    test_get_schedule.py
    test_resolve_proxy_name.py
    test_get_arch.py
```

## How shell tests work

`helper.sh` is driven as a black box from Python via `subprocess`. The exact
on-disk string the router runs is pulled from `build_ipk.src_files`, so escape
quirks (`\t`→Tab, `\\"`→`\"`) are handled identically to production. A throwaway
`bin/` of fake `uci`/`logger`/`sed`/`uname`/`opkg`/`curl` is prepended to `PATH`,
and UCI state is fed in via data files (`uci_env` fixture).

Internal helpers not exposed as subcommands (`urlencode`, `resolve_proxy_name`,
`emit_access_rules_yaml`) are tested by sourcing a dispatcher-stripped copy of
the library (`run_fn` fixture).

> Note: `prepare_config` uses GNU-style `sed -i`; on macOS the `sed` stub
> translates it to BSD `sed` so the test runs locally. On the router's busybox
> the real `sed -i` is used directly.
