"""Shared fixtures: no real network, no real LLM."""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import personalize as P  # noqa: E402

SITE = "https://acme-stanki.ru"

HOME_HTML = """<html><head><title>Акме Станки — станки с ЧПУ</title>
<meta name="description" content="станки, ЧПУ, купить станок, цена"></head>
<body>
<header><nav>
  <a href="/about/">О компании</a> <a href="/news/">Новости</a>
  <a href="/catalog/press-machines/">Прессы</a> <a href="https://other.example/">Партнёр</a>
</nav></header>
<main><h1>Акме Станки</h1><p>Поставляем оборудование.</p></main>
<footer>© 2026 ООО «Акме Станки». Все права защищены. sales@acme-stanki.ru</footer>
</body></html>"""

ABOUT_HTML = """<html><head><title>О компании — Акме Станки</title></head><body><main>
<p>Компания «Акме Станки» основана в 2009 году в Екатеринбурге и производит токарные станки с ЧПУ
для металлообработки.</p>
<p>Мы поставляем оборудование на 140 предприятий в России и Казахстане.</p>
</main></body></html>"""

NEWS_HTML = """<html><head><title>Новости — Акме Станки</title></head><body><main>
<p>12.09.2026 Запустили участок лазерной резки мощностью 30 кВт на площадке в Екатеринбурге.</p>
</main></body></html>"""

TODAY = date(2026, 10, 3)  # freshness checks are pinned to the day of the test task

VALID_ANSWER = {
    "personalization": "Увидели, что вы с 2009 года производите токарные станки с ЧПУ в Екатеринбурге.",
    "source_url": f"{SITE}/about/",
    "evidence": "основана в 2009 году в Екатеринбурге и производит токарные станки с ЧПУ",
    "reason": "",
}

# The freshest fact of the acme site: a dated news item (no freshness nudge).
NEWS_ANSWER = {
    "personalization": "Увидели, что в сентябре 2026 вы запустили участок лазерной резки на 30 кВт в Екатеринбурге.",
    "source_url": f"{SITE}/news/",
    "evidence": "12.09.2026 Запустили участок лазерной резки мощностью 30 кВт на площадке в Екатеринбурге",
    "date": "2026-09-12",
    "reason": "",
}


@pytest.fixture(autouse=True)
def pinned_today(monkeypatch):
    monkeypatch.setattr(P, "today", lambda: TODAY)


class FakeBackend:
    """LLM stand-in: returns queued answers (the last one repeats) and records prompts."""

    name = "fake"

    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts: list[str] = []

    def complete(self, system: str, user: str) -> str:
        self.prompts.append(user)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)


@pytest.fixture
def fetcher(tmp_path):
    f = P.Fetcher(cache_dir=tmp_path / "cache", delay=0, retries=1, backoff=0)
    yield f
    f.close()


def no_robots_txt(respx_mock, *sites):
    """The made-up sites have no robots.txt: the file answers 404, so nothing is closed.

    The fetcher asks robots.txt of a host before its first page and cannot be told not to;
    a test that mocks single pages registers the file here.
    """
    import httpx

    for site in sites:
        respx_mock.get(f"{site}/robots.txt").mock(return_value=httpx.Response(404))


@pytest.fixture
def acme_site(respx_mock):
    """A small, well-formed company site; everything else (robots.txt too) answers 404."""
    import httpx

    respx_mock.get(f"{SITE}/").mock(return_value=httpx.Response(200, html=HOME_HTML))
    respx_mock.get(f"{SITE}/about/").mock(return_value=httpx.Response(200, html=ABOUT_HTML))
    respx_mock.get(f"{SITE}/news/").mock(return_value=httpx.Response(200, html=NEWS_HTML))
    respx_mock.route().mock(return_value=httpx.Response(404))
    return respx_mock


def make_ctx(rows, fetcher, backend, **kw):
    issues, hints = P.cross_row_checks(rows)
    return P.Context(fetcher=fetcher, backend=backend, rows=rows, cross_issues=issues,
                     cross_hints=hints, **{"max_pages": 3, **kw})


def acme_pages(with_news: bool = False) -> list:
    pages = [P.parse_html(f"{SITE}/", HOME_HTML, "home"), P.parse_html(f"{SITE}/about/", ABOUT_HTML, "about")]
    if with_news:
        pages.append(P.parse_html(f"{SITE}/news/", NEWS_HTML, "news"))
    return pages
