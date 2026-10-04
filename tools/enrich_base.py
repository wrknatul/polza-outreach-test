#!/usr/bin/env python3
"""Enrich the Task 1-2 base: decision maker, contact level, segments, triggers, pain hypothesis.

Input:  task2_personalized.csv (the base with the checked personalisation) and
        lpr/part_*.csv (research on each company's own website: the person's
        name split into parts, a short job title for the letter, an address
        only if the page prints it next to that person, the date of the name
        source, a department phone number). For the lead base both come from
        tools/build_base_all.py: every row is a named decision maker whose
        address is printed in his or her card (column `тип_адреса`).
Output: task1_2_enriched.csv (utf-8-sig). The first six columns are the ones
        the Polza course asks for: Имя, Фамилия, Должность, Email, Телефон,
        Компания. The same run fills four columns of task4_final.csv
        (сегмент, язык_письма, триггер, Гипотеза_боли) from the same dictionaries.

Nothing is guessed. A person's address is used only when it is printed next
to the surname on the `источник_имени` page (re-fetched on every run), is on
the company's own domain and the domain has MX. Every trigger is re-checked on
its page on the day of the run: a vacancy by its title, a dealer programme by
the word «дилер», an exhibition or an expansion by the phrase the row names
(on the page the fact was taken from). An exhibition or an expansion is a
trigger only while it is fresh: the event is at most TRIGGER_MONTHS months old
on the day of the run or still ahead (TRIGGER_EVENT holds its month). A page
that answers but no longer confirms the trigger (the phrase is gone, the page
is closed in robots.txt or answers 404) drops it: the row falls back to the
hypothesis of its vertical, and the reason is written to `примечание` and
printed as ВНИМАНИЕ. A trigger page that does not answer stops the run.

Priority of a lead (the sending order): A — a confirmed trigger and a decision
maker from sales; B — a confirmed trigger or a personal mailbox; C — a role
mailbox (dir@, kd@ in the person's card) with no trigger: somebody else may
read it. For a row of the old base (a department mailbox) the rule is the old
one: A — a trigger, a sales person and addressing by name; B — a trigger or
a named person; C — the rest.

One rule for a name that cannot be used in the letter (checked for every row):
  - the last mention of the person on the site is 24 months old or older, or
    the person works in another legal entity or not in the division the letter
    goes to: the name is not stored, the row stays at level В;
  - the mention is 12 to 24 months old, or it is older but a standing page of
    the site (STANDING_PAGE, re-fetched on every run) still names the person:
    the name is stored for reference with `обращаться_по_имени = нет`
    (level Б, email 1 goes out in variant «а»).

Whether a department mailbox is published on the site is checked by
tools/build_base_all.py; this script checks syntax and MX for every address.
Mailbox liveness is not probed here (no SMTP): that is the validator's job
before the launch.

A page that does not answer is not a reason to change a row quietly. In
the lead base the address stays as tools/build_base_all.py validated it, and
the row says that it was not re-checked today. A trigger is different: it
changes the priority and the hypothesis, so a trigger whose page did not
answer is neither kept unchecked nor dropped — the script stops with code 3
and writes nothing. It stops the same way if more than half of the live checks
of a run got no answer: the network (or the proxy) is down. A lead whose page
answers but no longer prints the address next to the surname is a mistake
(ОШИБКА): rebuild the base.

Network: DNS (dig) and GET requests to the companies' own pages only; the
pages go through a SOCKS proxy when POLZA_SOCKS=host:port is set. The site's
robots.txt is asked before every page (build_base_common.get_page): a closed
page is never requested, and a lead whose page is closed is a mistake (ОШИБКА)
— tools/build_base_all.py rejects such a lead when the base is rebuilt.

Usage: python3 tools/enrich_base.py [--check] [--out PATH]
  --check     validate and report only, write nothing
  --out PATH  enriched CSV (default: task1_2_enriched.csv)

Exit code: 0 — done, 1 — the input has mistakes (each is printed as ОШИБКА),
2 — there is no lpr/part_*.csv, 3 — the network failed: more than half of the
live checks got no answer, or the page of a trigger did not answer (nothing is
written). lpr/part_1.csv is a working file written by tools/build_base_all.py
and is not published; a clone of the public repository has the result,
task1_2_enriched.csv, and can rebuild it after a run of build_base_all.py.
"""
import csv
import io
import os
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path

from build_base_common import (FREE_BOX, GENERIC_ROLE_PREFIX, NAMED_BOX, ROBOTS_CLOSED, ROLE_BOX, ROLE_WINDOW, ROOT,
                               email_on_page, get_page, has_mx, host_of, name_distance, no_page,
                               site_uses_mail_domain, visible_text)
from build_base_common import same_site as same_host

BASE = ROOT / "task2_personalized.csv"
LPR_DIR = ROOT / "lpr"
OUT = ROOT / "task1_2_enriched.csv"
THEIR_BASE = ROOT / "task4_final.csv"
BOM = b"\xef\xbb\xbf"

OUT_FIELDS = [
    # 1. the six columns of the course (lesson 2, item 4.4)
    "Имя", "Фамилия", "Должность", "Email", "Телефон", "Компания",
    # 2. the rest of the name and the company
    "Отчество", "компания_в_письме", "site",
    # 3. the decision maker and how the first letter addresses the company
    "уровень_контакта", "вариант_письма_1", "обращаться_по_имени", "должность_в_письме",
    "сегмент_ЛПР", "источник_имени", "дата_источника_имени",
    # 4. the address and its validation
    "email_отдела", "contact_role", "email_source", "тип_адреса", "валидация", "дата_проверки",
    # 5. segments and triggers
    "вертикаль", "segment", "sales_signal", "тип_триггера", "приоритет",
    # 6. geography
    "город", "регион_группа", "часовой_пояс",
    # 7. personalisation and the pain hypothesis
    "Персонализация", "Источник", "Гипотеза_боли", "Проверка_соответствия", "Комментарий", "примечание",
]

MAX_HYPOTHESIS_WORDS = 16   # counted by word_count(), the strict `wc -w` variant
WORD_BREAK = re.compile(rb"[\t\n\x0b\x0c\r \x85\xa0]+")
MAX_TITLE_WORDS = 3         # {{jobTitle}} in letter 1, variant «б»
# The title in the letter is a run of the site's own words. Where no run of three words names the role,
# the whole title is kept: company -> its word limit (tools/check_chain.py recounts the letter with it).
TITLE_WORDS = {}  # e.g. {"Teachbase": 4} for «руководитель направления развития бизнеса»
STALE_MONTHS = 12           # a dated name source this old (to the month) is not used for addressing by name
DROP_MONTHS = 24            # ... and one this old is not stored at all, unless a standing page still names the person
TRIGGER_MONTHS = 6          # an exhibition or an expansion older than this (to the month) is not a trigger any more
NO_DATE = "страница без даты"
NO_LPR_FILES = ("нет lpr/part_*.csv: этот рабочий файл пишет tools/build_base_all.py, в публичный репозиторий он "
                "не входит; готовый результат — task1_2_enriched.csv")
YES, NO = "да", "нет"

VALIDATION = ("синтаксис: да · MX: да · опубликован на сайте: да · "
              "живость ящика: не проверялась, перед запуском — валидатор")
# A lead whose page did not answer today: the address stands as validated when the base was built.
VALIDATION_NOT_RECHECKED = ("синтаксис: да · MX: да · опубликован на сайте: сегодня страница не ответила, "
                            "адрес проверен при сборке базы · "
                            "живость ящика: не проверялась, перед запуском — валидатор")
NO_ANSWER = "не ответила"
# The site answered that the page is gone: a change on the site, not a network failure.
PAGE_GONE = ("(HTTP 404)", "(HTTP 410)")
SCHEMES = ("https://", "http://")  # four sites of the base have no https at all
EXIT_NETWORK_DOWN = 3
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
PHONE_RE = re.compile(r"^[+\d][\d\s()\-]{8,}\d( \(доб\. [\d, ]+\))?$")
DATE_RE = re.compile(r"^(20\d\d)(?:-(0[1-9]|1[0-2]))?$")  # «ГГГГ-ММ», or «ГГГГ» when the page gives the year only
MONTH_RE = re.compile(r"^20\d\d-(0[1-9]|1[0-2])$")          # «ГГГГ-ММ» only: the month of a trigger event
PUNCTUATION = ",.;:()«»\"\u2014"

# --- geography: every value of the base `city` column -> (city, region group, time zone, note) ---
MSK_MO, SPB, REGIONS = "Москва и МО", "Санкт-Петербург", "регионы"
CITY = {
    "Москва": ("Москва", MSK_MO, "МСК", ""),
    "Красногорск (Московская обл.)": ("Красногорск", MSK_MO, "МСК", ""),
    "Павловский Посад (МО)": ("Павловский Посад", MSK_MO, "МСК", ""),
    "Воскресенск (МО)": ("Воскресенск", MSK_MO, "МСК", ""),
    "Луховицы (МО)": ("Луховицы", MSK_MO, "МСК", ""),
    "Подольск (МО)": ("Подольск", MSK_MO, "МСК", ""),
    "Люберцы (МО)": ("Люберцы", MSK_MO, "МСК", ""),
    "Московская обл. (Томилино)": ("Томилино", MSK_MO, "МСК", ""),
    "Московская обл. (склады в Чехове и Ступине)": ("Московская обл.", MSK_MO, "МСК", "склады в Чехове и Ступине"),
    "Санкт-Петербург": ("Санкт-Петербург", SPB, "МСК", ""),
    "Волгоград": ("Волгоград", REGIONS, "МСК", ""),
    "Нижегородская обл.": ("Нижегородская обл.", REGIONS, "МСК", ""),
    "Казань": ("Казань", REGIONS, "МСК", ""),
    "Пенза": ("Пенза", REGIONS, "МСК", ""),
    "Орёл": ("Орёл", REGIONS, "МСК", ""),
    "Калуга": ("Калуга", REGIONS, "МСК", ""),
    "Ростов-на-Дону": ("Ростов-на-Дону", REGIONS, "МСК", ""),
    "Набережные Челны (пункт выдачи в Москве)": ("Набережные Челны", REGIONS, "МСК", "пункт выдачи в Москве"),
    "Ижевск": ("Ижевск", REGIONS, "МСК+1", ""),
    "Челябинск": ("Челябинск", REGIONS, "МСК+2", ""),
    "Миасс (Челябинская обл.)": ("Миасс", REGIONS, "МСК+2", "Миасс — Челябинская обл."),
    "Екатеринбург": ("Екатеринбург", REGIONS, "МСК+2", ""),
    # the lead base
    "Подольск (Московская обл.)": ("Подольск", MSK_MO, "МСК", ""),
    "Наро-Фоминск (Московская обл.)": ("Наро-Фоминск", MSK_MO, "МСК", "офис продаж — в Москве"),
    "Томилино (Московская обл.)": ("Томилино", MSK_MO, "МСК", ""),
    "Тула": ("Тула", REGIONS, "МСК", ""),
    "Чебоксары": ("Чебоксары", REGIONS, "МСК", ""),
    "Канаш (Чувашская Республика)": ("Канаш", REGIONS, "МСК", "Канаш — Чувашская Республика"),
    "Мурманск": ("Мурманск", REGIONS, "МСК", ""),
    "Муром (Владимирская обл.)": ("Муром", REGIONS, "МСК", "Муром — Владимирская обл."),
    "Краснодар": ("Краснодар", REGIONS, "МСК", ""),
    "Нижний Новгород": ("Нижний Новгород", REGIONS, "МСК", ""),
    "Белгород": ("Белгород", REGIONS, "МСК", ""),
    "Липецк": ("Липецк", REGIONS, "МСК", ""),
    "Самара": ("Самара", REGIONS, "МСК+1", ""),
    "Уфа": ("Уфа", REGIONS, "МСК+2", ""),
    "Курган": ("Курган", REGIONS, "МСК+2", ""),
    "Новосибирск": ("Новосибирск", REGIONS, "МСК+4", ""),
    "Красноярск": ("Красноярск", REGIONS, "МСК+4", ""),
    "Барнаул": ("Барнаул", REGIONS, "МСК+4", ""),
    "Томск": ("Томск", REGIONS, "МСК+4", ""),
    "Новокузнецк": ("Новокузнецк", REGIONS, "МСК+4", ""),
    "Благовещенск": ("Благовещенск", REGIONS, "МСК+6", ""),
    "Клин (Московская обл.)": ("Клин", MSK_MO, "МСК", ""),
    "Брянск": ("Брянск", REGIONS, "МСК", ""),
    "Воронеж": ("Воронеж", REGIONS, "МСК", ""),
    "Кострома": ("Кострома", REGIONS, "МСК", ""),
    "Тверь": ("Тверь", REGIONS, "МСК", ""),
    "Выкса (Нижегородская обл.)": ("Выкса", REGIONS, "МСК", "Выкса — Нижегородская обл."),
    "Тюмень": ("Тюмень", REGIONS, "МСК+2", ""),
    "Бийск (Алтайский край)": ("Бийск", REGIONS, "МСК+4", "Бийск — Алтайский край"),
}
ZONES = ["МСК", "МСК+1", "МСК+2", "МСК+4", "МСК+6"]

# --- triggers (course, lesson 2, item 7.4: collect by key events) ---
HIRING, DEALERS, GROWTH, EXPO, NO_EVENT = ("нанимают в продажи", "набирают дилеров и партнёров",
                                           "расширение", "выставка", "события нет")
# company -> (trigger, phrase that must be in sales_signal or Персонализация: the reason the row has this trigger).
# For «расширение» and «выставка» the same phrase must also stand today on the page the fact was taken from
# (column `Источник`), so it is a word of that page: the name of the exhibition, of the new site, of the new line.
TRIGGERS = {
    "Optimalog (ООО «Оптима лог»)": (HIRING, "менеджера по продажам B2B"),
    "ООО «Ижевский кузнечно-механический завод» (ИКМЗ)": (HIRING, "менеджера по продажам"),
    "ЗАО «ПО «Муромский завод трубопроводной арматуры» (МЗТА)": (DEALERS, "дилер"),
    "Mavlad": (GROWTH, "офис"),                                           # an office in Moscow
    "Завод «Краски КВИЛ» (ООО «Завод Краски КВИЛ»)": (GROWTH, "Сенькино"),  # a new production site
    "ООО «Челябинский завод «Теплоприбор» (ЧТП)": (GROWTH, "кислородном исполнении"),  # a new serial product
    "НПФ «ТехноТранс» (ООО НПФ «Технотранс»)": (GROWTH, "инструктаж"),     # a new line of service
    "ROCCS (ООО «ИТЦ «РОККС»)": (GROWTH, "ребрендинг"),                    # a new line of business
    "ГК Волгаэнергопром": (EXPO, "ИННОПРОМ"),
    "ООО «ТЕРМОТРОНИК»": (EXPO, "АГРОПРОДМАШ"),
    "ООО «Канмаш ДСО» (Канашский машиностроительный завод)": (EXPO, "Mining & Metals"),
    "ПАО «Пензмаш»": (EXPO, "АГРОСАЛОН"),
    "RFL (reFresh Logic)": (EXPO, "CeMAT"),
    "ПЗПИ (ООО «ПЗПИ»)": (EXPO, "InterCHARM"),
    "СОЛВО (ООО «СОЛВО»)": (EXPO, "CeMAT"),
    "ООО «ЭКРОСХИМ» (ГК «ЭКРОС»)": (EXPO, "Pharmtech"),
    "ООО «БЗПА»": (EXPO, "Уголь России и Майнинг"),
    "ООО «ЭЛМАТЕК»": (EXPO, "Электрические сети"),
    "ООО «МДМпринт»": (EXPO, "IPSA"),
    "ООО Торговый Дом «Айболит» (ТД Айболит)": (EXPO, "Здравоохранение Урала"),
}
# The month of the event behind every exhibition and expansion trigger («ГГГГ-ММ», as the page of the fact dates
# it; for an announced stand with no dates on the page — the month of the announcement). An event more than
# TRIGGER_MONTHS months old is not a trigger: main() refuses such a row, so a stale event cannot stay here.
# Exhibitions of November and December 2025 in the base (Манотомь, КомплектСнаб, «Медицинские изделия») are
# facts of the personalisation only.
TRIGGER_EVENT = {
    "Mavlad": "2026-05",
    "Завод «Краски КВИЛ» (ООО «Завод Краски КВИЛ»)": "2026-07",
    "ООО «Челябинский завод «Теплоприбор» (ЧТП)": "2026-07",
    "НПФ «ТехноТранс» (ООО НПФ «Технотранс»)": "2026-07",
    "ROCCS (ООО «ИТЦ «РОККС»)": "2026-07",
    "ГК Волгаэнергопром": "2026-09",
    "ООО «ТЕРМОТРОНИК»": "2026-09",
    "ООО «Канмаш ДСО» (Канашский машиностроительный завод)": "2026-09",
    "ПАО «Пензмаш»": "2026-10",
    "RFL (reFresh Logic)": "2026-09",
    "ПЗПИ (ООО «ПЗПИ»)": "2026-10",
    "СОЛВО (ООО «СОЛВО»)": "2026-09",
    "ООО «ЭКРОСХИМ» (ГК «ЭКРОС»)": "2026-11",
    "ООО «БЗПА»": "2026-06",
    "ООО «ЭЛМАТЕК»": "2026-11",
    "ООО «МДМпринт»": "2026-08",
    "ООО Торговый Дом «Айболит» (ТД Айболит)": "2026-04",
}
# The vacancy title must be on the vacancy page today. It is taken verbatim from the signal text, or, when the
# vacancy is the fact of the personalisation, from VACANCY_TITLE (the title as the page prints it).
VACANCY_RE = re.compile(r"вакансия[^«»]*«([^»]+)»")
VACANCY_TITLE = {
    "Optimalog (ООО «Оптима лог»)": "Менеджер по продажам B2B",
    "ООО «Ижевский кузнечно-механический завод» (ИКМЗ)": "Менеджер по продажам",
}
# The page of a hiring or dealer trigger. Default = the URL at the end of sales_signal.
TRIGGER_PAGE = {
    "Optimalog (ООО «Оптима лог»)": "https://optimalog.ru/company/vacancy/",
    "ООО «Ижевский кузнечно-механический завод» (ИКМЗ)": "https://ikmz.ru/o-nas/vakansii/",
    "ЗАО «ПО «Муромский завод трубопроводной арматуры» (МЗТА)":
        "https://mztpa.ru/about/news/produktsiya-muromskogo-zavoda-truboprovodnoj-armatury-v-reestre-eaes",
}
DEALER_WORD = "дилер"
PAGE_TRIGGERS = (GROWTH, EXPO)  # confirmed by the trigger phrase on the `Источник` page

# --- pain hypothesis (course, lesson 2, item 5.3): by vertical, overridden by a confirmed trigger ---
SAAS, INDUSTRIAL = "B2B SaaS", "Промоборудование"
HYPOTHESIS_BY_VERTICAL = {
    SAAS: ("Предполагаем, что на демо записываются те, кто уже ищет решение, "
           "а до остальных продажи не дотягиваются."),
    "Интеграторы и автоматизация": ("Предполагаем, что проекты идут от вендора и из рейтингов, "
                                    "а напрямую заказчикам никто не пишет."),
    "HR-tech": ("Предполагаем, что HR-директоров вы встречаете на конференциях, "
                "а между конференциями напрямую им никто не пишет."),
    "Логистика": ("Предполагаем, что перед высоким сезоном нужны новые интернет-магазины, "
                  "а менеджеры заняты текущими клиентами."),
    "B2B-маркетинг": ("Предполагаем, что заявки идут из рейтингов и контента, "
                      "а исходящего канала на нужные компании нет."),
    "Упаковка": ("Предполагаем, что отдел продаж занят входящими заявками, "
                 "а новых производственных покупателей системно никто не ищет."),
    INDUSTRIAL: ("Предполагаем, что сделки длинные и зависят от числа начатых разговоров, "
                 "а новых заказчиков системно не ищут."),
    "Промдистрибуция": ("Предполагаем, что продажи держатся на постоянных клиентах, "
                        "а искать новых снабженцев заводов менеджерам некогда."),
    # verticals of the lead base
    "Юридические услуги": ("Предполагаем, что клиенты идут по рекомендациям и из рейтингов, "
                           "а напрямую собственникам никто не пишет."),
    "Аудит и консалтинг": ("Предполагаем, что заказчики идут по рекомендациям и с тендеров, "
                           "а напрямую нужным компаниям никто не пишет."),
    "Подбор руководителей": ("Предполагаем, что заказы дают знакомые HR-директора, "
                             "а новым компаниям напрямую никто не пишет."),
    "Инжиниринг и промбезопасность": ("Предполагаем, что заказы идут с тендеров и по рекомендациям, "
                                      "а напрямую предприятиям никто не пишет."),
    "Оптовая дистрибуция": ("Предполагаем, что продажи держатся на постоянной базе, "
                            "а искать новые компании менеджерам некогда."),
    "Стройматериалы": ("Предполагаем, что продажи идут через дилеров и постоянную базу, "
                       "а новым подрядчикам никто не пишет."),
    "Коммерческая техника": ("Предполагаем, что покупатели идут с площадок объявлений, "
                             "а автопаркам и перевозчикам напрямую никто не пишет."),
    "Логистика ВЭД": ("Предполагаем, что грузы дают постоянные клиенты, "
                      "а новым импортёрам и экспортёрам напрямую никто не пишет."),
    "Медицинские изделия": ("Предполагаем, что заказы идут с тендеров и через дистрибьюторов, "
                            "а новым покупателям напрямую никто не пишет."),
    "Полиграфия и сувениры": ("Предполагаем, что заказы приносят постоянные клиенты и поиск, "
                              "а новым компаниям напрямую никто не пишет."),
}
# «Между выставками» is said only to a company that is known to exhibit: a row with the exhibition trigger
# (and an exhibitor from the organisers' base, Task 4). The other rows of the vertical get the text above.
HYPOTHESIS_EXHIBITOR_BY_VERTICAL = {
    INDUSTRIAL: ("Предполагаем, что между выставками поток запросов проседает, "
                 "а сделки длинные и зависят от числа начатых разговоров."),
}
# Product companies that the base keeps in the integrators' vertical. They sell their own software (Клеверенс is
# a vendor itself, its projects are done by partners), so «проекты идут от вендора и из рейтингов» is not about them.
HYPOTHESIS_VERTICAL_BY_COMPANY = {"Аспро": SAAS, "Клеверенс": SAAS}
# The personalisation of these rows already says, as a fact from the site, that clients come by recommendation.
# The vertical's wording would guess the same thing again, so the guess is about the other clients.
HYPOTHESIS_BY_COMPANY = {
    "Консалтинговая Группа ЭТАЛОН": ("Предполагаем, что вторую половину клиентов приходится искать самим, "
                                     "а напрямую компаниям никто не пишет."),
    "Лаборатория промышленной безопасности": ("Предполагаем, что остальных клиентов приходится искать самим, "
                                              "а напрямую предприятиям никто не пишет."),
}
RECOMMENDATION_STEM = "рекомендац"  # what makes a HYPOTHESIS_BY_COMPANY entry necessary
# A confirmed trigger: trigger -> (the fact, the guess, stems that show {{персонализация}} already states the fact).
# In the letter the hypothesis follows the personalisation, so a fact said there is not repeated here.
HYPOTHESIS_BY_TRIGGER = {
    HIRING: ("Вы открыли вакансию «{vacancy}» — ", "предполагаем, что новому сотруднику сразу понадобятся встречи.",
             ("ваканси", "ищете")),
    DEALERS: ("Вы набираете дилеров — ", "предполагаем, что напрямую потенциальным дилерам пока никто не пишет.",
              ("дилер",)),
}

# --- the decision maker ---
SALES, CHIEF, GROWTH_ROLE, MARKETING, NO_PERSON = "продажи", "первое лицо", "развитие", "маркетинг", "отдел без имени"
# Checked in this order: «коммерческий директор» is sales, «директор по развитию» is not a chief executive.
LPR_SEGMENT_KEYWORDS = [
    (SALES, ("продаж", "коммерческ")),
    (GROWTH_ROLE, ("развити",)),
    (MARKETING, ("маркетинг",)),
    (CHIEF, ("генеральный директор", "исполнительный директор", "директор", "ceo", "основатель",
             "партнер", "партнёр")),  # «управляющий партнёр» of a law or search firm
]
# Titles the keywords cannot settle.
LPR_SEGMENT_BY_COMPANY = {
    # The card says «Руководитель отдела» in the section «Корпоративный отдел»; the «О компании» page of the
    # same site calls him the head of the corporate sales department.
    "Vkorpe (ООО «Вкорпе»)": SALES,
}
# A dated name source older than DROP_MONTHS is kept for reference only while a standing page of the same site
# (not a dated post) still names the person: company -> that page. It is re-fetched on every run.
STANDING_PAGE = {}
PERSON_FIELDS = ("имя_ЛПР", "должность_ЛПР", "источник_имени", "Имя", "Отчество", "Фамилия", "должность_в_письме",
                 "email_ЛПР_на_странице", "дата_источника_имени", "обращаться_по_имени")
# Whose mailbox is printed in the person's card (level А).
PERSONAL_BOX, SERVICE_BOX, COMPANY_BOX = "именной (у ЛПР)", "ящик должности ЛПР", "общий ящик в карточке ЛПР"
FREE_PERSONAL_BOX = "личный ящик на бесплатном домене"
# The lead base names the type itself (column `тип_адреса`, checked by tools/build_base_all.py).
LEAD_BOX = {NAMED_BOX: PERSONAL_BOX, ROLE_BOX: SERVICE_BOX, FREE_BOX: FREE_PERSONAL_BOX}

# The fact of these rows is an event that is still ahead on the day of the review. A letter sent after the date
# must speak of it in the past tense: company -> the date and what to do.
PAST_TENSE = "после этой даты формулировку факта перевести в прошедшее время"
NOTE_BY_COMPANY = {
    "ПЗПИ (ООО «ПЗПИ»)": f"InterCHARM 14–17.10.2026: писать после выставки, {PAST_TENSE}",
    "ПАО «Пензмаш»": f"«АГРОСАЛОН» 06–09.10.2026: {PAST_TENSE}",
    "Центр консалтинговых проектов (ООО «Центр консалтинговых проектов»)": f"вебинар 14.10.2026: {PAST_TENSE}",
    "Адвокатское бюро «Качкин и Партнеры»": f"вебинар 06.10.2026: {PAST_TENSE}",
    "ООО «ЭЛМАТЕК»": f"форум «Электрические сети» 17–19.11.2026: {PAST_TENSE}",
    "ООО «ЭКРОСХИМ» (ГК «ЭКРОС»)": f"«Pharmtech & Ingredients» 24–27.11.2026: {PAST_TENSE}",
    "Клеверенс": f"события октября 2026: в ноябре {PAST_TENSE.removeprefix('после этой даты ')}",
}

# --- Task 4 (the organisers' base): company -> (segment, letter language, exhibition years, hypothesis) ---
CN_EXPO = "китайский производитель-экспонент"
RU_FIRM = "российский дистрибьютор или производитель"
PUBLISHER = "издательство (ICP: спорно)"
HYPOTHESIS_EXHIBITOR = "Предполагаем, что заказчиков в России вам дают в основном выставки, а между ними запросов мало."
HYPOTHESIS_HNC = "Предполагаем, что заводы ищут замену западным брендам, но о вас как о поставщике знают не все."
# A Russian publisher: the exhibitor hypothesis without «в России».
HYPOTHESIS_PUBLISHER = "Предполагаем, что заказчиков вам дают в основном выставки, а между ними запросов мало."
# Language: RU when the company is Russian or its own site has a Russian version (checked on the pages in
# .cache/ and on the live site, 03.10.2026), otherwise EN. Ezhong: ru.ezhonggroup.com, one of 11 languages
# in the menu. Howfit: ru.howfit-press.com, one of about 190 auto-translated mirrors — a weak signal, but the
# rule is the same for both. JAT and Jimmy: the sites answer with an anti-bot challenge, which is not
# bypassed; a Russian version is not confirmed -> EN.
THEIR = {
    "Mingwen Intelligent": (CN_EXPO, "EN", "2024", HYPOTHESIS_EXHIBITOR),
    "HNC": (RU_FIRM, "RU", "2024", HYPOTHESIS_HNC),
    "Tengzhong Machinery": (CN_EXPO, "EN", "2024", HYPOTHESIS_EXHIBITOR),
    "Tesid Equipment": (CN_EXPO, "EN", "2024", HYPOTHESIS_EXHIBITOR),
    "ТД Ункомтех": (RU_FIRM, "RU", "2024", HYPOTHESIS_BY_VERTICAL["Промдистрибуция"]),
    "JAT Cemented Carbide": (CN_EXPO, "EN", "2024", HYPOTHESIS_EXHIBITOR),
    "Rogen Technologies": (CN_EXPO, "EN", "2024", HYPOTHESIS_EXHIBITOR),
    "Искролайн": (RU_FIRM, "RU", "2024", HYPOTHESIS_EXHIBITOR_BY_VERTICAL[INDUSTRIAL]),
    "Jimmy CNC Tool": (CN_EXPO, "EN", "2024 и 2026", HYPOTHESIS_EXHIBITOR),
    "Ezhong Heavy Machinery": (CN_EXPO, "RU", "2024 и 2026", HYPOTHESIS_EXHIBITOR),
    "Shixinghong Precision": (CN_EXPO, "EN", "2024", HYPOTHESIS_EXHIBITOR),
    "Internor Machinery": (CN_EXPO, "EN", "2024", HYPOTHESIS_EXHIBITOR),
    "РИЦ Техносфера": (PUBLISHER, "RU", "2024", HYPOTHESIS_PUBLISHER),
    "Howfit Science": (CN_EXPO, "RU", "2024", HYPOTHESIS_EXHIBITOR),
    "Fengyi Yinhu": (CN_EXPO, "EN", "2024 и 2026", HYPOTHESIS_EXHIBITOR),
}
THEIR_FIELDS = ["сегмент", "язык_письма", "триггер", "Гипотеза_боли"]
THEIR_TRIGGER = "экспонент Металлообработки-{years}"

_pages = {}
_silent = {}  # url -> why the page gave no text: closed in robots.txt (not requested) or no answer
_live = Counter()  # live checks of this run: "total" and "failed" (the page did not answer)
_unchecked_triggers = []  # triggers of this run whose page did not answer: the run stops, see main()


# ---------- small helpers ----------

def word_count(text):
    """Strict word count: what macOS `wc -w` prints (a lone dash is a word).

    That `wc` also breaks a word at the bytes 0x85 and 0xA0, which sit inside the UTF-8 codes of «х» and «Р»,
    so the number is never smaller than a plain split on spaces. The whole project counts this way
    (see tools/check_chain.py), and the 16-word limit of the hypothesis is checked against it.
    """
    return sum(1 for chunk in WORD_BREAK.split(text.encode("utf-8")) if chunk)


def host(value):
    """Lowercase host of a URL or an email address, without the leading www (a Cyrillic host in its IDNA form)."""
    return host_of(value)


def same_site(a, b):
    """True if two hosts are the same site (equal, or one is a subdomain of the other)."""
    return same_host(a, b)


def months_old(source_date, today):
    """Whole months between «ГГГГ-ММ» and today's month; a bare «ГГГГ» counts from January, the oldest it can be."""
    year, month = DATE_RE.match(source_date).groups()
    return (today.year - int(year)) * 12 + today.month - int(month or 1)


def plain_words(text):
    """Lowercase words of a job title without punctuation."""
    return [w for w in (word.strip(PUNCTUATION) for word in text.lower().split()) if w]


def is_run_of(short, full):
    """True if the words of `short` stand in `full` next to each other and in the same order."""
    short, full = plain_words(short), plain_words(full)
    return bool(short) and any(full[i:i + len(short)] == short for i in range(len(full) - len(short) + 1))


def fetch_page(url):
    """GET a page (20 s limit, two attempts) and return decoded HTML, '' when there is none.

    The site's robots.txt is asked first, and again for every redirect target: a closed page is not
    requested. With POLZA_SOCKS the request goes through that SOCKS proxy; the second attempt resolves
    the name over DNS-over-HTTPS (see build_base_common.curl_command). Why a page gave nothing is kept
    for silent_page().
    """
    if url in _pages:
        return _pages[url]
    text, why = get_page(url, attempts=2, max_time=20, pause=1, accept_short=False)
    if not text:
        _silent[url] = why
    _pages[url] = text
    return text


def live_page(url):
    """fetch_page() for a live check: the check is counted, and so is a page that did not answer.

    A page closed in robots.txt is not a silent one: it was never requested, the network is not to blame.
    """
    page = fetch_page(url)
    _live["total"] += 1
    _live["failed"] += not page and ROBOTS_CLOSED not in _silent.get(url, "")
    return page


def silent_page(url):
    """Why a live check got no page: «страница … закрыта в robots.txt сайта (Disallow: …)» or «… не ответила»."""
    return no_page(url, why=_silent.get(url, ""))


def network_is_down():
    """True if more than half of the live checks of this run got no answer."""
    return _live["failed"] * 2 > _live["total"]


TRANSLIT = dict(zip("абвгдеёзийклмнопрстуфыэ", "abvgdeeziyklmnoprstufye"))
TRANSLIT.update({"ж": "zh", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ю": "yu", "я": "ya",
                 "ь": "", "ъ": ""})


def is_personal_mailbox(email, surname):
    """True if the local part carries the surname (p.obraztsov@ for Образцов), i.e. it is not a role mailbox."""
    latin = "".join(TRANSLIT.get(ch, ch) for ch in surname.lower())
    return len(latin) >= 4 and latin[:4] in email.split("@", 1)[0].lower()


def read_csv(path):
    """Return (rows, fieldnames) of a UTF-8 CSV with or without BOM."""
    reader = csv.DictReader(io.StringIO(path.read_bytes().decode("utf-8-sig"), newline=""))
    return list(reader), list(reader.fieldnames)


def write_csv(path, rows, fields):
    """Write a utf-8-sig CSV with CRLF line ends, atomically."""
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=fields, lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(rows)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(BOM + out.getvalue().encode("utf-8"))
    os.replace(tmp, path)


# ---------- input ----------

def load_lpr():
    """Read lpr/part_*.csv and return {row number: row}."""
    people = {}
    for path in sorted(LPR_DIR.glob("part_*.csv")):
        for row in read_csv(path)[0]:
            number = int(row["row"])
            if number in people:
                raise SystemExit(f"ОШИБКА: строка {number} встречается дважды в lpr/part_*.csv")
            people[number] = row
    return people


def check_person(number, base, lpr, today):
    """Problems in the hand-filled columns of one lpr row (empty list if clean)."""
    problems = []
    company = base["company"]
    if lpr["company"] != company:
        # The same mistake as the traps of Task 4: a shifted row pairs a person with another company.
        return [f"строка {number}: в lpr «{lpr['company']}», в базе «{company}» — строки сдвинуты"]
    if base["имя_ЛПР"] != lpr["имя_ЛПР"]:
        problems.append(f"{company}: в базе ЛПР «{base['имя_ЛПР']}», в lpr «{lpr['имя_ЛПР']}»")
    elif base["должность_ЛПР"].lower() != lpr["должность_ЛПР"].lower():
        problems.append(f"{company}: в базе должность «{base['должность_ЛПР']}», в lpr «{lpr['должность_ЛПР']}»")
    if not lpr["имя_ЛПР"]:
        problems += [f"{company}: ЛПР не назван, но заполнено «{f}»" for f in PERSON_FIELDS if lpr[f]]
        if company in STANDING_PAGE:
            problems.append(f"{company}: ЛПР не назван, а компания стоит в STANDING_PAGE")
    else:
        words = lpr["имя_ЛПР"].split()
        for field in ("Имя", "Фамилия"):
            if not lpr[field]:
                problems.append(f"{company}: пустое «{field}»")
        for field in ("Имя", "Отчество", "Фамилия"):
            if lpr[field] and lpr[field] not in words:
                problems.append(f"{company}: {field} «{lpr[field]}» нет в «{lpr['имя_ЛПР']}»")
        if lpr["Имя"].endswith(("вич", "вна", "ична")):
            problems.append(f"{company}: в «Имя» стоит отчество «{lpr['Имя']}»")
        title = lpr["должность_в_письме"]
        limit = TITLE_WORDS.get(company, MAX_TITLE_WORDS)
        if not title or len(title.split()) > limit:
            problems.append(f"{company}: должность_в_письме «{title}» — нужно 1–{limit} слова")
        if any(w != w.lower() and not w.isupper() for w in title.split()):
            problems.append(f"{company}: должность_в_письме «{title}» — пишется строчными")
        if title and not is_run_of(title, lpr["должность_ЛПР"]):
            # The letter says «На вашем сайте указан …»: the words must be the site's, in the site's order.
            problems.append(f"{company}: должность_в_письме «{title}» — не подряд идущие слова должности с сайта "
                            f"«{lpr['должность_ЛПР']}»")
        if base["компания_в_письме"].lower() in title.lower():
            problems.append(f"{company}: в должность_в_письме попало название компании")
        source = lpr["источник_имени"]
        if not source.startswith(SCHEMES) or not same_site(host(base["site"]), host(source)):
            problems.append(f"{company}: источник имени {source} не на сайте компании {base['site']}")
        if lpr["обращаться_по_имени"] not in (YES, NO):
            problems.append(f"{company}: обращаться_по_имени должно быть «да» или «нет»")
        dated = lpr["дата_источника_имени"]
        age = months_old(dated, today) if DATE_RE.match(dated) else 0
        if dated != NO_DATE and not DATE_RE.match(dated):
            problems.append(f"{company}: дата_источника_имени «{dated}» — нужно ГГГГ-ММ, ГГГГ или «{NO_DATE}»")
        if age >= STALE_MONTHS and lpr["обращаться_по_имени"] == YES:
            problems.append(f"{company}: источник имени от {dated} старше {STALE_MONTHS} месяцев, "
                            f"а обращаться_по_имени = «да»")
        # The rule for a name that cannot be used: an old mention is not stored, unless a standing page backs it.
        if age >= DROP_MONTHS and company not in STANDING_PAGE:
            problems.append(f"{company}: источник имени от {dated} старше {DROP_MONTHS} месяцев — такое имя в базу "
                            f"не идёт (или действующая страница с этим именем должна стоять в STANDING_PAGE)")
        if company in STANDING_PAGE:
            if age < DROP_MONTHS:
                problems.append(f"{company}: стоит в STANDING_PAGE, но источник имени не старше {DROP_MONTHS} месяцев")
            if not same_site(host(base["site"]), host(STANDING_PAGE[company])):
                problems.append(f"{company}: страница {STANDING_PAGE[company]} не на сайте компании {base['site']}")
    if lpr["телефон"] and not PHONE_RE.match(lpr["телефон"]):
        problems.append(f"{company}: телефон «{lpr['телефон']}» не похож на номер")
    return problems


# ---------- checks that need the network ----------

def confirm_lpr_email(base, lpr):
    """Return '' if the person's address is confirmed on the name source page, else the reason.

    The address must be on the page within ROLE_WINDOW characters of the surname (a mailto link
    counts where it stands). Its domain is the site's own, or one the same page uses for another
    address too: a few companies keep their mail on a second domain and print it themselves.
    """
    email, source = lpr["email_ЛПР_на_странице"], lpr["источник_имени"]
    if not EMAIL_RE.match(email):
        return "адрес записан с ошибкой"
    page = live_page(source)
    if not page:
        return silent_page(source)
    if not email_on_page(email, page):
        return f"адреса нет на странице {source}"
    if not same_site(host(email), host(base["site"])) and not site_uses_mail_domain(email, page):
        return f"домен адреса {host(email)} не совпадает с сайтом компании"
    if name_distance(email, lpr["Фамилия"], page) is None:
        return f"фамилия стоит дальше {ROLE_WINDOW} знаков от адреса на странице {source}"
    return ""


def confirm_standing_page(company, lpr):
    """Return '' if the standing page still names the person (the surname is in its visible text), else the reason."""
    url = STANDING_PAGE[company]
    page = live_page(url)
    if not page:
        return silent_page(url)
    if lpr["Фамилия"].lower() not in visible_text(page).lower():
        return f"на странице {url} больше нет фамилии «{lpr['Фамилия']}»"
    return ""


def trigger_hypothesis(trigger, vacancy, personalisation):
    """Hypothesis of a confirmed hiring/dealer trigger: the fact and the guess, or the guess alone."""
    fact, guess, stems = HYPOTHESIS_BY_TRIGGER[trigger]
    if any(stem in personalisation.lower() for stem in stems):
        return guess[0].upper() + guess[1:]
    return fact.format(vacancy=vacancy) + guess


def confirm_trigger(base, trigger, evidence=""):
    """Return (vacancy title or '', reason): reason is '' if the page of the trigger confirms it today.

    A vacancy is confirmed by its title, a dealer programme by the word «дилер», an exhibition or an expansion
    by `evidence` — the phrase of the row — on the page the fact was taken from.
    """
    company = base["company"]
    vacancy = ""
    if trigger in PAGE_TRIGGERS:
        url, needle = base["Источник"], evidence
    else:
        url = TRIGGER_PAGE.get(company) or base["sales_signal"].rsplit(" — ", 1)[-1].strip()
        needle = DEALER_WORD
    if trigger == HIRING:
        found = VACANCY_RE.search(base["sales_signal"])
        vacancy = VACANCY_TITLE.get(company) or (found.group(1) if found else "")
        if not vacancy:
            return "", "в sales_signal нет названия вакансии, и его нет в VACANCY_TITLE"
        needle = vacancy
    page = live_page(url)
    if not page:
        return vacancy, silent_page(url)
    if needle.lower() not in visible_text(page).lower():
        return vacancy, f"на странице {url} больше нет «{needle}»"
    return vacancy, ""


def stale_triggers(today):
    """Problems of TRIGGERS against TRIGGER_EVENT: an exhibition or an expansion with no month or an old one."""
    problems = []
    for company, (trigger, _) in TRIGGERS.items():
        if trigger not in PAGE_TRIGGERS:
            continue
        month = TRIGGER_EVENT.get(company, "")
        if not MONTH_RE.match(month):
            problems.append(f"{company}: у триггера «{trigger}» нет месяца события в TRIGGER_EVENT (ГГГГ-ММ)")
        elif months_old(month, today) > TRIGGER_MONTHS:
            problems.append(f"{company}: событие {month} старше {TRIGGER_MONTHS} месяцев — это уже не триггер "
                            f"«{trigger}», уберите строку из TRIGGERS")
    return problems


def priority_of(lead_type, trigger, segment, named, by_name):
    """Sending priority A / B / C of a row (see the module docstring)."""
    event = trigger != NO_EVENT
    if lead_type:
        if event and segment == SALES:
            return "A"
        return "B" if event or lead_type == NAMED_BOX else "C"
    if event and segment == SALES and by_name == YES:
        return "A"
    return "B" if event or named else "C"


# ---------- one row ----------

def lpr_segment(company, title):
    """Segment of the decision maker by the keywords of the job title."""
    if not title:
        return NO_PERSON
    if company in LPR_SEGMENT_BY_COMPANY:
        return LPR_SEGMENT_BY_COMPANY[company]
    low = title.lower()
    for segment, keywords in LPR_SEGMENT_KEYWORDS:
        if any(k in low for k in keywords):
            return segment
    raise SystemExit(f"ОШИБКА: {company}: должность «{title}» не попала ни в один сегмент ЛПР")


def enrich_row(base, lpr, today, warnings, errors=None):
    """Build one output row from a base row and its lpr row.

    `errors` collects what must stop the run: a lead whose page no longer prints the address.
    """
    company = base["company"]
    notes = []
    errors = [] if errors is None else errors
    lead_type = base.get("тип_адреса", "")  # set only in the lead base: the row's address is the person's own
    if lead_type and lead_type not in LEAD_BOX:
        raise SystemExit(f"ОШИБКА: {company}: тип_адреса «{lead_type}» — нужно одно из: {', '.join(LEAD_BOX)}")

    # An old dated mention is kept for reference only while a standing page of the site still names the person.
    if lpr["имя_ЛПР"] and company in STANDING_PAGE:
        reason = confirm_standing_page(company, lpr)
        if reason:
            warnings.append(f"{company}: старое упоминание ЛПР не подтверждено ({reason}) — имя в базу не идёт")
            notes.append(f"имя ЛПР не подтверждено {today:%d.%m.%Y}: {reason}")
            lpr = {**lpr, **dict.fromkeys(PERSON_FIELDS, "")}
    named = bool(lpr["имя_ЛПР"])
    by_name = YES if lpr["обращаться_по_имени"] == YES else NO

    # Contact level: А — the address is printed next to the person, Б — the person is named, В — nobody is named.
    lpr_email, validation = "", VALIDATION
    if lead_type and (not named or lpr["email_ЛПР_на_странице"].lower() != base["email"].lower()):
        raise SystemExit(f"ОШИБКА: {company}: в базе лидов адрес строки должен совпадать с адресом ЛПР в lpr/")
    if named and lpr["email_ЛПР_на_странице"]:
        reason = confirm_lpr_email(base, lpr)
        if not reason:
            lpr_email = lpr["email_ЛПР_на_странице"]
        elif lead_type and NO_ANSWER in reason:
            # The page is silent today: nothing is downgraded, the row says that it was not re-checked.
            lpr_email, validation = lpr["email_ЛПР_на_странице"], VALIDATION_NOT_RECHECKED
            warnings.append(f"{company}: адрес ЛПР сегодня не перепроверен ({reason}) — "
                            f"оставлен по проверке при сборке базы")
            notes.append(f"адрес ЛПР не перепроверен {today:%d.%m.%Y}: {reason}")
        elif lead_type:
            # The row has no department mailbox to fall back to: the lead is gone, the base must be rebuilt.
            lpr_email = lpr["email_ЛПР_на_странице"]
            errors.append(f"{company}: лид больше не подтверждается ({reason}) — "
                          f"пересоберите базу: tools/build_base_all.py")
        else:
            warnings.append(f"{company}: адрес ЛПР не подтверждён ({reason}) — письмо пойдёт на ящик отдела")
            notes.append(f"адрес ЛПР не подтверждён {today:%d.%m.%Y}: {reason}")
    level = "А" if lpr_email else "Б" if named else "В"
    variant = {"А": "в", "Б": "б"}[level] if named and by_name == YES else "а"
    email = lpr_email or base["email"]
    if lead_type:
        address_type = LEAD_BOX[lead_type]
    elif not lpr_email:
        address_type = "общий" if base["contact_role"].startswith(GENERIC_ROLE_PREFIX) else "отдел продаж"
    elif is_personal_mailbox(lpr_email, lpr["Фамилия"]):
        address_type = PERSONAL_BOX
    else:
        # The card repeats the mailbox the base already holds for the company, or prints one of the person's service.
        address_type = COMPANY_BOX if lpr_email.lower() == base["email"].lower() else SERVICE_BOX

    # Segments, trigger, hypothesis.
    vertical = base["segment"].split(":", 1)[0].strip()
    if vertical not in HYPOTHESIS_BY_VERTICAL:
        raise SystemExit(f"ОШИБКА: {company}: вертикали «{vertical}» нет в словаре гипотез")
    if base["city"] not in CITY:
        raise SystemExit(f"ОШИБКА: {company}: города «{base['city']}» нет в словаре CITY")
    city, region, zone, city_note = CITY[base["city"]]
    trigger, evidence = TRIGGERS.get(company, (NO_EVENT, ""))
    if evidence and evidence.lower() not in (base["sales_signal"] + " " + base["Персонализация"]).lower():
        raise SystemExit(f"ОШИБКА: {company}: в сигнале и персонализации нет «{evidence}» — основание триггера пропало")
    hypothesis = HYPOTHESIS_BY_VERTICAL[HYPOTHESIS_VERTICAL_BY_COMPANY.get(company, vertical)]
    if company in HYPOTHESIS_BY_COMPANY:
        if RECOMMENDATION_STEM not in base["Персонализация"].lower():
            raise SystemExit(f"ОШИБКА: {company}: в персонализации больше нет «{RECOMMENDATION_STEM}» — "
                             f"строка в HYPOTHESIS_BY_COMPANY не нужна")
        hypothesis = HYPOTHESIS_BY_COMPANY[company]
    if trigger != NO_EVENT:
        vacancy, reason = confirm_trigger(base, trigger, evidence)
        if reason and NO_ANSWER in reason and not reason.endswith(PAGE_GONE):
            # Not kept unchecked and not dropped quietly: a silent page is the network, the run stops in main().
            _unchecked_triggers.append(f"{company}: триггер «{trigger}» — {reason}")
        elif reason:
            warnings.append(f"{company}: триггер «{trigger}» не подтверждён ({reason}) — взята гипотеза по вертикали")
            notes.append(f"триггер «{trigger}» не подтверждён {today:%d.%m.%Y}: {reason}")
            trigger = NO_EVENT
        elif trigger in (HIRING, DEALERS):
            hypothesis = trigger_hypothesis(trigger, vacancy, base["Персонализация"])
        elif trigger == EXPO:
            hypothesis = HYPOTHESIS_EXHIBITOR_BY_VERTICAL.get(vertical, hypothesis)
    segment = lpr_segment(company, lpr["должность_ЛПР"])
    priority = priority_of(lead_type, trigger, segment, named, by_name)

    notes = [n for n in (city_note, lpr["примечание_контакта"], NOTE_BY_COMPANY.get(company, "")) if n] + notes
    return {
        # The title is spelled as in task2_personalized.csv (check_person() makes sure it is the same title).
        "Имя": lpr["Имя"], "Фамилия": lpr["Фамилия"], "Должность": base["должность_ЛПР"] if named else "",
        "Email": email, "Телефон": lpr["телефон"], "Компания": company,
        "Отчество": lpr["Отчество"], "компания_в_письме": base["компания_в_письме"], "site": base["site"],
        "уровень_контакта": level, "вариант_письма_1": variant, "обращаться_по_имени": by_name,
        "должность_в_письме": lpr["должность_в_письме"], "сегмент_ЛПР": segment,
        "источник_имени": lpr["источник_имени"], "дата_источника_имени": lpr["дата_источника_имени"],
        # The lead base has no department mailbox: the row's only address is the person's own.
        "email_отдела": "" if lead_type else base["email"],
        "contact_role": base["contact_role"], "email_source": base["email_source"],
        "тип_адреса": address_type, "валидация": validation, "дата_проверки": today.isoformat(),
        "вертикаль": vertical, "segment": base["segment"], "sales_signal": base["sales_signal"],
        "тип_триггера": trigger, "приоритет": priority,
        "город": city, "регион_группа": region, "часовой_пояс": zone,
        "Персонализация": base["Персонализация"], "Источник": base["Источник"], "Гипотеза_боли": hypothesis,
        "Проверка_соответствия": base["Проверка_соответствия"], "Комментарий": base["Комментарий"],
        "примечание": " · ".join(notes),
    }


# ---------- Task 4: four columns of the organisers' base ----------

def enrich_their_base(write):
    """Fill сегмент / язык_письма / триггер / Гипотеза_боли in task4_final.csv; return (rows, problems)."""
    rows, fields = read_csv(THEIR_BASE)
    problems = [f"task4_final.csv: компании «{r['company']}» нет в словаре THEIR" for r in rows
                if r["company"] not in THEIR]
    if problems:
        return rows, problems
    for row in rows:
        segment, language, years, hypothesis = THEIR[row["company"]]
        row.update({"сегмент": segment, "язык_письма": language,
                    "триггер": THEIR_TRIGGER.format(years=years), "Гипотеза_боли": hypothesis})
    if write:
        write_csv(THEIR_BASE, rows, [f for f in fields if f not in THEIR_FIELDS] + THEIR_FIELDS)
    return rows, problems


# ---------- report ----------

def tally(rows, field, order=None):
    """«value N · value N» for one column, in `order` if given, else by first appearance."""
    counts = Counter(r[field] for r in rows)
    keys = [k for k in (order or counts) if k in counts]
    return " · ".join(f"{k} {counts[k]}" for k in keys)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    check_only = "--check" in argv
    out = Path(argv[argv.index("--out") + 1]) if "--out" in argv else OUT
    today = date.today()
    _unchecked_triggers.clear()

    people = load_lpr()
    if not people:
        # A clone of the public repository before tools/build_base_all.py has run: nothing to rebuild from.
        print(NO_LPR_FILES)
        return 2
    base_rows, _ = read_csv(BASE)
    problems = []
    if sorted(people) != list(range(1, len(base_rows) + 1)):
        problems.append(f"в lpr/part_*.csv номера строк {min(people)}–{max(people)} ({len(people)} шт.), "
                        f"а в базе {len(base_rows)} строк")
    else:
        for number, base in enumerate(base_rows, 1):
            problems += check_person(number, base, people[number], today)
    dictionaries = (TRIGGERS, VACANCY_TITLE, TRIGGER_PAGE, LPR_SEGMENT_BY_COMPANY, STANDING_PAGE, TITLE_WORDS,
                    HYPOTHESIS_VERTICAL_BY_COMPANY, HYPOTHESIS_BY_COMPANY, NOTE_BY_COMPANY)
    unknown = sorted({c for d in dictionaries for c in d} - {r["company"] for r in base_rows})
    problems += [f"в словарях скрипта есть компания «{c}», которой нет в базе" for c in unknown]
    problems += stale_triggers(today)
    if problems:
        print("\n".join("ОШИБКА  " + p for p in problems))
        return 1

    warnings = []
    rows = [enrich_row(base, people[n], today, warnings, problems) for n, base in enumerate(base_rows, 1)]

    # No answer from more than half of the pages is a dead network, not a change on the sites:
    # nothing is downgraded and nothing is written.
    if network_is_down():
        print(f"СТОП: не ответили страницы в {_live['failed']} проверках из {_live['total']} — больше половины. "
              f"Похоже, нет сети или прокси (POLZA_SOCKS). {out.name} не изменён, ничего не записано.")
        return EXIT_NETWORK_DOWN
    if _unchecked_triggers:
        # A trigger moves a row between the waves, so it is neither kept unchecked nor dropped because of the network.
        print("\n".join("СТОП: " + line for line in _unchecked_triggers))
        print(f"Страница триггера не ответила. {out.name} не изменён, ничего не записано: запустите ещё раз; если "
              f"страница пропала насовсем, уберите компанию из TRIGGERS.")
        return EXIT_NETWORK_DOWN
    if problems:
        print("\n".join("ОШИБКА  " + p for p in problems))
        return 1

    # Syntax and MX for every address the letter may go to; duplicates; hypothesis length.
    mx = {}
    for row in rows:
        for email in {row["Email"], row["email_отдела"]} - {""}:
            if not EMAIL_RE.match(email):
                problems.append(f"{row['Компания']}: адрес «{email}» записан с ошибкой")
                continue
            domain = host(email)
            if domain not in mx:
                mx[domain] = has_mx(domain)[0]
            if not mx[domain]:
                problems.append(f"{row['Компания']}: у домена {domain} нет MX")
    seen = {}
    for row in rows:
        if row["Email"].lower() in seen:
            problems.append(f"один и тот же Email у «{seen[row['Email'].lower()]}» и «{row['Компания']}»")
        seen[row["Email"].lower()] = row["Компания"]
    their_rows, their_problems = enrich_their_base(write=False)
    problems += their_problems
    for row in rows + ([] if their_problems else their_rows):
        if not 0 < word_count(row["Гипотеза_боли"]) <= MAX_HYPOTHESIS_WORDS:
            problems.append(f"{row.get('Компания') or row['company']}: гипотеза длиннее {MAX_HYPOTHESIS_WORDS} слов "
                            f"или пустая: «{row['Гипотеза_боли']}»")
    if problems:
        print("\n".join("ОШИБКА  " + p for p in problems))
        return 1

    if not check_only:
        write_csv(out, rows, OUT_FIELDS)
        enrich_their_base(write=True)

    for w in warnings:
        print("ВНИМАНИЕ", w)
    named = [r for r in rows if r["Имя"]]
    in_card = Counter(r["тип_адреса"] for r in rows if r["уровень_контакта"] == "А")
    print(f"{len(rows)} строк -> {out.name}" + (" (проверка, файл не записан)" if check_only else ""))
    print(f"ЛПР назван на сайте компании: {len(named)} из {len(rows)}, "
          f"из них имя только для справки (обращаться_по_имени = нет): "
          f"{sum(1 for r in named if r['обращаться_по_имени'] == NO)}; "
          f"телефон отдела: {sum(1 for r in rows if r['Телефон'])}")
    print(f"адрес напечатан в карточке ЛПР: {sum(in_card.values())}, из них именных — {in_card[PERSONAL_BOX]}, "
          f"ящик должности ЛПР — {in_card[SERVICE_BOX]}, общий ящик компании — {in_card[COMPANY_BOX]}")
    print("уровень контакта:", tally(rows, "уровень_контакта", "АБВ"))
    print("вариант письма 1:", tally(rows, "вариант_письма_1", "абв"))
    print("приоритет:", tally(rows, "приоритет", "ABC"))
    print("сегмент ЛПР:", tally(rows, "сегмент_ЛПР", [SALES, CHIEF, GROWTH_ROLE, MARKETING, NO_PERSON]))
    print("тип адреса:", tally(rows, "тип_адреса", [PERSONAL_BOX, SERVICE_BOX, FREE_PERSONAL_BOX, COMPANY_BOX,
                                                    "отдел продаж", "общий"]))
    print("триггеры:", tally(rows, "тип_триггера", [HIRING, DEALERS, GROWTH, EXPO, NO_EVENT]))
    print("вертикали:", tally(rows, "вертикаль", HYPOTHESIS_BY_VERTICAL))
    print("регионы:", tally(rows, "регион_группа", [MSK_MO, SPB, REGIONS]))
    print("часовые пояса:", tally(rows, "часовой_пояс", ZONES))
    region_only = [r for r in rows if r["город"].endswith("обл.")]
    print(f"города: {len({r['город'] for r in rows if r not in region_only})} "
          f"(ещё у {len(region_only)} компаний указана только область); "
          f"домены с MX: {sum(mx.values())} из {len(mx)}; "
          f"гипотеза боли: {sum(1 for r in rows if r['Гипотеза_боли'])} из {len(rows)}, "
          f"самая длинная — {max(word_count(r['Гипотеза_боли']) for r in rows)} слов")
    print(f"task4_final.csv: {len(their_rows)} строк; сегменты: {tally(their_rows, 'сегмент')}; "
          f"язык письма: {tally(their_rows, 'язык_письма', ['RU', 'EN'])}; гипотеза боли: "
          f"{sum(1 for r in their_rows if r['Гипотеза_боли'])} из {len(their_rows)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
