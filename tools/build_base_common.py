#!/usr/bin/env python3
"""Shared validators for the Task 1 base.

Two kinds of rows are validated here, and nothing is invented for either: the
pages are re-fetched on every run and a row is kept only if all checks pass.

Leads (tools/build_base_all.py, the base itself) — a named decision maker with
a direct work address, see validate_leads():
  0. the site's robots.txt does not close `источник`: a closed page is never
     requested, and a lead whose source page is closed is rejected;
  1. the address literally appears on `источник` (raw HTML, entities decoded)
     and is printed there as visible text: an address that stands only inside
     a mailto link («Написать письмо») does not show whose mailbox it is;
  2. the surname stands within ROLE_WINDOW characters of the address in the
     text of that page (a mailto link counts at the place where it stands),
     and so does the job title — the short one of the letter or the full one:
     a page that prints the address but not the role does not show a decision
     maker;
  3. the address is not a generic mailbox (info@, sales@ ...), and a mail
     domain that differs from the site is accepted only when the same page
     prints one more address on it (the site itself uses that domain);
  4. the mail domain has a non-null MX record (`dig +short MX <domain>`);
  5. the sales-signal phrase (`signal_check`) is on `sales_signal_url`.

Company mailboxes (tools/build_base_A.py / build_base_B.py, the reserve) —
validate(): the address is on `email_source`, MX, the signal phrase, and a
non-generic contact_role is labelled next to the address (ROLE_EVIDENCE).

robots.txt (RFC 9309) is asked once per host before its first page, see
robots_refusal(): the requests carry a browser User-Agent, but they are made by
a script, so the «User-agent: *» group applies, and no other. Redirects are
followed by hand, so the rules are asked again at every hop. The matcher, the
reader of the answer to «/robots.txt» and the spelling of an address that is
checked and requested are the ones of personalize.py (RobotsRules, read_robots,
request_url): the three fetchers of the project cannot disagree. No robots.txt
(4xx, an empty answer, an HTML page instead of it) closes nothing; a robots.txt
that could not be read (5xx, 429, no answer, redirects that lead nowhere) closes
the host for this run.

Network notes (all were seen in practice, 2026-10-03):
  * the sites may be unreachable from the local network; POLZA_SOCKS=host:port
    sends every page request through a SOCKS proxy that also resolves the
    names (curl --socks5-hostname). DNS queries for MX (dig) go direct;
  * the proxy's resolver does not know every name (aitis.pro): every second
    attempt resolves the name over DNS-over-HTTPS and hands the proxy an
    address (curl --socks5 + --doh-url);
  * the local DNS resolver sometimes times out for whole TLDs (.app, .work,
    some .com), so without a proxy curl falls back to DNS-over-HTTPS and dig
    to public resolvers instead of silently dropping a valid row;
  * some pages mix encodings (okdesk.ru: UTF-8 markup + a cp1251 JS comment),
    so strict decoding fails both ways; we fall back to lossy UTF-8.
"""
import csv
import html
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import unquote, urlparse, urlsplit

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# robots.txt: the matcher, the reader of the answer and the spelling of an address live in personalize.py. That
# part of it needs the standard library only, so the tools still run without third-party packages.
import personalize as P  # noqa: E402
FIELDS = ["company", "site", "contact_role", "email", "email_source",
          "segment", "sales_signal", "city"]
# The lead base: the company-level columns above, the contact fields, and the
# three name columns of tools/merge_lpr.py at the end (where that script keeps them).
LEAD_FIELDS = FIELDS + ["компания_в_письме", "тип_адреса", "дата_страницы", "оговорка",
                        "имя_ЛПР", "должность_ЛПР", "источник_имени"]
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124 Safari/537.36")
DOH_URL = "https://1.1.1.1/dns-query"
PUBLIC_RESOLVERS = ("1.1.1.1", "8.8.8.8")
PROXY_ENV = "POLZA_SOCKS"  # optional: host:port of a SOCKS5 proxy for the companies' sites
GENERIC_ROLE_PREFIX = "Общий адрес"
ROLE_WINDOW = 350  # max distance (chars of visible text) between label and email

# Address types of a lead, best first (the base is sorted in this order).
NAMED_BOX, ROLE_BOX, FREE_BOX = "именной", "ящик должности ЛПР", "личный ящик на бесплатном домене"
ADDRESS_TYPES = (NAMED_BOX, ROLE_BOX, FREE_BOX)
# A mailbox with one of these names belongs to a department, whoever's card it is printed in.
GENERIC_LOCAL_PARTS = frozenset({
    "info", "sales", "sale", "zakaz", "office", "mail", "hello", "contact", "contacts", "admin", "support",
    "marketing", "pr", "hr", "manager", "order", "orders", "secretar", "secretary", "reception", "opt", "post",
})
FREE_MAIL_DOMAINS = frozenset({
    "mail.ru", "bk.ru", "inbox.ru", "list.ru", "internet.ru", "yandex.ru", "ya.ru", "yandex.com", "rambler.ru",
    "gmail.com", "outlook.com", "hotmail.com", "icloud.com", "yahoo.com",
})
MAILTO_RE = re.compile(r"""(?is)<a\b[^>]*?href\s*=\s*["']\s*mailto:([^"'?>\s]+)[^>]*>""")
ADDRESS_RE = re.compile(r"[a-z0-9._%+-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+")
META_RE = re.compile(r"""(?is)<meta\b[^>]*?\bcontent\s*=\s*(?:"([^"]*)"|'([^']*)')[^>]*>""")
TITLE_RE = re.compile(r"(?is)<title[^>]*>(.*?)</title>")

# A page the site's robots.txt closes is never requested; the phrase is how such a page is named in a problem.
ROBOTS_CLOSED = "закрыта в robots.txt"
ROBOTS_DOWN = "robots.txt сайта не получен"  # 5xx, 429 or no answer: the host is closed for this run (RFC 9309)
NO_ANSWER = "не ответила"
MAX_REDIRECTS = 5
# curl follows the redirects of «/robots.txt» itself: the same number of hops as the other fetchers, web addresses only
FOLLOW_ARGS = ["-L", "--max-redirs", str(MAX_REDIRECTS), "--proto-redir", "=http,https"]
MIN_PAGE_BYTES = 1000  # a shorter 2xx answer is usually a stub: it is asked again

_cache = {}
_errors = {}
_robots = {}  # scheme://host -> (RobotsRules or None, why the file could not be read)


def proxy_args(env=None):
    """curl arguments for the optional SOCKS proxy: POLZA_SOCKS=127.0.0.1:1080.

    host:port -> --socks5-hostname (the proxy resolves the names too); a full
    URL (socks5h://host:port) is passed to --proxy as is. Empty -> no proxy.
    """
    value = (os.environ if env is None else env).get(PROXY_ENV, "").strip()
    if not value:
        return []
    return ["--proxy", value] if "://" in value else ["--socks5-hostname", value]


def decode(raw):
    """Decode page bytes: strict UTF-8, then cp1251, then lossy UTF-8."""
    for enc in ("utf-8", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def curl_command(attempt=0, max_time=40, env=None):
    """The curl call of every request (without the URL). Redirects are not followed here, see get_page().

    «-g»: the address is requested as it is written. Without it curl reads «[1-3]» and «{a,b}» in an address
    as a list of addresses and requests every one of them — addresses robots.txt was never asked about.
    «-q» (it must come first): the user's ~/.curlrc is not read. A «location» line there would make curl follow
    the redirects of a page itself, past the check of every hop.

    Without a proxy the system resolver is used first and DNS-over-HTTPS from the 2nd attempt on: a flaky
    local resolver must not drop a valid row. With a SOCKS5 proxy the proxy resolves the name on the even
    attempts; on the odd ones the name is resolved over DNS-over-HTTPS and the proxy gets an address,
    because the proxy's resolver does not know every name.
    """
    proxy = proxy_args(env)
    cmd = ["curl", "-q", "-s", "-g", "--connect-timeout", "10", "--max-time", str(max_time),
           "-A", UA, "-H", "Accept-Language: ru-RU,ru;q=0.9"]
    if not proxy:
        return cmd + (["--doh-url", DOH_URL] if attempt else [])
    if attempt % 2 and proxy[0] == "--socks5-hostname":
        return cmd + ["--socks5", proxy[1], "--doh-url", DOH_URL]
    if attempt % 2 and proxy[1].startswith("socks5h://"):
        return cmd + ["--proxy", "socks5://" + proxy[1].removeprefix("socks5h://"), "--doh-url", DOH_URL]
    return cmd + proxy


# robots.txt: nothing of it is implemented here. The names below are the ones of personalize.py, kept so that
# the tools and their tests can go on calling them from this module.
ROBOTS_BOMS = P.ROBOTS_BOMS
robots_key = P.robots_key
robots_match = P.robots_match


class RobotsRules(P.RobotsRules):
    """The matcher of personalize.py for a client with a browser User-Agent: such a client has no robot's name,
    so the only group that applies is «User-agent: *» («User-agent: Mozilla» is somebody else's group).
    verdict() names the rule that decided."""

    def __init__(self, text, user_agent=UA):
        super().__init__(text, user_agent)


def _request(url, attempts, max_time, pause, follow=False):
    """One URL: (http code, body bytes, redirect target, why it failed).

    `follow` lets curl follow redirects itself (robots.txt only), over http and https and nothing else: a
    redirect to «ftp://» ends the request, like in the other fetchers. An answer counts at once when it is a
    redirect or a 2xx page of at least MIN_PAGE_BYTES; anything else is asked again, and the last answer
    is returned (some small sites intermittently hang on connect or answer with a stub).
    """
    code, raw, target, why = "", b"", "", ""
    for attempt in range(attempts):
        if attempt:
            time.sleep(pause)
        cmd = curl_command(attempt, max_time) + (FOLLOW_ARGS if follow else [])
        res = subprocess.run(cmd + ["-w", "\n%{http_code} %{redirect_url}", url], capture_output=True, check=False)
        raw, _, tail = res.stdout.rpartition(b"\n")
        code, _, target = tail.decode("ascii", "replace").strip().partition(" ")
        why = "" if res.returncode == 0 else f"curl: код {res.returncode}"
        if not why and ((code.startswith("3") and target) or (code.startswith("2") and len(raw) >= MIN_PAGE_BYTES)
                        or (follow and code.startswith(("2", "4")) and code != "429")):
            break
    return code, raw, target.strip(), why


def robots_of(url):
    """(rules, why) for the site of `url`, asked once per scheme and host.

    What the answer means is decided by personalize.read_robots(), the reader every fetcher of the project
    uses. rules is None and why is '' when the site has no robots.txt (4xx, an empty answer, an HTML page
    instead of the file): nothing is closed. A file that could not be read (5xx, 429, no answer, redirects
    that do not end in a file) gives why = ROBOTS_DOWN: the host is treated as closed until the next run.
    curl follows the redirects of «/robots.txt» itself (RFC 9309, 2.3.1.2), at most MAX_REDIRECTS of them.
    """
    parts = urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    if origin not in _robots:
        code, raw, target, why = _request(origin + "/robots.txt", attempts=3, max_time=20, pause=1, follow=True)
        # an answer curl did not finish (a timeout, a broken stream) is no answer, whatever status came first
        status = int(code) if code.isdigit() and not why else 0
        answer = P.read_robots(status, {"location": target} if target else {}, raw, UA, why)
        _robots[origin] = (answer.rules, f"{ROBOTS_DOWN} ({answer.why})" if answer.state == "closed" else "")
    return _robots[origin]


def robots_summary():
    """One line about the robots.txt files read in this run, for the report."""
    states = list(_robots.values())
    down = sum(1 for _, why in states if why)
    with_rules = sum(1 for rules, _ in states if rules is not None)
    no_file = len(states) - with_rules - down
    return (f"robots.txt: сайтов {len(states)} — с правилами {with_rules}, без файла {no_file}, "
            f"файл не получен {down}")


def robots_refusal(url):
    """'' if the site's robots.txt lets the page be requested, else why not.

    «закрыта в robots.txt сайта (Disallow: /contacts/*)» — a rule closes the page;
    «robots.txt сайта не получен (HTTP 503)» — the rules are unknown today, so nothing is requested.
    """
    rules, why = robots_of(url)
    if why or rules is None:
        return why
    allowed, rule = rules.verdict(url)
    return "" if allowed else f"{ROBOTS_CLOSED} сайта ({rule})"


def get_page(url, attempts=4, max_time=40, pause=2, accept_short=True):
    """(html, '') for a page that answered 2xx, else ('', why).

    robots.txt is asked before every request, the targets of redirects included: a closed page is never
    requested. The address is first brought to the spelling curl would send (personalize.request_url: no
    «/../», no «%2E»), and that spelling is both checked and requested, at every hop; only http(s) addresses
    are requested. An error page («доступ ограничен», 403) is not the company's page. A 2xx answer shorter
    than MIN_PAGE_BYTES is taken after the last attempt only when `accept_short`.
    """
    for _ in range(MAX_REDIRECTS + 1):
        url = P.request_url(url)
        if not url.startswith(("http://", "https://")):  # «ftp://» from a redirect, a value that is not an address
            return "", "адрес нельзя открыть (не http и не https)"
        refusal = robots_refusal(url)
        if refusal:
            return "", refusal
        code, raw, target, why = _request(url, attempts, max_time, pause)
        if why:
            return "", why
        if code.startswith("3") and target:
            url = target
            continue
        if not code.startswith("2"):
            return "", f"HTTP {code}"
        if len(raw) < MIN_PAGE_BYTES and not accept_short:
            return "", "пустой ответ"
        return decode(raw), ""
    return "", f"больше {MAX_REDIRECTS} перенаправлений"


def fetch(url):
    """Fetch a page and return decoded HTML ('' when there is none).

    Up to 4 attempts with a short connect timeout. Why a page gave nothing — it is closed in robots.txt
    (and was not requested), or it did not answer — is kept for the report, see fetch_error().
    """
    if url in _cache:
        return _cache[url]
    text, why = get_page(url)
    if not text:
        _errors[url] = why or "пустой ответ"
    _cache[url] = text
    return text


def fetch_error(url):
    """Why fetch(url) returned nothing: «HTTP 403», «curl: код 28» (timeout), closed in robots.txt; '' if it did."""
    return _errors.get(url, "")


def no_page(url, what="страница", why=None):
    """The problem line of a page that gave no text: closed by robots.txt (never requested) or no answer."""
    why = fetch_error(url) if why is None else why
    if ROBOTS_CLOSED in why:
        return f"{what} {url} {why} — не открывалась"
    return f"{what} {url} {NO_ANSWER}" + (f" ({why})" if why else "")


def fetch_all(urls, threads=6):
    """Warm the page cache: hosts in parallel, the pages of one host one after another."""
    by_host = {}
    for url in sorted(set(urls)):
        by_host.setdefault(host_of(url), []).append(url)
    with ThreadPoolExecutor(threads) as pool:
        list(pool.map(lambda group: [fetch(u) for u in group], by_host.values()))


def visible_text(page):
    """Strip scripts/styles/tags, unescape entities, collapse whitespace."""
    page = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", page)
    page = re.sub(r"(?s)<[^>]+>", " ", page)
    return re.sub(r"\s+", " ", html.unescape(page).replace("\xa0", " "))


def contact_text(page):
    """Visible text in which every mailto link also shows its address.

    A person's card often prints the address only as a link («Написать
    письмо»); the address then stands in the text where that link stands.
    """
    return visible_text(MAILTO_RE.sub(lambda m: f"{m.group(0)} {html.unescape(unquote(m.group(1)))} ", page))


def signal_text(page):
    """Text a visitor or a search engine reads: visible text, <title>, meta descriptions."""
    extra = [m.group(1) for m in TITLE_RE.finditer(page)]
    extra += [m.group(1) or m.group(2) or "" for m in META_RE.finditer(page)]
    return visible_text(page) + " " + re.sub(r"\s+", " ", html.unescape(" ".join(extra)).replace("\xa0", " "))


def fold(text):
    """Lowercase, «ё» = «е», single spaces: how names and phrases are compared."""
    return re.sub(r"\s+", " ", text.lower().replace("ё", "е").replace("\xa0", " ")).strip()


def host_of(value):
    """Lowercase ASCII host of a URL or an email address, without the leading www.

    A Cyrillic host is converted to its IDNA form, so «технотранс.рф» and
    xn--80ajybdmjbd1a.xn--p1ai compare equal.
    """
    if "@" in value and "//" not in value:
        host = value.rsplit("@", 1)[1]
    else:  # a bare «example.ru» (their_base.csv) is a host too
        host = urlparse(value if "//" in value else "//" + value).hostname or ""
    host = host.lower().removeprefix("www.")
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host


def same_site(a, b):
    """True if two hosts are the same site (equal, or one is a subdomain of the other)."""
    return bool(a and b) and (a == b or a.endswith("." + b) or b.endswith("." + a))


def has_mx(domain):
    """Return (ok, records): ok if the domain has at least one non-null MX.

    Tries the system resolver first, then public resolvers if it gave no
    answer (a timeout prints ';;' lines, not records).
    """
    records = []
    for server in (None,) + PUBLIC_RESOLVERS:
        cmd = ["dig", "+short", "+time=5", "+tries=2", "MX", domain]
        if server:
            cmd.append("@" + server)
        out = subprocess.run(cmd, capture_output=True, text=True).stdout
        records = [ln.strip() for ln in out.splitlines()
                   if ln.strip() and not ln.startswith(";")]
        if records:
            break
    return any(not r.endswith(" .") for r in records), records


def _email_re(email):
    # The address must not be part of a longer one (sales@ vs presales@).
    return re.compile(r"(?<![\w.+-])" + re.escape(email.lower()) + r"(?![\w-])")


def email_on_page(email, page):
    """True if the exact address is in the raw HTML (entities decoded)."""
    rx = _email_re(email)
    return bool(rx.search(page.lower()) or rx.search(html.unescape(page).lower()))


def role_is_labelled(email, label, page):
    """True if `label` is within ROLE_WINDOW chars of the email in visible text."""
    vis = visible_text(page).lower()
    label = label.lower()
    for m in _email_re(email).finditer(vis):
        lo, hi = max(0, m.start() - ROLE_WINDOW), m.end() + ROLE_WINDOW
        if label in vis[lo:hi]:
            return True
    return False


def name_distance(email, surname, page):
    """Characters between the surname and the address where they stand closest, None if never within ROLE_WINDOW.

    The text is contact_text(): a mailto link counts where it stands.
    """
    text, surname = fold(contact_text(page)), fold(surname)
    best = None
    for m in _email_re(email).finditer(text):
        lo = max(0, m.start() - ROLE_WINDOW - len(surname))
        before = text.rfind(surname, lo, m.start())
        after = text.find(surname, m.end(), m.end() + ROLE_WINDOW + len(surname))
        for gap in ((m.start() - before - len(surname)) if before >= 0 else None,
                    (after - m.end()) if after >= 0 else None):
            if gap is not None and gap <= ROLE_WINDOW and (best is None or gap < best):
                best = gap
    return best


def email_is_printed(email, page):
    """True if the address stands in the visible text of the page, not only inside a mailto link or the markup."""
    return bool(_email_re(email).search(fold(visible_text(page))))


def title_distance(email, titles, page):
    """Characters between the job title and the address where they stand closest, None if never within ROLE_WINDOW.

    `titles` are the spellings of one title (the short one of the letter, the full one of the site); the closest counts.
    """
    gaps = [gap for gap in (name_distance(email, title, page) for title in titles if title.strip()) if gap is not None]
    return min(gaps) if gaps else None


def site_uses_mail_domain(email, page):
    """True if the page prints one more address on the mail domain of `email`."""
    domain = host_of(email)
    seen = {m.group(0) for m in ADDRESS_RE.finditer(html.unescape(page).lower())}
    return any(host_of(other) == domain for other in seen - {email.lower()})


def signal_on_page(phrase, page):
    """True if the sales-signal phrase is in the text of the page (see signal_text)."""
    return bool(phrase.strip()) and fold(phrase) in fold(signal_text(page))


def check_lead(row, fetch_page=None, mx=None):
    """Problems of one lead row (empty list = the row is a lead), plus what was measured.

    Returns (problems, facts): facts = {"gap": chars between surname and address, "title_gap": the same for the
    job title, "mx": first MX record}. `fetch_page` and `mx` can be replaced in tests; by default they hit the network.
    """
    fetch_page, mx = fetch_page or fetch, mx or has_mx
    email, surname = row["email"].strip(), row["Фамилия"].strip()
    problems, facts = [], {"gap": None, "title_gap": None, "mx": ""}
    local, domain = email.lower().rsplit("@", 1) if "@" in email else ("", "")
    if not ADDRESS_RE.fullmatch(email.lower()):
        return [f"адрес «{email}» записан с ошибкой"], facts
    if row["тип_адреса"] not in ADDRESS_TYPES:
        problems.append(f"тип_адреса «{row['тип_адреса']}» — нужно одно из: {', '.join(ADDRESS_TYPES)}")
    if local in GENERIC_LOCAL_PARTS:
        problems.append(f"{local}@ — общий ящик, лидом не считается")
    if (domain in FREE_MAIL_DOMAINS) != (row["тип_адреса"] == FREE_BOX):
        problems.append("бесплатный почтовый домен и тип_адреса не согласованы")
    if not surname or fold(surname) not in fold(row["имя_ЛПР"]):
        problems.append("фамилии нет в имя_ЛПР")
    if not same_site(host_of(row["site"]), host_of(row["источник"])):
        problems.append(f"источник {row['источник']} не на сайте компании {row['site']}")
    page = fetch_page(row["источник"])
    if not page:
        problem = no_page(row["источник"])
        # A person's page the site closes to robots is not a source: the lead is rejected, not postponed.
        problems.append(problem + ": лид с такой страницы не берётся" if ROBOTS_CLOSED in problem else problem)
    else:
        if not email_on_page(email, page):
            problems.append("адреса нет на странице-источнике")
        else:
            if not email_is_printed(email, page):
                problems.append("адрес стоит только в ссылке mailto: видимым текстом страница его не печатает, "
                                "чей это ящик — по странице не видно")
            facts["gap"] = name_distance(email, surname, page)
            facts["title_gap"] = title_distance(email, (row["должность_в_письме"], row["должность_ЛПР"]), page)
            if facts["gap"] is None:
                problems.append(f"фамилия стоит дальше {ROLE_WINDOW} знаков от адреса")
            elif facts["title_gap"] is None:
                problems.append(f"должности «{row['должность_в_письме'] or row['должность_ЛПР']}» нет рядом с адресом "
                                f"(в пределах {ROLE_WINDOW} знаков): по странице не видно, что это ЛПР")
        if (domain not in FREE_MAIL_DOMAINS and not same_site(host_of(email), host_of(row["site"]))
                and not site_uses_mail_domain(email, page)):
            problems.append(f"почтовый домен {domain} не совпадает с сайтом, "
                            f"и других адресов на нём страница не печатает")
    mx_ok, records = mx(domain)
    facts["mx"] = records[0] if records else ""
    if not mx_ok:
        problems.append(f"у домена {domain} нет MX")
    signal_page = fetch_page(row["sales_signal_url"])
    if not signal_page:
        problems.append(no_page(row["sales_signal_url"], "страница сигнала"))
    elif not signal_on_page(row["signal_check"], signal_page):
        problems.append(f"фразы «{row['signal_check']}» нет на {row['sales_signal_url']}")
    return problems, facts


def lead_to_base_row(row):
    """One row of task1_base.csv (LEAD_FIELDS) from a validated lead."""
    return {
        "company": row["company"], "site": row["site"],
        "contact_role": f"{row['должность_ЛПР']} — {row['тип_адреса']}",
        "email": row["email"], "email_source": row["источник"],
        "segment": row["segment"], "sales_signal": f"{row['sales_signal']} — {row['sales_signal_url']}",
        "city": row["city"], "компания_в_письме": row["компания_в_письме"],
        "тип_адреса": row["тип_адреса"], "дата_страницы": row["дата_страницы"], "оговорка": row["оговорка"],
        "имя_ЛПР": row["имя_ЛПР"], "должность_ЛПР": row["должность_ЛПР"], "источник_имени": row["источник"],
    }


def validate_leads(rows, fetch_page=None, mx=None, label_width=34):
    """Validate lead rows in the order given; return (kept_rows, report_lines, dropped).

    kept_rows are the source rows that passed (see lead_to_base_row for the
    base layout); dropped = [(row, problems)].
    """
    kept, report, dropped = [], [], []
    for row in rows:
        problems, facts = check_lead(row, fetch_page, mx)
        if problems:
            dropped.append((row, problems))
            report.append(f"DROP     {row['company']}: " + "; ".join(problems))
        else:
            kept.append(row)
            report.append(f"OK       {row['company'][:label_width]:{label_width}s} {row['email']:34s} "
                          f"{row['тип_адреса']:18s} фамилия–адрес: {facts['gap']:>3} зн. "
                          f"должность–адрес: {facts['title_gap']:>3} зн. MX={facts['mx'] or '-'}")
    return kept, report, dropped


def validate(candidates, role_evidence, target, label_width=40):
    """Validate candidates in priority order; return (kept_rows, report_lines).

    candidates: tuples (company, site, contact_role, email, email_source,
    segment, signal_text, signal_url, signal_check, city).
    role_evidence: {email: label phrase} for every non-generic contact_role.
    """
    kept, report = [], []
    for (company, site, role, email, src, segment,
         sig_text, sig_url, sig_check, city) in candidates:
        if len(kept) >= target:
            report.append(f"RESERVE  {company}")
            continue
        page = fetch(src)
        email_ok = email_on_page(email, page)
        mx_ok, mx = has_mx(email.split("@", 1)[1])
        signal_ok = sig_check.lower() in visible_text(fetch(sig_url)).lower()
        if role.startswith(GENERIC_ROLE_PREFIX):
            role_ok = True
        else:
            label = role_evidence.get(email)
            role_ok = bool(label) and role_is_labelled(email, label, page)
        if email_ok and mx_ok and signal_ok and role_ok:
            kept.append({
                "company": company, "site": site, "contact_role": role,
                "email": email, "email_source": src, "segment": segment,
                "sales_signal": f"{sig_text} — {sig_url}", "city": city,
            })
            report.append(f"OK       {company[:label_width]:{label_width}s} "
                          f"{email:28s} MX={mx[0] if mx else '-'}")
        else:
            report.append(f"DROP     {company}: email_on_page={email_ok} "
                          f"mx={mx_ok} signal={signal_ok} role_labelled={role_ok}")
    return kept, report


def write_csv(rows, out, fields=FIELDS):
    """Write rows with the fixed Task 1 header (UTF-8)."""
    with Path(out).open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
