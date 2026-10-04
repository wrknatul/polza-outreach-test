"""Export the campaign for a mailing service and render a preview of the letters. Nothing is sent.

The course, lesson 2: items 4.3-4.4 ask to save a CSV «для дальнейшей загрузки в вашу CRM или сервис
рассылок» with a structure that is easy to import; item 5.3 asks to make sure the variables are
substituted correctly. This script does both for the base of tasks 1-2 and for the reviewers' base of
task 4.

There is no sending code here: no SMTP, no HTTP, no API of a mailing service, no network at all. The
script reads four CSV files and writes three files.

Input:
  task1_2_enriched.csv  the base of tasks 1-2: leads — a named decision maker with a work address the
                        company prints next to the name; personalisation, hypothesis, priority
  task4_final.csv       the reviewers' base: corrected addresses, language of the letter
  task3_chain.csv       the templates: email 1 (the by-name text and two spare variants), emails 2 and 3
  task1_reserve.csv     companies with a department mailbox only; they are not launched and are read for
                        one thing: an example of spare variant «б» in preview.md

Output (directory export/, UTF-8 with BOM like the other deliverables):
  import_instantly.csv  one row per contact, in sending order; the columns are the variables of a
                        mailing service (plus `lprName`, which spare variant «б» of email 1 needs, and
                        `address_type`: a personal mailbox or a role mailbox of the decision maker)
  letters_preview.csv   one row per contact: the subject and email 1 with the variables substituted
  preview.md            rendered examples to read aloud before the launch, the table «column of the
                        import file -> variable of the template» and a note on the columns

Rules:
  - a row with «нет данных» instead of a personalisation and a row with `язык_письма = EN` (the chain
    is written in Russian) do not get into the import; the reason is printed;
  - the own base: every row is a lead, email 1 goes in variant «в» and all three letters greet by name;
  - the reviewers' base: the address is the first one in `исправленный_email`, else `email`; nobody is
    named there, so email 1 goes in spare variant «а» and the follow-ups start with the no-name greeting
    (column `приветствие_без_имени` of task3_chain.csv); the file has no city and no time zone;
  - batches: the own rows by priority A -> B -> C, then by row number, 20 per batch (A, B, C, D, E); the
    reviewers' base is batch F; the mailbox alternates 1 / 2 along the sending order, and a contact
    keeps its mailbox for all three letters;
  - subject of email 1: batches A-C are the A/B test — halves inside each vertical, drawn with
    random.Random(20261003); batches D and E are sent after the test is read and get the winning
    subject; the reviewers' base gets the main subject of its own variant and is not part of the test
    (launch_plan.md, section 7).

Checks (any failure: exit code 1, nothing is written): no `{{` or `}}` is left in a rendered letter;
the only square brackets left are the sender's `[Имя]`, `[Имя Фамилия]`, `[должность]`; subject + body
of every letter is at most 120 words (the strict count of tools/check_chain.py, the longest greeting);
the addresses are unique; the own batches are filled in order, 20 in each, the last one takes the rest,
and batch F holds at most 15; the test is balanced inside every vertical and runs on one variant of email 1.

Public API for tools/build_xlsx.py:
  chain = load_chain()
  contacts, report = build_contacts()
  for contact, shown in preview_examples(contacts):        # leads, a reserve row, a row of the reviewers' base
      for letter in render_chain(contact, chain)[:shown]:  # Letter(step, delay, subject, body, in_thread)
          ...                                              # no {{RANDOM}} and no {{companyName}} left

Usage:
  python tools/export_campaign.py                write export/
  python tools/export_campaign.py --check        run the checks and print the report, write nothing
  python tools/export_campaign.py --out-dir DIR  write the three files to another directory
"""
import argparse
import csv
import io
import random
import re
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from check_chain import (
    GREETING_COLUMN,
    count_words,
    expand_random,
    is_thread_marker,
    longest,
    plain_body,
    plain_greeting,
    subject_of,
)

ROOT = Path(__file__).resolve().parent.parent
ENRICHED = ROOT / "task1_2_enriched.csv"
THEIR_BASE = ROOT / "task4_final.csv"
RESERVE_CSV = ROOT / "task1_reserve.csv"  # not launched: read only for an example in preview.md
CHAIN_CSV = ROOT / "task3_chain.csv"
EXPORT_DIR = ROOT / "export"
BOM = b"\xef\xbb\xbf"

SEED = 20261003
LIMIT = 120  # words in subject + body, the limit of the TZ
NO_DATA = "нет данных"
SKIPPED_LANGUAGE = "EN"
OWN, THEIR, RESERVE = "моя база", "ваша база", "резерв"

DEFAULT_VARIANT = "а"  # spare: email 1 to a shared mailbox, nobody is named
LPR_VARIANT = "б"  # spare: the decision maker is named in the text, the address is a department mailbox
NAMED_VARIANT = "в"  # the main email 1: a lead, greeted by name in every letter
SUBJECT_A, SUBJECT_B = "A", "B"
WINNER = "победитель A/B"  # the own batches that are sent after the test is read
FIXED = "вне теста"  # the reviewers' base: the main subject of its own variant

PRIORITY_ORDER = ("A", "B", "C")
OWN_BATCHES = (("A", 20), ("B", 20), ("C", 20), ("D", 20), ("E", 20))  # filled in this order; the last may be short
TEST_BATCHES = ("A", "B", "C")  # the A/B test of the subject: the letters 1 of the first week
LATER_BATCHES = tuple(name for name, _ in OWN_BATCHES if name not in TEST_BATCHES)  # sent after the test is read
THEIR_BATCH, THEIR_BATCH_MAX = "F", 15
MAILBOXES = (1, 2)

CAMPAIGN_IT, CAMPAIGN_SERVICES, CAMPAIGN_INDUSTRY = "IT и интеграторы", "услуги для бизнеса", "промышленность"
CAMPAIGN_THEIR = "экспоненты"
OWN_CAMPAIGNS = (CAMPAIGN_IT, CAMPAIGN_SERVICES, CAMPAIGN_INDUSTRY)
CAMPAIGN_BY_VERTICAL = {
    "B2B SaaS": CAMPAIGN_IT,
    "Интеграторы и автоматизация": CAMPAIGN_IT,
    "B2B-маркетинг": CAMPAIGN_IT,
    "Юридические услуги": CAMPAIGN_SERVICES,
    "Аудит и консалтинг": CAMPAIGN_SERVICES,
    "Подбор руководителей": CAMPAIGN_SERVICES,
    "Логистика": CAMPAIGN_SERVICES,
    "Логистика ВЭД": CAMPAIGN_SERVICES,
    "Инжиниринг и промбезопасность": CAMPAIGN_INDUSTRY,
    "Промоборудование": CAMPAIGN_INDUSTRY,
    "Промдистрибуция": CAMPAIGN_INDUSTRY,
    "Оптовая дистрибуция": CAMPAIGN_INDUSTRY,
    "Упаковка": CAMPAIGN_INDUSTRY,
    "Стройматериалы": CAMPAIGN_INDUSTRY,
    "Коммерческая техника": CAMPAIGN_INDUSTRY,
    "Медицинские изделия": CAMPAIGN_INDUSTRY,
    "Полиграфия и сувениры": CAMPAIGN_SERVICES,
    "HR-tech": CAMPAIGN_IT,  # only in the reserve
}

IMPORT_FIELDS = [
    "email", "firstName", "lastName", "jobTitle", "phone", "companyName", "personalization",
    "hypothesis", "letter1_variant", "subject_variant", "campaign", "batch", "mailbox", "priority",
    "vertical", "city", "timezone",
    # not in the course list: whose mailbox it is, and {{lprName}} of spare variant «б»
    "address_type", "lprName",
]
PREVIEW_FIELDS = ["компания", "адрес", "вариант", "вариант_темы", "тема", "письмо_1", "слов"]

# Variable of task3_chain.csv -> column of the import file.
VARIABLES = {
    "companyName": "companyName",
    "персонализация": "personalization",
    "гипотеза": "hypothesis",
    "firstName": "firstName",
    "lprName": "lprName",
    "jobTitle": "jobTitle",
}
# Where each variable stands in the letters: the third column of the table in preview.md.
VARIABLE_PLACES = {
    "companyName": "тема писем 1 и 3; письмо 2 уходит ответом в ветку письма 1",
    "персонализация": "письмо 1, второй абзац",
    "гипотеза": "письмо 1, второй абзац, сразу после персонализации",
    "firstName": "приветствие писем 1–3",
    "lprName": "письмо 1, четвёртый абзац, только запасной вариант «б»",
    "jobTitle": "письмо 1, четвёртый абзац, только запасной вариант «б»",
}
# What a contact must have besides the address, by email-1 variant.
REQUIRED = {
    DEFAULT_VARIANT: ("companyName", "personalization", "hypothesis"),
    LPR_VARIANT: ("companyName", "personalization", "hypothesis", "lprName", "jobTitle"),
    NAMED_VARIANT: ("companyName", "personalization", "hypothesis", "firstName"),
}
SENDER_PLACEHOLDERS = ("[Имя Фамилия]", "[Имя]", "[должность]")  # filled in by the team

# The examples of preview.md: (company, base, email-1 variant, trigger, address type or '', letters to show).
# If the company left the base after a rebuild, the first row that fits the other conditions is shown.
PERSONAL_BOX, ROLE_BOX = "именной (у ЛПР)", "ящик должности ЛПР"  # `тип_адреса` of a lead, see tools/enrich_base.py
PREVIEW_EXAMPLES = (
    ("Интерволга", OWN, NAMED_VARIANT, "", PERSONAL_BOX, 3),  # all three letters start with the name
    ("Optimalog", OWN, NAMED_VARIANT, "нанимают в продажи", "", 1),  # the hypothesis of a confirmed trigger
    ("10-ГПЗ", OWN, NAMED_VARIANT, "", ROLE_BOX, 1),
    ("Logsis", RESERVE, LPR_VARIANT, "", "", 1),
    ("Ezhong", THEIR, DEFAULT_VARIANT, "", "", 1),
)
VARIANT_NOTES = {
    DEFAULT_VARIANT: "ЛПР на сайте не назван, письмо идёт на ящик отдела с вопросом «кто отвечает»",
    LPR_VARIANT: "ЛПР назван на сайте, но адрес — ящик отдела; в письме стоят его должность и имя",
    NAMED_VARIANT: ("рабочий адрес напечатан на сайте компании рядом с этим человеком, поэтому письмо идёт ему "
                    "и обращается по имени во всех трёх письмах"),
}
ADDRESS_LABELS = {PERSONAL_BOX: "именной адрес ЛПР", ROLE_BOX: "ящик должности ЛПР"}
ROLE_BOX_NOTE = ("адрес — ящик должности (dir@, kd@): его может разбирать помощник, без события такие строки "
                 "идут последними")
RESERVE_NOTE = "резерв в запуск не идёт; пример показывает, как выглядел бы запасной вариант"
THEIR_NOTE = "имён для вашей базы я не собирал, письмо идёт на проверенный адрес компании"
PREVIEW_HEADER = """\
Письма не отправлялись. Это рендер шаблонов на данных базы.

# Превью писем

Файл собирает `tools/export_campaign.py` из `task3_chain.csv` (шаблоны), `task1_2_enriched.csv` (моя база: \
лиды) и `task4_final.csv` (ваша база). Курс просит перед запуском убедиться, что переменные подставляются \
корректно (урок 2, п. 5.3). Ниже примеры, которые стоит прочитать вслух: три лида (все три письма, гипотеза \
по событию, ящик должности), запасной вариант «б» на строке резерва и строка вашей базы.

- Письмо 1 для всех строк импорта ({count}) — в `letters_preview.csv`, файл для сервиса рассылок — \
`import_instantly.csv`.
- В квадратных скобках остались только данные отправителя: {placeholders}. Их заполняет команда.
- Лид получает обращение по имени во всех трёх письмах.
{plain_note}- Счёт слов строгий, как в `tools/check_chain.py`: обычный счёт по пробелам даёт на 2–8 слов \
меньше. Тема входит в лимит {limit} слов.
- Что проверить при чтении: в письме 1 одно обращение, один вопрос и подпись в две строки; в письмах 2 и 3 — \
одна просьба ответить.
"""
# A line of the header of preview.md; it is there only if the follow-ups have a no-name greeting.
PLAIN_NOTE = """\
- Строки запасных вариантов «а» и «б» (ящик отдела) получают безличное приветствие: в письмах 2 и 3 первая \
строка — `{plain_greeting}` вместо имени (колонка `{greeting_column}` в `task3_chain.csv`). Ротация \
(Здравствуйте / Добрый день) здесь раскрыта в один из вариантов, в сервисе рассылок она остаётся включённой.
"""
# The last section of preview.md: (columns of the import file, what they hold).
IMPORT_NOTES = (
    ("`email`",
     ("моя база — рабочий адрес ЛПР, напечатанный на сайте компании рядом с его именем; ваша база — адрес "
      "компании, исправленный, если исправление есть")),
    ("`firstName`, `lastName`, `jobTitle`, `lprName`",
     ("имя (с отчеством, если сайт его даёт), фамилия и должность ЛПР; `firstName` — приветствие писем 1–3. "
      "У вашей базы пусто: людей там я не собирал")),
    ("`address_type`",
     f"«{PERSONAL_BOX}» или «{ROLE_BOX}»: ящик должности напечатан в карточке того же человека, но разбирать "
     "его может помощник"),
    ("`phone`", "телефон отдела или общий, как он напечатан на сайте; личных мобильных нет"),
    ("`companyName`", "короткое название, стоит в теме перед двоеточием"),
    ("`personalization`, `hypothesis`", "факт о компании и гипотеза боли — второй абзац письма 1"),
    ("`letter1_variant`",
     ("какой текст письма 1 получает строка: «в» — основной, лиду по имени; «а» — запасной, без имени, "
      "у вашей базы")),
    ("`subject_variant`",
     (f"A или B — тема письма 1 в тесте (батчи {', '.join(TEST_BATCHES)}); «{WINNER}» — батчи "
      f"{' и '.join(LATER_BATCHES)}, они уходят после замера теста и получают тему-победителя; «{FIXED}» — ваша "
      "база")),
    ("`campaign`, `vertical`", "кампания, по которой считается статистика, и вертикаль строки"),
    ("`batch`, `mailbox`, `priority`",
     f"батч, ящик 1 или 2 (закреплён за контактом на все письма), приоритет строки; батч {THEIR_BATCH} — ваша база"),
    ("`city`, `timezone`",
     f"нужны для отправки в рабочее время получателя; у батча {THEIR_BATCH} пусто: в вашей базе этих данных нет"),
)

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
VARIABLE_RE = re.compile(r"\{\{\s*([^{}|]+?)\s*\}\}")
BRACKET_RE = re.compile(r"[\[\]]")

Pick = Callable[[list[str]], str]


class Letter(NamedTuple):
    """One rendered letter of the chain."""

    step: int
    delay: str
    subject: str  # as the recipient sees it; a reply in the thread shows «Re: » + the subject of email 1
    body: str
    in_thread: bool

    @property
    def subject_words(self) -> int:
        return count_words(self.subject)

    @property
    def body_words(self) -> int:
        return count_words(self.body)

    @property
    def words(self) -> int:
        return self.subject_words + self.body_words


class Chain(NamedTuple):
    """The templates of task3_chain.csv."""

    first: dict[str, dict]  # email-1 variant -> row
    followups: list[dict]  # rows of emails 2 and 3, in order


class Report(NamedTuple):
    """What was read and what was left out; printed by main()."""

    own_rows: int
    their_rows: int
    excluded: list[tuple[str, int, str, str]]  # (base, row number, company, reason)
    corrected: list[str]  # reviewers' rows whose address comes from `исправленный_email`


def read_rows(path: Path) -> list[dict]:
    return list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8-sig"), newline="")))


def write_bytes(path: Path, data: bytes) -> None:
    """Write atomically: a crash never leaves a half-written file in export/."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def csv_bytes(rows: list[dict], fields: list[str]) -> bytes:
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return BOM + out.getvalue().encode("utf-8")


def load_chain(path: Path = CHAIN_CSV) -> Chain:
    rows = read_rows(path)
    first = {row["вариант"].strip(): row for row in rows if row["шаг"].strip() == "1"}
    followups = sorted((row for row in rows if row["шаг"].strip() != "1"), key=lambda row: int(row["шаг"]))
    return Chain(first, followups)


def skip_reason(personalization: str, language: str = "") -> str:
    """Why the row does not get into the import; '' if it does."""
    if language.strip().upper() == SKIPPED_LANGUAGE:
        return f"язык_письма = {SKIPPED_LANGUAGE}, а цепочка написана на русском"
    if personalization.lower().strip(" .«»\"") in ("", NO_DATA):
        return f"персонализации нет («{NO_DATA}»), а письмо 1 без факта не уходит"
    return ""


def first_email(cell: str) -> str:
    """The first address of a cell like «sale01@x.com (опубликован на сайте; также y@x.com)», or ''."""
    match = EMAIL_RE.search(cell or "")
    return match.group(0) if match else ""


def their_address(row: dict) -> str:
    """The corrected address if the row has one, else the original address."""
    return first_email(row.get("исправленный_email", "")) or first_email(row.get("email", ""))


def join_words(*parts: str) -> str:
    return " ".join(part.strip() for part in parts if part and part.strip())


def own_contact(number: int, row: dict, base: str = OWN) -> dict:
    """A row of task1_2_enriched.csv (or of the reserve, the same columns) as a row of the import file."""
    variant = row["вариант_письма_1"].strip()
    named = variant in (LPR_VARIANT, NAMED_VARIANT)  # the letter uses the name
    vertical = row["вертикаль"].strip()
    return {
        "email": row["Email"].strip(),
        "firstName": join_words(row["Имя"], row["Отчество"]) if named else "",
        "lastName": row["Фамилия"].strip() if named else "",
        "jobTitle": row["должность_в_письме"].strip() if named else "",
        "phone": row["Телефон"].strip(),
        "companyName": row["компания_в_письме"].strip(),
        "personalization": row["Персонализация"].strip(),
        "hypothesis": row["Гипотеза_боли"].strip(),
        "letter1_variant": variant,
        "subject_variant": "",
        "campaign": CAMPAIGN_BY_VERTICAL.get(vertical, ""),
        "batch": "",
        "mailbox": "",
        "priority": row["приоритет"].strip(),
        "vertical": vertical,
        "city": row["город"].strip(),
        "timezone": row["часовой_пояс"].strip(),
        "address_type": row["тип_адреса"].strip(),
        "lprName": join_words(row["Имя"], row["Отчество"], row["Фамилия"]) if named else "",
        # not columns of the import file
        "base": base,
        "row": number,
        "company": row["Компания"].strip(),
        "trigger": row["тип_триггера"].strip(),
        "source": row["Источник"].strip(),
    }


def their_contact(number: int, row: dict) -> dict:
    """A row of task4_final.csv as a row of the import file: variant «а», no names, the reviewers' batch."""
    return {
        "email": their_address(row),
        "firstName": "",
        "lastName": "",
        "jobTitle": "",
        "phone": "",
        "companyName": row["компания_в_письме"].strip(),
        "personalization": row["персонализация"].strip(),
        "hypothesis": row["Гипотеза_боли"].strip(),
        "letter1_variant": DEFAULT_VARIANT,
        "subject_variant": "",
        "campaign": CAMPAIGN_THEIR,
        "batch": "",
        "mailbox": "",
        "priority": "",
        "vertical": row["сегмент"].strip(),
        "city": "",
        "timezone": "",
        "address_type": "",
        "lprName": "",
        "base": THEIR,
        "row": number,
        "company": row["company"].strip(),
        "trigger": row["триггер"].strip(),
        "source": row["источник"].strip(),
    }


def assign_subject_variants(contacts: list[dict], seed: int = SEED) -> None:
    """Fill `subject_variant`; the batches must be assigned first.

    The own rows of the test batches get A or B in halves; the own rows sent later wait for the winner;
    the reviewers' base keeps the main subject of its variant. The halves are drawn inside each vertical.
    The odd contact of a vertical goes to the variant that is behind over the whole test, so the totals
    differ by one at most.
    """
    rng = random.Random(seed)  # a reproducible split
    groups: dict[str, list[dict]] = {}
    for contact in contacts:
        if contact["base"] != OWN:
            contact["subject_variant"] = FIXED
        elif contact["batch"] not in TEST_BATCHES:
            contact["subject_variant"] = WINNER
        else:
            groups.setdefault(contact["vertical"], []).append(contact)
    total_a = total_b = 0
    for group in groups.values():
        shuffled = list(group)
        rng.shuffle(shuffled)
        half, odd = divmod(len(shuffled), 2)
        count_a = half + (odd if total_a <= total_b else 0)
        for index, contact in enumerate(shuffled):
            contact["subject_variant"] = SUBJECT_A if index < count_a else SUBJECT_B
        total_a += count_a
        total_b += len(shuffled) - count_a


def assign_batches(contacts: list[dict]) -> list[dict]:
    """Fill `batch` and `mailbox`; return the contacts in sending order."""
    order = {priority: index for index, priority in enumerate(PRIORITY_ORDER)}
    own = sorted((c for c in contacts if c["base"] == OWN),
                 key=lambda c: (order.get(c["priority"], len(order)), c["row"]))
    their = sorted((c for c in contacts if c["base"] == THEIR), key=lambda c: c["row"])
    names = [name for name, size in OWN_BATCHES for _ in range(size)]
    for index, contact in enumerate(own):
        contact["batch"] = names[index] if index < len(names) else ""  # no batch left: the checks report it
        contact["mailbox"] = MAILBOXES[index % len(MAILBOXES)]
    for index, contact in enumerate(their):
        contact["batch"] = THEIR_BATCH
        contact["mailbox"] = MAILBOXES[index % len(MAILBOXES)]
    return own + their


def build_contacts(enriched: Path = ENRICHED, their_base: Path = THEIR_BASE) -> tuple[list[dict], Report]:
    """Read both bases, drop the rows that cannot be sent, assign subjects, batches and mailboxes."""
    own_rows, their_rows = read_rows(enriched), read_rows(their_base)
    contacts, excluded, corrected = [], [], []
    for number, row in enumerate(own_rows, 1):
        reason = skip_reason(row["Персонализация"])
        if reason:
            excluded.append((OWN, number, row["Компания"].strip(), reason))
        else:
            contacts.append(own_contact(number, row))
    for number, row in enumerate(their_rows, 1):
        reason = skip_reason(row["персонализация"], row["язык_письма"])
        if reason:
            excluded.append((THEIR, number, row["company"].strip(), reason))
            continue
        contact = their_contact(number, row)
        contacts.append(contact)
        if contact["email"] != first_email(row["email"]):
            corrected.append(f"{contact['companyName']}: {contact['email']} вместо {first_email(row['email'])}")
    ordered = assign_batches(contacts)
    assign_subject_variants(ordered)
    return ordered, Report(len(own_rows), len(their_rows), excluded, corrected)


def reserve_contacts(path: Path = RESERVE_CSV) -> list[dict]:
    """Rows of the reserve as contacts for the preview only: no batch, no mailbox, the main subject."""
    if not path.exists():
        return []
    contacts = [own_contact(number, row, RESERVE) for number, row in enumerate(read_rows(path), 1)
                if not skip_reason(row["Персонализация"])]
    for contact in contacts:
        contact["subject_variant"] = FIXED
    return contacts


def render(template: str, contact: dict, pick: Pick = longest) -> str:
    """Expand {{RANDOM | a | b}} with `pick`, then substitute the variables of the contact.

    A variable with no value stays as it is, so the check for leftover braces catches it.
    """
    def value(match: re.Match) -> str:
        return contact.get(VARIABLES.get(match.group(1), ""), "") or match.group(0)

    return VARIABLE_RE.sub(value, expand_random(template, pick))


def greeting_pick(contact: dict, step: int) -> Pick:
    """The RANDOM choice pinned to the address and the step: a re-run renders the same greeting."""
    return random.Random(f"{SEED}:{contact['email']}:{step}").choice


def subject_templates(contact: dict, chain: Chain) -> list[str]:
    """Subject templates of email 1 the contact may get: one, or both while the winner is unknown."""
    row = chain.first[contact["letter1_variant"]]
    main, alternative = subject_of(row["тема"]) or "", subject_of(row["тема_AB"])
    if alternative and contact["subject_variant"] == SUBJECT_B:
        return [alternative]
    if alternative and contact["subject_variant"] == WINNER:
        return [main, alternative]
    return [main]


def render_chain(contact: dict, chain: Chain, pick: Pick | None = None,
                 subject_template: str | None = None) -> list[Letter]:
    """The three letters of the contact, variables substituted.

    A contact of a spare variant («а», «б») gets the follow-ups with the no-name greeting of task3_chain.csv.
    `pick` chooses the RANDOM option (default: pinned to the address); `subject_template` overrides the
    subject of email 1 (default: the first of subject_templates(), which is subject A for the later batches).
    """
    first = chain.first[contact["letter1_variant"]]
    first_subject = render(subject_template or subject_templates(contact, chain)[0], contact)
    letters = []
    for row in (first, *chain.followups):
        step = int(row["шаг"])
        in_thread = step > 1 and is_thread_marker(row["тема"])
        if step == 1:
            subject = first_subject
        elif in_thread:
            subject = f"Re: {first_subject}"
        else:
            subject = render(subject_of(row["тема"]) or "", contact)
        template = row["текст"].strip()
        if step > 1 and contact["letter1_variant"] != NAMED_VARIANT and plain_greeting(row):
            template = plain_body(row)  # the same letter, the first line names nobody
        body = render(template, contact, pick or greeting_pick(contact, step))
        letters.append(Letter(step, row["задержка"].strip(), subject, body, in_thread))
    return letters


def worst_case(contact: dict, chain: Chain) -> list[Letter]:
    """Every letter the contact may get, with the longest greeting and each possible subject."""
    return [letter for template in subject_templates(contact, chain)
            for letter in render_chain(contact, chain, pick=longest, subject_template=template)]


def letter_errors(label: str, letter: Letter) -> list[str]:
    """Leftover variables, foreign square brackets and the word limit of one rendered letter."""
    errors = []
    if not letter.subject.strip() or not letter.body.strip():
        errors.append(f"{label}: пустая тема или пустое тело")
    text = f"{letter.subject}\n{letter.body}"
    if "{{" in text or "}}" in text:
        left = sorted(set(re.findall(r"\{\{[^{}]*\}\}", text))) or ["{{ или }}"]
        errors.append(f"{label}: остались переменные {', '.join(left)}")
    rest = text
    for placeholder in SENDER_PLACEHOLDERS:
        rest = rest.replace(placeholder, "")
    if BRACKET_RE.search(rest):
        errors.append(f"{label}: квадратные скобки вне {', '.join(SENDER_PLACEHOLDERS)}")
    if letter.words > LIMIT:
        errors.append(f"{label}: тема + тело = {letter.words} слов, лимит {LIMIT}")
    return errors


def expected_batches(own_count: int) -> dict[str, int]:
    """Sizes the own batches must have: filled in order, 20 / 20 / 20 / 20 / 19 for a base of 99 leads."""
    sizes, left = {}, own_count
    for name, size in OWN_BATCHES:
        sizes[name] = min(size, left)
        left -= sizes[name]
    return sizes


def check_export(contacts: list[dict], chain: Chain) -> list[str]:
    """All checks of the export; an empty list means the files may be written."""
    errors = []
    seen = Counter(contact["email"].lower() for contact in contacts)
    errors += [f"адрес {email} стоит в {count} строках" for email, count in seen.items() if count > 1]

    for contact in contacts:
        label = f"{contact['base']}, строка {contact['row']}, {contact['company']}"
        if not EMAIL_RE.fullmatch(contact["email"]):
            errors.append(f"{label}: адрес «{contact['email']}» не похож на email")
        if contact["letter1_variant"] not in chain.first:
            errors.append(f"{label}: варианта письма 1 «{contact['letter1_variant']}» нет в task3_chain.csv")
            continue
        empty = [name for name in REQUIRED[contact["letter1_variant"]] if not contact[name]]
        if empty:
            errors.append(f"{label}: пустые поля {', '.join(empty)}")
        if contact["base"] == OWN and contact["priority"] not in PRIORITY_ORDER:
            errors.append(f"{label}: приоритет «{contact['priority']}», ожидается A, B или C")
        if not contact["campaign"]:
            errors.append(f"{label}: вертикаль «{contact['vertical']}» не отнесена ни к одной кампании")
        for letter in (*render_chain(contact, chain), *worst_case(contact, chain)):
            errors += letter_errors(f"{label}, письмо {letter.step}", letter)

    # The test compares two subjects of one text: one variant of email 1, and that variant has both subjects.
    in_test = [contact for contact in contacts if contact["subject_variant"] in (SUBJECT_A, SUBJECT_B)]
    tested = sorted({contact["letter1_variant"] for contact in in_test})
    if len(tested) > 1:
        errors.append(f"в A/B-тесте темы строки разных вариантов письма 1 ({', '.join(tested)}): тест сравнивает "
                      "две темы одного текста")
    for variant in tested:
        row = chain.first.get(variant)
        if row and not (subject_of(row["тема"]) and subject_of(row["тема_AB"])):
            errors.append(f"task3_chain.csv: у варианта «{variant}» письма 1 нет второй темы для A/B-теста")

    sizes = Counter(contact["batch"] for contact in contacts)
    own_count = sum(1 for contact in contacts if contact["base"] == OWN)
    for name, size in expected_batches(own_count).items():
        if sizes[name] != size:
            errors.append(f"батч {name}: {sizes[name]} строк, ожидается {size}")
    if sizes[""]:
        places = sum(size for _, size in OWN_BATCHES)
        errors.append(f"без батча осталось {sizes['']} строк: в батчах {OWN_BATCHES[0][0]}–{OWN_BATCHES[-1][0]} "
                      f"только {places} мест")
    if sizes[THEIR_BATCH] > THEIR_BATCH_MAX:
        errors.append(f"батч {THEIR_BATCH}: {sizes[THEIR_BATCH]} строк, максимум {THEIR_BATCH_MAX}")
    for name in sizes:
        boxes = Counter(contact["mailbox"] for contact in contacts if contact["batch"] == name)
        if max(boxes[box] for box in MAILBOXES) - min(boxes[box] for box in MAILBOXES) > 1:
            errors.append(f"батч {name}: ящики загружены неравномерно — {dict(boxes)}")

    split = Counter((contact["vertical"], contact["subject_variant"]) for contact in in_test)
    for vertical in sorted({vertical for vertical, _ in split}):
        if abs(split[vertical, SUBJECT_A] - split[vertical, SUBJECT_B]) > 1:
            errors.append(f"A/B в вертикали «{vertical}»: {split[vertical, SUBJECT_A]} / {split[vertical, SUBJECT_B]}")
    totals = Counter(contact["subject_variant"] for contact in in_test)
    if abs(totals[SUBJECT_A] - totals[SUBJECT_B]) > 1:
        errors.append(f"A/B по всему тесту: {totals[SUBJECT_A]} / {totals[SUBJECT_B]}")
    return list(dict.fromkeys(errors))  # a letter is rendered twice, so an error may repeat


def preview_row(contact: dict, chain: Chain) -> dict:
    """A row of letters_preview.csv: email 1 as the contact gets it."""
    letter = render_chain(contact, chain)[0]
    return {
        "компания": contact["company"],
        "адрес": contact["email"],
        "вариант": contact["letter1_variant"],
        "вариант_темы": contact["subject_variant"],
        "тема": letter.subject,
        "письмо_1": letter.body,
        "слов": letter.words,
    }


def preview_examples(contacts: list[dict]) -> list[tuple[dict, int]]:
    """The contacts shown in preview.md with the number of letters to show.

    Three leads (all three letters; a confirmed trigger; a role mailbox), a row of the reserve in spare
    variant «б» and a row of the reviewers' base in spare variant «а».
    """
    examples, everyone = [], [*contacts, *reserve_contacts()]
    for company, base, variant, trigger, address_type, shown in PREVIEW_EXAMPLES:
        pool = sorted((c for c in everyone if c["base"] == base and c["letter1_variant"] == variant),
                      key=lambda c: c["row"])
        if trigger:
            pool = [c for c in pool if c["trigger"] == trigger] or pool
        if address_type:
            pool = [c for c in pool if c["address_type"] == address_type] or pool
        preferred = [c for c in pool if c["companyName"] == company]
        chosen = [c for c in (preferred or pool) if all(c is not shown_before for shown_before, _ in examples)]
        if chosen:
            examples.append((chosen[0], shown))
    return examples


def example_lines(number: int, contact: dict) -> list[str]:
    """The heading of an example and the lines above the letter: who gets it and why in this variant."""
    variant = contact["letter1_variant"]
    title = f"Лид — {contact['company']}"
    why = VARIANT_NOTES[variant]
    if contact["address_type"] == ROLE_BOX:
        why += f"; {ROLE_BOX_NOTE}"
    if contact["subject_variant"] == WINNER:
        subject = "тема — победитель A/B-теста; пока теста нет, здесь показана тема A"
    else:
        subject = f"тема {contact['subject_variant']} в A/B-тесте"
    sending = f"батч {contact['batch']}, ящик {contact['mailbox']}, {subject}"
    if contact["base"] == THEIR:
        title = f"Строка вашей базы (задание 4), запасной вариант «{variant}» — {contact['company']}"
        why = THEIR_NOTE
        sending = f"батч {contact['batch']}, ящик {contact['mailbox']}, основная тема варианта «{variant}», вне теста"
    elif contact["base"] == RESERVE:
        title = f"Строка резерва, запасной вариант «{variant}» — {contact['company']}"
        why = f"{VARIANT_NOTES[variant]}; {RESERVE_NOTE}"
        sending = "в запуск не идёт"
    return [
        "",
        f"## {number}. {title}",
        "",
        f"- Кому: `{contact['email']}`" + (f" — {ADDRESS_LABELS.get(contact['address_type'], contact['address_type'])}"
                                             if contact["base"] == OWN else ""),
        f"- Почему этот вариант: {why}.",
        f"- Триггер в базе: {contact['trigger']}.",
        f"- Факт взят со страницы: {contact['source']}",
        f"- Отправка: {sending}.",
    ]


def chain_variables(chain: Chain) -> set[str]:
    """Names of the variables the templates use: subjects, bodies and the by-name greetings."""
    cells = []
    for row in (*chain.first.values(), *chain.followups):
        cells += [subject_of(row["тема"]) or "", subject_of(row["тема_AB"]) or "", row["текст"], plain_greeting(row)]
    return {name for cell in cells for name in VARIABLE_RE.findall(expand_random(cell, longest))}


def variables_section(chain: Chain) -> list[str]:
    """A section of preview.md: which column of the import file feeds which variable of the templates."""
    used = chain_variables(chain)
    rename = [f"`{{{{{variable}}}}}` → `{{{{{column}}}}}`" for variable, column in VARIABLES.items()
              if variable != column and variable in used]
    lines = [
        "",
        "## Колонка CSV → переменная шаблона",
        "",
        "Шаблоны писем лежат в `task3_chain.csv`, данные — в `import_instantly.csv`. Сервис рассылок подставляет "
        "в переменную колонку с тем же именем.",
        "",
        "| Колонка `import_instantly.csv` | Переменная в `task3_chain.csv` | Где стоит |",
        "|---|---|---|",
    ]
    lines += [f"| `{column}` | `{{{{{variable}}}}}` | {VARIABLE_PLACES[variable]} |"
              for variable, column in VARIABLES.items() if variable in used]
    if rename:
        lines += ["", f"Перед загрузкой шаблонов в сервис переименовать в них {', '.join(rename)}: в шаблонах эти "
                      "переменные названы по-русски, как в ТЗ, а колонки файла импорта — латиницей. Имена остальных "
                      "переменных совпадают с колонками."]
    return lines


def import_section(contacts: list[dict], report: Report) -> list[str]:
    """The last section of preview.md: what is in import_instantly.csv and what was left out."""
    total = report.own_rows + report.their_rows
    reasons = Counter((base, reason) for base, _, _, reason in report.excluded)
    lines = [
        "",
        "## Что в файле импорта",
        "",
        f"В `import_instantly.csv` строки стоят в порядке отправки. Строк в файле: {len(contacts)} из {total}.",
    ]
    if reasons:
        lines += ["", "В запуск не идут:", ""]
        lines += [f"- {base}, строк: {count} — {reason}." for (base, reason), count in reasons.items()]
    lines += ["", "| Колонка | Что в ней |", "|---|---|"]
    return lines + [f"| {columns} | {note} |" for columns, note in IMPORT_NOTES]


def preview_markdown(contacts: list[dict], chain: Chain, report: Report) -> str:
    """preview.md: the header, the rendered examples and the notes on the import file."""
    placeholders = ", ".join(f"`{placeholder}`" for placeholder in SENDER_PLACEHOLDERS)
    greetings = sorted({plain_greeting(row) for row in chain.followups} - {""})
    plain_note = PLAIN_NOTE.format(plain_greeting=" / ".join(greetings), greeting_column=GREETING_COLUMN)
    header = PREVIEW_HEADER.format(count=len(contacts), placeholders=placeholders, limit=LIMIT,
                                   plain_note=plain_note if greetings else "")
    lines = [header.rstrip("\n")]
    for number, (contact, shown) in enumerate(preview_examples(contacts), 1):
        lines += example_lines(number, contact)
        for letter in render_chain(contact, chain)[:shown]:
            thread = ", ответом в ветку письма 1" if letter.in_thread else ""
            lines += [
                "",
                f"### Письмо {letter.step}",
                "",
                f"**Когда:** {letter.delay}{thread}",
                "",
                f"**Тема:** {letter.subject}",
                "",
                "```",
                letter.body,
                "```",
                "",
                f"Слов: {letter.words} (тема {letter.subject_words} + тело {letter.body_words}).",
            ]
    lines += variables_section(chain)
    lines += import_section(contacts, report)
    return "\n".join(lines) + "\n"


def tally(values: list[str], order: tuple[str, ...] = ()) -> str:
    """«A 20 · B 20 · C 16»: the keys of `order` first, then the rest alphabetically."""
    counts = Counter(values)
    keys = [key for key in order if key in counts] + sorted(key for key in counts if key not in order)
    return " · ".join(f"{key} {counts[key]}" for key in keys)


def print_report(contacts: list[dict], report: Report, chain: Chain) -> None:
    total = report.own_rows + report.their_rows
    steps = 1 + len(chain.followups)
    print("Экспорт кампании. Письма не отправляются: скрипт читает CSV и пишет три файла.\n")
    print(f"Прочитано строк: {ENRICHED.name} — {report.own_rows}, {THEIR_BASE.name} — {report.their_rows}, "
          f"всего {total}.")
    print(f"Не попали в импорт — {len(report.excluded)}:")
    for base, number, company, reason in report.excluded:
        print(f"  - {base}, строка {number}, {company}: {reason}")
    print(f"Строк в импорте: {len(contacts)} = {total} − {len(report.excluded)}. "
          f"Писем в цепочке: {len(contacts)} × {steps} = {len(contacts) * steps}.\n")

    own = [c for c in contacts if c["base"] == OWN]
    in_test = [c for c in own if c["subject_variant"] in (SUBJECT_A, SUBJECT_B)]
    waiting = [c for c in own if c["subject_variant"] == WINNER]
    campaigns = (*OWN_CAMPAIGNS, CAMPAIGN_THEIR)
    print(f"Вариант письма 1: {tally([c['letter1_variant'] for c in contacts])}")
    print(f"Тип адреса лидов: {tally([c['address_type'] for c in own], (PERSONAL_BOX, ROLE_BOX))}")
    print(f"Тема письма 1, A/B-тест на батчах {', '.join(TEST_BATCHES)}: "
          f"{tally([c['subject_variant'] for c in in_test])}, всего {len(in_test)}")
    for vertical in sorted({c["vertical"] for c in in_test}, key=list(CAMPAIGN_BY_VERTICAL).index):
        print(f"  {vertical}: {tally([c['subject_variant'] for c in in_test if c['vertical'] == vertical])}")
    print(f"  вне теста: батчи {' и '.join(LATER_BATCHES)} — {len(waiting)}, тема-победитель; "
          f"батч {THEIR_BATCH} — {len(contacts) - len(own)}, основная тема варианта «{DEFAULT_VARIANT}»")
    print(f"Батчи: {tally([c['batch'] for c in contacts])}")
    print(f"Приоритет: {tally([c['priority'] for c in own], PRIORITY_ORDER)}")
    print(f"Ящики: {tally([str(c['mailbox']) for c in contacts])}")
    print(f"Кампании: {tally([c['campaign'] for c in contacts], campaigns)}")
    print(f"Строк с именем ЛПР: {sum(1 for c in contacts if c['lprName'])} из {len(contacts)}")
    if report.corrected:
        print(f"Адрес взят из «исправленный_email» — {len(report.corrected)}: {'; '.join(report.corrected)}")
    if len(contacts) > len(own):
        print(f"Батч {THEIR_BATCH}: города и часового пояса в {THEIR_BASE.name} нет, колонки city и timezone "
              f"пустые — проставить перед запуском.")
    if contacts:
        top, owner = max(((letter, contact) for contact in contacts for letter in worst_case(contact, chain)),
                         key=lambda pair: pair[0].words)
        by_name = owner["letter1_variant"] == NAMED_VARIANT
        greeting = "с обращением по имени" if by_name else "с приветствием «Добрый день»"
        print(f"Самое длинное письмо: {top.words} слов — письмо {top.step}, {owner['company']} "
              f"({greeting}; лимит {LIMIT}).")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="run the checks and print the report, write nothing")
    parser.add_argument("--out-dir", type=Path, default=EXPORT_DIR, help="directory for the three files")
    args = parser.parse_args()

    chain = load_chain()
    contacts, report = build_contacts()
    print_report(contacts, report, chain)

    errors = check_export(contacts, chain)
    if errors:
        print("\nОШИБКИ (файлы не записаны):")
        print("\n".join(f"  - {error}" for error in errors))
        return 1
    own_count = sum(1 for contact in contacts if contact["base"] == OWN)
    sizes = " / ".join(str(size) for size in expected_batches(own_count).values() if size)
    print("\nПроверки пройдены: фигурных скобок не осталось; из квадратных — только данные отправителя; "
          f"тема + тело каждого письма не длиннее {LIMIT} слов; адреса уникальны; "
          f"батчи {sizes} и до {THEIR_BATCH_MAX} в батче {THEIR_BATCH}; A/B-тест сбалансирован.")
    if args.check:
        print("OK: файлы не записывались (--check)")
        return 0

    args.out_dir.mkdir(parents=True, exist_ok=True)
    write_bytes(args.out_dir / "import_instantly.csv", csv_bytes(contacts, IMPORT_FIELDS))
    write_bytes(args.out_dir / "letters_preview.csv",
                csv_bytes([preview_row(contact, chain) for contact in contacts], PREVIEW_FIELDS))
    write_bytes(args.out_dir / "preview.md", preview_markdown(contacts, chain, report).encode("utf-8"))
    out_dir = args.out_dir.resolve()
    shown = out_dir.relative_to(ROOT) if out_dir.is_relative_to(ROOT) else out_dir
    print(f"OK: записано в {shown}/ — import_instantly.csv и letters_preview.csv, строк в каждом: {len(contacts)}; "
          f"preview.md, примеров: {len(preview_examples(contacts))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
