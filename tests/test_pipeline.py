"""Fetching + row processing + CLI, with the network mocked by respx."""

from __future__ import annotations

import csv
import gzip
import threading
import time

import httpx
import pytest
from conftest import (ABOUT_HTML, HOME_HTML, NEWS_ANSWER, NEWS_HTML, SITE, VALID_ANSWER, FakeBackend, make_ctx,
                      no_robots_txt)

import personalize as P


def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def write_csv(path, rows, header=("company", "email", "site")):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


# --- fetching ---------------------------------------------------------------- #

def test_unreachable_site_gives_no_data(fetcher, respx_mock):
    respx_mock.route().mock(side_effect=httpx.ConnectError("connection refused"))
    backend = FakeBackend(VALID_ANSWER)
    info = P.RowInfo(0, 2, "Акме Станки", "acme-stanki.ru", "sales@acme-stanki.ru")
    out = P.process_row(info, make_ctx([info], fetcher, backend))
    assert out[P.COL_PERSONALIZATION] == P.NO_DATA
    assert out[P.COL_SOURCE] == ""
    assert "недоступен" in out[P.COL_COMMENT]
    assert out[P.COL_CHECK].startswith(P.STATUS_REVIEW)
    assert backend.prompts == []  # no LLM call without fetched text


def test_antibot_403_is_not_retried_on_other_variants(fetcher, respx_mock):
    route = respx_mock.route().mock(return_value=httpx.Response(403, html="<h1>Just a moment...</h1>"))
    res, errors = P.fetch_homepage(fetcher, "jimmytool.com")
    assert not res.ok and "403" in res.error
    # robots.txt answers 403 as well (no rules: nothing is closed), then the page is asked once
    assert [str(call.request.url) for call in route.calls] == ["https://jimmytool.com/robots.txt",
                                                               "https://jimmytool.com/"]


def test_http_fallback_when_https_fails(fetcher, respx_mock):
    respx_mock.route(scheme="https", host="fengyi.test").mock(side_effect=httpx.ConnectError("tls"))
    respx_mock.route(scheme="https", host="www.fengyi.test").mock(side_effect=httpx.ConnectError("tls"))
    respx_mock.route(scheme="http", host="fengyi.test").mock(side_effect=httpx.ConnectError("refused"))
    no_robots_txt(respx_mock, "http://www.fengyi.test")
    respx_mock.get("http://www.fengyi.test/").mock(return_value=httpx.Response(200, html=HOME_HTML))
    res, _ = P.fetch_homepage(fetcher, "fengyi.test")
    assert res.ok and res.url == "http://www.fengyi.test/"


def test_web_server_placeholder_is_not_the_company_site(fetcher, respx_mock):
    # label4u.ru: https timed out, plain http answered with the nginx welcome page.
    nginx = ("<html><head><title>Welcome to nginx!</title></head><body><h1>Welcome to nginx!</h1><p>If you see "
             "this page, the nginx web server is successfully installed and working.</p></body></html>")
    respx_mock.route(scheme="https", host="label.test").mock(side_effect=httpx.ConnectTimeout("timed out"))
    respx_mock.route(scheme="https", host="www.label.test").mock(side_effect=httpx.ConnectTimeout("timed out"))
    no_robots_txt(respx_mock, "http://label.test", "http://www.label.test")
    respx_mock.get("http://label.test/").mock(return_value=httpx.Response(200, html=nginx))
    respx_mock.get("http://www.label.test/").mock(return_value=httpx.Response(200, html=nginx))
    res, errors = P.fetch_homepage(fetcher, "label.test")
    assert not res.ok and "заглушка" in res.error
    assert any("заглушка" in e for e in errors)


def test_retry_with_backoff_on_503(fetcher, respx_mock):
    no_robots_txt(respx_mock, SITE)
    route = respx_mock.get(f"{SITE}/").mock(side_effect=[httpx.Response(503), httpx.Response(200, html=HOME_HTML)])
    res = fetcher.get(f"{SITE}/")
    assert res.ok and route.call_count == 2


def test_cache_avoids_second_request(fetcher, respx_mock):
    no_robots_txt(respx_mock, SITE)
    route = respx_mock.get(f"{SITE}/about/").mock(return_value=httpx.Response(200, html=ABOUT_HTML))
    first, second = fetcher.get(f"{SITE}/about/"), fetcher.get(f"{SITE}/about/")
    assert first.ok and second.ok and second.from_cache
    assert route.call_count == 1


def test_robots_txt_is_respected(tmp_path, respx_mock):
    respx_mock.get(f"{SITE}/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /private/\n"))
    respx_mock.route().mock(return_value=httpx.Response(200, html=HOME_HTML))
    f = P.Fetcher(cache_dir=None, delay=0, retries=0, backoff=0)
    try:
        assert f.get(f"{SITE}/").ok
        blocked = f.get(f"{SITE}/private/prices/")
        assert not blocked.ok and "robots" in blocked.error
    finally:
        f.close()


# --- robots.txt: redirects, a file that cannot be read, the cache, no switch -------------------- #

OPEN_ROBOTS = "User-agent: *\nDisallow: /private/\n"
WWW = "https://www.acme-stanki.ru"


@pytest.fixture
def plain():
    """A fetcher without a cache and without retries: every request reaches the mock."""
    f = P.Fetcher(cache_dir=None, delay=0, retries=0, backoff=0)
    yield f
    f.close()


def cached_fetcher(tmp_path, **kw):
    return P.Fetcher(cache_dir=tmp_path / "c", delay=0, **{"retries": 0, "backoff": 0, **kw})


def asked(respx_mock) -> list[str]:
    return [str(call.request.url) for call in respx_mock.calls]


def test_redirect_into_a_path_closed_by_robots_is_not_followed(plain, respx_mock):
    respx_mock.get(f"{SITE}/robots.txt").mock(return_value=httpx.Response(200, text=OPEN_ROBOTS))
    respx_mock.get(f"{SITE}/team").mock(return_value=httpx.Response(301, headers={"location": "/private/team/"}))
    closed = respx_mock.get(f"{SITE}/private/team/").mock(return_value=httpx.Response(200, html=ABOUT_HTML))
    respx_mock.get(f"{SITE}/old").mock(return_value=httpx.Response(302, headers={"location": f"{SITE}/about/#team"}))
    about = respx_mock.get(f"{SITE}/about/").mock(return_value=httpx.Response(200, html=ABOUT_HTML))
    res = plain.get(f"{SITE}/team")
    assert not res.ok and res.html == ""
    assert res.error == f"запрещено robots.txt (адрес после редиректа: {SITE}/private/team/)"
    assert closed.call_count == 0  # the closed address is never requested
    moved = plain.get(f"{SITE}/old")  # an open target is followed, and the page is known by its final address
    assert moved.ok and moved.final_url == f"{SITE}/about/" and about.call_count == 1


def test_robots_of_the_host_a_redirect_leads_to_is_asked_before_the_hop(plain, respx_mock):
    no_robots_txt(respx_mock, SITE)
    respx_mock.get(f"{SITE}/").mock(return_value=httpx.Response(301, headers={"location": f"{WWW}/"}))
    rules = {"text": "User-agent: *\nDisallow: /\n"}
    respx_mock.get(f"{WWW}/robots.txt").mock(side_effect=lambda request: httpx.Response(200, text=rules["text"]))
    www_home = respx_mock.get(f"{WWW}/").mock(return_value=httpx.Response(200, html=HOME_HTML))
    res, errors = P.fetch_homepage(plain, "acme-stanki.ru")
    assert not res.ok and res.error.startswith("запрещено robots.txt") and www_home.call_count == 0
    # the site said no: its other addresses (http, www) are not tried either
    assert asked(respx_mock) == [f"{SITE}/robots.txt", f"{SITE}/", f"{WWW}/robots.txt"]

    rules["text"] = OPEN_ROBOTS
    again = P.Fetcher(cache_dir=None, delay=0, retries=0, backoff=0)
    try:
        res, _ = P.fetch_homepage(again, "acme-stanki.ru")
    finally:
        again.close()
    assert res.ok and res.final_url == f"{WWW}/"
    assert asked(respx_mock)[3:] == [f"{SITE}/robots.txt", f"{SITE}/", f"{WWW}/robots.txt", f"{WWW}/"]


def test_redirect_chain_is_cut_and_a_redirect_to_nowhere_is_not_followed(plain, respx_mock):
    no_robots_txt(respx_mock, SITE)

    def next_hop(request):
        return httpx.Response(302, headers={"location": f"/hop/{int(request.url.path.rsplit('/', 1)[1]) + 1}"})

    hops = respx_mock.get(url__regex=rf"{SITE}/hop/\d+").mock(side_effect=next_hop)
    res = plain.get(f"{SITE}/hop/0")
    assert not res.ok and res.error == f"больше {P.MAX_REDIRECTS} редиректов подряд"
    assert hops.call_count == P.MAX_REDIRECTS + 1  # the address itself and five hops, not one more
    files = respx_mock.get(f"{SITE}/price").mock(
        return_value=httpx.Response(302, headers={"location": "ftp://files.acme-stanki.ru/price.zip"}))
    assert plain.get(f"{SITE}/price").error == "редирект на адрес, который нельзя открыть" and files.call_count == 1
    # an address httpx itself cannot build is an error of the row, not a crash of the run
    respx_mock.get(f"{SITE}/mail").mock(return_value=httpx.Response(302, headers={"location": "mailto:a@acme.ru"}))
    assert plain.get(f"{SITE}/mail").error.startswith("InvalidURL")


def test_http_client_does_not_follow_redirects_by_itself():
    f = P.Fetcher(cache_dir=None, proxy="")
    try:
        assert f.client.follow_redirects is False  # every hop goes through Fetcher._fetch_network
    finally:
        f.close()


@pytest.mark.parametrize("answer, why", [
    (500, "robots.txt не получен (HTTP 500)"),
    (503, "robots.txt не получен (HTTP 503)"),
    (429, "robots.txt не получен (HTTP 429)"),
    (httpx.ReadTimeout("timed out"), "robots.txt не получен (ReadTimeout"),
    (httpx.RemoteProtocolError("peer closed the connection"), "robots.txt не получен (RemoteProtocolError"),
    (httpx.ConnectError("connection refused"), "нет соединения (ConnectError"),
])
def test_robots_txt_that_cannot_be_read_closes_the_host(plain, respx_mock, answer, why):
    def robots_txt(request):
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(answer, text="try later")

    robots = respx_mock.get(f"{SITE}/robots.txt").mock(side_effect=robots_txt)
    page = respx_mock.get(f"{SITE}/about/").mock(return_value=httpx.Response(200, html=ABOUT_HTML))
    res = plain.get(f"{SITE}/about/")
    assert not res.ok and res.html == "" and res.error.startswith(why)
    assert page.call_count == 0  # the rules are unknown: nothing is requested
    assert not plain.get(f"{SITE}/news/").ok and robots.call_count == 1  # closed for the run, the file is asked once


@pytest.mark.parametrize("status, body", [
    (404, "not found"), (410, "gone"), (403, "forbidden"), (200, ""),
    (200, "<!DOCTYPE html><html><body>Страница не найдена</body></html>"),
])
def test_site_without_robots_txt_closes_nothing(plain, respx_mock, status, body):
    respx_mock.get(f"{SITE}/robots.txt").mock(return_value=httpx.Response(status, text=body))
    respx_mock.get(f"{SITE}/about/").mock(return_value=httpx.Response(200, html=ABOUT_HTML))
    assert plain.get(f"{SITE}/about/").ok


def test_robots_txt_gets_one_more_attempt_before_the_host_is_closed(respx_mock):
    robots = respx_mock.get(f"{SITE}/robots.txt").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, text=OPEN_ROBOTS)])
    down = respx_mock.get(f"{WWW}/robots.txt").mock(return_value=httpx.Response(503))
    respx_mock.get(f"{SITE}/about/").mock(return_value=httpx.Response(200, html=ABOUT_HTML))
    f = P.Fetcher(cache_dir=None, delay=0, retries=2, backoff=0)
    try:
        assert f.get(f"{SITE}/about/").ok and not f.get(f"{SITE}/private/prices/").ok
        assert robots.call_count == 2
        assert f.get(f"{WWW}/about/").error.startswith("robots.txt не получен (HTTP 503)")
        assert down.call_count == 2  # one retry whatever --retries says: a silent site is not hammered
    finally:
        f.close()


def test_robots_txt_answering_5xx_stops_the_row_and_a_silent_one_lets_other_addresses_be_tried(respx_mock):
    respx_mock.get(f"{SITE}/robots.txt").mock(return_value=httpx.Response(503))
    f = P.Fetcher(cache_dir=None, delay=0, retries=0, backoff=0)
    try:
        res, errors = P.fetch_homepage(f, "acme-stanki.ru")
    finally:
        f.close()
    assert not res.ok and "robots.txt не получен (HTTP 503)" in errors[0]
    assert asked(respx_mock) == [f"{SITE}/robots.txt"]  # the site answered: www and http are not hammered

    respx_mock.get("https://slow.test/robots.txt").mock(side_effect=httpx.ReadTimeout("timed out"))
    no_robots_txt(respx_mock, "https://www.slow.test")
    respx_mock.get("https://www.slow.test/").mock(return_value=httpx.Response(200, html=HOME_HTML))
    f = P.Fetcher(cache_dir=None, delay=0, retries=0, backoff=0)
    try:
        res, errors = P.fetch_homepage(f, "slow.test")
    finally:
        f.close()
    assert res.ok and res.url == "https://www.slow.test/"
    assert "https://slow.test/" not in asked(respx_mock)  # the page of the silent host itself was never requested


def test_row_of_a_site_whose_robots_txt_is_down_gets_no_data_and_is_retried(tmp_path, respx_mock):
    state = {"robots": 503}

    def site(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(state["robots"])
        return _acme_handler({})(request)

    route = respx_mock.route().mock(side_effect=site)
    src, dst = tmp_path / "in.csv", tmp_path / "out.csv"
    write_csv(src, [("Акме Станки", "sales@acme-stanki.ru", "acme-stanki.ru")])
    argv = [str(src), "-o", str(dst), "--cache-dir", str(tmp_path / "c"), "--delay", "0", "--retries", "0",
            "--backend", "none"]
    P.run(argv)
    out = read_csv(dst)[0]
    assert out[P.COL_PERSONALIZATION] == P.NO_DATA and out[P.COL_SOURCE] == ""
    assert "сайт недоступен" in out[P.COL_COMMENT] and "robots.txt не получен (HTTP 503)" in out[P.COL_COMMENT]
    assert [str(call.request.url) for call in route.calls] == [f"{SITE}/robots.txt"]  # not one page was requested

    state["robots"] = 404  # the next run: the file is asked again, the row is done
    P.run(argv)
    out = read_csv(dst)[0]
    assert "Запустили участок лазерной резки" in out[P.COL_PERSONALIZATION] and out[P.COL_CHECK] == P.STATUS_OK


def test_robots_txt_is_read_as_utf8_whatever_charset_the_server_announces(plain, respx_mock):
    # a UTF-8 file with a byte-order mark, served by a host whose default charset is windows-1251
    body = "\ufeffUser-agent: *\nDisallow: /контакты\n".encode()
    respx_mock.get(f"{SITE}/robots.txt").mock(return_value=httpx.Response(
        200, content=body, headers={"content-type": "text/plain; charset=windows-1251"}))
    respx_mock.get(f"{SITE}/about/").mock(return_value=httpx.Response(200, html=ABOUT_HTML))
    assert plain.get(f"{SITE}/about/").ok
    for path in ("/контакты/", "/%D0%BA%D0%BE%D0%BD%D1%82%D0%B0%D0%BA%D1%82%D1%8B/"):
        assert plain.get(f"{SITE}{path}").error == "запрещено robots.txt"
    assert asked(respx_mock) == [f"{SITE}/robots.txt", f"{SITE}/about/"]


def test_cached_page_is_checked_against_the_rules_of_today(tmp_path, respx_mock, monkeypatch):
    rules = {"text": OPEN_ROBOTS}
    robots = respx_mock.get(f"{SITE}/robots.txt").mock(
        side_effect=lambda request: httpx.Response(200, text=rules["text"]))
    about = respx_mock.get(f"{SITE}/about/").mock(return_value=httpx.Response(200, html=ABOUT_HTML))

    def one_run():
        f = cached_fetcher(tmp_path)
        try:
            return f.get(f"{SITE}/about/")
        finally:
            f.close()

    assert one_run().ok
    again = one_run()  # the same day: the page and the rules both come from the cache
    assert again.ok and again.from_cache and (robots.call_count, about.call_count) == (1, 1)

    rules["text"] = "User-agent: *\nDisallow: /about/\n"  # the site has closed the section
    monkeypatch.setattr(P, "ROBOTS_TTL", -1)  # ... and a day has passed: the file is asked again
    closed = one_run()
    assert not closed.ok and closed.html == "" and closed.error == "запрещено robots.txt"
    assert (robots.call_count, about.call_count) == (2, 1)


def test_cached_page_reached_through_a_redirect_is_checked_at_both_addresses(tmp_path, respx_mock, monkeypatch):
    rules = {"text": OPEN_ROBOTS}
    respx_mock.get(f"{SITE}/robots.txt").mock(side_effect=lambda request: httpx.Response(200, text=rules["text"]))
    respx_mock.get(f"{SITE}/team").mock(return_value=httpx.Response(301, headers={"location": "/company/team/"}))
    team = respx_mock.get(f"{SITE}/company/team/").mock(return_value=httpx.Response(200, html=ABOUT_HTML))
    first = cached_fetcher(tmp_path)
    res = first.get(f"{SITE}/team")
    first.close()
    assert res.ok and res.final_url == f"{SITE}/company/team/"

    rules["text"] = "User-agent: *\nDisallow: /company/\n"  # /team itself stays open, its target does not
    monkeypatch.setattr(P, "ROBOTS_TTL", -1)
    second = cached_fetcher(tmp_path)
    res = second.get(f"{SITE}/team")
    second.close()
    assert not res.ok and res.html == "" and res.error == "запрещено robots.txt" and team.call_count == 1


def test_cached_page_is_not_served_while_robots_txt_cannot_be_read(tmp_path, respx_mock, monkeypatch):
    state = {"robots": 404}
    respx_mock.get(f"{SITE}/robots.txt").mock(side_effect=lambda request: httpx.Response(state["robots"]))
    about = respx_mock.get(f"{SITE}/about/").mock(return_value=httpx.Response(200, html=ABOUT_HTML))
    first = cached_fetcher(tmp_path)
    assert first.get(f"{SITE}/about/").ok
    first.close()
    state["robots"] = 503
    monkeypatch.setattr(P, "ROBOTS_TTL", -1)
    second = cached_fetcher(tmp_path)
    res = second.get(f"{SITE}/about/")
    second.close()
    assert not res.ok and res.html == "" and res.error.startswith("robots.txt не получен (HTTP 503)")
    assert about.call_count == 1


def test_cached_robots_txt_lives_a_day_and_an_unreadable_one_is_not_cached(tmp_path, respx_mock, monkeypatch):
    robots = respx_mock.get(f"{SITE}/robots.txt").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, text=OPEN_ROBOTS), httpx.Response(200, text=OPEN_ROBOTS)])
    respx_mock.get(f"{SITE}/news/").mock(return_value=httpx.Response(200, html=NEWS_HTML))

    def one_run():
        f = cached_fetcher(tmp_path)
        try:
            return f.get(f"{SITE}/news/")
        finally:
            f.close()

    assert not one_run().ok and robots.call_count == 1
    assert one_run().ok and robots.call_count == 2  # a 503 is not remembered: the next run asks the file again
    assert one_run().ok and robots.call_count == 2  # the rules are: for a day
    monkeypatch.setattr(P, "ROBOTS_TTL", -1)
    assert one_run().ok and robots.call_count == 3


def test_robots_txt_is_asked_once_when_threads_start_on_one_host(respx_mock):
    def slow_robots(request):
        time.sleep(0.05)
        return httpx.Response(200, text=OPEN_ROBOTS)

    robots = respx_mock.get(f"{SITE}/robots.txt").mock(side_effect=slow_robots)
    respx_mock.get(f"{SITE}/about/").mock(return_value=httpx.Response(200, html=ABOUT_HTML))
    f = P.Fetcher(cache_dir=None, delay=0, retries=0, backoff=0, proxy="")
    results = []
    try:
        threads = [threading.Thread(target=lambda: results.append(f.get(f"{SITE}/private/x").error)) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        f.close()
    assert results == ["запрещено robots.txt"] * 6 and robots.call_count == 1


def test_there_is_no_way_to_switch_robots_txt_off(capsys):
    with pytest.raises(SystemExit) as refused:
        P.parse_args(["in.csv", "--no-robots"])
    assert refused.value.code == 2 and "--no-robots" in capsys.readouterr().err  # an unknown argument
    with pytest.raises(SystemExit):
        P.parse_args(["--help"])
    help_text = capsys.readouterr().out
    assert "--no-robots" not in help_text and "robots.txt сайтов соблюдается всегда" in " ".join(help_text.split())
    with pytest.raises(TypeError):
        P.Fetcher(cache_dir=None, respect_robots=False)
    f = P.Fetcher(cache_dir=None, proxy="")
    try:
        with pytest.raises(TypeError):
            f.get(f"{SITE}/", check_robots=False)
    finally:
        f.close()


# --- robots.txt: the address that is checked is the address that is requested ------------------ #

@pytest.mark.parametrize("raw, requested", [
    ("https://x.ru/a/../private/x", "https://x.ru/private/x"),
    ("https://x.ru/a/%2E%2E/private/x", "https://x.ru/private/x"),      # a server reads «%2E» as a dot
    ("https://x.ru/a/%2e%2e/private/x", "https://x.ru/private/x"),
    ("https://x.ru/a/.%2E/private/x", "https://x.ru/private/x"),
    ("https://x.ru/a/%2e./private/x", "https://x.ru/private/x"),
    ("https://x.ru/./private/./x", "https://x.ru/private/x"),
    ("https://x.ru/%2E/private/x", "https://x.ru/private/x"),
    ("https://x.ru/a/b/../../private/x", "https://x.ru/private/x"),
    ("https://x.ru/../../private/x", "https://x.ru/private/x"),        # there is nothing above the root
    ("https://x.ru/a//../private/x", "https://x.ru/a/private/x"),      # «..» takes the empty segment (RFC 3986)
    ("https://x.ru/a/b/..", "https://x.ru/a/"),                        # the directory, with its slash
    ("https://x.ru/a/b/.", "https://x.ru/a/b/"),
    ("https://x.ru/a/..", "https://x.ru/"),
    ("https://x.ru/a/../private/x?next=/../y", "https://x.ru/private/x?next=/../y"),  # the query is not a path
    ("https://x.ru/private/x#/../open", "https://x.ru/private/x"),     # the fragment is never sent
    ("https://x.ru/page?", "https://x.ru/page"),                       # nor is a «?» with nothing after it
    ("https://x.ru/a/.\t./private/x", "https://x.ru/private/x"),       # a tab inside an address is dropped
    ("HTTPS://x.ru/a/../b", "https://x.ru/b"),
    # nothing to change
    ("https://x.ru", "https://x.ru"),
    ("https://x.ru/", "https://x.ru/"),
    ("https://x.ru/about/team.html?id=1&a=..", "https://x.ru/about/team.html?id=1&a=.."),
    ("https://x.ru/контакты/../о-нас/", "https://x.ru/о-нас/"),
    ("https://x.ru/v1.2/..hidden/...", "https://x.ru/v1.2/..hidden/..."),  # dots inside a name are a name
    ("https://x.ru/a/..%2Fprivate/x", "https://x.ru/a/..%2Fprivate/x"),    # «%2F» is not a slash: one segment
    ("https://x.ru//private/x", "https://x.ru//private/x"),                # «//» is compared as it is written
    # not an address a fetcher can request: returned as it is
    ("mailto:a@x.ru", "mailto:a@x.ru"), ("ftp://x.ru/a/../b", "ftp://x.ru/a/../b"), ("x.ru/a/../b", "x.ru/a/../b"),
    ("http://[bad/a/../b", "http://[bad/a/../b"), ("", ""),
])
def test_address_is_brought_to_the_spelling_that_is_requested(raw, requested):
    assert P.request_url(raw) == requested
    assert P.request_url(requested) == requested  # doing it twice changes nothing


@pytest.mark.parametrize("raw", [
    "https://x.ru/a/../private/x", "https://x.ru/a/%2E%2E/private/x", "https://x.ru/a/.%2e/private/x?q=1",
    "https://x.ru/a/./b/.", "https://x.ru/..", "https://x.ru/page?", "https://x.ru/контакты/../цены/",
    "https://x.ru/price list.xlsx", "https://x.ru/a[1]/x", "https://x.ru/a%zz/b", "https://x.ru/a\"b<c>/d",
    "https://x.ru/a/b#top", "https://x.ru", "https://x.ru/a/..;/b", "https://x.ru//a///b",
])
def test_http_client_sends_the_address_the_rules_were_asked_about(raw):
    # httpx rewrites an address when it builds the request; after request_url() there is nothing left to rewrite
    asked_about = P.request_url(raw)
    sent = httpx.Request("GET", asked_about).url
    assert P.robots_key(sent.raw_path.decode("ascii")) == P.robots_path(asked_about)
    assert "/../" not in sent.raw_path.decode("ascii") and "/./" not in sent.raw_path.decode("ascii")


def test_matcher_reads_an_address_the_way_it_is_requested():
    rules = P.RobotsRules(OPEN_ROBOTS, P.DEFAULT_UA)
    for path in ("/a/../private/x", "/a/%2E%2E/private/x", "/a/%2e./private/x", "/./private/x", "/private/./x",
                 "/open/../../private/x"):
        assert not rules.allows(f"https://x.ru{path}"), path
    assert rules.allows("https://x.ru/private/../open/x") and rules.allows("https://x.ru/a/..%2Fprivate/x")
    assert rules.verdict("https://x.ru/a/../private/x") == (False, "Disallow: /private/")
    assert rules.verdict("https://x.ru/open/") == (True, "")


DOTTED = ["/a/../private/x", "/a/%2E%2E/private/x", "/a/%2e%2e/private/x", "/a/.%2E/private/x", "/./private/x",
          "/open/../private/./x", "/private/y/../x"]


@pytest.mark.parametrize("dotted", DOTTED)
def test_dot_segments_do_not_lead_around_a_rule(plain, respx_mock, dotted):
    respx_mock.get(f"{SITE}/robots.txt").mock(return_value=httpx.Response(200, text=OPEN_ROBOTS))
    respx_mock.route().mock(return_value=httpx.Response(200, html=ABOUT_HTML))
    res = plain.get(SITE + dotted)
    assert not res.ok and res.html == "" and res.error == "запрещено robots.txt"
    assert res.url == f"{SITE}/private/x"  # the address that was checked is named
    assert asked(respx_mock) == [f"{SITE}/robots.txt"]  # ... and it was not requested


def test_address_with_dot_segments_is_requested_in_the_spelling_that_was_checked(plain, respx_mock):
    respx_mock.get(f"{SITE}/robots.txt").mock(return_value=httpx.Response(200, text=OPEN_ROBOTS))
    about = respx_mock.get(f"{SITE}/about/").mock(return_value=httpx.Response(200, html=ABOUT_HTML))
    for dotted in ("/private/../about/", "/a/%2E%2E/about/", "/about/."):
        res = plain.get(SITE + dotted)
        assert res.ok and res.url == res.final_url == f"{SITE}/about/"
    assert about.call_count == 3 and set(asked(respx_mock)) == {f"{SITE}/robots.txt", f"{SITE}/about/"}


@pytest.mark.parametrize("location", [
    f"{SITE}/a/../private/x", f"{SITE}/a/%2E%2E/private/x", "/a/../private/x", "../private/x", "/a/.%2e/private/x",
])
def test_redirect_through_dot_segments_into_a_closed_path_is_not_followed(plain, respx_mock, location):
    respx_mock.get(f"{SITE}/robots.txt").mock(return_value=httpx.Response(200, text=OPEN_ROBOTS))
    respx_mock.get(f"{SITE}/team/").mock(return_value=httpx.Response(302, headers={"location": location}))
    closed = respx_mock.get(url__regex=r".*private.*").mock(return_value=httpx.Response(200, html=ABOUT_HTML))
    res = plain.get(f"{SITE}/team/")
    assert not res.ok and res.error == f"запрещено robots.txt (адрес после редиректа: {SITE}/private/x)"
    assert closed.call_count == 0 and asked(respx_mock) == [f"{SITE}/robots.txt", f"{SITE}/team/"]


def test_redirect_through_dot_segments_to_an_open_page_is_followed_in_the_checked_spelling(plain, respx_mock):
    respx_mock.get(f"{SITE}/robots.txt").mock(return_value=httpx.Response(200, text=OPEN_ROBOTS))
    respx_mock.get(f"{SITE}/old").mock(
        return_value=httpx.Response(301, headers={"location": f"{SITE}/private/%2E%2E/about/#team"}))
    respx_mock.get(f"{SITE}/about/").mock(return_value=httpx.Response(200, html=ABOUT_HTML))
    res = plain.get(f"{SITE}/old")
    assert res.ok and res.final_url == f"{SITE}/about/"
    assert asked(respx_mock) == [f"{SITE}/robots.txt", f"{SITE}/old", f"{SITE}/about/"]


def test_link_with_dot_segments_on_a_page_does_not_lead_around_a_rule(tmp_path, respx_mock):
    home = HOME_HTML.replace('<a href="/about/">', f'<a href="{SITE}/x/../private/about/">') \
        .replace('<a href="/news/">', f'<a href="{SITE}/x/%2E%2E/private/news/">')
    respx_mock.get(f"{SITE}/robots.txt").mock(return_value=httpx.Response(200, text=OPEN_ROBOTS))
    respx_mock.get(f"{SITE}/").mock(return_value=httpx.Response(200, html=home))
    closed = respx_mock.get(url__regex=r".*private.*").mock(return_value=httpx.Response(200, html=ABOUT_HTML))
    respx_mock.route().mock(return_value=httpx.Response(404))
    f = cached_fetcher(tmp_path)
    try:
        pages, _ = P.collect_pages(f, "acme-stanki.ru", 4)
    finally:
        f.close()
    assert [p.url for p in pages] == [f"{SITE}/"] and closed.call_count == 0
    assert not any("private" in url or ".." in url or "%2E" in url for url in asked(respx_mock))


# --- robots.txt: one reader of the answer for every tool ----------------------------------------- #

RULES = b"User-agent: *\nDisallow: /private/\n"


@pytest.mark.parametrize("status, body, error, state", [
    # a file with at least one directive line is parsed, whatever the 2xx and whatever else is in the body
    (200, RULES, "", "rules"), (202, RULES, "", "rules"), (203, RULES, "", "rules"), (206, RULES, "", "rules"),
    (200, b"\xef\xbb\xbf" + RULES, "", "rules"),
    (200, b"<!-- generated by the CMS -->\n" + RULES, "", "rules"),              # the first character is «<»
    (200, b"# <html> pages are listed in <!DOCTYPE> order\n" + RULES, "", "rules"),  # the words in a remark
    (200, b"<html><body><pre>\n" + RULES + b"</pre></body></html>", "", "rules"),
    (200, b"Sitemap: https://x.ru/sitemap.xml\n", "", "rules"),                  # a robots.txt without groups
    (200, RULES.decode(), "", "rules"),                                          # text from the cache
    # 2xx without a directive line: the site has no robots.txt
    (200, b"", "", "open"), (204, b"", "", "open"), (200, b"  \n\n", "", "open"), (200, b"\xef\xbb\xbf", "", "open"),
    (200, b"<!DOCTYPE html><html><body>404</body></html>", "", "open"),
    (200, b"\xef\xbb\xbf\n  <html><head><title>User-agent</title></head></html>", "", "open"),
    (200, b"Not found", "", "open"), (200, b'{"error": "not found"}', "", "open"),
    (200, b"# nothing here\n", "", "open"),
    # 4xx except 429: no file
    (400, b"", "HTTP 400", "open"), (401, b"", "HTTP 401", "open"), (403, b"", "HTTP 403", "open"),
    (404, b"", "HTTP 404", "open"), (410, b"", "HTTP 410", "open"), (451, b"", "HTTP 451", "open"),
    # the rules are unknown: the host is closed
    (429, b"", "HTTP 429", "closed"), (500, b"", "HTTP 500", "closed"), (502, b"", "", "closed"),
    (503, RULES, "", "closed"), (599, b"", "", "closed"),
    (301, b"", "больше 5 редиректов подряд", "closed"), (302, b"", "HTTP 302", "closed"), (307, b"", "", "closed"),
    (0, b"", "ReadTimeout: timed out", "closed"), (0, b"", "нет соединения (ConnectError: refused)", "closed"),
    (0, RULES, "curl: код 18", "closed"), (0, b"", "", "closed"), (101, b"", "", "closed"),
])
def test_one_reader_decides_what_an_answer_to_robots_txt_means(status, body, error, state):
    answer = P.read_robots(status, {}, body, P.DEFAULT_UA, error)
    assert answer.state == state and answer.why
    assert (answer.rules is not None) == (state == "rules")
    if state == "rules" and b"Disallow" in (body if isinstance(body, bytes) else body.encode()):
        assert not answer.rules.allows("https://x.ru/private/x") and answer.rules.allows("https://x.ru/open/x")


def test_reader_names_the_fact_behind_its_decision_and_headers_do_not_change_it():
    read = P.read_robots
    assert read(200, {}, RULES, P.DEFAULT_UA).why == "HTTP 200"
    assert read(200, {}, b"", P.DEFAULT_UA).why == "HTTP 200, пустой ответ"
    assert read(200, {}, b"<html>404</html>", P.DEFAULT_UA).why == "HTTP 200, HTML-страница вместо файла"
    assert read(200, {"Content-Type": "text/html"}, b"Not found", P.DEFAULT_UA).why \
        == "HTTP 200, HTML-страница вместо файла"
    assert read(200, {}, b"Not found", P.DEFAULT_UA).why == "HTTP 200, в ответе нет ни одной директивы"
    assert read(404, {}, b"", P.DEFAULT_UA, "HTTP 404").why == "HTTP 404"
    assert read(503, {}, b"", P.DEFAULT_UA, "HTTP 503").why == "HTTP 503"
    assert read(302, {}, b"", P.DEFAULT_UA, "HTTP 302").why == "HTTP 302, переадресация не привела к файлу"
    assert read(302, {"Location": "ftp://x.ru/robots.txt"}, b"", P.DEFAULT_UA).why \
        == "HTTP 302, переадресация не привела к файлу: ftp://x.ru/robots.txt"
    assert read(301, {}, b"", P.DEFAULT_UA, "больше 5 редиректов подряд").why \
        == "HTTP 301, переадресация не привела к файлу: больше 5 редиректов подряд"
    assert read(0, {}, b"", P.DEFAULT_UA, "ReadTimeout: timed out").why == "ReadTimeout: timed out"
    assert read(0, {}, b"", P.DEFAULT_UA).why == "нет ответа"
    # a content type cannot turn a file with rules into «no file», nor an HTML page into rules
    assert read(200, {"content-type": "text/html; charset=utf-8"}, RULES, P.DEFAULT_UA).state == "rules"
    assert read(200, {"content-type": "text/plain"}, b"<html>404</html>", P.DEFAULT_UA).state == "open"


def test_robots_txt_bytes_are_read_as_utf8_then_as_cp1251():
    assert P.decode_robots("Disallow: /контакты".encode()) == "Disallow: /контакты"
    assert P.decode_robots("# закрыто\nDisallow: /a".encode("cp1251")) == "# закрыто\nDisallow: /a"
    # a body that is still gzip (curl does not unpack what it did not ask for) is unpacked, or the host is closed
    packed = gzip.compress(RULES)
    assert P.decode_robots(packed) == RULES.decode()
    assert not P.read_robots(200, {}, packed, P.DEFAULT_UA).rules.allows("https://x.ru/private/x")
    broken = P.read_robots(200, {}, packed[:10] + b"garbage", P.DEFAULT_UA)
    assert broken.state == "closed" and broken.why.startswith("HTTP 200, сжатый robots.txt не распакован")
    cyr = P.read_robots(200, {}, "\ufeffUser-agent: *\nDisallow: /контакты\n".encode(), P.DEFAULT_UA)
    assert not cyr.rules.allows("https://x.ru/%D0%BA%D0%BE%D0%BD%D1%82%D0%B0%D0%BA%D1%82%D1%8B/")
    old = P.read_robots(200, {}, "# правила сайта\nUser-agent: *\nDisallow: /private/\n".encode("cp1251"), P.DEFAULT_UA)
    assert old.state == "rules" and not old.rules.allows("https://x.ru/private/x")


@pytest.mark.parametrize("status", [202, 203, 206])
def test_robots_txt_served_with_another_2xx_is_still_the_rules(plain, respx_mock, status):
    respx_mock.get(f"{SITE}/robots.txt").mock(return_value=httpx.Response(status, text=OPEN_ROBOTS))
    page = respx_mock.get(f"{SITE}/private/prices/").mock(return_value=httpx.Response(200, html=ABOUT_HTML))
    respx_mock.get(f"{SITE}/about/").mock(return_value=httpx.Response(200, html=ABOUT_HTML))
    assert plain.get(f"{SITE}/private/prices/").error == "запрещено robots.txt" and page.call_count == 0
    assert plain.get(f"{SITE}/about/").ok


@pytest.mark.parametrize("body", [
    "<!-- robots.txt of the site -->\n" + OPEN_ROBOTS,
    "# the <html> pages of the shop, see <!DOCTYPE html>\n" + OPEN_ROBOTS,
    "\ufeff  \n<html>\n" + OPEN_ROBOTS + "</html>\n",
])
def test_robots_txt_that_looks_like_html_but_carries_rules_is_obeyed(plain, respx_mock, body):
    respx_mock.get(f"{SITE}/robots.txt").mock(return_value=httpx.Response(200, text=body))
    page = respx_mock.get(f"{SITE}/private/prices/").mock(return_value=httpx.Response(200, html=ABOUT_HTML))
    assert plain.get(f"{SITE}/private/prices/").error == "запрещено robots.txt" and page.call_count == 0


def test_answer_with_no_body_or_no_directive_is_a_site_without_robots_txt(plain, respx_mock):
    respx_mock.get(f"{SITE}/robots.txt").mock(return_value=httpx.Response(204))
    respx_mock.get(f"{WWW}/robots.txt").mock(return_value=httpx.Response(200, text="Not found"))
    respx_mock.route().mock(return_value=httpx.Response(200, html=ABOUT_HTML))
    assert plain.get(f"{SITE}/private/prices/").ok and plain.get(f"{WWW}/private/prices/").ok


def test_redirect_of_robots_txt_is_followed_and_its_rules_apply_to_the_host_that_was_asked(plain, respx_mock):
    # RFC 9309, 2.3.1.2: the file may live elsewhere, on another site too
    respx_mock.get(f"{SITE}/robots.txt").mock(
        return_value=httpx.Response(301, headers={"location": "https://cdn.acme-files.test/acme/robots.txt"}))
    respx_mock.get("https://cdn.acme-files.test/acme/robots.txt").mock(
        return_value=httpx.Response(200, text=OPEN_ROBOTS))
    page = respx_mock.get(f"{SITE}/private/prices/").mock(return_value=httpx.Response(200, html=ABOUT_HTML))
    respx_mock.get(f"{SITE}/about/").mock(return_value=httpx.Response(200, html=ABOUT_HTML))
    assert plain.get(f"{SITE}/private/prices/").error == "запрещено robots.txt" and page.call_count == 0
    assert plain.get(f"{SITE}/about/").ok
    assert asked(respx_mock) == [f"{SITE}/robots.txt", "https://cdn.acme-files.test/acme/robots.txt",
                                 f"{SITE}/about/"]


@pytest.mark.parametrize("answer, why", [
    (httpx.Response(302), "HTTP 302, переадресация не привела к файлу"),
    (httpx.Response(301, headers={"location": "/robots.txt"}),
     "HTTP 301, переадресация не привела к файлу: больше 5 редиректов подряд"),
    (httpx.Response(302, headers={"location": "ftp://acme-stanki.ru/robots.txt"}),
     "HTTP 302, переадресация не привела к файлу: редирект на адрес, который нельзя открыть"),
])
def test_redirect_of_robots_txt_that_does_not_end_in_a_file_closes_the_host(plain, respx_mock, answer, why):
    robots = respx_mock.get(f"{SITE}/robots.txt").mock(return_value=answer)
    page = respx_mock.get(f"{SITE}/about/").mock(return_value=httpx.Response(200, html=ABOUT_HTML))
    res = plain.get(f"{SITE}/about/")
    assert res.error == f"robots.txt не получен ({why}): правила сайта неизвестны, страницы не запрашиваются"
    assert page.call_count == 0 and robots.call_count in (1, P.MAX_REDIRECTS + 1)


# --- robots.txt: whose group applies -------------------------------------------------------------- #

BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124"
LEADFINDER_UA = "Mozilla/5.0 (compatible; LeadFinderBot/1.0; company contact pages only; respects robots.txt)"


def test_product_token_of_a_user_agent():
    assert P.robots_token(P.DEFAULT_UA) == "outreachresearchbot"
    assert P.robots_token(LEADFINDER_UA) == "leadfinderbot"  # «robots.txt» in the remark is not a robot's name
    assert P.robots_token(BROWSER_UA) == "" and P.robots_token("") == ""


@pytest.mark.parametrize("name, ours", [
    ("OutreachResearchBot", True), ("outreachresearchbot", True), ("OUTREACHRESEARCHBOT", True),
    ("OutreachResearchBot/1.0", True),  # a version after the token still names the robot
    # a part of the name is another robot's name
    ("bot", False), ("Bot", False), ("search", False), ("research", False), ("outreach", False),
    ("ResearchBot", False), ("OutreachResearchBot2", False), ("MyOutreachResearchBot", False),
    ("Mozilla", False), ("compatible", False), ("Googlebot", False), ("", False),
])
def test_group_applies_only_when_it_names_exactly_our_product_token(name, ours):
    text = f"User-agent: {name}\nDisallow: /own/\n\nUser-agent: *\nDisallow: /private/\n"
    rules = P.RobotsRules(text, P.DEFAULT_UA)
    # our own group replaces «*»; without one «*» applies
    assert rules.allows("https://x.ru/own/x") is not ours and rules.allows("https://x.ru/private/x") is ours


@pytest.mark.parametrize("name", ["lead", "finder", "bot", "leadfinder", "LeadFinderBotX", "OutreachResearchBot"])
def test_group_of_a_similar_name_is_not_the_group_of_leadfinder(name):
    text = f"User-agent: {name}\nAllow: /\n\nUser-agent: *\nDisallow: /private/\n"
    assert not P.RobotsRules(text, LEADFINDER_UA).allows("https://x.ru/private/x")
    own = "User-agent: leadfinderbot\nDisallow: /own/\n\nUser-agent: *\nDisallow: /private/\n"
    assert not P.RobotsRules(own, LEADFINDER_UA).allows("https://x.ru/own/x")
    assert P.RobotsRules(own, P.DEFAULT_UA).allows("https://x.ru/own/x")  # the other robot has no group here


@pytest.mark.parametrize("name", ["Mozilla", "mozilla", "Chrome", "Safari", "AppleWebKit", "Macintosh", "Gecko",
                                  "KHTML", "Mozilla/5.0", "OutreachResearchBot", "LeadFinderBot", "bot"])
def test_client_with_a_browser_user_agent_obeys_only_the_star_group(name):
    text = f"User-agent: {name}\nDisallow:\n\nUser-agent: *\nDisallow: /private/\n"
    rules = P.RobotsRules(text, BROWSER_UA)
    assert not rules.allows("https://x.ru/private/x") and rules.allows("https://x.ru/open/x")


def test_groups_of_one_robot_are_read_together_and_a_named_group_wins_over_the_star():
    text = ("User-agent: OutreachResearchBot\nDisallow: /a/\n\nUser-agent: *\nDisallow: /\n\n"
            "User-agent: Yandex\nUser-agent: outreachresearchbot\nDisallow: /b/\n")
    rules = P.RobotsRules(text, P.DEFAULT_UA)
    assert not rules.allows("https://x.ru/a/1") and not rules.allows("https://x.ru/b/1") and rules.allows("https://x.ru/c")


def test_record_that_is_not_a_rule_neither_ends_a_group_nor_splits_its_names():
    # RFC 9309, 2.2.4: Sitemap, Crawl-delay, Host and the like must not interfere with the groups
    text = ("User-agent: *\nCrawl-delay: 5\nUser-agent: Yandex\nHost: x.ru\nDisallow: /private/\n"
            "Sitemap: https://x.ru/s.xml\nClean-param: utm /\nDisallow: /tmp/\n")
    rules = P.RobotsRules(text, P.DEFAULT_UA)
    assert not rules.allows("https://x.ru/private/x") and not rules.allows("https://x.ru/tmp/x")
    assert rules.allows("https://x.ru/open/x")
    # a group of our own that carries no rule yet is not closed off from the names that follow it
    text = "User-agent: OutreachResearchBot\nCrawl-delay: 1\n\nUser-agent: *\nDisallow: /\n"
    assert not P.RobotsRules(text, P.DEFAULT_UA).allows("https://x.ru/")


def test_robots_txt_itself_is_the_one_address_requested_unasked(plain, respx_mock):
    respx_mock.get(f"{SITE}/robots.txt").mock(return_value=httpx.Response(
        200, text="User-agent: *\nDisallow: /\n", headers={"content-type": "text/plain"}))
    res = plain.get(f"{SITE}/private/../robots.txt")  # the spelling is settled first, then the address is judged
    assert res.ok and res.url == f"{SITE}/robots.txt" and res.html == "User-agent: *\nDisallow: /\n"
    assert plain.get(f"{SITE}/robots.txt/../about/").error == "запрещено robots.txt"
    assert asked(respx_mock) == [f"{SITE}/robots.txt"] * 2  # asked as a page, then read as the rules of the host


def test_group_named_like_a_part_of_our_name_does_not_open_a_closed_page(plain, respx_mock):
    text = "User-agent: bot\nUser-agent: research\nAllow: /\n\nUser-agent: *\nDisallow: /private/\n"
    respx_mock.get(f"{SITE}/robots.txt").mock(return_value=httpx.Response(200, text=text))
    page = respx_mock.get(f"{SITE}/private/prices/").mock(return_value=httpx.Response(200, html=ABOUT_HTML))
    assert plain.get(f"{SITE}/private/prices/").error == "запрещено robots.txt" and page.call_count == 0


def test_non_html_is_skipped(fetcher, respx_mock):
    no_robots_txt(respx_mock, SITE)
    respx_mock.get(f"{SITE}/price.pdf").mock(
        return_value=httpx.Response(200, content=b"%PDF", headers={"content-type": "application/pdf"}))
    assert "не HTML" in fetcher.get(f"{SITE}/price.pdf").error


def test_cp1251_page_is_decoded():
    raw = "<html><head><meta charset='windows-1251'><title>О компании</title></head></html>".encode("cp1251")
    assert "О компании" in P.decode_html(raw, "text/html")


# --- parsing ----------------------------------------------------------------- #

def test_subpages_prefer_about_and_skip_product_links():
    home = P.parse_html(f"{SITE}/", HOME_HTML)
    picked = P.pick_subpages(home, 3)
    assert picked[0] == (f"{SITE}/about/", "about")
    assert (f"{SITE}/news/", "news") in picked
    assert all("press-machines" not in url and "other.example" not in url for url, _ in picked)


def test_about_and_news_match_path_segments_not_substrings():
    # ezhonggroup.com: a case/product page whose slug merely contains "about".
    links = ('<a href="/cases-about-sheet-metal-forming-machine/">Sheet Metal Forming Machine Project Cases</a>'
             '<a href="/hydraulic-press-news-letter-holder/">Hydraulic press</a>'
             '<a href="/products/about-face-milling-cutter/">Milling cutters</a>'
             '<a href="/company/news/">Company news</a>'
             '<a href="/about-us/">About Us</a>')
    home = P.parse_html("https://www.ezhonggroup.com/", f"<html><body><nav>{links}</nav></body></html>")
    ranked = dict(P.rank_subpages(home))
    assert "https://www.ezhonggroup.com/cases-about-sheet-metal-forming-machine/" not in ranked
    assert "https://www.ezhonggroup.com/products/about-face-milling-cutter/" not in ranked
    assert "https://www.ezhonggroup.com/hydraulic-press-news-letter-holder/" not in ranked
    assert ranked["https://www.ezhonggroup.com/about-us/"] == "about"
    assert ranked["https://www.ezhonggroup.com/company/news/"] == "news"
    assert P.classify_link("/cases-about-sheet-metal-forming-machine/", "Cases") == ("news", 2)  # anchor text
    assert P.classify_link("/o-kompanii.html", "") == ("about", 2)
    assert P.classify_link("/updates", "") == ("news", 2) and P.classify_link("/blog/", "Блог") == ("news", 3)
    # Chinese menus: ideographs are word characters, the keyword is followed by more of them.
    assert P.classify_link("/a/123.html", "关于我们") == ("about", 3)
    assert P.classify_link("/list/9.html", "新闻动态") == ("news", 3)
    assert P.classify_link("/p/1.html", "Read more about our presses") is None


def test_stdlib_parser_fallback(monkeypatch):
    monkeypatch.setattr(P, "BeautifulSoup", None)
    page = P.parse_html(f"{SITE}/", HOME_HTML)
    assert page.title == "Акме Станки — станки с ЧПУ"
    assert page.h1 == ["Акме Станки"]
    assert (f"{SITE}/about/", "О компании") in page.links
    assert "Поставляем оборудование." in page.lines
    assert not any("Все права" in line for line in page.lines)  # footer is not main content
    assert page.copyright and "Акме Станки" in page.copyright[0]


def test_bs4_keeps_inline_markup_in_one_sentence():
    page = P.parse_html("https://x.ru/", "<html><body><p>Мы <b>производим</b> станки <a href='/a'>с ЧПУ</a> "
                                         "с 2009 года.</p></body></html>")
    assert "Мы производим станки с ЧПУ с 2009 года." in page.lines


# --- row processing ---------------------------------------------------------- #

def test_row_ok_with_llm(fetcher, acme_site):
    backend = FakeBackend(NEWS_ANSWER)
    info = P.RowInfo(0, 2, "Акме Станки", "acme-stanki.ru", "sales@acme-stanki.ru")
    out = P.process_row(info, make_ctx([info], fetcher, backend))
    assert out[P.COL_CHECK] == P.STATUS_OK
    assert out[P.COL_PERSONALIZATION] == NEWS_ANSWER["personalization"]
    assert out[P.COL_SOURCE] == f"{SITE}/news/"
    assert "основание" in out[P.COL_COMMENT] and "дата факта на странице: 09.2026" in out[P.COL_COMMENT]
    assert len(backend.prompts) == 1  # the freshest fact was chosen: no extra round


def test_mismatched_row_is_not_personalized(fetcher, acme_site):
    backend = FakeBackend(VALID_ANSWER)
    info = P.RowInfo(0, 2, "Tengzhong Machinery", "acme-stanki.ru", "sales01@nttzmt.com")
    out = P.process_row(info, make_ctx([info], fetcher, backend))
    assert out[P.COL_CHECK].startswith(P.STATUS_MISMATCH)
    assert "nttzmt.com" in out[P.COL_CHECK]
    assert out[P.COL_PERSONALIZATION] == P.NO_DATA
    assert backend.prompts == []  # never ask the LLM to describe someone else's company


def test_email_mismatch_on_confirmed_site_suggests_published_role_address(fetcher, acme_site):
    info = P.RowInfo(0, 2, "Акме Станки", "acme-stanki.ru", "sales@thebestcnc.com")
    out = P.process_row(info, make_ctx([info], fetcher, None))
    assert out[P.COL_CHECK].startswith(P.STATUS_MISMATCH)
    assert "sales@acme-stanki.ru" in out[P.COL_COMMENT]
    assert out[P.COL_PERSONALIZATION] != P.NO_DATA  # the company itself is right, only the email is wrong


# --- CLI --------------------------------------------------------------------- #

def test_cli_none_backend_end_to_end(tmp_path, acme_site):
    src, dst = tmp_path / "in.csv", tmp_path / "out.csv"
    write_csv(src, [("Акме Станки", "sales@acme-stanki.ru", "acme-stanki.ru")])
    code = P.run([str(src), "-o", str(dst), "--backend", "none", "--cache-dir", str(tmp_path / "c"),
                  "--delay", "0", "--retries", "0"])
    assert code == 0
    rows = read_csv(dst)
    assert list(rows[0].keys()) == ["company", "email", "site", *P.OUTPUT_COLUMNS]
    assert rows[0][P.COL_CHECK] == P.STATUS_OK
    # Without an LLM the freshest dated line wins over the evergreen "founded in 2009".
    assert "Запустили участок лазерной резки" in rows[0][P.COL_PERSONALIZATION]
    assert rows[0][P.COL_SOURCE] == f"{SITE}/news/"


def test_cli_resume_skips_done_rows_and_limit(tmp_path, acme_site):
    src, dst = tmp_path / "in.csv", tmp_path / "out.csv"
    write_csv(src, [("Акме Станки", "sales@acme-stanki.ru", "acme-stanki.ru"),
                    ("Акме Станки 2", "sales@acme-stanki.ru", "acme-stanki.ru"),
                    ("Третья", "info@third.ru", "third.ru")])
    argv = [str(src), "-o", str(dst), "--cache-dir", str(tmp_path / "c"), "--delay", "0", "--retries", "0"]

    first = FakeBackend(NEWS_ANSWER)
    P.run(argv + ["--limit", "2"], backend=first)
    assert len(first.prompts) == 2 and len(read_csv(dst)) == 2

    second = FakeBackend(NEWS_ANSWER)
    P.run(argv + ["--limit", "2"], backend=second)
    assert second.prompts == []  # everything already done

    third = FakeBackend(NEWS_ANSWER)
    P.run(argv + ["--limit", "2", "--force"], backend=third)
    assert len(third.prompts) == 2


def test_cli_rejects_input_without_required_columns(tmp_path):
    src = tmp_path / "bad.csv"
    write_csv(src, [("x",)], header=("whatever",))
    try:
        P.run([str(src), "--backend", "none"])
    except SystemExit as exc:
        assert "company" in str(exc)
    else:
        raise AssertionError("expected SystemExit")


def test_unreachable_host_is_negatively_cached(tmp_path, respx_mock, monkeypatch):
    route = respx_mock.route().mock(side_effect=httpx.ConnectTimeout("no route"))

    def one_run():
        f = P.Fetcher(cache_dir=tmp_path / "c", delay=0, retries=1, backoff=0)
        try:
            return f.get("https://dead.test/"), f.get("https://dead.test/")
        finally:
            f.close()

    assert not any(res.ok for res in one_run())
    # the host is given up at its robots.txt: a connect timeout is not retried, the page is not asked at all
    assert [str(call.request.url) for call in route.calls] == ["https://dead.test/robots.txt"]
    assert "нет соединения" in one_run()[0].error and route.call_count == 1  # the next run trusts the cached failure
    monkeypatch.setattr(P, "NEGATIVE_CACHE_TTL", -1)  # stale entry -> network again
    one_run()
    assert route.call_count == 2


def test_email_found_on_contacts_page(fetcher, respx_mock):
    home = HOME_HTML.replace("sales@acme-stanki.ru", "").replace(
        '<a href="/news/">Новости</a>', '<a href="/news/">Новости</a> <a href="/kontakty/">Контакты</a>')
    respx_mock.get(f"{SITE}/").mock(return_value=httpx.Response(200, html=home))
    respx_mock.get(f"{SITE}/about/").mock(return_value=httpx.Response(200, html=ABOUT_HTML))
    contacts = respx_mock.get(f"{SITE}/kontakty/").mock(
        return_value=httpx.Response(200, html="<html><body><p>Отдел продаж: sales@acme-stanki.ru</p></body></html>"))
    respx_mock.route().mock(return_value=httpx.Response(404))
    info = P.RowInfo(0, 2, "Акме Станки", "acme-stanki.ru", "sales@acme-stanki.ru")
    out = P.process_row(info, make_ctx([info], fetcher, None))
    assert contacts.call_count == 1
    assert out[P.COL_CHECK] == P.STATUS_OK
    assert "email указан на сайте" in out[P.COL_COMMENT]
    assert "kontakty" not in out[P.COL_SOURCE]  # contacts page is never a personalization source


STAFF_HTML = ("<html><head><title>Сотрудники — Акме Станки</title></head><body><main>"
              "<p>Коммерческий директор Иван Петров: i.petrov@acme-stanki.ru</p></main></body></html>")


def _acme_without_address(respx_mock):
    home = HOME_HTML.replace("sales@acme-stanki.ru", "")
    respx_mock.get(f"{SITE}/").mock(return_value=httpx.Response(200, html=home))
    respx_mock.get(f"{SITE}/about/").mock(return_value=httpx.Response(200, html=ABOUT_HTML))
    staff = respx_mock.get(f"{SITE}/company/staff/").mock(return_value=httpx.Response(200, html=STAFF_HTML))
    catalog = respx_mock.get("https://catalog.example/acme").mock(return_value=httpx.Response(200, html=STAFF_HTML))
    respx_mock.route().mock(return_value=httpx.Response(404))
    return staff, catalog


def test_email_is_checked_on_the_page_the_row_names(fetcher, respx_mock):
    # A direct address sits in a staff card, not on the pages the script reads by itself:
    # without the row's own email_source the comment said "not found (maybe stale)".
    staff, _ = _acme_without_address(respx_mock)
    info = P.RowInfo(0, 2, "Акме Станки", "acme-stanki.ru", "I.Petrov@acme-stanki.ru", f"{SITE}/company/staff/")
    out = P.process_row(info, make_ctx([info], fetcher, None))
    assert staff.call_count == 1
    assert out[P.COL_CHECK] == P.STATUS_OK
    assert "email указан на сайте" in out[P.COL_COMMENT] and "не найден" not in out[P.COL_COMMENT]
    assert "email_source" in out[P.COL_COMMENT]
    # The staff page confirms the address and nothing else: no fact and no name is taken from it.
    assert "staff" not in out[P.COL_SOURCE] and "Петров" not in " ".join(out.values())


def test_email_source_outside_the_company_site_is_not_read(fetcher, respx_mock):
    staff, catalog = _acme_without_address(respx_mock)
    info = P.RowInfo(0, 2, "Акме Станки", "acme-stanki.ru", "i.petrov@acme-stanki.ru", "https://catalog.example/acme")
    out = P.process_row(info, make_ctx([info], fetcher, None))
    assert catalog.call_count == 0 and staff.call_count == 0
    assert "email на прочитанных страницах сайта не найден" in out[P.COL_COMMENT]
    assert "email_source не прочитана: не страница сайта компании" in out[P.COL_COMMENT]


def test_email_source_closed_by_robots_is_not_read_and_the_comment_says_so(tmp_path, respx_mock):
    # emg.com.ru: «Disallow: /company/staff/». The script keeps to robots.txt, so the address
    # stays unconfirmed, and the reviewer sees the reason instead of a bare "not found".
    respx_mock.get(f"{SITE}/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /company/staff/\n"))
    staff, _ = _acme_without_address(respx_mock)  # registered after robots.txt: its last route answers 404 to the rest
    fetcher = P.Fetcher(cache_dir=tmp_path / "c", delay=0, retries=0, backoff=0)
    info = P.RowInfo(0, 2, "Акме Станки", "acme-stanki.ru", "i.petrov@acme-stanki.ru", f"{SITE}/company/staff/")
    out = P.process_row(info, make_ctx([info], fetcher, None))
    fetcher.close()
    assert staff.call_count == 0
    assert "email_source не прочитана: запрещено robots.txt" in out[P.COL_COMMENT]
    assert "email на прочитанных страницах сайта не найден" in out[P.COL_COMMENT]


def test_email_source_on_a_cyrillic_domain_matches_its_punycode_form(fetcher, respx_mock):
    no_robots_txt(respx_mock, "https://xn--80ajybdmjbd1a.xn--p1ai")
    page = respx_mock.get("https://xn--80ajybdmjbd1a.xn--p1ai/managment").mock(
        return_value=httpx.Response(200, html=STAFF_HTML))
    found, why = P.fetch_email_source(fetcher, "https://xn--80ajybdmjbd1a.xn--p1ai/managment", "технотранс.рф", [])
    assert page.call_count == 1 and found is not None and found.kind == "contacts" and why == ""
    assert P.fetch_email_source(fetcher, "см. сайт", "технотранс.рф", []) == (None, "")  # not a URL: nothing to read


def test_cli_reads_the_email_source_column(tmp_path, respx_mock):
    staff, _ = _acme_without_address(respx_mock)
    src, dst = tmp_path / "in.csv", tmp_path / "out.csv"
    write_csv(src, [("Акме Станки", "i.petrov@acme-stanki.ru", "acme-stanki.ru", f"{SITE}/company/staff/")],
              header=("company", "email", "site", "email_source"))
    P.run([str(src), "-o", str(dst), "--backend", "none", "--cache-dir", str(tmp_path / "c"), "--delay", "0",
           "--retries", "0"])
    out = read_csv(dst)[0]
    assert staff.call_count == 1 and "email указан на сайте" in out[P.COL_COMMENT]
    assert out["email_source"] == f"{SITE}/company/staff/"  # input columns pass through unchanged


# --- robots.txt matcher ------------------------------------------------------- #

LOGSIS_LIKE_ROBOTS = """User-agent: SMTBot
Disallow: /

User-agent: *
Disallow: /?
Disallow: *?s=
Disallow: *i*=
Disallow: /search/
Allow: /search/public$
Sitemap: https://x.ru/sitemap.xml
"""


def test_robots_disallow_query_root_does_not_block_site():
    # urllib.robotparser turns 'Disallow: /?' into 'Disallow: /' and blocks everything.
    rules = P.RobotsRules(LOGSIS_LIKE_ROBOTS, P.DEFAULT_UA)
    assert rules.allows("https://x.ru/")
    assert rules.allows("https://x.ru/about/")
    assert not rules.allows("https://x.ru/?utm=1")
    assert not rules.allows("https://x.ru/news/?s=press")
    assert not rules.allows("https://x.ru/search/q")
    assert rules.allows("https://x.ru/search/public")  # longer Allow wins
    assert not rules.allows("https://x.ru/search/public2")  # '$' anchors the end


def test_robots_group_for_our_bot_and_full_block():
    rules = P.RobotsRules("User-agent: OutreachResearchBot\nDisallow: /\n\nUser-agent: *\nAllow: /\n", P.DEFAULT_UA)
    assert not rules.allows("https://x.ru/")
    rules = P.RobotsRules("User-agent: *\nDisallow:\n", P.DEFAULT_UA)
    assert rules.allows("https://x.ru/anything")


CYR_PATH = "/%D0%BA%D0%BE%D0%BD%D1%82%D0%B0%D0%BA%D1%82%D1%8B"  # «/контакты», percent-encoded


@pytest.mark.parametrize("bom", ["\ufeff", "п»ї", "ï»¿"], ids=["utf-8", "cp1251", "latin-1"])
def test_robots_byte_order_mark_does_not_hide_the_first_group(bom):
    # Without this the first line reads «\ufeffUser-agent», the group is lost and its Disallow with it.
    rules = P.RobotsRules(f"{bom}User-agent: *\nDisallow: /private/\n", P.DEFAULT_UA)
    assert not rules.allows("https://x.ru/private/prices/") and rules.allows("https://x.ru/about/")


def test_robots_byte_order_mark_inside_the_file_does_not_hide_a_group():
    # two files glued together: the mark stands at the start of a later line
    glued = P.RobotsRules("Sitemap: https://x.ru/s.xml\n\ufeffUser-agent: *\nDisallow: /private/\n", P.DEFAULT_UA)
    assert not glued.allows("https://x.ru/private/prices/") and glued.allows("https://x.ru/about/")


def test_robots_rule_and_path_are_compared_in_one_spelling():
    spellings = ("/контакты", CYR_PATH, CYR_PATH.lower())
    for rule in spellings:
        rules = P.RobotsRules(f"User-agent: *\nDisallow: {rule}\n", P.DEFAULT_UA)
        assert [rules.allows(f"https://x.ru{path}/") for path in spellings] == [False, False, False]
        assert rules.allows("https://x.ru/контакт") and rules.allows("https://x.ru/about/")
    # a percent-encoded letter is the letter, «%7E» is «~», a space in a rule is «%20» in an address
    rules = P.RobotsRules("User-agent: *\nDisallow: /c%6Fntacts\nDisallow: /%7Eivanov\nDisallow: /price list\n",
                          P.DEFAULT_UA)
    assert not rules.allows("https://x.ru/contacts/") and not rules.allows("https://x.ru/~ivanov/")
    assert not rules.allows("https://x.ru/price%20list.xlsx") and rules.allows("https://x.ru/price")
    assert P.robots_key("/контакты?q=%d1%8f&a=b%2fc") == CYR_PATH + "?q=%D1%8F&a=b%2Fc"
    assert P.robots_key("/100%/%zz") == "/100%25/%25zz"  # a stray «%» is a character, not the start of an octet


def test_robots_percent_encoded_slash_is_not_a_slash():
    rules = P.RobotsRules("User-agent: *\nDisallow: *%2F\n", P.DEFAULT_UA)
    assert rules.allows("https://x.ru/") and rules.allows("https://x.ru/about/team/")  # the site is not closed
    assert not rules.allows("https://x.ru/catalog/a%2Fb") and not rules.allows("https://x.ru/catalog/a%2fb")
    rules = P.RobotsRules("User-agent: *\nDisallow: /a/b\n", P.DEFAULT_UA)
    assert not rules.allows("https://x.ru/a/b") and rules.allows("https://x.ru/a%2Fb")
    # an encoded star or dollar is a character of the path, not a wildcard
    rules = P.RobotsRules("User-agent: *\nDisallow: /file%2A\nDisallow: /sum%24\n", P.DEFAULT_UA)
    assert rules.allows("https://x.ru/file-1") and not rules.allows("https://x.ru/file%2A")
    assert not rules.allows("https://x.ru/sum%24total") and rules.allows("https://x.ru/sum")


def test_robots_group_without_a_name_is_not_the_group_of_our_robot():
    text = "User-agent:\nAllow: /\n\nUser-agent: *\nDisallow: /private/\n"
    assert not P.RobotsRules(text, P.DEFAULT_UA).allows("https://x.ru/private/")


def test_robots_rule_made_of_stars_does_not_hang_the_run():
    rules = P.RobotsRules(f"User-agent: *\nDisallow: /{'*a' * 40}*b$\n", P.DEFAULT_UA)
    started = time.perf_counter()
    assert rules.allows("https://x.ru/" + "a" * 2000) and not rules.allows("https://x.ru/" + "a" * 2000 + "b")
    assert time.perf_counter() - started < 2  # a backtracking regex needs years for this rule


def test_guessed_contacts_path_when_homepage_has_no_link(fetcher, respx_mock):
    home = HOME_HTML.replace("sales@acme-stanki.ru", "")
    respx_mock.get(f"{SITE}/").mock(return_value=httpx.Response(200, html=home))
    respx_mock.get(f"{SITE}/about/").mock(return_value=httpx.Response(200, html=ABOUT_HTML))
    respx_mock.get(f"{SITE}/contacts").mock(
        return_value=httpx.Response(200, html="<html><body><p>Почта: sales@acme-stanki.ru</p></body></html>"))
    respx_mock.route().mock(return_value=httpx.Response(404))
    info = P.RowInfo(0, 2, "Акме Станки", "acme-stanki.ru", "sales@acme-stanki.ru")
    out = P.process_row(info, make_ctx([info], fetcher, None))
    assert "email указан на сайте" in out[P.COL_COMMENT]


# --- page collection: dedupe, guessed news paths ------------------------------ #

def test_duplicate_pages_are_skipped_and_next_candidate_used(fetcher, respx_mock):
    # internor-mach.com: '/about' redirects to '/web/about/', '/about/' serves the same text.
    home = ('<html><head><title>Internor</title></head><body><nav><a href="/about">About</a>'
            '<a href="/about/">About us</a><a href="/history/">History</a></nav>'
            '<main><p>Internor machines.</p></main></body></html>')
    about = ("<html><body><main><p>Internor was founded in 2004 and builds CNC press brakes "
             "for 40 countries.</p></main></body></html>")
    history = ("<html><body><main><p>In 2015 Internor opened a second plant in Nanjing for laser cutters."
               "</p></main></body></html>")
    site = "https://internor.test"
    respx_mock.get(f"{site}/").mock(return_value=httpx.Response(200, html=home))
    respx_mock.get(f"{site}/about").mock(return_value=httpx.Response(301, headers={"location": f"{site}/web/about/"}))
    respx_mock.get(f"{site}/web/about/").mock(return_value=httpx.Response(200, html=about))
    respx_mock.get(f"{site}/about/").mock(return_value=httpx.Response(200, html=about))
    respx_mock.get(f"{site}/history/").mock(return_value=httpx.Response(200, html=history))
    respx_mock.route().mock(return_value=httpx.Response(404))
    pages, error = P.collect_pages(fetcher, "internor.test", 4)
    assert error == ""
    texts = [p.text for p in pages]
    assert len(texts) == len(set(texts))  # the same About text is read once
    assert any("second plant" in t for t in texts)  # the freed slot went to the next candidate
    assert sum("founded in 2004" in t for t in texts) == 1


def test_news_paths_are_probed_when_homepage_links_none(fetcher, respx_mock):
    # experium.ru: JS menu, but /updates is plain HTML with fresh dated releases.
    home = "<html><head><title>Experium</title></head><body><div id='app'></div></body></html>"
    updates = ("<html><head><title>Обновления</title></head><body><main>"
               "<p>30.09.2026 Релиз 89g — множественные источники обновления данных в карточке человека</p>"
               "<p>12.08.2026 Релиз 89f — массовая рассылка приглашений кандидатам из воронки</p>"
               "</main></body></html>")
    respx_mock.get("https://experium.test/").mock(return_value=httpx.Response(200, html=home))
    route = respx_mock.get("https://experium.test/updates").mock(return_value=httpx.Response(200, html=updates))
    respx_mock.route().mock(return_value=httpx.Response(404))
    pages, _ = P.collect_pages(fetcher, "experium.test", 4)
    assert route.call_count == 1
    news = [p for p in pages if p.kind == "news"]
    assert news and news[0].url == "https://experium.test/updates"
    text, source, notes = P.extractive_fact(pages)
    assert source == "https://experium.test/updates" and "Релиз 89g" in text


def test_splash_homepage_leads_to_the_sections_it_links(fetcher, respx_mock):
    # simbio.ru: the homepage is two picture links to the divisions of the group, with no
    # menu and no text. The row ended as "almost no text (JavaScript?)" with one page read.
    home = ("<html><head><title>СИМБИО</title></head><body><a href='/'><img src='logo.png'></a>"
            "<a href='/selhoz/'><img src='a.jpg'></a><a href='/pets/'><img src='b.jpg'></a>"
            "<a href='https://partner.example/'>Партнёр</a><a href='/files/price.pdf'>Прайс</a></body></html>")
    agro = ("<html><head><title>СИМБИО — сельское хозяйство</title></head><body><main><p>Поставляем ветеринарные "
            "препараты и кормовые добавки для 400 птицефабрик и свинокомплексов России.</p></main></body></html>")
    pets = ("<html><head><title>СИМБИО — домашние животные</title></head><body><main><p>Дистрибутор кормов "
            "для ветеринарных клиник, заводчиков и зоомагазинов.</p><p>Работаем с ветеринарными клиниками "
            "и зоомагазинами по всей России, отгружаем со склада в Подмосковье и консультируем по ассортименту "
            "диагностических наборов и оборудования.</p></main></body></html>")
    respx_mock.get("https://splash.test/").mock(return_value=httpx.Response(200, html=home))
    respx_mock.get("https://splash.test/selhoz/").mock(return_value=httpx.Response(200, html=agro))
    respx_mock.get("https://splash.test/pets/").mock(return_value=httpx.Response(200, html=pets))
    foreign = respx_mock.get("https://partner.example/").mock(return_value=httpx.Response(200, html=agro))
    respx_mock.route().mock(return_value=httpx.Response(404))
    info = P.RowInfo(0, 2, "СИМБИО", "splash.test", "")
    out = P.process_row(info, make_ctx([info], fetcher, None, max_pages=4))
    assert "страниц прочитано: 3" in out[P.COL_COMMENT] and "почти нет текста" not in out[P.COL_COMMENT]
    assert "400 птицефабрик" in out[P.COL_PERSONALIZATION] and out[P.COL_SOURCE] == "https://splash.test/selhoz/"
    assert foreign.call_count == 0  # only the company's own site is read


def test_homepage_with_text_does_not_wander_into_its_links(fetcher, respx_mock):
    text = "<p>Поставляем токарные и фрезерные станки с ЧПУ, запасные части и оснастку заводам Урала и Сибири.</p>" * 3
    home = HOME_HTML.replace("</nav>", "<a href='/catalog/'>Каталог</a><a href='/delivery/'>Доставка</a></nav>") \
        .replace("<p>Поставляем оборудование.</p>", text.replace("</p><p>", "</p><p>А также: "))
    respx_mock.get(f"{SITE}/").mock(return_value=httpx.Response(200, html=home))
    extra = respx_mock.get(url__regex=rf"{SITE}/(catalog|delivery)/").mock(
        return_value=httpx.Response(200, html=ABOUT_HTML))
    respx_mock.route().mock(return_value=httpx.Response(404))
    pages, _ = P.collect_pages(fetcher, "acme-stanki.ru", 4)
    assert [p.kind for p in pages] == ["home"] and extra.call_count == 0


def test_soft_404_and_homepage_clones_are_not_pages(fetcher, respx_mock):
    home = HOME_HTML.replace('<a href="/news/">Новости</a>', "")
    respx_mock.get(f"{SITE}/").mock(return_value=httpx.Response(200, html=home))
    respx_mock.get(f"{SITE}/about/").mock(return_value=httpx.Response(200, html=ABOUT_HTML))
    # SPA fallback: every path answers 200 with the homepage, or with a "not found" page.
    respx_mock.get(f"{SITE}/news/").mock(return_value=httpx.Response(200, html=home))
    respx_mock.get(f"{SITE}/updates").mock(return_value=httpx.Response(
        200, html="<html><head><title>Страница не найдена</title></head><body><main><p>Ошибка 404: такой "
                  "страницы нет, вернитесь на главную страницу сайта.</p></main></body></html>"))
    respx_mock.route().mock(return_value=httpx.Response(404))
    pages, _ = P.collect_pages(fetcher, "acme-stanki.ru", 4)
    assert [p.kind for p in pages] == ["home", "about"]


# --- cache policy and resume --------------------------------------------------- #

def test_near_empty_200_page_is_cached_only_briefly(fetcher, respx_mock, monkeypatch):
    no_robots_txt(respx_mock, "https://stub.test", SITE)
    stub = respx_mock.get("https://stub.test/").mock(
        return_value=httpx.Response(200, html="<html><body><div id='root'></div><script>app()</script></body></html>"))
    full = respx_mock.get(f"{SITE}/about/").mock(return_value=httpx.Response(200, html=ABOUT_HTML))
    assert fetcher.get("https://stub.test/").ok and fetcher.get("https://stub.test/").from_cache
    assert fetcher.get(f"{SITE}/about/").ok
    monkeypatch.setattr(P, "NEGATIVE_CACHE_TTL", -1)  # the short TTL has expired
    assert not fetcher.get("https://stub.test/").from_cache
    assert stub.call_count == 2
    assert fetcher.get(f"{SITE}/about/").from_cache and full.call_count == 1  # real pages stay cached


def test_resume_reprocesses_unavailable_and_empty_site_rows(tmp_path, acme_site):
    src, dst = tmp_path / "in.csv", tmp_path / "out.csv"
    rows = [("Акме Станки", "sales@acme-stanki.ru", "acme-stanki.ru"),
            ("Акме Станки 2", "sales@acme-stanki.ru", "acme-stanki.ru"),
            ("Акме Станки 3", "sales@acme-stanki.ru", "acme-stanki.ru"),
            ("Акме Станки 4", "sales@acme-stanki.ru", "acme-stanki.ru")]
    write_csv(src, rows)
    comments = ["сайт недоступен: https://acme-stanki.ru/: нет соединения",
                "на сайте почти нет текста (вероятно, страница рендерится JavaScript) | страниц прочитано: 1",
                P.LLM_DOWN_NOTE,
                "LLM (claude), основание: «...»"]
    header = ["company", "email", "site", *P.OUTPUT_COLUMNS]
    with open(dst, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        for (company, email, site), comment in zip(rows, comments, strict=True):
            w.writerow([company, email, site, P.NO_DATA, "", P.STATUS_OK, comment])
    backend = FakeBackend(NEWS_ANSWER)
    P.run([str(src), "-o", str(dst), "--cache-dir", str(tmp_path / "c"), "--delay", "0", "--retries", "0"],
          backend=backend)
    assert len(backend.prompts) == 3  # rows 1-3 again, row 4 was finished
    out = read_csv(dst)
    assert [r[P.COL_PERSONALIZATION] == NEWS_ANSWER["personalization"] for r in out] == [True, True, True, False]


def _acme_handler(state):
    """respx side effect: the acme site, switchable between "down" and "up" by the test."""
    def handler(request):
        if state.get("down"):
            raise state["down"]
        if state.get("shell"):  # a JavaScript shell instead of every page
            return httpx.Response(200, html=state["shell"])
        pages = {"/": HOME_HTML, "/about/": ABOUT_HTML, "/news/": NEWS_HTML}
        html = pages.get(request.url.path)
        return httpx.Response(200, html=html) if html else httpx.Response(404)
    return handler


@pytest.mark.parametrize("again", [[], ["--force"]])
@pytest.mark.parametrize("failure", [httpx.ConnectTimeout("handshake timed out"), httpx.ReadTimeout("timed out")])
def test_resume_asks_the_network_again_for_a_site_that_failed(tmp_path, respx_mock, failure, again):
    # A stalled proxy made the first run cache "no connection" (robots.txt included) for
    # 6 hours; the next run of that row, resumed or forced, must not be answered from this cache.
    src, dst = tmp_path / "in.csv", tmp_path / "out.csv"
    write_csv(src, [("Акме Станки", "sales@acme-stanki.ru", "acme-stanki.ru")])
    argv = [str(src), "-o", str(dst), "--cache-dir", str(tmp_path / "c"), "--delay", "0", "--retries", "0",
            "--backend", "none"]
    state = {"down": failure}
    respx_mock.route().mock(side_effect=_acme_handler(state))
    P.run(argv)
    assert "сайт недоступен" in read_csv(dst)[0][P.COL_COMMENT]

    state["down"] = None  # the site answers again
    P.run(argv + again)
    out = read_csv(dst)[0]
    assert "сайт недоступен" not in out[P.COL_COMMENT]
    assert "Запустили участок лазерной резки" in out[P.COL_PERSONALIZATION]


def test_resume_rereads_a_page_that_was_nearly_empty(tmp_path, respx_mock):
    src, dst = tmp_path / "in.csv", tmp_path / "out.csv"
    write_csv(src, [("Акме Станки", "sales@acme-stanki.ru", "acme-stanki.ru")])
    argv = [str(src), "-o", str(dst), "--cache-dir", str(tmp_path / "c"), "--delay", "0", "--retries", "0",
            "--backend", "none"]
    state = {"shell": "<html><head><title>Акме Станки</title></head><body><div id='root'></div></body></html>"}
    respx_mock.route().mock(side_effect=_acme_handler(state))
    P.run(argv)
    assert "почти нет текста" in read_csv(dst)[0][P.COL_COMMENT]

    state["shell"] = None  # the one-off stub is gone
    P.run(argv)
    out = read_csv(dst)[0]
    assert "почти нет текста" not in out[P.COL_COMMENT]
    assert "Запустили участок лазерной резки" in out[P.COL_PERSONALIZATION]


def test_a_row_processed_for_the_first_time_trusts_the_cached_failure(tmp_path, respx_mock):
    # Only a retried row forgets the cached failures of its site: a host that failed
    # minutes ago is not probed again for a row that is new to the output.
    src, dst, cache = tmp_path / "in.csv", tmp_path / "out.csv", tmp_path / "c"
    route = respx_mock.route().mock(side_effect=httpx.ConnectTimeout("no route"))
    fetcher = P.Fetcher(cache_dir=cache, delay=0, retries=0, backoff=0)
    assert not fetcher.get("https://dead.test/").ok
    fetcher.close()
    seen = route.call_count
    write_csv(src, [("Dead", "info@dead.test", "https://dead.test/")])
    P.run([str(src), "-o", str(dst), "--cache-dir", str(cache), "--delay", "0", "--retries", "0",
           "--backend", "none"])
    assert "сайт недоступен" in read_csv(dst)[0][P.COL_COMMENT]
    asked_again = [str(call.request.url) for call in route.calls[seen:]]
    assert "https://dead.test/robots.txt" not in asked_again and "https://dead.test/" not in asked_again


def test_host_that_answers_is_not_kept_dead_by_a_robots_hiccup(tmp_path, respx_mock):
    # compressor-zavod.ru through a stalling SOCKS tunnel: the TLS handshake of robots.txt
    # timed out once, plain http then answered and redirected back to https. The https host
    # stayed "dead" for the run, and for 6 hours in the cache: one page read instead of three.
    hiccup = iter([httpx.ConnectTimeout("handshake timed out")])

    def robots(request):
        for exc in hiccup:
            raise exc
        return httpx.Response(200, text="User-agent: *\nDisallow: /private/\n")

    https_robots = respx_mock.get(f"{SITE}/robots.txt").mock(side_effect=robots)
    respx_mock.get("https://www.acme-stanki.ru/robots.txt").mock(side_effect=httpx.ConnectTimeout("timed out"))
    respx_mock.get("http://acme-stanki.ru/robots.txt").mock(return_value=httpx.Response(404))
    respx_mock.get("http://acme-stanki.ru/").mock(return_value=httpx.Response(301, headers={"Location": f"{SITE}/"}))
    respx_mock.get(f"{SITE}/").mock(return_value=httpx.Response(200, html=HOME_HTML))
    respx_mock.get(f"{SITE}/about/").mock(return_value=httpx.Response(200, html=ABOUT_HTML))
    respx_mock.get(f"{SITE}/news/").mock(return_value=httpx.Response(200, html=NEWS_HTML))
    respx_mock.route().mock(return_value=httpx.Response(404))

    fetcher = P.Fetcher(cache_dir=tmp_path / "c", delay=0, retries=0, backoff=0)
    pages, error = P.collect_pages(fetcher, "acme-stanki.ru", 3)
    fetcher.close()
    assert not error and [p.kind for p in pages] == ["home", "about", "news"]
    assert https_robots.call_count == 2  # robots.txt is read before the pages of the revived host
    again = P.Fetcher(cache_dir=tmp_path / "c", delay=0, retries=0, backoff=0)
    assert again.get(f"{SITE}/robots.txt", html_only=False).ok  # no failure left in the cache
    assert not again.get(f"{SITE}/private/x").ok  # and its rules are in force
    again.close()


def test_failures_of_a_host_that_answers_are_not_cached(tmp_path, respx_mock):
    no_robots_txt(respx_mock, SITE)
    respx_mock.get(f"{SITE}/").mock(return_value=httpx.Response(200, html=HOME_HTML))
    about = respx_mock.get(f"{SITE}/about/").mock(side_effect=[httpx.ReadTimeout("timed out"),
                                                              httpx.Response(200, html=ABOUT_HTML)])
    first = P.Fetcher(cache_dir=tmp_path / "c", delay=0, retries=0, backoff=0)
    assert first.get(f"{SITE}/").ok and not first.get(f"{SITE}/about/").ok
    first.close()
    second = P.Fetcher(cache_dir=tmp_path / "c", delay=0, retries=0, backoff=0)
    assert second.get(f"{SITE}/about/").ok and about.call_count == 2  # one slow answer is not a dead page
    second.close()


def test_cached_failure_is_not_trusted_once_the_host_answers(tmp_path, respx_mock):
    # robots.txt failed while the whole host was silent (so the failure was cached, and the host is
    # closed); in the next run the site answers at its other address, and robots.txt of the closed
    # host is asked again instead of staying "failed".
    state = {"down": httpx.ConnectTimeout("no route")}
    route = respx_mock.route().mock(side_effect=_acme_handler(state))
    first = P.Fetcher(cache_dir=tmp_path / "c", delay=0, retries=0, backoff=0)
    assert not first.get(f"{SITE}/about/").ok and not first.get(f"{SITE}/about/").ok
    assert route.call_count == 1  # a silent host is still not probed twice
    first.close()
    state["down"] = None
    second = P.Fetcher(cache_dir=tmp_path / "c", delay=0, retries=0, backoff=0)
    assert not second.get(f"{SITE}/about/").ok  # nothing has answered yet: the cache is trusted
    assert route.call_count == 1
    assert second.get("http://acme-stanki.ru/").ok and second.get(f"{SITE}/about/").ok
    asked = [str(call.request.url) for call in route.calls[1:]]
    assert asked == ["http://acme-stanki.ru/robots.txt", "http://acme-stanki.ru/", f"{SITE}/robots.txt",
                     f"{SITE}/about/"]
    second.close()


# --- TLS: one SSL context per thread ------------------------------------------ #

def test_threads_do_not_share_http_client_or_ssl_context():
    # truststore toggles check_hostname on its SSLContext during every handshake;
    # a context shared across threads can skip hostname verification.
    f = P.Fetcher(cache_dir=None, delay=0, retries=0, backoff=0)
    seen = {}
    barrier = threading.Barrier(2)

    def grab(name):
        barrier.wait()
        client = f.client
        assert f.client is client  # stable within the thread
        transport_ctx = getattr(getattr(getattr(client, "_transport", None), "_pool", None), "_ssl_context", None)
        seen[name] = (client, f._local.ssl_context, transport_ctx)

    try:
        threads = [threading.Thread(target=grab, args=(n,)) for n in ("a", "b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        (client_a, ctx_a, used_a), (client_b, ctx_b, used_b) = seen["a"], seen["b"]
        assert client_a is not client_b
        assert ctx_a is not ctx_b
        assert ctx_a.check_hostname and ctx_b.check_hostname
        if used_a is not None:  # httpx really uses the per-thread context
            assert used_a is ctx_a and used_b is ctx_b
        assert len(f._clients) == 2
    finally:
        f.close()
    assert f._clients == []


# --- text extraction ----------------------------------------------------------- #

CATALOGUE = ("<div class='content'><h2>Производственная компания</h2>"
             "<p>Полный цикл производства стрейч-плёнки и скотча на европейском сырье.</p>"
             "<p>Каждая партия перед продажей проходит двойное тестирование.</p>"
             "<p>Офис и склад в Москве и Санкт-Петербурге, доставка по всей России.</p></div>")


def test_small_article_widget_does_not_hide_the_page_text():
    # novastretch.ru wraps its cookie dialog in <article>: taking the first
    # <article> as "the content" left the LLM with the cookie text only.
    html = (f"<html><body><nav><a href='/company/'>О компании</a></nav>{CATALOGUE}"
            "<article><p>Аналитические куки выключены.</p></article></body></html>")
    page = P.parse_html("https://novastretch.test/", html)
    text = "\n".join(page.lines)
    assert "двойное тестирование" in text
    assert "О компании" not in text  # navigation is still cut


def test_blog_listing_keeps_every_article_card():
    cards = "".join(f"<article><h3>Новость {i}</h3><p>{i:02d}.09.2026 Запустили линию номер {i}.</p></article>"
                    for i in range(1, 7))
    page = P.parse_html("https://blog.test/news/", f"<html><body><div>{cards}</div></body></html>", "news")
    text = "\n".join(page.lines)
    assert all(f"Запустили линию номер {i}." in text for i in range(1, 7))


def test_main_element_still_wins_when_it_holds_the_content():
    html = (f"<html><body><div class='promo'><p>Скидка дня на перчатки.</p></div><main>{CATALOGUE}</main>"
            "</body></html>")
    page = P.parse_html("https://shop.test/", html)
    text = "\n".join(page.lines)
    assert "двойное тестирование" in text
    assert "Скидка дня" not in text


# --- CSV round trip (resume) ------------------------------------------------------- #

def test_resume_reads_back_quoted_words_after_the_sniffed_sample(tmp_path):
    # csv.Sniffer guesses doublequote=False when the first 4 KB hold no "" pair;
    # resume then cut every later comment with a quoted word (ГЕНЕЗИС: «компании "Генезис"»).
    path = tmp_path / "out.csv"
    fields = ["company", "site", P.COL_PERSONALIZATION, P.COL_COMMENT]
    rows = [{"company": f"Компания {i}", "site": f"c{i}.ru", P.COL_PERSONALIZATION: "Увидели, что вы растёте.",
             P.COL_COMMENT: "страниц прочитано: 3 | " + "без кавычек " * 40} for i in range(12)]
    rows.append({"company": "ГЕНЕЗИС", "site": "gnzs.ru", P.COL_PERSONALIZATION: "Увидели, что вы пишете.",
                 P.COL_COMMENT: 'основание: «директор компании "Генезис" Что делать после внедрения CRM» | дата'})
    P.write_rows(path, fields, rows)
    assert path.read_text(encoding="utf-8-sig").find('""') > 4096  # the trap: no "" in the sample
    back, header = P.read_rows(path)
    assert header == fields
    assert back == rows


def test_semicolon_csv_from_excel_is_still_detected(tmp_path):
    path = tmp_path / "in.csv"
    path.write_bytes('компания;сайт;почта\r\nООО "Ромашка";romashka.ru;info@romashka.ru\r\n'.encode("cp1251"))
    back, header = P.read_rows(path)
    assert header == ["компания", "сайт", "почта"]
    assert back[0]["компания"] == 'ООО "Ромашка"'
