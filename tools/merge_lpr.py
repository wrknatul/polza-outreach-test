#!/usr/bin/env python3
"""Attach decision-maker names (task1_lpr.csv) to the Task 1 base and the Task 2 table.

task1_lpr.csv holds at most one person per company: the name and the job title
as the company itself publishes them on its own website, plus the URL of that
page. Nothing else about a person is stored (no personal emails, phones or
photos). A company whose site names no suitable person is simply absent from
the file: its name cells stay empty and `contact_role` remains the fallback.

The script is offline and idempotent. It rewrites the three columns from
task1_lpr.csv on every run. tools/build_base_all.py writes task1_base.csv
with the same three columns and task1_lpr.csv next to it, so on the base this
run changes nothing; in task2_personalized.csv it puts the columns after
`contact_role`. Run it after personalize.py has rebuilt that file.

Usage: python3 tools/merge_lpr.py [--check]
  --check  validate and report only, write nothing
"""
import csv
import io
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
NAMES = ROOT / "task1_lpr.csv"
LPR_FIELDS = ["имя_ЛПР", "должность_ЛПР", "источник_имени"]
# (file, column after which the name columns go; None = append at the end)
TARGETS = [(ROOT / "task1_base.csv", None), (ROOT / "task2_personalized.csv", "contact_role")]
BOM = b"\xef\xbb\xbf"
SCHEMES = ("https://", "http://")
PHONE_RE = re.compile(r"\d[\d\s()\-]{5,}\d")  # 7+ digits in a row, separators allowed


def host(url):
    """Lowercase ASCII host of a URL without the leading www (a Cyrillic host in its IDNA form)."""
    name = (urlparse(url).hostname or "").lower().removeprefix("www.")
    try:
        return name.encode("idna").decode("ascii")
    except UnicodeError:
        return name


def same_site(site, source):
    """True if `source` is on the company's own domain (same host, or one is a subdomain of the other)."""
    a, b = host(site), host(source)
    return bool(a and b) and (a == b or a.endswith("." + b) or b.endswith("." + a))


def read_csv(path):
    """Return (rows, fieldnames, has_bom); the BOM is remembered so the file is written back the same way."""
    raw = path.read_bytes()
    has_bom = raw.startswith(BOM)
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""))
    return list(reader), list(reader.fieldnames), has_bom


def write_csv(path, rows, fields, has_bom):
    """Write atomically, keeping the original encoding (BOM or not) and CRLF line ends."""
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes((BOM if has_bom else b"") + out.getvalue().encode("utf-8"))
    os.replace(tmp, path)


def load_names(sites):
    """Read task1_lpr.csv and return ({company: row}, problems). `sites` is {company: site URL} of the base."""
    rows, _, _ = read_csv(NAMES)
    names, problems = {}, []
    for row in rows:
        company = row["company"]
        if company not in sites:
            problems.append(f"{company}: no such company in the base")
            continue
        if company in names:
            problems.append(f"{company}: more than one person")
        for field in LPR_FIELDS:
            if not row[field].strip():
                problems.append(f"{company}: empty {field}")
        if "@" in row["имя_ЛПР"] + row["должность_ЛПР"] or PHONE_RE.search(row["имя_ЛПР"] + row["должность_ЛПР"]):
            problems.append(f"{company}: an email or a phone number in the name or title")
        # Four sites of the base have no https at all, so http:// is a valid source too.
        if not row["источник_имени"].startswith(SCHEMES) or not same_site(sites[company], row["источник_имени"]):
            problems.append(f"{company}: source {row['источник_имени']} is not on the company site {sites[company]}")
        names[company] = row
    return names, problems


def place(fields, after):
    """Column order with the name columns right after `after` (or at the end when `after` is None)."""
    base = [f for f in fields if f not in LPR_FIELDS]
    at = len(base) if after is None else base.index(after) + 1
    return base[:at] + LPR_FIELDS + base[at:]


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    base_rows, _, _ = read_csv(TARGETS[0][0])
    names, problems = load_names({r["company"]: r["site"] for r in base_rows})
    for problem in problems:
        print("PROBLEM ", problem)
    if problems:
        return 1
    for path, after in TARGETS:
        rows, fields, has_bom = read_csv(path)
        for row in rows:
            person = names.get(row["company"], {})
            for field in LPR_FIELDS:
                row[field] = person.get(field, "")
        named = sum(1 for row in rows if row["имя_ЛПР"])
        if named != len(names):
            print(f"PROBLEM  {path.name}: {named} rows matched, {len(names)} names in {NAMES.name}")
            return 1
        if "--check" not in argv:
            write_csv(path, rows, place(fields, after), has_bom)
        print(f"{path.name}: {named} of {len(rows)} rows have a name")
    return 0


if __name__ == "__main__":
    sys.exit(main())
