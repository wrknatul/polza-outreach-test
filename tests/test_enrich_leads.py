"""tools/enrich_base.py on the lead base: a silent page never changes a contact quietly.

Made-up companies; pages and MX answers are stand-ins, nothing touches the network.
"""

from __future__ import annotations

import csv
import sys
from collections import Counter
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import build_base_all as B
import build_base_common as C
import enrich_base as E

TODAY = date(2026, 10, 3)
NAMES = ["alfa", "beta", "gamma", "delta"]


def lead(slug: str) -> dict:
    site = f"https://{slug}.example"
    return {
        "company": slug.title(), "компания_в_письме": slug.title(), "site": site, "city": "Москва",
        "segment": "Промоборудование: станки", "sales_signal": "«Отдел продаж»", "sales_signal_url": f"{site}/",
        "signal_check": "Отдел продаж", "имя_ЛПР": "Иван Петров", "должность_ЛПР": "Коммерческий директор",
        "Имя": "Иван", "Отчество": "", "Фамилия": "Петров", "должность_в_письме": "коммерческий директор",
        "email": f"petrov@{slug}.example", "тип_адреса": C.NAMED_BOX, "источник": f"{site}/team/",
        "дата_страницы": "на странице не указана", "дата_источника_имени": E.NO_DATE, "обращаться_по_имени": E.YES,
        "телефон": "+7 (495) 000-00-00", "оговорка": "", "batch": "test",
    }


def card(slug: str) -> str:
    return f"<div>Коммерческий директор Иван Петров <a href='mailto:petrov@{slug}.example'>Написать</a></div>"


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A four-lead base laid out the way build_base_all.py writes it, plus the personalisation columns."""
    leads = [lead(slug) for slug in NAMES]
    base = []
    for row in leads:
        base.append({**C.lead_to_base_row(row), "Персонализация": "Увидели, что вы поставляете станки.",
                     "Источник": row["site"], "Проверка_соответствия": "OK", "Комментарий": ""})
    personalised = tmp_path / "task2_personalized.csv"
    C.write_csv(base, personalised, list(base[0]))
    (tmp_path / "lpr").mkdir()
    C.write_csv([B.lpr_row(n, row) for n, row in enumerate(leads, 1)], tmp_path / "lpr" / "part_1.csv", B.LPR_FIELDS)
    out = tmp_path / "task1_2_enriched.csv"
    out.write_text("прежний результат", encoding="utf-8")

    monkeypatch.setattr(E, "BASE", personalised)
    monkeypatch.setattr(E, "LPR_DIR", tmp_path / "lpr")
    monkeypatch.setattr(E, "OUT", out)
    for name in ("TRIGGERS", "VACANCY_TITLE", "TRIGGER_PAGE", "LPR_SEGMENT_BY_COMPANY", "STANDING_PAGE", "TITLE_WORDS",
                 "HYPOTHESIS_VERTICAL_BY_COMPANY", "HYPOTHESIS_BY_COMPANY", "NOTE_BY_COMPANY"):
        monkeypatch.setattr(E, name, {})
    monkeypatch.setattr(E, "_live", Counter())
    monkeypatch.setattr(E, "_pages", {})
    monkeypatch.setattr(E, "_silent", {})
    monkeypatch.setattr(E, "has_mx", lambda domain: (True, ["10 mx."]))
    monkeypatch.setattr(E, "enrich_their_base", lambda write: ([], []))
    return out


def pages(monkeypatch, silent=(), changed=()):
    def fetch_page(url):
        slug = url.split("//")[1].split(".")[0]
        if slug in silent:
            return ""
        return "<div>Иван Петров, коммерческий директор</div>" if slug in changed else card(slug)
    monkeypatch.setattr(E, "fetch_page", fetch_page)


def read(out: Path) -> list[dict]:
    with out.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def test_all_pages_answer(project, monkeypatch, capsys):
    pages(monkeypatch)
    assert E.main([]) == 0
    rows = read(project)
    assert [r["Email"] for r in rows] == [f"petrov@{slug}.example" for slug in NAMES]
    assert {r["уровень_контакта"] for r in rows} == {"А"} and {r["вариант_письма_1"] for r in rows} == {"в"}
    assert {r["тип_адреса"] for r in rows} == {E.PERSONAL_BOX} and {r["валидация"] for r in rows} == {E.VALIDATION}
    assert {r["email_отдела"] for r in rows} == {""}        # a lead row has no department mailbox behind it
    assert "ВНИМАНИЕ" not in capsys.readouterr().out


@pytest.mark.parametrize("argv", [[], ["--check"]])
def test_more_than_half_silent_pages_abort_without_writing(argv, project, monkeypatch, capsys):
    pages(monkeypatch, silent=("alfa", "beta", "gamma"))
    assert E.main(argv) == E.EXIT_NETWORK_DOWN == 3
    printed = capsys.readouterr().out
    assert "СТОП" in printed and "3 проверках из 4" in printed and "POLZA_SOCKS" in printed
    assert project.read_text(encoding="utf-8") == "прежний результат"      # the old result is not overwritten


def test_a_single_silent_page_keeps_the_contact_and_says_so(project, monkeypatch, capsys):
    pages(monkeypatch, silent=("beta",))
    assert E.main([]) == 0
    beta = read(project)[1]
    assert beta["Email"] == "petrov@beta.example" and beta["уровень_контакта"] == "А"   # nothing is downgraded
    assert beta["тип_адреса"] == E.PERSONAL_BOX and beta["валидация"] == E.VALIDATION_NOT_RECHECKED
    assert "не перепроверен" in beta["примечание"] and "не ответила" in beta["примечание"]
    assert "ВНИМАНИЕ Beta: адрес ЛПР сегодня не перепроверен" in capsys.readouterr().out


def test_exactly_half_silent_is_not_an_abort(project, monkeypatch):
    pages(monkeypatch, silent=("alfa", "beta"))
    assert E.main(["--check"]) == 0


def test_a_lead_whose_page_lost_the_address_stops_the_run(project, monkeypatch, capsys):
    pages(monkeypatch, changed=("gamma",))
    assert E.main([]) == 1
    printed = capsys.readouterr().out
    assert "ОШИБКА  Gamma: лид больше не подтверждается" in printed and "build_base_all.py" in printed
    assert project.read_text(encoding="utf-8") == "прежний результат"


def test_role_mailbox_keeps_its_label_and_second_mail_domain_is_accepted(monkeypatch):
    monkeypatch.setattr(E, "_live", Counter())
    row = lead("alfa")
    row.update({"email": "kd@alfa-mail.example", "тип_адреса": C.ROLE_BOX})
    base = {**C.lead_to_base_row(row), "Персонализация": "Увидели, что вы поставляете станки.", "Источник": "",
            "Проверка_соответствия": "OK", "Комментарий": ""}
    page = ("<div>Коммерческий директор Иван Петров kd@alfa-mail.example</div>"
            "<footer>Приёмная: priemnaya@alfa-mail.example</footer>")
    monkeypatch.setattr(E, "fetch_page", lambda url: page)
    warnings, errors = [], []
    out = E.enrich_row(base, B.lpr_row(1, row), TODAY, warnings, errors)
    assert (warnings, errors) == ([], [])
    assert out["Email"] == "kd@alfa-mail.example" and out["тип_адреса"] == E.SERVICE_BOX
    assert out["уровень_контакта"] == "А"


def test_http_only_site_is_a_valid_name_source():
    row = lead("alfa")
    row.update({"site": "http://alfa.example", "источник": "http://alfa.example/team/"})
    base = {**C.lead_to_base_row(row)}
    assert E.check_person(1, base, B.lpr_row(1, row), TODAY) == []


def test_priority_of_a_lead():
    # A — a confirmed trigger and a decision maker from sales; B — a trigger or a personal mailbox;
    # C — a role mailbox with no trigger: somebody else may read it.
    assert E.priority_of(C.NAMED_BOX, E.EXPO, E.SALES, True, E.YES) == "A"
    assert E.priority_of(C.ROLE_BOX, E.HIRING, E.SALES, True, E.YES) == "A"
    assert E.priority_of(C.NAMED_BOX, E.HIRING, E.CHIEF, True, E.YES) == "B"
    assert E.priority_of(C.NAMED_BOX, E.NO_EVENT, E.SALES, True, E.YES) == "B"
    assert E.priority_of(C.ROLE_BOX, E.GROWTH, E.CHIEF, True, E.YES) == "B"
    assert E.priority_of(C.ROLE_BOX, E.NO_EVENT, E.SALES, True, E.YES) == "C"
    # a row of the old base (a department mailbox): the old rule
    assert E.priority_of("", E.DEALERS, E.SALES, True, E.YES) == "A"
    assert E.priority_of("", E.DEALERS, E.SALES, True, E.NO) == "B"
    assert E.priority_of("", E.NO_EVENT, E.CHIEF, True, E.YES) == "B"
    assert E.priority_of("", E.NO_EVENT, E.NO_PERSON, False, E.NO) == "C"


def trigger_row(slug: str, personalisation: str) -> tuple[dict, dict]:
    row = lead(slug)
    base = {**C.lead_to_base_row(row), "Персонализация": personalisation, "Источник": f"https://{slug}.example/news/1/",
            "Проверка_соответствия": "OK", "Комментарий": ""}
    return base, B.lpr_row(1, row)


def test_exhibition_trigger_is_confirmed_on_the_page_of_the_fact(monkeypatch):
    base, lpr = trigger_row("alfa", "Увидели, что в сентябре 2026 вы показали станок на «ТЕХНОФОРУМ».")
    monkeypatch.setattr(E, "_live", Counter())
    monkeypatch.setattr(E, "TRIGGERS", {"Alfa": (E.EXPO, "ТЕХНОФОРУМ")})
    news = "<div>Итоги выставки «Технофорум»: три дня на стенде</div>"
    monkeypatch.setattr(E, "fetch_page", lambda url: news if "/news/" in url else card("alfa"))
    warnings = []
    out = E.enrich_row(base, lpr, TODAY, warnings, [])
    assert warnings == [] and out["тип_триггера"] == E.EXPO and out["приоритет"] == "A"
    assert out["Гипотеза_боли"] == E.HYPOTHESIS_EXHIBITOR_BY_VERTICAL[E.INDUSTRIAL]

    # the page no longer names the exhibition: the trigger is dropped and the row says why
    monkeypatch.setattr(E, "fetch_page", lambda url: "<div>Новости завода</div>" if "/news/" in url else card("alfa"))
    warnings = []
    out = E.enrich_row(base, lpr, TODAY, warnings, [])
    assert out["тип_триггера"] == E.NO_EVENT and out["приоритет"] == "B"
    assert out["Гипотеза_боли"] == E.HYPOTHESIS_BY_VERTICAL[E.INDUSTRIAL]
    assert len(warnings) == 1 and "больше нет «ТЕХНОФОРУМ»" in warnings[0] and "не подтверждён" in out["примечание"]


def test_vacancy_told_in_the_personalisation_is_checked_by_its_title(monkeypatch):
    base, lpr = trigger_row("beta", "Увидели на сайте, что вы ищете менеджера по продажам — заключать договоры.")
    monkeypatch.setattr(E, "_live", Counter())
    monkeypatch.setattr(E, "TRIGGERS", {"Beta": (E.HIRING, "менеджера по продажам")})
    monkeypatch.setattr(E, "VACANCY_TITLE", {"Beta": "Менеджер по продажам"})
    monkeypatch.setattr(E, "TRIGGER_PAGE", {"Beta": "https://beta.example/vacancy/"})
    pages = {"https://beta.example/vacancy/": "<h2>Вакансии</h2><p>Менеджер по продажам</p>"}
    monkeypatch.setattr(E, "fetch_page", lambda url: pages.get(url, card("beta")))
    warnings = []
    out = E.enrich_row(base, lpr, TODAY, warnings, [])
    assert warnings == [] and out["тип_триггера"] == E.HIRING
    # the personalisation already tells about the vacancy, so the hypothesis is the guess alone
    assert out["Гипотеза_боли"] == "Предполагаем, что новому сотруднику сразу понадобятся встречи."


def test_a_silent_trigger_page_stops_the_run_and_a_deleted_one_drops_the_trigger(project, monkeypatch, capsys):
    # A trigger moves a row between the waves, so the network must neither keep it unchecked nor take it away.
    monkeypatch.setattr(E, "TRIGGERS", {"Alfa": (E.EXPO, "станки")})
    monkeypatch.setattr(E, "TRIGGER_EVENT", {"Alfa": "2099-01"})   # ahead of any day the test runs on
    fact_page = "https://alfa.example"                             # `Источник` of the row; the card is on /team/

    def answers(why):
        def fetch_page(url):
            if url == fact_page:
                E._silent[url] = why
                return ""
            return card(url.split("//")[1].split(".")[0])
        monkeypatch.setattr(E, "fetch_page", fetch_page)

    answers("curl: код 28")                                        # a timeout
    assert E.main([]) == E.EXIT_NETWORK_DOWN
    printed = capsys.readouterr().out
    assert "СТОП: Alfa: триггер «выставка»" in printed and "не ответила" in printed and "TRIGGERS" in printed
    assert project.read_text(encoding="utf-8") == "прежний результат"

    answers("HTTP 404")                                            # the site answered: the page is gone
    assert E.main([]) == 0
    alfa = read(project)[0]
    assert alfa["тип_триггера"] == E.NO_EVENT and "не подтверждён" in alfa["примечание"]
    assert "ВНИМАНИЕ Alfa: триггер «выставка» не подтверждён" in capsys.readouterr().out


def test_hypothesis_of_a_company_needs_its_reason(monkeypatch):
    base, lpr = trigger_row("gamma", "Увидели на сайте, что половину клиентов вы получаете по рекомендациям.")
    monkeypatch.setattr(E, "_live", Counter())
    monkeypatch.setattr(E, "fetch_page", lambda url: card("gamma"))
    own = "Предполагаем, что вторую половину клиентов приходится искать самим."
    monkeypatch.setattr(E, "HYPOTHESIS_BY_COMPANY", {"Gamma": own})
    assert E.enrich_row(base, lpr, TODAY, [], [])["Гипотеза_боли"] == own

    base["Персонализация"] = "Увидели, что вы поставляете станки."
    with pytest.raises(SystemExit, match="HYPOTHESIS_BY_COMPANY"):
        E.enrich_row(base, lpr, TODAY, [], [])


def robots_web(monkeypatch, robots: str) -> list[str]:
    """Replace curl for the made-up sites: robots.txt answers `robots`, every page is the person's card."""
    asked: list[str] = []

    def run(cmd, **kw):
        url = cmd[-1]
        asked.append(url)
        slug = url.split("//")[1].split(".")[0]
        body = robots.encode() if url.endswith("/robots.txt") else (card(slug) + "<p>Отдел продаж</p>" * 60).encode()
        return C.subprocess.CompletedProcess(cmd, 0, body + b"\n200 ", b"")

    monkeypatch.setattr(C.subprocess, "run", run)
    monkeypatch.setattr(C, "_robots", {})
    return asked


def test_page_closed_in_robots_txt_is_not_requested_and_the_lead_stops_the_run(project, monkeypatch, capsys):
    asked = robots_web(monkeypatch, "User-agent: *\nDisallow: /team/\n")
    assert E.main([]) == 1                                   # not 3: the network answered, the site closed the page
    printed = capsys.readouterr().out
    assert "ОШИБКА  Alfa: лид больше не подтверждается" in printed
    assert "закрыта в robots.txt сайта (Disallow: /team/)" in printed and "build_base_all.py" in printed
    assert not any(url.endswith("/team/") for url in asked)
    assert sorted(asked) == sorted(f"https://{slug}.example/robots.txt" for slug in NAMES)   # once per host
    assert project.read_text(encoding="utf-8") == "прежний результат"


def test_open_pages_are_read_after_robots_txt(project, monkeypatch):
    asked = robots_web(monkeypatch, "User-agent: *\nDisallow: /admin/\nAllow: /team/\n")
    assert E.main([]) == 0
    assert asked[:2] == ["https://alfa.example/robots.txt", "https://alfa.example/team/"]
    assert {r["валидация"] for r in read(project)} == {E.VALIDATION}
    assert not any("robots.txt" in r["примечание"] for r in read(project))
