"""Freshness: newest dated item first, no meta descriptions, honest dates, no stale «теперь»."""

from __future__ import annotations

import json
from datetime import date

import pytest
from conftest import NEWS_ANSWER, SITE, VALID_ANSWER, FakeBackend, acme_pages

import personalize as P

ROW = P.RowInfo(0, 2, "Акме Станки", "acme-stanki.ru", "")


def page(url, body, kind, title="", head=""):
    return P.parse_html(url, f"<html><head><title>{title}</title>{head}</head><body><main>{body}</main></body></html>",
                        kind)


@pytest.mark.parametrize("text,expected", [
    ("30.09.2026 Релиз 89g — множественные источники", date(2026, 9, 30)),
    ("2026-03-19 TESID will participate CCMT2026 ▪ Shanghai", date(2026, 3, 19)),
    ("Опубликовано 6 сентября 2024 года", date(2024, 9, 6)),
    ("Новинки оборудования за март 2026", date(2026, 3, 31)),  # month precision: end of month
    ("15 мая 2026 года открыли склад", date(2026, 5, 15)),
    ("July 10, 2025 — visit of the DMG MORI president", date(2025, 7, 10)),
    ("10 July 2025", date(2025, 7, 10)),
    ("2026年3月19日 参加展会", date(2026, 3, 19)),
    ("Весна 2026: знания, обучение и ИИ", date(2026, 4, 30)),
    ("C 1.10.2021 работаем в субботу", date(2021, 10, 1)),
])
def test_find_dates_formats(text, expected):
    assert P.find_dates(text)[0] == expected


@pytest.mark.parametrize("text", ["основана в 2009 году", "© 2015-2026", "версия 2.5.1", "ГОСТ 8338-75"])
def test_year_only_and_non_dates_are_not_dates(text):
    assert P.find_dates(text) == []


def test_staleness_is_relative_to_today():
    assert P.is_stale(date(2025, 7, 10)) and not P.is_stale(date(2025, 11, 1))
    assert P.year_is_stale(2024) and not P.year_is_stale(2025)  # 2025 may still be < 12 months ago


# --- what the LLM sees ------------------------------------------------------- #

def test_meta_description_is_never_a_fact_source():
    head = ('<meta name="description" content="⭐⭐⭐⭐⭐ Купить можно из наличия 6066 станков. Доставка и '
            'самовывоз по всей России">')
    body = ("<p>Мировое оборудование поставляет металлообрабатывающие станки и ведёт дилерскую программу "
            "для региональных партнёров.</p>")
    pages = [page("https://ok-stanok.test/", body, "home", "Мировое оборудование", head)]
    sources = P.build_sources(pages)
    assert "6066" not in sources[0].content and "Описание" not in sources[0].content
    text, _, _ = P.extractive_fact(pages)
    assert "6066" not in text and "дилерскую программу" in text
    meta_answer = json.dumps({"personalization": "Увидели, что у вас в наличии 6066 станков.",
                              "source_url": "https://ok-stanok.test/",
                              "evidence": "Купить можно из наличия 6066 станков"}, ensure_ascii=False)
    ans, reason = P.validate_llm_answer(meta_answer, sources)
    assert ans is None and "цитата не найдена" in reason  # the snippet is not on the page the LLM sees


def test_news_first_and_digest_shows_newest_items_of_an_oldest_first_page():
    body = ("<p>03.10.2025 Выпустили новый вертикальный 30-тонный пресс PZO-30 PRO для мастерских</p>"
            "<p>12.12.2025 Новинки оборудования декабря: гидравлические прессы с ЧПУ</p>"
            "<p>15.05.2026 Новинки оборудования за 1 квартал 2026 года: листогибы и вальцы</p>")
    pages = acme_pages() + [page("https://p-z-o.test/news", body, "news", "Новости")]
    sources = P.build_sources(pages)
    assert sources[0].kind == "news"
    lines = sources[0].content.split("\n")
    assert lines[1] == P.DIGEST_TITLE and lines[2].startswith("15.05.2026")  # newest first
    assert sources[0].body_start == lines.index(P.DIGEST_END) + 1
    fresh = P.freshest_item(sources)
    assert fresh.when == date(2026, 5, 15) and fresh.url == "https://p-z-o.test/news"


def test_holiday_notices_and_future_events_are_not_fresh_items():
    body = ("<p>30.09.2026 Поздравляем с Днём машиностроителя всех партнёров и клиентов компании!</p>"
            "<p>14.04.2027 Приглашаем на выставку Металлообработка-2027 в Москве, стенд 21B45</p>"
            "<p>06.09.2024 Получен патент на модульные весы для статического взвешивания вагонов</p>")
    sources = P.build_sources([page("https://vzvt.test/news/", body, "news")])
    assert P.freshest_item(sources) is None  # only a 2024 item is left, and it is stale


# A catalogue menu built from <div>s (no <nav>): it opens every page of the site and is
# longer than the whole per-page budget of the prompt (manotom.ru, alfamatic.ru).
DIV_MENU = "<div class='menu'>" + "".join(
    f"<div><a href='/catalog/{i}/'>Раздел каталога номер {i}: приборы и запасные части</a></div>"
    for i in range(1, 91)) + "</div>"
WIDGET = "<div class='last-news'><p>25.09.2026</p><p>Открыли сервисный центр в Томске</p></div>"


def _site_with_div_menu(bodies):
    kinds = {"": "home", "about/": "about", "news/": "news"}
    return [P.parse_html(f"https://menu.test/{path}", f"<html><body>{DIV_MENU}<div>{body}</div>{WIDGET}</body></html>",
                         kinds[path]) for path, body in bodies.items()]


def test_site_wide_menu_does_not_eat_the_page_budget():
    pages = _site_with_div_menu({
        "": "<p>Завод выпускает манометры и поставляет их на 300 предприятий.</p>",
        "about/": "<p>Завод основан в 1941 году и работает в Томске.</p>",
        "news/": "<p>12.08.2026 Запустили серийный выпуск цифровых манометров ДМ5002 для газовых сетей.</p>"})
    assert len("\n".join(pages[2].lines[:90])) > 3500  # the menu alone is longer than the page budget
    sources = P.build_sources(pages)
    shown = "\n".join(s.content for s in sources)
    assert [s.kind for s in sources] == ["news", "about", "home"]
    assert "Запустили серийный выпуск цифровых манометров" in sources[0].content
    assert "основан в 1941 году" in shown and "на 300 предприятий" in shown
    assert "Раздел каталога номер" not in shown
    # A dated entry repeated on every page (a "latest news" block) is a fact, not a menu:
    # it stays, together with the line next to its date.
    assert "25.09.2026\nОткрыли сервисный центр в Томске" in sources[0].content
    answer = json.dumps({"personalization": "Увидели, что в августе 2026 вы запустили серийный выпуск цифровых "
                                            "манометров ДМ5002.",
                         "source_url": "https://menu.test/news/",
                         "evidence": "12.08.2026 Запустили серийный выпуск цифровых манометров ДМ5002"},
                        ensure_ascii=False)
    assert P.validate_llm_answer(answer, sources)[0] is not None
    menu_answer = json.dumps({"personalization": "Увидели, что у вас есть раздел каталога номер 7.",
                              "source_url": "https://menu.test/news/",
                              "evidence": "Раздел каталога номер 7: приборы и запасные части"}, ensure_ascii=False)
    assert "цитата не найдена" in P.validate_llm_answer(menu_answer, sources)[1]  # the LLM never saw the menu


def test_dropped_menu_lines_do_not_make_strangers_neighbours():
    # «Подробнее» closes every entry on every page. Without it the date of the first entry
    # would sit right above the title of the second one and pass for its date.
    more = "<p>Подробнее</p>"
    news = ("<p>Открыли склад в Казани для дилеров Поволжья</p><p>10.03.2024</p>" + more
            + "<p>Запустили линию порошковой окраски корпусов</p><p>12.09.2026</p>" + more)
    pages = [page("https://gap.test/", "<p>Завод корпусов для электрощитов.</p>" + more, "home"),
             page("https://gap.test/about/", "<p>Работаем в Самаре с 2008 года.</p>" + more, "about"),
             page("https://gap.test/news/", news, "news")]
    src = P.build_sources(pages)[0]
    body = src.content.split("\n")[src.body_start:]
    assert body == ["Открыли склад в Казани для дилеров Поволжья", "10.03.2024", P.GAP,
                    "Запустили линию порошковой окраски корпусов", "12.09.2026"]
    assert P.fact_date_of("", "Запустили линию порошковой окраски корпусов", src) == (date(2026, 9, 12), None)
    assert all(P.GAP not in "\n".join(P._item_excerpt(src.content.split("\n"), i))
               for _, i in P.dated_items(src.content.split("\n")))  # the marker never reaches the digest


def test_two_pages_are_too_few_to_call_a_shared_line_a_menu():
    pages = _site_with_div_menu({"": "<p>Завод выпускает манометры.</p>", "about/": "<p>Основан в 1941 году.</p>"})
    assert P.site_boilerplate(pages) == frozenset()
    assert "Раздел каталога номер 1:" in P.build_sources(pages)[0].content


# --- validation of the date wording ------------------------------------------ #

def _answer(**kw):
    return json.dumps({**VALID_ANSWER, **kw}, ensure_ascii=False)


LABEL_PAGE = ("<p>Почему мы</p><p>Мы дополнительно расширили парк станков, чтобы брать и срочные заказы на "
              "этикетки.</p><p>Печатаем этикетки на флексографских машинах с 2015 года.</p>")


@pytest.mark.parametrize("word", ["в этом году", "недавно", "теперь"])
def test_relative_time_without_a_date_is_rejected(word):
    sources = P.build_sources([page("https://label.test/", LABEL_PAGE, "about")])
    text = f"Увидели, что {word} вы расширили парк станков, чтобы брать и срочные заказы на этикетки."
    ans, reason = P.validate_llm_answer(_answer(personalization=text, source_url="https://label.test/",
                                                evidence="Мы дополнительно расширили парк станков"), sources)
    assert ans is None and word in reason.lower()
    fixed = "Увидели, что вы расширили парк станков, чтобы брать и срочные заказы на этикетки."
    ans, reason = P.validate_llm_answer(_answer(personalization=fixed, source_url="https://label.test/",
                                                evidence="Мы дополнительно расширили парк станков"), sources)
    assert reason == "" and ans.fact_date is None


def test_relative_time_with_an_old_date_is_rejected_but_fresh_date_allows_it():
    old = page("https://bearing.test/", "<p>C 1.10.2021 товары «Под заказ 1-2 дня» теперь можно оформить "
                                        "самостоятельно в корзине</p>", "news")
    ans, reason = P.validate_llm_answer(_answer(
        personalization="Увидели, что теперь товары «Под заказ 1-2 дня» можно оформить самостоятельно.",
        source_url="https://bearing.test/", evidence="товары «Под заказ 1-2 дня» теперь можно оформить"),
        P.build_sources([old]))
    assert ans is None and "теперь" in reason
    sources = P.build_sources(acme_pages(with_news=True))
    ans, reason = P.validate_llm_answer(json.dumps({
        **NEWS_ANSWER, "personalization": "Увидели, что недавно вы запустили участок лазерной резки на 30 кВт."},
        ensure_ascii=False), sources)
    assert reason == "" and ans.fact_date == date(2026, 9, 12)


VZVT_NEWS = ("<p>06.09.2024</p><p>Получен патент на модульные весы для статического взвешивания вагонов "
             "и цистерн на ходу</p>")


def test_stale_fact_without_year_is_marked_in_comment():
    pages = [page("https://vzvt.test/news/", VZVT_NEWS, "news", "Новости")]
    backend = FakeBackend({"personalization": "Увидели, что вы получили патент на модульные весы для "
                                              "статического взвешивания вагонов.",
                           "source_url": "https://vzvt.test/news/",
                           "evidence": "Получен патент на модульные весы для статического взвешивания вагонов"})
    text, source, notes = P.personalize(ROW, pages, backend)
    assert text.startswith("Увидели") and source == "https://vzvt.test/news/"
    assert any("факт старше 12 мес." in n and "09.2024" in n and "год не указан" in n for n in notes)


def test_years_inside_the_quote_outrank_the_date_of_the_line_above():
    # intervolga.ru: a company history, one year per line. The quote names 2022 and 2024, and
    # the comment said "date on the page: 10.2019, no year in the text" after the line above.
    body = ("<p>В октябре 2019 нас стало почти 70. Мы стали аккредитованной ИТ-компанией.</p>"
            "<p>В 2022 году компания доросла до 100 человек. В 2024 — до 150.</p>"
            "<p>Моя цель — построить команду, в которой хочется работать.</p>")
    src = P.build_sources([page("https://history.test/about/", body, "about")])[0]
    quote = "В 2022 году компания доросла до 100 человек. В 2024 — до 150."
    assert P.fact_date_of("", quote, src) == (None, 2024)
    backend = FakeBackend({"personalization": "Увидели, что вы в 2024 году выросли до 150 человек, а в 2022 году в "
                                              "компании было 100.",
                           "source_url": "https://history.test/about/", "evidence": quote})
    _, _, notes = P.personalize(ROW, [page("https://history.test/about/", body, "about")], backend)
    assert not any("год не указан" in n or "10.2019" in n for n in notes)
    # A date above a title that names an earlier year is still the date of that entry.
    news = "<p>15.01.2026</p><p>Подвели итоги 2025 года: отгрузили 300 станков в 40 регионов</p>"
    src = P.build_sources([page("https://history.test/news/", news, "news")])[0]
    assert P.fact_date_of("", "Подвели итоги 2025 года: отгрузили 300 станков", src) == (date(2026, 1, 15), None)


def test_stale_fact_with_honest_year_is_accepted_without_warning():
    # The year must be inside the quote, as any number: a neighbour line's date could
    # belong to another entry. The retry quotes the date line together with the title.
    pages = [page("https://vzvt.test/news/", VZVT_NEWS, "news", "Новости")]
    text = "Увидели, что в 2024 году вы получили патент на модульные весы для взвешивания вагонов."
    without_date = {"personalization": text, "source_url": "https://vzvt.test/news/",
                    "evidence": "Получен патент на модульные весы для статического взвешивания вагонов"}
    with_date = {**without_date, "evidence": "06.09.2024 Получен патент на модульные весы для статического"}
    backend = FakeBackend(without_date, with_date)
    result, _, notes = P.personalize(ROW, pages, backend)
    assert "не в цитате" in backend.prompts[1]
    assert result == text
    assert any("год назван в тексте" in n for n in notes)
    assert not any("год не указан" in n for n in notes)


@pytest.mark.parametrize("line,expected", [
    ("30.09.2026Релиз 89g", date(2026, 9, 30)),  # experium.ru/updates
    ("1 октября 2026 Читать ~ 8 минутЧитать ~ 8 мин", date(2026, 10, 1)),  # adesk.ru/blog
    ("15.05.2026 Новинки оборудования за 1 квартал 2026 года", date(2026, 5, 15)),
    ("Опубликовано: 12.09.2026", date(2026, 9, 12)),
    ("Ниже рассматриваем требования и сроки, актуальные на сентябрь 2026 года.", None),  # teamly.ru/blog teaser
    ("В июле 2026 года депутаты завершили рассмотрение законопроекта № 1271570-8 о поддержке технологий", None),
])
def test_only_entry_dates_count_as_news_dates(line, expected):
    assert P.item_date(line) == expected


def test_experium_layout_short_date_line_is_joined_with_its_first_line():
    body = ("<p>Последние изменения</p><p>30.09.2026Релиз 89g</p>"
            "<p>Множественные источники обновления данных в карточке человека</p>"
            "<p>Теперь в карточке человека сохраняется не только источник поступления в БД, но и история "
            "источников последующих обновлений.</p>")
    text, source, notes = P.extractive_fact([page("https://experium.test/updates", body, "news")])
    assert text.startswith("30.09.2026Релиз 89g Множественные источники")
    assert not any("старше" in n for n in notes)


def test_date_stub_below_its_entry_is_not_glued_to_the_next_entry():
    # adesk.ru/blog: title, teaser, then "1 октября 2026 Читать ~ 8 мин"; the next line is ANOTHER post.
    body = ("<p>Автоматизация учета: как Adesk помогает избавиться от финансового хаоса</p>"
            "<p>1 октября 2026 Читать ~ 8 минут</p>"
            "<p>Как выбрать сервис управленческого учета для малого и среднего бизнеса</p>")
    text, _, _ = P.extractive_fact([page("https://adesk.test/blog/", body, "news")])
    assert not text.startswith("1 октября 2026")


# --- one extra round when a fresher / more specific fact exists --------------- #

def test_llm_is_pointed_to_a_fresher_item_and_can_switch():
    backend = FakeBackend(VALID_ANSWER, NEWS_ANSWER)  # first an evergreen "since 2009", then the news
    text, source, notes = P.personalize(ROW, acme_pages(with_news=True), backend)
    assert len(backend.prompts) == 2
    assert "более свежая запись (09.2026)" in backend.prompts[1] and "12.09.2026 Запустили" in backend.prompts[1]
    assert text == NEWS_ANSWER["personalization"] and source == f"{SITE}/news/"
    assert any("LLM выбрала другой факт" in n for n in notes)
    assert any("дата факта на странице: 09.2026" in n for n in notes)


def test_llm_may_keep_its_fact_after_the_nudge():
    backend = FakeBackend(VALID_ANSWER)  # repeats the same answer
    text, source, notes = P.personalize(ROW, acme_pages(with_news=True), backend)
    assert len(backend.prompts) == 2  # exactly one extra round, never a loop
    assert text == VALID_ANSWER["personalization"] and source == f"{SITE}/about/"
    assert any("оставила прежний факт" in n for n in notes)
    assert any("слабый факт" in n and "с 2009 года" in n for n in notes)


def test_failed_retry_after_nudge_keeps_the_first_valid_answer():
    broken = {**NEWS_ANSWER, "evidence": "этой цитаты нет на странице вообще никак"}
    backend = FakeBackend(VALID_ANSWER, broken)
    text, _, notes = P.personalize(ROW, acme_pages(with_news=True), backend)
    assert text == VALID_ANSWER["personalization"]
    assert len(backend.prompts) == P.LLM_ATTEMPTS + 1
    assert any("оставлен первый" in n for n in notes)


def test_hero_banner_fact_is_nudged_when_other_pages_exist():
    home = page(f"{SITE}/", "<h1>Акме Станки</h1><p>Доставка станков по всей России и самовывоз со склада в "
                            "Екатеринбурге</p>", "home", "Акме Станки")
    pages = [home] + acme_pages()[1:]
    hero = {"personalization": "Увидели, что вы доставляете станки по всей России и со склада в Екатеринбурге.",
            "source_url": f"{SITE}/", "evidence": "Доставка станков по всей России и самовывоз со склада"}
    better = {**VALID_ANSWER, "personalization": "Увидели, что вы поставляете оборудование на 140 предприятий "
                                                 "в России и Казахстане.",
              "evidence": "Мы поставляем оборудование на 140 предприятий в России и Казахстане"}
    backend = FakeBackend(hero, better)
    text, _, _ = P.personalize(ROW, pages, backend)
    assert "слабый факт" in backend.prompts[1] and "по всей России" in backend.prompts[1]
    assert "140 предприятий" in text


def test_extractive_mode_prefers_fresh_news_and_flags_stale_lines():
    body = "<p>06.09.2024 Получен патент на модульные весы для статического взвешивания вагонов и цистерн</p>"
    text, source, notes = P.extractive_fact([page("https://vzvt.test/news/", body, "news")])
    assert "патент" in text and any("факт старше 12 мес." in n for n in notes)
    text, source, _ = P.extractive_fact(acme_pages(with_news=True))
    assert source == f"{SITE}/news/" and "лазерной резки" in text


def test_opening_hours_notice_is_not_a_personalization():
    body = "<p>C 1.10.2021 работаем в субботу с 10:00 до 17:00.</p><p>Подшипники SKF и FAG со склада в Москве.</p>"
    sources = P.build_sources([page("https://bearing.test/", body, "about")])
    ans, reason = P.validate_llm_answer(_answer(
        personalization="Увидели, что с 1 октября 2021 года вы работаете в субботу с 10:00 до 17:00.",
        source_url="https://bearing.test/", evidence="C 1.10.2021 работаем в субботу с 10:00 до 17:00"), sources)
    assert ans is None and "служебное объявление" in reason


def test_weaker_answer_after_nudge_does_not_replace_the_first():
    first = {**VALID_ANSWER, "personalization": "Увидели, что вы поставляете оборудование на 140 предприятий "
                                                "в России и Казахстане.",
             "evidence": "Мы поставляем оборудование на 140 предприятий в России и Казахстане"}
    backend = FakeBackend(first, VALID_ANSWER)  # undated -> nudge to the news; then "since 2009" (weak)
    text, _, notes = P.personalize(ROW, acme_pages(with_news=True), backend)
    assert text == first["personalization"]
    assert any("оставлен первый" in n for n in notes)
