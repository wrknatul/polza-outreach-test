#!/usr/bin/env python3
"""Build task1_base.csv: every lead of task1_leads.csv, re-validated on the live pages.

The base consists of leads — a named decision maker with a direct work address
that the company prints next to the name on its own site. For every row of
task1_leads.csv (tools/assemble_leads.py) the pages are fetched again and the
row is kept only if (see build_base_common.check_lead):
  * the site's robots.txt does not close `источник` (a closed page is never
    requested: such a row is rejected with the Disallow rule as the reason);
  * the address is printed on `источник` as visible text (not only inside a
    mailto link), within 350 characters of the surname and of the job title;
  * it is not a generic mailbox, and a mail domain other than the site's is one
    the same page uses for another address;
  * the mail domain has MX;
  * the sales-signal phrase is on its page.
Then the kept rows are checked together: no duplicate site domains or emails,
no domain shared with their_base.csv (the organisers' base for Task 4) or with
task1_reserve.csv (a company is either a lead or a reserve row), at least
MIN_ROWS rows.

A row whose page did not answer is not a lead today: it is dropped and listed,
and it comes back on a later run. If more than half of the rows fail this way,
the network (or the proxy) is down: nothing is written, exit code 3. A page that
robots.txt closes is not «no answer»: the row is dropped for good.

Network: GET requests to the companies' own pages and their robots.txt (through
POLZA_SOCKS=host:port when the variable is set) and DNS queries for MX. Nothing
is sent to anybody.

Usage: python3 tools/build_base_all.py [--out PATH]
  --out PATH  write only the validated base to PATH (a re-validation run);
              without it task1_base.csv, task1_lpr.csv and lpr/part_1.csv are
              rewritten together, so the row numbers cannot drift apart.

Exit code: 0 — done, 1 — the kept rows have problems (each is printed as PROBLEM),
2 — there is no task1_leads.csv, 3 — more than half of the pages did not answer.

The company mailboxes of the first version of the base (tools/build_base_A.py,
tools/build_base_B.py) are kept as a reserve in task1_reserve.csv.
"""
import csv
import sys
from collections import Counter
from pathlib import Path

from build_base_common import (ADDRESS_TYPES, LEAD_FIELDS, NO_ANSWER, ROBOTS_CLOSED, ROOT, fetch_all, host_of,
                               lead_to_base_row, robots_summary, validate_leads, write_csv)

MIN_ROWS = 50  # Task 1 asks for at least 50 companies
LEADS = ROOT / "task1_leads.csv"
OUT = ROOT / "task1_base.csv"
NAMES = ROOT / "task1_lpr.csv"
THEIR_BASE = ROOT / "their_base.csv"
RESERVE = ROOT / "task1_reserve.csv"
LPR_DIR = ROOT / "lpr"
LPR_PART = LPR_DIR / "part_1.csv"
NAME_FIELDS = ["company", "имя_ЛПР", "должность_ЛПР", "источник_имени"]
# The layout tools/enrich_base.py reads (lpr/part_*.csv).
LPR_FIELDS = ["row", "company", "имя_ЛПР", "должность_ЛПР", "источник_имени", "note", "Имя", "Отчество", "Фамилия",
              "должность_в_письме", "email_ЛПР_на_странице", "дата_источника_имени", "обращаться_по_имени",
              "телефон", "примечание_контакта"]

def read_leads(path=None):
    """Rows of task1_leads.csv."""
    with Path(path or LEADS).open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def domains_of(path, columns):
    """Hosts of the sites and mail domains in the given columns of a CSV (empty set if the file is not there)."""
    if not Path(path).exists():
        return set()
    with Path(path).open(encoding="utf-8-sig", newline="") as fh:
        return {host_of(r[c]) for r in csv.DictReader(fh) for c in columns if r.get(c)} - {""}


def check_merged(rows, min_rows=MIN_ROWS, their_base=None, reserve=None):
    """Return a list of problems in the kept rows (empty if clean)."""
    problems = []
    seen = {}
    for row in rows:
        for key in {host_of(row["site"]), row["email"].lower()}:
            if key in seen and seen[key] != row["company"]:
                problems.append(f"duplicate {key}: {seen[key]} / {row['company']}")
            seen.setdefault(key, row["company"])
    theirs = domains_of(their_base or THEIR_BASE, ("site", "email"))
    spare = domains_of(reserve or RESERVE, ("site",))  # free-mail domains of the reserve are not companies
    for row in rows:
        for d in sorted({host_of(row["site"]), host_of(row["email"])} & theirs):
            problems.append(f"overlaps their_base.csv: {row['company']} ({d})")
        if host_of(row["site"]) in spare:
            problems.append(f"overlaps task1_reserve.csv: {row['company']} ({host_of(row['site'])})")
    if len(rows) < min_rows:
        problems.append(f"only {len(rows)} rows, need >= {min_rows}")
    return problems


def network_is_down(total, dropped):
    """True if more than half of the rows were dropped because a page did not answer."""
    silent = sum(1 for _, problems in dropped if any(NO_ANSWER in p for p in problems))
    return total > 0 and silent * 2 > total


def lpr_row(number, row):
    """One row of lpr/part_1.csv (the research layout of tools/enrich_base.py) from a validated lead."""
    return {
        "row": number, "company": row["company"], "имя_ЛПР": row["имя_ЛПР"], "должность_ЛПР": row["должность_ЛПР"],
        "источник_имени": row["источник"],
        "note": f"адрес в карточке ЛПР: {row['тип_адреса']}; дата страницы: {row['дата_страницы']}",
        "Имя": row["Имя"], "Отчество": row["Отчество"], "Фамилия": row["Фамилия"],
        "должность_в_письме": row["должность_в_письме"], "email_ЛПР_на_странице": row["email"],
        "дата_источника_имени": row["дата_источника_имени"], "обращаться_по_имени": row["обращаться_по_имени"],
        "телефон": row["телефон"], "примечание_контакта": row["оговорка"],
    }


def write_pipeline_inputs(kept, out=None, names=None, lpr_part=None):
    """Write the base and the two files tools/merge_lpr.py and tools/enrich_base.py read; return a problem or ''."""
    out, names, lpr_part = out or OUT, names or NAMES, lpr_part or LPR_PART
    stale = sorted(p.name for p in lpr_part.parent.glob("part_*.csv") if p != lpr_part)
    if stale:
        return (f"в {lpr_part.parent.name}/ лежат файлы прежней базы ({', '.join(stale)}): "
                f"перенесите их, иначе enrich_base.py прочитает строки дважды")
    write_csv([lead_to_base_row(r) for r in kept], out, LEAD_FIELDS)
    write_csv([{f: r[f] if f != "источник_имени" else r["источник"] for f in NAME_FIELDS} for r in kept],
              names, NAME_FIELDS)
    lpr_part.parent.mkdir(exist_ok=True)
    write_csv([lpr_row(n, r) for n, r in enumerate(kept, 1)], lpr_part, LPR_FIELDS)
    return ""


def tally(rows, key, order=None):
    """«value N · value N», in `order` if given, else most frequent first."""
    counts = Counter(key(r) for r in rows)
    keys = [k for k in order if k in counts] if order else [k for k, _ in counts.most_common()]
    return " · ".join(f"{k} {counts[k]}" for k in keys)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    out = Path(argv[argv.index("--out") + 1]) if "--out" in argv else None
    if not LEADS.exists():
        print(f"нет {LEADS.name}: сначала tools/assemble_leads.py")
        return 2
    leads = read_leads()
    # Warm the page cache (hosts in parallel); the checks below then read from it.
    fetch_all(r[f] for r in leads for f in ("источник", "sales_signal_url"))
    kept, report, dropped = validate_leads(leads)
    print("\n".join(report))
    silent = [row["company"] for row, problems in dropped if any(NO_ANSWER in p for p in problems)]
    closed = [row["company"] for row, problems in dropped if any(ROBOTS_CLOSED in p for p in problems)]
    print(f"\n== лидов: {len(leads)}, прошли проверку: {len(kept)}, отброшено: {len(dropped)} "
          f"(из них страница не ответила: {len(silent)}, закрыта в robots.txt: {len(closed)})")
    print(robots_summary())
    if network_is_down(len(leads), dropped):
        print(f"СТОП: не ответили страницы у {len(silent)} строк из {len(leads)} — больше половины. Похоже, нет сети "
              f"или прокси (POLZA_SOCKS). Ничего не записано.")
        return 3
    problems = check_merged(kept)
    for p in problems:
        print("PROBLEM ", p)
    if out is not None:
        write_csv([lead_to_base_row(r) for r in kept], out, LEAD_FIELDS)
        print(f"written {len(kept)} rows -> {out}")
    elif not problems:
        problem = write_pipeline_inputs(kept)
        if problem:
            print("PROBLEM ", problem)
            return 1
        print(f"written {len(kept)} rows -> {OUT.name}, {NAMES.name}, {LPR_PART.relative_to(ROOT)}")
    else:
        print("ничего не записано: сначала исправьте PROBLEM")
    print("тип адреса:", tally(kept, lambda r: r["тип_адреса"], ADDRESS_TYPES))
    print("вертикали:", tally(kept, lambda r: r["segment"].split(":", 1)[0].strip()))
    print("города:", tally(kept, lambda r: r["city"]))
    print("партии:", tally(kept, lambda r: r.get("batch", "")))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
