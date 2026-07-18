"""Tests for the internal ``resolve_proxy_name`` helper.

Fuzz-tolerant matching of a requested node name against the controller's
/proxies list (driven by a stubbed curl). Quotes, CR and surrounding spaces in
the requested name are normalized before comparison.
"""
PROXIES = '{"proxies":[{"name":"节点 A"},{"name":"节点B"},{"name":"PROXY"}]}'


def test_exact_match(run_fn):
    res = run_fn('resolve_proxy_name "节点B"', env={"CURL_RESPONSE": PROXIES})
    assert res.stdout.strip() == "节点B"


def test_spaces_stripped(run_fn):
    res = run_fn('resolve_proxy_name " 节点B "', env={"CURL_RESPONSE": PROXIES})
    assert res.stdout.strip() == "节点B"


def test_surrounding_quotes_stripped(run_fn):
    # The requested name arrives double-quoted; resolve_proxy_name unwraps it.
    res = run_fn("""resolve_proxy_name '"节点B"'""", env={"CURL_RESPONSE": PROXIES})
    assert res.stdout.strip() == "节点B"


def test_no_match_is_silent(run_fn):
    res = run_fn('resolve_proxy_name "does-not-exist"', env={"CURL_RESPONSE": PROXIES})
    assert res.stdout.strip() == ""
