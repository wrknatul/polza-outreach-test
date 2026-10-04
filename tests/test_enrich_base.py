"""Base enrichment (tools/enrich_base.py): the rules that need no network.

A made-up company is used throughout; no page is fetched and no DNS query is made.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import enrich_base as E

TODAY = date(2026, 10, 3)


def make_rows(**lpr_overrides) -> tuple[dict, dict]:
    """A base row and its research row for a named decision maker of a made-up company."""
    base = {"company": "Акме Станки", "компания_в_письме": "Акме Станки", "site": "https://acme-stanki.ru",
            "имя_ЛПР": "Иван Петров", "должность_ЛПР": "Коммерческий директор"}
    lpr = {"company": "Акме Станки", "имя_ЛПР": "Иван Петров", "должность_ЛПР": "Коммерческий директор",
           "источник_имени": "https://acme-stanki.ru/news/itogi/", "Имя": "Иван", "Отчество": "", "Фамилия": "Петров",
           "должность_в_письме": "коммерческий директор", "email_ЛПР_на_странице": "",
           "дата_источника_имени": "2026-06", "обращаться_по_имени": E.YES, "телефон": "+7 (495) 000-00-00",
           "примечание_контакта": ""}
    return base, {**lpr, **lpr_overrides}


def forbid_network(monkeypatch) -> None:
    """Any page fetch or MX lookup fails the test."""
    def fail(*args, **kwargs):
        raise AssertionError("the network must not be touched")
    monkeypatch.setattr(E, "fetch_page", fail)
    monkeypatch.setattr(E, "has_mx", fail)


@pytest.mark.parametrize("argv", [[], ["--check"]])
def test_clean_clone_without_research_files_exits_with_code_2(argv, tmp_path, monkeypatch, capsys):
    # The public repository has no lpr/: the script explains it in one line instead of a traceback.
    out = tmp_path / "enriched.csv"
    monkeypatch.setattr(E, "LPR_DIR", tmp_path / "lpr")
    monkeypatch.setattr(E, "OUT", out)
    forbid_network(monkeypatch)

    assert E.main(argv) == 2

    printed = capsys.readouterr().out.strip().splitlines()
    assert printed == [E.NO_LPR_FILES]
    assert "lpr/part_*.csv" in printed[0] and "task1_2_enriched.csv" in printed[0]
    assert not out.exists()


def test_fresh_dated_name_passes():
    base, lpr = make_rows()
    assert E.check_person(1, base, lpr, TODAY) == []


def test_name_older_than_12_months_is_kept_for_reference_only():
    base, lpr = make_rows(дата_источника_имени="2025-06")
    problems = E.check_person(1, base, lpr, TODAY)
    assert len(problems) == 1 and "старше 12 месяцев" in problems[0]

    base, lpr = make_rows(дата_источника_имени="2025-06", обращаться_по_имени=E.NO)
    assert E.check_person(1, base, lpr, TODAY) == []


def test_name_older_than_24_months_is_not_stored():
    base, lpr = make_rows(дата_источника_имени="2024-03", обращаться_по_имени=E.NO)
    problems = E.check_person(1, base, lpr, TODAY)
    assert len(problems) == 1 and "старше 24 месяцев" in problems[0]


def test_old_name_stays_when_a_standing_page_names_the_person(monkeypatch):
    base, lpr = make_rows(дата_источника_имени="2023", обращаться_по_имени=E.NO)
    monkeypatch.setitem(E.STANDING_PAGE, "Акме Станки", "https://acme-stanki.ru/about/")
    assert E.check_person(1, base, lpr, TODAY) == []

    # The standing page must be on the company's own site.
    monkeypatch.setitem(E.STANDING_PAGE, "Акме Станки", "https://catalog.example/acme/")
    assert any("не на сайте компании" in p for p in E.check_person(1, base, lpr, TODAY))


def test_year_only_date_counts_from_january():
    assert E.months_old("2023", TODAY) == 45
    assert E.months_old("2025-10", TODAY) == 12
    assert E.DATE_RE.match("2023") and E.DATE_RE.match("2026-09") and not E.DATE_RE.match("2023 (запись)")


def test_title_in_the_letter_is_a_run_of_the_site_words():
    site = "Руководитель направления развития бизнеса"
    assert E.is_run_of("руководитель направления развития бизнеса", site)
    assert E.is_run_of("руководитель направления", site)
    assert not E.is_run_of("руководитель развития бизнеса", site)  # a word from the middle is dropped
    assert E.is_run_of("генеральный директор", "Генеральный директор, сооснователь сервиса Shtab")

    base, lpr = make_rows(должность_ЛПР="Руководитель направления развития бизнеса",
                          должность_в_письме="руководитель развития бизнеса")
    base["должность_ЛПР"] = lpr["должность_ЛПР"]
    assert any("не подряд идущие слова" in p for p in E.check_person(1, base, lpr, TODAY))


def test_trigger_hypothesis_does_not_repeat_the_personalisation():
    plain = "Увидели, что в ноябре 2025 вы освоили выпуск стальных задвижек."
    about_dealers = "Увидели, что вы приглашаете дилеров и агентов по России и СНГ."

    assert E.trigger_hypothesis(E.DEALERS, "", plain).startswith("Вы набираете дилеров — предполагаем")
    assert E.trigger_hypothesis(E.DEALERS, "", about_dealers) == (
        "Предполагаем, что напрямую потенциальным дилерам пока никто не пишет.")
    assert E.trigger_hypothesis(E.HIRING, "Менеджер по продажам", plain).startswith(
        "Вы открыли вакансию «Менеджер по продажам» — предполагаем")
    looking = "Увидели на сайте, что вы ищете менеджера по продажам B2B."
    assert E.trigger_hypothesis(E.HIRING, "Менеджер по продажам B2B", looking) == (
        "Предполагаем, что новому сотруднику сразу понадобятся встречи.")


def test_every_hypothesis_fits_the_limit():
    texts = [*E.HYPOTHESIS_BY_VERTICAL.values(), *E.HYPOTHESIS_EXHIBITOR_BY_VERTICAL.values(),
             *E.HYPOTHESIS_BY_COMPANY.values(),
             E.trigger_hypothesis(E.DEALERS, "", ""), E.trigger_hypothesis(E.DEALERS, "", "дилеров")]
    assert all(text.startswith(("Предполагаем, что", "Вы набираете дилеров — предполагаем")) for text in texts)
    assert all(0 < E.word_count(text) <= E.MAX_HYPOTHESIS_WORDS for text in texts)
    assert "выставк" not in E.HYPOTHESIS_BY_VERTICAL[E.INDUSTRIAL]
    assert set(E.HYPOTHESIS_VERTICAL_BY_COMPANY.values()) <= set(E.HYPOTHESIS_BY_VERTICAL)


def test_exhibition_and_expansion_triggers_are_fresh(monkeypatch):
    # every trigger that is confirmed on the page of the fact has the month of its event, and nothing else has
    assert set(E.TRIGGER_EVENT) == {c for c, (trigger, _) in E.TRIGGERS.items() if trigger in E.PAGE_TRIGGERS}
    assert E.stale_triggers(TODAY) == []

    monkeypatch.setattr(E, "TRIGGERS", {"Акме Станки": (E.EXPO, "ТЕХНОФОРУМ")})
    monkeypatch.setattr(E, "TRIGGER_EVENT", {})
    assert "нет месяца события" in E.stale_triggers(TODAY)[0]
    monkeypatch.setattr(E, "TRIGGER_EVENT", {"Акме Станки": "2026-04"})   # six months old: still a trigger
    assert E.stale_triggers(TODAY) == []
    monkeypatch.setattr(E, "TRIGGER_EVENT", {"Акме Станки": "2026-03"})
    assert "старше 6 месяцев" in E.stale_triggers(TODAY)[0]
    monkeypatch.setattr(E, "TRIGGER_EVENT", {"Акме Станки": "2026-11"})   # an exhibition that is still ahead
    assert E.stale_triggers(TODAY) == []
    monkeypatch.setattr(E, "TRIGGER_EVENT", {"Акме Станки": "2026"})      # a bare year is not a month
    assert "нет месяца события" in E.stale_triggers(TODAY)[0]

    monkeypatch.setattr(E, "TRIGGERS", {"Акме Станки": (E.HIRING, "вакансия")})  # a vacancy is a state, not an event
    monkeypatch.setattr(E, "TRIGGER_EVENT", {})
    assert E.stale_triggers(TODAY) == []
