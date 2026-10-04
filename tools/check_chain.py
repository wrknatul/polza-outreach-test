"""Length and structure check for the task-3 email chain (task3_chain.csv + task3_chain.md).

task3_chain.csv is the single source of the letter texts. The script
  1. counts words in every letter for the worst case and fails when subject + body is over 120
     (the limit from the TZ);
  2. checks the structure the course asks for: an introduction, exactly one CTA paragraph, a two-line
     signature, known variables only, no links in email 1, a declared source for every number;
  3. checks that task3_chain.md shows the same texts, subjects and word counts as the CSV.

The base consists of leads, so the main email 1 is variant «в»: the addressee is greeted by name, and so
are the follow-ups. Variants «б» and «а» are kept for rows that are not leads (a department mailbox: the
reserve and the reviewers' base). For them the column `приветствие_без_имени` holds the line that replaces
the by-name greeting of a follow-up; both versions of a follow-up are checked and counted.

Sources of the numbers are checked by reference, not by line number: task3_chain.md must cite the lesson
and the point of the course on the same line as the number, and that point of the course must contain
the phrase. The second half runs only where the saved course text exists (course/course.txt is not part
of the repository).

Word counting reproduces macOS `wc -w` in a UTF-8 locale, the stricter variant used since the first
version of task3_chain.md: besides ASCII whitespace it breaks a word on the bytes 0x85 and 0xA0, so a
word with «х» (D1 85) or a capital «Р» (D0 A0) counts as two. The count is done in pure Python to give
the same numbers on any OS; on macOS it is cross-checked against the real `wc -w`.

The worst case is measured twice:
  - by contract: every variable takes its contract maximum (a 30-word {{персонализация}}, a 16-word
    {{гипотеза}}, a 3-word {{companyName}}, ...). These numbers go to the CSV columns and to the table
    in task3_chain.md, so they do not move when the base is rebuilt;
  - by the base: the longest {{гипотеза}}, {{персонализация}}, names and job titles are read from
    task1_2_enriched.csv, task2_personalized.csv, task1_reserve.csv and task4_final.csv. Where the base
    holds something longer than the contract, the letters are counted again with it and must still fit.

Usage:
  python tools/check_chain.py           check only; exit code 1 on any failure
  python tools/check_chain.py --write   also refresh the word-count columns of task3_chain.csv
"""
import argparse
import csv
import io
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parent.parent
CHAIN_CSV = ROOT / "task3_chain.csv"
CHAIN_MD = ROOT / "task3_chain.md"
ENRICHED = ROOT / "task1_2_enriched.csv"  # written by tools/enrich_base.py; may not exist yet
BASE = ROOT / "task2_personalized.csv"
THEIR_BASE = ROOT / "task4_final.csv"
RESERVE = ROOT / "task1_reserve.csv"  # companies with a department mailbox only: variants «а» and «б» of email 1
COURSE = ROOT / "course" / "course.txt"  # local copy of the course text; not part of the repository

LIMIT = 120
NO_DATA = "нет данных"
VARIANT_COLUMN = "вариант_письма_1"
NAMED_VARIANT = "в"  # the main email 1: a lead, greeted by name in every letter
GREETING_COLUMN = "приветствие_без_имени"  # the first line of a follow-up for the variants that name nobody
NAME_VARIABLE = "{{firstName}}"
SUBJECT_B_LABEL = "тема B"  # the second subject of an email 1 (the A/B test)
PLAIN_LABEL = "без имени («а», «б»)"  # a follow-up as a department mailbox gets it
CLIENT_LINE_WORDS = 8  # contract maximum of the line that names a client (a placeholder in the markdown)

# Contract maximum of every variable.
STUBS = {
    "персонализация": " ".join(["слово"] * 29 + ["слово."]),  # 30 words, the limit of personalize.py
    "гипотеза": " ".join(["слово"] * 15 + ["слово."]),  # 16 words, the limit of the hypothesis dictionary
    "companyName": "Ezhong Heavy Machinery",
    # a first name with a patronymic; the strict count breaks a word at «х», so this one counts as three words,
    # and a few names of the base do
    "firstName": "Михаил Аркадьевич",
    "lprName": "Пётр Михайлович Образцов",
    "jobTitle": "руководитель отдела продаж",
}
# Where the real values live: variable -> [(file, columns joined with a space, email-1 variant or None)].
# A variant limits the rows to those that really get this variable in the letter.
REAL_VALUES = {
    "персонализация": [(ENRICHED, ("Персонализация",), None), (BASE, ("Персонализация",), None),
                       (RESERVE, ("Персонализация",), None), (THEIR_BASE, ("персонализация",), None)],
    "гипотеза": [(ENRICHED, ("Гипотеза_боли",), None), (RESERVE, ("Гипотеза_боли",), None),
                 (THEIR_BASE, ("Гипотеза_боли",), None)],
    "companyName": [(ENRICHED, ("компания_в_письме",), None), (BASE, ("компания_в_письме",), None),
                    (RESERVE, ("компания_в_письме",), None), (THEIR_BASE, ("компания_в_письме",), None)],
    "firstName": [(ENRICHED, ("Имя", "Отчество"), NAMED_VARIANT)],
    "lprName": [(ENRICHED, ("Имя", "Отчество", "Фамилия"), "б"), (RESERVE, ("Имя", "Отчество", "Фамилия"), "б")],
    "jobTitle": [(ENRICHED, ("должность_в_письме",), "б"), (RESERVE, ("должность_в_письме",), "б")],
}
# Variables each email-1 variant must and must not contain.
VARIANT_VARIABLES = {
    "а": ({"персонализация", "гипотеза"}, {"firstName", "lprName", "jobTitle"}),
    "б": ({"персонализация", "гипотеза", "lprName", "jobTitle"}, {"firstName"}),
    NAMED_VARIANT: ({"персонализация", "гипотеза", "firstName"}, {"lprName", "jobTitle"}),
}


class Source(NamedTuple):
    """Where the course states a number."""

    reference: str  # how task3_chain.md cites it, on the same line as the number
    lesson: int  # 0 is the introduction
    point: int
    quote: str  # the phrase of the course behind the number


# Every number a letter states -> its source in the course.
NUMBER_SOURCES = {
    "7 дней": Source("введение, п. 5", 0, 5, "через 7 дней"),
    "300+ кейсов": Source("введение, п. 5", 0, 5, "+300 кейсов"),
}
IGNORED_TOKENS = ("B2B",)  # a digit inside a term is not a number
FORBIDDEN = ["{{компания}}", "{{имя}}", "продажник", "пилот"]
INTRO_FIRST = "Меня зовут [Имя], я из Polza Agency"
INTRO_NEXT = "Это [Имя] из Polza Agency"
SIGNATURE = ["[Имя Фамилия], [должность]", "Polza Agency"]
OPT_OUT = "Неактуально — ответьте «нет»."

WORD_BREAK = re.compile(rb"[\t\n\x0b\x0c\r \x85\xa0]+")
RANDOM_RE = re.compile(r"\{\{RANDOM\s*\|([^}]*)\}\}")
VARIABLE_RE = re.compile(r"\{\{\s*([^}|]+?)\s*(?:\|[^}]*)?\}\}")
CTA_RE = re.compile(r"\b(?:подскажите|ответьте)\b", re.IGNORECASE)
LINK_RE = re.compile(r"https?://|www\.|\b[\w-]+\.(?:ru|com|io|net|org|рф)\b", re.IGNORECASE)
LIST_LINE_RE = re.compile(r"^\d+\.\s")
LESSON_RE = re.compile(r"^\s*Урок (\d+)\.", re.MULTILINE)  # a lesson heading of the saved course text
MD_SUBJECT_RE = re.compile(r"`(\{\{companyName\}\}: [^`]+)`")
MD_SWAP_RE = re.compile(r"Вместо строки «([^»]+)» вставить «([^»]+)»")
CORE_HEADING_RE = re.compile(r"Письмо [123] —")


class Measured(NamedTuple):
    """Result of counting the chain with one set of variable values."""

    table: list[tuple[str, int, int]]  # (label, subject words, body words)
    errors: list[str]  # structure of the letters and markdown-vs-CSV differences
    over_limit: list[str]  # letters longer than the limit
    fresh: dict[int, dict[str, int]]  # CSV row index -> count columns
    bodies: list[str]  # every letter template that was counted
    top: int  # the longest subject together with the longest body
    top_subject: str
    thread_subject: str  # the thread subject of the variants that name nobody
    named_thread: str  # the thread subject contacts of variant «в» see


def count_words(text: str) -> int:
    """Strict word count: what macOS `wc -w` prints in a UTF-8 locale."""
    return sum(1 for chunk in WORD_BREAK.split(text.encode("utf-8")) if chunk)


def system_wc(text: str) -> int | None:
    """Real `wc -w` on macOS in a UTF-8 locale; None on other systems, where `wc` counts differently."""
    if sys.platform != "darwin" or not shutil.which("wc"):
        return None
    env = dict(os.environ, LC_ALL="en_US.UTF-8")
    out = subprocess.run(["wc", "-w"], input=text.encode("utf-8"), capture_output=True, env=env, check=True)
    return int(out.stdout.split()[0])


def longest(options: list[str]) -> str:
    return max(options, key=count_words)


def shortest(options: list[str]) -> str:
    return min(options, key=count_words)


def expand_random(text: str, pick=longest) -> str:
    """Replace every {{RANDOM | a | b}} with pick([a, b])."""
    return RANDOM_RE.sub(lambda m: pick([o.strip() for o in m.group(1).split("|") if o.strip()]), text)


def render(text: str, values: dict[str, str], pick=longest) -> str:
    """Expand RANDOM and substitute {{name}} for every name in `values`."""
    text = expand_random(text, pick)
    for name, value in values.items():
        text = text.replace("{{" + name + "}}", value)
    return text


def read_rows(path: Path) -> tuple[list[dict], list[str]]:
    reader = csv.DictReader(io.StringIO(path.read_text(encoding="utf-8-sig"), newline=""))
    return list(reader), list(reader.fieldnames or [])


def write_rows(path: Path, rows: list[dict], fields: list[str]) -> None:
    """Write atomically, in the format of the other deliverables: UTF-8 with BOM, CRLF row ends."""
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(b"\xef\xbb\xbf" + out.getvalue().encode("utf-8"))
    tmp.replace(path)


def column_values(path: Path, columns: tuple[str, ...], variant: str | None) -> list[str]:
    """Non-empty values of the joined columns; [] when the file or a column is missing."""
    if not path.exists():
        return []
    rows, fields = read_rows(path)
    if any(c not in fields for c in columns):
        return []
    if variant and VARIANT_COLUMN in fields:
        rows = [r for r in rows if r[VARIANT_COLUMN].strip(" «»\"'") == variant]
    values = (" ".join(r[c].strip() for c in columns if r[c].strip()) for r in rows)
    return [v for v in values if v and v != NO_DATA]


def base_values() -> tuple[dict[str, str], list[str]]:
    """The longer of the contract stub and the longest value in the base, and a report line per variable."""
    values, report = {}, []
    for name, stub in STUBS.items():
        real = [(v, path.name) for path, columns, variant in REAL_VALUES[name]
                for v in column_values(path, columns, variant)]
        line = f"  {{{{{name}}}}}: контракт — {count_words(stub)}"
        if real:
            top, source = max(real, key=lambda pair: count_words(pair[0]))
            values[name] = longest([stub, top])
            line += f"; в базе самое длинное — {count_words(top)} ({source})"
            if values[name] != stub:
                line += " ← длиннее контракта"
        else:
            values[name] = stub
            line += "; в базе значений ещё нет"
        report.append(line)
    return values, report


def subject_of(cell: str) -> str | None:
    """A real subject, or None for an empty cell and for the «(пусто — ответ в ветку…)» marker."""
    cell = cell.strip()
    return None if not cell or cell.startswith("(") else cell


def is_thread_marker(cell: str) -> bool:
    return cell.strip().startswith("(")


def plain_greeting(row: dict) -> str:
    """The no-name greeting of a CSV row; '' when the row has none."""
    return (row.get(GREETING_COLUMN) or "").strip()


def plain_body(row: dict) -> str:
    """A follow-up as a contact of variant «а» or «б» gets it: the first line is the no-name greeting."""
    lines = row["текст"].strip().splitlines()
    return "\n".join([plain_greeting(row), *lines[1:]])


def greets_by_name(text: str) -> bool:
    """True if the first line of a letter holds the name of the addressee."""
    return NAME_VARIABLE in text.strip().splitlines()[0]


def paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]


def text_lines(text: str) -> set[str]:
    return {line.strip() for line in text.splitlines() if line.strip()}


def md_letter_blocks(md: str) -> list[dict]:
    """Untagged fenced blocks of the markdown file with their heading and the nearest «**Тема:**» line."""
    blocks, heading, subject, inside, tagged, buffer = [], "", "", False, False, []
    for line in md.splitlines():
        if line.startswith("```"):
            if inside and not tagged:
                blocks.append({"heading": heading, "subject": subject, "text": "\n".join(buffer).strip()})
            inside, tagged, buffer = not inside, bool(line[3:].strip()), []
        elif inside:
            buffer.append(line)
        elif line.startswith("#"):
            heading, subject = line.lstrip("#").strip(), ""
        elif line.removeprefix("- ").startswith("**Тема:**"):  # a paragraph or a list item
            subject = line.removeprefix("- ").removeprefix("**Тема:**").strip().strip("`")
    return blocks


def md_section(md: str, title: str) -> str:
    """Text of the «## » section whose heading starts with `title`; '' when there is no such section."""
    match = re.search(rf"^## {re.escape(title)}.*?(?=^## |\Z)", md, re.MULTILINE | re.DOTALL)
    return match.group(0) if match else ""


def course_point(course: str, lesson: int, point: int) -> str:
    """Text of one numbered point of a lesson of the saved course; '' when it is not found.

    Lesson 0 is the introduction: everything above «Урок 1.». A point runs from its line «N. …» to the
    line «N+1. …» or to the end of the lesson.
    """
    starts = {int(match.group(1)): match.start() for match in LESSON_RE.finditer(course)}
    if lesson and lesson not in starts:
        return ""
    begin = starts[lesson] if lesson else 0
    block = course[begin:min((pos for pos in starts.values() if pos > begin), default=len(course))]
    head = re.search(rf"^\s*{point}\.\s", block, re.MULTILINE)
    if not head:
        return ""
    tail = re.search(rf"^\s*{point + 1}\.\s", block[head.end():], re.MULTILINE)
    return block[head.start():head.end() + tail.start()] if tail else block[head.start():]


def structure_errors(label: str, step: int, variant: str, body: str, named: bool = False) -> list[str]:
    """Checks one letter template against the rules of the course and of the plan.

    `named` marks a follow-up that greets by name: {{firstName}} is allowed in the first line and only there.
    """
    errors = []
    intro = INTRO_FIRST if step == 1 else INTRO_NEXT
    if intro not in body:
        errors.append(f"{label}: нет представления «{intro}»")
    if body.strip().splitlines()[-2:] != SIGNATURE:
        errors.append(f"{label}: подпись не в две строки «{SIGNATURE[0]}» / «{SIGNATURE[1]}»")

    # The opt-out line is a courtesy, not a second CTA.
    blocks = [p for p in paragraphs(body) if p != OPT_OUT]
    cta = [p for p in blocks if CTA_RE.search(p)]
    if len(cta) != 1:
        errors.append(f"{label}: абзацев с CTA — {len(cta)}, нужен ровно один")
    for block in blocks:
        prose = "\n".join(line for line in block.splitlines() if not LIST_LINE_RE.match(line))
        if "?" in prose and block not in cta:
            errors.append(f"{label}: вопрос вне абзаца с CTA: «{block[:50]}…»")
    if cta and cta[0].count("?") > 1:
        errors.append(f"{label}: в абзаце с CTA больше одного вопроса")

    names = set(VARIABLE_RE.findall(RANDOM_RE.sub("", body)))
    if unknown := names - set(STUBS):
        errors.append(f"{label}: неизвестные переменные {sorted(unknown)}")
    if step == 1:
        need, ban = VARIANT_VARIABLES.get(variant, ({"персонализация"}, set()))
        if missing := need - names:
            errors.append(f"{label}: нет переменных {sorted(missing)}")
        if extra := ban & names:
            errors.append(f"{label}: лишние переменные {sorted(extra)}")
        if LINK_RE.search(body):
            errors.append(f"{label}: в письме 1 есть ссылка")
    else:
        # A follow-up carries no variables; the only exception is the name in the greeting of variant «в».
        if extra := names - ({"firstName"} if named else set()):
            errors.append(f"{label}: переменные {sorted(extra)} допустимы только в письме 1")
        first_line, _, rest = body.strip().partition("\n")
        if named and ("{{firstName}}" not in first_line or "{{firstName}}" in rest):
            errors.append(f"{label}: {{{{firstName}}}} должно стоять в приветствии и только в нём")

    rest = "\n".join(LIST_LINE_RE.sub("", line) for line in body.splitlines())
    for phrase in (*NUMBER_SOURCES, *IGNORED_TOKENS):
        rest = rest.replace(phrase, "")
    if stray := re.findall(r"\S*\d\S*", rest):
        errors.append(f"{label}: числа без строки-источника: {stray}")
    return errors


def greeting_errors(label: str, row: dict, has_named: bool, has_plain: bool) -> list[str]:
    """The no-name greeting column: empty for email 1, replaces the by-name first line of a follow-up."""
    greeting, by_name = plain_greeting(row), greets_by_name(row["текст"])
    if row["шаг"].strip() == "1":
        # every variant of email 1 has its own text, so there is nothing to replace
        return [f"{label}: колонка «{GREETING_COLUMN}» у письма 1 должна быть пустой"] if greeting else []
    if not has_named:
        errors = [f"{label}: приветствие по имени, а варианта «{NAMED_VARIANT}» у письма 1 нет"] if by_name else []
        if greeting:
            errors.append(f"{label}: колонка «{GREETING_COLUMN}» заполнена, а варианта «{NAMED_VARIANT}» "
                          "у письма 1 нет")
        return errors
    errors = []
    if not by_name:
        errors.append(f"{label}: первая строка — не приветствие по имени ({NAME_VARIABLE})")
    if has_plain and not greeting:
        errors.append(f"{label}: в колонке «{GREETING_COLUMN}» нет приветствия для вариантов без имени")
    if greeting and (not RANDOM_RE.fullmatch(greeting.rstrip("!")) or NAME_VARIABLE in greeting):
        errors.append(f"{label}: «{GREETING_COLUMN}» — это ротация RANDOM без имени, а стоит «{greeting}»")
    return errors


def number_source_errors(bodies: list[str], md: str) -> tuple[list[str], bool]:
    """Every number of the letters is cited in the markdown by lesson and point, and the course says it there.

    Returns the errors and whether the saved course text was there to check the phrases against.
    """
    errors = []
    course = COURSE.read_text(encoding="utf-8") if COURSE.exists() else None
    for phrase, source in NUMBER_SOURCES.items():
        if not any(phrase in body for body in bodies):
            continue
        if not any(phrase in line and source.reference in line for line in md.splitlines()):
            errors.append(f"«{phrase}»: в task3_chain.md нет строки с этой цифрой и источником «{source.reference}»")
        if course is not None and source.quote.lower() not in course_point(course, source.lesson, source.point).lower():
            errors.append(f"«{phrase}»: в курсе ({source.reference}) нет фразы «{source.quote}»")
    return errors, course is not None


def csv_letters(rows: list[dict], values: dict[str, str], thread_subject: str,
                named_thread: str) -> tuple[list, list[str], dict, list[str]]:
    """Count every CSV letter.

    Returns table rows, structure errors, fresh count columns per row index and the follow-ups as the
    variants that name nobody get them.
    """
    table, errors, fresh, plain_bodies = [], [], {}, []
    variants = {r["вариант"].strip() for r in rows if r["шаг"].strip() == "1"}
    has_named, has_plain = NAMED_VARIANT in variants, bool(variants - {NAMED_VARIANT})
    for index, row in enumerate(rows):
        step, variant, body = int(row["шаг"]), row["вариант"].strip(), row["текст"]
        label = f"{step} «{variant}»" if variant else str(step)
        by_name = step > 1 and greets_by_name(body)
        errors += structure_errors(f"письмо {label}", step, variant, body, named=by_name)
        errors += greeting_errors(f"письмо {label}", row, has_named, has_plain)
        body_max = count_words(render(body, values))
        main_subject, alt_cell = subject_of(row["тема"]), row["тема_AB"].strip()
        own_thread = named_thread if by_name else thread_subject  # the thread the main text of the row goes to
        subjects = [main_subject or own_thread]
        if is_thread_marker(alt_cell):  # the alternative is a reply in the thread of email 1
            subjects.append(own_thread)
            names = [f"{label}, своя тема", f"{label}, в ветке письма 1"]
        elif alt_cell:
            subjects.append(alt_cell)
            # A follow-up in the thread may get a subject of its own; an email 1 has two subjects for the A/B test.
            names = [label, f"{label}, отдельной темой" if main_subject is None else f"{label}, {SUBJECT_B_LABEL}"]
        else:
            names = [label]
        subject_counts = [count_words(render(subject, values)) for subject in subjects]
        table += [(name, count, body_max) for name, count in zip(names, subject_counts, strict=True)]
        totals = [max(subject_counts) + body_max]

        # The same follow-up for a department mailbox: the no-name greeting, the thread of their email 1.
        if step > 1 and plain_greeting(row):
            plain = plain_body(row)
            plain_label = f"{label} {PLAIN_LABEL}"
            errors += structure_errors(f"письмо {plain_label}", step, "", plain)
            plain_max = count_words(render(plain, values))
            plain_subjects = [thread_subject if subject == own_thread else subject for subject in subjects]
            plain_subject = max(count_words(render(subject, values)) for subject in plain_subjects)
            table.append((plain_label, plain_subject, plain_max))
            plain_bodies.append(plain)
            subject_counts.append(plain_subject)
            totals.append(plain_subject + plain_max)
            body_max = max(body_max, plain_max)
        fresh[index] = {
            "слов": count_words(render(body, {}, shortest)),
            "слов_тело_макс": body_max,
            "слов_тема_макс": max(subject_counts),
            "слов_тема_плюс_тело_макс": max(totals),
        }
    # Variants of email 1 differ only in the greeting and in the CTA paragraph.
    first = {r["вариант"].strip(): paragraphs(r["текст"]) for r in rows if r["шаг"] == "1"}
    main = NAMED_VARIANT if has_named else next(iter(first), "")
    for variant, parts in first.items():
        base = first[main]
        changed = {i for i, (x, y) in enumerate(zip(base, parts, strict=False)) if x != y}
        if len(parts) != len(base) or not changed <= {0, 3}:
            errors.append(f"письмо 1 «{variant}»: отличается от «{main}» не только приветствием и абзацем с CTA")
    return table, errors, fresh, plain_bodies


def markdown_letters(md: str, rows: list[dict], values: dict[str, str], thread_subject: str,
                     named_thread: str) -> tuple[list, list[str], list[str]]:
    """Compare the markdown with the CSV and count the letters that live only in the markdown.

    Returns table rows, errors and the bodies of the markdown-only letters.
    """
    table, errors, extra_bodies = [], [], []
    bodies = [r["текст"].strip() for r in rows]
    blocks = md_letter_blocks(md)
    core = [b for b in blocks if CORE_HEADING_RE.match(b["heading"])]  # the sections of emails 1-3
    md_lines = set().union(set(), *(text_lines(b["text"]) for b in blocks))
    core_lines = set().union(set(), *(text_lines(b["text"]) for b in core))
    csv_lines = set().union({plain_greeting(r) for r in rows} - {""}, *(text_lines(body) for body in bodies))
    errors += [f"строки письма нет в task3_chain.md: «{line[:60]}»" for line in sorted(csv_lines - md_lines)]
    errors += [f"строки из task3_chain.md нет в CSV: «{line[:60]}»" for line in sorted(core_lines - csv_lines)]

    # The section of a follow-up names the greeting the variants without a name get.
    for row in rows:
        step, greeting = row["шаг"].strip(), plain_greeting(row)
        if step != "1" and greeting and greeting not in md_section(md, f"Письмо {step} —"):
            errors.append(f"в разделе «Письмо {step}» task3_chain.md нет приветствия «{greeting}»")

    for block in blocks:
        text = block["text"]
        if block in core or text.splitlines()[-1:] != SIGNATURE[-1:]:
            continue  # a letter of the CSV, or a fragment of a letter
        step = 1 if INTRO_FIRST in text else 2
        by_name = greets_by_name(text)
        errors += structure_errors(block["heading"], step, "", text, named=by_name and step > 1)
        subject = subject_of(block["subject"]) or (named_thread if by_name else thread_subject)
        table.append((block["heading"], count_words(render(subject, values)), count_words(render(text, values))))
        extra_bodies.append(text)

    # A one-sentence swap described in the markdown («Вместо строки «…» вставить «…»») applies to email 2.
    letter2 = next((r for r in rows if r["шаг"] == "2"), None)
    for old, new in MD_SWAP_RE.findall(md):
        if letter2 is None or old not in letter2["текст"]:
            errors.append(f"замена из task3_chain.md: строки «{old}» нет в письме 2")
            continue
        if "[" in new:  # a placeholder the team fills in: counted at its contract maximum
            if f"до {CLIENT_LINE_WORDS} слов" not in md:
                errors.append(f"замена из task3_chain.md: не сказано, что строка — до {CLIENT_LINE_WORDS} слов")
            new = " ".join(["слово"] * (CLIENT_LINE_WORDS - 1) + ["слово."])
        by_name = greets_by_name(letter2["текст"])
        versions = [(letter2["текст"], named_thread if by_name else thread_subject, by_name)]
        if plain_greeting(letter2):
            versions.append((plain_body(letter2), thread_subject, False))
        counted = []
        for text, subject, named in versions:
            swapped = text.replace(old, new)
            errors += structure_errors("письмо 2 с заменой строки", 2, "", swapped, named=named)
            counted.append((count_words(render(subject, values)), count_words(render(swapped, values))))
            extra_bodies.append(swapped)
        table.append(("2, со строкой про клиента", *max(counted, key=sum)))  # the longer of the two versions
    return table, errors, extra_bodies


def measure(rows: list[dict], md: str, values: dict[str, str]) -> Measured:
    """Count every letter of the CSV and of the markdown with the given variable values."""
    def subjects(variant: str | None = None) -> list[str]:
        """Subjects of email 1: of every variant, or of one."""
        return sorted({s for r in rows if r["шаг"] == "1" and variant in (None, r["вариант"].strip())
                       for s in (subject_of(r["тема"]), subject_of(r["тема_AB"])) if s})

    def thread(options: list[str]) -> str:
        """A reply in the thread shows «Re: » plus the subject of email 1; the longest one is the worst case."""
        return "Re: " + max(options, key=lambda s: count_words(render(s, values)))

    csv_subjects = {s for r in rows for s in (subject_of(r["тема"]), subject_of(r["тема_AB"])) if s}
    if not subjects():
        raise SystemExit("ОШИБКА: у письма 1 нет темы")
    plain = sorted({s for r in rows if r["шаг"] == "1" and r["вариант"].strip() != NAMED_VARIANT
                    for s in (subject_of(r["тема"]), subject_of(r["тема_AB"])) if s})
    thread_subject = thread(plain or subjects())
    named_thread = thread(subjects(NAMED_VARIANT) or subjects())
    md_subjects = set(MD_SUBJECT_RE.findall(md))
    errors = [f"темы «{s}» из CSV нет в task3_chain.md" for s in sorted(csv_subjects - md_subjects)]

    table, csv_errors, fresh, plain_bodies = csv_letters(rows, values, thread_subject, named_thread)
    md_table, md_errors, extra_bodies = markdown_letters(md, rows, values, thread_subject, named_thread)
    table += md_table
    errors += csv_errors + md_errors
    over_limit = [f"{label}: тема + тело = {s + b} слов, лимит {LIMIT}" for label, s, b in table if s + b > LIMIT]

    # Any subject of the document with any body: the widest guarantee.
    every_subject = sorted(csv_subjects | md_subjects | {thread_subject, named_thread})
    top_subject = max(every_subject, key=lambda s: count_words(render(s, values)))
    top = count_words(render(top_subject, values)) + max(b for _, _, b in table)
    bodies = [r["текст"] for r in rows] + plain_bodies + extra_bodies
    return Measured(table, errors, over_limit, fresh, bodies, top, top_subject, thread_subject, named_thread)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write", action="store_true", help="refresh the word-count columns of task3_chain.csv")
    args = parser.parse_args()

    rows, fields = read_rows(CHAIN_CSV)
    md = CHAIN_MD.read_text(encoding="utf-8")

    # --- worst case by contract: the numbers of the CSV columns and of the markdown table ---
    contract = measure(rows, md, STUBS)
    errors = contract.errors + contract.over_limit
    if contract.top > LIMIT:
        errors.append(f"самая длинная тема с самым длинным телом — {contract.top} слов, лимит {LIMIT}")
    source_errors, course_checked = number_source_errors(contract.bodies, md)
    errors += source_errors
    for path in (CHAIN_CSV, CHAIN_MD):
        text = path.read_text(encoding="utf-8-sig")
        errors += [f"{path.name}: осталось «{word}»" for word in FORBIDDEN if word in text]

    print(f"Худший случай по контракту. Тема письма в ветке: «{contract.named_thread}»,")
    print(f"для вариантов без имени — «{contract.thread_subject}»\n")
    print("| Письмо | Тема | Тело | Всего | Запас |")
    print("|---|---|---|---|---|")
    for label, subject_count, body_count in contract.table:
        total = subject_count + body_count
        line = f"| {label} | {subject_count} | {body_count} | **{total}** | {LIMIT - total} |"
        print(line + ("" if total <= LIMIT else "  ← ПРЕВЫШЕНИЕ"))
        if line not in md:
            errors.append(f"строки таблицы длины нет в task3_chain.md: {line}")
    print(f"\nСамая длинная тема («{contract.top_subject}») с самым длинным телом: {contract.top} слов")

    # The «Слов» line in the section of a letter shows the worst case of every CSV row of that letter.
    for i, counts in contract.fresh.items():
        total, step = counts["слов_тема_плюс_тело_макс"], rows[i]["шаг"].strip()
        if f"**{total}**" not in md_section(md, f"Письмо {step} —"):
            errors.append(f"в разделе «Письмо {step}» {CHAIN_MD.name} нет числа слов **{total}**")

    # Word-count columns of the CSV: refreshed with --write, otherwise a stale value is an error.
    stale = [(i, column) for i, counts in contract.fresh.items() for column, value in counts.items()
             if rows[i][column] != str(value)]
    if stale and args.write:
        for i, counts in contract.fresh.items():
            rows[i].update({column: str(value) for column, value in counts.items()})
        write_rows(CHAIN_CSV, rows, fields)
        print(f"Колонки счёта обновлены: {CHAIN_CSV.name}")
    else:
        errors += [f"строка {i + 2} CSV: колонка «{column}» = {rows[i][column] or 'пусто'}, по расчёту "
                   f"{contract.fresh[i][column]} (запустите с --write)" for i, column in stale]

    # --- worst case by the base: the longest real values must fit the limit too ---
    values, report = base_values()
    spare_pair = contract.top  # any subject of the document with any body, counted with the longest real values
    print("\nСамые длинные значения переменных, слов (строгий счёт):")
    print("\n".join(report))
    if values == STUBS:
        print("В базе нет значений длиннее контракта: таблица выше — худший случай и для реальных строк.")
    else:
        real = measure(rows, md, values)
        spare_pair = real.top
        print("С самыми длинными значениями из базы:")
        for (label, s, b), (_, s0, b0) in zip(real.table, contract.table, strict=True):
            if s + b != s0 + b0:
                over = "" if s + b <= LIMIT else "  ← ПРЕВЫШЕНИЕ"
                print(f"  {label}: {s + b} слов (по контракту {s0 + b0}){over}")
        errors += [f"с данными базы — {e}" for e in real.over_limit]
        if real.top > LIMIT:  # a warning only: the letters themselves fit, a spare A/B subject may not
            print(f"  ВНИМАНИЕ: самая длинная тема документа («{real.top_subject}») с самым длинным телом "
                  f"и самыми длинными значениями из базы: {real.top} при лимите {LIMIT} слов. Письма таблицы это "
                  "не затрагивает; такой строке эту тему с этим телом не ставить")

    # The pure-Python count must agree with the real `wc -w` wherever that tool counts the same way.
    samples = [render(t, values) for t in [*contract.bodies, contract.top_subject, contract.thread_subject]]
    samples += [*values.values(), *STUBS.values()]
    if system_wc("") is not None:
        differ = [t for t in samples if system_wc(t) != count_words(t)]
        print(f"\nСверка с системным `wc -w` (UTF-8): текстов {len(samples)}, расхождений {len(differ)}")
        if differ:
            errors.append(f"счёт расходится с системным `wc -w` в {len(differ)} текстах")

    if not source_errors and course_checked:
        print(f"Источники цифр: ссылка на урок и пункт стоит в {CHAIN_MD.name}, фраза найдена в этом пункте курса")
    elif not source_errors:
        print(f"Источники цифр: ссылка на урок и пункт стоит в {CHAIN_MD.name}; текста курса рядом нет "
              "(в репозиторий он не входит), фразы с ним не сверялись")

    if errors:
        print("\nОШИБКИ:")
        print("\n".join(f"  - {e}" for e in errors))
        return 1
    # What the run has really checked: every letter of the table with its own subject, twice (the contract maximum
    # and the longest values of the base). The pair «any subject of the document with any body» is an error only
    # by contract; with the values of the base it is reported, not enforced.
    print(f"\nOK: тема + тело не длиннее {LIMIT} слов у каждого письма таблицы с его темой — по контракту "
          "и с самыми длинными значениями из базы; структура, источники цифр и task3_chain.md сходятся с CSV")
    if spare_pair > LIMIT:
        print(f"Не гарантировано: любая тема документа с любым телом. По контракту эта пара укладывается в лимит "
              f"({contract.top}), с самыми длинными значениями из базы — нет ({spare_pair}), см. ВНИМАНИЕ выше")
    else:
        print(f"Любая тема документа с любым телом тоже укладывается в лимит: слов не больше {spare_pair}")
    print("Не считается: ответ в ветку письма 1, которое ушло с запасной темой из раздела «Варианты для A/B» "
          "(«Re:» добавляет слово)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
