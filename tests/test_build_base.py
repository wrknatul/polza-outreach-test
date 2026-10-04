"""The lead base (tools/build_base_common.py, build_base_all.py, assemble_leads.py, merge_lpr.py).

Made-up companies and people throughout; no page is fetched and no DNS query is made.
"""

from __future__ import annotations

import csv
import gzip
import re
import sys
from pathlib import Path
from urllib.parse import urljoin

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import leadfinder as L
import personalize as P

import assemble_leads as A
import build_base_all as B
import build_base_common as C
import merge_lpr as M

SITE = "https://acme-stanki.ru"
STAFF = f"{SITE}/company/staff/"
CARD = ('<div class="card"><b>Петров Иван Сергеевич</b><p>Коммерческий директор</p>'
        '<a href="mailto:petrov@acme-stanki.ru">petrov@acme-stanki.ru</a></div>')
HOME = ('<html><head><title>Акме Станки</title><meta name="description" content="Поставляем станки по всей России">'
        '</head><body><h1>Отдел продаж: оставьте заявку</h1></body></html>')


def lead(**overrides) -> dict:
    """A lead row of task1_leads.csv for a made-up company."""
    row = {
        "company": "Акме Станки", "компания_в_письме": "Акме Станки", "site": SITE, "city": "Москва",
        "segment": "Промоборудование: станки", "sales_signal": "«Отдел продаж: оставьте заявку»",
        "sales_signal_url": f"{SITE}/", "signal_check": "Отдел продаж: оставьте заявку",
        "имя_ЛПР": "Петров Иван Сергеевич", "должность_ЛПР": "Коммерческий директор",
        "Имя": "Иван", "Отчество": "Сергеевич", "Фамилия": "Петров", "должность_в_письме": "коммерческий директор",
        "email": "petrov@acme-stanki.ru", "тип_адреса": C.NAMED_BOX, "источник": STAFF,
        "дата_страницы": "на странице не указана", "дата_источника_имени": "страница без даты",
        "обращаться_по_имени": "да", "телефон": "+7 (495) 000-00-00", "оговорка": "", "batch": "test",
    }
    return {**row, **overrides}


def site(staff: str = CARD, home: str = HOME):
    """fetch_page / mx stand-ins for one made-up site."""
    pages = {STAFF: staff, f"{SITE}/": home}
    return pages.get, lambda domain: (True, [f"10 mx.{domain}."])


def problems_of(row: dict, staff: str = CARD, home: str = HOME) -> list[str]:
    fetch_page, mx = site(staff, home)
    return C.check_lead(row, fetch_page, mx)[0]


# --- one lead ------------------------------------------------------------------------- #

def test_named_address_in_the_person_card_is_a_lead():
    fetch_page, mx = site()
    problems, facts = C.check_lead(lead(), fetch_page, mx)
    assert problems == []
    assert facts["gap"] is not None and facts["gap"] < 60 and facts["mx"].startswith("10 mx.")


def test_address_given_only_as_a_mailto_link_does_not_make_a_lead():
    # «Написать письмо» in a card: the page never prints the address, so whose mailbox it is cannot be seen
    card = '<div><b>Иван Петров</b> Директор <a href="mailto:dir@acme-stanki.ru?subject=x">Написать письмо</a></div>'
    row = lead(email="dir@acme-stanki.ru", тип_адреса=C.ROLE_BOX, имя_ЛПР="Иван Петров", Отчество="",
               должность_ЛПР="Директор", должность_в_письме="директор")
    assert "dir@acme-stanki.ru" in C.contact_text(card) and not C.email_is_printed("dir@acme-stanki.ru", card)
    problems = problems_of(row, staff=card)
    assert len(problems) == 1 and "только в ссылке mailto" in problems[0]

    # the same link with the address as its text is a printed address; the link counts where it stands
    printed = card.replace("Написать письмо", "dir@acme-stanki.ru")
    assert problems_of(row, staff=printed) == []
    # entities are how some sites hide addresses from scrapers; a browser shows them
    assert C.email_is_printed("dir@acme-stanki.ru", "<p>dir&#64;acme-stanki&#46;ru</p>")


def test_job_title_must_stand_next_to_the_address():
    # the page prints the name and the address, but the role is named only elsewhere (an old news item)
    bare = "<div>Петров Иван Сергеевич <a href='mailto:petrov@acme-stanki.ru'>petrov@acme-stanki.ru</a></div>"
    problems = problems_of(lead(), staff=bare)
    assert len(problems) == 1 and "должности «коммерческий директор» нет рядом с адресом" in problems[0]

    far = "<p>Коммерческий директор</p><p>" + "слово " * 80 + "</p>" + bare
    assert any("нет рядом с адресом" in p for p in problems_of(lead(), staff=far))
    # the short title of the letter is enough when the site spells the full one differently
    short = lead(должность_ЛПР="Директор по продажам и развитию", должность_в_письме="директор по продажам")
    card = "<div>Петров Иван Сергеевич, директор по продажам (Урал), petrov@acme-stanki.ru</div>"
    fetch_page, mx = site(staff=card)
    problems, facts = C.check_lead(short, fetch_page, mx)
    assert problems == [] and facts["title_gap"] is not None and facts["title_gap"] < 40


def test_surname_far_from_the_address_is_not_a_lead():
    far = "<p>Петров Иван Сергеевич</p>" + "<p>" + "слово " * 80 + "</p><p>petrov@acme-stanki.ru</p>"
    assert any("дальше 350 знаков" in p for p in problems_of(lead(), staff=far))
    assert C.name_distance("petrov@acme-stanki.ru", "Петров", far) is None


def test_address_missing_from_the_page_is_not_a_lead():
    assert any("адреса нет на странице" in p for p in problems_of(lead(), staff="<p>Петров Иван Сергеевич</p>"))
    # presales@ is another mailbox, not sales@
    assert not C.email_on_page("sales@acme-stanki.ru", "<p>presales@acme-stanki.ru</p>")


@pytest.mark.parametrize("box", ["info", "sales", "zakaz", "office", "mail"])
def test_generic_mailbox_in_a_card_does_not_make_a_lead(box):
    card = f"<div>Петров Иван Сергеевич, коммерческий директор, {box}@acme-stanki.ru</div>"
    row = lead(email=f"{box}@acme-stanki.ru", тип_адреса=C.ROLE_BOX)
    assert any("общий ящик" in p for p in problems_of(row, staff=card))


def test_second_mail_domain_needs_another_address_on_the_same_page():
    alone = "<div>Коммерческий директор Петров Иван Сергеевич petrov@acme-mail.ru</div>"
    used = alone + "<footer>Приёмная: priemnaya@acme-mail.ru</footer>"
    row = lead(email="petrov@acme-mail.ru")
    assert any("почтовый домен acme-mail.ru" in p for p in problems_of(row, staff=alone))
    assert problems_of(row, staff=used) == []


def test_free_mail_domain_must_be_labelled_as_such():
    card = "<div>Петров Иван Сергеевич, коммерческий директор: petrov.acme@mail.ru</div>"
    assert any("бесплатный почтовый домен" in p for p in problems_of(lead(email="petrov.acme@mail.ru"), staff=card))
    assert problems_of(lead(email="petrov.acme@mail.ru", тип_адреса=C.FREE_BOX), staff=card) == []


def test_source_must_be_on_the_company_site():
    row = lead(источник="https://catalog.example/acme/")
    fetch_page = {"https://catalog.example/acme/": CARD, f"{SITE}/": HOME}.get
    problems = C.check_lead(row, fetch_page, lambda d: (True, ["10 mx."]))[0]
    assert any("не на сайте компании" in p for p in problems)


def test_no_mx_and_no_signal_are_reported():
    fetch_page, _ = site()
    problems = C.check_lead(lead(signal_check="Запишитесь на демо"), fetch_page, lambda d: (False, []))[0]
    assert any("нет MX" in p for p in problems) and any("Запишитесь на демо" in p for p in problems)


def test_signal_may_stand_in_the_title_or_the_meta_description():
    assert C.signal_on_page("Поставляем станки по всей России", HOME)   # meta description
    assert C.signal_on_page("отдел продаж:  оставьте заявку", HOME)       # case and spaces do not matter
    assert not C.signal_on_page("", HOME)


def test_silent_page_is_reported_as_no_answer():
    problems = problems_of(lead(), staff="", home="")
    assert sum("не ответила" in p for p in problems) == 2


def test_cyrillic_host_equals_its_idna_form():
    assert C.host_of("https://технотранс.рф/kontakty") == C.host_of("https://xn--80ajybdmjbd1a.xn--p1ai/")
    assert C.host_of("mgwmachine.com") == "mgwmachine.com" and C.host_of("A@B.Ru") == "b.ru"
    assert C.same_site("shop.acme.ru", "acme.ru") and not C.same_site("acme.ru", "notacme.ru")
    assert M.same_site("https://технотранс.рф", "https://xn--80ajybdmjbd1a.xn--p1ai/managment")


PAGE = b"<html>" + b"x" * 2000 + b"</html>"


def fake_web(monkeypatch, answers: dict) -> list[str]:
    """Replace curl: `answers` is {url: (body bytes, «200» or «301 https://target/») or a curl exit code}.

    A URL that is not listed answers 404. Returns the list of the URLs that were requested.
    """
    asked: list[str] = []

    def run(cmd, **kw):
        asked.append(cmd[-1])
        answer = answers.get(cmd[-1], (b"not found", "404"))
        if isinstance(answer, int):
            return C.subprocess.CompletedProcess(cmd, answer, b"\n000 ", b"")
        return C.subprocess.CompletedProcess(cmd, 0, answer[0] + b"\n" + answer[1].encode(), b"")

    monkeypatch.setattr(C.subprocess, "run", run)
    monkeypatch.setattr(C.time, "sleep", lambda s: None)
    for name in ("_cache", "_errors", "_robots"):
        monkeypatch.setattr(C, name, {})
    return asked


def test_fetch_takes_only_a_2xx_answer(monkeypatch):
    asked = fake_web(monkeypatch, {"https://blocked.example/": (PAGE, "403"), "https://open.example/": (PAGE, "200")})
    assert C.fetch("https://blocked.example/") == "" and C.fetch_error("https://blocked.example/") == "HTTP 403"
    assert asked.count("https://blocked.example/") == 4                # an error page is asked again, then given up
    assert "xxx" in C.fetch("https://open.example/") and C.fetch_error("https://open.example/") == ""


# --- robots.txt ----------------------------------------------------------------------- #

FORPOST = "User-Agent: *\nDisallow: /bitrix/\nDisallow: /contacts/*\nAllow: /contacts/\nSitemap: https://x/s.xml\n"


def test_longest_robots_rule_wins_and_allow_wins_a_tie():
    rules = C.RobotsRules(FORPOST)
    # «Disallow: /contacts/*» is one character longer than «Allow: /contacts/»: the page is closed
    assert rules.verdict("https://forpost.example/contacts/") == (False, "Disallow: /contacts/*")
    assert rules.verdict("https://forpost.example/about/") == (True, "")
    tie = C.RobotsRules("User-agent: *\nDisallow: /team\nAllow: /team\n")
    assert tie.verdict("https://x.example/team/") == (True, "Allow: /team")


def test_robots_wildcards_groups_and_the_common_question_mark_rule():
    rules = C.RobotsRules("User-agent: Yandex\nDisallow: /\n\nUser-agent: *\nDisallow: /?\nDisallow: /*.pdf$\n"
                          "Disallow: /company/staff/\nDisallow:\n")
    assert rules.allows("https://x.example/") and rules.allows("https://x.example/contacts/")   # «/?» is not «/»
    assert not rules.allows("https://x.example/?page=2")
    assert not rules.allows("https://x.example/files/price.pdf") and rules.allows("https://x.example/price.pdf?v=1")
    assert rules.verdict("https://x.example/company/staff/ivanov/") == (False, "Disallow: /company/staff/")
    # the group of another robot does not apply to a script with a browser User-Agent
    assert C.RobotsRules("User-agent: Googlebot\nDisallow: /\n").allows("https://x.example/contacts/")
    # a Cyrillic rule closes the percent-encoded path of the same page
    cyr = C.RobotsRules("User-agent: *\nDisallow: /контакты\n")
    assert not cyr.allows("https://x.example/%D0%BA%D0%BE%D0%BD%D1%82%D0%B0%D0%BA%D1%82%D1%8B/")


CYR = "/%D0%BA%D0%BE%D0%BD%D1%82%D0%B0%D0%BA%D1%82%D1%8B"  # «/контакты», percent-encoded

ROBOTS_TEXTS = [
    FORPOST,
    "User-agent: *\nDisallow: /?\nDisallow: /*.pdf$\nAllow: /company/$\nDisallow: /company/\n",
    "User-agent: Yandex\nDisallow: /\n\nUser-agent: *\nUser-agent: Bingbot\nDisallow: /contacts\nCrawl-delay: 2\n",
    "# no rules at all\nSitemap: https://x.example/sitemap.xml\n",
    "User-agent: *\nDisallow: /\nAllow: /contacts/\n",
    # a byte-order mark before the first line: as it is, and read as cp1251 and as latin-1
    "\ufeffUser-agent: *\nDisallow: /contacts\n",
    "п»їUser-agent: *\nDisallow: /company/\n",
    "ï»¿User-agent: *\nDisallow: /bitrix/\n",
    "Sitemap: https://x.example/s.xml\n\ufeffUser-agent: *\nDisallow: /contacts\n",  # two files glued together
    # Cyrillic: the rule as it is, the rule percent-encoded (hex digits in both cases)
    "User-agent: *\nDisallow: /контакты\n",
    f"User-agent: *\nDisallow: {CYR}/\nAllow: {CYR.lower()}/москва\n",
    # a percent-encoded reserved character is not the character: «%2F» is not «/», «%2A» is not the wildcard
    "User-agent: *\nDisallow: *%2F\n",
    "User-agent: *\nDisallow: /a%2fb\nDisallow: /c%6Fntacts/moscow\nDisallow: /file%2A\n",
    "User-agent: *\nDisallow: /a/b\nDisallow: /price list\n",
    # «*» and «$»
    "User-agent: *\nDisallow: /*/staff/$\nAllow: /company/*.pdf$\nDisallow: /company/*\nDisallow: *?page=\n",
    "User-agent: *\nDisallow: /*a*a*a*a*a*a*a*a*a*a*a*a*a*a*a*a*a*a*a*a*b$\n",
    # an empty Disallow closes nothing, an empty Allow opens nothing
    "User-agent: *\nDisallow:\n",
    "User-agent: *\nDisallow: /\nAllow:\n",
    # the longest rule wins whatever the order, Allow wins a tie
    "User-agent: *\nAllow: /contacts/moscow\nDisallow: /contacts/\nAllow: /contacts\n",
    "User-agent: *\nDisallow: /team\nAllow: /team\n",
    f"User-agent: *\nDisallow: /контакты/москва\nAllow: {CYR}/\n",
    # a group without a name is nobody's group
    "User-agent:\nAllow: /\n\nUser-agent: *\nDisallow: /contacts\n",
]
ROBOTS_PATHS = (
    "/", "/contacts", "/contacts/", "/contacts/moscow/", "/company/", "/company/staff/", "/about/team.pdf",
    "/?utm=1", "/bitrix/admin/", "/news/2026/?page=2", "/team", "/team/ivanov", "/company/price.pdf",
    "/company/price.pdf?v=2", "/контакты", "/контакты/", "/контакты/москва", "/контакт", CYR, CYR + "/",
    CYR.lower() + "/", CYR + "/%D0%BC%D0%BE%D1%81%D0%BA%D0%B2%D0%B0", "/a/b", "/a%2Fb", "/a%2fb", "/x%2Fy/",
    "/c%6Fntacts/moscow", "/file-1", "/file%2A", "/file*", "/price%20list.xlsx", "/price list.xlsx",
    "/" + "a" * 300, "/" + "a" * 300 + "b")


def test_tools_have_no_matcher_of_their_own():
    # one parser, one matcher, one spelling of a rule in the whole project: nothing to keep in step
    assert issubclass(C.RobotsRules, P.RobotsRules) and C.RobotsRules.verdict is P.RobotsRules.verdict
    assert C.robots_key is P.robots_key and C.robots_match is P.robots_match and C.ROBOTS_BOMS is P.ROBOTS_BOMS
    assert P.robots_token(C.UA) == ""  # a browser User-Agent names no robot: only «User-agent: *» applies


def test_tools_read_robots_txt_without_third_party_packages():
    # the tools take the robots.txt code from personalize.py; that part of it must not need httpx or bs4
    code = ("import sys\n"
            "for name in ('httpx', 'bs4', 'truststore'):\n"
            "    sys.modules[name] = None  # «import httpx» now fails, as on a machine without the package\n"
            "sys.path.insert(0, 'tools')\n"
            "import build_base_common as C\n"
            "assert C.P.httpx is None\n"
            "answer = C.P.read_robots(200, {}, b'User-agent: *\\nDisallow: /private/\\n', C.UA)\n"
            "print(answer.state, answer.rules.verdict('https://x.example/a/%2E%2E/private/x'))\n")
    done = C.subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=C.ROOT, check=False)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "rules (False, 'Disallow: /private/')"


@pytest.mark.parametrize("text", ROBOTS_TEXTS)
def test_tools_matcher_agrees_with_the_matcher_of_personalize(text):
    urls = [f"https://x.example{path}" for path in ROBOTS_PATHS]
    ours, theirs = C.RobotsRules(text), P.RobotsRules(text, C.UA)
    assert [ours.allows(u) for u in urls] == [theirs.allows(u) for u in urls]


@pytest.mark.parametrize("text, path, allowed", [
    # a byte-order mark does not hide the first group
    ("\ufeffUser-agent: *\nDisallow: /contacts\n", "/contacts/", False),
    ("п»їUser-agent: *\nDisallow: /company/\n", "/company/staff/", False),
    ("ï»¿User-agent: *\nDisallow: /bitrix/\n", "/bitrix/admin/", False),
    ("\ufeffUser-agent: *\nDisallow: /contacts\n", "/about/", True),
    ("Sitemap: https://x.example/s.xml\n\ufeffUser-agent: *\nDisallow: /contacts\n", "/contacts/", False),
    # Cyrillic: a rule as it is closes the percent-encoded path, and the other way round
    ("User-agent: *\nDisallow: /контакты\n", CYR + "/", False),
    ("User-agent: *\nDisallow: /контакты\n", CYR.lower() + "/", False),
    ("User-agent: *\nDisallow: /контакты\n", "/контакты/", False),
    ("User-agent: *\nDisallow: /контакты\n", "/контакт", True),
    (f"User-agent: *\nDisallow: {CYR}/\n", "/контакты/", False),
    (f"User-agent: *\nDisallow: {CYR.lower()}/\n", "/контакты/", False),
    (f"User-agent: *\nDisallow: {CYR.lower()}/\n", CYR + "/", False),
    (f"User-agent: *\nDisallow: {CYR}/\nAllow: {CYR.lower()}/москва\n", "/контакты/москва", True),
    # a percent-encoded unreserved character is the character itself, a space is «%20»
    ("User-agent: *\nDisallow: /c%6Fntacts/moscow\n", "/contacts/moscow/", False),
    ("User-agent: *\nDisallow: /contacts/moscow\n", "/c%6Fntacts/moscow", False),
    ("User-agent: *\nDisallow: /price list\n", "/price%20list.xlsx", False),
    # «%2F» stays encoded: «Disallow: *%2F» does not close the site
    ("User-agent: *\nDisallow: *%2F\n", "/", True),
    ("User-agent: *\nDisallow: *%2F\n", "/contacts/moscow/", True),
    ("User-agent: *\nDisallow: *%2F\n", "/x%2Fy/", False),
    ("User-agent: *\nDisallow: *%2F\n", "/a%2fb", False),
    ("User-agent: *\nDisallow: /a%2fb\n", "/a/b", True),
    ("User-agent: *\nDisallow: /a%2fb\n", "/a%2Fb", False),
    ("User-agent: *\nDisallow: /a/b\n", "/a%2Fb", True),
    ("User-agent: *\nDisallow: /file%2A\n", "/file-1", True),
    ("User-agent: *\nDisallow: /file%2A\n", "/file%2A", False),
    # «*» and «$»
    ("User-agent: *\nDisallow: /*/staff/$\n", "/company/staff/", False),
    ("User-agent: *\nDisallow: /*/staff/$\n", "/company/staff/ivanov", True),
    ("User-agent: *\nDisallow: /company/*\nAllow: /company/*.pdf$\n", "/company/price.pdf", True),
    ("User-agent: *\nDisallow: /company/*\nAllow: /company/*.pdf$\n", "/company/price.pdf?v=2", False),
    ("User-agent: *\nDisallow: *?page=\n", "/news/2026/?page=2", False),
    ("User-agent: *\nDisallow: /*a*a*a*a*a*a*a*a*a*a*a*a*a*a*a*a*a*a*a*a*b$\n", "/" + "a" * 300, True),
    ("User-agent: *\nDisallow: /*a*a*a*a*a*a*a*a*a*a*a*a*a*a*a*a*a*a*a*a*b$\n", "/" + "a" * 300 + "b", False),
    # an empty Disallow closes nothing, an empty Allow opens nothing
    ("User-agent: *\nDisallow:\n", "/contacts/", True),
    ("User-agent: *\nDisallow: /\nAllow:\n", "/contacts/", False),
    # the longest rule wins whatever the order, Allow wins a tie
    ("User-agent: *\nAllow: /contacts/moscow\nDisallow: /contacts/\nAllow: /contacts\n", "/contacts/", False),
    ("User-agent: *\nAllow: /contacts/moscow\nDisallow: /contacts/\nAllow: /contacts\n", "/contacts/moscow/", True),
    ("User-agent: *\nAllow: /contacts/moscow\nDisallow: /contacts/\nAllow: /contacts\n", "/contacts", True),
    ("User-agent: *\nDisallow: /team\nAllow: /team\n", "/team/ivanov", True),
    # ... and the length is counted in one spelling: the Cyrillic Disallow is the longer rule here, not the Allow
    (f"User-agent: *\nDisallow: /контакты/москва\nAllow: {CYR}/\n", "/контакты/москва", False),
    (f"User-agent: *\nDisallow: /контакты/москва\nAllow: {CYR}/\n", "/контакты/тула", True),
    # a group without a name is nobody's group: «User-agent: *» still applies
    ("User-agent:\nAllow: /\n\nUser-agent: *\nDisallow: /contacts\n", "/contacts/", False),
])
def test_both_matchers_give_the_verdict_the_standard_asks_for(text, path, allowed):
    url = f"https://x.example{path}"
    assert C.RobotsRules(text).allows(url) is allowed
    assert P.RobotsRules(text, C.UA).allows(url) is allowed
    assert P.RobotsRules(text, P.DEFAULT_UA).allows(url) is allowed  # the robot of personalize.py has no group here


def test_rule_that_decided_is_named_as_the_site_wrote_it():
    assert C.RobotsRules("User-agent: *\nDisallow: /контакты\n").verdict(f"https://x.example{CYR}/") \
        == (False, "Disallow: /контакты")
    assert C.RobotsRules(f"User-agent: *\nDisallow: {CYR.lower()}\n").verdict("https://x.example/контакты/") \
        == (False, f"Disallow: {CYR.lower()}")
    # the length of a rule is counted in one spelling: the encoded Allow is longer than the Cyrillic Disallow
    both = C.RobotsRules(f"User-agent: *\nDisallow: /контакты\nAllow: {CYR}/\n")
    assert both.verdict("https://x.example/контакты/") == (True, f"Allow: {CYR}/")
    assert both.verdict("https://x.example/контакты") == (False, "Disallow: /контакты")


def test_byte_order_mark_and_encoded_slash_in_a_fetched_robots_txt(monkeypatch):
    asked = fake_web(monkeypatch, {
        "https://bom.example/robots.txt": ("\ufeffUser-agent: *\nDisallow: /contacts/\n".encode(), "200"),
        "https://bom.example/contacts/": (PAGE, "200"),
        "https://slash.example/robots.txt": (b"User-agent: *\nDisallow: *%2F\n", "200"),
        "https://slash.example/contacts/": (PAGE, "200")})
    assert C.fetch("https://bom.example/contacts/") == "" and "https://bom.example/contacts/" not in asked
    assert C.fetch_error("https://bom.example/contacts/") == "закрыта в robots.txt сайта (Disallow: /contacts/)"
    assert "xxx" in C.fetch("https://slash.example/contacts/")          # «*%2F» is not «*/»: the site stays open


def test_a_page_closed_in_robots_txt_is_never_requested(monkeypatch):
    site = "https://forpost.example"
    asked = fake_web(monkeypatch, {f"{site}/robots.txt": (FORPOST.encode(), "200"), f"{site}/contacts/": (PAGE, "200"),
                                   f"{site}/about/": (PAGE, "200")})
    assert C.fetch(f"{site}/contacts/") == ""
    assert C.fetch_error(f"{site}/contacts/") == "закрыта в robots.txt сайта (Disallow: /contacts/*)"
    assert "xxx" in C.fetch(f"{site}/about/")
    assert f"{site}/contacts/" not in asked
    assert asked.count(f"{site}/robots.txt") == 1                      # the file is read once per host


def test_redirect_target_is_checked_against_robots_txt_too(monkeypatch):
    site = "https://moved.example"
    asked = fake_web(monkeypatch, {
        f"{site}/robots.txt": (b"User-agent: *\nDisallow: /private/\n", "200"),
        f"{site}/team": (b"", f"301 {site}/private/team/"), f"{site}/private/team/": (PAGE, "200"),
        f"{site}/old": (b"", f"302 {site}/new/"), f"{site}/new/": (PAGE, "200")})
    assert C.get_page(f"{site}/team") == ("", "закрыта в robots.txt сайта (Disallow: /private/)")
    assert f"{site}/private/team/" not in asked
    assert "xxx" in C.get_page(f"{site}/old")[0] and asked[-1] == f"{site}/new/"


def test_site_without_robots_txt_closes_nothing_and_an_unreadable_one_closes_the_host(monkeypatch):
    asked = fake_web(monkeypatch, {
        "https://none.example/": (PAGE, "200"),                                         # robots.txt -> 404
        "https://html.example/robots.txt": (b"<!DOCTYPE html><html>404</html>", "200"),
        "https://html.example/": (PAGE, "200"),
        "https://down.example/robots.txt": (b"oops", "503"), "https://down.example/": (PAGE, "200"),
        "https://dead.example/robots.txt": 28, "https://dead.example/": (PAGE, "200")})
    assert "xxx" in C.fetch("https://none.example/") and "xxx" in C.fetch("https://html.example/")
    assert C.fetch("https://down.example/") == "" and C.fetch("https://dead.example/") == ""
    assert C.fetch_error("https://down.example/") == "robots.txt сайта не получен (HTTP 503)"
    assert C.fetch_error("https://dead.example/") == "robots.txt сайта не получен (curl: код 28)"
    assert "https://down.example/" not in asked and "https://dead.example/" not in asked
    assert C.robots_summary() == "robots.txt: сайтов 4 — с правилами 0, без файла 2, файл не получен 2"


def test_lead_whose_source_page_is_closed_in_robots_txt_is_rejected(monkeypatch):
    asked = fake_web(monkeypatch, {f"{SITE}/robots.txt": (b"User-agent: *\nDisallow: /company/staff/\n", "200"),
                                   STAFF: (CARD.encode() + PAGE, "200"), f"{SITE}/": (HOME.encode() + PAGE, "200")})
    kept, report, dropped = C.validate_leads([lead()], mx=lambda domain: (True, ["10 mx."]))
    assert kept == [] and STAFF not in asked
    problems = dropped[0][1]
    assert problems == [f"страница {STAFF} закрыта в robots.txt сайта (Disallow: /company/staff/) — не открывалась: "
                        f"лид с такой страницы не берётся"]
    # a closed page is a decision of the site, not a dead network
    assert not B.network_is_down(1, dropped)


def test_unreadable_robots_txt_counts_as_a_silent_page(monkeypatch):
    fake_web(monkeypatch, {f"{SITE}/robots.txt": (b"", "502")})
    problems = C.check_lead(lead(), mx=lambda domain: (True, ["10 mx."]))[0]
    assert sum("не ответила (robots.txt сайта не получен (HTTP 502))" in p for p in problems) == 2
    assert B.network_is_down(1, [(lead(), problems)])


# --- robots.txt: the address that is checked is the address that is requested --------- #

DOTS = "https://dots.example"
CLOSED = b"User-agent: *\nDisallow: /private/\n"


@pytest.mark.parametrize("dotted", [
    "/a/../private/x", "/a/%2E%2E/private/x", "/a/%2e%2e/private/x", "/a/.%2E/private/x", "/./private/x",
    "/open/../private/./x", "/private/y/../x",
])
def test_dot_segments_do_not_lead_around_a_rule(monkeypatch, dotted):
    # curl drops «/../» before it sends an address: the rules must be asked about what it will send
    asked = fake_web(monkeypatch, {f"{DOTS}/robots.txt": (CLOSED, "200"), f"{DOTS}/private/x": (PAGE, "200")})
    assert C.get_page(DOTS + dotted) == ("", "закрыта в robots.txt сайта (Disallow: /private/)")
    assert C.fetch(DOTS + dotted) == "" and C.fetch_error(DOTS + dotted).startswith(C.ROBOTS_CLOSED)
    assert asked == [f"{DOTS}/robots.txt"]  # the page was not requested, in any spelling


def test_address_with_dot_segments_is_requested_in_the_spelling_that_was_checked(monkeypatch):
    asked = fake_web(monkeypatch, {f"{DOTS}/robots.txt": (CLOSED, "200"), f"{DOTS}/about/": (PAGE, "200")})
    for dotted in ("/private/../about/", "/a/%2E%2E/about/", "/about/."):
        assert "xxx" in C.get_page(DOTS + dotted)[0]
    assert asked == [f"{DOTS}/robots.txt"] + [f"{DOTS}/about/"] * 3


@pytest.mark.parametrize("target", [f"{DOTS}/a/../private/x", f"{DOTS}/a/%2E%2E/private/x", f"{DOTS}/a/.%2e/private/x"])
def test_redirect_through_dot_segments_into_a_closed_path_is_not_followed(monkeypatch, target):
    asked = fake_web(monkeypatch, {f"{DOTS}/robots.txt": (CLOSED, "200"), f"{DOTS}/team": (b"", f"302 {target}"),
                                   f"{DOTS}/private/x": (PAGE, "200"),
                                   f"{DOTS}/old": (b"", f"301 {DOTS}/private/%2E%2E/new/"),
                                   f"{DOTS}/new/": (PAGE, "200")})
    assert C.get_page(f"{DOTS}/team") == ("", "закрыта в robots.txt сайта (Disallow: /private/)")
    assert asked == [f"{DOTS}/robots.txt", f"{DOTS}/team"]
    assert "xxx" in C.get_page(f"{DOTS}/old")[0] and asked[2:] == [f"{DOTS}/old", f"{DOTS}/new/"]


def test_lead_whose_source_is_written_with_dot_segments_into_a_closed_path_is_rejected(monkeypatch):
    asked = fake_web(monkeypatch, {f"{SITE}/robots.txt": (b"User-agent: *\nDisallow: /company/staff/\n", "200"),
                                   STAFF: (CARD.encode() + PAGE, "200"), f"{SITE}/": (HOME.encode() + PAGE, "200")})
    dotted = f"{SITE}/about/../company/staff/"
    kept, _, dropped = C.validate_leads([lead(источник=dotted)], mx=lambda domain: (True, ["10 mx."]))
    assert kept == [] and STAFF not in asked and dotted not in asked
    assert "закрыта в robots.txt сайта (Disallow: /company/staff/)" in dropped[0][1][0]


# --- robots.txt: the answer is read by the reader of personalize.py ------------------- #

@pytest.mark.parametrize("body, code", [
    (b"<!-- robots.txt of the site -->\n" + CLOSED, "200"),                     # the first character is «<»
    (b"# the <html> pages of the shop, see <!DOCTYPE html>\n" + CLOSED, "200"),  # the words stand in a remark
    (b"\xef\xbb\xbf  \n<html>\n" + CLOSED + b"</html>\n", "200"),
    (CLOSED, "202"), (CLOSED, "203"), (CLOSED, "206"),
    ("# правила сайта\n".encode("cp1251") + CLOSED, "200"),
])
def test_robots_txt_that_looks_like_html_or_comes_with_another_2xx_is_still_the_rules(monkeypatch, body, code):
    asked = fake_web(monkeypatch, {f"{DOTS}/robots.txt": (body, code), f"{DOTS}/private/x": (PAGE, "200"),
                                   f"{DOTS}/open/x": (PAGE, "200")})
    assert C.get_page(f"{DOTS}/private/x") == ("", "закрыта в robots.txt сайта (Disallow: /private/)")
    assert "xxx" in C.get_page(f"{DOTS}/open/x")[0] and f"{DOTS}/private/x" not in asked
    assert C.robots_summary() == "robots.txt: сайтов 1 — с правилами 1, без файла 0, файл не получен 0"


@pytest.mark.parametrize("name", ["Mozilla", "mozilla", "Chrome", "Safari", "AppleWebKit", "Macintosh", "Mozilla/5.0",
                                  "OutreachResearchBot", "LeadFinderBot", "bot"])
def test_group_named_like_a_part_of_the_browser_user_agent_does_not_replace_the_star_group(monkeypatch, name):
    text = f"User-agent: {name}\nDisallow:\n\nUser-agent: *\nDisallow: /private/\n"
    assert not C.RobotsRules(text).allows(f"{DOTS}/private/x") and C.RobotsRules(text).allows(f"{DOTS}/open/x")
    asked = fake_web(monkeypatch, {f"{DOTS}/robots.txt": (text.encode(), "200"), f"{DOTS}/private/x": (PAGE, "200")})
    assert C.get_page(f"{DOTS}/private/x") == ("", "закрыта в robots.txt сайта (Disallow: /private/)")
    assert asked == [f"{DOTS}/robots.txt"]


def test_curl_follows_the_redirects_of_robots_txt_only_and_only_over_http(monkeypatch):
    commands = []

    def run(cmd, **kw):
        commands.append(cmd)
        body = CLOSED if cmd[-1].endswith("/robots.txt") else PAGE
        return C.subprocess.CompletedProcess(cmd, 0, body + b"\n200 ", b"")

    monkeypatch.setattr(C.subprocess, "run", run)
    for name in ("_cache", "_errors", "_robots"):
        monkeypatch.setattr(C, name, {})
    assert "xxx" in C.get_page(f"{DOTS}/open/x")[0]
    robots, page = commands
    assert robots[-1] == f"{DOTS}/robots.txt" and page[-1] == f"{DOTS}/open/x"
    follow = ["-L", "--max-redirs", str(C.MAX_REDIRECTS), "--proto-redir", "=http,https"]
    assert [arg for arg in robots if arg in follow] == follow  # RFC 9309, 2.3.1.2; never to ftp://
    assert "-L" not in page  # the redirects of a page are followed by hand, robots.txt first
    # «-g»: curl must not read «{a,b}» and «[1-3]» in an address as a list of addresses to request
    proxied = C.curl_command(attempt=1, env={C.PROXY_ENV: "127.0.0.1:1080"})
    assert "-g" in robots and "-g" in page and "-g" in proxied
    # «-q», the first argument: a «location» line in the user's ~/.curlrc must not switch the following back on
    assert robots[:2] == page[:2] == proxied[:2] == ["curl", "-q"]


def test_only_http_addresses_are_requested(monkeypatch):
    asked = fake_web(monkeypatch, {f"{DOTS}/robots.txt": (CLOSED, "200"),
                                   f"{DOTS}/files": (b"", "302 ftp://dots.example/price.zip")})
    assert C.get_page(f"{DOTS}/files") == ("", "адрес нельзя открыть (не http и не https)")
    assert C.get_page("ftp://dots.example/price.zip")[0] == "" and C.get_page("-o /tmp/x")[0] == ""
    assert asked == [f"{DOTS}/robots.txt", f"{DOTS}/files"]  # nothing went to curl but the two web addresses


# --- robots.txt: one list of answers, three fetchers, one decision --------------------- #

HOST, ELSEWHERE = "https://robots-case.example", "https://elsewhere.example"
ROBOTS_TXT, REAL, THEIRS = f"{HOST}/robots.txt", f"{HOST}/robots-real.txt", f"{ELSEWHERE}/robots.txt"
TIMEOUT, BROKEN = "timeout", "broken stream"
HTML_PAGE = ("<html><head><title>Страница</title></head><body><p>" + "Текст страницы сайта. " * 80
             + "</p></body></html>").encode()
STAR = "User-agent: *\nDisallow: /private/\n"
CYR_RULE = "\ufeffUser-agent: *\nDisallow: /контакты\n".encode()
GZIPPED = gzip.compress(STAR.encode(), mtime=0)


def ans(status=200, body=b"", **headers):
    """One HTTP answer of the made-up web: (status, body, headers)."""
    body = body if isinstance(body, bytes) else body.encode()
    return status, body, {name.replace("_", "-"): value for name, value in headers.items()}


def to(target, status=301):
    return ans(status, b"<html><body>moved</body></html>", location=target)


def chain(hops):
    """/robots.txt -> /r1 -> ... -> /r<hops>, which is the file."""
    steps = [ROBOTS_TXT] + [f"{HOST}/r{i}" for i in range(1, hops + 1)]
    return {**{a: to(b, 302) for a, b in zip(steps, steps[1:], strict=False)}, steps[-1]: ans(200, STAR)}


# (what the case is, the answers of the web, the decision every fetcher must come to, the page the rules close)
ROBOTS_ANSWERS = [
    # --- a file with rules, whatever it looks like
    ("rules", {ROBOTS_TXT: ans(200, STAR, content_type="text/plain; charset=utf-8")}, "rules"),
    ("byte-order mark", {ROBOTS_TXT: ans(200, "\ufeff" + STAR)}, "rules"),
    ("CRLF line ends", {ROBOTS_TXT: ans(200, STAR.replace("\n", "\r\n"))}, "rules"),
    ("served as text/html", {ROBOTS_TXT: ans(200, STAR, content_type="text/html; charset=utf-8")}, "rules"),
    ("no content type", {ROBOTS_TXT: ans(200, STAR)}, "rules"),
    ("an HTML comment first: the first character is <", {ROBOTS_TXT: ans(200, "<!-- robots -->\n" + STAR)}, "rules"),
    ("<html> and <!DOCTYPE in a remark", {ROBOTS_TXT: ans(200, "# <html> pages, <!DOCTYPE html>\n" + STAR)}, "rules"),
    ("rules wrapped in an HTML page", {ROBOTS_TXT: ans(200, "<html><body><pre>\n" + STAR + "</pre></body></html>")},
     "rules"),
    ("202 with rules", {ROBOTS_TXT: ans(202, STAR)}, "rules"),
    ("203 with rules", {ROBOTS_TXT: ans(203, STAR)}, "rules"),
    ("206 with rules", {ROBOTS_TXT: ans(206, STAR)}, "rules"),
    ("a remark in cp1251", {ROBOTS_TXT: ans(200, "# правила сайта\n".encode("cp1251") + STAR.encode())}, "rules"),
    ("UTF-8 announced as windows-1251, a Cyrillic rule",
     {ROBOTS_TXT: ans(200, CYR_RULE, content_type="text/plain; charset=windows-1251")}, "rules", "/контакты/x"),
    ("a Cyrillic rule, the page percent-encoded", {ROBOTS_TXT: ans(200, CYR_RULE)}, "rules",
     "/%D0%BA%D0%BE%D0%BD%D1%82%D0%B0%D0%BA%D1%82%D1%8B/x"),
    # curl does not ask for compression and hands a compressed body over as it is; httpx unpacks it itself
    ("gzip nobody asked for", {ROBOTS_TXT: ans(200, GZIPPED, content_encoding="gzip")}, "rules"),
    ("robots.txt.gz served as the file", {ROBOTS_TXT: ans(200, GZIPPED, content_type="application/gzip")}, "rules"),
    # «{a,b}» and «[1-1]» are characters of an address; curl without «-g» would request /private/x and /private1/x
    ("braces in the address are not a list of addresses", {ROBOTS_TXT: ans(200, STAR)}, "open", "/{private,open}/x"),
    ("brackets in the address are not a range", {ROBOTS_TXT: ans(200, "User-agent: *\nDisallow: /private1/\n")},
     "open", "/private[1-1]/x"),
    ("dot segments in the address", {ROBOTS_TXT: ans(200, STAR)}, "rules", "/open/../private/x"),
    ("percent-encoded dot segments in the address", {ROBOTS_TXT: ans(200, STAR)}, "rules", "/open/%2E%2E/private/x"),
    # --- whose group applies: none of these names is the name of a fetcher of ours
    ("group Mozilla with an empty Disallow", {ROBOTS_TXT: ans(200, "User-agent: Mozilla\nDisallow:\n\n" + STAR)},
     "rules"),
    ("groups bot, search, lead, finder that allow everything",
     {ROBOTS_TXT: ans(200, "User-agent: bot\nUser-agent: search\nUser-agent: lead\nUser-agent: finder\nAllow: /\n\n"
                      + STAR)}, "rules"),
    ("another robot closed entirely", {ROBOTS_TXT: ans(200, "User-agent: Googlebot\nDisallow: /\n\n" + STAR)}, "rules"),
    ("a group without a name", {ROBOTS_TXT: ans(200, "User-agent:\nAllow: /\n\n" + STAR)}, "rules"),
    # --- no file: nothing is closed
    ("404", {ROBOTS_TXT: ans(404, "not found")}, "open"),
    ("404 with rules in the body", {ROBOTS_TXT: ans(404, STAR)}, "open"),
    ("410", {ROBOTS_TXT: ans(410)}, "open"),
    ("403", {ROBOTS_TXT: ans(403, "forbidden")}, "open"),
    ("401", {ROBOTS_TXT: ans(401)}, "open"),
    ("400", {ROBOTS_TXT: ans(400)}, "open"),
    ("empty 200", {ROBOTS_TXT: ans(200)}, "open"),
    ("204", {ROBOTS_TXT: ans(204)}, "open"),
    ("only a byte-order mark and spaces", {ROBOTS_TXT: ans(200, "\ufeff  \n\n")}, "open"),
    ("an HTML page instead of the file", {ROBOTS_TXT: ans(200, HTML_PAGE, content_type="text/html")}, "open"),
    ("an HTML page after a byte-order mark and spaces", {ROBOTS_TXT: ans(200, b"\xef\xbb\xbf \n" + HTML_PAGE)}, "open"),
    ("plain text without a directive", {ROBOTS_TXT: ans(200, "Not found")}, "open"),
    ("a remark and nothing else", {ROBOTS_TXT: ans(200, "# robots.txt\n")}, "open"),
    # --- the rules are unknown: the host is closed
    ("429", {ROBOTS_TXT: ans(429, "slow down")}, "closed"),
    ("500", {ROBOTS_TXT: ans(500)}, "closed"),
    ("503 with rules in the body", {ROBOTS_TXT: ans(503, STAR)}, "closed"),
    ("a timeout", {ROBOTS_TXT: TIMEOUT}, "closed"),
    ("a broken stream", {ROBOTS_TXT: BROKEN}, "closed"),
    ("gzip that cannot be unpacked", {ROBOTS_TXT: ans(200, GZIPPED[:10] + b"not a gzip stream at all")}, "closed"),
    # --- redirects of robots.txt itself (RFC 9309, 2.3.1.2)
    ("redirect on the same host", {ROBOTS_TXT: to("/robots-real.txt"), REAL: ans(200, STAR)}, "rules"),
    ("redirect to another site", {ROBOTS_TXT: to(THEIRS), THEIRS: ans(200, STAR)}, "rules"),
    ("redirect with 300 and a Location", {ROBOTS_TXT: to("/robots-real.txt", 300), REAL: ans(200, STAR)}, "rules"),
    ("as many hops as allowed", chain(C.MAX_REDIRECTS), "rules"),
    ("one hop too many", chain(C.MAX_REDIRECTS + 1), "closed"),
    ("a loop", {ROBOTS_TXT: to("/robots.txt", 302)}, "closed"),
    ("302 without a Location", {ROBOTS_TXT: ans(302)}, "closed"),
    ("redirect to ftp://", {ROBOTS_TXT: to("ftp://robots-case.example/robots.txt", 302)}, "closed"),
    ("redirect to a site without the file", {ROBOTS_TXT: to(THEIRS, 302), THEIRS: ans(404)}, "open"),
    ("redirect to an HTML page", {ROBOTS_TXT: to(f"{ELSEWHERE}/"), f"{ELSEWHERE}/": ans(200, HTML_PAGE)}, "open"),
    ("redirect to a file that answers 503", {ROBOTS_TXT: to(THEIRS), THEIRS: ans(503)}, "closed"),
    ("redirect to a file that never answers", {ROBOTS_TXT: to(THEIRS), THEIRS: TIMEOUT}, "closed"),
]


def one_web(monkeypatch, respx_mock, site: dict) -> tuple[list[str], list[str]]:
    """The same made-up web for every fetcher: respx for httpx (personalize.py, leadfinder.py) and a stand-in for
    curl (the tools) that does what curl does — with «-L» it follows the redirects itself, over http(s) only and
    at most «--max-redirs» of them; exit code 28 is a timeout, 18 a broken stream, 47 too many redirects.
    Every address not in `site` is a page. Returns the addresses requested over httpx and with curl."""
    table = {str(httpx.URL(url)): answer for url, answer in site.items()}
    page = ans(200, HTML_PAGE, content_type="text/html; charset=utf-8")
    over_httpx, with_curl = [], []

    def serve(request):
        over_httpx.append(str(request.url))
        answer = table.get(str(request.url), page)
        if answer == TIMEOUT:
            raise httpx.ReadTimeout("timed out")
        if answer == BROKEN:
            raise httpx.RemoteProtocolError("peer closed connection without sending complete message body")
        status, body, headers = answer
        return httpx.Response(status, content=body, headers=headers)

    def globbed(url):
        """The addresses curl requests for one address when «-g» is not given: {a,b} lists and [1-3] ranges."""
        found = re.search(r"\{([^{}]*)\}|\[(\d+)-(\d+)\]", url)
        if not found:
            return [url]
        parts = found[1].split(",") if found[1] is not None else range(int(found[2]), int(found[3]) + 1)
        return [each for part in parts for each in globbed(url[:found.start()] + str(part) + url[found.end():])]

    def curl(cmd, **kw):
        answers = [curl_one(cmd, url) for url in ([cmd[-1]] if "-g" in cmd else globbed(cmd[-1]))]
        return C.subprocess.CompletedProcess(cmd, answers[-1].returncode, b"".join(a.stdout for a in answers), b"")

    def curl_one(cmd, url):
        def done(code, body=b"", status="000", target=""):
            return C.subprocess.CompletedProcess(cmd, code, body + f"\n{status} {target}".encode(), b"")

        follow, hops = "-L" in cmd, 0
        while True:
            with_curl.append(str(httpx.URL(url)))
            answer = table.get(str(httpx.URL(url)), page)
            if answer == TIMEOUT:
                return done(28)
            if answer == BROKEN:
                return done(18, STAR.encode()[:9], "200")
            status, body, headers = answer
            target = urljoin(url, headers["location"]) if 300 <= status < 400 and headers.get("location") else ""
            if not (follow and target):
                return done(0, body, status, target)
            if not target.startswith(("http://", "https://")):
                # real curl: exit code 1 with «--proto-redir =http,https», an FTP connection without it
                return done(1 if "=http,https" in cmd else 7, status=status)
            hops += 1
            if hops > int(cmd[cmd.index("--max-redirs") + 1]):
                return done(47, status=status, target=target)
            url = target

    respx_mock.route().mock(side_effect=serve)
    monkeypatch.setattr(C.subprocess, "run", curl)
    monkeypatch.setattr(C.time, "sleep", lambda s: None)
    for name in ("_cache", "_errors", "_robots"):
        monkeypatch.setattr(C, name, {})
    return over_httpx, with_curl


def decision(get, calls: list[str], closed_path: str) -> str:
    """What a fetcher made of robots.txt, seen from outside: which of the two pages it read."""
    private, public = HOST + closed_path, f"{HOST}/open/x"
    read = (get(private), get(public))
    for url, was_read in zip((private, public), read, strict=True):
        # a page that was refused was not requested, in the spelling it was given in or in any other
        assert (str(httpx.URL(P.request_url(url))) in calls) is was_read, (url, calls)
        assert was_read or str(httpx.URL(url)) not in calls
    return {(False, True): "rules", (True, True): "open", (False, False): "closed"}[read]


@pytest.mark.respx(assert_all_called=False)
@pytest.mark.parametrize("site, expected, closed_path", [
    pytest.param(case[1], case[2], case[3] if len(case) > 3 else "/private/x", id=case[0]) for case in ROBOTS_ANSWERS])
def test_three_fetchers_make_the_same_decision_from_the_same_answer(monkeypatch, respx_mock, site, expected,
                                                                    closed_path):
    over_httpx, with_curl = one_web(monkeypatch, respx_mock, site)
    decisions = {}
    personalize = P.Fetcher(cache_dir=None, delay=0, retries=0, backoff=0, proxy="")
    try:
        decisions["personalize.Fetcher"] = decision(lambda url: personalize.get(url).ok, over_httpx, closed_path)
    finally:
        personalize.close()
    over_httpx.clear()
    leadfinder = L.LeadFetcher(delay=0, retries=0, backoff=0)
    try:
        decisions["leadfinder.LeadFetcher"] = decision(lambda url: leadfinder.get(url).ok, over_httpx, closed_path)
    finally:
        leadfinder.close()
    decisions["build_base_common.get_page"] = decision(lambda url: bool(C.get_page(url)[0]), with_curl, closed_path)
    assert decisions == dict.fromkeys(decisions, expected)
    if (expected, closed_path) != ("open", "/private/x"):  # the page the rules close was requested by nobody
        assert not {f"{HOST}/private/x", f"{HOST}/private1/x"} & set(with_curl)


def test_table_of_robots_answers_covers_every_kind_of_decision():
    kinds = [case[2] for case in ROBOTS_ANSWERS]
    assert {kind: kinds.count(kind) for kind in ("rules", "open", "closed")} == {"rules": 26, "open": 17, "closed": 12}
    assert len({case[0] for case in ROBOTS_ANSWERS}) == len(ROBOTS_ANSWERS) == 55


@pytest.mark.respx(assert_all_called=False)
@pytest.mark.parametrize("name, obeyed_by", [
    ("OutreachResearchBot", "personalize.Fetcher"), ("outreachresearchbot/1.0", "personalize.Fetcher"),
    ("LeadFinderBot", "leadfinder.LeadFetcher"), ("LEADFINDERBOT", "leadfinder.LeadFetcher"),
])
def test_group_with_the_name_of_one_robot_is_obeyed_by_that_robot_alone(monkeypatch, respx_mock, name, obeyed_by):
    # the one place where the tools differ, and on purpose: each obeys the group that names it (RFC 9309, 2.2.1)
    text = f"User-agent: {name}\nDisallow: /\n\nUser-agent: *\nDisallow: /private/\n"
    over_httpx, with_curl = one_web(monkeypatch, respx_mock, {ROBOTS_TXT: ans(200, text)})
    personalize = P.Fetcher(cache_dir=None, delay=0, retries=0, backoff=0, proxy="")
    leadfinder = L.LeadFetcher(delay=0, retries=0, backoff=0)
    try:
        opened = {"personalize.Fetcher": personalize.get(f"{HOST}/open/x").ok,
                  "leadfinder.LeadFetcher": leadfinder.get(f"{HOST}/open/x").ok,
                  "build_base_common.get_page": bool(C.get_page(f"{HOST}/open/x")[0])}
        closed = [personalize.get(f"{HOST}/private/x").ok, leadfinder.get(f"{HOST}/private/x").ok,
                  bool(C.get_page(f"{HOST}/private/x")[0])]
    finally:
        personalize.close()
        leadfinder.close()
    assert opened == {tool: tool != obeyed_by for tool in opened}
    assert closed == [False, False, False]  # closed for everybody: by «Disallow: /» for one, by «*» for the others
    assert f"{HOST}/private/x" not in over_httpx + with_curl


# --- the whole base ------------------------------------------------------------------- #

def test_kept_rows_have_the_pipeline_layout():
    fetch_page, mx = site()
    kept, report, dropped = C.validate_leads([lead()], fetch_page, mx)
    assert [r["company"] for r in kept] == ["Акме Станки"] and dropped == [] and report[0].startswith("OK")
    base = C.lead_to_base_row(kept[0])
    assert list(base) == C.LEAD_FIELDS
    assert base["email"] == "petrov@acme-stanki.ru" and base["email_source"] == base["источник_имени"] == STAFF
    assert base["contact_role"] == "Коммерческий директор — именной"
    assert base["sales_signal"].endswith(f" — {SITE}/")          # enrich_base.py reads the URL after « — »
    assert len(base["компания_в_письме"].split()) <= 3
    part = B.lpr_row(1, kept[0])
    assert list(part) == B.LPR_FIELDS and part["email_ЛПР_на_странице"] == base["email"] and part["row"] == 1


def test_more_than_half_silent_rows_mean_the_network_is_down():
    silent = (lead(), ["страница https://x/ не ответила (curl: код 28)"])
    changed = (lead(), ["адреса нет на странице-источнике"])
    assert B.network_is_down(4, [silent, silent, silent])
    assert not B.network_is_down(4, [silent, silent])           # exactly half is not «more than half»
    assert not B.network_is_down(4, [changed, changed, changed])
    assert not B.network_is_down(0, [])


def test_main_writes_nothing_when_the_network_is_down(tmp_path, monkeypatch, capsys):
    leads = tmp_path / "task1_leads.csv"
    C.write_csv([lead(), lead(company="Бета", site="https://beta.example", источник="https://beta.example/team",
                           sales_signal_url="https://beta.example/")], leads, A.FIELDS)
    out = tmp_path / "base.csv"
    monkeypatch.setattr(B, "LEADS", leads)
    monkeypatch.setattr(B, "fetch_all", lambda urls: None)
    monkeypatch.setattr(B, "validate_leads", lambda rows: C.validate_leads(rows, lambda url: "", lambda d: (True, [])))

    assert B.main(["--out", str(out)]) == 3
    assert "Ничего не записано" in capsys.readouterr().out and not out.exists()


def test_main_revalidates_into_out_only(tmp_path, monkeypatch, capsys):
    leads = tmp_path / "task1_leads.csv"
    C.write_csv([lead()], leads, A.FIELDS)
    out = tmp_path / "base.csv"
    fetch_page, mx = site()
    monkeypatch.setattr(B, "LEADS", leads)
    monkeypatch.setattr(B, "MIN_ROWS", 1)
    monkeypatch.setattr(B, "fetch_all", lambda urls: None)
    monkeypatch.setattr(B, "validate_leads", lambda rows: C.validate_leads(rows, fetch_page, mx))
    monkeypatch.setattr(B, "check_merged", lambda rows: [])
    monkeypatch.setattr(B, "write_pipeline_inputs", lambda kept: pytest.fail("--out must not touch the project files"))

    assert B.main(["--out", str(out)]) == 0
    with out.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert [r["email"] for r in rows] == ["petrov@acme-stanki.ru"] and "именной 1" in capsys.readouterr().out


def test_pipeline_inputs_are_written_together_and_old_parts_are_not_mixed_in(tmp_path):
    out, names, part = tmp_path / "task1_base.csv", tmp_path / "task1_lpr.csv", tmp_path / "lpr" / "part_1.csv"
    assert B.write_pipeline_inputs([lead()], out, names, part) == ""
    assert out.exists() and names.exists() and part.exists()
    with names.open(encoding="utf-8", newline="") as fh:
        assert list(csv.DictReader(fh))[0]["источник_имени"] == STAFF

    (tmp_path / "lpr" / "part_2.csv").write_text("row,company\n", encoding="utf-8")
    assert "part_2.csv" in B.write_pipeline_inputs([lead()], out, names, part)


def test_duplicates_the_organisers_base_and_the_reserve_are_problems(tmp_path):
    their_base, reserve = tmp_path / "their_base.csv", tmp_path / "task1_reserve.csv"
    their_base.write_text("company,email,site\nHNC,sales@hnc.su,hnc.su\n", encoding="utf-8")
    reserve.write_text("Компания,site,Email\nБета,https://www.beta-opt.ru,info@beta-opt.ru\n", encoding="utf-8")
    files = {"their_base": their_base, "reserve": reserve}

    assert B.check_merged([lead()], min_rows=1, **files) == []
    twin = lead(company="Акме Станки Урал")
    assert any("duplicate acme-stanki.ru" in p for p in B.check_merged([lead(), twin], min_rows=1, **files))
    theirs = lead(company="HNC", site="https://hnc.su", email="ivanov@hnc.su")
    assert any("overlaps their_base.csv" in p for p in B.check_merged([theirs], min_rows=1, **files))
    spare = lead(company="Бета", site="https://beta-opt.ru", email="petrov@beta-opt.ru")
    assert B.check_merged([spare], min_rows=1, **files) == ["overlaps task1_reserve.csv: Бета (beta-opt.ru)"]
    assert any("need >= 50" in p for p in B.check_merged([lead()], **files))
    # the files of the project: the base, the reserve and the organisers' base do not share a company
    assert B.domains_of(B.THEIR_BASE, ("site", "email")) and B.domains_of(tmp_path / "missing.csv", ("site",)) == set()


# --- assembling and merging ----------------------------------------------------------- #

def test_segment_detail_and_page_date():
    assert A.detail("Насосное оборудование: производство насосов") == "насосное оборудование — производство насосов"
    assert A.detail("КИПиА: датчики") == "КИПиА — датчики"                       # an acronym keeps its case
    assert A.detail("Упаковка: гибкая упаковка", "Упаковка") == "гибкая упаковка"  # no «Упаковка: упаковка — …»
    assert A.source_date("2026-03-12 (дата обновления на странице)") == "2026-03"
    assert A.source_date("в подвале страницы: © 2026") == A.NO_DATE and A.source_date("") == A.NO_DATE


def test_hand_filled_cells_are_checked():
    assert A.check_row(lead()) == []
    assert any("Отчество" in p for p in A.check_row(lead(Отчество="Петрович")))
    assert any("компания_в_письме" in p for p in A.check_row(lead(компания_в_письме="ООО «Акме Станки» Москва Урал")))
    long_title = lead(должность_в_письме="директор по продажам и маркетингу")
    assert any("должность_в_письме" in p for p in A.check_row(long_title))
    assert any("телефон" in p for p in A.check_row(lead(телефон="(495) 000-00-00")))
    assert any("VERTICAL_ORDER" in p for p in A.check_row(lead(segment="Космос: ракеты")))


def test_merge_lpr_accepts_an_http_only_site(tmp_path, monkeypatch):
    names = tmp_path / "task1_lpr.csv"
    names.write_text("company,имя_ЛПР,должность_ЛПР,источник_имени\n"
                     "Акме,Иван Петров,Директор,http://acme.su/contacts/\n", encoding="utf-8")
    monkeypatch.setattr(M, "NAMES", names)
    assert M.load_names({"Акме": "http://acme.su"})[1] == []
    assert any("is not on the company site" in p for p in M.load_names({"Акме": "https://other.example"})[1])
