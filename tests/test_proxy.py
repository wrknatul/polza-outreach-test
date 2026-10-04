"""POLZA_SOCKS: an optional SOCKS proxy for the companies' sites only.

The page requests of personalize.py (httpx) and of the tools (curl) go through it; the LLM
backend never does. Nothing is fetched here: httpx.Client and subprocess.run are replaced.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

import personalize as P

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import build_base_common as C
import enrich_base as E

TUNNEL = "127.0.0.1:1080"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in (P.PROXY_ENV, *P.AMBIENT_PROXY_VARS):
        monkeypatch.delenv(name, raising=False)


# --- personalize.py: httpx ------------------------------------------------------------ #

def test_proxy_url_comes_from_polza_socks(monkeypatch):
    assert P.socks_proxy_url() == ""                                  # the option is off by default
    monkeypatch.setenv(P.PROXY_ENV, TUNNEL)
    assert P.socks_proxy_url() == "socks5h://127.0.0.1:1080"          # names are resolved by the proxy
    assert P.socks_proxy_url("socks5://10.0.0.1:9050") == "socks5://10.0.0.1:9050"
    assert P.socks_proxy_url("") == "" and P.socks_proxy_url("  ") == ""


def test_fetcher_passes_the_proxy_to_every_client(monkeypatch):
    seen = []
    real_client = httpx.Client

    def fake_client(**kwargs):
        seen.append(kwargs)
        return real_client()

    monkeypatch.setattr(P.httpx, "Client", fake_client)

    plain = P.Fetcher(cache_dir=None)
    assert plain.proxy == "" and plain.client is not None and seen[-1]["proxy"] is None
    plain.close()

    monkeypatch.setenv(P.PROXY_ENV, TUNNEL)
    tunnelled = P.Fetcher(cache_dir=None)
    assert tunnelled.client is not None and seen[-1]["proxy"] == "socks5h://127.0.0.1:1080"
    tunnelled.close()

    explicit_off = P.Fetcher(cache_dir=None, proxy="")                # an explicit '' wins over the environment
    assert explicit_off.proxy == ""
    explicit_off.close()


def test_fetcher_builds_a_real_socks_client(monkeypatch):
    pytest.importorskip("socksio")                                    # pip install "httpx[socks]"
    monkeypatch.setenv(P.PROXY_ENV, TUNNEL)
    fetcher = P.Fetcher(cache_dir=None)
    try:
        assert isinstance(fetcher.client, httpx.Client)               # no connection is opened here
    finally:
        fetcher.close()


# --- personalize.py: the LLM never sees the proxy ------------------------------------- #

def test_llm_environment_drops_the_proxy_only_when_the_option_is_set():
    ambient = {"PATH": "/usr/bin", "HTTPS_PROXY": "http://corp:3128"}
    assert P.llm_subprocess_env(ambient) == ambient                   # option off: nothing changes

    env = P.llm_subprocess_env({**ambient, P.PROXY_ENV: TUNNEL, "ALL_PROXY": f"socks5h://{TUNNEL}",
                                "all_proxy": f"socks5h://{TUNNEL}"})
    assert env == {"PATH": "/usr/bin"}
    assert P.llm_subprocess_env({P.PROXY_ENV: "", "ALL_PROXY": "x"}) == {"ALL_PROXY": "x"}


def test_claude_cli_runs_without_the_sites_proxy(monkeypatch):
    calls = {}

    def fake_run(cmd, **kw):
        calls["env"] = kw["env"]
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"is_error": False, "result": "{}"}), stderr="")

    monkeypatch.setenv(P.PROXY_ENV, TUNNEL)
    monkeypatch.setenv("ALL_PROXY", f"socks5h://{TUNNEL}")
    monkeypatch.setattr(P.shutil, "which", lambda name: "/usr/local/bin/claude")
    monkeypatch.setattr(P.subprocess, "run", fake_run)

    P.ClaudeCLIBackend().complete("SYSTEM", "USER")

    assert P.PROXY_ENV not in calls["env"] and "ALL_PROXY" not in calls["env"]
    assert calls["env"]["MAX_THINKING_TOKENS"] == "0"
    assert P.os.environ[P.PROXY_ENV] == TUNNEL                        # the process itself keeps the setting


# --- tools: curl ---------------------------------------------------------------------- #

def test_curl_goes_through_the_proxy_and_lets_it_resolve_names():
    assert C.proxy_args({}) == []
    assert C.proxy_args({C.PROXY_ENV: TUNNEL}) == ["--socks5-hostname", TUNNEL]
    assert C.proxy_args({C.PROXY_ENV: "socks5h://10.0.0.1:9050"}) == ["--proxy", "socks5h://10.0.0.1:9050"]

    direct = C.curl_command(attempt=1, env={})
    assert "--socks5-hostname" not in direct and "--doh-url" in direct     # flaky local DNS -> DoH on a retry
    assert "--doh-url" not in C.curl_command(attempt=0, env={})
    for attempt in (0, 2):                                                  # the proxy resolves the name
        proxied = C.curl_command(attempt=attempt, env={C.PROXY_ENV: TUNNEL})
        assert proxied[proxied.index("--socks5-hostname") + 1] == TUNNEL and "--doh-url" not in proxied
    assert "-L" not in proxied                                              # redirects are followed by hand


def test_every_second_attempt_resolves_the_name_itself_and_still_goes_through_the_proxy():
    # the proxy's resolver does not know every name (aitis.pro): DoH gives the address, the proxy gets an IP
    retry = C.curl_command(attempt=1, env={C.PROXY_ENV: TUNNEL})
    assert retry[retry.index("--socks5") + 1] == TUNNEL and "--socks5-hostname" not in retry
    assert retry[retry.index("--doh-url") + 1] == C.DOH_URL
    by_url = C.curl_command(attempt=3, env={C.PROXY_ENV: "socks5h://10.0.0.1:9050"})
    assert by_url[by_url.index("--proxy") + 1] == "socks5://10.0.0.1:9050" and "--doh-url" in by_url
    other = C.curl_command(attempt=1, env={C.PROXY_ENV: "http://corp:3128"})  # not SOCKS5: left as it is
    assert other[other.index("--proxy") + 1] == "http://corp:3128" and "--doh-url" not in other


@pytest.mark.parametrize("module, call", [(C, lambda: C.fetch("https://acme-stanki.ru/")),
                                          (E, lambda: E.fetch_page("https://acme-stanki.ru/"))])
def test_tools_fetch_pages_through_the_proxy(module, call, monkeypatch):
    commands = []

    def fake_run(cmd, **kw):
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, b"<html>" + b"x" * 2000 + b"</html>\n200", b"")

    monkeypatch.setenv(C.PROXY_ENV, TUNNEL)
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(C, "_cache", {})
    monkeypatch.setattr(C, "_robots", {})
    monkeypatch.setattr(E, "_pages", {})

    assert "xxx" in call()
    # robots.txt of the host is asked first (an HTML page instead of it closes nothing), then the page itself
    assert [cmd[-1] for cmd in commands] == ["https://acme-stanki.ru/robots.txt", "https://acme-stanki.ru/"]
    for cmd in commands:
        assert cmd[0] == "curl" and cmd[cmd.index("--socks5-hostname") + 1] == TUNNEL
