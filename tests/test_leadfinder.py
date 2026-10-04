"""leadfinder.py: extraction, pairing, validation, discovery, crawl, CLI. No network, no LLM.

Every page here is SYNTHETIC: the layouts are modelled on real company sites (staff cards
of a CMS template, a contacts table, a definition list, a paragraph with line breaks), but
the companies, people and addresses are invented.
"""

from __future__ import annotations

import csv
import json
import re

import httpx
import pytest

import leadfinder as L
import personalize as P

pytestmark = pytest.mark.respx(assert_all_called=False)

SITE = "https://primer-zavod.ru"
DOMAIN = "primer-zavod.ru"


def page(body: str, title: str = "Завод Пример", footer: str = "© 2011–2026 ООО «Завод Пример»") -> str:
    return f"""<html><head><title>{title}</title></head><body>
<header><a href="/">Завод Пример</a> <span>+7 (495) 000-00-00</span>
 <a href="mailto:info@{DOMAIN}">info@{DOMAIN}</a>
 <nav><a href="/company/staff/">Сотрудники</a> <a href="/contacts/">Контакты</a> <a href="/about/">О компании</a>
 <a href="/catalog/">Каталог</a> <a href="https://partner-site.ru/contacts/">Партнёр</a></nav></header>
<main>{body}</main>
<footer>{footer}</footer></body></html>"""


HOME = page("<h1>Завод Пример</h1><p>Производим токарные станки с ЧПУ и поставляем их по всей России.</p>")

# Staff cards, the way a common CMS template prints them.
CARDS = page("""<h1>Сотрудники</h1><div class="staff">
 <div class="item"><div class="name">Образцов Пётр Ильич</div><div class="post">Коммерческий директор</div>
   <div class="props"><div>Телефон: +7 (495) 000-00-01, доб. 101</div>
   <div>E-mail: <a href="mailto:obraztsov@primer-zavod.ru">obraztsov@primer-zavod.ru</a></div></div></div>
 <div class="item"><div class="name">Примерова Анна Сергеевна</div><div class="post">Менеджер по продажам</div>
   <div>E-mail: <a href="mailto:primerova@primer-zavod.ru">primerova@primer-zavod.ru</a></div></div>
 <div class="item"><div class="name">Тестов Олег Петрович</div><div class="post">Генеральный директор</div>
   <div>E-mail: <a href="mailto:gd@primer-zavod.ru">gd@primer-zavod.ru</a></div></div>
 <div class="item"><div class="name">Шаблонова Ирина Олеговна</div><div class="post">Главный бухгалтер</div>
   <div>E-mail: <a href="mailto:buh@primer-zavod.ru">buh@primer-zavod.ru</a></div></div>
 <div class="item"><div class="name">Черновиков Глеб Ильич</div><div class="post">Директор по персоналу</div>
   <div>E-mail: <a href="mailto:chernovikov@primer-zavod.ru">chernovikov@primer-zavod.ru</a></div></div>
</div>""", title="Сотрудники — Завод Пример")

# A contacts table: the title stands before the name, initials instead of a full name.
TABLE = page("""<h1>Контакты</h1><table>
<tr><th>Должность</th><th>ФИО</th><th>Телефон</th><th>E-mail</th></tr>
<tr><td>Директор по продажам</td><td>Образцов П.И.</td><td>+7 (343) 000-00-11</td>
    <td>p.obraztsov@primer-zavod.ru</td></tr>
<tr><td>Начальник отдела снабжения</td><td>Примеров А.С.</td><td>+7 (343) 000-00-12</td>
    <td>snab@primer-zavod.ru</td></tr>
<tr><td>Директор по маркетингу</td><td>Шаблонова Ирина</td><td></td><td>shablonova@primer-zavod.ru</td></tr>
</table>""", title="Контакты — Завод Пример")

# A definition list: the title is in <dt>, the person and the address in <dd>.
DEFLIST = page("""<h1>Руководство</h1><dl>
 <dt>Генеральный директор</dt>
 <dd>Тестов Олег Петрович<br>тел. +7 (812) 000-00-21<br>
     <a href="mailto:testov@primer-zavod.ru">testov@primer-zavod.ru</a></dd>
 <dt>Руководитель отдела продаж</dt><dd>Образцов Пётр Ильич, obraztsov [at] primer-zavod.ru</dd>
 <dt>Отдел кадров</dt><dd>hr@primer-zavod.ru</dd>
</dl>""", title="Руководство — Завод Пример")

# One paragraph, people separated by <br>: no cards at all, only reading order.
PARAGRAPH = page("""<h1>Контакты</h1><div class="text"><p><strong>Коммерческий отдел</strong><br>
Шаблонов Пётр Ильич — коммерческий директор<br>Тел.: +7 (351) 000-00-31<br>E-mail: shablonov@primer-zavod.ru<br><br>
Примерова Анна Сергеевна — ведущий менеджер<br>E-mail: primerova@primer-zavod.ru</p></div>""")

AMBIGUOUS = page("""<h1>Контакты</h1><div class="block"><p>Клиентов Игорь Ильич, коммерческий директор, и Заказова
Анна Сергеевна, ассистент. Для связи: komdir77@primer-zavod.ru</p></div>""")

ONLY_GENERIC = page("""<h1>Контакты</h1><p>Генеральный директор — Тестов Олег Петрович</p>
<p>Отдел продаж: <a href="mailto:sales@primer-zavod.ru">sales@primer-zavod.ru</a>, +7 (495) 000-00-05</p>
<p>Бухгалтерия: buh@primer-zavod.ru</p>""", title="Контакты — Завод Пример")


class FakeMX:
    """MX lookup stand-in: every domain accepts mail unless listed in `dead`."""

    def __init__(self, dead=()):
        self.dead, self.asked = set(dead), []

    def lookup(self, domain):
        self.asked.append(domain)
        return [] if domain in self.dead else [f"mx.{domain}"]


class Web:
    """A dict of URL -> page served through respx; everything else is 404."""

    def __init__(self, router):
        self.pages: dict[str, object] = {}
        self.calls: list[str] = []
        router.route().mock(side_effect=self._serve)

    def _serve(self, request):
        url = str(request.url)
        self.calls.append(url)
        body = self.pages.get(url)
        if isinstance(body, list):  # a sequence of answers: one per request, the last one stays
            body = body.pop(0) if len(body) > 1 else body[0]
        if body is None:
            return httpx.Response(404, html="<html><head><title>404</title></head><body>Не найдено</body></html>")
        if isinstance(body, Exception):
            raise body
        if isinstance(body, httpx.Response):
            return body
        kind = "text/plain" if url.endswith(".txt") else "application/xml" if url.endswith(".xml") else "text/html"
        return httpx.Response(200, content=body.encode("utf-8"), headers={"content-type": f"{kind}; charset=utf-8"})

    def site(self, base: str, pages: dict[str, str], robots: str = "") -> None:
        for path, body in pages.items():
            self.pages[base + path] = body
        if robots:
            self.pages[base + "/robots.txt"] = robots

    def fetched(self, fragment: str) -> int:
        return sum(fragment in url for url in self.calls)


@pytest.fixture
def web(respx_mock):
    return Web(respx_mock)


@pytest.fixture
def fetcher(tmp_path):
    f = L.LeadFetcher(cache_dir=tmp_path / "cache", delay=0, retries=0, backoff=0)
    yield f
    f.close()


def company(name="Завод Пример", site=SITE, **kw) -> L.Company:
    return L.Company(name=name, site=site, **kw)


def candidates(html: str, url: str = f"{SITE}/contacts/") -> dict[str, L.Candidate]:
    doc = L.annotate(L.flatten(url, html))
    return {c.email: c for c in L.extract_candidates(doc)}


def verdicts(html: str, url: str = f"{SITE}/contacts/", *, icp: L.ICP | None = None, mx=None, extra=(),
             **settings) -> dict[str, L.Verdict]:
    """Judge every address of one page the way the pipeline does."""
    icp = icp or L.ICP()
    pages = [L.SitePage(url, "contacts", L.annotate(L.flatten(url, html)))]
    pages += [L.SitePage(u, "about", L.annotate(L.flatten(u, h))) for u, h in extra]
    comp = company()
    site = L.build_site_ctx(comp, pages, icp)
    cfg = L.Settings(icp=icp, mx=mx or FakeMX(), **settings)
    best: dict[str, L.Verdict] = {}
    for cand in L.extract_candidates(pages[0].doc):  # one verdict per address, the one that got furthest
        verdict = L.judge(cand, comp, site, cfg)
        if cand.email not in best or L._rank(verdict) >= L._rank(best[cand.email]):
            best[cand.email] = verdict
    return best


def printed(address: str, text: str) -> bool:
    """Is exactly this address in the text (not a longer one that ends the same way)?"""
    return re.search(rf"(?<![\w.+-]){re.escape(address)}(?![\w-])", text) is not None


def read_csv(path) -> list[dict[str, str]]:
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def write_seeds(path, rows, header=("company", "site", "city", "segment")):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)
    return path


# --- names, titles, addresses -------------------------------------------------- #

@pytest.mark.parametrize("text, surname, first, kind", [
    ("Образцов Пётр Ильич", "Образцов", "Пётр", "full"),
    ("Пётр Ильич Образцов", "Образцов", "Пётр", "full"),
    ("Пётр Образцов", "Образцов", "Пётр", "full"),
    ("Образцов Пётр", "Образцов", "Пётр", "full"),
    ("ОБРАЗЦОВ ПЁТР ИЛЬИЧ", "Образцов", "Пётр", "full"),
    ("Образцов П.И.", "Образцов", "П.", "initials"),
    ("П. И. Образцов", "Образцов", "П.", "initials"),
    ("Peter Obraztsov", "Obraztsov", "Peter", "latin"),
    ("Пётр Ильич", "", "Пётр", "no_surname"),
])
def test_personal_names_are_recognised(text, surname, first, kind):
    found = L.find_names(f"Контакт: {text}, тел. +7 (495) 000-00-00")
    assert len(found) == 1
    person = found[0][2]
    assert (person.surname, person.first, person.kind) == (surname, first, kind)


@pytest.mark.parametrize("text", [
    "Генеральный Директор", "Нижний Новгород", "Отдел Продаж", "Коммерческий Отдел", "ул. Петра Образцова, д. 5",
    "Sales Director", "Завод Пример", "В Примере Пётр прошёл путь от стажёра", "Наш Пример Анна считает лучшим",
])
def test_capitalised_words_that_are_not_names(text):
    assert L.find_names(text) == []


def test_surname_first_without_patronymic_needs_a_clean_context():
    # a surname of an unusual shape is fine when the name stands alone or is followed by a title
    assert [p.surname for _, _, p in L.find_names("Шаблондт Олег")] == ["Шаблондт"]
    assert [p.surname for _, _, p in L.find_names("Шаблондт Олег, начальник отдела продаж")] == ["Шаблондт"]
    assert [p.surname for _, _, p in L.find_names("Шаблондт Олег начальник отдела продаж")] == ["Шаблондт"]
    assert [p.surname for _, _, p in L.find_names("у нас Образцов Пётр отвечает за продажи")] == ["Образцов"]


@pytest.mark.parametrize("text, surname, patronymic, kind", [
    ("Пётр Ильич", "", "Ильич", "no_surname"), ("Олег Михайлович", "", "Михайлович", "no_surname"),
    ("Анна Юрьевна", "", "Юрьевна", "no_surname"), ("Глеб Дмитриевич", "", "Дмитриевич", "no_surname"),
    ("Ирина Сергеевна", "", "Сергеевна", "no_surname"), ("Олег Шаблонович", "Шаблонович", "", "full"),
    ("Анна Примеркевич", "Примеркевич", "", "full"),
])
def test_patronymic_or_a_surname_that_ends_like_one(text, surname, patronymic, kind):
    person = L.find_names(text)[0][2]
    assert (person.surname, person.patronymic, person.kind) == (surname, patronymic, kind)


def test_latin_lookalike_letters_inside_a_russian_name():
    found = L.find_names("Aлексей Образцов")
    assert [(p.first, p.surname) for _, _, p in found] == [("Алексей", "Образцов")]
    assert found[0][2].raw.startswith("A")  # the evidence keeps the page's own spelling


def test_career_sentence_is_not_a_title():
    doc = L.annotate(L.flatten(SITE, "<p>Прошёл путь от менеджера до руководителя отдела продаж.</p>"))
    assert doc.of_kind("title") == []


@pytest.mark.parametrize("title, role", [
    ("Генеральный директор", "ceo"), ("ген. директор", "ceo"), ("Директор", "ceo"), ("Владелец компании", "ceo"),
    ("Основатель и CEO", "ceo"), ("Управляющий партнёр", "ceo"), ("Исполнительный директор", "ceo"),
    ("Коммерческий директор", "commercial"), ("Директор по коммерции", "commercial"), ("CCO", "commercial"),
    ("Руководитель коммерческого отдела", "commercial"),
    ("Руководитель отдела продаж", "sales"), ("Начальник отдела сбыта", "sales"), ("Директор по продажам", "sales"),
    ("Head of Sales", "sales"), ("Заместитель директора по продажам", "sales"),
    ("Директор по маркетингу", "marketing"), ("Руководитель отдела маркетинга", "marketing"), ("CMO", "marketing"),
    ("Директор по развитию", "bizdev"), ("Head of Business Development", "bizdev"),
    ("Технический директор", "other_director"), ("Директор по персоналу", "other_director"),
    ("Финансовый директор", "other_director"), ("Начальник отдела снабжения", "dept_head"),
    ("Менеджер по продажам", "staff"), ("Ведущий специалист отдела продаж", "staff"), ("Главный бухгалтер", "staff"),
    ("Секретарь", "staff"), ("Помощник генерального директора", "staff"), ("Инженер техподдержки", "staff"),
    ("Менеджер по работе с ключевыми клиентами", "staff"),
])
def test_role_groups(title, role):
    assert L.classify_role(title)[0] == role


def test_deputy_is_marked():
    assert L.classify_role("Заместитель генерального директора") == ("ceo", True)
    assert L.classify_role("Генеральный директор") == ("ceo", False)


def test_department_names_a_plain_head_title():
    assert L.classify_role("начальник отдела", "Отдел продаж")[0] == "sales"
    assert L.classify_role("начальник отдела")[0] == "dept_head"


def test_title_in_oblique_case_is_not_somebodys_title():
    # «приёмная директора» describes a room, not a person
    doc = L.annotate(L.flatten(SITE, "<p>Приёмная директора: +7 (495) 000-00-09</p>"))
    assert doc.of_kind("title") == []


@pytest.mark.parametrize("text, expected", [
    ("obraztsov [at] primer-zavod.ru", "obraztsov@primer-zavod.ru"),
    ("obraztsov (собака) primer-zavod.ru", "obraztsov@primer-zavod.ru"),
    ("obraztsov[at]primer-zavod[dot]ru", "obraztsov@primer-zavod.ru"),
    ("obraztsov {at} primer-zavod (dot) ru", "obraztsov@primer-zavod.ru"),
    ("Пишите: Obraztsov@Primer-Zavod.ru.", "obraztsov@primer-zavod.ru"),
    ("e-mail: obraztsov@primer-zavod .ru", "obraztsov@primer-zavod.ru"),
    # the domain ends where the sentence ends: «… .ru. Call us» is not «primer-zavod.ru.call»
    ("Пишите: obraztsov [at] primer-zavod.ru. Call us", "obraztsov@primer-zavod.ru"),
    ("Пишите: obraztsov [at] primer-zavod.ru. Тел. 8 800", "obraztsov@primer-zavod.ru"),
])
def test_obfuscated_addresses_are_read(text, expected):
    assert [e for _, _, e in L.find_emails(text)] == [expected]


@pytest.mark.parametrize("text", [
    "Follow us @ primer-zavod.ru и приходите на стенд",  # words around a bare « @ » are not an address
    "obraztsov @ primer-zavod.ru",  # no brackets: the page does not say this is an address
    "Вариант (a) primer-zavod.ru подходит для заказа", "option (a) primer-zavod.ru",
    "наша собака primer-zavod.ru", "ivanov собака primer-zavod.ru",
    "Мы в Telegram: @primer_zavod", "price @ 100.50 rub",
])
def test_an_address_is_never_assembled_from_loose_words(text):
    assert L.find_emails(text) == []


def test_loose_words_next_to_a_director_give_no_candidate_and_no_lead():
    html = page("""<div class="item"><div>Образцов Пётр Ильич</div><div>Генеральный директор</div>
      <div>Follow us @ primer-zavod.ru</div></div>""")
    assert [e for e in candidates(html) if e != f"info@{DOMAIN}"] == []
    assert all(v.lead is None for v in verdicts(html).values())


def test_cloudflare_protected_address_is_decoded():
    key = 0x5A
    code = f"{key:02x}" + "".join(f"{ord(ch) ^ key:02x}" for ch in "obraztsov@primer-zavod.ru")
    html = page(f"""<div class="item"><div>Образцов Пётр Ильич</div><div>Коммерческий директор</div>
      <a href="/cdn-cgi/l/email-protection" class="__cf_email__" data-cfemail="{code}">[email&#160;protected]</a>
      </div>""")
    cand = candidates(html)["obraztsov@primer-zavod.ru"]
    assert cand.person.surname == "Образцов" and cand.visible


def test_html_entities_and_comments():
    html = page("""<div class="item"><div>Образцов Пётр Ильич</div><div>Коммерческий директор</div>
      <div>&#111;braztsov&#64;primer-zavod.ru</div><!-- <div>Скрытов Иван Ильич, skrytov@primer-zavod.ru</div> -->
      </div>""")
    found = candidates(html)
    assert "obraztsov@primer-zavod.ru" in found
    assert "skrytov@primer-zavod.ru" not in found  # commented out = not published


def test_junk_addresses_are_ignored():
    assert L.find_emails("logo@2x.png name@example.com user@domain.ru") == []


# --- pairing ------------------------------------------------------------------- #

def test_pairing_in_cards():
    found = candidates(CARDS, f"{SITE}/company/staff/")
    boss = found["obraztsov@primer-zavod.ru"]
    assert (boss.person.display, boss.title, boss.method) == ("Образцов Пётр Ильич", "Коммерческий директор", "card")
    # the phone of the card is not taken along, only its digits (to recognise a numbered mailbox)
    assert not hasattr(boss, "phone") and {"74950000001", "101"} <= set(boss.card_digits)
    assert "Образцов Пётр Ильич" in boss.fragment and "obraztsov@primer-zavod.ru" in boss.fragment
    assert found["gd@primer-zavod.ru"].person.surname == "Тестов"
    assert found["primerova@primer-zavod.ru"].title == "Менеджер по продажам"
    assert found[f"info@{DOMAIN}"].chrome and found[f"info@{DOMAIN}"].person is None  # the header box is nobody's


def test_pairing_in_table_rows():
    found = candidates(TABLE)
    sales = found["p.obraztsov@primer-zavod.ru"]
    assert (sales.person.surname, sales.person.kind, sales.title) == ("Образцов", "initials", "Директор по продажам")
    assert sales.method == "card" and "73430000011" in sales.card_digits
    assert found["shablonova@primer-zavod.ru"].title == "Директор по маркетингу"
    assert found["snab@primer-zavod.ru"].person.surname == "Примеров"  # each row keeps its own person


def test_pairing_in_definition_list():
    found = candidates(DEFLIST, f"{SITE}/about/rukovodstvo/")
    ceo = found["testov@primer-zavod.ru"]
    assert (ceo.person.display, ceo.title) == ("Тестов Олег Петрович", "Генеральный директор")
    assert "78120000021" in ceo.card_digits
    head = found["obraztsov@primer-zavod.ru"]
    assert (head.person.surname, head.title) == ("Образцов", "Руководитель отдела продаж")
    assert found["hr@primer-zavod.ru"].person is None  # a department box under its own <dt>


def test_pairing_by_reading_order_in_one_paragraph():
    found = candidates(PARAGRAPH)
    boss = found["shablonov@primer-zavod.ru"]
    assert (boss.person.surname, boss.title, boss.method) == ("Шаблонов", "коммерческий директор", "sequence")
    assert "73510000031" in boss.card_digits
    assert found["primerova@primer-zavod.ru"].person.surname == "Примерова"


def test_person_page_with_the_name_in_the_page_heading():
    # the name is the <h1> of the page, the address sits in another section next to "other staff"
    html = page("""<section class="page-top"><div class="topic"><h1>Пётр Образцов</h1></div></section>
      <div class="detail"><div class="post">Директор по продажам</div>
        <div class="props"><div><span>E-mail</span> <a href="mailto:po@primer-zavod.ru">po@primer-zavod.ru</a></div>
        <div><span>Телефон</span> 8 (800) 000-00-41</div></div>
        <div class="bio">В Примере Пётр прошёл путь от менеджера до руководителя отдела продаж.</div>
        <div class="others"><a href="/team/primerova/">Анна Примерова</a><div>Менеджер по продажам</div>
          <a href="/team/testov/">Олег Тестов</a><div>Менеджер проектов</div></div></div>""")
    cand = candidates(html, f"{SITE}/team/obraztsov/")["po@primer-zavod.ru"]
    assert (cand.person.display, cand.title, cand.method) == ("Образцов Пётр", "Директор по продажам", "sequence")
    verdict = verdicts(html, f"{SITE}/team/obraztsov/")["po@primer-zavod.ru"]
    assert verdict.reason == "" and verdict.lead.address_type == L.TYPE_PERSONAL  # «po» = Пётр Образцов


def test_another_name_after_the_address_does_not_take_it():
    # a person page: the owner is named in the heading and the breadcrumbs, the details block
    # ends with «Ваш менеджер …», the only name inside that block
    html = page("""<section class="top"><h1>Пётр Образцов</h1>
      <div class="crumbs">Главная — Сотрудники — Пётр Образцов</div></section>
      <div class="detail"><div class="props"><div>Должность</div><div>Руководитель отдела продаж</div>
        <div>E-mail</div><div>obraztsov@primer-zavod.ru</div></div>
        <div class="side">Ваш менеджер <b>Анна Примерова</b> <a href="/ask/">Задать вопрос</a></div></div>""")
    cand = candidates(html, f"{SITE}/company/staff/obraztsov/")["obraztsov@primer-zavod.ru"]
    assert (cand.person.surname, cand.title, cand.method) == ("Образцов", "Руководитель отдела продаж", "sequence")
    assert verdicts(html, f"{SITE}/company/staff/obraztsov/")["obraztsov@primer-zavod.ru"].reason == ""


def test_cards_printed_address_first_keep_their_own_person():
    # every card: phone, address, then the name and the title
    html = page("""<h3>Корпоративный отдел</h3><div class="team">
      <div class="item"><div>(812) 000-00-51</div><div>primerova@primer-zavod.ru</div><div>Примерова Анна</div>
        <div>Менеджер по продажам</div></div>
      <div class="item"><div>(812) 000-00-52</div><div>po@primer-zavod.ru</div><div>Образцов Пётр Ильич</div>
        <div>Руководитель отдела</div></div></div>""")
    cand = candidates(html)["po@primer-zavod.ru"]
    assert (cand.person.surname, cand.title, cand.method) == ("Образцов", "Руководитель отдела", "card")
    assert candidates(html)["primerova@primer-zavod.ru"].person.surname == "Примерова"


def test_same_person_in_russian_and_english_is_one_owner(web, fetcher):
    team = page("""<h1>Команда</h1><div class="team">
      <div class="item"><div>Образцов Пётр</div><div>Коммерческий директор</div><div>po@primer-zavod.ru</div></div>
      </div><div class="team en">
      <div class="item"><div>Petr Obraztsov</div><div>Commercial director</div><div>po@primer-zavod.ru</div></div>
      </div>""")
    web.site(SITE, {"/": HOME, "/company/staff/": team})
    result = L.process_company(company(), run_ctx(fetcher))
    assert [r["Email"] for r in result.leads] == ["po@primer-zavod.ru"]


def test_department_label_with_an_adjective_names_the_head():
    html = page("""<div class="group"><h3>Корпоративный отдел</h3>
      <div class="item"><div>Образцов Пётр Ильич</div><div>Руководитель отдела</div>
      <div>obraztsov@primer-zavod.ru</div></div></div>""")
    lead = verdicts(html)["obraztsov@primer-zavod.ru"].lead
    assert lead.role == "sales" and lead.title == "Руководитель отдела (Корпоративный отдел)"


def test_two_names_and_one_address_is_ambiguous():
    cand = candidates(AMBIGUOUS)["komdir77@primer-zavod.ru"]
    assert cand.person is None and cand.reason == L.R_AMBIGUOUS
    assert [p.surname for p, _, _ in cand.name_options] == ["Клиентов", "Заказова"]


def test_address_that_agrees_with_one_of_two_names():
    html = page("""<p>Клиентов Игорь Ильич, коммерческий директор, и Заказова Анна Сергеевна, ассистент.
      Почта: zakazova@primer-zavod.ru</p>""")
    cand = candidates(html)["zakazova@primer-zavod.ru"]
    assert cand.person.surname == "Заказова" and cand.method == "sequence+local"


@pytest.mark.parametrize("local, how", [
    ("obraztsov", "фамилия"), ("p.obraztsov", "фамилия"), ("obrazcov", "фамилия"), ("petr", "имя"),
    ("peter", "имя"), ("po", "инициалы"), ("pio", "инициалы"), ("sales", ""), ("ivanov", ""),
])
def test_address_is_only_confirmed_by_the_name(local, how):
    person = L.Person(surname="Образцов", first="Пётр", patronymic="Ильич")
    assert L.local_matches_person(local, person) == how


# --- address type --------------------------------------------------------------- #

@pytest.mark.parametrize("email, expected", [
    ("obraztsov@primer-zavod.ru", L.TYPE_PERSONAL),
    ("gd@primer-zavod.ru", L.TYPE_ROLE),
    ("director@primer-zavod.ru", L.TYPE_ROLE),
    ("info@primer-zavod.ru", L.TYPE_GENERIC),
    ("sales@primer-zavod.ru", L.TYPE_GENERIC),
    ("zakaz@primer-zavod.ru", L.TYPE_GENERIC),
    ("office2@primer-zavod.ru", L.TYPE_GENERIC),
    ("primer-zavod@primer-zavod.ru", L.TYPE_GENERIC),
    ("director.p@primer-zavod.ru", L.TYPE_ROLE),  # a post plus an initial is still a post
    ("kom.dir@primer-zavod.ru", L.TYPE_ROLE), ("gen-dir@primer-zavod.ru", L.TYPE_ROLE),  # a post in two parts
    ("kom.otdel@primer-zavod.ru", L.TYPE_GENERIC), ("kd.msk@primer-zavod.ru", L.TYPE_GENERIC),
    ("askceo@primer-zavod.ru", L.TYPE_ROLE),
    ("stankoprom2010@primer-zavod.ru", L.TYPE_UNKNOWN),  # neither the name nor a post: nobody's by the page
    ("okb30@primer-zavod.ru", L.TYPE_UNKNOWN),
    ("109@primer-zavod.ru", L.TYPE_UNKNOWN),  # a numbered box with no such number next to the person
    ("obraztsov@mail.ru", L.TYPE_FREE),
    ("zavod.primer@gmail.com", L.TYPE_FREE),
])
def test_address_types(email, expected):
    site = L.SiteCtx(domain=DOMAIN, domains={DOMAIN})
    person = L.Person(surname="Образцов", first="Пётр", patronymic="Ильич")
    assert L.classify_address(email, person, site)[0] == expected


def test_surname_that_looks_like_a_generic_box_is_still_personal():
    site = L.SiteCtx(domain=DOMAIN, domains={DOMAIN})
    assert L.classify_address("zakazov@primer-zavod.ru", L.Person(surname="Заказов", first="Игорь"), site)[0] \
        == L.TYPE_PERSONAL
    assert L.classify_address("zakazov@primer-zavod.ru", L.Person(surname="Образцов", first="Пётр"), site)[0] \
        == L.TYPE_GENERIC
    # «ok@» (отдел кадров) fits the initials of Олег Кузнецов and is a department box all the same
    assert L.classify_address("ok@primer-zavod.ru", L.Person(surname="Кузнецов", first="Олег"), site)[0] \
        == L.TYPE_GENERIC


def test_address_from_the_site_header_is_a_company_box():
    site = L.SiteCtx(domain=DOMAIN, domains={DOMAIN}, chrome_emails={"obraztsov@primer-zavod.ru"})
    person = L.Person(surname="Образцов", first="Пётр")
    assert L.classify_address("obraztsov@primer-zavod.ru", person, site)[0] == L.TYPE_GENERIC


# --- validation ------------------------------------------------------------------ #

def test_leads_and_rejects_on_a_staff_page():
    result = verdicts(CARDS, f"{SITE}/company/staff/")
    lead = result["obraztsov@primer-zavod.ru"].lead
    assert result["obraztsov@primer-zavod.ru"].reason == ""
    assert (lead.surname, lead.first, lead.title, lead.address_type) == (
        "Образцов", "Пётр", "Коммерческий директор", L.TYPE_PERSONAL)
    assert lead.role == "commercial" and lead.confidence >= 90
    assert lead.source_url == f"{SITE}/company/staff/" and lead.checked_on == "2026-10-03"
    assert any(check.startswith("MX: mx.primer-zavod.ru") for check in lead.checks)
    boss = result["gd@primer-zavod.ru"]
    assert boss.reason == "" and boss.lead.address_type == L.TYPE_ROLE and boss.lead.role == "ceo"
    assert result["primerova@primer-zavod.ru"].reason == L.R_NOT_DM  # a sales manager
    assert result["buh@primer-zavod.ru"].reason == L.R_GENERIC  # an accounting box, even inside a card
    assert result["chernovikov@primer-zavod.ru"].reason == L.R_ROLE  # HR director: a director outside the ICP
    assert result[f"info@{DOMAIN}"].reason == L.R_GENERIC


def test_icp_roles_decide_who_is_wanted():
    only_sales = L.ICP(roles=["sales", "commercial"])
    result = verdicts(CARDS, icp=only_sales)
    assert result["gd@primer-zavod.ru"].reason == L.R_ROLE
    assert result["obraztsov@primer-zavod.ru"].reason == ""
    with_hr = L.ICP(roles=["other_director"])
    assert verdicts(CARDS, icp=with_hr)["chernovikov@primer-zavod.ru"].reason == ""


def test_initials_lower_the_confidence_but_pass():
    result = verdicts(TABLE)
    sales = result["p.obraztsov@primer-zavod.ru"]
    assert sales.reason == "" and "на странице только инициалы" in sales.lead.checks
    assert sales.lead.confidence < verdicts(CARDS)["obraztsov@primer-zavod.ru"].lead.confidence
    assert result["snab@primer-zavod.ru"].reason == L.R_GENERIC


def test_domain_mismatch_is_rejected():
    html = page("""<div class="item"><div>Образцов Пётр Ильич</div><div>Коммерческий директор</div>
      <div>obraztsov@drugaya-firma.ru</div></div>""")
    verdict = verdicts(html)["obraztsov@drugaya-firma.ru"]
    assert verdict.reason == L.R_DOMAIN and verdict.detail == "drugaya-firma.ru"


TWO_ON_ANOTHER_DOMAIN = """<div class="item"><div>Образцов Пётр Ильич</div><div>Коммерческий директор</div>
      <div>obraztsov@{domain}</div></div>
      <div class="item"><div>Тестов Олег Петрович</div><div>Генеральный директор</div>
      <div>testov@{domain}</div></div>"""


def test_two_addresses_of_another_domain_do_not_make_it_the_sites_own():
    # two people of a supplier printed on the page: their domain is not "used by the site"
    result = verdicts(page(TWO_ON_ANOTHER_DOMAIN.format(domain="postavshik-group.ru")))
    assert {v.reason for e, v in result.items() if e.endswith("@postavshik-group.ru")} == {L.R_DOMAIN}


def test_second_domain_is_accepted_when_the_site_itself_shows_it_is_its_own():
    cards = TWO_ON_ANOTHER_DOMAIN.format(domain="mail-primera.ru")
    # 1. a box on that domain stands in the footer of the site
    footer = page(cards, footer="© 2011–2026 ООО «Завод Пример», office@mail-primera.ru")
    lead = verdicts(footer)["obraztsov@mail-primera.ru"].lead
    assert "домен mail-primera.ru используется сайтом (ящик на нём стоит в шапке или подвале сайта)" in lead.checks
    # 2. the domain is spelled like the site's own (primer-zavod.ru -> primer-zavod.com)
    similar = verdicts(page(TWO_ON_ANOTHER_DOMAIN.format(domain="primer-zavod.com")))["obraztsov@primer-zavod.com"]
    assert similar.reason == "" and "пишется как домен сайта" in "; ".join(similar.lead.checks)
    # 3. the site prints no address on its own domain and all its addresses are on that one
    bare = cards.replace("</div></div>", "</div></div>", 1) + "<p>Приёмная: priemnaya@mail-primera.ru</p>"
    html = page(bare).replace(f'<a href="mailto:info@{DOMAIN}">info@{DOMAIN}</a>', "")
    only = verdicts(html)["obraztsov@mail-primera.ru"]
    assert only.reason == "" and "на нём все адреса сайта" in "; ".join(only.lead.checks)
    # ... but not while the site prints its own domain next to it (the header box info@primer-zavod.ru)
    assert verdicts(page(bare))["obraztsov@mail-primera.ru"].reason == L.R_DOMAIN


def test_person_of_another_organisation_on_a_second_domain_is_foreign():
    html = page("""<div class="item"><div>Чужаков Пётр Ильич</div><div>Генеральный директор ООО «Поставщик»</div>
      <div>chuzhakov@mail-primera.ru</div></div>""", footer="© 2026 ООО «Завод Пример», office@mail-primera.ru")
    verdict = verdicts(html)["chuzhakov@mail-primera.ru"]
    assert verdict.reason == L.R_FOREIGN and "Поставщик" in verdict.detail


def test_free_mailbox_is_not_a_work_address():
    html = page("""<div class="item"><div>Образцов Пётр Ильич</div><div>Коммерческий директор</div>
      <div>obraztsov.primer@mail.ru</div></div>""")
    assert verdicts(html)["obraztsov.primer@mail.ru"].reason == L.R_FREE


def test_no_mx_rejects_and_mx_is_dns_only():
    mx = FakeMX(dead={DOMAIN})
    assert verdicts(CARDS, mx=mx)["obraztsov@primer-zavod.ru"].reason == L.R_NO_MX
    assert mx.asked and set(mx.asked) == {DOMAIN}
    resolver = L.MXResolver(enabled=False)
    assert resolver.lookup(DOMAIN) is None  # no dig -> "could not check", never a guess


def test_client_testimonial_is_not_staff():
    html = page("""<h1>О компании</h1><h2>Отзывы клиентов</h2>
      <div class="item"><p>Станок работает третий год без остановок.</p>
      <div>Довольнов Игорь Петрович, генеральный директор, dovolnov@primer-zavod.ru</div></div>""")
    verdict = verdicts(html)["dovolnov@primer-zavod.ru"]
    assert verdict.reason == L.R_FOREIGN and "Отзывы клиентов" in verdict.detail


@pytest.mark.parametrize("path", ["/blog/kak-vybrat-stanok/", "/partners/", "/news/2026/vystavka/",
                                  "/authors/obraztsov/"])
def test_bylines_and_partner_pages_are_not_staff(path):
    html = page("""<div class="item"><div>Автор: Образцов Пётр Ильич</div><div>Директор по маркетингу</div>
      <div>obraztsov@primer-zavod.ru</div></div>""")
    assert verdicts(html, f"{SITE}{path}")["obraztsov@primer-zavod.ru"].reason == L.R_FOREIGN


def test_name_far_from_the_address_is_rejected():
    filler = "Мы работаем с 2011 года и поставляем станки. " * 12
    html = page(f"<div><p>Образцов Пётр Ильич, коммерческий директор.</p><p>{filler}</p>"
                "<p>Почта: obraztsov@primer-zavod.ru</p></div>")
    verdict = verdicts(html)["obraztsov@primer-zavod.ru"]
    assert verdict.reason == L.R_GAP
    assert verdicts(html, max_gap=5000)["obraztsov@primer-zavod.ru"].reason == ""


def test_mailto_button_inside_a_persons_card_is_his_address():
    html = page("""<div class="item"><div>Образцов Пётр Ильич</div><div>Коммерческий директор</div>
      <a href="mailto:obraztsov@primer-zavod.ru">Написать письмо</a></div>""")
    # the address is not printed, it is only the target of a button: by default that is not a lead
    assert verdicts(html)["obraztsov@primer-zavod.ru"].reason == L.R_MAILTO
    assert L.Settings(icp=L.ICP()).mailto == "none" and L.parse_args([]).mailto == "none"
    # opt-in: the button stands in the person's own card and its address agrees with the person's name
    lead = verdicts(html, mailto="matched")["obraztsov@primer-zavod.ru"].lead
    assert "адрес стоит в ссылке mailto (текстом не напечатан)" in lead.checks
    assert lead.fragment.endswith("Написать письмо [ссылка mailto: obraztsov@primer-zavod.ru]")
    assert lead.confidence < verdicts(CARDS)["obraztsov@primer-zavod.ru"].lead.confidence


def test_mailto_button_to_another_box_is_not_the_persons_address():
    # «Написать письмо» in the director's card leads to a post box, «Отправить заявку» to a department box
    for address, mode, reason in (("askceo@primer-zavod.ru", "matched", L.R_MAILTO),
                                  ("sales-msk@primer-zavod.ru", "matched", L.R_GENERIC),
                                  ("sales-msk@primer-zavod.ru", "card", L.R_GENERIC),
                                  ("zayavki2026@primer-zavod.ru", "card", L.R_UNMATCHED)):
        html = page(f"""<div class="item"><div>Образцов Пётр Ильич</div><div>Генеральный директор</div>
          <a href="mailto:{address}">Написать письмо</a></div>""")
        assert verdicts(html, mailto=mode)[address].reason == reason, (address, mode)
    html = page("""<div class="item"><div>Образцов Пётр Ильич</div><div>Генеральный директор</div>
      <a href="mailto:askceo@primer-zavod.ru">Написать письмо</a></div>""")
    assert verdicts(html, mailto="card")["askceo@primer-zavod.ru"].reason == ""  # the old behaviour, on request


def test_mailto_link_in_a_block_with_several_people_is_not_enough():
    html = page("""<p>Образцов Пётр Ильич — коммерческий директор, Примерова Анна Сергеевна — ведущий менеджер<br>
      <a href="mailto:obraztsov@primer-zavod.ru">Написать</a></p>""")
    assert verdicts(html, mailto="matched")["obraztsov@primer-zavod.ru"].reason == L.R_MAILTO
    assert verdicts(html, mailto="card")["obraztsov@primer-zavod.ru"].reason == L.R_MAILTO
    assert verdicts(html, mailto="all")["obraztsov@primer-zavod.ru"].reason == ""


def test_published_refusal_and_personal_data_ban_are_respected():
    refusal = page("<p>Коммерческие предложения не рассматриваем.</p>")
    ban = page("<p>Установлен запрет на обработку персональных данных неограниченным кругом лиц.</p>")
    about = f"{SITE}/about/"
    assert verdicts(CARDS, extra=[(about, refusal)])["obraztsov@primer-zavod.ru"].reason == L.R_REFUSAL
    assert verdicts(CARDS, extra=[(about, ban)])["obraztsov@primer-zavod.ru"].reason == L.R_PD_BAN


def test_ban_published_on_the_site_is_the_reason_given_for_the_company(web, fetcher):
    ban = "<p>Субъектами установлены запреты на обработку неограниченным кругом лиц персональных данных.</p>"
    staff = page("""<div class="item"><div>Примерова Анна Сергеевна</div><div>Менеджер по продажам</div>
      <div>primerova@primer-zavod.ru</div></div>""" + ban)
    web.site(SITE, {"/": HOME, "/contacts/": staff})
    result = L.process_company(company(), run_ctx(fetcher))
    assert result.leads == [] and result.no_lead["причина"].startswith(f"{L.R_PD_BAN}: {SITE}/contacts/")
    assert "кроме того: не ЛПР" in result.no_lead["причина"]


def test_person_named_with_another_organisation_is_flagged():
    html = page("""<div class="item"><div>Образцов Пётр Ильич</div>
      <div>Генеральный директор ООО «Совсем Другая Фирма»</div><div>obraztsov@primer-zavod.ru</div></div>""")
    lead = verdicts(html)["obraztsov@primer-zavod.ru"].lead
    assert any("Совсем Другая Фирма" in check for check in lead.checks)
    assert lead.confidence < verdicts(CARDS)["obraztsov@primer-zavod.ru"].lead.confidence


# --- freshness -------------------------------------------------------------------- #

def test_page_updated_long_ago_is_stale():
    old = CARDS.replace("<h1>Сотрудники</h1>", "<h1>Сотрудники</h1><p>Информация обновлена: 12.03.2019</p>")
    verdict = verdicts(old)["obraztsov@primer-zavod.ru"]
    assert verdict.reason == L.R_STALE and "12.03.2019" in verdict.detail
    fresh = CARDS.replace("<h1>Сотрудники</h1>", "<h1>Сотрудники</h1><p>Информация обновлена: 12.08.2026</p>")
    lead = verdicts(fresh)["obraztsov@primer-zavod.ru"].lead
    assert "свежесть: страница обновлена 12.08.2026" in lead.checks


def test_old_footer_year_is_a_warning_not_a_reject():
    # «© 2009 Компания» is often the founding year: it lowers the confidence and is said out loud
    lead = verdicts(CARDS.replace("© 2011–2026", "© 2009"))["obraztsov@primer-zavod.ru"].lead
    assert "год в подвале давно не менялся: контакт мог устареть" in lead.checks
    assert "свежесть: дата страницы не указана, в подвале © 2009" in lead.checks
    assert lead.confidence == verdicts(CARDS)["obraztsov@primer-zavod.ru"].lead.confidence - 10


def test_old_dates_in_the_text_do_not_make_a_page_stale():
    # yearly reports on the About page: the site is alive, the footer year is current
    reports = page("<p>Отчётность на 31.12.2018, на 30.06.2019 и на 31.12.2019 опубликована.</p>")
    result = verdicts(CARDS, extra=[(f"{SITE}/about/", reports)])
    assert result["obraztsov@primer-zavod.ru"].reason == ""
    undated = verdicts(CARDS.replace("© 2011–2026 ", ""))["obraztsov@primer-zavod.ru"]
    assert undated.reason == "" and "свежесть: дата на странице не указана" in undated.lead.checks


def test_freshness_threshold_comes_from_the_icp():
    old = CARDS.replace("<h1>Сотрудники</h1>", "<h1>Сотрудники</h1><p>Обновлено 01.06.2025</p>")
    assert verdicts(old, icp=L.ICP(freshness_months=24))["obraztsov@primer-zavod.ru"].reason == ""
    assert verdicts(old, icp=L.ICP(freshness_months=12))["obraztsov@primer-zavod.ru"].reason == L.R_STALE


# --- ICP ----------------------------------------------------------------------------- #

def test_icp_file_is_read(tmp_path):
    path = tmp_path / "icp.json"
    path.write_text(json.dumps({
        "name": "Производители", "cities": ["Екатеринбург"], "roles": ["commercial", "sales"],
        "segments": [{"name": "станки", "keywords": ["станк", "металлообраб"]}],
        "exclude": {"companies": ["Газпром"], "keywords": ["маркетинговое агентство"], "domains": ["gigant.ru"]},
        "limits": {"companies": 7, "pages_per_site": 5, "leads_per_company": 2, "freshness_months": 12},
        "sources": [{"type": "expocentr", "exhibition": "0" * 8 + "-0000-0000-0000-" + "0" * 12}],
    }, ensure_ascii=False), "utf-8")
    icp = L.load_icp(path)
    assert (icp.max_companies, icp.pages_per_site, icp.leads_per_company, icp.freshness_months) == (7, 5, 2, 12)
    assert icp.match_segment("Токарные станки с ЧПУ") == ("станки", "станк")
    assert icp.excluded_by_name("ООО «Газпром трансгаз»") and not icp.excluded_by_name("Газпромбанк-лизинг")
    assert icp.excluded_by_name("Гигант", "shop.gigant.ru")
    assert icp.city_ok("г. Екатеринбург") and not icp.city_ok("Москва") and icp.city_ok("")


def test_unknown_role_in_icp_is_an_error(tmp_path):
    path = tmp_path / "icp.json"
    path.write_text('{"roles": ["cto_of_everything"]}', "utf-8")
    with pytest.raises(SystemExit, match="неизвестные роли"):
        L.load_icp(path)


@pytest.mark.parametrize("name", ["icp.example.json", "icp.demo.json"])
def test_example_icp_in_the_repository_loads(name):
    icp = L.load_icp(L.SCRIPT_DIR / name)
    assert icp.segments and icp.roles and icp.exclude_companies and icp.max_companies > 0
    assert icp.sources and all(L.host_of(d) and not L._is_free_mail(L.host_of(d)) for d in icp.exclude_domains)


# --- discovery ------------------------------------------------------------------------ #

def test_seeds_adapter_reads_sites(tmp_path):
    path = write_seeds(tmp_path / "seeds.csv", [
        ["Завод Пример", "https://www.primer-zavod.ru/contacts/", "Екатеринбург", "станки"],
        ["", "obrazec-stanki.ru", "", ""], ["Без сайта", "", "", ""]])
    icp = L.ICP(segments=[L.Segment("станки", ["stanki"])])
    found = list(L.SeedsAdapter(path).discover(icp))
    assert [(c.name, c.domain, c.city, c.segment) for c in found] == [
        ("Завод Пример", "primer-zavod.ru", "Екатеринбург", "станки"),
        ("obrazec-stanki", "obrazec-stanki.ru", "", "станки")]
    assert found[1].site == "https://obrazec-stanki.ru" and found[0].why == "список seeds.csv"


def test_seeds_adapter_accepts_a_plain_list_and_russian_headers(tmp_path):
    plain = tmp_path / "plain.csv"
    plain.write_text("primer-zavod.ru\nobrazec-stanki.ru\n", "utf-8")
    assert [c.domain for c in L.SeedsAdapter(plain).discover(L.ICP())] == ["primer-zavod.ru", "obrazec-stanki.ru"]
    ru = write_seeds(tmp_path / "ru.csv", [["Завод Пример", "primer-zavod.ru"]], header=("Компания", "Сайт"))
    assert [c.name for c in L.SeedsAdapter(ru).discover(L.ICP())] == ["Завод Пример"]
    bad = write_seeds(tmp_path / "bad.csv", [["a", "b"]], header=("x", "y"))
    with pytest.raises(L.DiscoveryError, match="нет колонки site"):
        list(L.SeedsAdapter(bad).discover(L.ICP()))


def test_discovery_applies_icp_filters_and_limit(tmp_path):
    path = write_seeds(tmp_path / "seeds.csv", [
        ["Завод Пример", "primer-zavod.ru", "Екатеринбург", ""], ["Завод (дубль)", "www.primer-zavod.ru", "", ""],
        ["Газпром трансгаз", "gazprom-primer.ru", "", ""], ["Справочник", "https://2gis.ru/firm/1", "", ""],
        ["Московская фирма", "moscow-primer.ru", "Москва", ""], ["Отказники", "otkaz-primer.ru", "", ""],
        ["Образец", "obrazec-stanki.ru", "", ""], ["Лишний", "lishniy-primer.ru", "", ""]])
    icp = L.ICP(cities=["Екатеринбург"], exclude_companies=["Газпром"])
    found, dropped = L.discover_all([L.SeedsAdapter(path)], icp, None, limit=2,
                                    suppressed=frozenset({L._hash("otkaz-primer.ru")}), explicit_limit=True)
    assert [c.domain for c in found] == ["primer-zavod.ru", "obrazec-stanki.ru"]
    assert dropped == {"дубль сайта": 1, "исключение ICP: «Газпром»": 1, "вместо сайта агрегатор или соцсеть": 1,
                       "город вне ICP": 1, L.R_OPTOUT: 1, L.R_OVER_LIMIT: 1}  # the cut is counted, not silent


def test_a_list_of_sites_is_not_cut_by_the_default_company_limit(tmp_path, web, caplog):
    seeds = tmp_path / "seeds.csv"
    seeds.write_text("site\n" + "\n".join(f"https://site{i}-primer.ru" for i in range(60)) + "\n", "utf-8")
    assert L.ICP().max_companies == 50
    found, dropped = L.discover_all([L.SeedsAdapter(seeds)], L.ICP(), None, L.ICP().max_companies)
    assert len(found) == 60 and not dropped  # the user's own list is the request: taken whole
    # the same through the CLI: no --limit -> 60 companies; --limit 10 -> 10 and a warning about the other 50
    out = cli(tmp_path, "--discover-only", seeds=seeds)
    assert len(read_csv(out / "companies.csv")) == 60
    with caplog.at_level("WARNING", logger="leadfinder"):
        cli(tmp_path, "--discover-only", "--limit", "10", seeds=seeds)
    assert len(read_csv(out / "companies.csv")) == 10
    assert "взято 10 компаний, 50 оставлено" in caplog.text


EXPO_ID = "0a1b2c3d-1111-2222-3333-444455556666"
EXPO = f"https://icatalog.expocentr.ru/ru/exhibitions/{EXPO_ID}"


def expo_row(num: int, name: str, country: str, rubric: str) -> str:
    return f"""<tr><td><span class="glyphicon glyphicon-star" id="{num}"></span>
      <a href="{EXPO}/exhibitors/{num}?stand=1A0{num}">{name}</a></td>
      <td><a href="{EXPO}/countries/x">{country}</a></td><td><a href="{EXPO}?hallid=1">1</a></td>
      <td><span class="badge">1A0{num}</span></td>
      <td><a class="category" href="{EXPO}/rubricator/1.1">{rubric}</a></td></tr>"""


def expo_card(name: str, site: str, city: str) -> str:
    site_row = f'<dt>Сайт:</dt><dd><a href="{site}" rel="nofollow">{site}</a></dd>' if site else ""
    return f"""<html><body><h1 class="text-center">СТАНКИ-ПРИМЕР-2026</h1><h3 class="panel-title">{name}</h3>
      <dl class="dl-horizontal"><dt>Стенд:</dt><dd>1A01</dd><dt>Страна:</dt><dd><i class="flag-RU"></i> Россия</dd>
      <dt>Город:</dt><dd>{city}</dd><dt>Телефон:</dt><dd>8 800 000-00-00</dd>{site_row}
      <dt>E-mail:</dt><dd><a href="mailto:catalog-box@{DOMAIN}">catalog-box@{DOMAIN}</a></dd>
      <dt>Описание:</dt><dd>Производитель оборудования.</dd></dl></body></html>"""


EXPO_LIST = f"""<html><body><h1 class="text-center">СТАНКИ-ПРИМЕР-2026</h1>
<h3 class="text-center">Список компаний <span class="badge badge-pill">5</span></h3>
<table class="table" id="fresh-table"><thead><tr><th>Компания</th><th>Страна</th><th>Павильон</th><th>Стенд</th>
<th>Рубрики</th></tr></thead><tbody>
{expo_row(1, "ЗАВОД ПРИМЕР, ООО", "Россия", "Токарные станки")}
{expo_row(2, "SHANGHAI SAMPLE TOOLS", "Китай", "Токарные станки")}
{expo_row(3, "ОБРАЗЕЦ ЛОГИСТИК, ООО", "Россия", "Транспортные услуги")}
{expo_row(4, "ОБРАЗЕЦ СТАНКИ, АО", "Россия", "Фрезерные станки")}
{expo_row(5, "БЕЗ САЙТА, ООО", "Россия", "Токарные станки")}
</tbody></table></body></html>"""


def test_expocentr_adapter_filters_by_country_and_keywords(web, fetcher):
    web.pages[f"{EXPO}/list"] = EXPO_LIST
    web.pages[f"{EXPO}/exhibitors/1?stand=1A01"] = expo_card("ЗАВОД ПРИМЕР, ООО", "http://www.primer-zavod.ru", "Тула")
    web.pages[f"{EXPO}/exhibitors/4?stand=1A04"] = expo_card("ОБРАЗЕЦ СТАНКИ, АО", "https://obrazec-stanki.ru/",
                                                             "Пермь")
    web.pages[f"{EXPO}/exhibitors/5?stand=1A05"] = expo_card("БЕЗ САЙТА, ООО", "", "Омск")
    icp = L.ICP(segments=[L.Segment("станки", ["станк"])])
    found = list(L.ExpocentrAdapter(f"{EXPO}/list").discover(icp, fetcher))
    assert [(c.name, c.site, c.city, c.segment, c.source) for c in found] == [
        ("ЗАВОД ПРИМЕР, ООО", "https://primer-zavod.ru", "Тула", "станки", "expocentr"),
        ("ОБРАЗЕЦ СТАНКИ, АО", "https://obrazec-stanki.ru", "Пермь", "станки", "expocentr")]
    assert "СТАНКИ-ПРИМЕР-2026" in found[0].why and "стенд 1A01" in found[0].why and "«станк»" in found[0].why
    # the Chinese exhibitor and the logistics company were filtered on the list: their cards were not opened
    assert web.fetched("/exhibitors/2") == 0 and web.fetched("/exhibitors/3") == 0
    # nothing but name, site and city is taken from the catalog
    assert all("catalog-box" not in json.dumps(L.asdict(c), ensure_ascii=False) for c in found)


def test_expocentr_adapter_needs_a_real_id_and_a_reachable_list(web, fetcher):
    with pytest.raises(L.DiscoveryError, match="не похоже на id"):
        L.ExpocentrAdapter("metalloobrabotka")
    companies, dropped = L.discover_all([L.ExpocentrAdapter(EXPO_ID)], L.ICP(), fetcher, 5)
    assert companies == [] and dropped == {"источник expocentr недоступен": 1}


def test_catalog_robots_rules_are_respected(web, fetcher):
    web.pages["https://icatalog.expocentr.ru/robots.txt"] = "User-agent: *\nDisallow: /ru/exhibitions/*/exhibitors/*\n"
    web.pages[f"{EXPO}/list"] = EXPO_LIST
    web.pages[f"{EXPO}/exhibitors/1?stand=1A01"] = expo_card("ЗАВОД ПРИМЕР, ООО", "http://primer-zavod.ru", "Тула")
    assert list(L.ExpocentrAdapter(EXPO_ID).discover(L.ICP(), fetcher)) == []
    assert web.fetched("/exhibitors/") == 0


def test_search_adapter_needs_a_key_and_uses_only_the_api(respx_mock):
    with pytest.raises(L.DiscoveryError, match="SERPER_API_KEY"):
        L.SerperAdapter("")
    route = respx_mock.post(L.SerperAdapter.ENDPOINT).mock(return_value=httpx.Response(200, json={"organic": [
        {"title": "Завод Пример — токарные станки", "link": "https://www.primer-zavod.ru/catalog/"},
        {"title": "Завод Пример на 2ГИС", "link": "https://2gis.ru/firm/123"},
        {"title": "Образец Станки | Официальный сайт", "link": "https://obrazec-stanki.ru/"}]}))
    icp = L.ICP(segments=[L.Segment("станки", ["токарные станки"])], cities=["Екатеринбург"])
    found = list(L.SerperAdapter("test-key").discover(icp))
    assert [(c.name, c.domain, c.segment) for c in found] == [
        ("Завод Пример", "primer-zavod.ru", "станки"), ("Образец Станки", "obrazec-stanki.ru", "станки")]
    request = route.calls[0].request
    assert request.headers["x-api-key"] == "test-key" and "токарные станки Екатеринбург" in request.content.decode()
    assert all(str(call.request.url) == L.SerperAdapter.ENDPOINT for call in respx_mock.calls)


def test_search_results_about_a_company_are_not_its_own_site():
    class Client:
        def post(self, url, headers=None, json=None):
            return httpx.Response(200, request=httpx.Request("POST", url), json={"organic": [
                {"link": "https://www.export-base.ru/company/zavod-primer/", "title": "Завод Пример - контакты"},
                {"link": "https://spravka-zavodov.ru/firms/zavod-primer/", "title": "Завод Пример - руководство"},
                {"link": "https://rocketreach.co/zavod-primer-management", "title": "Zavod Primer management team"},
                {"link": "https://vk.com/primer_zavod", "title": "Завод Пример | ВКонтакте"},
                {"link": "https://primer-zavod.ru/about/", "title": "О компании - Завод Пример"},
                {"link": "https://obrazec-stanki.ru/", "title": "Купить станки в Перми"}]})

        def close(self):
            pass

    icp = L.ICP(segments=[L.Segment("станки", ["станки чпу"])])
    found, _ = L.discover_all([L.SerperAdapter("key", client=Client())], icp, None, 10)
    # a contact database, an unknown directory (a deep page whose title does not name the owner of the
    # domain) and a social network are dropped; the company's own page and a root page stay
    assert [(c.name, c.domain) for c in found] == [("Завод Пример", "primer-zavod.ru"),
                                                   ("Купить станки в Перми", "obrazec-stanki.ru")]


def test_search_source_is_skipped_without_a_key(tmp_path):
    seeds = write_seeds(tmp_path / "s.csv", [["Завод Пример", "primer-zavod.ru", "", ""]])
    args = L.parse_args(["--seeds", str(seeds)])
    icp = L.ICP(sources=[{"type": "search"}])
    assert [a.name for a in L.build_adapters(args, icp, {})] == ["seeds"]
    args = L.parse_args([])
    assert L.build_adapters(args, icp, {}) == []
    assert [a.name for a in L.build_adapters(args, icp, {"SERPER_API_KEY": "k"})] == ["search"]
    with pytest.raises(L.DiscoveryError):
        L.build_adapters(L.parse_args(["--search"]), L.ICP(), {})


# --- crawl ---------------------------------------------------------------------------- #

def test_page_kinds_by_url_and_link_text():
    assert L.classify_page(f"{SITE}/contacts/", "Контакты") == ("contacts", 11)
    assert L.classify_page(f"{SITE}/kontakty.html")[0] == "contacts"
    assert L.classify_page(f"{SITE}/company/staff/")[0] == "team"
    assert L.classify_page(f"{SITE}/about/rukovodstvo/")[0] == "team"
    assert L.classify_page(f"{SITE}/?page_id=17", "Контакты")[0] == "contacts"
    assert L.classify_page(f"{SITE}/o-kompanii/")[0] == "about"
    assert L.classify_page(f"{SITE}/catalog/stanki/") is None
    assert L.classify_page(f"{SITE}/contacts/price.pdf") is None
    # a page deeper inside a section is worth less than the section itself
    assert L.classify_page(f"{SITE}/company/staff/")[1] > L.classify_page(f"{SITE}/company/staff/otdel-7/")[1]


def test_crawl_ranks_contact_pages_and_stays_on_the_site(web, fetcher):
    web.site(SITE, {"/": HOME, "/contacts/": TABLE, "/company/staff/": CARDS, "/about/": page("<p>О заводе.</p>"),
                    "/catalog/": page("<p>Каталог станков.</p>")})
    crawl = L.crawl_site(fetcher, company(), L.ICP())
    assert [(p.kind, p.url.rstrip("/")) for p in crawl.pages] == [
        ("home", SITE), ("contacts", f"{SITE}/contacts"), ("team", f"{SITE}/company/staff"),
        ("about", f"{SITE}/about")]
    assert web.fetched("/catalog/") == 0 and web.fetched("partner-site.ru") == 0


def test_crawl_respects_the_page_budget(web, fetcher):
    web.site(SITE, {"/": HOME, "/contacts/": TABLE, "/company/staff/": CARDS, "/about/": page("<p>О заводе.</p>")})
    crawl = L.crawl_site(fetcher, company(), L.ICP(pages_per_site=2))
    assert [p.kind for p in crawl.pages] == ["home", "contacts"] and crawl.attempts == 2
    assert web.fetched("/company/staff/") == 0


def test_crawl_uses_sitemap_when_the_menu_has_no_links(web, fetcher):
    bare_home = "<html><head><title>Завод Пример</title></head><body><div id='app'>Станки с ЧПУ</div></body></html>"
    sitemap = f"""<?xml version="1.0"?><urlset><url><loc>{SITE}/</loc></url>
      <url><loc>{SITE}/o-zavode/rukovodstvo/</loc></url><url><loc>{SITE}/catalog/tokarnye/</loc></url>
      <url><loc>https://chuzhoy-sayt.ru/contacts/</loc></url></urlset>"""
    web.site(SITE, {"/": bare_home, "/sitemap.xml": sitemap, "/o-zavode/rukovodstvo/": DEFLIST,
                    "/kontakty/": TABLE})
    crawl = L.crawl_site(fetcher, company(), L.ICP())
    assert {(p.kind, p.url.rstrip("/")) for p in crawl.pages} == {
        ("home", SITE), ("team", f"{SITE}/o-zavode/rukovodstvo"), ("contacts", f"{SITE}/kontakty")}
    assert web.fetched("chuzhoy-sayt.ru") == 0 and web.fetched("/catalog/") == 0


def test_crawl_opens_person_pages_of_decision_makers_only(web, fetcher):
    listing = page("""<h1>Команда</h1><div class="team">
      <div class="item"><a href="/team/obraztsov/">Образцов Пётр</a><div>Коммерческий директор</div></div>
      <div class="item"><a href="/team/primerova/">Примерова Анна</a><div>Менеджер по продажам</div></div>
      <div class="item"><a href="/team/testov/">Тестов Олег</a><div>Генеральный директор</div></div></div>""")
    person = page("""<div class="person"><h1>Образцов Пётр Ильич</h1><p>Коммерческий директор</p>
      <p>E-mail: obraztsov@primer-zavod.ru</p></div>""")
    home = HOME.replace('<a href="/company/staff/">Сотрудники</a>', '<a href="/team/">Команда</a>')
    web.site(SITE, {"/": home, "/team/": listing, "/team/obraztsov/": person,
                    "/team/testov/": person.replace("Образцов Пётр Ильич", "Тестов Олег Петрович")
                    .replace("obraztsov@", "testov@").replace("Коммерческий", "Генеральный"),
                    "/team/primerova/": page("<h1>Примерова Анна</h1>")})
    crawl = L.crawl_site(fetcher, company(), L.ICP())
    assert {p.url for p in crawl.pages if p.kind == "person"} == {f"{SITE}/team/obraztsov/", f"{SITE}/team/testov/"}
    assert web.fetched("/team/primerova/") == 0  # ordinary staff pages are not opened


def test_listing_where_the_whole_card_is_a_link(web, fetcher):
    # every card is one <a>; the sales head must be opened before the CEO, staff never
    def card(slug, name, title):
        return f'<a class="card" href="/team/{slug}.html"><div class="n">{name}</div><div class="t">{title}</div></a>'

    listing = page('<h1>Команда</h1><div class="grid">' + "".join([
        card("testov", "Олег Тестов", "Генеральный директор"), card("primerova", "Анна Примерова", "Ведущий менеджер"),
        card("chernovikov", "Глеб Черновиков", "Руководитель отдела разработки"),
        card("obraztsov", "Пётр Образцов", "Руководитель отдела продаж"),
        card("shablonova", "Ирина Шаблонова", "Дизайнер")]) + "</div>")
    links, seen = L.person_links(L.SitePage(f"{SITE}/team/", "team", L.annotate(L.flatten(f"{SITE}/team/", listing))),
                                 L.ICP(), {DOMAIN})
    assert [(score, url.rsplit("/", 1)[1]) for score, url in links] == [
        (7.9, "obraztsov.html"), (7.8, "testov.html"), (5.0, "chernovikov.html")]
    assert len(seen) == 5  # the staff pages are known as person pages and are never queued as "team" pages
    home = HOME.replace('<a href="/company/staff/">Сотрудники</a>', '<a href="/team/">Команда</a>')
    web.site(SITE, {"/": home, "/team/": listing})
    L.crawl_site(fetcher, company(), L.ICP())
    assert web.fetched("/team/primerova") == 0 and web.fetched("/team/shablonova") == 0
    assert web.fetched("/team/obraztsov") == 1


def test_tracking_parameters_and_closed_urls_do_not_eat_the_budget(web, fetcher):
    home = HOME.replace('<a href="/contacts/">Контакты</a>', " ".join(
        f'<a href="/contacts/?from={tag}">Контакты</a>' for tag in ("menu", "footer", "banner", "popup")))
    web.site(SITE, {"/": home, "/contacts/": TABLE, "/company/staff/": CARDS, "/about/": page("<p>О заводе.</p>")},
             robots="User-agent: *\nDisallow: /company/\nDisallow: /*?from=\n")
    crawl = L.crawl_site(fetcher, company(), L.ICP(pages_per_site=3))
    assert [p.kind for p in crawl.pages] == ["home", "contacts", "about"]
    assert web.fetched("/contacts/") == 1 and web.fetched("?from=") == 0 and web.fetched("/company/staff/") == 0


def test_press_page_is_worth_a_look_and_its_archive_is_not():
    assert L.classify_page(f"{SITE}/press-center/")[0] == "press"
    assert L.classify_page(f"{SITE}/company/", "О компании")[0] == "about"
    assert L.classify_page(f"{SITE}/press-center/2024/reliz-7/") is None
    assert L.classify_page(f"{SITE}/company/news/vystavka/") is None


def test_robots_txt_closes_a_page(web, fetcher):
    web.site(SITE, {"/": HOME, "/contacts/": TABLE, "/company/staff/": CARDS},
             robots="User-agent: *\nDisallow: /company/\n")
    crawl = L.crawl_site(fetcher, company(), L.ICP())
    assert f"{SITE}/company/staff/" not in {p.url for p in crawl.pages}
    assert web.fetched("/company/staff/") == 0 and web.fetched("/contacts/") == 1


def test_robots_txt_closing_the_whole_site_means_no_crawl(web, fetcher):
    web.site(SITE, {"/": HOME, "/contacts/": TABLE}, robots="User-agent: LeadFinderBot\nDisallow: /\n")
    crawl = L.crawl_site(fetcher, company(), L.ICP())
    assert crawl.pages == [] and "robots.txt" in crawl.error
    assert web.fetched("/contacts/") == 0


def test_one_request_per_second_per_host(web, tmp_path, monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(P.time, "sleep", slept.append)
    f = L.LeadFetcher(cache_dir=tmp_path / "c", retries=0)
    web.site(SITE, {"/": HOME, "/contacts/": TABLE})
    assert f.delay == 1.0 and "LeadFinderBot" in f.user_agent
    f.get(f"{SITE}/")
    f.get(f"{SITE}/contacts/")
    f.close()
    # robots.txt, the homepage, the contacts page: two full pauses (the clock does not move while sleep is faked)
    assert len(slept) == 2 and all(0.9 < pause <= 1.0 for pause in slept)


def test_proxy_comes_from_polza_socks(monkeypatch):
    assert L.normalize_proxy("127.0.0.1:1080") == "socks5h://127.0.0.1:1080"
    assert L.normalize_proxy("socks5://10.0.0.1:9050") == "socks5://10.0.0.1:9050"
    assert L.normalize_proxy("") == ""
    assert L.LeadFetcher(proxy="127.0.0.1:1080").proxy == "socks5h://127.0.0.1:1080"
    with pytest.raises(SystemExit, match="TUNNEL DOWN"):
        L.run(["--seeds", "x.csv"], env={"POLZA_SOCKS": "127.0.0.1:1"})


# --- whole pipeline -------------------------------------------------------------------- #

def run_ctx(fetcher, icp=None, backend=None, **settings) -> L.RunCtx:
    return L.RunCtx(fetcher=fetcher, cfg=L.Settings(icp=icp or L.ICP(), mx=FakeMX(), **settings), backend=backend)


def test_company_with_leads(web, fetcher):
    web.site(SITE, {"/": HOME, "/contacts/": TABLE, "/company/staff/": CARDS})
    result = L.process_company(company(segment="станки"), run_ctx(fetcher))
    assert result.status == "lead" and result.pages == 3
    assert [(r["Фамилия"], r["Email"], r["роль"]) for r in result.leads] == [
        ("Образцов", "obraztsov@primer-zavod.ru", "commercial"),
        ("Тестов", "gd@primer-zavod.ru", "ceo"),
        ("Шаблонова", "shablonova@primer-zavod.ru", "marketing")]
    first = result.leads[0]
    assert first["Компания"] == "Завод Пример" and first["сегмент"] == "станки"
    assert first["источник"] == f"{SITE}/company/staff/" and first["Email"] in first["фрагмент"]
    # «Телефон» is the company's number from the site header, not the direct line printed in the card
    assert first["Телефон"] == "+7 (495) 000-00-00" and "общий номер компании" in first["проверки"]
    # the same person under a second address (initials in the table) is not a second lead
    assert result.rejects[L.R_DUPLICATE] == 1 and result.rejects[L.R_NOT_DM] == 1


def test_leads_per_company_limit(web, fetcher):
    web.site(SITE, {"/": HOME, "/contacts/": TABLE, "/company/staff/": CARDS})
    result = L.process_company(company(), run_ctx(fetcher, L.ICP(leads_per_company=1)))
    assert [r["Email"] for r in result.leads] == ["obraztsov@primer-zavod.ru"]
    assert result.rejects[L.R_LIMIT] == 2


def test_company_without_a_lead_is_reported_with_the_reason(web, fetcher):
    web.site(SITE, {"/": HOME, "/contacts/": ONLY_GENERIC})
    result = L.process_company(company(), run_ctx(fetcher))
    assert result.status == "no_lead" and result.leads == []
    row = result.no_lead
    assert row["общий_контакт"] == "sales@primer-zavod.ru" and row["телефон"] == "+7 (495) 000-00-00"
    assert "ЛПР на сайте назван (Генеральный директор)" in row["причина"] and "общие ящики" in row["причина"]
    # the named director has no address on the page: nothing is constructed for him
    assert "testov" not in json.dumps(L.asdict(result), ensure_ascii=False).lower()


def test_one_address_printed_for_several_people_is_a_team_box(web, fetcher):
    team = page("""<h1>Наша команда</h1><div class="team">
      <div class="item"><div>Образцов Пётр Ильич</div><div>Директор по развитию</div>
        <div>team7@primer-zavod.ru</div></div>
      <div class="item"><div>Тестов Олег Петрович</div><div>Генеральный директор</div>
        <div>team7@primer-zavod.ru</div></div></div>""")
    web.site(SITE, {"/": HOME, "/company/staff/": team})
    result = L.process_company(company(), run_ctx(fetcher))
    assert result.leads == [] and result.rejects[L.R_GENERIC] >= 1
    assert result.no_lead["общий_контакт"] in ("team7@primer-zavod.ru", f"info@{DOMAIN}")


def test_unreachable_site_is_an_error_to_retry(web, fetcher):
    web.pages[f"{SITE}/robots.txt"] = httpx.ConnectError("connection refused")
    result = L.process_company(company(), run_ctx(fetcher))
    assert result.status == "error" and result.no_lead["причина"].startswith("сайт недоступен")


def test_icp_exclusion_by_homepage_keyword(web, fetcher):
    agency = HOME.replace("Производим токарные станки с ЧПУ", "Маркетинговое агентство полного цикла")
    web.site(SITE, {"/": agency, "/company/staff/": CARDS})
    icp = L.ICP(exclude_keywords=["маркетинговое агентство"])
    result = L.process_company(company(), run_ctx(fetcher, icp))
    assert result.status == "no_lead" and result.leads == []
    assert result.no_lead["причина"] == "исключение ICP: «маркетинговое агентство» на главной странице"


def test_icp_exclusion_by_staff_count(web, fetcher):
    giant = HOME.replace("поставляем их", "5 000 сотрудников поставляют их")
    web.site(SITE, {"/": giant, "/company/staff/": CARDS})
    result = L.process_company(company(), run_ctx(fetcher, L.ICP(max_staff=500)))
    assert result.leads == [] and "5000 сотрудников" in result.no_lead["причина"]


def test_segment_and_city_are_filled_from_the_site(web, fetcher):
    contacts = CARDS.replace("<h1>Сотрудники</h1>", "<h1>Контакты</h1><p>620000, г. Екатеринбург, ул. Примерная, 1</p>")
    web.site(SITE, {"/": HOME, "/contacts/": contacts})
    icp = L.ICP(segments=[L.Segment("станки", ["станки с чпу"])])
    result = L.process_company(company(), run_ctx(fetcher, icp))
    assert (result.leads[0]["сегмент"], result.leads[0]["город"]) == ("станки", "Екатеринбург")


# --- LLM adjudication -------------------------------------------------------------------- #

class Backend:
    name = "fake"

    def __init__(self, answer):
        self.answer, self.prompts = answer, []

    def complete(self, system: str, user: str) -> str:
        self.prompts.append(user)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer if isinstance(self.answer, str) else json.dumps(self.answer, ensure_ascii=False)


QUOTE = "Клиентов Игорь Ильич, коммерческий директор, и Заказова Анна Сергеевна, ассистент. Для связи: " \
        "komdir77@primer-zavod.ru"


def llm_run(web, fetcher, answer):
    web.site(SITE, {"/": HOME, "/contacts/": AMBIGUOUS})
    backend = Backend(answer)
    return L.process_company(company(), run_ctx(fetcher, backend=backend)), backend


def test_without_llm_an_ambiguous_block_gives_no_lead(web, fetcher):
    web.site(SITE, {"/": HOME, "/contacts/": AMBIGUOUS})
    result = L.process_company(company(), run_ctx(fetcher))
    assert result.leads == [] and result.rejects[L.R_AMBIGUOUS] == 1


def test_llm_chooses_among_extracted_candidates(web, fetcher):
    result, backend = llm_run(web, fetcher, {"name": 1, "title": 1, "fragment": QUOTE})
    assert len(backend.prompts) == 1 and "1. Клиентов Игорь Ильич" in backend.prompts[0]
    lead = result.leads[0]
    assert (lead["Фамилия"], lead["Должность"], lead["Email"]) == (
        "Клиентов", "коммерческий директор", "komdir77@primer-zavod.ru")
    assert lead["фрагмент"] == QUOTE and "привязку выбрала LLM" in lead["проверки"]
    assert lead["тип_адреса"] == L.TYPE_ROLE


@pytest.mark.parametrize("answer, why", [
    ({"name": 1, "title": 1, "fragment": "Клиентов Игорь Ильич — владелец ящика komdir77@primer-zavod.ru"},
     "цитаты нет на странице"),
    ({"name": 3, "title": 1, "fragment": QUOTE}, "номер имени вне списка"),
    ({"name": 1, "title": 9, "fragment": QUOTE}, "номер должности вне списка"),
    ({"name": "Директоров Иван Иванович", "title": 1, "fragment": QUOTE}, "номер имени вне списка"),
    ({"name": 2, "title": 2, "fragment": "коммерческий директор, и"}, "в цитате нет выбранного имени"),
    ({"name": None, "title": None, "fragment": ""}, L.LLM_NO_NAME),
    ("Думаю, это Клиентов.", "ответ не JSON"),
])
def test_llm_answer_that_is_not_on_the_page_is_rejected(answer, why):
    cand = candidates(AMBIGUOUS)["komdir77@primer-zavod.ru"]
    raw = answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)
    assert L.apply_llm_answer(cand, raw).startswith(why)
    assert cand.person is None  # nothing was attached


def test_rejected_llm_answer_gives_no_lead(web, fetcher):
    invented = {"name": 1, "title": 1, "fragment": "Клиентов Игорь Ильич, komdir77@primer-zavod.ru, личный ящик"}
    result, backend = llm_run(web, fetcher, invented)
    assert len(backend.prompts) == 1 and result.leads == [] and result.rejects[L.R_AMBIGUOUS] == 1


def test_llm_outage_changes_nothing(web, fetcher):
    result, _ = llm_run(web, fetcher, P.LLMError("CLI не найден"))
    assert result.leads == [] and result.rejects[L.R_AMBIGUOUS] == 1


WEAK = page("""<h1>Контакты</h1><div class="text"><p>Коммерческий директор<br>Шаблонов П.И.<br>
  e-mail: kd7@primer-zavod.ru<br><br>Ведущий инженер<br>Примерова Анна Сергеевна<br>
  e-mail: primerova@primer-zavod.ru</p></div>""")
WEAK_QUOTE = "Коммерческий директор Шаблонов П.И. e-mail: kd7@primer-zavod.ru"


def test_llm_confirms_a_weak_pairing_and_keeps_the_evidence(web, fetcher):
    web.site(SITE, {"/": HOME, "/contacts/": WEAK})
    plain = L.process_company(company(), run_ctx(fetcher)).leads[0]
    assert int(plain["уверенность"]) < L.LLM_BELOW  # initials only, a post box, read in order: a weak lead
    backend = Backend({"name": 1, "title": 1, "fragment": WEAK_QUOTE})
    ctx = run_ctx(fetcher, backend=backend)
    confirmed = L.process_company(company(), ctx).leads[0]
    assert len(backend.prompts) == 1 and (ctx.llm_calls, ctx.llm_accepted) == (1, 1)
    assert int(confirmed["уверенность"]) == int(plain["уверенность"]) + 5
    assert "привязку подтвердила LLM" in confirmed["проверки"] and confirmed["фрагмент"] == plain["фрагмент"]


def test_llm_that_cannot_tell_removes_a_weak_lead(web, fetcher):
    web.site(SITE, {"/": HOME, "/contacts/": WEAK})
    result = L.process_company(company(), run_ctx(fetcher, backend=Backend({"name": None, "title": None})))
    assert result.leads == [] and result.rejects[L.R_AMBIGUOUS] == 1


def test_llm_confirmation_keeps_a_mailto_card(web, fetcher):
    card = page("""<div class="item"><div>Шаблонов П.И.</div><div>Коммерческий директор</div>
      <a href="mailto:kd7@primer-zavod.ru">Написать письмо</a></div>""")
    web.site(SITE, {"/": HOME, "/contacts/": card})
    answer = {"name": 1, "title": 1, "fragment": "Шаблонов П.И. Коммерческий директор Написать письмо"}
    assert L.process_company(company(), run_ctx(fetcher, backend=Backend(answer))).leads == []  # not printed
    backend = Backend(answer)
    lead = L.process_company(company(), run_ctx(fetcher, backend=backend, mailto="card")).leads[0]
    assert len(backend.prompts) == 1 and lead["Email"] == "kd7@primer-zavod.ru"
    assert lead["фрагмент"].endswith("[ссылка mailto: kd7@primer-zavod.ru]")


def test_llm_is_not_asked_about_clear_blocks(web, fetcher):
    web.site(SITE, {"/": HOME, "/company/staff/": CARDS})
    backend = Backend({"name": None})
    result = L.process_company(company(), run_ctx(fetcher, backend=backend))
    assert backend.prompts == [] and len(result.leads) == 2


# --- CLI: outputs, resume, opt-out --------------------------------------------------------- #

SECOND = "https://obrazec-stanki.ru"


@pytest.fixture
def two_sites(web):
    web.site(SITE, {"/": HOME, "/contacts/": TABLE, "/company/staff/": CARDS})
    web.site(SECOND, {"/": HOME.replace("primer-zavod", "obrazec-stanki").replace("Завод Пример", "Образец Станки"),
                      "/contacts/": ONLY_GENERIC.replace("primer-zavod", "obrazec-stanki")})
    return web


def cli(tmp_path, *extra, seeds=None, **kw):
    seeds = seeds or write_seeds(tmp_path / "seeds.csv", [
        ["Завод Пример", SITE, "Екатеринбург", "станки"], ["Образец Станки", SECOND, "Пермь", "станки"],
        ["Недоступный", "https://nedostupen-primer.ru", "", ""]])
    f = L.LeadFetcher(cache_dir=tmp_path / "cache", delay=0, retries=0, backoff=0,
                      suppressed=L.load_suppressed(tmp_path / "out"))
    try:
        code = L.run(["--seeds", str(seeds), "--out", str(tmp_path / "out"), "--cache-dir", str(tmp_path / "cache"),
                      "--workers", "1", "--retry-wait", "0", *extra], fetcher=f, mx=FakeMX(), env={}, **kw)
    finally:
        f.close()
    assert code == 0
    return tmp_path / "out"


def test_cli_writes_leads_no_lead_and_summary(tmp_path, two_sites):
    two_sites.pages["https://nedostupen-primer.ru/robots.txt"] = httpx.ConnectError("refused")
    out = cli(tmp_path)
    leads = read_csv(out / "leads.csv")
    header = list(leads[0])
    assert header[:6] == ["Имя", "Фамилия", "Должность", "Email", "Телефон", "Компания"]  # the course's order
    assert header[6:15] == ["site", "город", "сегмент", "тип_адреса", "источник", "фрагмент", "дата_проверки",
                            "уверенность", "проверки"]
    assert [r["Email"] for r in leads] == ["obraztsov@primer-zavod.ru", "gd@primer-zavod.ru",
                                           "shablonova@primer-zavod.ru"]
    assert leads[0]["тип_адреса"] == L.TYPE_PERSONAL and leads[1]["тип_адреса"] == L.TYPE_ROLE
    assert all(r["дата_проверки"] == "2026-10-03" and r["Email"] in r["фрагмент"] for r in leads)
    no_lead = {r["Компания"]: r for r in read_csv(out / "no_lead.csv")}
    assert no_lead["Образец Станки"]["общий_контакт"] == "sales@obrazec-stanki.ru"
    assert "общие ящики" in no_lead["Образец Станки"]["причина"]
    assert no_lead["Недоступный"]["причина"].startswith("сайт недоступен")
    summary = json.loads((out / "summary.json").read_text("utf-8"))
    assert (summary["компаний_просмотрено"], summary["с_лидом"], summary["без_лида"], summary["сайт_недоступен"],
            summary["лидов"]) == (3, 1, 1, 1, 3)
    assert summary["лиды_по_типу_адреса"] == {L.TYPE_PERSONAL: 2, L.TYPE_ROLE: 1}
    assert summary["отклонено_по_причинам"][L.R_GENERIC] >= 3 and summary["отклонено_по_причинам"][L.R_NOT_DM] == 1
    assert summary["страниц_разобрано"] == 5 and summary["запросов_в_сеть"] > 5
    assert "лидов: 3" in (out / "summary.md").read_text("utf-8")
    assert [r["site"] for r in read_csv(out / "companies.csv")] == [SITE, SECOND, "https://nedostupen-primer.ru"]


def test_cli_resumes_and_retries_only_unreachable_sites(tmp_path, two_sites):
    two_sites.pages["https://nedostupen-primer.ru/robots.txt"] = httpx.ConnectError("refused")
    out = cli(tmp_path)
    first_calls = len(two_sites.calls)
    cached = list((tmp_path / "cache").glob("*.json"))
    assert any('"status": 0' in path.read_text("utf-8") for path in cached)  # the failure is remembered ...
    for path in cached:
        if "primer-zavod" in path.read_text("utf-8") or "obrazec-stanki" in path.read_text("utf-8"):
            path.unlink()  # even with an empty cache finished companies are not crawled again
    del two_sites.pages["https://nedostupen-primer.ru/robots.txt"]  # ... and still the site is asked again
    two_sites.site("https://nedostupen-primer.ru", {"/": HOME, "/contacts/": DEFLIST})
    cli(tmp_path)
    new_calls = two_sites.calls[first_calls:]
    assert new_calls and all("nedostupen-primer.ru" in url for url in new_calls)
    assert len(read_csv(out / "leads.csv")) == 3 + 2
    summary = json.loads((out / "summary.json").read_text("utf-8"))
    assert summary["сайт_недоступен"] == 0 and summary["с_лидом"] == 2
    calls = len(two_sites.calls)
    cli(tmp_path, "--refresh")
    assert len(two_sites.calls) > calls


def test_discover_only_does_not_crawl(tmp_path, two_sites):
    out = cli(tmp_path, "--discover-only")
    assert len(read_csv(out / "companies.csv")) == 3 and two_sites.calls == []
    assert not (out / "leads.csv").exists()


def test_forget_an_address(tmp_path, two_sites, capsys):
    out = cli(tmp_path)
    cache = tmp_path / "cache"
    address = "obraztsov@primer-zavod.ru"
    assert any(printed(address, p.read_text("utf-8")) for p in cache.glob("*.json"))
    assert L.run(["--forget", "Obraztsov@primer-zavod.ru", "--out", str(out), "--cache-dir", str(cache)]) == 0
    assert "лидов_удалено: 1" in capsys.readouterr().out
    assert [r["Email"] for r in read_csv(out / "leads.csv")] == ["gd@primer-zavod.ru", "shablonova@primer-zavod.ru"]
    for path in list(out.iterdir()) + list(cache.glob("*.json")):
        assert not printed(address, path.read_text("utf-8-sig")), path.name
    suppression = json.loads((out / "suppression.json").read_text("utf-8"))
    # hashes only: the address and the person (so that his second address is not emitted either)
    assert set(suppression["sha256"]) == {L._hash(address), L.person_hash("Образцов", "Пётр", DOMAIN)}
    assert "obraztsov" not in json.dumps(suppression).lower()
    # the next run fetches the pages again and still never emits or stores the address
    cli(tmp_path, "--refresh")
    assert [r["Email"] for r in read_csv(out / "leads.csv")] == ["gd@primer-zavod.ru", "shablonova@primer-zavod.ru"]
    cached = [p.read_text("utf-8") for p in cache.glob("*.json")]
    assert not any(printed(address, text) for text in cached) and any(L.SCRUBBED in text for text in cached)
    summary = json.loads((out / "summary.json").read_text("utf-8"))
    assert summary["лидов"] == 2 and summary["отклонено_по_причинам"][L.R_OPTOUT] >= 1


def test_forget_a_domain(tmp_path, two_sites):
    out = cli(tmp_path)
    cache = tmp_path / "cache"
    stats = L.forget("https://www.primer-zavod.ru/", out, cache)
    assert stats["компаний_удалено"] == 1 and stats["лидов_удалено"] == 3 and stats["страниц_кэша_удалено"] >= 3
    assert read_csv(out / "leads.csv") == []
    assert not any("primer-zavod.ru" in p.read_text("utf-8") for p in cache.glob("*.json"))
    assert "primer-zavod" not in (out / "state.json").read_text("utf-8")
    calls = len(two_sites.calls)
    cli(tmp_path)
    assert not any("primer-zavod.ru" in url for url in two_sites.calls[calls:])  # the company is skipped for good
    summary = json.loads((out / "summary.json").read_text("utf-8"))
    assert summary["отсеяно_на_поиске"] == {L.R_OPTOUT: 1} and summary["лидов"] == 0


def test_forget_a_generic_contact(tmp_path, two_sites):
    out = cli(tmp_path)
    stats = L.forget("sales@obrazec-stanki.ru", out, tmp_path / "cache")
    assert stats["общих_контактов_удалено"] == 1
    assert {r["Компания"]: r["общий_контакт"] for r in read_csv(out / "no_lead.csv")}["Образец Станки"] == ""


def test_forget_rejects_garbage(tmp_path):
    with pytest.raises(SystemExit, match="не адрес и не домен"):
        L.forget("   ", tmp_path / "out", tmp_path / "cache")


def test_no_sending_and_no_guessing_code():
    """The product rules, checked against the source: no SMTP, no mailbox probing, no pattern guessing."""
    source = (L.SCRIPT_DIR / "leadfinder.py").read_text("utf-8")
    for banned in ("smtplib", "RCPT TO", "VRFY", "sendmail", "linkedin.com/in", "imaplib"):
        assert banned not in source


def test_fixtures_of_this_file_are_invented():
    """No phone number or mailbox of a real company or person in the fixtures: every number here is made
    of zeros, every address stands on an invented domain, a placeholder or a free-mail service."""
    source = (L.SCRIPT_DIR / "tests" / "test_leadfinder.py").read_text("utf-8")
    numbers = {re.sub(r"\D", "", m.group(0))
               for m in re.finditer(r"(?<![\w@.])\+?[78][\d\s()\-]{9,16}\d(?!\d)", source)}
    assert len(numbers) > 10 and not sorted(n for n in numbers if "000" not in n)
    domains = set(re.findall(r"(?<=[a-z0-9}])@([a-z0-9-]+(?:\.[a-z0-9-]+)+)", source.lower()))
    invented = ("primer", "obrazec", "drugaya-firma", "postavshik", "example.", "domain.")
    real = sorted(d for d in domains if not d.startswith(invented) and not d.endswith("primera.ru")
                  and not L._is_free_mail(d) and not d.endswith(".png"))
    assert len(domains) > 5 and not real


# =========================================================================================== #
# Regression tests for the product rules (one synthetic case per failure class the review found)
# =========================================================================================== #

def redirect(to: str, status: int = 301) -> httpx.Response:
    return httpx.Response(status, headers={"location": to})


DIRECTOR = """<div class="item"><div>Образцов Пётр Ильич</div><div>Коммерческий директор</div>
  <div>Тел.: +7 (495) 000-00-01</div><div>E-mail: obraztsov@{domain}</div></div>"""


# --- 1. the crawl never leaves the company's own site ---------------------------------------- #

@pytest.mark.parametrize("host", ["zavod.nnov.ru", "www.zavod.nnov.ru", "shop.zavod.nnov.ru"])
def test_site_on_a_shared_suffix_is_the_host_not_the_suffix(host):
    assert L.site_of(host) == "zavod.nnov.ru" != L.site_of("drugaya-firma.nnov.ru")
    assert L.site_of("zavod.tilda.ws") != L.site_of("konkurent.tilda.ws")
    assert L.site_of("shop.primer-zavod.ru") == L.site_of("www.primer-zavod.ru") == "primer-zavod.ru"
    assert L.site_of("пример.рф") == L.site_of("www.xn--e1afmkfd.xn--p1ai")


def test_redirect_of_the_homepage_to_a_social_network_is_not_followed(web, fetcher):
    web.pages[f"{SITE}/"] = redirect("https://vk.com/primer_zavod", 302)
    web.pages["https://vk.com/primer_zavod"] = page('<a href="https://vk.com/primer_zavod/contacts/">Контакты</a>')
    web.pages["https://vk.com/primer_zavod/contacts/"] = page(DIRECTOR.format(domain=DOMAIN))
    result = L.process_company(company(), run_ctx(fetcher))
    assert web.fetched("vk.com") == 0  # not a single request leaves for the social network
    assert result.leads == [] and result.status == "no_lead"  # the answer is final: nothing to retry
    assert "перенаправляет на vk.com" in result.no_lead["причина"]


def test_redirect_to_an_unrelated_domain_is_not_followed(web, fetcher):
    web.site(SITE, {"/": HOME})
    web.pages[f"{SITE}/contacts/"] = redirect("https://drugaya-firma.ru/contacts/")
    web.pages["https://drugaya-firma.ru/contacts/"] = page(DIRECTOR.format(domain="drugaya-firma.ru"))
    result = L.process_company(company(), run_ctx(fetcher))
    assert web.fetched("drugaya-firma.ru") == 0 and result.leads == []
    # and the homepage itself: a site that now belongs to somebody else
    web.pages["https://obrazec-stanki.ru/"] = redirect("https://domain-parking.ru/lot/obrazec-stanki")
    parked = L.process_company(company("Образец Станки", "https://obrazec-stanki.ru"), run_ctx(fetcher))
    assert web.fetched("domain-parking.ru") == 0 and parked.status == "no_lead"
    assert "это не сайт компании" in parked.no_lead["причина"]


def test_company_that_moved_to_a_similar_domain_is_followed_and_the_move_is_written_down(tmp_path, web):
    new = "https://primer-zavod.com"
    web.pages[f"{SITE}/"] = redirect(f"{new}/")
    web.site(new, {"/": HOME.replace(DOMAIN, "primer-zavod.com"),
                   "/contacts/": page(DIRECTOR.format(domain="primer-zavod.com"))},
             robots="User-agent: *\nDisallow: /company/\n")
    seeds = write_seeds(tmp_path / "seeds.csv", [["Завод Пример", SITE, "", ""]])
    out = cli(tmp_path, seeds=seeds)
    lead = read_csv(out / "leads.csv")[0]
    assert lead["Email"] == "obraztsov@primer-zavod.com" and lead["источник"] == f"{new}/contacts/"
    assert "сайт компании перенаправляет на primer-zavod.com" in lead["проверки"]
    assert read_csv(out / "companies.csv")[0]["переезд"] == "сайт перенаправляет на primer-zavod.com, обход шёл там"
    # the new host is a host like any other: its robots.txt is read before its first page and obeyed
    assert web.calls.index(f"{new}/robots.txt") < web.calls.index(f"{new}/")
    assert web.fetched("/company/staff/") == 0


def test_link_to_another_company_on_the_same_shared_suffix_is_not_opened(web, fetcher):
    own, other = "https://zavod.nnov.ru", "https://drugaya-firma.nnov.ru"
    web.site(own, {"/": page(f'<h1>Завод</h1><a href="{other}/contacts/">Контакты</a>')})
    web.pages[f"{other}/contacts/"] = page(DIRECTOR.format(domain="drugaya-firma.nnov.ru"))
    result = L.process_company(company("Завод", own), run_ctx(fetcher))
    assert web.fetched("drugaya-firma.nnov.ru") == 0 and result.leads == []


def test_page_cached_by_older_code_with_a_blind_redirect_is_not_used(web, fetcher, tmp_path):
    # the old fetcher followed redirects itself: its cache may hold a page of another site under the company's URL
    stale = {"url": f"{SITE}/", "final_url": "https://vk.com/primer_zavod", "status": 200,
             "html": page(DIRECTOR.format(domain=DOMAIN)), "error": "", "fetched_at": 1790000000}
    fetcher._cache_path(f"{SITE}/").write_text(json.dumps(stale), "utf-8")
    web.site(SITE, {"/": HOME})
    crawl = L.crawl_site(fetcher, company(), L.ICP(pages_per_site=1))
    assert web.fetched(f"{SITE}/") >= 1 and crawl.pages[0].url.rstrip("/") == SITE  # fetched anew, by the rules


# --- 2. robots.txt on every hop, the request rate ---------------------------------------------- #

def test_redirect_into_a_path_closed_by_robots_is_not_followed(web, fetcher):
    web.site(SITE, {"/": HOME, "/private/contacts/": page(DIRECTOR.format(domain=DOMAIN))},
             robots="User-agent: *\nDisallow: /private/\n")
    web.pages[f"{SITE}/contacts/"] = redirect(f"{SITE}/private/contacts/")
    result = L.process_company(company(), run_ctx(fetcher))
    assert web.fetched("/private/") == 0 and result.leads == []
    assert "закрыты в robots.txt" in result.no_lead["причина"] and "/contacts/" in result.no_lead["причина"]


def test_robots_of_the_host_a_redirect_leads_to_is_read_first_and_obeyed(web, fetcher):
    www = "https://www.primer-zavod.ru"
    web.pages[f"{SITE}/"] = redirect(f"{www}/")
    web.site(www, {"/": page(DIRECTOR.format(domain=DOMAIN))}, robots="User-agent: *\nDisallow: /\n")
    result = L.process_company(company(), run_ctx(fetcher))
    assert f"{www}/" not in web.calls and f"{www}/robots.txt" in web.calls
    assert result.leads == [] and result.status == "no_lead"
    assert result.no_lead["причина"].startswith("robots.txt закрывает сайт для обхода")
    # an open robots.txt on the same host: the page is fetched, but only after robots.txt
    web.calls.clear()
    web.pages[f"{www}/robots.txt"] = "User-agent: *\nDisallow: /admin/\n"
    f2 = L.LeadFetcher(delay=0, retries=0, backoff=0)
    assert L.crawl_site(f2, company(), L.ICP(pages_per_site=1)).pages
    f2.close()
    assert web.calls.index(f"{www}/robots.txt") < web.calls.index(f"{www}/")


@pytest.mark.parametrize("answer, crawled", [
    (httpx.Response(503), False), (httpx.Response(500), False), (httpx.Response(429), False),
    (httpx.ReadTimeout("timed out"), False),  # RFC 9309: unreachable = everything is closed
    (httpx.Response(404), True), (httpx.Response(403), True),  # unavailable = no restrictions
    (httpx.Response(200, html="<html><body>Страница не найдена</body></html>"), True),  # a soft 404
])
def test_unreachable_robots_txt_closes_the_host_and_a_missing_one_does_not(web, fetcher, answer, crawled):
    web.site(SITE, {"/": HOME})
    web.pages[f"{SITE}/robots.txt"] = answer
    web.pages[f"https://www.{DOMAIN}/robots.txt"] = web.pages[f"http://{DOMAIN}/robots.txt"] = answer
    web.pages[f"http://www.{DOMAIN}/robots.txt"] = answer
    result = L.process_company(company(), run_ctx(fetcher))
    assert (web.fetched(f"{SITE}/") - web.fetched("robots.txt") > 0) is crawled
    if not crawled:
        assert result.status == "error"  # not a verdict about the company: the site is asked again later


def test_evidence_says_what_robots_txt_really_answered(web, fetcher):
    web.site(SITE, {"/": HOME, "/contacts/": page(DIRECTOR.format(domain=DOMAIN))})
    lead = L.process_company(company(), run_ctx(fetcher)).leads[0]
    assert "robots.txt на сайте нет (HTTP 404): ограничений нет" in lead["проверки"]
    other = "https://obrazec-stanki.ru"
    web.site(other, {"/": HOME, "/contacts/": page(DIRECTOR.format(domain="obrazec-stanki.ru"))},
             robots="User-agent: *\nDisallow: /admin/\n")
    lead = L.process_company(company("Образец Станки", other), run_ctx(fetcher)).leads[0]
    assert "robots.txt разрешает страницу" in lead["проверки"]


def test_robots_txt_is_obeyed_for_cached_pages_too(web, tmp_path):
    web.site(SITE, {"/": HOME, "/contacts/": page(DIRECTOR.format(domain=DOMAIN))})
    first = L.LeadFetcher(cache_dir=tmp_path / "c", delay=0, retries=0, backoff=0)
    assert L.process_company(company(), run_ctx(first)).leads
    first.close()
    web.pages[f"{SITE}/robots.txt"] = "User-agent: *\nDisallow: /contacts/\n"  # the site closed the page later
    first._cache_path(f"{SITE}/robots.txt").unlink()  # (its cached robots.txt has expired: it lives for a day)
    second = L.LeadFetcher(cache_dir=tmp_path / "c", delay=0, retries=0, backoff=0)
    web.calls.clear()
    result = L.process_company(company(), run_ctx(second))
    second.close()
    assert result.leads == [] and web.fetched("/contacts/") == 0  # neither requested nor taken from the cache


# --- 2a. robots.txt: the address that is checked is the address that is requested -------------- #

CLOSED = "User-agent: *\nDisallow: /private/\n"


@pytest.mark.parametrize("dotted", [
    "/a/../private/x", "/a/%2E%2E/private/x", "/a/%2e%2e/private/x", "/a/.%2E/private/x", "/./private/x",
    "/open/../private/./x", "/private/y/../x",
])
def test_dot_segments_do_not_lead_around_a_rule(web, fetcher, dotted):
    web.site(SITE, {"/private/x": page(DIRECTOR.format(domain=DOMAIN))}, robots=CLOSED)
    res = fetcher.get(SITE + dotted)
    assert not res.ok and res.html == "" and res.error == L.ROBOTS_DENIED
    assert res.url == f"{SITE}/private/x"  # the address that was checked is named
    assert web.calls == [f"{SITE}/robots.txt"]  # ... and it was not requested


def test_address_with_dot_segments_is_requested_in_the_spelling_that_was_checked(web):
    web.site(SITE, {"/contacts/": TABLE}, robots=CLOSED)
    f = L.LeadFetcher(delay=0, retries=0, backoff=0)  # no cache: every address reaches the mock
    try:
        for dotted in ("/private/../contacts/", "/a/%2E%2E/contacts/", "/contacts/."):
            res = f.get(SITE + dotted)
            assert res.ok and res.url == res.final_url == f"{SITE}/contacts/"
    finally:
        f.close()
    assert web.calls == [f"{SITE}/robots.txt"] + [f"{SITE}/contacts/"] * 3


@pytest.mark.parametrize("location", [
    f"{SITE}/a/../private/contacts/", f"{SITE}/a/%2E%2E/private/contacts/", "/a/.%2e/private/contacts/",
    "../private/contacts/",
])
def test_redirect_through_dot_segments_into_a_closed_path_is_not_followed(web, fetcher, location):
    web.site(SITE, {"/": HOME, "/private/contacts/": page(DIRECTOR.format(domain=DOMAIN))}, robots=CLOSED)
    web.pages[f"{SITE}/contacts/"] = redirect(location, 302)
    res = fetcher.get(f"{SITE}/contacts/")
    assert res.error == f"{L.ROBOTS_DENIED} (адрес после редиректа)" and res.final_url == f"{SITE}/private/contacts/"
    result = L.process_company(company(), run_ctx(fetcher))
    assert web.fetched("private") == 0 and result.leads == []
    assert not any(".." in url or "%2e" in url.lower() for url in web.calls)


def test_link_with_dot_segments_on_a_page_does_not_lead_around_a_rule(web, fetcher):
    home = HOME.replace('<a href="/contacts/">', f'<a href="{SITE}/x/../private/contacts/">') \
        .replace('<a href="/company/staff/">', f'<a href="{SITE}/x/%2E%2E/private/staff/">') \
        .replace('<a href="/about/">', f'<a href="{SITE}/private/../about/">')
    web.site(SITE, {"/": home, "/private/contacts/": page(DIRECTOR.format(domain=DOMAIN)), "/private/staff/": CARDS,
                    "/about/": page(DIRECTOR.format(domain=DOMAIN), title="О компании")}, robots=CLOSED)
    result = L.process_company(company(), run_ctx(fetcher))
    assert web.fetched("private") == 0 and not any(".." in url or "%2E" in url for url in web.calls)
    # the open page behind a dotted link is read under the address that was checked
    assert [r["источник"] for r in result.leads] == [f"{SITE}/about/"] and web.fetched(f"{SITE}/about/") == 1
    crawl = L.crawl_site(fetcher, company(), L.ICP())
    assert crawl.closed == [f"{SITE}/private/contacts/", f"{SITE}/private/staff/"]


# --- 2b. robots.txt: its own redirects, and one reader of its answer for every tool ------------- #

def test_redirect_of_robots_txt_to_another_site_is_followed_and_its_rules_apply_here(web, fetcher):
    # RFC 9309, 2.3.1.2: the rules of the file a redirect leads to apply to the host that was asked
    other = "https://files.hosting.example"
    web.site(SITE, {"/": HOME, "/contacts/": page(DIRECTOR.format(domain=DOMAIN)), "/company/staff/": CARDS})
    web.pages[f"{SITE}/robots.txt"] = redirect(f"{other}/primer-zavod/robots.txt")
    web.pages[f"{other}/primer-zavod/robots.txt"] = "User-agent: *\nDisallow: /company/\n"
    result = L.process_company(company(), run_ctx(fetcher))
    assert web.calls[:2] == [f"{SITE}/robots.txt", f"{other}/primer-zavod/robots.txt"]
    assert web.fetched("/company/staff/") == 0 and web.fetched(other) == 1  # the file, and nothing else from there
    assert [r["источник"] for r in result.leads] == [f"{SITE}/contacts/"]
    assert "robots.txt разрешает страницу" in result.leads[0]["проверки"]
    assert fetcher.get(f"{SITE}/company/staff/").error == L.ROBOTS_DENIED


@pytest.mark.parametrize("answer, why", [
    (httpx.Response(302), "HTTP 302, переадресация не привела к файлу"),  # no Location
    (redirect("/robots.txt"), "HTTP 301, переадресация не привела к файлу: больше 5 редиректов подряд"),
    (redirect("ftp://primer-zavod.ru/robots.txt", 302),
     "HTTP 302, переадресация не привела к файлу: редирект на адрес, который нельзя открыть"),
    (redirect("https://other-hosting.example/robots.txt", 307), "HTTP 503"),  # the file elsewhere answers 503
])
def test_redirect_of_robots_txt_that_does_not_end_in_a_file_closes_the_host(web, fetcher, answer, why):
    web.site(SITE, {"/": HOME, "/contacts/": page(DIRECTOR.format(domain=DOMAIN))})
    for base in (SITE, f"https://www.{DOMAIN}", f"http://{DOMAIN}", f"http://www.{DOMAIN}"):
        web.pages[f"{base}/robots.txt"] = answer
    web.pages["https://other-hosting.example/robots.txt"] = httpx.Response(503)
    res = fetcher.get(f"{SITE}/contacts/")
    assert res.error == f"robots.txt не получен ({why}): по RFC 9309 сайт не обходится"
    result = L.process_company(company(), run_ctx(fetcher))
    assert result.status == "error" and result.leads == []  # not a verdict about the company: asked again later
    assert not [url for url in web.calls if DOMAIN in url and not url.endswith("/robots.txt")]


def test_cached_redirect_of_robots_txt_left_by_an_earlier_run_is_not_trusted(web, fetcher):
    # the fetcher used to stop at a redirect of robots.txt to another site and remember «no restrictions»
    stale = {"v": L.CACHE_VERSION, "url": f"{SITE}/robots.txt", "final_url": "https://files.hosting.example/r.txt",
             "status": 301, "html": "", "error": f"{L.OFFSITE}: files.hosting.example", "short_lived": True,
             "fetched_at": int(L.time.time()), "fetched_on": "2026-10-03"}
    fetcher._cache_path(f"{SITE}/robots.txt").write_text(json.dumps(stale), "utf-8")
    web.site(SITE, {"/private/x": HOME, "/open/x": HOME})
    web.pages[f"{SITE}/robots.txt"] = redirect("https://files.hosting.example/r.txt")
    web.pages["https://files.hosting.example/r.txt"] = CLOSED
    assert fetcher.get(f"{SITE}/private/x").error == L.ROBOTS_DENIED and fetcher.get(f"{SITE}/open/x").ok
    assert web.calls == [f"{SITE}/robots.txt", "https://files.hosting.example/r.txt", f"{SITE}/open/x"]


def test_redirect_of_robots_txt_to_a_site_without_the_file_closes_nothing(web, fetcher):
    web.site(SITE, {"/contacts/": TABLE})
    web.pages[f"{SITE}/robots.txt"] = redirect("https://other-hosting.example/robots.txt", 302)  # ... which answers 404
    assert fetcher.get(f"{SITE}/contacts/").ok
    assert fetcher.robots_note(f"{SITE}/contacts/") == "robots.txt на сайте нет (HTTP 404): ограничений нет"


RULES_AS_HTML = [
    "<!-- robots.txt of the site -->\n" + CLOSED,                      # the first character is «<»
    "# the <html> pages of the shop, see <!DOCTYPE html>\n" + CLOSED,  # the words stand in a remark
    "\ufeff  \n<html>\n" + CLOSED + "</html>\n",
]


@pytest.mark.parametrize("answer, closed", [
    *[(httpx.Response(status, text=CLOSED), True) for status in (200, 202, 203, 206)],
    *[(httpx.Response(200, text=body), True) for body in RULES_AS_HTML],
    (httpx.Response(200, text=CLOSED, headers={"content-type": "text/html; charset=utf-8"}), True),
    (httpx.Response(204), False), (httpx.Response(200, text=""), False), (httpx.Response(200, text="Not found"), False),
    (httpx.Response(200, html="<!DOCTYPE html><html><body>Страница не найдена</body></html>"), False),
    (httpx.Response(410), False), (httpx.Response(401), False),
])
def test_answer_to_robots_txt_is_read_by_the_reader_all_tools_share(web, fetcher, answer, closed):
    web.site(SITE, {"/private/x": HOME, "/open/x": HOME})
    web.pages[f"{SITE}/robots.txt"] = answer
    res = fetcher.get(f"{SITE}/private/x")
    assert (res.error == L.ROBOTS_DENIED) is closed and res.ok is not closed
    assert (f"{SITE}/private/x" in web.calls) is not closed
    assert fetcher.get(f"{SITE}/open/x").ok


def test_robots_txt_is_read_as_utf8_whatever_charset_the_server_announces(web, fetcher):
    # a UTF-8 file with a byte-order mark, served by a host whose default charset is windows-1251
    body = "\ufeffUser-agent: *\nDisallow: /контакты\n".encode()
    web.pages[f"{SITE}/robots.txt"] = httpx.Response(
        200, content=body, headers={"content-type": "text/plain; charset=windows-1251"})
    web.site(SITE, {"/about/": HOME})
    assert fetcher.get(f"{SITE}/about/").ok
    for path in ("/контакты/", "/%D0%BA%D0%BE%D0%BD%D1%82%D0%B0%D0%BA%D1%82%D1%8B/"):
        assert fetcher.get(f"{SITE}{path}").error == L.ROBOTS_DENIED
    assert web.calls == [f"{SITE}/robots.txt", f"{SITE}/about/"]


@pytest.mark.parametrize("name", ["bot", "lead", "finder", "leadfinder", "LeadFinderBotX", "Mozilla", "compatible",
                                  "OutreachResearchBot", "Googlebot"])
def test_group_of_another_name_does_not_open_what_the_star_group_closes(web, fetcher, name):
    web.site(SITE, {"/private/x": HOME}, robots=f"User-agent: {name}\nAllow: /\n\n{CLOSED}")
    assert fetcher.get(f"{SITE}/private/x").error == L.ROBOTS_DENIED
    assert web.calls == [f"{SITE}/robots.txt"]


@pytest.mark.parametrize("name", ["LeadFinderBot", "leadfinderbot", "LEADFINDERBOT", "LeadFinderBot/1.0"])
def test_group_with_exactly_our_product_token_is_ours(web, fetcher, name):
    web.site(SITE, {"/private/x": HOME, "/own/x": HOME}, robots=f"User-agent: {name}\nDisallow: /own/\n\n{CLOSED}")
    assert fetcher.get(f"{SITE}/own/x").error == L.ROBOTS_DENIED and f"{SITE}/own/x" not in web.calls
    assert fetcher.get(f"{SITE}/private/x").ok  # the robot's own group replaces «*» (RFC 9309, 2.2.1)


def test_there_is_no_way_to_switch_robots_txt_off(web, capsys):
    with pytest.raises(TypeError):
        L.LeadFetcher(respect_robots=False)
    web.site(SITE, {"/private/x": HOME}, robots=CLOSED)
    f = L.LeadFetcher(delay=0, retries=0, backoff=0)
    try:
        assert not hasattr(f, "respect_robots")
        with pytest.raises(TypeError):
            f.get(f"{SITE}/private/x", check_robots=False)
        assert f.get(f"{SITE}/private/x").error == L.ROBOTS_DENIED
        assert f.get(f"{SITE}/robots.txt").html == CLOSED  # the one address that is requested unasked
    finally:
        f.close()
    assert f"{SITE}/private/x" not in web.calls
    with pytest.raises(SystemExit) as refused:
        L.parse_args(["--no-robots"])
    assert refused.value.code == 2 and "--no-robots" in capsys.readouterr().err  # an unknown argument
    with pytest.raises(SystemExit):
        L.parse_args(["--help"])
    help_text = " ".join(capsys.readouterr().out.split())
    assert "--no-robots" not in help_text and "robots.txt сайтов соблюдается всегда" in help_text


def test_requests_to_one_site_are_never_closer_than_the_delay(respx_mock, tmp_path):
    import time as clock

    times: list[tuple[float, str]] = []
    www = f"https://www.{DOMAIN}"
    home = HOME.replace('href="/contacts/"', 'href="/contacts"').replace('href="/about/"', f'href="{www}/about/"') \
        .replace('<a href="/company/staff/">Сотрудники</a>', "")
    pages = {f"{SITE}/": home, f"{SITE}/contacts": redirect(f"{SITE}/contacts/"), f"{SITE}/contacts/": TABLE,
             f"{www}/about/": page("<p>О заводе: история и продукция.</p>")}

    def serve(request):
        times.append((clock.monotonic(), str(request.url)))
        body = pages.get(str(request.url))
        if isinstance(body, httpx.Response):
            return body
        return httpx.Response(200, html=body) if body else httpx.Response(404, html="<html>нет</html>")

    respx_mock.route().mock(side_effect=serve)
    delay = 0.15
    f = L.LeadFetcher(cache_dir=tmp_path / "c", delay=delay, retries=0, backoff=0)
    crawl = L.crawl_site(f, company(), L.ICP(pages_per_site=4))
    f.close()
    assert {p.kind for p in crawl.pages} == {"home", "contacts", "about"}
    assert len(times) >= 7  # robots.txt of both hosts, home, sitemap, the redirect hop, contacts, the www page
    moments = [t for t, _ in times]
    gaps = [b - a for a, b in zip(moments, moments[1:], strict=False)]
    # one site = site.ru and www.site.ru together; a redirect hop is a request like any other
    assert min(gaps) >= delay * 0.98, [round(g, 3) for g in gaps]
    assert L.LeadFetcher().delay == 1.0 and L.parse_args([]).delay == 1.0  # the default pace


def test_run_never_goes_faster_than_one_request_per_second(tmp_path, two_sites, monkeypatch):
    seen = {}
    monkeypatch.setattr(L, "LeadFetcher", lambda **kw: seen.update(kw) or (_ for _ in ()).throw(SystemExit("stop")))
    with pytest.raises(SystemExit, match="stop"):
        L.run(["--seeds", str(write_seeds(tmp_path / "s.csv", [["Завод Пример", SITE, "", ""]])),
               "--out", str(tmp_path / "o"), "--cache-dir", str(tmp_path / "c"), "--delay", "0.01"], env={})
    assert seen["delay"] == 1.0  # --delay can slow the crawl down, never speed it up


# --- 3. department boxes and unmatched addresses are not leads --------------------------------- #

@pytest.mark.parametrize("box", [
    "sales-msk", "info.spb", "zakaz-ekb", "otdel.prodazh", "op", "sales.department", "office-manager", "b2b-sales",
    "opt-zakaz", "client.service", "tender-otdel", "prodazhi", "primer.zavod.sales", "sbt4", "market15", "1_sales",
    "fin", "marketing", "pochta", "zapros", "kommerc", "sbyt-opt", "torg.otdel", "sale_opt",
])
def test_compound_department_box_in_a_managers_card_is_not_a_lead(box):
    address = f"{box}@{DOMAIN}"
    html = page(f"""<div class="item"><div>Черновиков Глеб Ильич</div><div>Руководитель отдела продаж</div>
      <div>Тел.: +7 (495) 000-00-01</div><div>E-mail: {address}</div></div>""")
    verdict = verdicts(html)[address]
    assert verdict.lead is None and verdict.reason in (L.R_GENERIC, L.R_UNMATCHED)


def test_address_that_is_neither_the_name_nor_a_post_is_not_a_lead(web, fetcher):
    # a group box printed in a manager's card and once more under the group's heading, a quality-control
    # box in a deputy's card, a second box of the same person for another region
    contacts = page("""<h1>Контакты</h1>
      <div class="dept"><b>Группа продаж насосного оборудования</b><div>8 (35251) 0-00-29</div>
        <div>okb30@primer-zavod.ru mto@primer-zavod.ru; nasosy@primer-zavod.ru</div></div>
      <div class="item"><div>Начальник отдела проектных продаж</div><div>Черновиков Глеб Ильич</div>
        <div>тел.: +7 (35251) 0-00-29</div><div>okb30@primer-zavod.ru</div></div>
      <div class="item"><div>Заместитель генерального директора - начальник службы качества</div>
        <div>Шаблонов Олег Петрович</div><div>otk5@primer-zavod.ru</div></div>
      <div class="item"><div>Руководитель региональных продаж</div><div>Примерова Анна Сергеевна</div>
        <div>soyuz@primer-zavod.ru</div><div>volga@primer-zavod.ru</div></div>
      <div class="item"><div>Генеральный директор</div><div>Образцов Пётр Ильич</div>
        <div>stankoprom2010@primer-zavod.ru</div></div>""")
    web.site(SITE, {"/": HOME, "/contacts/": contacts})
    result = L.process_company(company(), run_ctx(fetcher))
    assert result.leads == [] and result.status == "no_lead"
    assert result.rejects[L.R_UNMATCHED] >= 4 and "адрес не совпал с ФИО" in result.no_lead["причина"]
    found = verdicts(contacts)
    assert found["okb30@primer-zavod.ru"].address_type == L.TYPE_UNKNOWN
    assert found["volga@primer-zavod.ru"].reason in (L.R_GENERIC, L.R_UNMATCHED)


def test_numbered_box_needs_its_number_next_to_the_person():
    card = """<div class="item"><div>{name}</div><div>Коммерческий директор</div><div>{phone}</div>
      <div>{box}@primer-zavod.ru</div></div>"""
    by_extension = page(card.format(name="Образцов Пётр Ильич", phone="+7 (343) 000-00-17, доб. 105", box="105"))
    lead = verdicts(by_extension)["105@primer-zavod.ru"].lead
    assert lead.address_type == L.TYPE_ROLE and "номер совпадает с телефоном или добавочным" in "; ".join(lead.checks)
    by_phone = page(card.format(name="Образцов Пётр Ильич", phone="+7 (343) 000-00-17", box="3430000017"))
    assert verdicts(by_phone)["3430000017@primer-zavod.ru"].reason == ""
    alone = page(card.format(name="Образцов Пётр Ильич", phone="+7 (343) 000-00-17", box="7"))
    assert verdicts(alone)["7@primer-zavod.ru"].reason == L.R_UNMATCHED
    # a site where every employee has a numbered box: the number is the site's way to name a mailbox
    everyone = page("".join(card.format(name=name, phone="8 (800) 000-00-00", box=box) for name, box in (
        ("Образцов Пётр Ильич", "201"), ("Тестов Олег Петрович", "202"), ("Шаблонова Ирина Олеговна", "203"))))
    lead = verdicts(everyone)["201@primer-zavod.ru"].lead
    assert lead is not None and "на сайте такие адреса у всех сотрудников" in "; ".join(lead.checks)


def test_personal_address_that_also_stands_in_the_footer_is_still_personal():
    html = page("""<div class="item"><div>Образцов Пётр</div><div>Директор по маркетингу</div>
      <div>po@primer-zavod.ru</div></div>""", footer="© 2026 Завод Пример. Пишите: po@primer-zavod.ru")
    lead = verdicts(html)["po@primer-zavod.ru"].lead
    assert lead.address_type == L.TYPE_PERSONAL and "он же стоит в подвале сайта" in "; ".join(lead.checks)
    # the same box in the footer with nobody of that name next to it stays a company box
    nobody = page("<p>Отдел маркетинга</p>", footer="© 2026 Завод Пример. Пишите: po@primer-zavod.ru")
    assert verdicts(nobody)["po@primer-zavod.ru"].reason == L.R_GENERIC


def test_surname_prefix_with_both_initials_confirms_an_address():
    person = L.Person(surname="Образцов", first="Пётр", patronymic="Ильич")
    assert L.local_matches_person("obpi", person) == "инициалы"  # ОБразцов П. И.
    assert L.local_matches_person("piobr", person) == "инициалы"
    assert L.local_matches_person("soyuz", person) == "" and L.local_matches_person("obri", person) == ""


# --- 4. an address goes to the person it is printed with, or to nobody -------------------------- #

def test_name_broken_over_two_lines_is_read_whole():
    html = page("""<div class="staff"><div class="li"><b>Образцов Пётр Ильич</b><small>Генеральный директор</small>
      <div class="info">Мобильный: +7 908 000 00 00</div></div>
      <div class="li"><b>Шаблонов <br>Александр</b><small>Коммерческий директор</small>
      <div class="info">E-mail: shablonov@primer-zavod.ru</div></div></div>""")
    cand = candidates(html)["shablonov@primer-zavod.ru"]
    assert (cand.person.display, cand.title) == ("Шаблонов Александр", "Коммерческий директор")
    # a label before a line break is still a label, not half of a name
    doc = L.flatten(SITE, "<p>Телефон<br>+7 495 000-00-00<br>Контакты<br>Образцов Пётр</p>")
    assert [a.text for a in doc.atoms] == ["Телефон", "+7 495 000-00-00", "Контакты", "Образцов Пётр"]


@pytest.mark.parametrize("markup", [
    "<b>Примеров Зульфат</b><small>Директор филиала</small>",  # the title in the same run
    "<div>Примеров Зульфат</div><div>Директор филиала</div>",  # the title in the next element
])
def test_unknown_first_name_is_accepted_right_before_a_job_title(markup):
    assert "зульфат" not in L.RU_FIRST_NAMES
    html = page(f"""<div class="li">{markup}<div>+79990000000</div><div>primerov@primer-zavod.ru</div></div>""")
    cand = candidates(html)["primerov@primer-zavod.ru"]
    assert (cand.person.surname, cand.person.first, cand.title) == ("Примеров", "Зульфат", "Директор филиала")
    # without a title next to them two capitalised words are not a name
    assert L.find_names("Примеров Зульфат приглашает на выставку") == []
    assert L.find_names("Образцов Завод, директор") == []


@pytest.mark.parametrize("second_card", [
    # the tool cannot read the name of the second card: it is in Latin, in a picture, a single word ...
    '<b>Zulfat P.</b><small>менеджер по логистике</small><div class="info">E-mail: zp@primer-zavod.ru</div>',
    '<img alt="" src="/i/7.png"><small>Инженер по проектам</small><div class="info">E-mail: zp@primer-zavod.ru</div>',
    '<b>Зульфат</b><small>Руководитель проектов</small><div class="info">E-mail: gd2@primer-zavod.ru</div>',
])
def test_address_of_an_unread_card_is_not_given_to_the_previous_person(second_card):
    html = page(f"""<div class="staff"><div class="li"><b>Образцов Пётр Ильич</b><small>Генеральный директор</small>
      <div class="info">Мобильный: +7 908 000 00 00</div></div><div class="li">{second_card}</div></div>""")
    for email, verdict in verdicts(html).items():
        if email != f"info@{DOMAIN}":
            assert verdict.lead is None and verdict.reason == L.R_NO_NAME, (email, verdict.reason)
    cand = next(c for e, c in candidates(html).items() if e != f"info@{DOMAIN}")
    assert cand.person is None and cand.name_options == []  # nothing for the LLM to choose from either


def test_address_block_under_the_persons_name_block_is_still_his():
    # one card split into two blocks is not two cards: the second block has no person of its own
    html = page("""<div class="card"><div class="who"><h3>Образцов Пётр Ильич</h3><p>Генеральный директор</p></div>
      <div class="how"><p>Приёмная: +7 (495) 000-00-09</p><p>E-mail: gd@primer-zavod.ru</p></div></div>""")
    assert verdicts(html)["gd@primer-zavod.ru"].reason == ""
    # a key-value table: the rows are siblings of one kind, the row of the address names nobody
    table = page("""<table><tr><td>Генеральный директор</td><td>Образцов Пётр Ильич</td></tr>
      <tr><td>E-mail</td><td>gd@primer-zavod.ru</td></tr></table>""")
    assert verdicts(table)["gd@primer-zavod.ru"].reason == ""


def test_title_far_from_the_name_is_not_this_persons_title():
    filler = "Завод выпускает станки и поставляет их по всей стране. " * 40
    html = page(f"<div><p>Коммерческий директор рассказал о планах завода на выставке.</p><p>{filler}</p>"
                "<p>Образцов Пётр Ильич, obraztsov@primer-zavod.ru</p></div>")
    verdict = verdicts(html)["obraztsov@primer-zavod.ru"]
    assert verdict.lead is None and verdict.reason == L.R_NO_TITLE and "от имени при пороге 350" in verdict.detail


@pytest.mark.parametrize("title, role, deputy", [
    ("Региональный директор", "branch", False), ("Директор филиала в г. Казань", "branch", False),
    ("Руководитель московского филиала", "branch", False), ("Руководитель региональных продаж", "sales", False),
    ("Заместитель генерального директора - начальник службы качества", "dept_head", True),
    ("Заместитель генерального директора, руководитель отдела продаж", "sales", True),
    ("Заместитель генерального директора по производству", "other_director", True),
    ("Заместитель генерального директора", "ceo", True),
])
def test_branch_heads_and_deputies_with_a_second_title(title, role, deputy):
    assert L.classify_role(title) == (role, deputy)
    assert "branch" in L.DEFAULT_ROLES and "branch" in L.ROLE_LABELS


def test_own_employee_on_a_partners_page_is_not_foreign():
    card = """<div class="item"><div>Образцов Пётр Ильич</div><div>{title}</div>
      <div>obraztsov@primer-zavod.ru</div></div>"""
    url = f"{SITE}/partners/experts/76"
    own = verdicts(page(card.format(title="Директор по маркетингу Завода Пример")), url)
    assert own["obraztsov@primer-zavod.ru"].reason == ""
    partner = verdicts(page(card.format(title="Директор по маркетингу")), url)
    assert partner["obraztsov@primer-zavod.ru"].reason == L.R_FOREIGN


def test_printed_address_wins_over_a_link_target_for_the_same_person(web, fetcher):
    contacts = page("""<div class="item"><div>Коммерческий директор</div><div>Образцов Пётр Ильич</div>
      <div>+7 (863) 000-00-10 доб. 730</div><div><a href="mailto:obraztsov@primer-zavod.ru">kd@primer-zavod.ru</a>
      </div></div>""")
    web.site(SITE, {"/": HOME, "/contacts/": contacts})
    for mode in ("none", "matched", "card"):
        result = L.process_company(company(), run_ctx(fetcher, mailto=mode))
        assert [r["Email"] for r in result.leads] == ["kd@primer-zavod.ru"], mode


def test_every_lead_address_is_printed_on_its_source_page(web, fetcher):
    # behaviour, not a source scan: whatever the pages say, an emitted address is a substring of its page
    web.site(SITE, {"/": HOME, "/contacts/": TABLE, "/company/staff/": CARDS, "/about/": DEFLIST})
    pages_by_url = {url: body for url, body in web.pages.items() if isinstance(body, str)}
    result = L.process_company(company(), run_ctx(fetcher))
    assert len(result.leads) == 3
    for lead in result.leads:
        source = pages_by_url[lead["источник"]]
        assert lead["Email"] in source.replace(" [at] ", "@") and lead["Фамилия"] in source
    director = [r for r in result.leads if r["роль"] == "ceo"][0]
    assert director["Email"] in ("gd@primer-zavod.ru", "testov@primer-zavod.ru")
    # ONLY_GENERIC names a director with no address: no address appears for him out of thin air
    web.site(SECOND, {"/": HOME, "/contacts/": ONLY_GENERIC.replace("primer-zavod", "obrazec-stanki")})
    none = L.process_company(company("Образец Станки", SECOND), run_ctx(fetcher))
    assert none.leads == [] and "testov" not in json.dumps(L.asdict(none), ensure_ascii=False).lower()


# --- 5. the evidence is as old as the page ------------------------------------------------------ #

def test_evidence_date_is_the_day_the_page_was_fetched(tmp_path, two_sites, monkeypatch):
    out = cli(tmp_path)
    assert {r["дата_проверки"] for r in read_csv(out / "leads.csv")} == {"2026-10-03"}
    # twelve days later another run, into another directory, reads the same pages from the cache
    monkeypatch.setattr(P, "today", lambda: P.date(2026, 10, 15))
    calls = len(two_sites.calls)
    f = L.LeadFetcher(cache_dir=tmp_path / "cache", delay=0, retries=0, backoff=0)
    L.run(["--seeds", str(tmp_path / "seeds.csv"), "--out", str(tmp_path / "later"), "--cache-dir",
           str(tmp_path / "cache"), "--workers", "1", "--retry-wait", "0"], fetcher=f, mx=FakeMX(), env={})
    f.close()
    rows = read_csv(tmp_path / "later" / "leads.csv")
    assert len(rows) == 3 and {r["дата_проверки"] for r in rows} == {"2026-10-03"}  # the pages are of that day
    assert not any("/contacts/" in url or "/company/staff/" in url for url in two_sites.calls[calls:])
    summary = json.loads((tmp_path / "later" / "summary.json").read_text("utf-8"))
    assert summary["дата"] == "2026-10-15"  # the run itself is dated honestly too


def test_refresh_fetches_the_pages_again(tmp_path, two_sites, monkeypatch):
    out = cli(tmp_path)
    assert len(read_csv(out / "leads.csv")) == 3
    # the commercial director has left: his card is gone from the page
    two_sites.pages[f"{SITE}/company/staff/"] = page("<h1>Сотрудники</h1><p>Отдел продаж: sales@primer-zavod.ru</p>")
    two_sites.pages[f"{SITE}/contacts/"] = page("<h1>Контакты</h1><p>Отдел продаж: sales@primer-zavod.ru</p>")
    monkeypatch.setattr(P, "today", lambda: P.date(2027, 1, 15))
    calls = len(two_sites.calls)
    cli(tmp_path, "--refresh")
    assert any(url == f"{SITE}/company/staff/" for url in two_sites.calls[calls:])  # the network, not the cache
    assert read_csv(out / "leads.csv") == []  # nobody is emitted from a page that no longer names him


def test_cached_page_expires(web, tmp_path):
    web.site(SITE, {"/": HOME})
    f = L.LeadFetcher(cache_dir=tmp_path / "c", delay=0, retries=0, backoff=0, cache_days=30)
    assert not f.get(f"{SITE}/").from_cache and f.get(f"{SITE}/").from_cache
    path = f._cache_path(f"{SITE}/")
    entry = json.loads(path.read_text("utf-8"))
    assert entry["v"] == L.CACHE_VERSION and entry["fetched_on"] == "2026-10-03"
    entry["fetched_at"] -= 31 * 86400
    entry["fetched_on"] = "2026-09-02"
    path.write_text(json.dumps(entry), "utf-8")
    g = L.LeadFetcher(cache_dir=tmp_path / "c", delay=0, retries=0, backoff=0, cache_days=30)
    assert not g.get(f"{SITE}/").from_cache  # older than 30 days: the page is asked again
    entry["fetched_at"] += 20 * 86400
    path.write_text(json.dumps(entry), "utf-8")
    h = L.LeadFetcher(cache_dir=tmp_path / "c", delay=0, retries=0, backoff=0, cache_days=30)
    res = h.get(f"{SITE}/")
    assert res.from_cache and res.fetched_on == "2026-09-02"  # the date of the fetch travels with the page
    for item in (f, g, h):
        item.close()


def test_site_whose_news_feed_stopped_long_ago_is_stale():
    feed = "".join(f"<div class='news'><div class='date'>{d}</div><a href='/news/{i}/'>Новость {i}</a></div>"
                   for i, d in enumerate(("29.05.2023", "13.02.2020", "11.02.2020", "05.05.2016")))
    home = f"{SITE}/"
    dead = verdicts(CARDS.replace("© 2011–2026 ", ""), extra=[(home, page(feed, footer="ООО «Завод Пример»"))])
    assert dead["obraztsov@primer-zavod.ru"].reason == ""  # the feed counts on the homepage only ...
    pages = [L.SitePage(home, "home", L.annotate(L.flatten(home, page(feed, footer="ООО «Завод Пример»")))),
             L.SitePage(f"{SITE}/company/staff/", "team",
                        L.annotate(L.flatten(f"{SITE}/company/staff/", CARDS.replace("© 2011–2026 ", ""))))]
    site = L.build_site_ctx(company(), pages, L.ICP())
    assert site.feed_last == P.date(2023, 5, 29)
    verdict = {c.email: L.judge(c, company(), site, L.Settings(icp=L.ICP(), mx=FakeMX()))
               for c in L.extract_candidates(pages[1].doc)}["obraztsov@primer-zavod.ru"]
    assert verdict.reason == L.R_STALE and "29.05.2023" in verdict.detail
    # ... and a current footer year means the site is alive, whatever the feed says
    pages[1] = L.SitePage(f"{SITE}/company/staff/", "team", L.annotate(L.flatten(f"{SITE}/company/staff/", CARDS)))
    alive = L.build_site_ctx(company(), pages, L.ICP())
    assert alive.stale_reason(f"{SITE}/company/staff/") == ""


# --- 6. opt-out works everywhere ------------------------------------------------------------------ #

TWO_IN_A_LINE = page("""<h1>Контакты</h1><p>Образцов Пётр Ильич — коммерческий директор,
  Шаблонова Ирина Олеговна — директор по маркетингу<br>obraztsov@primer-zavod.ru, shablonova@primer-zavod.ru</p>""")


def run_into(tmp_path, name: str, cache: str = "cache"):
    f = L.LeadFetcher(cache_dir=tmp_path / cache, delay=0, retries=0, backoff=0)
    L.run(["--seeds", str(tmp_path / "seeds.csv"), "--out", str(tmp_path / "out" / name), "--cache-dir",
           str(tmp_path / cache), "--workers", "1", "--retry-wait", "0"], fetcher=f, mx=FakeMX(), env={})
    f.close()
    return tmp_path / "out" / name


def test_forget_covers_every_result_directory_and_every_later_run(tmp_path, web, capsys):
    web.site(SITE, {"/": HOME, "/contacts/": TWO_IN_A_LINE})
    write_seeds(tmp_path / "seeds.csv", [["Завод Пример", SITE, "", ""]])
    address = "obraztsov@primer-zavod.ru"
    run_a, run_b = run_into(tmp_path, "run_a"), run_into(tmp_path, "nested/run_b")
    assert all(address in (d / "leads.csv").read_text("utf-8-sig") for d in (run_a, run_b))
    # the command as the documentation gives it: the root of the results, not one run
    assert L.run(["--forget", address, "--out", str(tmp_path / "out"), "--cache-dir", str(tmp_path / "cache")]) == 0
    assert "лидов_удалено: 2" in capsys.readouterr().out
    for directory in (run_a, run_b):
        assert [r["Email"] for r in read_csv(directory / "leads.csv")] == ["shablonova@primer-zavod.ru"]
        for path in directory.iterdir():
            assert not printed(address, path.read_text("utf-8-sig")), path
        # the neighbour's evidence quoted both addresses: the forgotten one is blanked there
        assert L.SCRUBBED in read_csv(directory / "leads.csv")[0]["фрагмент"]
    # a later run into a NEW directory, even with another (empty) page cache next to the same list
    run_c = run_into(tmp_path, "run_c")
    assert [r["Email"] for r in read_csv(run_c / "leads.csv")] == ["shablonova@primer-zavod.ru"]
    assert not any(printed(address, p.read_text("utf-8")) for p in (tmp_path / "cache").glob("*.json"))
    # the list survives the loss of the cache: a copy lies in the result root
    run_d = run_into(tmp_path, "run_d", cache="cache2")
    assert address not in (run_d / "leads.csv").read_text("utf-8-sig")
    hashes = json.loads((tmp_path / "out" / "suppression.json").read_text("utf-8"))["sha256"]
    assert L._hash(address) in hashes and address not in json.dumps(hashes)


def test_forget_of_an_unknown_contact_says_so(tmp_path, two_sites, capsys):
    cli(tmp_path)
    assert L.run(["--forget", "nikto@primer-zavod.ru", "--out", str(tmp_path / "out"),
                  "--cache-dir", str(tmp_path / "cache")]) == 0
    text = capsys.readouterr().out
    assert "ВНИМАНИЕ" in text and "не найден" in text and "Хэш записан" in text
    assert L._hash("nikto@primer-zavod.ru") in L.load_suppressed(tmp_path / "out", tmp_path / "cache")


# --- 7. failures of the network are not answers about the company -------------------------------- #

def test_dns_failure_is_not_remembered_as_no_mx(tmp_path, monkeypatch):
    import subprocess

    resolver = L.MXResolver(cache_path=tmp_path / "mx.json")
    resolver._dig = "/usr/bin/dig"
    answers = iter([subprocess.CompletedProcess([], 9, stdout=";; connection timed out; no servers could be reached\n",
                                                stderr="")] * 2
                   + [subprocess.CompletedProcess([], 0, stdout="10 mx.primer-zavod.ru.\n", stderr="")])
    monkeypatch.setattr(L.subprocess, "run", lambda *a, **kw: next(answers))
    assert resolver.lookup(DOMAIN) is None  # "could not check", and the lead only gets «MX не проверялся»
    assert not (tmp_path / "mx.json").exists()
    assert resolver.lookup(DOMAIN) == ["mx.primer-zavod.ru"]  # asked again, answered, remembered
    monkeypatch.setattr(L.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess([], 0, stdout="", stderr=""))
    assert resolver.lookup("net-pochty-primer.ru") == []  # the server answered: there is no MX


def test_pages_lost_to_the_connection_make_the_site_an_error_not_a_no_lead(web, fetcher):
    web.site(SITE, {"/": HOME})
    web.pages[f"{SITE}/contacts/"] = httpx.ReadTimeout("timed out")
    web.pages[f"{SITE}/company/staff/"] = httpx.ConnectTimeout("handshake timed out")
    result = L.process_company(company(), run_ctx(fetcher))
    assert result.status == "error" and result.no_lead["причина"].startswith("сайт недоступен: 2 стр. не открылись")


def test_sites_that_did_not_answer_are_asked_again_within_the_run(tmp_path, web, caplog):
    def stall():  # the tunnel stalls for the first request to every address of the site, then recovers
        stalled = [httpx.ConnectTimeout("handshake timed out"), httpx.Response(404)]
        for base in (SITE, f"https://www.{DOMAIN}", f"http://{DOMAIN}", f"http://www.{DOMAIN}"):
            web.pages[f"{base}/robots.txt"] = list(stalled)

    web.site(SITE, {"/": HOME, "/contacts/": TABLE})
    stall()
    seeds = write_seeds(tmp_path / "seeds.csv", [["Завод Пример", SITE, "", ""]])
    with caplog.at_level("INFO", logger="leadfinder"):
        out = cli(tmp_path, seeds=seeds)
    summary = json.loads((out / "summary.json").read_text("utf-8"))
    assert (summary["сайт_недоступен"], summary["с_лидом"]) == (0, 1)
    assert "повтор 1: 1 сайтов не ответили" in caplog.text
    # with the retry switched off the same hiccup leaves the site «недоступен» until the next run
    stall()
    (tmp_path / "second").mkdir()
    out2 = cli(tmp_path / "second", "--retry-errors", "0", seeds=seeds)
    assert json.loads((out2 / "summary.json").read_text("utf-8"))["сайт_недоступен"] == 1
    assert cli(tmp_path / "second", "--retry-errors", "0", seeds=seeds) == out2  # ... and the next run picks it up
    assert json.loads((out2 / "summary.json").read_text("utf-8"))["с_лидом"] == 1


# --- 8. the crawl finds the people pages ----------------------------------------------------------- #

def test_splash_homepage_is_read_one_level_deeper(web, fetcher):
    splash = """<html><head><title>ГК Пример</title></head><body><div class="logo">ГК Пример</div>
      <a href="/mdzh/">Мясо</a> <a href="/selhoz/">Сельхозтехника</a> <a href="/oplata">Оплата</a></body></html>"""
    section = page("<h1>Мясной дом</h1><p>Производим и продаём.</p>") \
        .replace('href="/contacts/"', 'href="/mdzh/kontakty/"')
    web.site(SITE, {"/": splash, "/mdzh/": section, "/mdzh/kontakty/": page(DIRECTOR.format(domain=DOMAIN))})
    result = L.process_company(company(), run_ctx(fetcher))
    assert [r["источник"] for r in result.leads] == [f"{SITE}/mdzh/kontakty/"]


def test_mirror_on_a_subdomain_comes_after_the_main_host_and_guesses_come_last(web, fetcher):
    eng = f"https://eng.{DOMAIN}"
    home = HOME.replace("<h1>Завод Пример</h1>", f"""<h1>Завод Пример</h1><a href="{eng}/contacts/">Contacts</a>
      <a href="{eng}/about/">About</a><a href="{eng}/team/">Team</a>""") \
        .replace('<a href="/company/staff/">Сотрудники</a>', "")
    web.site(SITE, {"/": home, "/contacts/": TABLE, "/about/": page("<p>О заводе.</p>")})
    web.site(eng, {"/contacts/": page("<p>Contacts of the plant: sales@primer-zavod.ru</p>"),
                   "/team/": page("<p>Our team works hard every day.</p>"), "/about/": page("<p>About us.</p>")})
    crawl = L.crawl_site(fetcher, company(), L.ICP(pages_per_site=7))  # 6 pages + the dead «Сотрудники» link
    order = [p.url for p in crawl.pages]
    assert [u.rstrip("/") for u in order[:3]] == [SITE, f"{SITE}/contacts", f"{SITE}/about"]  # the main host first
    assert len(order) >= 5 and all(u.startswith(eng) for u in order[3:])  # then the mirror
    assert web.fetched("/kontakty/") == 0 and web.fetched(f"{SITE}/team/") == 0  # the budget did not go to guesses


def test_guessed_paths_stop_after_two_misses(web, fetcher):
    bare = "<html><head><title>Завод Пример</title></head><body><main>" + "Станки. " * 60 + "</main></body></html>"
    web.site(SITE, {"/": bare})
    crawl = L.crawl_site(fetcher, company(), L.ICP())
    guesses = [u for u in web.calls if u.rstrip("/").rsplit("/", 1)[-1] in ("contacts", "kontakty", "contact",
                                                                            "staff", "team")]
    assert len(crawl.pages) == 1 and len(guesses) == 4  # two per kind, not all six


def test_run_stops_with_tunnel_down_when_the_proxy_dies_in_the_middle(tmp_path, web):
    # six sites in a row do not answer and the proxy port is closed: the run stops, what is done is kept
    sites = [f"https://site{i}-primer.ru" for i in range(8)]
    for site in sites:
        for base in (site, site.replace("https://", "https://www."), site.replace("https://", "http://"),
                     site.replace("https://", "http://www.")):
            web.pages[f"{base}/robots.txt"] = httpx.ConnectError("proxy: connection refused")
    seeds = write_seeds(tmp_path / "seeds.csv", [[f"Фирма {i}", site, "", ""] for i, site in enumerate(sites)])
    f = L.LeadFetcher(cache_dir=tmp_path / "cache", delay=0, retries=0, backoff=0)
    with pytest.raises(SystemExit, match="TUNNEL DOWN"):
        L.run(["--seeds", str(seeds), "--out", str(tmp_path / "out"), "--cache-dir", str(tmp_path / "cache"),
               "--workers", "1", "--retry-wait", "0"], fetcher=f, mx=FakeMX(), env={"POLZA_SOCKS": "127.0.0.1:1"})
    f.close()
    state = json.loads((tmp_path / "out" / "state.json").read_text("utf-8"))["companies"]
    assert 5 <= len(state) < 8 and {entry["status"] for entry in state.values()} == {"error"}


# --- 9. no personal phone numbers ------------------------------------------------------------------ #

MOBILE_CARD = """<div class="item"><div>Образцов Пётр Ильич</div><div>Коммерческий директор</div>
  <div>Моб.: {mobile}</div><div>Прямой: +7 (495) 000-00-01, доб. 101</div>
  <div>E-mail: obraztsov@primer-zavod.ru</div></div>"""


def bare_page(body: str) -> str:
    """A page with no site header: no site-wide number of the company on it."""
    return (f"<html><head><title>Завод Пример</title></head><body><main>{body}</main>"
            "<footer>© 2011–2026 ООО «Завод Пример»</footer></body></html>")


def site_page(html: str, kind: str = "contacts") -> L.SitePage:
    return L.SitePage(f"{SITE}/{kind}/", kind, L.annotate(L.flatten(f"{SITE}/{kind}/", html)))


@pytest.mark.parametrize("printed_as, masked", [
    ("+79990000000", "+7********00"), ("+7 (999) 000-00-00", "+7 (***) ***-**-00"),
    ("8 999 000 00 00", "8 *** *** ** 00"), ("8(999)000-00-00", "8(***)***-**-00"),
    ("+7-999-000-00-00", "+7-***-***-**-00"), ("999 000-00-00", "*** ***-**-00"),
    ("+7 999 0000000", "+7 *** *****00"), ("7 999 000-00-00", "7 *** ***-**-00"),
])
def test_mobile_number_is_masked_in_the_evidence_fragment(printed_as, masked):
    assert L.is_mobile(printed_as) and L.mask_mobiles(f"Моб.: {printed_as}.") == f"Моб.: {masked}."
    assert L.mask_mobiles(masked) == masked  # masking twice changes nothing
    lead = verdicts(page(MOBILE_CARD.format(mobile=printed_as)))["obraztsov@primer-zavod.ru"].lead
    assert masked in lead.fragment and "999" not in lead.fragment
    # the rest of the evidence is verbatim: the name, the address, the office line with its extension
    assert "Образцов Пётр Ильич" in lead.fragment and "obraztsov@primer-zavod.ru" in lead.fragment
    assert "+7 (495) 000-00-01, доб. 101" in lead.fragment


@pytest.mark.parametrize("text", [
    "+7 (495) 000-00-01, доб. 101", "8 800 000-00-00", "8 (35251) 0-00-29", "+7 (343) 000-00-17 (105)",
    "ИНН 7700000000, КПП 770001001", "Р/с 40702810900000000001", "ГОСТ 9000-2015", "с 9:00 до 18:00",
])
def test_landlines_and_other_numbers_are_not_masked(text):
    assert L.mask_mobiles(text) == text and not L.is_mobile(text)


def test_phone_column_is_the_companys_number_never_the_one_in_a_persons_card(web, fetcher):
    web.site(SITE, {"/": HOME, "/company/staff/": page(MOBILE_CARD.format(mobile="+7 (999) 000-00-00"))})
    result = L.process_company(company(), run_ctx(fetcher))
    lead = result.leads[0]
    assert lead["Email"] == "obraztsov@primer-zavod.ru"
    assert lead["Телефон"] == "+7 (495) 000-00-00"  # the number in the site header, not the card's direct line
    assert "телефон: общий номер компании" in lead["проверки"]
    assert "999" not in json.dumps(L.asdict(result), ensure_ascii=False)  # the mobile is nowhere in what is kept


def test_site_without_a_general_landline_gives_an_empty_phone(web, fetcher):
    # the only numbers of the site: a mobile on the homepage and the two numbers of the director's card
    home = bare_page('<h1>Завод Пример</h1><p>Производим станки с ЧПУ.</p><p>Звоните: +7 999 000-00-00</p>'
                     '<a href="/company/staff/">Сотрудники</a>')
    web.site(SITE, {"/": home, "/company/staff/": bare_page(MOBILE_CARD.format(mobile="8 (999) 000-00-00"))})
    result = L.process_company(company(), run_ctx(fetcher))
    assert [r["Email"] for r in result.leads] == ["obraztsov@primer-zavod.ru"]
    assert result.leads[0]["Телефон"] == "" and "телефон" not in result.leads[0]["проверки"]
    # a company without a lead: the report carries no mobile number either
    web.site(SECOND, {"/": home.replace("/company/staff/", "/about/")})
    nobody = L.process_company(company("Образец Станки", SECOND), run_ctx(fetcher))
    assert nobody.status == "no_lead" and nobody.no_lead["телефон"] == ""
    assert "999" not in json.dumps(L.asdict(nobody), ensure_ascii=False)


def test_general_number_is_told_from_a_persons_number_without_cards():
    # one paragraph, no cards: a number right after a name is the person's, a department line is the company's
    text = """<h1>Контакты</h1><div class="text"><p>Шаблонов Пётр Ильич — коммерческий директор<br>
      Тел.: +7 (351) 000-00-31<br>E-mail: shablonov@primer-zavod.ru<br><br>
      Примерова Анна Сергеевна — ведущий менеджер<br>Тел.: +7 (351) 000-00-32<br><br>
      Отдел продаж: +7 (351) 000-00-40</p></div>"""
    assert L.company_phone([site_page(bare_page(text))]) == "+7 (351) 000-00-40"
    # with no department line the page has no general number at all: nothing is better than somebody's own
    assert L.company_phone([site_page(bare_page(text.replace("Отдел продаж: +7 (351) 000-00-40", "")))]) == ""
    # a page that names nobody: the fax and the mobile are skipped, the office number is taken
    office = bare_page("<h1>Контакты</h1><p>Факс: +7 (351) 000-00-39</p><p>Моб.: +7 999 000-00-00</p>"
                       "<p>Телефон: +7 (351) 000-00-41</p>")
    assert L.company_phone([site_page(office)]) == "+7 (351) 000-00-41"
    # a person's own page: whatever number stands in its text is that person's
    assert L.company_phone([site_page(office, "person")]) == ""
    # the contacts page is asked first, the homepage after it
    assert L.company_phone([site_page(HOME, "home"), site_page(office)]) == "+7 (351) 000-00-41"


def test_mailbox_named_after_a_mobile_number_is_never_kept(web, fetcher):
    card = """<div class="item"><div>Образцов Пётр Ильич</div><div>Коммерческий директор</div>
      <div>+7 999 000-00-00</div><div>{box}@primer-zavod.ru</div>{more}</div>"""
    for box in ("9990000000", "89990000000", "79990000000", "obraztsov-9990000000"):
        verdict = verdicts(page(card.format(box=box, more="")))[f"{box}@primer-zavod.ru"]
        assert verdict.lead is None and verdict.reason == L.R_MOBILE_BOX, box
    # next to the person's real address such a box is masked in the evidence like any mobile number
    both = verdicts(page(card.format(box="9990000000", more="<div>obraztsov@primer-zavod.ru</div>")))
    fragment = both["obraztsov@primer-zavod.ru"].lead.fragment
    assert "********00@primer-zavod.ru" in fragment and "999" not in fragment
    # and it is not reported as the company's contact when there is nobody to write to
    web.site(SITE, {"/": bare_page("<h1>Завод Пример</h1><p>Производим станки с ЧПУ.</p>"
                                   "<p>Пишите: 89990000000@mail.ru</p>")})
    result = L.process_company(company(), run_ctx(fetcher))
    assert result.status == "no_lead" and result.no_lead["общий_контакт"] == ""
    assert "999" not in json.dumps(L.asdict(result), ensure_ascii=False)


@pytest.mark.parametrize("box, reported", [
    ("zakaz@mail.ru", True), ("primer-zavod@mail.ru", True), ("zavodprimer2011@gmail.com", True),
    ("obraztsov.petr@mail.ru", False), ("petr77@gmail.com", False), ("o_p_i@mail.ru", False),
])
def test_free_mail_box_is_a_company_contact_only_when_named_after_it(web, fetcher, box, reported):
    # no address on the company's own domain; the box on a free-mail service stands alone, no person next to it
    web.site(SITE, {"/": bare_page(f"<h1>Завод Пример</h1><p>Производим станки с ЧПУ.</p><p>Пишите: {box}</p>")})
    result = L.process_company(company(), run_ctx(fetcher))
    assert result.status == "no_lead" and result.leads == []
    assert result.no_lead["общий_контакт"] == (box if reported else "")  # a personal-looking box is not kept
    assert (box in json.dumps(L.asdict(result), ensure_ascii=False)) is reported


def test_llm_is_never_shown_a_mobile_number():
    cand = candidates(page(MOBILE_CARD.format(mobile="+7 (999) 000-00-00")))["obraztsov@primer-zavod.ru"]
    assert "+7 (***) ***-**-00" in cand.block and "999" not in cand.block and "999" not in L.llm_prompt(cand)


def test_no_output_file_of_a_run_holds_a_mobile_number(tmp_path, web):
    web.site(SITE, {"/": HOME, "/company/staff/": page(MOBILE_CARD.format(mobile="+7 (999) 000-00-00"))})
    web.site(SECOND, {"/": bare_page("<h1>Образец Станки</h1><p>Производим станки с ЧПУ.</p><p>Директор — Тестов "
                                     "Олег Петрович, моб. 8 999 000-00-01, sales@obrazec-stanki.ru</p>")})
    seeds = write_seeds(tmp_path / "seeds.csv", [["Завод Пример", SITE, "", ""], ["Образец Станки", SECOND, "", ""]])
    out = cli(tmp_path, seeds=seeds)
    assert [r["Телефон"] for r in read_csv(out / "leads.csv")] == ["+7 (495) 000-00-00"]
    assert [r["телефон"] for r in read_csv(out / "no_lead.csv")] == [""]
    files = sorted(path for path in out.iterdir() if path.is_file())
    assert {path.name for path in files} >= {"leads.csv", "no_lead.csv", "companies.csv", "state.json", "summary.json"}
    for path in files:
        assert "999" not in path.read_text("utf-8"), path.name


# --- 10. ICP exclusions hold after a site move and for the mail domain ------------------------------ #

def test_excluded_domain_matches_the_same_site_in_another_zone(web, fetcher):
    icp = L.ICP(exclude_domains=["primer-zavod.ru", "https://www.obrazec-stanki.ru/contacts/", "zavod.nnov.ru"])
    assert icp.excluded_domain("primer-zavod.ru") == "primer-zavod.ru"
    assert icp.excluded_domain("www.primer-zavod.com") == "primer-zavod.ru"  # the site has moved to another zone
    assert icp.excluded_domain("shop.obrazec-stanki.ru") == "https://www.obrazec-stanki.ru/contacts/"
    assert icp.excluded_domain("primer-zavod-stanki.ru") == ""  # a longer name is another company
    # on a shared suffix only the exact host counts, and a short name in another zone proves nothing
    assert icp.excluded_domain("zavod.tilda.ws") == "" and icp.excluded_domain("drugaya-firma.nnov.ru") == ""
    assert icp.excluded_domain("zavod.ru") == "" and icp.excluded_domain("") == ""
    assert L.ICP(exclude_domains=["abc.ru"]).excluded_domain("abc.com") == ""
    # the case this rule comes from: the list names the old domain, the catalogue gives the new one
    web.pages[f"{EXPO}/list"] = EXPO_LIST
    web.pages[f"{EXPO}/exhibitors/1?stand=1A01"] = expo_card("ЗАВОД ПРИМЕР, ООО", "https://primer-zavod.com", "Тула")
    web.pages[f"{EXPO}/exhibitors/4?stand=1A04"] = expo_card("ОБРАЗЕЦ СТАНКИ, АО", "https://drugaya-firma.ru", "Пермь")
    found, dropped = L.discover_all([L.ExpocentrAdapter(EXPO_ID)], L.ICP(exclude_domains=["primer-zavod.ru"]),
                                    fetcher, 10)
    assert [c.domain for c in found] == ["drugaya-firma.ru"]
    assert dropped == {"исключение ICP: домен primer-zavod.ru (тот же сайт в другой зоне: primer-zavod.com)": 1}
    assert web.fetched("primer-zavod") == 0  # the excluded company's site is never asked


def test_company_that_moved_to_an_excluded_domain_is_not_crawled(web, fetcher):
    new = "https://primerzavod-stanki.ru"  # spelled like the old domain: the crawl accepts it as the same company
    web.pages[f"{SITE}/"] = redirect(f"{new}/")
    web.site(new, {"/": HOME, "/contacts/": page(DIRECTOR.format(domain="primerzavod-stanki.ru"))})
    icp = L.ICP(exclude_domains=["primerzavod-stanki.ru"])
    assert icp.excluded_by_name("Завод Пример", DOMAIN) == ""  # the old domain is not listed: discovery lets it in
    result = L.process_company(company(), run_ctx(fetcher, icp))
    assert result.leads == [] and result.status == "no_lead"
    assert result.no_lead["причина"] == ("исключение ICP: домен primerzavod-stanki.ru "
                                         "(сайт компании перенаправляет на primerzavod-stanki.ru)")
    assert web.fetched("/contacts/") == 0  # nothing but the homepage was asked of the new site
    # without the exclusion the same site gives the lead
    again = L.process_company(company(), run_ctx(fetcher))
    assert [r["Email"] for r in again.leads] == ["obraztsov@primerzavod-stanki.ru"]


def test_exclusion_by_the_mail_domain_the_site_uses(web, fetcher):
    # the company is listed by the domain of its mail, the catalogue gave its site on another domain
    staff = page(TWO_ON_ANOTHER_DOMAIN.format(domain="mail-primera.ru"),
                 footer="© 2011–2026 ООО «Завод Пример», office@mail-primera.ru")
    web.site(SITE, {"/": HOME, "/company/staff/": staff})
    kept = L.process_company(company(), run_ctx(fetcher))
    assert {r["Email"] for r in kept.leads} == {"obraztsov@mail-primera.ru", "testov@mail-primera.ru"}
    result = L.process_company(company(), run_ctx(fetcher, L.ICP(exclude_domains=["mail-primera.ru"])))
    assert result.leads == [] and result.status == "no_lead" and result.rejects["исключение ICP"] == 2
    assert result.no_lead["причина"] == ("исключение ICP: домен mail-primera.ru "
                                         "(сайт или почта компании: mail-primera.ru)")
    # the same name in another zone counts for the mail domain too
    other_zone = L.process_company(company(), run_ctx(fetcher, L.ICP(exclude_domains=["mail-primera.com"])))
    assert other_zone.leads == [] and "mail-primera.com" in other_zone.no_lead["причина"]
    # a domain of somebody else printed on the site (not accepted as the site's own) excludes nobody
    unrelated = L.process_company(company(), run_ctx(fetcher, L.ICP(exclude_domains=["partner-site.ru"])))
    assert len(unrelated.leads) == 2
