"""The chain checker (tools/check_chain.py) and the numbers of the documents (tools/build_xlsx.py).

No network and no sending: the modules under test read the CSV and Markdown files of the repository.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import check_chain as C

NAME_GREETING = "Здравствуйте, {{firstName}}!"
RANDOM_GREETING = "{{RANDOM | Здравствуйте | Добрый день}}!"


def chain_rows() -> list[dict]:
    return C.read_rows(C.CHAIN_CSV)[0]


def test_the_published_chain_passes_its_own_check(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["check_chain.py"])
    assert C.main() == 0
    assert "OK: тема + тело не длиннее 120 слов" in capsys.readouterr().out


def test_every_letter_greets_the_lead_by_name():
    rows = chain_rows()
    first = {row["вариант"]: row for row in rows if row["шаг"] == "1"}
    assert list(first) == [C.NAMED_VARIANT, "б", "а"]  # the by-name text is the main one, the spare ones follow
    assert first[C.NAMED_VARIANT]["текст"].splitlines()[0] == NAME_GREETING
    assert C.subject_of(first[C.NAMED_VARIANT]["тема"]) and C.subject_of(first[C.NAMED_VARIANT]["тема_AB"])
    for row in rows:
        if row["шаг"] == "1":
            assert C.plain_greeting(row) == ""  # every variant of email 1 has its own text
            continue
        assert C.greets_by_name(row["текст"]) and row["текст"].count("{{firstName}}") == 1
        # a department mailbox gets the same letter with the no-name greeting
        assert C.plain_greeting(row) == RANDOM_GREETING
        plain = C.plain_body(row).splitlines()
        assert plain[0] == RANDOM_GREETING and plain[1:] == row["текст"].strip().splitlines()[1:]
        assert "{{" not in C.expand_random(C.plain_body(row))


def test_greeting_column_rules():
    by_name = {"шаг": "2", "вариант": "", "текст": f"{NAME_GREETING}\n\nТекст.", C.GREETING_COLUMN: RANDOM_GREETING}
    assert C.greeting_errors("письмо 2", by_name, has_named=True, has_plain=True) == []

    no_name = {**by_name, "текст": f"{RANDOM_GREETING}\n\nТекст."}
    assert any("не приветствие по имени" in e for e in C.greeting_errors("письмо 2", no_name, True, True))

    no_spare = {**by_name, C.GREETING_COLUMN: ""}
    errors = C.greeting_errors("письмо 2", no_spare, True, True)
    assert any("нет приветствия для вариантов без имени" in e for e in errors)
    assert C.greeting_errors("письмо 2", no_spare, has_named=True, has_plain=False) == []  # nobody needs it

    named_spare = {**by_name, C.GREETING_COLUMN: NAME_GREETING}
    assert any("ротация RANDOM без имени" in e for e in C.greeting_errors("письмо 2", named_spare, True, True))

    first = {"шаг": "1", "вариант": "в", "текст": f"{NAME_GREETING}\n\nТекст.", C.GREETING_COLUMN: RANDOM_GREETING}
    assert any("должна быть пустой" in e for e in C.greeting_errors("письмо 1 «в»", first, True, True))

    # a chain with no by-name email 1 must not greet by name in the follow-ups
    assert any("варианта «в» у письма 1 нет" in e for e in C.greeting_errors("письмо 2", by_name, False, True))


def test_a_name_in_the_body_of_a_follow_up_is_an_error():
    signature = "\n\n".join(["", *["\n".join(C.SIGNATURE)]])
    body = f"{NAME_GREETING}\n\n{C.INTRO_NEXT}. Ответьте «да» — предложим время.{signature}"
    assert C.structure_errors("письмо 2", 2, "", body, named=True) == []
    assert any("допустимы только в письме 1" in e for e in C.structure_errors("письмо 2", 2, "", body))
    twice = body.replace("Ответьте", "{{firstName}}, ответьте")
    assert any("только в нём" in e for e in C.structure_errors("письмо 2", 2, "", twice, named=True))


def test_strict_count_of_names():
    # macOS `wc -w` breaks a word at the byte 0x85, which is inside «х»: such a name counts as one word more
    assert C.count_words("Иван Петрович") == 2
    assert C.count_words(C.STUBS["firstName"]) == 3
    assert C.count_words("Здравствуйте, Иван!") == 2


def test_numbers_of_the_readme_match_the_data():
    pytest.importorskip("openpyxl")
    import build_xlsx as X

    enriched, their = X.read_csv("task1_2_enriched.csv"), X.read_csv("task4_final.csv")
    reserve, leads_before = X.read_csv("task1_reserve.csv"), len(X.read_csv("task1_leads.csv"))
    chain, replies = X.read_csv("task3_chain.csv"), X.read_csv("reply_playbook.csv")
    contacts, report = X.build_contacts()
    stats = X.collect_stats(enriched, their, contacts, report, chain, X.md_tables(X.LAUNCH_MD), replies, reserve,
                            leads_before, X.read_csv("task2_script_output.csv"))
    lines = X.number_lines(stats)

    problems, checked = X.stale_documents(stats, lines)
    assert X.README_MD in checked and problems == []
    # the base consists of leads: a named decision maker with an address from his or her own card
    assert stats.total == sum(stats.kinds[kind] for kind in X.LEAD_TYPES) == stats.levels["А"]
    assert stats.leads_before == stats.total  # every selected lead passed the live rebuild of the base
    research = X.RESEARCH
    assert research.candidates - research.rejected - research.final - research.spare - research.held == stats.total
    assert X.LEADFINDER_LINE in (X.ROOT / X.README_MD).read_text(encoding="utf-8")
    assert stats.reserve == sum(stats.reserve_reasons.values())
    # the first six columns of sheet 1-2 are the ones the course asks for
    assert [header for _, header, _ in X.BASE_COLUMNS[:6]] == X.COURSE_HEADERS == list(enriched[0])[:6]
