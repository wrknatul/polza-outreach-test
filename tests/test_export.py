"""Campaign export (tools/export_campaign.py): rendering of the letters and the assignment rules.

No network and no sending: the module under test only reads CSV files and renders text.
"""

from __future__ import annotations

import copy
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import export_campaign as E

GREETINGS = {"Здравствуйте!", "Добрый день!"}


def make_contact(variant: str = "а", **overrides) -> dict:
    """A row of the import file for a made-up company (the same one the other tests use)."""
    contact = {
        "email": "sales@acme-stanki.ru",
        "firstName": "",
        "lastName": "",
        "jobTitle": "",
        "phone": "",
        "companyName": "Акме Станки",
        "personalization": "Увидели, что в сентябре 2026 вы запустили участок лазерной резки на 30 кВт.",
        "hypothesis": "Предполагаем, что между выставками поток запросов проседает.",
        "letter1_variant": variant,
        "subject_variant": E.SUBJECT_A,
        "campaign": E.CAMPAIGN_INDUSTRY,
        "batch": "A",
        "mailbox": 1,
        "priority": "B",
        "vertical": "Промоборудование",
        "city": "Екатеринбург",
        "timezone": "МСК+2",
        "address_type": "",
        "lprName": "",
        "base": E.OWN,
        "row": 1,
        "company": "Акме Станки",
        "trigger": "события нет",
        "source": "https://acme-stanki.ru/news/",
    }
    return {**contact, **overrides}


def test_every_variable_is_substituted():
    chain = E.load_chain()
    contacts = {
        "а": make_contact("а"),
        "б": make_contact("б", firstName="Иван", lastName="Петров", lprName="Иван Петров",
                          jobTitle="коммерческий директор"),
        "в": make_contact("в", firstName="Иван Сергеевич", lastName="Петров", subject_variant=E.SUBJECT_B),
    }
    # the lead is the main case: both subjects of the test belong to its text
    lead_a = E.render_chain({**contacts["в"], "subject_variant": E.SUBJECT_A}, chain)[0]
    lead_b = E.render_chain(contacts["в"], chain)[0]
    assert lead_a.subject == "Акме Станки: вопрос о новых клиентах"
    assert lead_b.subject == "Акме Станки: за новых клиентов отвечаете вы?" and lead_a.body == lead_b.body
    rendered = {variant: E.render_chain(contact, chain) for variant, contact in contacts.items()}

    for variant, letters in rendered.items():
        assert [letter.step for letter in letters] == [1, 2, 3]
        for letter in letters:
            assert "{{" not in letter.subject + letter.body
            assert "}}" not in letter.subject + letter.body
            assert E.letter_errors("письмо", letter) == []
        first, second, third = letters
        assert first.subject.startswith("Акме Станки: ")
        assert contacts[variant]["personalization"] in first.body
        assert contacts[variant]["hypothesis"] in first.body
        assert second.in_thread and second.subject == f"Re: {first.subject}"
        assert third.subject.startswith("Акме Станки: ") and not third.in_thread

    assert "На вашем сайте указан коммерческий директор — Иван Петров." in rendered["б"][0].body
    assert rendered["в"][0].body.startswith("Здравствуйте, Иван Сергеевич!")
    assert rendered["а"][0].subject == "Акме Станки: кто отвечает за новых клиентов?"  # the spare variant asks who
    assert "это вопрос к вам или к кому-то из коллег?" in rendered["в"][0].body  # the lead is asked directly
    # Email 2 asks to forward the letter to a colleague, not to a named role: the addressee may hold that role.
    assert rendered["а"][1].body.splitlines()[-4].endswith("коллеге, который отвечает за новых клиентов.")

    # A variable with no value stays raw in the text, and the check names it.
    broken = E.render_chain(make_contact("б"), chain)[0]
    errors = E.letter_errors("письмо 1", broken)
    assert any("{{jobTitle}}" in error and "{{lprName}}" in error for error in errors)
    # Square brackets other than the sender's placeholders are reported too.
    foreign = E.render_chain(make_contact(personalization="Увидели [что-то] на сайте."), chain)[0]
    assert any("квадратные скобки" in error for error in E.letter_errors("письмо 1", foreign))


def test_named_contact_is_greeted_by_name_in_every_letter():
    chain = E.load_chain()
    named = make_contact("в", firstName="Иван Сергеевич", lastName="Петров", subject_variant=E.SUBJECT_B)
    plain, lpr = make_contact("а"), make_contact("б", lprName="Иван Петров", jobTitle="коммерческий директор")

    letters = E.render_chain(named, chain)
    assert [letter.body.splitlines()[0] for letter in letters] == ["Здравствуйте, Иван Сергеевич!"] * 3
    # only the first line differs from the letter a department mailbox gets
    for mine, common in zip(letters[1:], E.render_chain(plain, chain)[1:], strict=True):
        assert mine.body.splitlines()[1:] == common.body.splitlines()[1:]
        assert common.body.splitlines()[0] in GREETINGS
    # spare variant «б» names the person in email 1 only: the letters go to a shared mailbox
    for letter in E.render_chain(lpr, chain)[1:]:
        assert letter.body.splitlines()[0] in GREETINGS and "Иван" not in letter.body
    # the worst case of the word limit is counted with the by-name greeting
    assert all(letter.body.startswith("Здравствуйте, Иван Сергеевич!") for letter in E.worst_case(named, chain))

    # the real base: every own row is a lead with a name, and all three letters start with it
    contacts, _ = E.build_contacts()
    real = [contact for contact in contacts if contact["base"] == E.OWN]
    assert real and {contact["letter1_variant"] for contact in real} == {E.NAMED_VARIANT}
    assert {contact["address_type"] for contact in real} <= {E.PERSONAL_BOX, E.ROLE_BOX}
    for contact in real:
        greeting = f"Здравствуйте, {contact['firstName']}!"
        assert [letter.body.splitlines()[0] for letter in E.render_chain(contact, chain)] == [greeting] * 3


def test_preview_maps_columns_to_template_variables():
    chain = E.load_chain()
    contacts, report = E.build_contacts()
    preview = E.preview_markdown(contacts, chain, report)

    assert preview.splitlines()[0].startswith("Письма не отправлялись")
    used = E.chain_variables(chain)
    assert used <= set(E.VARIABLES) == set(E.VARIABLE_PLACES)  # every variable of the templates has a column
    assert "RANDOM" not in used
    for variable in used:
        column = E.VARIABLES[variable]
        assert column in E.IMPORT_FIELDS
        assert f"| `{column}` | `{{{{{variable}}}}}` |" in preview
    # the two variables named in Russian are renamed before the templates are uploaded
    assert "`{{персонализация}}` → `{{personalization}}`" in preview
    assert "`{{гипотеза}}` → `{{hypothesis}}`" in preview
    assert "`{{companyName}}` → " not in preview  # the same name in the template and in the file

    # the first example is a lead: all three letters, each greeting by name
    section = preview.split("## 1. Лид — ")[1].split("\n## ")[0]
    assert section.count("### Письмо") == 3
    assert section.count("```\nЗдравствуйте, ") == 3
    # a lead with a role mailbox says so; the spare variants are shown on a reserve row and on the reviewers' base
    assert f"` — {E.ADDRESS_LABELS[E.ROLE_BOX]}" in preview and E.ROLE_BOX_NOTE in preview
    reserve = preview.split("Строка резерва, запасной вариант «б»")[1].split("\n## ")[0]
    assert "в запуск не идёт" in reserve and "На вашем сайте указан " in reserve
    assert "Строка вашей базы (задание 4), запасной вариант «а»" in preview
    # outside the code blocks there are no raw variables except the table and the rename line
    letters = [block for index, block in enumerate(preview.split("```")) if index % 2]
    assert letters and all("{{" not in block for block in letters)


def test_random_greeting_is_expanded():
    template = "{{RANDOM | Здравствуйте | Добрый день}}!"
    assert E.render(template, {}, pick=lambda options: options[0]) == "Здравствуйте!"
    assert E.render(template, {}, pick=lambda options: options[-1]) == "Добрый день!"
    assert E.render("Без ротации.", {}) == "Без ротации."

    chain = E.load_chain()
    seen = set()
    for number in range(40):
        contact = make_contact(email=f"sales{number}@acme-stanki.ru")
        letters = E.render_chain(contact, chain)
        assert all("RANDOM" not in letter.body for letter in letters)
        # the choice is pinned to the address: a re-run renders the same text
        assert E.render_chain(contact, chain) == letters
        seen.add(letters[0].body.splitlines()[0])
    assert seen == GREETINGS  # both options are in use

    # the word limit is checked with the longest option
    assert all(letter.body.startswith("Добрый день!") for letter in E.worst_case(make_contact(), chain))


def test_word_limit():
    chain = E.load_chain()
    contacts, _ = E.build_contacts()  # the real bases: every letter of the export must fit

    assert contacts
    assert E.check_export(contacts, chain) == []
    longest = max(letter.words for contact in contacts for letter in E.worst_case(contact, chain))
    assert longest <= E.LIMIT == 120

    # the subject is inside the limit: body + subject
    letter = E.render_chain(contacts[0], chain)[0]
    assert letter.words == E.count_words(letter.subject) + E.count_words(letter.body)

    too_long = make_contact(personalization=" ".join(["слово"] * 60) + ".")
    errors = E.letter_errors("письмо 1", E.render_chain(too_long, chain)[0])
    assert len(errors) == 1 and "лимит 120" in errors[0]


def test_corrected_email_with_a_note_in_brackets():
    assert E.first_email("sale01@example.com (опубликован на сайте; на англ. версии также info@example.com)") \
        == "sale01@example.com"
    assert E.first_email("knigi@example.ru (запасной, опубликован на сайте)") == "knigi@example.ru"
    assert E.first_email("—") == ""
    assert E.first_email("") == ""

    corrected = {"email": "sales@wrong.example", "исправленный_email": "sales@right.example (опубликован на сайте)"}
    unchanged = {"email": "sales@example.su", "исправленный_email": "—"}
    assert E.their_address(corrected) == "sales@right.example"
    assert E.their_address(unchanged) == "sales@example.su"


def test_subject_split_is_balanced():
    sizes = {"B2B SaaS": 9, "Упаковка": 10, "Юридические услуги": 5, "Логистика": 4, "B2B-маркетинг": 1}
    contacts = [make_contact("в", vertical=vertical, subject_variant="", email=f"{vertical}-{number}@example.ru",
                             firstName="Иван")
                for vertical, size in sizes.items() for number in range(size)]
    later = [make_contact("в", vertical="Упаковка", subject_variant="", firstName="Иван", batch="D")
             for _ in range(3)]
    their = [make_contact(base=E.THEIR, vertical="экспонент", subject_variant="", batch=E.THEIR_BATCH)
             for _ in range(2)]
    everyone = contacts + later + their
    again = copy.deepcopy(everyone)

    E.assign_subject_variants(everyone)
    E.assign_subject_variants(again)

    assert [c["subject_variant"] for c in again] == [c["subject_variant"] for c in everyone]  # the seed is fixed
    assert {c["subject_variant"] for c in later} == {E.WINNER}  # sent after the test is read
    assert {c["subject_variant"] for c in their} == {E.FIXED}  # the reviewers' base is not part of the test
    for vertical, size in sizes.items():
        split = Counter(c["subject_variant"] for c in contacts if c["vertical"] == vertical)
        assert split[E.SUBJECT_A] + split[E.SUBJECT_B] == size
        assert abs(split[E.SUBJECT_A] - split[E.SUBJECT_B]) <= 1
    total = Counter(c["subject_variant"] for c in contacts)
    assert abs(total[E.SUBJECT_A] - total[E.SUBJECT_B]) <= 1  # three odd verticals, still even overall

    # the real base: the test runs on the batches of the first week, on one text, in equal halves
    real, _ = E.build_contacts()
    in_test = [c for c in real if c["subject_variant"] in (E.SUBJECT_A, E.SUBJECT_B)]
    assert {c["batch"] for c in in_test} == set(E.TEST_BATCHES) and {c["base"] for c in in_test} == {E.OWN}
    assert {c["letter1_variant"] for c in in_test} == {E.NAMED_VARIANT}
    real_split = Counter(c["subject_variant"] for c in in_test)
    assert abs(real_split[E.SUBJECT_A] - real_split[E.SUBJECT_B]) <= 1
    assert {c["subject_variant"] for c in real if c["base"] == E.THEIR} == {E.FIXED}


def test_batches_follow_the_priority():
    own = [make_contact("в", row=number, priority=priority, email=f"lead{number}@example.ru", batch="", mailbox="")
           for number, priority in enumerate(["C", "B", "A", "B"] * 6, 1)]  # 24 leads
    their = [make_contact(base=E.THEIR, row=number, email=f"sales{number}@example.cn", batch="", mailbox="")
             for number in (1, 2, 3)]

    ordered = E.assign_batches(their + own)

    assert [c["base"] for c in ordered] == [E.OWN] * 24 + [E.THEIR] * 3
    assert [c["priority"] for c in ordered[:24]] == ["A"] * 6 + ["B"] * 12 + ["C"] * 6  # A first, C last
    assert [c["row"] for c in ordered[:6]] == [3, 7, 11, 15, 19, 23]  # inside a priority — by row number
    assert Counter(c["batch"] for c in ordered) == {"A": 20, "B": 4, E.THEIR_BATCH: 3}
    assert E.expected_batches(99) == {"A": 20, "B": 20, "C": 20, "D": 20, "E": 19}
    assert E.expected_batches(69) == {"A": 20, "B": 20, "C": 20, "D": 9, "E": 0}  # a smaller base leaves E empty
    assert E.LATER_BATCHES == ("D", "E") and E.THEIR_BATCH not in dict(E.OWN_BATCHES)
    assert [c["mailbox"] for c in ordered[:4]] == [1, 2, 1, 2]

    # the real base: role mailboxes with no trigger (priority C) are sent last, after the test is read
    real, _ = E.build_contacts()
    last = [c for c in real if c["base"] == E.OWN and c["priority"] == "C"]
    assert last and {c["batch"] for c in last} == {E.OWN_BATCHES[-1][0]}
    assert {c["address_type"] for c in last} == {E.ROLE_BOX}
