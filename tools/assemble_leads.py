#!/usr/bin/env python3
"""Assemble task1_leads.csv — the source list of the Task 1 base — from the checked research.

A lead is a named decision maker (owner / chief executive, commercial director,
head of sales, head of marketing or business development) whose direct work
address the company itself prints next to the name on its own site. Role
mailboxes printed in the person's card count but are labelled; info@, sales@,
zakaz@ and the like do not make a lead.

Input (working material, not part of the repository):
  leads/*.checked.csv  research rows with a second-pass verdict; only verdict OK is used;
  leads/curation.csv   the hand decisions for every OK row: `статус` («в базе», a spare
                       contact of the same company, or «запас: <why>»), the vertical, the
                       short brand for the letter, the name split into parts, the short
                       job title, the department phone, the caveat, the literal phrase of
                       the sales signal. A company of the old base keeps its company-level
                       data (segment, signal, city) from tools/build_base_A.py / _B.py.
Output:
  task1_leads.csv      one row per company, best address type first («именной», then
                       «ящик должности ЛПР», then «личный ящик на бесплатном домене»).

Nothing is fetched and nothing is guessed here: the script only selects, checks the
form of the hand-filled cells and sorts. tools/build_base_all.py then re-validates
every row on the live pages and writes the base.

Usage: python3 tools/assemble_leads.py [--check]
  --check  validate and report only, write nothing
Exit code: 0 — done, 1 — the input has mistakes (each is printed as ОШИБКА),
2 — there is no leads/ research (a clone of the public repository).
"""
import csv
import re
import sys
from collections import Counter

from build_base_common import ADDRESS_TYPES, ROOT, host_of, write_csv

LEADS_DIR = ROOT / "leads"
CURATION = LEADS_DIR / "curation.csv"
OUT = ROOT / "task1_leads.csv"
THEIR_BASE = ROOT / "their_base.csv"
IN_BASE = "в базе"
NO_DATE = "страница без даты"
NO_RESEARCH = ("нет leads/*.checked.csv: рабочие файлы поиска лидов в публичный репозиторий не входят, "
               "готовый список — task1_leads.csv")
MAX_BRAND_WORDS = 3   # {{companyName}} in the subject of the letter
MAX_TITLE_WORDS = 3   # the short job title, see tools/enrich_base.py
ISO_DATE_RE = re.compile(r"^(20\d\d)-(0[1-9]|1[0-2])-\d\d\b")
PHONE_RE = re.compile(r"^[+\d][\d\s()\-]{8,}\d( \(доб\. [\d, ]+\))?$")
VERTICAL_ORDER = [
    "B2B SaaS", "Интеграторы и автоматизация", "B2B-маркетинг", "Юридические услуги", "Аудит и консалтинг",
    "Подбор руководителей", "Инжиниринг и промбезопасность", "Промоборудование", "Промдистрибуция",
    "Оптовая дистрибуция", "Упаковка", "Стройматериалы", "Коммерческая техника", "Логистика", "Логистика ВЭД",
]
FIELDS = ["company", "компания_в_письме", "site", "city", "segment", "sales_signal", "sales_signal_url",
          "signal_check", "имя_ЛПР", "должность_ЛПР", "Имя", "Отчество", "Фамилия", "должность_в_письме",
          "email", "тип_адреса", "источник", "дата_страницы", "дата_источника_имени", "обращаться_по_имени",
          "телефон", "оговорка", "batch"]


def read_csv(path):
    """Rows of a UTF-8 CSV with or without BOM."""
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def detail(segment, vertical=""):
    """The research segment as the detail part of «Вертикаль: детали» (no second «: », lowercase start)."""
    head, sep, tail = segment.strip().partition(": ")
    text = tail if sep and head.lower() == vertical.lower() else segment.strip().replace(": ", " — ")
    if len(text) > 1 and text[0].isupper() and text[1].islower():
        text = text[0].lower() + text[1:]
    return text


def source_date(page_date):
    """«ГГГГ-ММ» when the page itself is dated, else NO_DATE (a footer year or a server header is not a date)."""
    found = ISO_DATE_RE.match(page_date.strip())
    return f"{found.group(1)}-{found.group(2)}" if found else NO_DATE


def page_date(text):
    """The research note about the page date, with one wording for «no date on the page»."""
    text = text.strip()
    return "на странице не указана" if text in ("", "не указана") else text


def build_row(lead, hand):
    """One row of task1_leads.csv from a research row and its curation row."""
    vertical = hand["вертикаль"]
    return {
        "company": lead["company"], "компания_в_письме": hand["компания_в_письме"], "site": lead["site"].rstrip("/"),
        "city": hand["город"] or lead["city"],
        "segment": hand["segment"] or f"{vertical}: {detail(lead['segment'], vertical)}",
        "sales_signal": hand["sales_signal"] or lead["sales_signal"],
        "sales_signal_url": hand["sales_signal_url"] or lead["sales_signal_url"],
        "signal_check": hand["signal_check"],
        "имя_ЛПР": lead["имя_ЛПР"], "должность_ЛПР": hand["должность_ЛПР"] or lead["должность_ЛПР"],
        "Имя": hand["Имя"], "Отчество": hand["Отчество"], "Фамилия": hand["Фамилия"],
        "должность_в_письме": hand["должность_в_письме"],
        "email": lead["email_ЛПР"], "тип_адреса": lead["тип_адреса"], "источник": lead["источник"],
        "дата_страницы": page_date(lead["дата_страницы"]),
        "дата_источника_имени": source_date(lead["дата_страницы"]), "обращаться_по_имени": "да",
        "телефон": hand["телефон"], "оговорка": hand["оговорка"], "batch": hand["batch"],
    }


def check_row(row):
    """Mistakes in the hand-filled cells of one row (empty list if clean)."""
    company, problems = row["company"], []
    words = row["имя_ЛПР"].split()
    for field in ("Имя", "Фамилия"):
        if not row[field]:
            problems.append(f"{company}: пустое «{field}»")
    for field in ("Имя", "Отчество", "Фамилия"):
        if row[field] and row[field] not in words:
            problems.append(f"{company}: {field} «{row[field]}» нет в «{row['имя_ЛПР']}»")
    brand = row["компания_в_письме"]
    if not brand or len(brand.split()) > MAX_BRAND_WORDS or any(ch in brand for ch in "«»\""):
        problems.append(f"{company}: компания_в_письме «{brand}» — нужно 1–{MAX_BRAND_WORDS} слова без кавычек")
    title = row["должность_в_письме"]
    if not title or len(title.split()) > MAX_TITLE_WORDS:
        problems.append(f"{company}: должность_в_письме «{title}» — нужно 1–{MAX_TITLE_WORDS} слова")
    if row["тип_адреса"] not in ADDRESS_TYPES:
        problems.append(f"{company}: тип_адреса «{row['тип_адреса']}»")
    if row["segment"].split(":", 1)[0].strip() not in VERTICAL_ORDER:
        problems.append(f"{company}: вертикали «{row['segment'].split(':', 1)[0]}» нет в VERTICAL_ORDER")
    if row["телефон"] and not PHONE_RE.match(row["телефон"]):
        problems.append(f"{company}: телефон «{row['телефон']}» не похож на номер")
    for field in ("signal_check", "sales_signal", "sales_signal_url", "источник", "city"):
        if not row[field].strip():
            problems.append(f"{company}: пустое «{field}»")
    return problems


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    files = sorted(LEADS_DIR.glob("*.checked.csv"))
    if not files or not CURATION.exists():
        print(NO_RESEARCH)
        return 2
    hand = {(r["batch"], r["company"], r["email"]): r for r in read_csv(CURATION)}
    rows, skipped, problems = [], Counter(), []
    for path in files:
        batch = path.name.removesuffix(".checked.csv")
        for lead in read_csv(path):
            if lead["verdict"] != "OK":
                skipped["вердикт проверки — не OK"] += 1
                continue
            decision = hand.get((batch, lead["company"], lead["email_ЛПР"]))
            if decision is None:
                problems.append(f"{batch}: «{lead['company']}» ({lead['email_ЛПР']}) нет в leads/curation.csv")
            elif decision["статус"] != IN_BASE:
                skipped[decision["статус"].split(":", 1)[0]] += 1
            else:
                rows.append(build_row(lead, decision))
    for row in rows:
        problems += check_row(row)
    # One lead per company; no company of the organisers' base (Task 4).
    seen = {}
    for row in rows:
        for key in {host_of(row["site"]), row["email"].lower()}:
            if key in seen:
                problems.append(f"повтор {key}: «{seen[key]}» и «{row['company']}»")
            seen[key] = row["company"]
    theirs = {host_of(v) for r in read_csv(THEIR_BASE) for v in (r["site"], r["email"])} - {""}
    for row in rows:
        for domain in {host_of(row["site"]), host_of(row["email"])} & theirs:
            problems.append(f"{row['company']}: домен {domain} есть в their_base.csv")
    if problems:
        print("\n".join("ОШИБКА  " + p for p in problems))
        return 1

    rows.sort(key=lambda r: (ADDRESS_TYPES.index(r["тип_адреса"]),
                             VERTICAL_ORDER.index(r["segment"].split(":", 1)[0].strip())))
    if "--check" not in argv:
        write_csv(rows, OUT, FIELDS)
    kinds = Counter(r["тип_адреса"] for r in rows)
    print(f"{len(rows)} лидов -> {OUT.name}" + (" (проверка, файл не записан)" if "--check" in argv else ""))
    print("тип адреса:", " · ".join(f"{k} {kinds[k]}" for k in ADDRESS_TYPES if kinds[k]))
    print("не взято:", " · ".join(f"{k} {n}" for k, n in skipped.items()) or "—")
    return 0


if __name__ == "__main__":
    sys.exit(main())
