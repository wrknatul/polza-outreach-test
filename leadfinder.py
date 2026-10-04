#!/usr/bin/env python3
"""
leadfinder.py - a small lead-finding tool of our own: ICP filters in, decision-maker
contacts with evidence out. It walks the steps of the course lesson on building a base
(ICP -> company search -> decision-maker contacts -> validation -> export) on one source
only: what a company prints on its own site.

Pipeline:
  1. ICP config (JSON, or YAML when PyYAML is installed): segments / keywords,
     cities, wanted roles, exclusions, limits, discovery sources.
  2. Discovery adapters return "company + site + why it matches":
       seeds      - a CSV of sites;
       expocentr  - exhibitor lists of Expocentre exhibitions (a generic
                    catalog adapter, other catalogs subclass it);
       search     - Serper API, only when SERPER_API_KEY is set (search-engine
                    HTML is never scraped).
  3. Crawl of the company's OWN site only: contacts / team / management /
     department / about pages, ranked by URL segments, link text and
     sitemap.xml, within a page budget. robots.txt is respected, one request
     per second per site, pages are cached on disk for a limited time.
  4. Extraction of person blocks: name + title + email are paired by DOM
     proximity first (same card / table row / list item / dt-dd pair) and by
     reading order second. The address type is classified (именной / ящик
     должности ЛПР / общий ящик / бесплатный домен) and the verbatim evidence
     fragment is kept.
  5. Validation: syntax, domain belongs to the site, MX, not a generic box,
     the role is a decision maker from the ICP, distance name <-> address,
     freshness of the page, published refusals, not a byline / testimonial.
  6. Optional LLM adjudication (--llm claude) for ambiguous and weak blocks:
     the model only picks among already extracted candidates and must quote
     the page; an answer that is not on the page is dropped.
  7. Output: companies.csv (what discovery returned), leads.csv, no_lead.csv,
     summary.json / summary.md, state.json (resume). `--forget <domain|email>`
     removes a contact from every result directory and the cache and keeps its hash.

Hard rules (see LEADFINDER.md):
  * only the company's own site is crawled; a redirect is followed step by step and never
    to another site (a move of the company itself to a similar domain is the one exception,
    and it is written down); on shared suffixes (*.tilda.ws, *.nnov.ru) the site is the host;
  * robots.txt is checked for every URL that is requested, redirect targets included, and there
    is no switch for it; an unreachable robots.txt (5xx, timeout) closes the host (RFC 9309).
    The redirects of robots.txt itself are followed to wherever the file lives, another site
    included: its rules apply to the host that was asked, and nothing but the file is requested
    there. One request per second per site, counted from the end of the previous request;
  * a lead is a name, a title and an address printed together; an address is never built
    from a pattern or from loose words, and a mailbox is never probed (no SMTP);
  * a department box is not a lead, and neither is an address that agrees neither with the
    person's name nor with a decision maker's role;
  * no personal phone numbers: «Телефон» holds only the company's general landline (header,
    footer or a block that names nobody), a number from a person's card is never stored, and
    mobile numbers are masked in the evidence fragment;
  * an ICP exclusion holds after a site move (the same name in another zone, a redirect to a
    listed domain) and for the mail domain the site uses;
  * the evidence date is the day the page was fetched, cached pages expire;
  * `--forget` works across every result directory and the cache; nothing is sent.

Usage:
  python leadfinder.py --icp icp.example.json --seeds sites.csv
  python leadfinder.py --icp icp.example.json --expocentr <exhibition-id> --limit 20
  python leadfinder.py --forget ivanov@example.ru
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import date
from difflib import SequenceMatcher
from itertools import permutations
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

import httpx

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import personalize as P  # noqa: E402  (robots.txt matcher, dates, domain helpers, LLM backend)

try:
    from bs4 import BeautifulSoup, Comment, NavigableString, Tag
except ImportError as exc:  # pragma: no cover - a clear message instead of a traceback
    raise SystemExit("leadfinder.py needs beautifulsoup4: pip install beautifulsoup4") from exc

log = logging.getLogger("leadfinder")

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

USER_AGENT = "Mozilla/5.0 (compatible; LeadFinderBot/1.0; company contact pages only; respects robots.txt)"
DEFAULT_CACHE = SCRIPT_DIR / ".cache" / "leadfinder"
DEFAULT_OUT = SCRIPT_DIR / "leadfinder_out"
MAX_GAP = 350  # the name, the title and the address must be printed this close (characters of visible text)
MIN_CONFIDENCE = 50
CACHE_VERSION = 2  # pages cached by older code (redirects followed blindly) are not trusted
CACHE_DAYS = 30  # a cached page older than this is fetched again
ROBOTS_TTL = 24 * 3600  # RFC 9309: a cached robots.txt should not be used for more than a day
NEGATIVE_TTL = 6 * 3600  # failures and near-empty pages are remembered for six hours
ROBOTS_RETRY_AFTER = 60.0  # seconds before an unreachable robots.txt is asked again within one run
MAX_REDIRECTS = 5
MAX_PAGE_BYTES = 3_000_000
OFFSITE = "редирект на другой сайт"
ROBOTS_DENIED = "запрещено robots.txt"
ROBOTS_DOWN = "robots.txt не получен"
NET_ERRORS = ("нет соединения", "нет ответа")
LLM_BELOW = 65  # paired candidates under this confidence are shown to the LLM when it is enabled
FRAGMENT_LIMIT = 500
SCRUBBED = "[адрес удалён по запросу]"
LLM_NO_NAME = "модель не выбрала имя"

LEAD_COLUMNS = ["Имя", "Фамилия", "Должность", "Email", "Телефон", "Компания", "site", "город", "сегмент",
                "тип_адреса", "источник", "фрагмент", "дата_проверки", "уверенность", "проверки",
                "отчество", "роль"]
NO_LEAD_COLUMNS = ["Компания", "site", "город", "сегмент", "общий_контакт", "телефон", "причина",
                   "страниц_просмотрено", "дата_проверки", "откуда"]

TYPE_PERSONAL = "именной"
TYPE_ROLE = "ящик должности ЛПР"
TYPE_GENERIC = "общий ящик"
TYPE_FREE = "бесплатный домен"
TYPE_UNKNOWN = "не определён"

# Reject reasons (also the keys of the run summary).
R_GENERIC = "общий ящик"
R_FREE = "бесплатный домен"
R_DOMAIN = "домен не совпадает с сайтом"
R_NO_NAME = "нет имени рядом с адресом"
R_AMBIGUOUS = "несколько имён на один адрес"
R_NO_TITLE = "нет должности рядом с именем"
R_NOT_DM = "не ЛПР"
R_ROLE = "роль вне ICP"
R_FOREIGN = "чужой человек (отзыв, партнёр, автор)"
R_GAP = "имя и адрес далеко друг от друга"
R_MAILTO = "адрес не напечатан: стоит только в ссылке mailto"
R_PD_BAN = "на сайте запрет на обработку персональных данных"
R_REFUSAL = "на сайте опубликован отказ от предложений"
R_STALE = "страница давно не обновлялась"
R_NO_MX = "у домена нет MX"
R_SYNTAX = "некорректный адрес"
R_LOW = "низкая уверенность"
R_OPTOUT = "контакт удалён по запросу (--forget)"
R_UNMATCHED = "адрес не совпал с ФИО и не похож на ящик должности"
R_MOBILE_BOX = "адрес составлен из номера мобильного телефона"
R_DUPLICATE = "дубль адреса"
R_LIMIT = "сверх лимита лидов на компанию"

ROLE_LABELS = {
    "ceo": "первое лицо (ген. директор, владелец, основатель)",
    "commercial": "коммерческий директор",
    "sales": "руководитель продаж",
    "marketing": "руководитель маркетинга",
    "bizdev": "руководитель развития",
    "branch": "руководитель филиала или региона",
    "dept_head": "руководитель подразделения",
    "other_director": "директор вне целевых функций",
    "staff": "сотрудник без полномочий ЛПР",
}
DEFAULT_ROLES = ("commercial", "sales", "ceo", "marketing", "bizdev", "branch")

# --- Names ------------------------------------------------------------------- #

_RU_MALE = """
александр алексей анатолий андрей антон аркадий арсений артем артём артур богдан борис вадим валентин валерий
василий вениамин виктор виталий влад владимир владислав всеволод вячеслав гавриил геннадий георгий герман глеб
григорий давид даниил данил данила денис дмитрий евгений егор захар иван игнат игорь илья иннокентий иосиф кирилл
константин лев леонид макар максим марк матвей михаил назар никита николай олег павел петр пётр платон родион роман
ростислав руслан савва святослав семен семён сергей станислав степан тарас тимофей тимур трофим федор фёдор филипп
эдуард эльдар эмиль юлиан юрий яков ян ярослав ринат рустам ильдар марат азат айдар рамиль ильнур ильшат радик
рафаэль ренат рашид равиль фарид наиль дамир альберт артемий арсен армен арам ашот карен тигран гарик самвел вагиф
заур мурат шамиль магомед ахмед расул тагир ильяс айрат булат ришат виль салават альфред роберт эрик феликс ефим
емельян остап мирон демид елисей клим кузьма наум прохор шамхан рифат ильгиз ренальд эрнест аркадий никон
рамиз натиг эльчин вугар фуад рауф рафик ровшан сабир самир тофик фарход фаррух шавкат шухрат алишер бахтияр
бахром джамшед зафар ильхам камиль камил мансур назим расим рафаил рафис рифкат салим тахир умар фаиль фанис
халил хасан хусейн эдгар эльмир эмин юсуф ибрагим ислам исмаил мурад муса рамазан саид султан тамерлан аслан
азамат алан батыр даниял ильмир ильсур ильфат ленар линар марсель радмир раиль рамис ранис рузиль рушан талгат
фаниль фарит фидель халит эльнур анвар арслан вильдан гамлет гарри давлат искандер карим левон нурлан рафаэль
ришат севастьян спартак станислав тихон фома эльвин юлий ярослав герасим данияр ерлан жан зиновий лаврентий
"""
_RU_FEMALE = """
александра алена алёна алина алиса алла анастасия ангелина анжела анжелика анна антонина валентина валерия варвара
василиса вера вероника виктория виолетта галина дарья диана дина ева евгения екатерина елена елизавета жанна
зинаида зоя инна ирина карина кира клавдия кристина ксения лариса лидия лилия любовь людмила маргарита марина мария
майя милана надежда наталья наталия нелли нина оксана олеся ольга полина раиса регина римма светлана софия софья
таисия тамара татьяна ульяна элина эльвира элеонора эмилия юлия яна ярослава гульнара гузель гюзель алсу айгуль
альбина альфия венера динара зарина земфира лейла лиана мадина наиля резеда роза рената сабина эльмира асель ася
влада злата лада снежана стефания агата аглая ада белла инга инесса ирма марианна марьяна мила нонна станислава
алия амина аида гульшат гульназ диляра зульфия ляйсан миляуша рамиля фарида эльнара айгерим камила лейсан лия
нелля розалия рузалия сания фаина эвелина эльза юлиана ясмина дарина милена оксана олеся радмила серафима
"""
_RU_SHORT = """
настя саша женя катя лена таня дима миша паша коля вова юля оля ира света маша даша наташа лёша леша сережа серёжа
костя слава стас макс гоша гриша толя витя валя люба надя рита лиза соня ксюша аня вика лера гена жора петя рома
"""
RU_FIRST_NAMES = frozenset((_RU_MALE + _RU_FEMALE + _RU_SHORT).split())
_MALE_NAMES = frozenset(name.replace("ё", "е") for name in _RU_MALE.split())
# Patronymic stems that are not simply "name + ович": Михайлович, Павлович, Львович ...
_IRREGULAR_PATRONYMIC_STEMS = frozenset("михайл павл льв яковл никит ильин кузьм фом лук савв".split())

LATIN_FIRST_NAMES = frozenset("""
alexander aleksandr alexandr alex alexey alexei aleksey aleksei anatoly anatoliy andrey andrei andrew anton arkady
artem artyom artur arthur bogdan boris vadim valentin valery valeriy vasily victor viktor vitaly vitaliy vladimir
vladislav vyacheslav gennady georgy george german gleb grigory david daniil danil denis dmitry dmitri dmitriy
evgeny evgeniy eugene egor ivan igor ilya kirill konstantin lev leonid maxim maksim max mark matvey mikhail michael
nikita nikolay nikolai oleg pavel paul petr peter roman ruslan semyon sergey sergei stanislav stepan timur fedor
eduard yuri yury yuriy yakov yaroslav rinat rustam marat albert robert john james william richard thomas daniel
steven kevin brian alexandra alena alina alisa alla anastasia angelina anna antonina valentina valeria varvara vera
veronika victoria viktoria galina daria darya diana eva evgenia ekaterina elena elizaveta zhanna inna irina karina
kira kristina ksenia larisa lidia lilia lyubov lyudmila margarita marina maria mariya nadezhda natalia natalya nina
oksana olesya olga polina regina svetlana sofia tamara tatiana tatyana ulyana elvira yulia julia yana mary jennifer
sarah kate emily laura helen
""".split())

# Capitalised words that are never a surname next to a first name.
SURNAME_STOP = frozenset("""
отдел директор директора генеральный коммерческий исполнительный технический финансовый управляющий региональный
ведущий старший младший первый главный руководитель начальник менеджер заместитель специалист бухгалтер инженер
юрист партнер партнёр основатель владелец учредитель президент глава группа центр служба департамент управление
филиал представительство регион город телефон тел моб факс адрес почта сайт компания завод офис склад россия
москва санкт петербург область улица проспект контакты продажи сбыт бухгалтерия приемная приёмная режим время
график наш наши наша ваш ваши уважаемый уважаемая господин доктор профессор эксперт автор клиент отзыв проект
кейс новости подробнее профиль сотрудник сотрудники команда руководство вопросы сотрудничество мероприятия имя
фамилия отчество должность подразделение фио написать позвонить заказать оставить отправить получить скачать
email skype telegram whatsapp viber общество ооо зао оао пао ао ип тд нпо нпп гк пк завода компании республика
край район поселок посёлок шоссе переулок набережная площадь бульвар дом корпус строение этаж кабинет индекс
работы рабочие выходной понедельник пятница суббота воскресенье январь февраль март апрель май июнь июль август
сентябрь октябрь ноябрь декабрь и или для при под над без про это все всё как что кто где наша история миссия
ценности преимущества услуги продукция каталог цены доставка оплата гарантия сервис поддержка вакансии карьера
блог статьи пресс медиа лицензия сертификат реквизиты банк счет инн кпп огрн бик техника технологии системы решения
""".split())
LATIN_STOP = frozenset("""
sales director manager group company llc inc ltd street moscow russia email phone head chief officer founder owner
partner team about contact contacts support service services department office center centre and the for with
president marketing business development general executive commercial managing
""".split())

_CYR_TOKEN = r"(?:[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?|[А-ЯЁ]{2,}(?:-[А-ЯЁ]{2,})?)"
NAME_TOKEN_RE = re.compile(rf"(?<![\w-]){_CYR_TOKEN}(?![\w-])")
SURNAME_INITIALS_RE = re.compile(r"(?<![\w-])([А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?)\s+([А-ЯЁ])\.\s?([А-ЯЁ])\.")
INITIALS_SURNAME_RE = re.compile(r"(?<![\w.-])([А-ЯЁ])\.\s?([А-ЯЁ])\.\s?([А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?)(?![\w-])")
LATIN_NAME_RE = re.compile(r"(?<![\w-])([A-Z][a-z]+)\s+([A-Z][a-z]+(?:-[A-Z][a-z]+)?)(?![\w-])")
PATRONYMIC_RE = re.compile(r"(?:ович|евич|ьич|овна|евна|ична|инична)$")
SHORT_PATRONYMICS = frozenset({"ильич", "кузьмич", "лукич", "фомич", "никитич", "саввич", "ильинична", "кузьминична"})
# Latin letters typed instead of Cyrillic look-alikes inside a Russian word («Aлексей» with a Latin A).
HOMOGLYPHS = str.maketrans("AaBCcEeHKkMOoPpTXxy", "АаВСсЕеНКкМОоРрТХху")
MIXED_WORD_RE = re.compile(r"(?<![\w-])(?=[\w-]*[А-Яа-яЁё])(?=[\w-]*[A-Za-z])[A-Za-zА-Яа-яЁё-]+(?![\w-])")
# «Фамилия Имя» with no patronymic is the form most easily faked by ordinary text («В Примере Пётр
# прошёл путь…»): outside these typical endings it needs a clean context.
SURNAME_SUFFIX_RE = re.compile(
    r"(?:[оеё]в|[иы]н|[оеё]ва|[иы]на|ск(?:ий|ая|ой)|цк(?:ий|ая|ой)|ко|[ую]к|ич|[ыи]х|ян|дзе|швили)$")
WORD_BEFORE_NAME_RE = re.compile(
    r"(?:^|[\s(«\"])(?:в|во|на|к|ко|с|со|от|у|для|из|по|о|об|при|за|что|как|где)\s+$", re.I)
STREET_BEFORE_RE = re.compile(r"(?:ул\.|улица|пр\.|пр-т|просп\.|проспект|пер\.|имени|им\.|пл\.|наб\.|б-р|бульвар|"
                              r"шоссе|проезд)\s*$", re.I)

# --- Titles ------------------------------------------------------------------ #

_MOD = (r"(?:генеральн\w+|ген\.|коммерческ\w+|ком\.|исполнительн\w+|управляющ\w+|техническ\w+|финансов\w+|"
        r"операционн\w+|региональн\w+|главн\w+|ведущ\w+|старш\w+|младш\w+|перв\w+|заместител\w+|зам\.|"
        r"и\.\s?о\.|врио|креативн\w+|managing|general|commercial|executive|chief|senior|regional|deputy|vice)")
_HEAD = (r"(?:гендиректор\w*|директор\w*|руководител\w+|начальник\w*|глав[аы]|заместител\w+|менеджер\w*|"
         r"специалист\w*|бухгалтер\w*|инженер\w*|владел[еь]\w+|собственник\w*|(?:со)?основател\w+|учредител\w+|"
         r"партн[её]р\w*|президент\w*|управляющ\w+|председател\w+|секретар\w+|помощни\w+|ассистент\w*|"
         r"юрист\w*|консультант\w*|аналитик\w*|маркетолог\w*|логист\w*|экономист\w*|преподавател\w+|"
         r"эксперт\w*|координатор\w*|администратор\w*|представител\w+|конструктор\w*|технолог\w*|"
         r"диспетчер\w*|оператор\w*|программист\w*|разработчик\w*|дизайнер\w*|"
         r"ceo|cco|cmo|coo|cfo|cto|cio|cbdo|cro|co-?founder|founder|owner|president|director|manager|"
         r"head\s+of|vp|partner|chief\s+\w+\s+officer)")
TITLE_START_RE = re.compile(rf"(?<![\w-])(?:{_MOD}\s+){{0,3}}{_HEAD}(?![\w-])", re.I)
DEPUTY_RE = re.compile(r"заместител|\bзам\.|deputy|\bvice\b|вице-|и\.\s?о\.|врио", re.I)
# A head word in an oblique case ("приёмная директора", "к директору") is not somebody's title.
OBLIQUE_HEAD_RE = re.compile(
    r"(?:главы|(?:директор|руководител|начальник|управляющ|президент|председател|владельц|основател|учредител|"
    r"собственник|менеджер|специалист|инженер|бухгалтер|юрист|консультант|аналитик|маркетолог|логист|экономист|"
    r"эксперт|координатор|администратор|представител|конструктор|технолог|диспетчер|оператор|программист|"
    r"разработчик|дизайнер|партн[её]р)(?:а|я|у|ю|ом|ем|ём|е|ов|ей|ам|ям|ами|ями|ах|ях|его|ему|им))$", re.I)
PLURAL_HEAD_RE = re.compile(r"(?:[ыи]|ors|ers)$", re.I)
# Case-sensitive on purpose: «отд. продаж» goes on, «Смета". Специалист» ends the title.
TITLE_STOP_RE = re.compile(
    r"[;:!?|•]|\.(?=[\s»\"”]*[A-ZА-ЯЁ\d]|[\s»\"”]*$)|\s[-–—]\s+(?=[A-ZА-ЯЁ])|\+?\d[\d\s()\-]{6,}|"
    r"(?<![\w-])(?i:тел(?:ефон)?|моб|e-?mail|email|почта|эл\.|доб|факс|skype|whatsapp|telegram|профиль)(?![\w-])")
MAX_TITLE_CHARS = 140
GENERIC_HEAD_TITLE_RE = re.compile(
    r"^(?:начальник|руководитель|глава|директор)\s+(?:отдела|департамента|службы|управления|группы|направления|"
    r"подразделения|дирекции)$", re.I)
DEPT_RE = re.compile(
    r"^(?:(?:[а-яё-]+(?:ый|ий|ой|ая|яя|ое)\s+)?(?:отдел|департамент|служба|управление|дирекция)|бухгалтерия|"
    r"при[её]мная|секретариат|канцелярия|техподдержка|техническая поддержка|склад|производство|"
    r"подразделение)(?![\w-])", re.I)

_TARGET_HEAD = r"(?:директор|руководител|начальник|глава|head|director|chief|vp|vice president|заместител|зам\.)"
ROLE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("commercial", re.compile(
        r"коммерческ\w+\s+директор|ком\.\s*директор|директор\w*\s+по\s+коммерц|по\s+коммерческ|"
        r"(?:руководител|начальник|глава)\w*\s+коммерческ|\bcco\b|chief commercial|commercial director|"
        r"head of commercial", re.I)),
    ("sales", re.compile(
        rf"{_TARGET_HEAD}[^,;]*?(?:продаж|сбыт|реализаци|sales|корпоративн\w+\s+(?:отдел|клиент))|\bcro\b|"
        r"chief (?:sales|revenue)", re.I)),
    ("marketing", re.compile(
        rf"{_TARGET_HEAD}[^,;]*?маркетинг|\bcmo\b|chief marketing|marketing director|head of marketing", re.I)),
    ("bizdev", re.compile(
        rf"{_TARGET_HEAD}[^,;]*?(?:развити|клиент|партн[её]рск|стратег)|business development|\bcbdo\b", re.I)),
)
STAFF_START_RE = re.compile(
    r"^(?:(?:ведущ\w+|старш\w+|младш\w+|главн\w+|региональн\w+|персональн\w+|личн\w+)\s+)?"
    r"(?:помощни|ассистент|секретар|референт|советник|водител|стаж[её]р|менеджер|специалист|бухгалтер|инженер|"
    r"юрист|консультант|аналитик|маркетолог|логист|экономист|преподавател|эксперт|координатор|администратор|"
    r"представител|конструктор|технолог|диспетчер|оператор|программист|разработчик|дизайнер|"
    r"manager|assistant|engineer|specialist|accountant|consultant)", re.I)
OTHER_FUNCTION_RE = re.compile(
    r"по\s+(?:производств|качеств|персонал|кадр|финанс|экономик|безопасност|закупк|снабжени|логистик|ит\b|"
    r"информацион|правов|юридическ|общим|строительств|наук|научн|техническ|эксплуатац|сервис|охране|"
    r"административ|хозяйств|учебн|проект|разработк|внедрени|операционн)|"
    r"техническ\w+\s+директор|финансов\w+\s+директор|операционн\w+\s+директор|(?:ит|it|арт|hr)-директор|"
    r"креативн\w+\s+директор|\b(?:cfo|cto|cio|coo)\b", re.I)
CEO_RE = re.compile(
    r"генеральн\w+\s+директор|ген\.\s*директор|гендиректор|исполнительн\w+\s+директор|"
    r"управляющ\w+\s+(?:директор|партн[её]р)|^управляющ\w+$|^директор$|"
    r"^директор\s+(?:компании|предприятия|завода|фирмы|организации|ооо|ао|зао|оао|пао|ип|"
    r"студии|агентства|бюро|группы)|владел|собственник|"
    r"основател|учредител|^президент|председател\w+\s+(?:правления|совета)|"
    r"руководител\w+\s+(?:компании|предприятия|организации|фирмы|студии|агентства|бюро|завода)|"
    r"\bceo\b|chief executive|managing (?:director|partner)|founder|\bowner\b|^president|"
    r"general (?:director|manager)|^директор\s*[«\"(]", re.I)
# The head of a branch or a region decides for that branch only: a group of its own, not "the first person".
BRANCH_RE = re.compile(
    r"региональн\w+\s+(?:директор|руководител|управляющ)|"
    r"(?:директор|руководител|управляющ|глава|начальник)\w*\s+(?:[а-яё-]+\s+)?(?:филиал|представительств|"
    r"обособленн\w+\s+подразделени|регионального\s+(?:офиса|центра|отделения))|"
    r"regional (?:director|manager|head)|branch (?:director|manager|head)", re.I)
SECOND_TITLE_SPLIT_RE = re.compile(r"\s[-–—]\s+|,\s+")
DEPT_HEAD_RE = re.compile(
    r"(?:руководител|начальник|глава|head)\w*\s+.*?(?:отдел|департамент|служб|групп|управлени|направлени|"
    r"дирекци|центр|практик|подразделени)|head of", re.I)

# --- Emails ------------------------------------------------------------------ #

EMAIL_RE = P.EMAIL_RE
# An obfuscated address is read only when the page marks it as one: «name [at] site.ru», «name (собака)
# site.ru». Loose words around a bare « @ » («Follow us @ site.ru») are not an address, and a plain dot
# never jumps over a space («… site.ru. Call us» ends at «site.ru»).
OBFUSCATED_RE = re.compile(
    r"(?<![\w.+-])([a-z0-9][a-z0-9._%+-]{0,39})\s*"
    r"(?:\[\s*(?:at|собака|@)\s*\]|\(\s*(?:at|собака|@)\s*\)|\{\s*(?:at|собака|@)\s*\})\s*"
    r"([a-z0-9-]+(?:(?:\.|\s*\[\s*(?:dot|точка)\s*\]\s*|\s*\(\s*(?:dot|точка)\s*\)\s*)[a-z0-9-]+){1,3})(?![\w-])",
    re.I)
SPLIT_ZONE_RE = re.compile(
    r"(?<![\w.+-])([a-z0-9][a-z0-9._%+-]{0,39}@[a-z0-9-]+(?:\.[a-z0-9-]+)*)\s+\.\s?([a-z]{2,10})(?![\w-])", re.I)
JUNK_EMAIL_RE = re.compile(r"\.(?:png|jpe?g|gif|svg|webp|js|css)$|@(?:example\.|domain\.|sentry|wixpress|"
                           r"email\.|test\.)|^(?:name|email|user|username|your|you)@", re.I)
# Words that name a department, a function or a place: a box called so belongs to nobody in person.
_GENERIC_WORDS = (
    r"info|inform|mail|e?-?mail|office|ofis|post|pochta|hello|hi|contacts?|kontakt\w*|welcome|admin\w*|"
    r"webmaster|support|help|helpdesk|service|servis|sales?|sell|prodaj\w*|prodazh\w*|opt|optom|zakaz\w*|"
    r"orders?|shop|store|market|marketing|reklama|pr|press|media|smm|hr|jobs?|career|rabota|resume|personal|"
    r"kadry|ok|op|buh\w*|bux|account\w*|fin|finance|zakupki|zakup|snab|supply|tenders?|tendery|export|import|ved|"
    r"dealers?|diler\w*|partners?|clients?|klient\w*|customers?|managers?|team|company|corp|reception|"
    r"priem\w*|secretar\w*|sekretar\w*|kanc\w*|fax|sbyt|sbt|torg|trade|commerce|commercial|kommer[cs]\w*|komm?|"
    r"logist\w*|sklad|dostavka|delivery|transport|tech|it|no-?reply|robot|bot|feedback|request|zapros|zayavka|"
    r"leads?|crm|site|web|www|news|subscribe|pay|bill\w*|legal|law|urist|expo|edu|school|academy|moscow|msk|spb|"
    r"piter|regions?|filial|ekb|nsk|kzn|central|volga|north|south|sib|ural|zavod|factory|plant|lab|b2b|1c|"
    r"general|common|all|group|otdel\w*|department|dept|zapchasti|parts|project\w*|kp|cp")
GENERIC_LOCAL_RE = re.compile(rf"^(?:{_GENERIC_WORDS})[\d._-]*$", re.I)
# One part of a compound local part: «sales-msk», «info.spb», «1_sales», «otdel.prodazh», «b2b-sales».
GENERIC_TOKEN_RE = re.compile(rf"^\d*(?:{_GENERIC_WORDS})\d*$", re.I)
_ROLE_WORDS = (r"dir|director|direktor|gendir|gen-?dir|gd|ceo|cco|cmo|cbdo|cro|kd|komdir|comdir|commdir|"
               r"kommdir|head|boss|chief|president|owner|founder|upr|zamdir|zam|md")
ROLE_LOCAL_RE = re.compile(rf"^(?:to|ask)?(?:{_ROLE_WORDS})[\d._-]*$", re.I)
ROLE_TOKEN_RE = re.compile(rf"^(?:to|ask)?(?:{_ROLE_WORDS})\d*$", re.I)
NUMERIC_LOCAL_RE = re.compile(r"\d+[a-z]?|[a-z]?\d+", re.I)
EXTENSION_RE = re.compile(r"(?<![\w-])(?:доб|внутр|вн|ext)\.?\s*[:№(]*\s*(\d{1,5})", re.I)
PHONE_RE = re.compile(
    r"(?<!\d)(?:\+7|8)[\s (\-]*\d{3,5}[\s )\-]*\d{1,3}[\s \-]?\d{2}[\s \-]?\d{2,3}"
    r"(?:\s*\(?(?:доб|вн|ext)\.?\s*\(?\d{2,5}\)?)?")
# A Russian mobile number in any usual spelling, with or without the country prefix: «+7 9xx xxx-xx-xx»,
# «8 (9xx) xxx xx xx», «9xxxxxxxxx». Mobile numbers are personal data of whoever carries the phone: the
# tool never stores one, and in the evidence fragment only the last two digits stay.
MOBILE_RE = re.compile(
    r"(?<![\d+])(?P<pre>(?:\+\s?7|7|8)[\s\u00a0(\-]{0,3})?"
    r"(?P<num>\(?9\d{2}\)?[\s\u00a0\-]{0,3}\d{3}[\s\u00a0\-]{0,2}\d{2}[\s\u00a0\-]{0,2}\d{2})(?!\d)")
PHONE_EXT_RE = re.compile(r"\s*\(?(?:доб|вн|ext)", re.I)

# First-letter options when a name is abbreviated to initials in an address.
INITIAL_LETTERS = {
    "а": "a", "б": "b", "в": "vw", "г": "gh", "д": "d", "е": "ey", "ё": "ey", "ж": "zjg", "з": "z", "и": "i",
    "й": "yji", "к": "kc", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "sc", "т": "t",
    "у": "uy", "ф": "fp", "х": "khx", "ц": "tc", "ч": "c", "ш": "s", "щ": "s", "э": "e", "ю": "yuj", "я": "yjia",
}
FIRST_NAME_ALIASES = {
    "александр": ("alex", "alexander", "aleksandr"), "алексей": ("alex", "alexey", "alexei", "aleksey"),
    "александра": ("alex", "alexandra"), "юлия": ("julia", "yulia"), "юрий": ("yuri", "yury", "juri"),
    "евгений": ("eugene", "evgeny"), "евгения": ("eugenia", "evgenia"), "петр": ("petr", "peter"),
    "пётр": ("petr", "peter"), "михаил": ("michael", "mikhail"), "павел": ("pavel", "paul"),
    "екатерина": ("kate", "katya", "ekaterina"), "наталья": ("natalia", "natalya"), "наталия": ("natalia",),
    "анастасия": ("nastya", "anastasia"), "андрей": ("andrew", "andrey", "andrei"), "георгий": ("george",),
    "виктория": ("victoria", "vika"), "ксения": ("xenia", "ksenia"), "яна": ("jana", "yana"),
}

# --- Site-level red flags ---------------------------------------------------- #

PD_BAN_RE = re.compile(
    r"запрет\w*\s+на\s+обработку[^.]{0,160}(?:неограниченным\s+кругом|персональн\w+\s+данн)|"
    r"запрещ\w+\s+(?:сбор|обработк\w+|использовани\w+)\s+(?:опубликованных\s+)?персональн\w+\s+данн", re.I)
REFUSAL_RE = re.compile(
    r"(?:рекламн\w+|коммерческ\w+)\s+предложени\w+\s+(?:не\s+(?:рассматрива|принима)|просим\s+не)|"
    r"не\s+(?:присылайте|направляйте|отправляйте|присылать|направлять|отправлять)\s+(?:нам\s+)?"
    r"(?:рекламн|коммерческ\w+\s+предлож|спам)|неинтересующ\w+\s+предложени|"
    r"предложени\w+\s+(?:рекламы|рекламного\s+характера)\s+не\s+(?:рассматрива|принима)", re.I)
FOREIGN_SECTION_RE = re.compile(
    r"отзыв|благодарност|рекомендац|наши\s+клиенты|клиенты\s+о\s+нас|говорят\s+клиенты|партн[её]ры|"
    r"нам\s+доверяют|об\s+авторе|автор\s+стать|testimonial|reviews?|our\s+clients|partners", re.I)
FOREIGN_PATH_RE = re.compile(
    r"(?:^|/)(?:authors?|avtor\w*|blog|articles?|stati|news|novosti|otzyv\w*|reviews?|testimonials?|clients?|"
    r"klienty|partners?|partnery|cases?|kejsy|portfolio)(?:/|$)", re.I)
ORG_AFTER_TITLE_RE = re.compile(
    r"(?:ооо|ао|зао|оао|пао|ип|гк|нпо|нпп|тд|компании|фирмы|группы\s+компаний|холдинга|завода|банка|агентства)"
    r"\s+[«\"“]?([^»\"”,.;:()]{2,60})", re.I)
STAFF_COUNT_RE = re.compile(
    r"(?<![\d.,])(\d{1,3}(?:[\s\u00a0]?\d{3})?)\s*\+?\s*(?:[а-яё-]+(?:ых|их)\s+){0,2}"
    r"(?:сотрудник|специалист|человек\s+в\s+(?:штате|команде)|работник|employees)", re.I)
UPDATED_RE = re.compile(
    r"(?:обновлен[оаы]?|дата\s+(?:обновления|изменения)|последнее\s+(?:обновление|изменение)|"
    r"актуальн\w+\s+на|last\s+(?:updated|modified)|updated(?:\s+on)?)\s*:?\s*", re.I)
COPYRIGHT_YEAR_RE = re.compile(
    r"(?:©|\(c\)|copyright)[^\n]{0,40}?((?:19|20)\d{2})(?:\s*[-–—]\s*((?:19|20)\d{2}))?", re.I)

# --- Crawl ------------------------------------------------------------------- #

_SEG_END = r"(?:/|\.s?html?|\.php|\.aspx?|$)"
PAGE_KINDS: tuple[tuple[str, int, re.Pattern[str], re.Pattern[str]], ...] = (
    ("contacts", 10,
     re.compile(r"(?:^|/)(?:contacts?|contact[-_]?us|kontakty?i?|kontakti|kontaktnaya[-_]informatsiya\w*|svyaz\w*)"
                + _SEG_END, re.I),
     re.compile(r"^\s*(?:контакты|контактная информация|contacts?|contact us|связаться с нами|наши контакты)\s*$",
                re.I)),
    ("team", 9,
     re.compile(r"(?:^|/)(?:team|our[-_]team|komanda|nasha[-_]komanda|staff|sotrudniki|nashi[-_]sotrudniki|"
                r"rukovodstvo|rukovoditeli|management|managment|leadership|people|persons?|kollektiv|"
                r"administra[ct]i[yj]a|administration|employees|experts?|specialists?|specialisty)" + _SEG_END,
                re.I),
     re.compile(r"^\s*(?:(?:наша\s+)?команда|(?:наши\s+)?сотрудники|руководство(?:\s+компании)?|руководители|"
                r"менеджмент|коллектив|администрация|наши\s+специалисты|team|our team|management|leadership|"
                r"people)\s*$", re.I)),
    ("dept", 7,
     re.compile(r"(?:^|/)(?:otdel[-_]prodazh|sales|sbyt|commercial|kommercheskij[-_]otdel)" + _SEG_END, re.I),
     re.compile(r"^\s*(?:отдел продаж|отдел сбыта|коммерческий отдел|служба продаж|коммерческая служба)\s*$",
                re.I)),
    ("about", 6,
     re.compile(r"(?:^|/)(?:about(?:[-_]?us)?|about[-_]company|o[-_]?kompanii|o[-_]nas|company|kompaniya|"
                r"o[-_]zavode|o[-_]predpriyatii|press/contact)" + _SEG_END, re.I),
     re.compile(r"^\s*(?:о компании|о нас|компания|о заводе|о предприятии|about|about us|company)\s*$", re.I)),
    ("press", 5,
     re.compile(r"(?:^|/)(?:press|press[-_]?(?:cent(?:er|re)|room|kit|sluzhba)|pressa|dlya[-_]smi|smi|media)"
                + _SEG_END, re.I),
     re.compile(r"^\s*(?:пресс-центр|пресс-служба|прессе|для сми|сми|для прессы|press|media|press kit)\s*$", re.I)),
)
GUESSED_PATHS = {"contacts": ("/contacts/", "/kontakty/", "/contact/"),
                 "team": ("/company/staff/", "/team/", "/about/team/")}
SITEMAP_LIMIT = 5000
PERSON_PAGES_PER_SITE = 4
BLOCK_TAGS = P.BLOCK_TAGS | {"dl", "tbody", "thead", "tfoot", "figure", "details", "summary", "body", "html"}
SKIP_TAGS = P.SKIP_TAGS
CHROME_TAGS = frozenset({"header", "footer", "nav"})
CONTENT_TAGS = frozenset({"main", "article", "section"})
# Social networks, directories, marketplaces and contact databases: never "the company's own site".
AGGREGATOR_DOMAINS = frozenset({
    "2gis.ru", "yandex.ru", "google.com", "google.ru", "hh.ru", "rusprofile.ru", "zoon.ru", "wikipedia.org",
    "avito.ru", "vk.com", "vk.ru", "ok.ru", "t.me", "telegram.org", "youtube.com", "rutube.ru", "linkedin.com",
    "facebook.com", "instagram.com", "twitter.com", "x.com", "whatsapp.com", "wa.me", "tiktok.com",
    "list-org.com", "sbis.ru", "checko.ru", "orgpage.ru", "yell.ru", "flamp.ru", "tiu.ru", "satom.ru",
    "pulscen.ru", "blizko.ru", "all.biz", "spark-interfax.ru", "kontur.ru", "habr.com", "vc.ru", "dzen.ru",
    "export-base.ru", "metaprom.ru", "rocketreach.co", "zoominfo.com", "apollo.io", "hunter.io", "lusha.com",
    "contactout.com", "signalhire.com", "crunchbase.com", "companies.rbc.ru", "rbc.ru", "audit-it.ru",
    "zachestnyibiznes.ru", "vbankcenter.ru", "synapsenet.ru", "b2b-center.ru", "fabrikant.ru", "prom.ru",
    "ru.all.biz", "yandex.com", "mail.ru", "ozon.ru", "wildberries.ru", "market.yandex.ru", "tinkoff.ru",
    "expocentr.ru", "exponet.ru", "kompass.com", "europages.com", "alibaba.com", "made-in-china.com",
    "productcenter.ru", "fabricators.ru", "manufacturers.ru", "oborudunion.ru", "equipnet.ru", "bizorg.su",
    "spravker.ru", "cataloxy.ru", "yp.ru", "rosfirm.ru", "ru-bezh.ru", "trudvsem.ru", "superjob.ru",
})
# Suffixes under which unrelated companies keep their sites: hosting platforms, site builders and
# regional zones. Under such a suffix "the company's own site" is the exact host, not the last two labels.
SHARED_SUFFIXES = frozenset("""
tilda.ws tilda.cc wixsite.com wix.com ucoz.ru ucoz.net ucoz.com ucoz.org ucoz.ua 3dn.ru my1.ru at.ua clan.su
narod.ru narod2.ru github.io gitlab.io blogspot.com blogspot.ru wordpress.com webflow.io tb.ru nethouse.ru
umi.ru jimdo.com jimdofree.com weebly.com squarespace.com netlify.app vercel.app pages.dev herokuapp.com
web.app firebaseapp.com appspot.com azurewebsites.net myshopify.com myinsales.ru turbo.site bitrix24.site
bitrix24site.ru bitrix24.shop lpmotor.ru flexbe.net flexbe.com tobiz.net nubex.ru setup.ru okis.ru usite.pro
mya5.ru tiu.ru satom.ru pulscen.ru deal.by prom.ua business.site google.com sites.google.com vk.com
com.ru net.ru org.ru pp.ru ac.ru edu.ru gov.ru int.ru mil.ru msk.ru spb.ru msk.su spb.su nov.su
adygeya.ru altai.ru amur.ru arkhangelsk.ru astrakhan.ru bashkiria.ru belgorod.ru bir.ru bryansk.ru buryatia.ru
cbg.ru chel.ru chelyabinsk.ru chita.ru chukotka.ru chuvashia.ru dagestan.ru dudinka.ru e-burg.ru grozny.ru
irkutsk.ru ivanovo.ru izhevsk.ru jar.ru joshkar-ola.ru kalmykia.ru kaluga.ru kamchatka.ru karelia.ru kazan.ru
kchr.ru kemerovo.ru khabarovsk.ru khakassia.ru khv.ru kirov.ru koenig.ru komi.ru kostroma.ru krasnoyarsk.ru
kuban.ru kurgan.ru kursk.ru lipetsk.ru magadan.ru mari.ru mari-el.ru marine.ru mordovia.ru murmansk.ru
nalchik.ru nnov.ru nov.ru novosibirsk.ru nsk.ru omsk.ru orenburg.ru oryol.ru palana.ru penza.ru perm.ru ptz.ru
rnd.ru ryazan.ru sakhalin.ru samara.ru saratov.ru simbirsk.ru smolensk.ru stavropol.ru stv.ru surgut.ru
tambov.ru tatarstan.ru tom.ru tomsk.ru tsaritsyn.ru tsk.ru tula.ru tuva.ru tver.ru tyumen.ru udm.ru
udmurtia.ru ulan-ude.ru vladikavkaz.ru vladimir.ru vladivostok.ru volgograd.ru vologda.ru voronezh.ru vrn.ru
vyatka.ru yakutia.ru yamal.ru yaroslavl.ru yekaterinburg.ru yuzhno-sakhalinsk.ru
com.cn net.cn org.cn com.hk com.tw com.ua co.uk org.uk co.jp co.kr com.tr com.br com.au co.in com.kz com.by
com.sg com.my co.il in.ua kiev.ua
""".split())


def norm(text: str) -> str:
    """Lower-case, ё -> е, single spaces: the form used for all comparisons."""
    return P.collapse_ws(text).lower().replace("ё", "е")


def _ascii_host(host: str) -> str:
    """Punycode form of a host, so that «пример.рф» equals «xn--e1afmkfd.xn--p1ai»."""
    host = (host or "").strip().lower().rstrip(".")
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host


def site_of(host: str) -> str:
    """The unit that counts as one company's own site, for a host or an e-mail domain.

    Normally the registrable domain: «shop.site.ru» and «www.site.ru» are the site «site.ru».
    Under a shared suffix (a site builder, a hosting platform, a regional zone) every host is
    somebody else's site, so the unit is the label right before the suffix: «zavod.nnov.ru»
    and «drugaya-firma.nnov.ru» are two different sites.
    """
    labels = [label for label in _ascii_host(host).split(".") if label]
    if labels and labels[0] == "www" and len(labels) > 2:
        labels = labels[1:]
    for size in (3, 2):
        if len(labels) > size and ".".join(labels[-size:]) in SHARED_SUFFIXES:
            return ".".join(labels[-size - 1:])
    return ".".join(labels[-2:])


def host_of(url: str) -> str:
    """Host of a URL, an address or a bare domain, without «www.» (punycode for IDN hosts)."""
    return _ascii_host(P.normalize_domain(url))


def is_net_error(error: str) -> bool:
    """A failure of the connection (tunnel, DNS, timeout), not an answer of the site."""
    return (error or "").startswith(NET_ERRORS)


def is_mobile(phone: str) -> bool:
    """Is this printed number a mobile one (code 9xx)? An extension after the number is not part of it."""
    digits = re.sub(r"\D", "", PHONE_EXT_RE.split(phone or "", maxsplit=1)[0])
    if len(digits) == 11 and digits[0] in "78":
        digits = digits[1:]
    return len(digits) == 10 and digits[0] == "9"


def mask_mobiles(text: str) -> str:
    """Hide the mobile numbers in a piece of page text: «+7 (999) 000-00-00» -> «+7 (***) ***-**-00».
    Only the last two digits stay, enough to find the number on the page and not enough to dial it."""
    def hide(m: re.Match) -> str:
        left, out = 2, []
        for ch in reversed(m.group("num")):
            if ch.isdigit() and left:
                left -= 1
            elif ch.isdigit():
                ch = "*"
            out.append(ch)
        return (m.group("pre") or "") + "".join(reversed(out))

    return MOBILE_RE.sub(hide, text or "")


# --------------------------------------------------------------------------- #
# ICP config and companies
# --------------------------------------------------------------------------- #

@dataclass
class Segment:
    name: str
    keywords: list[str] = field(default_factory=list)


@dataclass
class ICP:
    """Ideal customer profile: what to look for and what to leave out."""

    name: str = "по умолчанию (файл ICP не задан)"
    country: str = "Россия"
    cities: list[str] = field(default_factory=list)  # empty = any city
    segments: list[Segment] = field(default_factory=list)
    roles: list[str] = field(default_factory=lambda: list(DEFAULT_ROLES))
    exclude_companies: list[str] = field(default_factory=list)  # giants, by name
    exclude_keywords: list[str] = field(default_factory=list)  # e.g. agencies, by homepage text
    exclude_domains: list[str] = field(default_factory=list)
    max_companies: int = 50
    pages_per_site: int = 8
    leads_per_company: int = 3
    freshness_months: int = 24
    max_staff: int = 0  # 0 = no size limit
    sources: list[dict] = field(default_factory=list)

    def match_segment(self, text: str) -> tuple[str, str]:
        """(segment name, keyword that matched) or ('', '') - substring match on stems."""
        low = norm(text)
        for seg in self.segments:
            for kw in seg.keywords:
                if norm(kw) and norm(kw) in low:
                    return seg.name, kw
        return "", ""

    def excluded_domain(self, host: str) -> str:
        """The entry of exclude.domains that names the site (or the mail domain) `host`, or ''.

        The same name in another zone is the same entry: «primer.ru» is listed, the catalogue gives
        «primer.com» - that is how a company's site moves, and the crawl itself accepts such a
        move as the same company. On shared suffixes (site builders, regional zones) only the
        exact host counts.
        """
        site = site_of(host_of(host or ""))
        if not site:
            return ""
        for item in self.exclude_domains:
            listed = site_of(host_of(item))
            if not listed:
                continue
            plain = site.count(".") == 1 and listed.count(".") == 1  # neither stands on a shared suffix
            if site == listed or (plain and len(core_of(site)) >= 4 and core_of(site) == core_of(listed)):
                return item
        return ""

    def excluded_by_name(self, name: str, domain: str = "") -> str:
        low = norm(name)
        for item in self.exclude_companies:
            if norm(item) and re.search(rf"(?<![^\W_]){re.escape(norm(item))}(?![^\W_])", low):
                return f"исключение ICP: «{item}»"
        item = self.excluded_domain(domain)
        if item:
            same = site_of(host_of(item)) == site_of(host_of(domain))
            note = "" if same else f" (тот же сайт в другой зоне: {host_of(domain)})"
            return f"исключение ICP: домен {item}{note}"
        return ""

    def city_ok(self, city: str) -> bool:
        if not self.cities or not city:
            return True  # unknown city is not a reason to drop a company
        low = norm(city)
        return any(norm(c) in low for c in self.cities)


def load_icp(path: Path | None) -> ICP:
    """Read the ICP from JSON (or YAML when PyYAML is available). No file = defaults."""
    if path is None:
        return ICP()
    raw = Path(path).read_text("utf-8")
    if Path(path).suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError as exc:
            raise SystemExit("YAML-конфиг требует PyYAML (pip install pyyaml); либо используйте .json") from exc
        data = yaml.safe_load(raw) or {}
    else:
        data = json.loads(raw)
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: в корне ICP должен быть объект")
    exclude, limits = data.get("exclude") or {}, data.get("limits") or {}
    roles = [r for r in (data.get("roles") or DEFAULT_ROLES) if r in ROLE_LABELS]
    unknown = sorted(set(data.get("roles") or []) - set(ROLE_LABELS))
    if unknown:
        raise SystemExit(f"{path}: неизвестные роли {unknown}; допустимые: {sorted(ROLE_LABELS)}")
    return ICP(
        name=str(data.get("name") or "ICP"),
        country=str(data.get("country") or "Россия"),
        cities=[str(c) for c in data.get("cities") or []],
        segments=[Segment(str(s.get("name") or ""), [str(k) for k in s.get("keywords") or []])
                  for s in data.get("segments") or [] if isinstance(s, dict)],
        roles=roles,
        exclude_companies=[str(x) for x in exclude.get("companies") or []],
        exclude_keywords=[str(x) for x in exclude.get("keywords") or []],
        exclude_domains=[str(x) for x in exclude.get("domains") or []],
        max_companies=int(limits.get("companies") or 50),
        pages_per_site=int(limits.get("pages_per_site") or 8),
        leads_per_company=int(limits.get("leads_per_company") or 3),
        freshness_months=int(limits.get("freshness_months") or 24),
        max_staff=int(limits.get("max_staff") or 0),
        sources=[s for s in data.get("sources") or [] if isinstance(s, dict)],
    )


@dataclass
class Company:
    name: str
    site: str
    city: str = ""
    segment: str = ""
    source: str = ""  # which adapter found it
    why: str = ""  # why it matches the ICP

    @property
    def domain(self) -> str:
        return P.normalize_domain(self.site)


# --------------------------------------------------------------------------- #
# Fetching: polite, cached, never leaves the site, robots.txt on every hop
# --------------------------------------------------------------------------- #

def normalize_proxy(value: str) -> str:
    """'127.0.0.1:1080' -> 'socks5h://127.0.0.1:1080' (DNS is resolved by the proxy)."""
    value = (value or "").strip()
    if not value:
        return ""
    return value if "://" in value else f"socks5h://{value}"


@dataclass
class RobotsState:
    """What robots.txt of one host said, as a fact that goes into the evidence of a lead."""

    rules: P.RobotsRules | None  # None = the host has no usable robots.txt: nothing is closed
    note: str  # the line written into the checks of a lead
    blocked: str = ""  # not empty: nothing may be requested from this host, and why
    at: float = 0.0  # time.monotonic() of the answer (an unreachable robots.txt is asked again later)


class LeadFetcher:
    """Thread-safe polite HTTP fetcher with an on-disk cache.

    * An address is brought to one spelling first (personalize.request_url: no «/../», no
      «%2E»); that spelling is both checked against robots.txt and requested.
    * Redirects are followed by hand, one hop at a time. A hop to another site is not made
      (the caller may widen the scope for a company that moved to a new domain); robots.txt
      of the target is checked before every hop.
    * robots.txt is always obeyed, there is no switch for it. What its answer means is decided
      by personalize.read_robots(), the same for every tool: a file is parsed, 4xx means "no
      restrictions", 5xx / 429 / no answer / redirects that lead nowhere close the host
      (RFC 9309) until it is asked again. The redirects of «/robots.txt» itself are followed
      to another site too: the rules of the final file apply to the host that was asked.
      The rules are checked for cached pages as well.
    * At most one request per `delay` seconds to one site (host without «www.», subdomains of
      a site together), counted from the end of the previous request, redirect hops included.
    * The cache is not eternal: pages live CACHE_DAYS, robots.txt a day, failures six hours.
      Every entry keeps the day it was fetched; that day is the evidence date of a lead.
    * Addresses removed with --forget are blanked before a page reaches the cache or the parser.

    Every worker thread has its own httpx.Client with its own SSLContext (truststore's
    context is not safe to share between threads during a handshake).
    """

    RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
    MIN_VISIBLE_TEXT = 200  # 200 OK pages with less text (stubs, JS shells) are cached briefly

    def __init__(self, *, cache_dir: Path | None = None, timeout: float = 15.0, retries: int = 2,
                 delay: float = 1.0, backoff: float = 1.5, user_agent: str = USER_AGENT,
                 client: httpx.Client | None = None, proxy: str = "",
                 suppressed: frozenset[str] = frozenset(), cache_days: float = CACHE_DAYS) -> None:
        self.proxy = normalize_proxy(proxy or "")
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.retries, self.delay, self.backoff = retries, delay, backoff
        self.user_agent = user_agent
        self.suppressed = frozenset(suppressed)
        self.cache_days = cache_days
        self.refresh = False  # True: nothing is read from the cache except what this run has written
        self.network_hits = 0  # HTTP requests really sent (robots.txt and redirect hops included)
        self.cache_hits = 0
        self._timeout = timeout
        self._injected_client = client  # tests only; used by every thread as is
        self._local = threading.local()
        self._clients: list[httpx.Client] = []
        self._clients_lock = threading.Lock()
        self._robots: dict[str, RobotsState] = {}
        self._site_locks: dict[str, threading.Lock] = {}
        self._last_done: dict[str, float] = {}
        self._registry_lock = threading.Lock()
        self._count_lock = threading.Lock()
        self._written: set[str] = set()  # URLs this object has fetched from the network
        self._retry_sites: set[str] = set()  # sites whose remembered failures are asked again
        self._down_sites: set[str] = set()  # sites whose robots.txt was lost to the connection in this run

    # -- clients ------------------------------------------------------------ #

    def _new_client(self) -> tuple[httpx.Client, ssl.SSLContext]:
        if P.truststore:
            context = P.truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        else:
            import certifi

            context = ssl.create_default_context(cafile=certifi.where())
        client = httpx.Client(
            verify=context,
            proxy=self.proxy or None,
            trust_env=False,  # only the explicit POLZA_SOCKS proxy, never ambient *_PROXY variables
            follow_redirects=False,  # every hop is checked by _fetch_network: site scope, robots.txt, pace
            timeout=httpx.Timeout(self._timeout, connect=min(self._timeout, 10.0)),
            headers={"User-Agent": self.user_agent,
                     "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
                     "Accept-Language": "ru,en;q=0.8"},
        )
        return client, context

    @property
    def client(self) -> httpx.Client:
        """The calling thread's own client (created on first use)."""
        if self._injected_client is not None:
            return self._injected_client
        client = getattr(self._local, "client", None)
        if client is None:
            client, self._local.ssl_context = self._new_client()
            self._local.client = client
            with self._clients_lock:
                self._clients.append(client)
        return client

    def close(self) -> None:
        with self._clients_lock:
            clients, self._clients = self._clients, []
        for client in clients:
            client.close()
        if self._injected_client is not None:
            self._injected_client.close()

    # -- public ------------------------------------------------------------- #

    def forget_failures(self, site: str) -> None:
        """Ask the network again for this site instead of replaying its remembered failures
        (a stalled tunnel, a slow minute of the site). Real pages stay cached."""
        key = site_of(host_of(site))
        if not key:
            return
        with self._registry_lock:
            self._retry_sites.add(key)
            self._down_sites.discard(key)
            self._written = {u for u in self._written if site_of(host_of(u)) != key}
            for base in [b for b, state in self._robots.items() if state.blocked and site_of(host_of(b)) == key]:
                del self._robots[base]

    def get(self, url: str, *, retries: int | None = None, html_only: bool = True, scope=None) -> P.FetchResult:
        """Fetch one URL, robots.txt of its host first. `scope(host) -> bool` names the hosts a redirect
        may lead to besides the site of `url` itself (None: only that site).

        Only «/robots.txt» itself is requested unasked: the standard leaves that address open on every site.
        The address is brought to the spelling it is requested in (personalize.request_url) before anything
        else: the rules are asked about that spelling, and the result carries it as `url`."""
        url = P.request_url(url)
        if P.is_robots_txt(url):  # the file itself: plain text, read the way the rules are read
            return self._get(url, retries, html_only=False, scope=scope, check=False)
        return self._get(url, retries, html_only, scope, check=True)

    def _get(self, url: str, retries: int | None, html_only: bool, scope, check: bool) -> P.FetchResult:
        """`check` is False for «/robots.txt» itself and for nothing else."""
        cached = self._cache_read(url)
        if cached is not None:
            if check and cached.status == 200:  # robots.txt may have closed the page since it was cached
                for target in dict.fromkeys((url, cached.final_url or url)):
                    allowed, why = self._robots_check(target)
                    if not allowed:
                        return P.FetchResult(url, cached.final_url, error=why)
            cached.html = self._scrub(cached.html)
            if not getattr(cached, "fetched_on", ""):
                cached.fetched_on = P.today().isoformat()
            with self._count_lock:
                self.cache_hits += 1
            return cached
        if check:
            allowed, why = self._robots_check(url)
            if not allowed:
                return P.FetchResult(url, error=why)
        result = self._fetch_network(url, self.retries if retries is None else retries, html_only, scope, check)
        result.fetched_on = P.today().isoformat()
        # Definitive answers are cached (pages for CACHE_DAYS). Connection failures, pages with almost
        # no text and every unusual answer to robots.txt are cached for NEGATIVE_TTL only. Other
        # refusals (401/403/429/5xx, robots.txt of a redirect target) are asked again next time.
        is_robots = P.is_robots_txt(url)
        if result.status in (200, 404, 410) or (result.status == 0 and result.error) or is_robots \
                or result.error.startswith(OFFSITE):
            thin = result.status == 200 and html_only \
                and P.visible_text_length(result.html) < self.MIN_VISIBLE_TEXT
            short = thin or (is_robots and result.status not in (200, 404, 410))
            self._cache_write(result, short_lived=short)
        result.html = self._scrub(result.html)
        return result

    def robots_note(self, url: str) -> str:
        """What robots.txt of the URL's host said during this run ('' when it was not asked)."""
        parts = urlsplit(url)
        state = self._robots.get(f"{parts.scheme}://{parts.netloc}")
        return state.note if state else ""

    # -- internals ---------------------------------------------------------- #

    def _scrub(self, html: str) -> str:
        """Blank the addresses that were removed with --forget, so they reach neither the
        cache nor the parser."""
        if not self.suppressed or not html:
            return html
        return EMAIL_RE.sub(lambda m: SCRUBBED if is_suppressed(m.group(0).lower(), self.suppressed)
                            else m.group(0), html)

    @contextmanager
    def _turn(self, host: str):
        """One request at a time per site, `delay` seconds after the previous one has ENDED:
        two requests can never leave closer than `delay`, whatever the connection takes."""
        key = site_of(host) or host
        with self._registry_lock:
            lock = self._site_locks.setdefault(key, threading.Lock())
        with lock:
            wait = self._last_done.get(key, float("-inf")) + self.delay - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            try:
                yield
            finally:
                self._last_done[key] = time.monotonic()

    def _sleep_backoff(self, attempt: int, retry_after: str = "") -> None:
        pause = self.backoff * (2 ** attempt + 0.25)
        if retry_after and retry_after.strip().isdigit():
            pause = max(pause, min(float(retry_after), 10.0))
        if pause > 0:
            time.sleep(pause)

    def _request(self, url: str, html_only: bool, as_robots: bool = False) -> tuple[str, int, str, str]:
        """One HTTP request, no redirect followed: (kind, status, payload, URL as sent).

        `as_robots`: the answer is a robots.txt — the body of any 2xx is read (a file served with 203 or
        206 still carries the rules) and decoded as UTF-8, whatever charset the server announces."""
        host = urlsplit(url).hostname or ""
        try:
            with self._turn(host):
                with self._count_lock:
                    self.network_hits += 1
                with self.client.stream("GET", url) as resp:
                    status, sent = resp.status_code, str(resp.url)
                    if 300 <= status < 400 and resp.headers.get("location"):  # any 3xx that names a target
                        return "redirect", status, resp.headers["location"], sent
                    if status != 200 and not (as_robots and 200 <= status < 300):
                        return "http", status, resp.headers.get("retry-after") or "", sent
                    ctype = resp.headers.get("content-type", "")
                    if html_only and ctype and not re.search(r"html|xml", ctype, re.I):
                        return "not_html", status, ctype.split(";")[0], sent
                    body = bytearray()
                    for chunk in resp.iter_bytes():
                        body.extend(chunk)
                        if len(body) >= MAX_PAGE_BYTES:
                            break
                    text = P.decode_robots(bytes(body)) if as_robots else P.decode_html(bytes(body), ctype)
                    return "ok", status, text, sent
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError) as exc:  # DNS / refused / TLS / tunnel
            return "net", 0, f"{NET_ERRORS[0]} ({P._short_error(exc)})", url
        except (httpx.TimeoutException, httpx.TransportError) as exc:  # connected, then silence or a broken stream
            return "net", 0, f"{NET_ERRORS[1]} ({P._short_error(exc)})", url
        except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:  # a bad URL and the like
            return "fatal", 0, P._short_error(exc), url

    def _fetch_network(self, url: str, retries: int, html_only: bool, scope, check: bool) -> P.FetchResult:
        """Request `url` and follow its redirects by hand, checking every hop.

        `check` is False for one address only, «/robots.txt» itself. Its answer is read as a robots.txt, and
        its redirects are followed wherever they lead, to another site too: by RFC 9309 (2.3.1.2) the rules
        of the file at the end of the chain apply to the host that was asked. A chain that does not end in
        a file leaves a 3xx status, and the host is closed (see personalize.read_robots)."""
        as_robots = not check
        home_site = site_of(urlsplit(url).hostname or "")
        current, hops, attempt = url, 0, 0
        while True:
            kind, status, payload, sent = self._request(current, html_only, as_robots)
            if kind == "ok":
                return P.FetchResult(url, sent, status, html=payload)
            if kind == "redirect":
                try:
                    target = P.request_url(urljoin(current, payload))  # the spelling that is checked and requested
                    parts = urlsplit(target)
                    usable = parts.scheme in ("http", "https") and bool(parts.hostname)
                except ValueError:
                    target, usable = "", False
                if not usable:
                    return P.FetchResult(url, target, status, error="редирект на адрес, который нельзя открыть")
                hops += 1
                if hops > MAX_REDIRECTS:
                    return P.FetchResult(url, target, status, error=f"больше {MAX_REDIRECTS} редиректов подряд")
                if not as_robots and site_of(parts.hostname) != home_site \
                        and not (scope is not None and scope(parts.hostname)):
                    return P.FetchResult(url, target, status, error=f"{OFFSITE}: {parts.hostname}")
                if check:
                    allowed, why = self._robots_check(target)
                    if not allowed:
                        return P.FetchResult(url, target, status, error=f"{why} (адрес после редиректа)")
                current = target
                continue
            if kind == "http":
                if status in self.RETRY_STATUSES and attempt < retries:
                    self._sleep_backoff(attempt, payload)
                    attempt += 1
                    continue
                note = " (доступ закрыт)" if status in (401, 403) else ""
                return P.FetchResult(url, sent, status, error=f"HTTP {status}{note}")
            if kind == "not_html":
                return P.FetchResult(url, sent, status, error=f"не HTML ({payload})")
            if kind == "net" and attempt < retries:
                self._sleep_backoff(attempt)
                attempt += 1
                continue
            return P.FetchResult(url, error=payload or "неизвестная ошибка")

    def _robots_state(self, url: str) -> RobotsState:
        parts = urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
        state = self._robots.get(base)
        if state is not None and not (state.blocked and time.monotonic() - state.at > ROBOTS_RETRY_AFTER):
            return state
        site = site_of(parts.hostname or "")
        # one patient attempt per site; its other addresses (www, http) are then probed without waiting twice
        patience = 0 if site in self._down_sites else min(1, self.retries)
        res = self._get(base + "/robots.txt", patience, html_only=False, scope=None, check=False)
        # one reader for every tool of the project: rules / no file, nothing is closed / not received, closed
        answer = P.read_robots(res.status, {}, res.html, self.user_agent, res.error)
        if answer.state == "rules":
            state = RobotsState(answer.rules, "robots.txt разрешает страницу")
        elif answer.state == "open":  # «HTTP 404», «HTTP 200, HTML-страница вместо файла», «HTTP 204, пустой ответ»
            state = RobotsState(None, f"robots.txt на сайте нет ({answer.why}): ограничений нет")
        elif res.status:  # 5xx, 429, or a redirect chain that did not end in a file
            state = RobotsState(None, "", blocked=f"{ROBOTS_DOWN} ({answer.why}): по RFC 9309 сайт не обходится")
        elif is_net_error(res.error):
            state = RobotsState(None, "", blocked=res.error)
            self._down_sites.add(site)
        else:
            state = RobotsState(None, "", blocked=f"{ROBOTS_DOWN} ({answer.why}): сайт не обходится")
        state.at = time.monotonic()
        final = urlsplit(res.final_url or "")
        final_base = f"{final.scheme}://{final.netloc}"
        with self._registry_lock:
            self._robots[base] = state
            if res.status == 200 and final.netloc and final_base != base and final.path == "/robots.txt":
                self._robots.setdefault(final_base, state)  # http -> https, site.ru -> www.site.ru: one file
        if res.status == 200 and final.netloc and final_base != base and final.path == "/robots.txt" \
                and not res.from_cache:
            twin = P.FetchResult(res.final_url, res.final_url, res.status, html=res.html)
            twin.fetched_on = getattr(res, "fetched_on", "")
            self._cache_write(twin)
        return state

    def _robots_check(self, url: str) -> tuple[bool, str]:
        """(may this URL be requested, why not)."""
        state = self._robots_state(url)
        if state.blocked:
            return False, state.blocked
        if state.rules is not None and not state.rules.allows(url):
            return False, ROBOTS_DENIED
        return True, ""

    def _cache_path(self, url: str) -> Path:
        return self.cache_dir / (hashlib.sha1(url.encode("utf-8")).hexdigest() + ".json")

    def _cache_read(self, url: str) -> P.FetchResult | None:
        if not self.cache_dir:
            return None
        try:
            data = json.loads(self._cache_path(url).read_text("utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("v") != CACHE_VERSION:
            return None  # written by older code: redirects were followed blindly, the entry is not trusted
        mine = url in self._written
        if self.refresh and not mine:
            return None
        fetched_at = float(data.get("fetched_at") or 0)
        age = time.time() - fetched_at
        status = int(data.get("status") or 0)
        if P.is_robots_txt(url) and 300 <= status < 400 and not mine:
            return None  # a redirect of robots.txt that an earlier run did not follow to its end: asked again
        if data.get("short_lived") or not status:
            if age > NEGATIVE_TTL or (not mine and site_of(host_of(url)) in self._retry_sites):
                return None  # a stale or distrusted failure: the network is asked again
        elif P.is_robots_txt(url):
            if age > ROBOTS_TTL:
                return None
        elif self.cache_days and age > self.cache_days * 86400:
            return None  # the page may have changed: the evidence must be fetched again
        res = P.FetchResult(url=url, final_url=data.get("final_url", ""), status=status,
                            html=data.get("html", ""), error=data.get("error", ""), from_cache=True)
        res.fetched_on = str(data.get("fetched_on") or "") or date.fromtimestamp(fetched_at).isoformat()
        return res

    def _cache_write(self, result: P.FetchResult, *, short_lived: bool = False) -> None:
        if not self.cache_dir:
            return
        result.html = self._scrub(result.html)
        self._written.add(result.url)
        payload = {"v": CACHE_VERSION, "url": result.url, "final_url": result.final_url, "status": result.status,
                   "html": result.html, "error": result.error, "fetched_at": int(time.time()),
                   "fetched_on": getattr(result, "fetched_on", "") or P.today().isoformat(),
                   "short_lived": short_lived}
        fd, tmp = tempfile.mkstemp(dir=self.cache_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            os.replace(tmp, self._cache_path(result.url))
        except OSError as exc:
            log.warning("не удалось записать кэш для %s: %s", result.url, exc)
            Path(tmp).unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# HTML -> atoms: visible text runs that remember where in the DOM they sit
# --------------------------------------------------------------------------- #

@dataclass
class Mention:
    kind: str  # name | title | email | phone | dept
    start: int  # offset in Doc.text
    end: int
    atom: int
    text: str
    data: dict = field(default_factory=dict)


@dataclass
class Atom:
    idx: int
    text: str
    start: int  # offset of the atom in Doc.text
    path: tuple[int, ...]  # ids of the enclosing block elements, root first
    chrome: bool = False  # site header / footer / navigation
    heading: int = 0  # 1..6 inside <h1>..<h6>
    section: str = ""  # text of the nearest heading above
    href_emails: tuple[str, ...] = ()  # addresses of mailto: links in this run


@dataclass
class Doc:
    url: str
    atoms: list[Atom] = field(default_factory=list)
    text: str = ""
    links: list[tuple[str, str, int]] = field(default_factory=list)  # (absolute url, anchor text, atom index)
    title: str = ""
    spans: dict[int, tuple[int, int]] = field(default_factory=dict)  # element id -> atom index range
    mentions: list[Mention] = field(default_factory=list)  # sorted by position
    elem_names: dict[int, set[str]] = field(default_factory=dict)  # element id -> person keys inside
    chrome_emails: set[str] = field(default_factory=set)
    sigs: dict[int, str] = field(default_factory=dict)  # element id -> "tag.class": siblings of one kind

    def of_kind(self, kind: str) -> list[Mention]:
        return [m for m in self.mentions if m.kind == kind]


def _cf_decode(tag: Tag) -> str:
    code = tag.get("data-cfemail") or ""
    if not code:
        m = re.search(r"/cdn-cgi/l/email-protection#([0-9a-f]+)", tag.get("href") or "", re.I)
        code = m.group(1) if m else ""
    return P._decode_cfemail(code).strip().lower() if code else ""


def _mailto_address(href: str) -> str:
    if not href.lower().startswith("mailto:"):
        return ""
    value = unquote(href[7:]).split("?", 1)[0].strip().lower()
    return value if EMAIL_RE.fullmatch(value) else ""


SOLO_WORD_RE = re.compile(r"[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?|[А-ЯЁ]{3,}")


def _solo_name_word(text: str) -> bool:
    """Is this whole run one capitalised word that may be half of a personal name?"""
    if not SOLO_WORD_RE.fullmatch(text):
        return False
    low = norm(text)
    return len(low) >= 3 and low not in SURNAME_STOP and not TITLE_START_RE.fullmatch(low) and not DEPT_RE.match(low)


def flatten(url: str, html: str) -> Doc:
    """Split a page into atoms: runs of visible text bounded by block elements and <br>.

    HTML comments are dropped (a name that the site has commented out is not published),
    scripts and styles too. Cloudflare-protected addresses are shown the way a browser
    shows them. Every atom keeps the chain of enclosing block elements: that chain is
    what "the same card / table row / list item" means later. A <br> right after a single
    capitalised word does not end the run: «Образцов<br>Пётр» is one name.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    for node in soup.find_all(string=lambda s: isinstance(s, Comment)):
        node.extract()
    doc = Doc(url=url, title=P.collapse_ws(soup.title.get_text(" ")) if soup.title else "")
    for tag in soup.find_all(list(SKIP_TAGS)):
        tag.decompose()
    root = soup.body or soup
    counter = [0]
    buf: list[str] = []
    run = {"path": (), "chrome": False, "heading": 0, "hrefs": [], "links": []}
    section = [""]
    pos = [0]
    parts: list[str] = []

    def flush() -> None:
        text = P.collapse_ws("".join(buf).replace("\u200b", "").replace("\ufeff", ""))
        buf.clear()
        hrefs, links = tuple(dict.fromkeys(run["hrefs"])), list(run["links"])
        run["hrefs"], run["links"] = [], []
        if not text and not hrefs:
            for href, anchor in links:  # a link that wraps a whole card: its text is in the atoms that follow
                doc.links.append((href, anchor, len(doc.atoms)))
            return
        atom = Atom(idx=len(doc.atoms), text=text, start=pos[0], path=run["path"], chrome=run["chrome"],
                    heading=run["heading"], section=section[0], href_emails=hrefs)
        doc.atoms.append(atom)
        parts.append(text)
        pos[0] += len(text) + 1
        if atom.heading and atom.heading <= 5 and text:
            section[0] = text[:120]
        for href, anchor in links:
            doc.links.append((href, anchor, atom.idx))

    def walk(node: Tag, path: tuple[int, ...], chrome: bool, heading: int, content: bool) -> None:
        for child in node.children:
            if isinstance(child, NavigableString):
                if not buf:
                    run.update(path=path, chrome=chrome, heading=heading)
                buf.append(str(child))
                continue
            if not isinstance(child, Tag):
                continue
            name = (child.name or "").lower()
            if name == "br" and _solo_name_word(P.collapse_ws("".join(buf))):
                buf.append(" ")  # a name broken over two lines stays one run
                continue
            if name in ("br", "hr"):
                flush()
                continue
            if child.get("data-cfemail"):  # Cloudflare: the browser shows the decoded address
                if not buf:
                    run.update(path=path, chrome=chrome, heading=heading)
                buf.append(f" {_cf_decode(child)} ")
                continue
            if name == "a" and child.get("href"):
                href = str(child.get("href")).strip()
                address = _mailto_address(href) or (_cf_decode(child) if "email-protection" in href else "")
                if address:
                    if not buf:
                        run.update(path=path, chrome=chrome, heading=heading)
                    run["hrefs"].append(address)
                elif not href.startswith(("#", "tel:", "javascript:", "mailto:")):
                    run["links"].append((urljoin(url, href), P.collapse_ws(child.get_text(" "))))
            if name in BLOCK_TAGS:
                flush()
                counter[0] += 1
                classes = child.get("class") or []
                doc.sigs[counter[0]] = ".".join([name, *sorted(classes if isinstance(classes, list) else [classes])])
                level = int(name[1]) if re.fullmatch(r"h[1-6]", name) else heading
                is_chrome = chrome or name == "nav" or (name in CHROME_TAGS and not content)
                walk(child, path + (counter[0],), is_chrome, level, content or name in CONTENT_TAGS)
                flush()
            else:  # inline element: the run it starts belongs to the enclosing block
                if not buf:
                    run.update(path=path, chrome=chrome, heading=heading)
                buf.append(" ")
                walk(child, path, chrome, heading, content)
                buf.append(" ")

    walk(root, (0,), False, 0, False)
    flush()
    doc.text = " ".join(parts)
    last = max(0, len(doc.atoms) - 1)
    doc.links = [(href, anchor, min(idx, last)) for href, anchor, idx in doc.links]
    for atom in doc.atoms:
        for el in atom.path:
            lo, hi = doc.spans.get(el, (atom.idx, atom.idx))
            doc.spans[el] = (min(lo, atom.idx), max(hi, atom.idx))
    return doc


# --------------------------------------------------------------------------- #
# Mentions: names, titles, emails, phones
# --------------------------------------------------------------------------- #

@dataclass
class Person:
    surname: str = ""
    first: str = ""
    patronymic: str = ""
    kind: str = "full"  # full | no_surname | initials | latin
    raw: str = ""

    @property
    def key(self) -> str:
        if self.surname:
            return f"{norm(self.surname)}|{norm(self.first)[:1]}"
        return f"|{norm(self.first)}|{norm(self.patronymic)}"

    @property
    def display(self) -> str:
        return " ".join(x for x in (self.surname, self.first, self.patronymic) if x)


def _cap(word: str) -> str:
    return "-".join(part.capitalize() for part in word.split("-")) if word.isupper() else word


def _is_patronymic(word: str) -> bool:
    low = norm(word)
    return len(low) >= 6 and bool(PATRONYMIC_RE.search(low)) or low in SHORT_PATRONYMICS


def _from_known_name(word: str) -> bool:
    """Is this «-ович / -овна» word derived from a known first name? «Шаблонович» and «Примеркевич»
    are surnames; without a third word only the stem can tell them from a patronymic."""
    m = re.match(r"(.+?)(?:ович|евич|ич|овна|евна|инична|ична)$", norm(word))
    if not m:
        return False
    stem = m.group(1)
    soft = stem[:-1] + "ий" if stem.endswith("ь") else stem + "ий"  # Юрьевич -> юрий, Дмитриевич -> дмитрий
    options = {stem, stem + "й", stem + "ь", stem + "а", stem + "я", soft, stem.rstrip("и") + "ий"}
    return bool(options & _MALE_NAMES) or stem in _IRREGULAR_PATRONYMIC_STEMS


def _is_first(word: str) -> bool:
    return norm(word) in RU_FIRST_NAMES or word.lower() in RU_FIRST_NAMES


def _surname_ok(word: str) -> bool:
    low = norm(word)
    return len(low) >= 2 and low not in SURNAME_STOP and not TITLE_START_RE.fullmatch(low)


def _name_from_tokens(words: list[str]) -> tuple[Person, int] | None:
    """Try to read a personal name at the start of a run of capitalised words.

    Returns (person, number of words used). Three-word forms need a patronymic, two-word
    forms need a known first name, so «Генеральный Директор» or «Нижний Новгород» never pass.
    """
    w = [_cap(x) for x in words[:3]]
    if len(w) == 3:
        if _is_patronymic(w[2]) and not _is_patronymic(w[1]) and _surname_ok(w[0]) and _surname_ok(w[1]):
            return Person(surname=w[0], first=w[1], patronymic=w[2]), 3  # Фамилия Имя Отчество
        if _is_patronymic(w[1]) and _surname_ok(w[0]) and _surname_ok(w[2]) \
                and (_is_first(w[0]) or not _is_patronymic(w[2])):
            return Person(surname=w[2], first=w[0], patronymic=w[1]), 3  # Имя Отчество Фамилия
    if len(w) >= 2:
        if _is_first(w[0]) and _is_patronymic(w[1]) and _from_known_name(w[1]):
            return Person(first=w[0], patronymic=w[1], kind="no_surname"), 2  # Имя Отчество
        if _is_first(w[0]) and _is_patronymic(w[1]) and _surname_ok(w[1]):
            return Person(surname=w[1], first=w[0]), 2  # Имя Фамилия: «Олег Шаблонович»
        if _is_first(w[0]) and _surname_ok(w[1]) and not _is_patronymic(w[1]):
            return Person(surname=w[1], first=w[0]), 2  # Имя Фамилия
        if _is_first(w[1]) and _surname_ok(w[0]) and not _is_first(w[0]):
            return Person(surname=w[0], first=w[1]), 2  # Фамилия Имя
    return None


TWO_CAPS_RE = re.compile(r"([А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?)\s+([А-ЯЁ][а-яё]+)")
UNREAD_NAME_RE = re.compile(r"([А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?|[A-Z][a-z]+)\s+([А-ЯЁ][а-яё]+|[A-Z][a-z]+)(?![\w-])")
TITLE_AFTER_NAME_STRIP = " ,—–-:("


def _probable_name(first_word: str, second_word: str) -> Person | None:
    """«Примеров Зульфат»: a typical surname next to a capitalised word that is not in the
    dictionary of first names. The caller accepts it only when a job title follows."""
    a, b = _cap(first_word), _cap(second_word)
    if not (_surname_ok(a) and _surname_ok(b)) or _is_patronymic(a) or _is_patronymic(b):
        return None
    if len(norm(a)) < 4 or len(norm(b)) < 3 or DEPT_RE.match(norm(a)) or DEPT_RE.match(norm(b)):
        return None
    a_surname, b_surname = (bool(SURNAME_SUFFIX_RE.search(norm(w))) for w in (a, b))
    if a_surname and not b_surname:
        return Person(surname=a, first=b)
    if b_surname and not a_surname:
        return Person(surname=b, first=a)
    return None


def find_names(text: str) -> list[tuple[int, int, Person]]:
    """Personal names in a text run: (start, end, person), in order, without overlaps."""
    found: list[tuple[int, int, Person]] = []
    original = text
    text = MIXED_WORD_RE.sub(lambda m: m.group(0).translate(HOMOGLYPHS), text)  # same length: offsets hold

    def free(a: int, b: int) -> bool:
        return all(b <= s or a >= e for s, e, _ in found)

    def loose(person: Person, first_word: str, start: int, end: int) -> bool:
        """A two-word «Фамилия Имя» that is really a piece of a sentence."""
        if person.patronymic or person.surname != _cap(first_word) or SURNAME_SUFFIX_RE.search(norm(person.surname)):
            return False
        tail = text[end:end + 40]
        continues = re.match(r"\s+[а-яё]", tail) and not TITLE_START_RE.match(tail.lstrip())
        return bool(continues or WORD_BEFORE_NAME_RE.search(text[max(0, start - 12):start]))

    tokens = [(m.start(), m.end(), m.group(0)) for m in NAME_TOKEN_RE.finditer(text)]
    i = 0
    while i < len(tokens):
        run = [tokens[i]]
        while i + len(run) < len(tokens) and len(run) < 3:
            nxt = tokens[i + len(run)]
            gap = text[run[-1][1]:nxt[0]]
            if not gap or gap.strip() or len(gap) > 3:
                break
            run.append(nxt)
        hit = _name_from_tokens([t[2] for t in run])
        if hit is None and len(run) >= 2:  # an unknown first name is accepted right before a job title
            person = _probable_name(run[0][2], run[1][2])
            if person and TITLE_START_RE.match(text[run[1][1]:].lstrip(TITLE_AFTER_NAME_STRIP)):
                hit = (person, 2)
        if hit and not STREET_BEFORE_RE.search(text[max(0, run[0][0] - 14):run[0][0]]) \
                and not loose(hit[0], run[0][2], run[0][0], run[hit[1] - 1][1]):
            person, used = hit
            start, end = run[0][0], run[used - 1][1]
            person.raw = original[start:end]
            found.append((start, end, person))
            i += used
        else:
            i += 1
    for regex, order in ((SURNAME_INITIALS_RE, "sio"), (INITIALS_SURNAME_RE, "ios")):
        for m in regex.finditer(text):
            if not free(m.start(), m.end()) or STREET_BEFORE_RE.search(text[max(0, m.start() - 14):m.start()]):
                continue
            surname, a, b = (m.group(1), m.group(2), m.group(3)) if order == "sio" else \
                (m.group(3), m.group(1), m.group(2))
            if not _surname_ok(surname):
                continue
            found.append((m.start(), m.end(), Person(surname=surname, first=f"{a}.", patronymic=f"{b}.",
                                                     kind="initials", raw=m.group(0))))
    for m in LATIN_NAME_RE.finditer(text):
        first, surname = m.group(1), m.group(2)
        if first.lower() in LATIN_FIRST_NAMES and surname.lower() not in LATIN_STOP \
                and surname.lower() not in LATIN_FIRST_NAMES and free(m.start(), m.end()):
            found.append((m.start(), m.end(), Person(surname=surname, first=first, kind="latin", raw=m.group(0))))
    return sorted(found, key=lambda item: item[0])


def find_emails(text: str) -> list[tuple[int, int, str]]:
    """Addresses printed in the text, plain or obfuscated ('name [at] site.ru')."""
    found: list[tuple[int, int, str]] = []
    for m in EMAIL_RE.finditer(text):
        address = m.group(0).strip(".").lower()
        if not JUNK_EMAIL_RE.search(address):
            found.append((m.start(), m.end(), address))
    for m in SPLIT_ZONE_RE.finditer(text):
        address = f"{m.group(1)}.{m.group(2)}".lower()
        if not any(m.start() < e and s < m.end() for s, e, _ in found) and not JUNK_EMAIL_RE.search(address):
            found.append((m.start(), m.end(), address))
    for m in OBFUSCATED_RE.finditer(text):
        if any(m.start() < e and s < m.end() for s, e, _ in found):
            continue
        domain = re.sub(r"\s*(?:\[\s*(?:dot|точка)\s*\]|\(\s*(?:dot|точка)\s*\)|\.)\s*", ".", m.group(2), flags=re.I)
        address = f"{m.group(1)}@{domain}".lower()
        if EMAIL_RE.fullmatch(address) and not JUNK_EMAIL_RE.search(address):
            found.append((m.start(), m.end(), address))
    return sorted(found, key=lambda item: item[0])


def find_titles(text: str, blocked: list[tuple[int, int]]) -> list[tuple[int, int, str]]:
    """Job titles in a text run: (start, end, title). `blocked` are the spans of names
    and addresses - a title never runs into them."""
    found: list[tuple[int, int, str]] = []
    cursor = 0
    for m in TITLE_START_RE.finditer(text):
        if m.start() < cursor or any(s <= m.start() < e for s, e in blocked):
            continue
        words = m.group(0).split()
        head = words[-1]
        if PLURAL_HEAD_RE.search(head) and not re.fullmatch(r"(?i)ceo|cco|cmo|coo|cfo|cto|cio|cro|cbdo|vp", head):
            continue
        if OBLIQUE_HEAD_RE.search(head) and not DEPUTY_RE.search(m.group(0)) \
                and not re.match(r"(?i)помощни|ассистент|секретар|советник", m.group(0)):
            continue
        limit = min(len(text), m.end() + MAX_TITLE_CHARS)
        end = limit
        stop = TITLE_STOP_RE.search(text, m.end(), limit)
        if stop:
            end = stop.start()
        for s, _ in blocked:
            if m.end() <= s < end:
                end = s
        title = text[m.start():end].rstrip(" ,-–—(«")
        title = re.sub(r"[\s,]+(?:и|а|или|and|or)$", "", title)  # «директор, и Петров ...»
        if title:
            found.append((m.start(), m.start() + len(title), title))
            cursor = m.start() + len(title)
    return found


def classify_role(title: str, dept: str = "") -> tuple[str, bool]:
    """(role group, is deputy) for a job title; the department helps with «начальник отдела»."""
    low = norm(title)
    if dept and GENERIC_HEAD_TITLE_RE.match(low):
        low = f"{low} {norm(dept)}"
    deputy = bool(DEPUTY_RE.search(low))
    if STAFF_START_RE.match(low):
        return "staff", deputy
    if deputy:
        # «Заместитель генерального директора - начальник службы качества»: the second title says
        # what the person really runs, the words «генерального директора» only say whose deputy he is
        parts = SECOND_TITLE_SPLIT_RE.split(low, maxsplit=1)
        if len(parts) == 2 and TITLE_START_RE.match(parts[1]) and not DEPUTY_RE.search(parts[1]):
            return classify_role(parts[1])[0], True
    for group, regex in ROLE_PATTERNS:
        m = regex.search(low)
        if m and not re.search(r"менеджер|специалист|представител", low[:m.start() + 1]):
            return group, deputy
    if OTHER_FUNCTION_RE.search(low):
        return "other_director", deputy
    if BRANCH_RE.search(low):
        return "branch", deputy
    if CEO_RE.search(low):
        return "ceo", deputy
    if DEPT_HEAD_RE.search(low):
        return "dept_head", deputy
    if re.search(r"директор|director|партн[её]р|partner|президент|председател", low):
        return "other_director", deputy
    if re.match(r"руководител|начальник|глава", low):
        return "dept_head", deputy  # head of something the page does not name
    return "staff", deputy


def annotate(doc: Doc) -> Doc:
    """Find every name, title, address, phone and department label on the page."""
    mentions: list[Mention] = []
    names_of = [find_names(atom.text) for atom in doc.atoms]
    for atom, following in zip(doc.atoms, doc.atoms[1:], strict=False):
        # «Примеров Зульфат» alone in its element, the title in the next one
        m = TWO_CAPS_RE.fullmatch(atom.text) if not names_of[atom.idx] and not atom.chrome else None
        person = _probable_name(m.group(1), m.group(2)) if m else None
        if person and TITLE_START_RE.match(following.text) and not names_of[following.idx]:
            person.raw = atom.text
            names_of[atom.idx] = [(0, len(atom.text), person)]
    for atom in doc.atoms:
        base = atom.start
        emails = find_emails(atom.text)
        names = names_of[atom.idx]
        visible = {e for _, _, e in emails}
        for s, e, address in emails:
            mentions.append(Mention("email", base + s, base + e, atom.idx, address, {"visible": True}))
        for address in atom.href_emails:
            if address not in visible and not JUNK_EMAIL_RE.search(address):
                at = base + len(atom.text)
                mentions.append(Mention("email", at, at, atom.idx, address, {"visible": False}))
        for s, e, person in names:
            mentions.append(Mention("name", base + s, base + e, atom.idx, person.raw, {"person": person}))
        blocked = [(s, e) for s, e, _ in names] + [(s, e) for s, e, _ in emails]
        titles = find_titles(atom.text, blocked)
        for s, e, title in titles:
            mentions.append(Mention("title", base + s, base + e, atom.idx, title))
        for m in PHONE_RE.finditer(atom.text):
            mentions.append(Mention("phone", base + m.start(), base + m.end(), atom.idx, P.collapse_ws(m.group(0))))
        if DEPT_RE.match(atom.text) and not names and not titles and len(atom.text) <= 160:
            cut = re.split(r"[:|]|\s[-–—]\s|\+?\d[\d\s()\-]{6,}", atom.text, maxsplit=1)[0].strip()
            mentions.append(Mention("dept", base, base + len(cut), atom.idx, cut))
    mentions.sort(key=lambda m: (m.start, m.kind != "name"))
    doc.mentions = mentions
    for m in mentions:
        atom = doc.atoms[m.atom]
        if m.kind == "name":
            for el in atom.path:
                doc.elem_names.setdefault(el, set()).add(m.data["person"].key)
        elif m.kind == "email" and atom.chrome:
            doc.chrome_emails.add(m.text)
    return doc


# --------------------------------------------------------------------------- #
# Pairing: who does an address belong to?
# --------------------------------------------------------------------------- #

CARD_MAX_CHARS = 700  # a "card" is a compact block around one person; bigger blocks are read in order


@dataclass
class Candidate:
    """One address found on a page and what stands next to it."""

    email: str
    url: str
    visible: bool = True  # printed as text (False: only inside a mailto: link)
    chrome: bool = False
    person: Person | None = None
    title: str = ""
    dept: str = ""
    gap: int = -1  # characters of visible text between the name and the address
    title_gap: int = 0  # ... and between the name and the title
    card_digits: tuple[str, ...] = ()  # phone numbers and extensions printed next to the person (digits only)
    method: str = ""  # card | sequence | sequence+local | zip | llm
    title_adjacent: bool = False
    fragment: str = ""
    section: str = ""
    reason: str = ""  # why no person was attached (R_NO_NAME / R_AMBIGUOUS)
    block: str = ""  # page text around the address (shown to the LLM)
    in_card: bool = False  # the address stands inside this person's own card
    llm: str = ""  # '' | confirmed (the model agreed with the pairing) | chosen (the model made it)
    email_span: tuple[int, int] = (0, 0)
    name_options: list[tuple[Person, int, int]] = field(default_factory=list)
    title_options: list[tuple[str, int, int]] = field(default_factory=list)


def _initial_sets(person: Person) -> dict[str, str]:
    """Latin letters each part of a name may be shortened to: {'s': ..., 'f': ..., 'p': ...}."""
    out = {}
    for label, value in (("s", person.surname), ("f", person.first), ("p", person.patronymic)):
        ch = norm(value)[:1]
        if ch:
            out[label] = INITIAL_LETTERS.get(ch, ch if ch.isascii() else "")
    return out


def local_matches_person(local: str, person: Person) -> str:
    """How the local part of an address agrees with a name: 'фамилия', 'имя', 'инициалы' or ''.

    This only CONFIRMS an address printed on the page; nothing is ever built from a name.
    """
    local = local.lower()
    tokens = [t for t in re.split(r"[^a-z]+", local) if t]
    letters = "".join(tokens)
    if not letters:
        return ""
    initials = _initial_sets(person)
    any_initial = "".join(initials.values())
    surname_key = P.squash(person.surname)
    if len(surname_key) >= 3:
        for token in dict.fromkeys(tokens + [letters]):
            key = P.squash(token)
            if len(key) < 3:
                continue
            if key == surname_key or surname_key.startswith(key) or (len(surname_key) >= 5 and surname_key[:-1] in key):
                return "фамилия"
            if len(key) >= 4 and SequenceMatcher(None, key, surname_key).ratio() >= 0.8:
                return "фамилия"
    first = norm(person.first)
    if person.kind != "initials" and len(first) >= 3:
        first_keys = {P.squash(first)} | {P.squash(a) for a in FIRST_NAME_ALIASES.get(first, ())}
        for token in tokens:
            key = P.squash(token)
            if len(key) < 3:
                continue
            for cand in first_keys:
                if key == cand or (len(key) >= 5 and SequenceMatcher(None, key, cand).ratio() >= 0.84):
                    return "имя"
                common = len(os.path.commonprefix([key, cand]))
                rest = key[common:]
                if common >= 3 and len(rest) <= 2 and all(ch in any_initial for ch in rest):
                    return "имя"
    if 2 <= len(letters) <= 3 and "f" in initials and (len(initials) >= 2):
        parts = [k for k in ("f", "s", "p") if initials.get(k)]
        for size in (len(letters),):
            for perm in permutations(parts, min(size, len(parts))):
                if len(perm) == size and all(ch in initials[k] for ch, k in zip(letters, perm, strict=True)):
                    return "инициалы"
        if len(letters) == 3 and "p" not in initials and "s" in initials:
            # patronymic is not printed: two letters must be the name and the surname, one is free
            for i, j in ((0, 1), (1, 0), (0, 2), (2, 0), (1, 2), (2, 1)):
                if letters[i] in initials["f"] and letters[j] in initials["s"]:
                    return "инициалы"
    if 4 <= len(letters) <= 6 and len(tokens) == 1 and {"f", "p"} <= set(initials) and len(surname_key) >= 4:
        # «obpi» = ОБразцов Пётр Ильич: the start of the surname plus both initials, either order
        surname = re.sub(r"[^a-z]", "", P.translit(person.surname))
        for size in range(2, len(letters) - 1):
            for prefix, rest in ((letters[:size], letters[size:]), (letters[-size:], letters[:-size])):
                if len(rest) == 2 and surname.startswith(prefix) and rest[0] in initials["f"] \
                        and rest[1] in initials["p"]:
                    return "инициалы"
    return ""


def _gap(name: Mention, email: Mention) -> int:
    return max(0, email.start - name.end) if email.start >= name.start else max(0, name.start - email.end)


def _title_orientation(scope: list[Mention]) -> bool:
    """True when titles stand before names on this page («Директор — Иванов»), False when after."""
    chain = [m for m in scope if m.kind in ("name", "title")]
    title_first = sum(1 for a, b in zip(chain, chain[1:], strict=False) if a.kind == "title" and b.kind == "name")
    name_first = sum(1 for a, b in zip(chain, chain[1:], strict=False) if a.kind == "name" and b.kind == "title")
    return title_first > name_first


def _pick_title(scope: list[Mention], name: Mention, strict: bool) -> tuple[Mention | None, bool]:
    """The title that belongs to `name`: the neighbour before or after it, with no other
    person, address or department in between. In a card (strict=False) any title of the
    card may be used, the nearest one first."""
    key = name.data["person"].key
    pos = scope.index(name)
    before = after = None
    for m in reversed(scope[:pos]):
        if m.kind == "title":
            before = m
            break
        if m.kind in ("email", "dept") or (m.kind == "name" and m.data["person"].key != key):
            break
    for m in scope[pos + 1:]:
        if m.kind == "title":
            after = m
            break
        if m.kind in ("email", "dept") or (m.kind == "name" and m.data["person"].key != key):
            break
    if before and after:
        if before.atom == name.atom and after.atom != name.atom:
            return before, True
        if after.atom == name.atom and before.atom != name.atom:
            return after, True
        return (before, True) if _title_orientation(scope) else (after, True)
    if before or after:
        return before or after, True
    if strict:
        return None, False
    titles = [m for m in scope if m.kind == "title" and abs(m.start - name.start) <= MAX_GAP]
    if not titles:
        return None, False
    return min(titles, key=lambda m: abs(m.start - name.start)), False


def _sequence_pair(scope: list[Mention], email: Mention) -> tuple[Mention | None, str, list[Mention]]:
    """Reading-order pairing inside a block with several people.

    Walk back from the address to the previous address / department label: the names in
    between are the candidates. One name -> it owns the address. Several names -> the
    address must agree with one of them, or names and addresses must line up one to one
    (two names followed by two addresses); otherwise the block is ambiguous.
    """
    pos = scope.index(email)
    names: list[Mention] = []
    for m in reversed(scope[:pos]):
        if m.kind == "email" and names:
            break
        if m.kind == "dept":
            break
        if m.kind == "name":
            names.append(m)
    names.reverse()
    distinct: dict[str, Mention] = {}
    for m in names:
        distinct[m.data["person"].key] = m  # the occurrence closest to the address wins
    people = list(distinct.values())
    local = email.text.split("@", 1)[0]
    matching = [m for m in people if local_matches_person(local, m.data["person"])]
    if len(matching) == 1:
        return matching[0], "sequence+local" if len(people) > 1 else "sequence", people
    if len(people) == 1:
        return people[0], "sequence", people
    if len(people) > 1:
        run = [email]
        for m in reversed(scope[:pos]):
            if m.kind != "email":
                break
            run.insert(0, m)
        for m in scope[pos + 1:]:
            if m.kind != "email":
                break
            run.append(m)
        if len(run) == len(people):
            return people[run.index(email)], "zip", people
        return None, R_AMBIGUOUS, people
    following: list[Mention] = []
    for m in scope[pos + 1:]:
        if m.kind in ("email", "dept"):
            break
        if m.kind == "name":
            following.append(m)
    matching = [m for m in following if local_matches_person(local, m.data["person"])]
    if len(matching) == 1:
        return matching[0], "sequence+local", following
    return None, R_NO_NAME, following


def _in_another_card(doc: Doc, name: Mention, email: Mention) -> bool:
    """Does this name sit in a compact block with an address of its own? Then it is the
    previous card of a list printed «address first, name second», not the owner of `email`."""
    for el in reversed(doc.atoms[name.atom].path):
        lo, hi = doc.spans[el]
        inside = [m for m in doc.mentions if m.kind == "email" and lo <= m.atom <= hi]
        if inside:
            size = doc.atoms[hi].start + len(doc.atoms[hi].text) - doc.atoms[lo].start
            return email not in inside and size <= CARD_MAX_CHARS
    return False


def _foreign_card(doc: Doc, name: Mention, email: Mention) -> bool:
    """Is the chosen name the person of ANOTHER card, while the address sits in a card of its
    own whose person the tool could not read (a rare name, a name inside a picture)?

    The name and the address live in two sibling blocks. Inside one card that is normal
    (<div class=name> + <div class=contacts>). It is two cards when the block of the address
    has a person of its own - a job title, or capitalised words at its start - and the block
    of the name is a complete card (its own title, phone or address) or a sibling of the same
    kind (two <li>, two <div class=item>). An address that agrees with the name is never refused.
    """
    if local_matches_person(email.text.split("@", 1)[0], name.data["person"]):
        return False
    a, b = doc.atoms[name.atom].path, doc.atoms[email.atom].path
    depth = 0
    while depth < min(len(a), len(b)) and a[depth] == b[depth]:
        depth += 1
    if depth >= len(a) or depth >= len(b):
        return False  # one of the two is printed directly in the common block: no card border between them
    name_el, email_el = a[depth], b[depth]
    if doc.elem_names.get(email_el):
        return False  # the block of the address names people itself: the reading-order rules decide
    (nlo, nhi), (elo, ehi) = doc.spans[name_el], doc.spans[email_el]
    first_text = next((doc.atoms[i].text for i in range(elo, ehi + 1) if doc.atoms[i].text), "")
    unread = UNREAD_NAME_RE.match(first_text)
    own_person = any(m.kind == "title" and elo <= m.atom <= ehi for m in doc.mentions) \
        or bool(unread and _surname_ok(unread.group(1)) and _surname_ok(unread.group(2)))
    if not own_person:
        return False
    complete = any(m.kind in ("title", "phone", "email") and nlo <= m.atom <= nhi for m in doc.mentions)
    sig = doc.sigs.get(name_el, "")
    same_kind = sig == doc.sigs.get(email_el) and ("." in sig or sig in ("li", "tr"))
    return complete or same_kind


def extract_candidates(doc: Doc) -> list[Candidate]:
    """Every address of the page with the person and the title printed next to it.

    DOM proximity first: the smallest element around the address that holds a person. If
    it holds exactly one person and is compact, that is a card (table row, list item) and
    the pairing is certain. Otherwise the block is read in order (see _sequence_pair).
    """
    body = [m for m in doc.mentions if not doc.atoms[m.atom].chrome]
    out: list[Candidate] = []
    for email in (m for m in doc.mentions if m.kind == "email"):
        atom = doc.atoms[email.atom]
        cand = Candidate(email=email.text, url=doc.url, visible=email.data["visible"], chrome=atom.chrome,
                         section=atom.section, email_span=(email.start, email.end),
                         block=mask_mobiles(doc.text[max(0, email.start - 600):email.end + 200]))
        out.append(cand)
        if atom.chrome:
            cand.reason = R_GENERIC
            continue
        anchor = next((el for el in reversed(atom.path) if doc.elem_names.get(el)), None)
        if anchor is None:
            cand.reason = R_NO_NAME
            continue
        keys = doc.elem_names[anchor]
        lo, hi = doc.spans[anchor]
        size = doc.atoms[hi].start + len(doc.atoms[hi].text) - doc.atoms[lo].start
        card = anchor
        if len(keys) == 1:  # grow the card while it still holds this one person only
            for el in reversed(atom.path[:atom.path.index(anchor)]):
                if doc.elem_names.get(el) != keys:
                    break
                card = el
        clo, chi = doc.spans[card]
        scope = [m for m in body if clo <= m.atom <= chi and m.kind != "phone"]
        is_card = len(keys) == 1 and size <= CARD_MAX_CHARS
        level = atom.path.index(card)
        if is_card and level > 0 and not any(m.kind == "title" for m in scope):
            # The card names no title (a <dd> under its <dt>, a cell under a heading):
            # read the enclosing block in order instead, the title stands right before the name.
            clo, chi = doc.spans[atom.path[level - 1]]
            scope = [m for m in body if clo <= m.atom <= chi and m.kind != "phone"]
            is_card = False
        if is_card:
            inner = [m for m in scope if m.kind == "name"]
            name = min(inner, key=lambda m: _gap(m, email))
            method, people, strict = "card", [name], False
            if name.start > email.start:
                # The block's only name FOLLOWS the address («Ваш менеджер …» under a person's details).
                # If the page names somebody right before the address, that person owns it.
                wide = [m for m in body if m.kind != "phone"]
                before = _sequence_pair(wide, email)
                if before[0] is not None and before[0].start < email.start \
                        and _gap(before[0], email) <= MAX_GAP \
                        and before[0].data["person"].key != name.data["person"].key \
                        and not _in_another_card(doc, before[0], email):
                    (name, method, people), scope, strict = before, wide, True
                    clo, chi = 0, len(doc.atoms) - 1
        else:
            name, method, people = _sequence_pair(scope, email)
            if name is None and method == R_NO_NAME and len(scope) < len(body):
                # Nobody is named before the address inside its own container (a person page: the
                # name is the <h1> in another section). Read the whole page in order instead; the
                # distance limit still applies.
                wide = [m for m in body if m.kind != "phone"]
                found = _sequence_pair(wide, email)
                if found[0] is not None:
                    (name, method, people), scope, (clo, chi) = found, wide, (0, len(doc.atoms) - 1)
            strict = True
        cand.name_options = [(m.data["person"], m.start, m.end) for m in people]
        cand.title_options = [(m.text, m.start, m.end) for m in scope
                              if m.kind == "title" and abs(m.start - email.start) <= 2 * MAX_GAP]
        if name is None:
            cand.reason = method
            continue
        if _foreign_card(doc, name, email):
            cand.reason, cand.name_options = R_NO_NAME, []  # nobody of this card is named: nothing to choose from
            continue
        _attach(doc, cand, scope, name, email, method, strict, (clo, chi))
    return out


def _attach(doc: Doc, cand: Candidate, scope: list[Mention], name: Mention, email: Mention, method: str,
            strict: bool, card: tuple[int, int]) -> None:
    """Fill a candidate with the chosen person: title, department, distance, evidence.

    The phone printed in the person's card is NOT taken: it is that person's number, often a mobile
    one. Its digits are only compared with a numbered mailbox («117@» next to «доб. 117»), and in
    the evidence fragment mobile numbers are masked.
    """
    cand.person, cand.method, cand.gap = name.data["person"], method, _gap(name, email)
    cand.in_card = method == "card"
    cand.section = doc.atoms[name.atom].section or cand.section
    title, cand.title_adjacent = _pick_title(scope, name, strict)
    spans = [(name.start, name.end), (email.start, email.end)]
    if title:
        cand.title = title.text
        cand.title_gap = max(0, title.start - name.end) if title.start >= name.start else max(0, name.start - title.end)
        if cand.title_gap <= MAX_GAP:  # a far title is refused later; it must not stretch the evidence fragment
            spans.append((title.start, title.end))
        if GENERIC_HEAD_TITLE_RE.match(norm(title.text)):
            depts = [m for m in scope if m.kind == "dept" and abs(m.start - name.start) <= MAX_GAP]
            if depts:
                cand.dept = min(depts, key=lambda m: abs(m.start - title.start)).text
            elif DEPT_RE.match(cand.section):
                cand.dept = cand.section
    lo, hi = min(s for s, _ in spans), max(e for _, e in spans)
    phones = [m for m in doc.mentions if m.kind == "phone" and card[0] <= m.atom <= card[1]]
    inside = [m for m in phones if lo <= m.start <= hi]
    around = [m for m in phones if lo - 60 <= m.start <= hi + 160] if method == "card" else []
    near = doc.text[max(0, lo - 60):hi + 160]
    digits = [re.sub(r"\D", "", m.text) for m in inside or around] + EXTENSION_RE.findall(near) \
        + [re.sub(r"\D", "", raw) for raw in re.findall(r"\+?\d[\d\s()\-]{5,}\d", near)]
    cand.card_digits = tuple(dict.fromkeys(d for d in digits if d))
    fragment = doc.text[lo:hi]
    if len(fragment) > FRAGMENT_LIMIT:
        fragment = fragment[:FRAGMENT_LIMIT // 2] + " […] " + fragment[-FRAGMENT_LIMIT // 2:]
    if not email.data.get("visible", True):
        fragment += f" [ссылка mailto: {email.text}]"  # the address is the link target, the page shows a button
    cand.fragment = mask_mobiles(fragment)


# --------------------------------------------------------------------------- #
# Site context and validation
# --------------------------------------------------------------------------- #

@dataclass
class SitePage:
    url: str
    kind: str
    doc: Doc
    fetched_on: str = ""  # ISO date the page was really fetched (from the cache entry when it was cached)
    robots: str = ""  # what robots.txt of the host said when the page was requested


@dataclass
class SiteCtx:
    """What the whole site says: which mail domains it uses, how fresh it is, what it forbids."""

    domain: str
    domains: set[str] = field(default_factory=set)
    domain_notes: dict[str, str] = field(default_factory=dict)  # a second mail domain -> why it is the site's own
    chrome_emails: set[str] = field(default_factory=set)
    shared_emails: set[str] = field(default_factory=set)  # one address printed next to several people
    numbered_boxes: set[str] = field(default_factory=set)  # «201@», «202@»: a site that numbers its mailboxes
    pd_ban_url: str = ""
    refusal_url: str = ""
    page_updated: dict[str, date] = field(default_factory=dict)  # page URL -> its "updated on" date
    fetched_on: dict[str, str] = field(default_factory=dict)  # page URL -> the day it was fetched
    robots: dict[str, str] = field(default_factory=dict)  # page URL -> what robots.txt said
    last_date: date | None = None  # the newest date printed anywhere on the fetched pages
    feed_last: date | None = None  # the newest entry of a dated feed on the homepage (news, updates)
    copyright_year: int = 0
    freshness_months: int = 24
    staff_count: int = 0
    brand_tokens: list[str] = field(default_factory=list)
    moved_to: str = ""  # the company's site redirects to this domain and it was accepted as the same company

    def stale_reason(self, url: str) -> str:
        """Why a contact from this page is too old to trust, or ''.

        The page's own "updated on" date counts. So does a site that has visibly stopped: a dated
        feed on the homepage (news, updates) whose newest entry is older than the threshold, while
        nothing else - the footer year, any other date on the pages read - is recent either. Old
        dates inside the text (reports, licences, the year of foundation) do not make a page stale,
        and neither does an old footer year alone: «© 2009 Компания» is often the year of foundation.
        """
        today = P.today()
        updated = self.page_updated.get(url)
        if updated and _months_between(updated, today) > self.freshness_months:
            return f"страница обновлена {updated:%d.%m.%Y}"
        if not updated and self.feed_last and _months_between(self.feed_last, today) > self.freshness_months \
                and self.copyright_year < today.year - 1 \
                and (self.last_date is None or _months_between(self.last_date, today) > self.freshness_months):
            return (f"последняя запись в ленте на главной - {self.feed_last:%d.%m.%Y}, более свежих дат на сайте "
                    "нет: сайт не обновляется")
        return ""

    def old_footer(self, url: str) -> bool:
        """The footer year stopped years ago and the page has no date of its own: a weak signal."""
        return url not in self.page_updated \
            and 0 < self.copyright_year < P.today().year - max(1, self.freshness_months // 12)

    def freshness_note(self, url: str) -> str:
        updated = self.page_updated.get(url)
        if updated:
            return f"страница обновлена {updated:%d.%m.%Y}"
        if self.copyright_year:
            return f"дата страницы не указана, в подвале © {self.copyright_year}"
        if self.last_date:
            return f"дата страницы не указана, последняя дата на сайте {self.last_date:%d.%m.%Y}"
        return "дата на странице не указана"


def _months_between(older: date, newer: date) -> int:
    return (newer.year - older.year) * 12 + newer.month - older.month - (newer.day < older.day)


def _is_free_mail(host: str) -> bool:
    return host in P.FREE_MAIL_DOMAINS or P.registrable_domain(host) in P.FREE_MAIL_DOMAINS


def _feed_dates(doc: Doc, today: date) -> list[date]:
    """Dates that stand alone in their own element («29.05.2023» above a news title): a dated feed."""
    found = []
    for atom in doc.atoms:
        if atom.chrome or not 6 <= len(atom.text) <= 24:
            continue
        spans = P._date_spans(atom.text)
        if len(spans) == 1 and spans[0][0] <= 1 and spans[0][1] >= len(atom.text) - 3 and spans[0][2] <= today:
            found.append(spans[0][2])
    return found


def build_site_ctx(company: Company, pages: list[SitePage], icp: ICP) -> SiteCtx:
    ctx = SiteCtx(domain=company.domain, freshness_months=icp.freshness_months)
    home_site = site_of(company.domain)
    own = {home_site} | {site_of(host_of(p.url)) for p in pages}  # the crawl never leaves the company's site(s)
    by_domain: dict[str, set[str]] = {}
    today = P.today()
    dates: list[date] = []
    for page in pages:
        doc = page.doc
        ctx.chrome_emails |= doc.chrome_emails
        ctx.fetched_on[page.url], ctx.robots[page.url] = page.fetched_on, page.robots
        for m in doc.of_kind("email"):
            local, _, host = m.text.rpartition("@")
            if not _is_free_mail(host):
                by_domain.setdefault(site_of(host), set()).add(m.text)
            if NUMERIC_LOCAL_RE.fullmatch(local) and not doc.atoms[m.atom].chrome:
                ctx.numbered_boxes.add(m.text)
        if not ctx.pd_ban_url and PD_BAN_RE.search(doc.text):
            ctx.pd_ban_url = page.url
        if not ctx.refusal_url and REFUSAL_RE.search(doc.text):
            ctx.refusal_url = page.url
        dates += [d for d in P.find_dates(doc.text) if d <= today]
        if page.kind == "home":
            feed = _feed_dates(doc, today)
            if len(feed) >= 3:
                ctx.feed_last = max(feed)
        for m in UPDATED_RE.finditer(doc.text):
            spans = P._date_spans(doc.text[m.end():m.end() + 40])
            if spans and spans[0][0] <= 3 and spans[0][2] <= today:
                ctx.page_updated[page.url] = max(spans[0][2], ctx.page_updated.get(page.url, spans[0][2]))
        for m in COPYRIGHT_YEAR_RE.finditer(doc.text):
            year = int(m.group(2) or m.group(1))
            if year <= today.year:
                ctx.copyright_year = max(ctx.copyright_year, year)
        for m in STAFF_COUNT_RE.finditer(doc.text):
            ctx.staff_count = max(ctx.staff_count, int(re.sub(r"\D", "", m.group(1))))
    # A mail domain other than the site's own is the company's when the site itself shows that:
    # it is spelled like the site's domain, or a box on it stands in the header / footer, or the
    # site prints no address on its own domain and nearly all of its addresses are on that one.
    # Two people of another firm on a page do not make their domain "used by the site".
    own_cores = [P.squash(P.domain_core(d)) for d in own]
    total = sum(len(addresses) for addresses in by_domain.values())
    prints_own = any(reg in own for reg in by_domain)
    for reg, addresses in by_domain.items():
        if reg in own:
            continue
        core = P.squash(P.domain_core(reg))
        similar = any(len(core) >= 4 and len(c) >= 4 and (core in c or c in core
                                                         or SequenceMatcher(None, core, c).ratio() >= 0.8)
                      for c in own_cores)
        if similar:
            ctx.domain_notes[reg] = "пишется как домен сайта"
        elif addresses & ctx.chrome_emails:
            ctx.domain_notes[reg] = "ящик на нём стоит в шапке или подвале сайта"
        elif not prints_own and len(addresses) >= 3 and len(addresses) >= 0.75 * total:
            ctx.domain_notes[reg] = "на нём все адреса сайта"
    ctx.domains = {d for d in own | set(ctx.domain_notes) if d}
    ctx.last_date = max(dates) if dates else None
    ctx.brand_tokens = P.name_tokens(company.name)
    return ctx


def is_generic_local(local: str, host: str = "") -> bool:
    """A box named after a department, a function or a place: «sales», «info.spb», «1_sales»,
    «otdel.prodazh», «b2b-sales», or after the company itself («zavod-primer@zavod-primer.ru»)."""
    local = local.lower()
    if GENERIC_LOCAL_RE.match(local):
        return True
    core = P.domain_core(host).replace("-", "")
    if core and re.sub(r"[\d._-]", "", local) == core:
        return True
    if ROLE_LOCAL_RE.match(re.sub(r"[._-]+", "", local)):
        return False  # «kom.dir», «gen-dir»: a post written in two parts, not the department «kom»
    return any(GENERIC_TOKEN_RE.match(token) for token in re.split(r"[._+-]+", local) if token)


def is_role_local(local: str) -> bool:
    """A box named after a decision maker's post: «gd», «kd», «director», «director.g», «askceo»."""
    local = local.lower()
    if ROLE_LOCAL_RE.match(local) or ROLE_LOCAL_RE.match(re.sub(r"[._-]+", "", local)):
        return True
    tokens = [token for token in re.split(r"[._+-]+", local) if token]
    roles = [token for token in tokens if ROLE_TOKEN_RE.match(token)]
    return bool(roles) and all(len(token) == 1 or token.isdigit() for token in tokens if token not in roles)


def classify_address(email: str, person: Person | None, site: SiteCtx, *, beside_person: bool = False,
                     card_digits: tuple[str, ...] = ()) -> tuple[str, str]:
    """(тип адреса, пояснение): именной / ящик должности ЛПР / общий ящик / бесплатный домен /
    не определён. «Не определён» is not a lead: the address agrees neither with the person's name
    nor with a decision maker's post, so nothing on the page says it is this person's box.

    `beside_person`: this occurrence of the address is printed next to the person (not in the
    header or footer). `card_digits`: phones and extensions printed next to the person.
    """
    local, _, host = email.partition("@")
    if _is_free_mail(host):
        return TYPE_FREE, ""
    if email in site.shared_emails:
        return TYPE_GENERIC, "один адрес указан у нескольких людей"
    how = local_matches_person(local, person) if person else ""
    in_chrome = email in site.chrome_emails
    if in_chrome and not (how and beside_person):
        return TYPE_GENERIC, "стоит в шапке или подвале сайта"
    if is_generic_local(local, host) and how not in ("фамилия", "имя"):
        return TYPE_GENERIC, ""  # «ok@», «pr@» stay department boxes even when they fit somebody's initials
    if how:
        return TYPE_PERSONAL, f"совпадает с ФИО ({how})" + (", он же стоит в подвале сайта" if in_chrome else "")
    if is_role_local(local):
        return TYPE_ROLE, "ящик должности"
    if NUMERIC_LOCAL_RE.fullmatch(local):
        number = re.sub(r"\D", "", local)
        if number in card_digits or (len(number) >= 7 and any(d.endswith(number) for d in card_digits)):
            return TYPE_ROLE, "номерной ящик: номер совпадает с телефоном или добавочным рядом с именем"
        if len(site.numbered_boxes) >= 3:
            return TYPE_ROLE, "номерной ящик: на сайте такие адреса у всех сотрудников"
    return TYPE_UNKNOWN, ""


class MXResolver:
    """MX lookup through `dig` (DNS only - a mailbox is never probed). None = could not check."""

    def __init__(self, server: str = "8.8.8.8", enabled: bool = True, cache_path: Path | None = None) -> None:
        self.server, self.enabled, self.cache_path = server, enabled, cache_path
        self._cache: dict[str, list[str] | None] = {}
        self._lock = threading.Lock()
        self._dig = shutil.which("dig") if enabled else None
        if cache_path and cache_path.exists():
            try:
                self._cache = json.loads(cache_path.read_text("utf-8"))
            except (OSError, ValueError):
                self._cache = {}

    def lookup(self, domain: str) -> list[str] | None:
        domain = domain.lower().strip(".")
        with self._lock:
            if domain in self._cache:
                return self._cache[domain]
        result = self._query(domain)
        if result is not None:
            with self._lock:
                self._cache[domain] = result
                if self.cache_path:
                    try:
                        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                        self.cache_path.write_text(json.dumps(self._cache, ensure_ascii=False), "utf-8")
                    except OSError as exc:
                        log.debug("MX cache write failed: %s", exc)
        return result

    def _query(self, domain: str) -> list[str] | None:
        """MX hosts, [] when the DNS server ANSWERED that there are none, None when it did not
        answer (a failed lookup is never remembered as «no MX»)."""
        if not self._dig:
            return None
        answered_empty = False
        for _ in range(2):  # one retry: an empty answer may be a dropped UDP packet
            try:
                proc = subprocess.run([self._dig, "+short", "+time=4", "+tries=1", "MX", domain, f"@{self.server}"],
                                      capture_output=True, text=True, timeout=12)
            except (OSError, subprocess.TimeoutExpired):
                return None
            if proc.returncode != 0 or "timed out" in proc.stdout or "no servers" in proc.stdout:
                continue  # the resolver did not answer: this is not an answer about the domain
            hosts = [m.group(1).rstrip(".").lower() for m in re.finditer(r"(?m)^\d+\s+(\S+)\s*$", proc.stdout)]
            hosts = [h for h in hosts if h]  # "0 ." is the null MX: the domain accepts no mail
            if hosts or proc.stdout.strip():
                return hosts
            answered_empty = True
        return [] if answered_empty else None


@dataclass
class Lead:
    first: str
    surname: str
    patronymic: str
    title: str
    email: str
    phone: str
    company: str
    site: str
    city: str
    segment: str
    address_type: str
    source_url: str
    fragment: str
    checked_on: str
    confidence: int
    checks: list[str]
    role: str

    def row(self) -> dict[str, str]:
        return {"Имя": self.first, "Фамилия": self.surname, "Должность": self.title, "Email": self.email,
                "Телефон": self.phone, "Компания": self.company, "site": self.site, "город": self.city,
                "сегмент": self.segment, "тип_адреса": self.address_type, "источник": self.source_url,
                "фрагмент": self.fragment, "дата_проверки": self.checked_on, "уверенность": str(self.confidence),
                "проверки": "; ".join(self.checks), "отчество": self.patronymic, "роль": self.role}


@dataclass
class Verdict:
    lead: Lead | None = None
    reason: str = ""  # one of the R_* constants when there is no lead
    detail: str = ""
    address_type: str = ""
    role: str = ""
    stage: int = 0  # how far the candidate got (for the "why no lead" note)


@dataclass
class Settings:
    icp: ICP
    max_gap: int = MAX_GAP
    min_confidence: int = MIN_CONFIDENCE
    # An address that is only the target of a mailto: link (the page shows a button, not the address):
    # none - never a lead, it is not printed (default); matched - inside the person's own card and only
    # when it agrees with the person's name; card - anything inside the person's own card; all - anywhere.
    mailto: str = "none"
    mx: MXResolver | None = None
    suppressed: frozenset[str] = frozenset()


def _hash(value: str) -> str:
    return hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()


def person_hash(surname: str, first: str, domain: str) -> str:
    """Opt-out key of a person at a company: an opt-out given for one address covers the
    person's other addresses on the same site."""
    return _hash(f"person:{norm(surname)}|{norm(first)[:1]}@{P.registrable_domain(P.normalize_domain(domain))}")


def is_suppressed(email: str, suppressed: frozenset[str]) -> bool:
    host = email.rsplit("@", 1)[-1]
    return bool(suppressed) and bool(
        {_hash(email), _hash(host), _hash(P.registrable_domain(host)), _hash(site_of(host))} & suppressed)


def _names_own_brand(text: str, site: SiteCtx) -> bool:
    """Does a title name the company itself («Директор по маркетингу Завода Пример»)?"""
    tokens = [*site.brand_tokens, P.domain_core(site.domain)]
    return any(t and P._token_in_text(t, text, allow_concat=True) for t in tokens)


def _foreign_person(cand: Candidate, site: SiteCtx) -> str:
    """A reason when the person is a client, partner or blog author rather than staff."""
    if FOREIGN_SECTION_RE.search(cand.section or ""):
        return f"раздел «{cand.section[:60]}»"
    path = urlsplit(cand.url).path
    if FOREIGN_PATH_RE.search(path) and not _names_own_brand(cand.title, site):
        return f"страница {path}"  # a partner or an author, unless the title itself names this very company
    return ""


def judge(cand: Candidate, company: Company, site: SiteCtx, cfg: Settings) -> Verdict:
    """All checks for one candidate, in order; the first failed check is the reject reason."""
    email = cand.email
    local, _, host = email.partition("@")
    if not EMAIL_RE.fullmatch(email) or ".." in email or len(local) > 64:
        return Verdict(reason=R_SYNTAX)
    if is_suppressed(email, cfg.suppressed):
        return Verdict(reason=R_OPTOUT)
    if MOBILE_RE.search(local):
        return Verdict(reason=R_MOBILE_BOX, stage=1)  # the address itself is somebody's mobile number
    kind, kind_note = classify_address(email, cand.person, site, card_digits=cand.card_digits,
                                       beside_person=cand.person is not None and not cand.chrome)
    if kind == TYPE_FREE:
        return Verdict(reason=R_FREE, address_type=kind, stage=1)
    reg = site_of(host)
    if reg not in site.domains:
        return Verdict(reason=R_DOMAIN, detail=reg, address_type=kind, stage=1)
    if kind == TYPE_GENERIC:
        return Verdict(reason=R_GENERIC, detail=kind_note, address_type=kind, stage=2)
    if cand.person is None:
        return Verdict(reason=cand.reason or R_NO_NAME, address_type=kind, stage=2)
    if cfg.suppressed and person_hash(cand.person.surname, cand.person.first, company.domain) in cfg.suppressed:
        return Verdict(reason=R_OPTOUT)
    if kind == TYPE_UNKNOWN:
        # the address says neither the person's name nor a decision maker's post: a department box
        # or somebody else's address standing next to this name
        return Verdict(reason=R_UNMATCHED, detail=cand.person.display, address_type=kind, stage=3)
    if not cand.title:
        return Verdict(reason=R_NO_TITLE, address_type=kind, stage=3)
    if cand.title_gap > cfg.max_gap:
        return Verdict(reason=R_NO_TITLE, detail=f"должность в {cand.title_gap} зн. от имени при пороге {cfg.max_gap}",
                       address_type=kind, stage=3)
    role, deputy = classify_role(cand.title, cand.dept)
    title = f"{cand.title} ({cand.dept})" if cand.dept else cand.title
    if role == "staff":
        return Verdict(reason=R_NOT_DM, detail=title, address_type=kind, role=role, stage=3)
    if role not in cfg.icp.roles:
        return Verdict(reason=R_ROLE, detail=title, address_type=kind, role=role, stage=4)
    foreign = _foreign_person(cand, site)
    org = ORG_AFTER_TITLE_RE.search(cand.title) if site.brand_tokens else None
    other_org = org.group(1).strip() if org and not any(
        P._token_in_text(t, org.group(1), allow_concat=True) for t in site.brand_tokens) else ""
    own_domain = reg not in site.domain_notes
    if not foreign and other_org and not own_domain:
        foreign = f"в должности названа организация «{other_org}», адрес на другом домене ({reg})"
    if foreign:
        return Verdict(reason=R_FOREIGN, detail=foreign, address_type=kind, role=role, stage=4)
    if cand.gap > cfg.max_gap:
        return Verdict(reason=R_GAP, detail=f"{title}: {cand.gap} зн. при пороге {cfg.max_gap}",
                       address_type=kind, role=role, stage=5)
    if not cand.visible:
        # The page shows a button, the address is only its target: it is not printed, so by default it
        # is not a lead. The softer modes are opt-in: inside the person's own card and in agreement with
        # the person's name (matched), anything inside the person's own card (card), anywhere (all).
        accepted = cfg.mailto == "all" or (cand.in_card and (
            cfg.mailto == "card" or (cfg.mailto == "matched" and kind == TYPE_PERSONAL)))
        if not accepted:
            return Verdict(reason=R_MAILTO, detail=title, address_type=kind, role=role, stage=5)
    if site.pd_ban_url:
        return Verdict(reason=R_PD_BAN, detail=site.pd_ban_url, address_type=kind, role=role, stage=6)
    if site.refusal_url:
        return Verdict(reason=R_REFUSAL, detail=site.refusal_url, address_type=kind, role=role, stage=6)
    stale = site.stale_reason(cand.url)
    if stale:
        return Verdict(reason=R_STALE, detail=stale, address_type=kind, role=role, stage=6)

    checks = ["синтаксис", f"домен {reg} = сайт" if own_domain
              else f"домен {reg} используется сайтом ({site.domain_notes[reg]})"]
    if site.moved_to:
        checks.append(f"сайт компании перенаправляет на {site.moved_to}: обход шёл там")
    confidence = {"card": 70, "sequence+local": 62, "sequence": 58, "zip": 55, "llm": 62}.get(cand.method, 50)
    checks.append({"card": "имя, должность и адрес в одной карточке", "zip": "имена и адреса идут парами",
                   "llm": "привязку выбрала LLM из извлечённых вариантов, цитата найдена на странице"}
                  .get(cand.method, "имя, должность и адрес подряд в одном блоке") + f" ({cand.gap} зн.)")
    if cand.llm == "confirmed":
        confidence += 5
        checks.append("привязку подтвердила LLM")
    mx = cfg.mx.lookup(host) if cfg.mx else None
    if mx is None:
        checks.append("MX не проверялся")
    elif not mx:
        return Verdict(reason=R_NO_MX, detail=host, address_type=kind, role=role, stage=7)
    else:
        checks.append(f"MX: {mx[0]}")
        confidence += 5
    if kind == TYPE_PERSONAL:
        confidence += 20
        checks.append(f"адрес {kind_note}")
    else:
        confidence += 5
        checks.append(kind_note)
    checks.append(f"ЛПР: {ROLE_LABELS[role]}" + (" (заместитель)" if deputy else ""))
    confidence += 5 if cand.title_adjacent else 0
    confidence -= 5 if deputy else 0
    if cand.person.kind == "initials":
        confidence -= 15
        checks.append("на странице только инициалы")
    elif cand.person.kind == "no_surname":
        confidence -= 15
        checks.append("фамилия на странице не указана")
    if not cand.visible:
        confidence -= 10
        checks.append("адрес стоит в ссылке mailto (текстом не напечатан)")
    if cand.gap > 200:
        confidence -= 5
    if other_org:
        confidence -= 15
        checks.append(f"в должности названа организация «{other_org}» - проверить, что это сотрудник")
    checks.append(f"свежесть: {site.freshness_note(cand.url)}")
    if site.old_footer(cand.url):
        confidence -= 10
        checks.append("год в подвале давно не менялся: контакт мог устареть")
    checks.append(site.robots.get(cand.url) or "robots.txt не проверялся: страница передана без обхода")
    confidence = max(0, min(100, confidence))
    lead = Lead(
        first=cand.person.first, surname=cand.person.surname, patronymic=cand.person.patronymic, title=title,
        # the phone is filled later with the company's general number; a person's own number is never stored
        email=email, phone="", company=company.name, site=company.site, city=company.city,
        segment=company.segment, address_type=kind, source_url=cand.url, fragment=mask_mobiles(cand.fragment),
        # the evidence is as old as the page: the day it was fetched, not the day of this run
        checked_on=site.fetched_on.get(cand.url) or P.today().isoformat(),
        confidence=confidence, checks=checks, role=role)
    if confidence < cfg.min_confidence:
        return Verdict(lead=lead, reason=R_LOW, detail=f"{title}: {confidence}", address_type=kind, role=role,
                       stage=8)
    return Verdict(lead=lead, address_type=kind, role=role, stage=9)


# --------------------------------------------------------------------------- #
# Optional LLM adjudication: choose among extracted candidates, quote the page
# --------------------------------------------------------------------------- #

LLM_SYSTEM = """Ты проверяешь, кому на странице сайта компании принадлежит адрес электронной почты.
Тебе дан фрагмент видимого текста страницы, адрес и пронумерованные списки имён и должностей, уже
извлечённых из этого же фрагмента. Выбери имя и должность человека, которому адрес принадлежит по тексту.

Правила:
- выбирать можно только номера из списков; ничего нового придумывать нельзя;
- поле fragment - дословная цитата из текста (без изменений), в которой есть и выбранное имя, и адрес;
- если по тексту нельзя уверенно сказать, чей это адрес (общий ящик отдела, несколько людей на один
  адрес), верни name: null.

Ответ - только JSON: {"name": <номер или null>, "title": <номер или null>, "fragment": "<цитата>"}"""


def llm_prompt(cand: Candidate) -> str:
    names = "\n".join(f"{i}. {p.raw}" for i, (p, _, _) in enumerate(cand.name_options, 1))
    titles = "\n".join(f"{i}. {t}" for i, (t, _, _) in enumerate(cand.title_options, 1)) or "(нет)"
    return (f"Адрес: {cand.email}\n\nИмена:\n{names}\n\nДолжности:\n{titles}\n\n"
            f"Текст страницы:\n\"\"\"\n{cand.block}\n\"\"\"")


def apply_llm_answer(cand: Candidate, raw: str, max_gap: int = MAX_GAP) -> str:
    """Validate an LLM answer and attach the chosen person. Returns '' or the rejection reason.

    The model cannot add anything: the name and the title are indexes into what was
    extracted, the quote must be on the page verbatim and contain the name and the address.
    """
    try:
        data = P.parse_llm_json(raw)
    except ValueError as exc:
        return f"ответ не JSON ({exc})"
    idx, tidx = data.get("name"), data.get("title")
    if idx is None:
        return LLM_NO_NAME
    if not isinstance(idx, int) or isinstance(idx, bool) or not 1 <= idx <= len(cand.name_options):
        return "номер имени вне списка"
    if tidx is not None and (not isinstance(tidx, int) or isinstance(tidx, bool)
                             or not 1 <= tidx <= len(cand.title_options)):
        return "номер должности вне списка"
    fragment = P.collapse_ws(str(data.get("fragment") or ""))
    person, start, end = cand.name_options[idx - 1]
    block, quote = P._norm_for_match(cand.block), P._norm_for_match(fragment)
    if not quote or quote not in block:
        return "цитаты нет на странице"
    if P._norm_for_match(person.raw) not in quote:
        return "в цитате нет выбранного имени"
    if cand.visible and cand.email.lower() not in quote and "@" in fragment:
        return "в цитате другой адрес"
    gap = max(0, cand.email_span[0] - end) if cand.email_span[0] >= start else max(0, start - cand.email_span[1])
    if gap > max_gap:
        return f"имя и адрес дальше {max_gap} знаков"
    if cand.person is not None and cand.person.key == person.key:
        cand.llm = "confirmed"  # the model agrees with the pairing found by the rules: the evidence stays as it is
        cand.title = cand.title_options[tidx - 1][0] if tidx and not cand.title else cand.title
        return ""
    cand.person, cand.gap, cand.method, cand.reason = person, gap, "llm", ""
    cand.llm, cand.in_card = "chosen", False
    cand.title = cand.title_options[tidx - 1][0] if tidx else cand.title
    cand.title_adjacent = False
    cand.fragment = mask_mobiles(fragment[:FRAGMENT_LIMIT])
    return ""


def adjudicate(cand: Candidate, backend, max_gap: int = MAX_GAP) -> str:
    """Ask the LLM about one ambiguous block. Returns '' when a person was attached."""
    if not cand.name_options:
        return "нет вариантов имени"
    try:
        raw = backend.complete(LLM_SYSTEM, llm_prompt(cand))
    except P.LLMError as exc:
        return f"LLM недоступна: {exc}"
    return apply_llm_answer(cand, raw, max_gap)


# --------------------------------------------------------------------------- #
# Discovery adapters: each one returns "company + site + why it matches"
# --------------------------------------------------------------------------- #

SEED_ALIASES = {
    "name": ("company", "компания", "название", "company_name", "name"),
    "site": ("site", "сайт", "website", "url", "domain", "домен"),
    "city": ("city", "город"),
    "segment": ("segment", "сегмент"),
}


class DiscoveryError(RuntimeError):
    pass


class SeedsAdapter:
    """A CSV of sites: a `site` column and, optionally, company / city / segment.
    A plain list of sites, one per line and without a header, works too."""

    name = "seeds"
    explicit = True  # the list IS the request: the default company limit of the ICP does not cut it

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def discover(self, icp: ICP, fetcher: LeadFetcher | None = None) -> Iterable[Company]:
        try:
            raw = self.path.read_text("utf-8-sig")
        except OSError as exc:
            raise DiscoveryError(f"не удалось прочитать {self.path}: {exc}") from exc
        lines = raw.splitlines()
        if not lines:
            return
        delimiter = ";" if lines[0].count(";") > lines[0].count(",") else ","
        reader = csv.DictReader(lines, delimiter=delimiter)
        fields = reader.fieldnames or []
        cols = {key: next((f for f in fields if f.strip().lower() in aliases), None)
                for key, aliases in SEED_ALIASES.items()}
        if cols["site"]:
            rows = [{key: (row.get(col) or "").strip() if col else "" for key, col in cols.items()} for row in reader]
        elif len(fields) == 1 and "." in fields[0]:  # no header: every line is a site
            rows = [{"site": line.strip(), "name": "", "city": "", "segment": ""} for line in lines if line.strip()]
        else:
            raise DiscoveryError(f"{self.path}: нет колонки site (сайт, url, domain)")
        for row in rows:
            domain = P.normalize_domain(row["site"])
            if not domain:
                continue
            name = row["name"] or P.domain_core(domain) or domain
            segment = row["segment"] or icp.match_segment(name)[0]
            site = row["site"] if "://" in row["site"] else f"https://{domain}"
            yield Company(name=name, site=site, city=row["city"], segment=segment, source=self.name,
                          why=f"список {self.path.name}")


@dataclass
class CatalogEntry:
    name: str
    url: str  # the entry's own page in the catalog
    country: str = ""
    rubrics: tuple[str, ...] = ()
    stand: str = ""


class CatalogAdapter:
    """A generic exhibitor / member catalog: one list page with the entries and a page per
    entry that names the company's own site. A new catalog is a subclass with the list
    URL and two parsers; filtering by country, ICP keywords and exclusions lives here.

    Only "who the company is and where its site is" is taken from a catalog. Contacts
    printed in the catalog are ignored: people are looked for on the company's own site.
    """

    name = "catalog"
    country = ""
    explicit = False

    def list_url(self) -> str:
        raise NotImplementedError

    def parse_list(self, html: str, url: str) -> tuple[str, list[CatalogEntry]]:
        """(catalog title, entries)"""
        raise NotImplementedError

    def parse_detail(self, html: str) -> dict[str, str]:
        """{'site': ..., 'city': ..., 'description': ...} of one entry page."""
        raise NotImplementedError

    def discover(self, icp: ICP, fetcher: LeadFetcher | None = None) -> Iterable[Company]:
        if fetcher is None:
            raise DiscoveryError(f"{self.name}: нужен сетевой клиент")
        res = fetcher.get(self.list_url())
        if not res.ok:
            raise DiscoveryError(f"{self.name}: список {self.list_url()} недоступен ({res.error or res.status})")
        title, entries = self.parse_list(res.html, res.final_url or res.url)
        if not entries:
            raise DiscoveryError(f"{self.name}: в списке {self.list_url()} не найдено ни одной компании")
        if len(res.html) >= P.MAX_PAGE_BYTES * 0.95:
            log.warning("%s: список длиннее %d МБ, взята только его первая часть", self.name,
                        P.MAX_PAGE_BYTES // 1_000_000)
        log.info("%s: «%s», компаний в списке: %d", self.name, title, len(entries))
        for entry in entries:
            if self.country and norm(entry.country) != norm(self.country):
                continue
            segment, keyword = icp.match_segment(" ".join((entry.name, *entry.rubrics)))
            if icp.segments and not segment:
                continue
            if icp.excluded_by_name(entry.name):
                continue
            detail = fetcher.get(entry.url)
            if not detail.ok:
                log.info("%s: карточка %s недоступна (%s)", self.name, entry.url, detail.error or detail.status)
                continue
            info = self.parse_detail(detail.html)
            domain = P.normalize_domain(info.get("site", ""))
            if not domain:
                continue
            why = f"участник «{title}»" + (f", стенд {entry.stand}" if entry.stand else "")
            if keyword:
                why += f"; ключевое слово ICP «{keyword}»"
            if entry.rubrics:  # the rubric that matched the keyword, else the first one
                rubric = next((r for r in entry.rubrics if keyword and norm(keyword) in norm(r)), entry.rubrics[0])
                why += f"; рубрика «{rubric}»"
            yield Company(name=entry.name, site=f"https://{domain}", city=info.get("city", ""), segment=segment,
                          source=self.name, why=why)


class ExpocentrAdapter(CatalogAdapter):
    """Exhibitor lists of Expocentre exhibitions (icatalog.expocentr.ru).

    An exhibitor paid for a stand to meet buyers: a B2B company that is actively selling.
    robots.txt of the catalog closes only /stands/, the list and the exhibitor pages are open.
    """

    name = "expocentr"
    BASE = "https://icatalog.expocentr.ru/ru/exhibitions"

    def __init__(self, exhibition: str, country: str = "Россия") -> None:
        m = re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", exhibition or "", re.I)
        if not m:
            raise DiscoveryError(f"expocentr: «{exhibition}» не похоже на id выставки (uuid из адреса каталога)")
        self.exhibition = m.group(0).lower()
        self.country = country

    def list_url(self) -> str:
        return f"{self.BASE}/{self.exhibition}/list"

    def parse_list(self, html: str, url: str) -> tuple[str, list[CatalogEntry]]:
        soup = BeautifulSoup(html or "", "html.parser")
        title = P.collapse_ws(soup.h1.get_text(" ")) if soup.h1 else "выставка Экспоцентра"
        entries: list[CatalogEntry] = []
        for row in soup.find_all("tr"):
            cells = row.find_all("td")
            link = cells[0].find("a", href=re.compile(r"/exhibitors/\d+")) if cells else None
            if not link:
                continue
            stand = cells[3].get_text(" ") if len(cells) > 3 else ""
            entries.append(CatalogEntry(
                name=P.collapse_ws(link.get_text(" ")),
                url=urljoin(url, str(link["href"]).strip()),
                country=P.collapse_ws(cells[1].get_text(" ")) if len(cells) > 1 else "",
                rubrics=tuple(P.collapse_ws(a.get_text(" ")) for a in cells[-1].find_all("a", class_="category")),
                stand=P.collapse_ws(stand)))
        return title, entries

    def parse_detail(self, html: str) -> dict[str, str]:
        soup = BeautifulSoup(html or "", "html.parser")
        info: dict[str, str] = {}
        labels = {"сайт": "site", "город": "city", "описание": "description", "страна": "country"}
        for dt in soup.find_all("dt"):
            key = labels.get(norm(dt.get_text(" ")).rstrip(": "))
            dd = dt.find_next_sibling("dd")
            if not key or dd is None or key in info:
                continue
            value = P.collapse_ws(dd.get_text(" "))
            if key == "site":
                link = dd.find("a", href=True)
                value = str(link["href"]).strip() if link else value
                value = re.split(r"[\s,;]+", value)[0] if value else ""
            info[key] = value
        return info


class SerperAdapter:
    """Web search through the Serper API. Works only with SERPER_API_KEY: search-engine
    pages are never scraped. The query is built from ICP keywords and cities."""

    name = "search"
    explicit = False
    ENDPOINT = "https://google.serper.dev/search"
    MAX_QUERIES = 6

    def __init__(self, api_key: str, queries: list[str] | None = None, per_query: int = 10,
                 client: httpx.Client | None = None) -> None:
        if not api_key:
            raise DiscoveryError("поиск выключен: в окружении нет SERPER_API_KEY")
        self.api_key, self.queries, self.per_query, self._client = api_key, queries or [], per_query, client

    def build_queries(self, icp: ICP) -> list[tuple[str, str]]:
        """(query, segment name)"""
        if self.queries:
            return [(q, icp.match_segment(q)[0]) for q in self.queries][:self.MAX_QUERIES]
        out = []
        for seg in icp.segments:
            for kw in seg.keywords[:2]:
                for city in (icp.cities[:2] or [""]):
                    out.append((P.collapse_ws(f"{kw} {city} официальный сайт контакты руководство"), seg.name))
        return out[:self.MAX_QUERIES]

    def discover(self, icp: ICP, fetcher: LeadFetcher | None = None) -> Iterable[Company]:
        client = self._client or httpx.Client(timeout=20.0)
        try:
            for query, segment in self.build_queries(icp):
                try:
                    resp = client.post(self.ENDPOINT, headers={"X-API-KEY": self.api_key},
                                       json={"q": query, "gl": "ru", "hl": "ru", "num": self.per_query})
                    resp.raise_for_status()
                    organic = resp.json().get("organic") or []
                except (httpx.HTTPError, ValueError) as exc:
                    raise DiscoveryError(f"поиск: запрос «{query}» не выполнен ({exc})") from exc
                for item in organic:
                    link = str(item.get("link") or "")
                    domain = P.normalize_domain(link)
                    if not domain or is_aggregator(domain):
                        continue
                    # A result is a company's own site when the title names the owner of the domain, or
                    # when it is the root page of a site. A deep page whose title names somebody else is
                    # a directory, a marketplace or a contact database writing ABOUT the company.
                    title = P.collapse_ws(str(item.get("title") or ""))
                    parts = [part for part in re.split(r"\s[-–—|]\s", title) if part]
                    name = next((part for part in parts if P.name_matches_domain(part, domain)), "")
                    if not name and urlsplit(link).path.strip("/"):
                        continue
                    yield Company(name=name or (parts[0] if parts else "") or P.domain_core(domain),
                                  site=f"https://{domain}", segment=segment, source=self.name,
                                  why=f"поиск: «{query}»")
        finally:
            if self._client is None:
                client.close()


R_OVER_LIMIT = "сверх лимита компаний (--limit)"


def is_aggregator(host: str) -> bool:
    """A social network, a directory, a marketplace or a contact database: never a company's own site."""
    host = _ascii_host(host)
    return bool({host, site_of(host), P.registrable_domain(host)} & AGGREGATOR_DOMAINS)


def discover_all(adapters: list, icp: ICP, fetcher: LeadFetcher | None, limit: int,
                 suppressed: frozenset[str] = frozenset(), *,
                 explicit_limit: bool = False) -> tuple[list[Company], Counter]:
    """Run the adapters in order until `limit` companies pass the ICP filters.
    Returns (companies, how many were dropped and why).

    A list of sites given by the user (seeds) is taken whole: `limit` cuts it only when it was
    asked for explicitly (`explicit_limit`), and then the rest is counted, not dropped silently.
    """
    companies: list[Company] = []
    dropped: Counter = Counter()
    seen: set[str] = set()
    for adapter in adapters:
        capped = explicit_limit or not getattr(adapter, "explicit", False)
        whole_list = getattr(adapter, "explicit", False)  # cheap to read to the end: no network behind it
        if capped and len(companies) >= limit and not whole_list:
            break
        try:
            for company in adapter.discover(icp, fetcher):
                reg = P.registrable_domain(company.domain)
                if not reg:
                    dropped["нет сайта"] += 1
                elif is_aggregator(company.domain) or _is_free_mail(company.domain):
                    dropped["вместо сайта агрегатор или соцсеть"] += 1
                elif company.domain in seen:
                    dropped["дубль сайта"] += 1
                elif icp.excluded_by_name(company.name, company.domain):
                    dropped[icp.excluded_by_name(company.name, company.domain)] += 1
                elif not icp.city_ok(company.city):
                    dropped["город вне ICP"] += 1
                elif {_hash(company.domain), _hash(reg), _hash(site_of(company.domain))} & suppressed:
                    dropped[R_OPTOUT] += 1
                elif capped and len(companies) >= limit:
                    dropped[R_OVER_LIMIT] += 1
                    if not whole_list:
                        break
                else:
                    seen.add(company.domain)
                    companies.append(company)
                    if capped and len(companies) >= limit and not whole_list:
                        break
        except DiscoveryError as exc:
            log.warning("%s", exc)
            dropped[f"источник {adapter.name} недоступен"] += 1
    return companies, dropped


# --------------------------------------------------------------------------- #
# Crawl: the company's own site only, within a page budget
# --------------------------------------------------------------------------- #

PERSON_PATH_RE = re.compile(
    r"(?:^|/)(?:persons?|people|team|staff|sotrudniki|komanda|rukovodstvo|rukovoditeli|management|experts?|"
    r"head|managers?|employees)/[^/?#]+/?(?:[^/?#]+/?)?$", re.I)
CITY_RE = re.compile(r"(?<![\w-])г\.\s?([А-ЯЁ][а-яё]+(?:[- ][А-ЯЁ][а-яё]+)?)")
SITEMAP_FILES = 3
NOISY_SITEMAP_RE = re.compile(r"product|catalog|goods|shop|news|blog|post|tag|image|video|iblock", re.I)
SPLASH_TEXT = 400  # a homepage with less visible text than this is a splash screen
SPLASH_LINKS = 5  # sections of the site opened from a splash homepage
GUESS_MISSES = 2  # usual paths tried per page kind before giving up on that kind
MIRROR_PENALTY = 5  # pages on another host of the site (eng.site.ru, shop.site.ru) come after the main host


@dataclass
class Crawl:
    pages: list[SitePage] = field(default_factory=list)
    attempts: int = 0  # requests for content pages (the budget counts these)
    error: str = ""
    refused: bool = False  # the site answered and the answer is final: robots.txt closes it, or it leads elsewhere
    moved_to: str = ""  # the site redirects to this domain and it was accepted as the same company's
    net_errors: int = 0  # page requests lost to the connection (tunnel, timeout): the result is incomplete
    closed: list[str] = field(default_factory=list)  # people pages that robots.txt closes (not requested)


def core_of(host: str) -> str:
    """Brand label of a site: «primer-zavod.ru» -> «primer-zavod», «zavod.tilda.ws» -> «zavod»."""
    label = site_of(host).split(".", 1)[0]
    if label.startswith("xn--"):
        try:
            label = label.encode("ascii").decode("idna")
        except UnicodeError:
            pass
    return label


def same_company_domain(company: Company, host: str) -> bool:
    """The company's site redirects to another domain: is that domain the same company's site?

    Yes when it is spelled like the old one (primer.com -> primer.ru, primsmeta.ru ->
    primer-smeta.ru) or like the company's name. A social network, a directory or a
    marketplace is never the company's own site, whatever redirects there.
    """
    target = site_of(host)
    if not target or target == site_of(company.domain):
        return bool(target)
    if is_aggregator(host) or _is_free_mail(host):
        return False
    old, new = P.squash(core_of(company.domain)), P.squash(core_of(host))
    if len(old) >= 4 and len(new) >= 4 and (old in new or new in old
                                            or SequenceMatcher(None, old, new).ratio() >= 0.75):
        return True
    zone = core_of(host).replace("-", " ") + " " + core_of(host).replace("-", "")
    return any(P._token_in_text(token, zone, allow_concat=True) for token in P.name_tokens(company.name)
               if len(token) >= 4)


def fetch_home(fetcher: LeadFetcher, company: Company) -> tuple[P.FetchResult, list[str]]:
    """The homepage: the URL as given, then https / http with and without «www.».

    Redirects inside the site are followed by the fetcher. A redirect to another domain is
    followed only when that domain is the same company's (see same_company_domain); its
    robots.txt is read before the first page, like on any other host.
    """
    domain = company.domain
    raw = (company.site or "").strip()
    variants = ([raw] if "://" in raw else []) + [f"https://{domain}/", f"https://www.{domain}/",
                                                  f"http://{domain}/", f"http://www.{domain}/"]
    errors: list[str] = []
    result = P.FetchResult(url=variants[0], error="нет URL")
    tried: set[str] = set()

    def scope(host: str) -> bool:
        return same_company_domain(company, host)

    for url in variants:
        if url in tried:
            continue
        # full retries for the first variant only; the rest are quick probes
        result = fetcher.get(url, retries=None if not tried else 0, scope=scope)
        tried.add(url)
        if result.ok and P.PLACEHOLDER_RE.search(result.html[:5000]):
            errors.append(f"{url}: заглушка веб-сервера вместо сайта")
            result = P.FetchResult(url, result.final_url, result.status, error="заглушка веб-сервера вместо сайта")
            continue
        if result.ok:
            return result, errors
        errors.append(f"{url}: {result.error or result.status}")
        if result.status in (401, 403, 429, 451) or "robots.txt" in result.error or result.error.startswith(OFFSITE):
            break  # the site answered and refused us, or leads elsewhere: other variants are not hammered
    return result, errors


def classify_page(url: str, anchor: str = "") -> tuple[str, int] | None:
    """(page kind, priority) by path segments and link text, or None for an ordinary page."""
    path = unquote(urlsplit(url).path)
    if P.SKIP_EXT_RE.search(path):
        return None
    best: tuple[str, int] | None = None
    for kind, score, path_re, text_re in PAGE_KINDS:
        m = path_re.search(path)
        by_text = bool(anchor and text_re.match(anchor))
        if not m and not by_text:
            continue
        value = score + (1 if m and by_text else 0)
        if m and not by_text and path[m.end():].strip("/"):
            if kind in ("about", "press"):
                continue  # /company/news/..., /press/2024/...: not a page about people
            value -= 3  # the matching segment is not the last one: a page deeper in that section
        if best is None or value > best[1]:
            best = (kind, value)
    return best


def person_links(page: SitePage, icp: ICP, regs: set[str]) -> tuple[list[tuple[float, str]], set[str]]:
    """Links from a team listing to the pages of individual people.

    `regs` are the sites (see site_of) that belong to the company. Returns (links worth opening
    as (priority, URL), decision makers first; every person link seen, as normalized URLs).
    The title printed next to the link decides: pages of ordinary staff are not opened at all."""
    doc = page.doc
    base = urlsplit(page.url).path.rstrip("/")
    names, titles = doc.of_kind("name"), doc.of_kind("title")
    found: dict[str, tuple[float, str]] = {}
    seen: set[str] = set()
    for href, anchor, atom_idx in doc.links:
        path = urlsplit(href).path.rstrip("/")
        if site_of(host_of(href)) not in regs or P.SKIP_EXT_RE.search(path):
            continue
        deeper = path.startswith(base + "/") and path.count("/") - base.count("/") <= 2
        if not (deeper and base) and not PERSON_PATH_RE.search(path):
            continue
        if not find_names(anchor) and not any(abs(m.atom - atom_idx) <= 1 for m in names):
            continue
        key = P.normalize_url(href)
        seen.add(key)
        score = 4.0
        # the title inside the link itself (the whole card is a link) is the surest, else the nearest one
        inside = find_titles(anchor, [(a, b) for a, b, _ in find_names(anchor)])
        near = [m.text for m in sorted((m for m in titles if abs(m.atom - atom_idx) <= 3),
                                       key=lambda m: (abs(m.atom - atom_idx), m.atom < atom_idx))]
        title = inside[0][2] if inside else near[0] if near else ""
        if title:
            role, _ = classify_role(title)
            if role == "staff":
                continue
            # wanted roles first, in the ICP's own order: 8.0 for the first role, 7.9 for the second ...
            score = 8.0 - 0.1 * icp.roles.index(role) if role in icp.roles else 5.0
        if key not in found or found[key][0] < score:
            found[key] = (score, href)
    return sorted(found.values(), key=lambda item: -item[0]), seen


def sitemap_urls(fetcher: LeadFetcher, base_url: str, regs: set[str], scope=None) -> list[str]:
    """Page URLs from sitemap.xml: one level of a sitemap index, at most SITEMAP_FILES files."""
    urls: list[str] = []
    queue = [urljoin(base_url, "/sitemap.xml")]
    fetched = 0
    while queue and fetched < SITEMAP_FILES:
        target = queue.pop(0)
        if site_of(host_of(target)) not in regs:
            continue
        res = fetcher.get(target, retries=0, html_only=False, scope=scope)
        fetched += 1
        if res.status != 200 or not res.html:
            continue
        locs = [u for u in re.findall(r"<loc>\s*(?:<!\[CDATA\[)?\s*([^<\]\s]+)", res.html, re.I)
                if site_of(host_of(u)) in regs]
        if "<sitemapindex" in res.html[:3000].lower():
            if fetched == 1:
                queue += sorted(locs, key=lambda u: bool(NOISY_SITEMAP_RE.search(u)))[:SITEMAP_FILES - 1]
        else:
            urls += locs[:SITEMAP_LIMIT]
    return urls


def _text_key(doc: Doc) -> str:
    return hashlib.sha1(norm(doc.text).encode("utf-8")).hexdigest()


def _site_page(fetcher: LeadFetcher, url: str, kind: str, res: P.FetchResult) -> SitePage:
    return SitePage(url, kind, annotate(flatten(url, res.html)), fetched_on=getattr(res, "fetched_on", ""),
                    robots=fetcher.robots_note(url))


def crawl_site(fetcher: LeadFetcher, company: Company, icp: ICP) -> Crawl:
    """Homepage, then the pages most likely to name people: contacts, team, management,
    sales department, about. Links are ranked by URL segments and link text; sitemap.xml
    and a few usual paths help when the menu is built by JavaScript; a splash homepage is
    read one level deeper. Never leaves the company's own site."""
    budget = max(1, icp.pages_per_site)
    crawl = Crawl(attempts=1)
    home_res, errors = fetch_home(fetcher, company)
    target_host = host_of(home_res.final_url or "") if home_res.status else ""
    if target_host and not same_company_domain(company, target_host):
        # the fetcher does not follow such a redirect; a page cached by older code may still carry one
        crawl.error = f"сайт перенаправляет на {target_host}: это не сайт компании, обход не начат"
        crawl.refused = True
        return crawl
    if not home_res.ok:
        crawl.error = "; ".join(errors[-2:]) or home_res.error or "сайт недоступен"
        crawl.refused = ROBOTS_DENIED in crawl.error
        return crawl
    home_url = home_res.final_url or home_res.url
    home_host = host_of(home_url)
    regs = {site_of(company.domain), site_of(home_host)} - {""}
    if site_of(home_host) != site_of(company.domain):
        crawl.moved_to = site_of(home_host)
        listed = icp.excluded_domain(crawl.moved_to)
        if listed:  # the list names the site the company has moved to: the same company, nothing more is asked
            crawl.error = f"исключение ICP: домен {listed} (сайт компании перенаправляет на {crawl.moved_to})"
            crawl.refused = True
            return crawl

    def scope(host: str) -> bool:
        return site_of(host) in regs

    home = _site_page(fetcher, home_url, "home", home_res)
    crawl.pages.append(home)
    seen_urls = {P.normalize_url(home_res.url), P.normalize_url(home_url)}
    seen_text = {_text_key(home.doc)}
    queue: dict[str, tuple[float, str, str]] = {}  # normalized URL -> (priority, URL, kind)
    guessed: set[str] = set()
    misses: Counter = Counter()

    def offer(url: str, kind: str, score: float) -> str:
        url = P.request_url(url)  # no fragment, no «/../»: the address the fetcher will check and request
        key = P.normalize_url(url)
        if key in seen_urls or not scope(host_of(url)) or urlsplit(url).scheme not in ("http", "https"):
            return ""
        if host_of(url) != home_host:
            score -= MIRROR_PENALTY  # a mirror or a shop on a subdomain: after the pages of the main host
        if key not in queue or queue[key][0] < score:
            queue[key] = (score, url, kind)
        return key

    def harvest(page: SitePage) -> None:
        people, person_urls = person_links(page, icp, regs) if page.kind != "person" else ([], set())
        for href, anchor, _ in page.doc.links:
            bare = urlunsplit(urlsplit(href)._replace(query="", fragment=""))
            target = bare if classify_page(bare) else href  # /contacts/?from=menu is the page /contacts/
            hit = classify_page(target, anchor)
            if hit and P.normalize_url(target) not in person_urls:
                offer(target, hit[0], hit[1])
        for score, href in people[:PERSON_PAGES_PER_SITE]:
            offer(href, "person", score)

    harvest(home)
    if budget > 1 and not queue and len(home.doc.text) < SPLASH_TEXT:
        # a splash screen: a logo and links to the sections of the site, none of them a contacts or a team
        # page. The sections are read like homepages.
        sections = [href for href, _, _ in home.doc.links
                    if host_of(href) == home_host and urlsplit(href).path.strip("/")
                    and not P.SKIP_EXT_RE.search(urlsplit(href).path)]
        for href in list(dict.fromkeys(sections))[:SPLASH_LINKS]:
            offer(href, "section", 4.5)
    if budget > 1 and not {"contacts", "team"} <= {kind for _, _, kind in queue.values()}:
        for url in sitemap_urls(fetcher, home_url, regs, scope):
            hit = classify_page(url)
            if hit:
                offer(url, hit[0], hit[1] - 1)
    linked = {kind for _, _, kind in queue.values()}
    for kind, paths in GUESSED_PATHS.items():
        if kind not in linked:
            for i, path in enumerate(paths):  # the last resort: after every page the site itself links to
                guessed.add(offer(urljoin(home_url, path), kind, 0.3 - 0.1 * i))
    got: set[str] = set()
    persons = 0
    while queue and crawl.attempts < budget:
        key = max(queue, key=lambda k: queue[k][0])
        _, url, kind = queue.pop(key)
        if kind == "person" and persons >= PERSON_PAGES_PER_SITE:
            continue
        if key in guessed and (kind in got or misses[kind] >= GUESS_MISSES):
            continue  # the section was found by a link, or the site clearly has no such usual paths
        seen_urls.add(key)
        res = fetcher.get(url, retries=0 if key in guessed else None, scope=scope)
        if res.error.startswith(ROBOTS_DENIED):
            if kind in ("contacts", "team", "dept", "person") and key not in guessed:
                crawl.closed.append(url)
            if not res.status:
                continue  # closed by robots.txt: nothing was requested, the budget is intact
        crawl.attempts += 1
        if not res.ok:
            if key in guessed:
                misses[kind] += 1  # a guess that did not work says nothing about the site
            elif is_net_error(res.error) or res.error.startswith(ROBOTS_DOWN):
                crawl.net_errors += 1  # a page the site itself links to was lost to the connection
            continue
        final = res.final_url or url
        final_key = P.normalize_url(final)
        if (final_key != key and final_key in seen_urls) or not scope(host_of(final)):
            continue  # redirected to a page we already have, or away from the site
        seen_urls.add(final_key)
        page = _site_page(fetcher, final, kind, res)
        text_key = _text_key(page.doc)
        if text_key in seen_text or P.NOT_FOUND_RE.search(page.doc.title) or P.PLACEHOLDER_RE.search(res.html[:5000]):
            if key in guessed:
                misses[kind] += 1
            continue  # the same text under another URL, a "soft 404" or a server stub
        seen_text.add(text_key)
        crawl.pages.append(page)
        got.add(kind)
        persons += kind == "person"
        harvest(page)
    return crawl


# --------------------------------------------------------------------------- #
# One company: crawl -> candidates -> checks -> leads, or the reason there are none
# --------------------------------------------------------------------------- #

BOX_ORDER = ("sales", "sale", "zakaz", "order", "opt", "sbyt", "commerce", "kp", "info", "mail", "office")


@dataclass
class CompanyResult:
    company: Company
    status: str = "no_lead"  # lead | no_lead | error (the site did not answer: retried on the next run)
    leads: list[dict[str, str]] = field(default_factory=list)
    no_lead: dict[str, str] = field(default_factory=dict)
    rejects: dict[str, int] = field(default_factory=dict)
    pages: int = 0
    checked_on: str = ""
    moved_to: str = ""  # the company's site redirects to this domain (accepted as the same company)


@dataclass
class RunCtx:
    fetcher: LeadFetcher
    cfg: Settings
    backend: object | None = None  # an LLM backend with .complete(system, user), or None
    llm_calls: int = 0
    llm_accepted: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


def _no_lead_row(company: Company, contact: str, phone: str, reason: str, pages: int) -> dict[str, str]:
    return {"Компания": company.name, "site": company.site, "город": company.city, "сегмент": company.segment,
            "общий_контакт": contact, "телефон": phone, "причина": reason, "страниц_просмотрено": str(pages),
            "дата_проверки": P.today().isoformat(), "откуда": company.why or company.source}


def named_decision_maker(pages: list[SitePage], icp: ICP) -> str:
    """The title of a decision maker who is named on the site (with no address of their own)."""
    for page in pages:
        if FOREIGN_PATH_RE.search(urlsplit(page.url).path):
            continue
        doc = page.doc
        names = [m for m in doc.of_kind("name") if not doc.atoms[m.atom].chrome]
        for title in doc.of_kind("title"):
            atom = doc.atoms[title.atom]
            if atom.chrome or FOREIGN_SECTION_RE.search(atom.section):
                continue
            if classify_role(title.text)[0] in icp.roles and any(abs(n.atom - title.atom) <= 2 for n in names):
                return title.text
    return ""


def _company_box(local: str, company: Company) -> bool:
    """Is a box on a free-mail service named after a department or the company («zakaz», «zavod-primer»)?
    Anything else there may be somebody's personal mailbox: it is not reported even as a general contact."""
    if is_generic_local(local, company.domain):
        return True
    words = re.sub(r"[\d._+-]+", " ", local.lower()).strip()
    glued, core = P.squash(words), P.squash(core_of(company.domain))
    if len(core) >= 4 and len(glued) >= 4 and (core in glued or glued in core
                                               or SequenceMatcher(None, core, glued).ratio() >= 0.75):
        return True
    zone = f"{words} {words.replace(' ', '')}"
    return any(len(token) >= 4 and P._token_in_text(token, zone, allow_concat=True)
               for token in P.name_tokens(company.name))


def best_generic_contact(finals: list[tuple[Candidate, Verdict]], company: Company) -> str:
    """The best company-level box for the "no lead" report: sales-like boxes first.

    A box on a free-mail service is taken only when no box on the company's own domain is printed,
    and only when it is named after a department or the company itself.
    """
    pool = [c.email for c, v in finals if v.reason == R_GENERIC]
    pool = pool or [c.email for c, v in finals if v.reason == R_FREE and c.person is None
                    and _company_box(c.email.split("@", 1)[0], company)]

    def rank(address: str) -> int:
        local = address.split("@", 1)[0]
        return next((i for i, prefix in enumerate(BOX_ORDER) if local.startswith(prefix)), len(BOX_ORDER))

    return min(pool, key=rank) if pool else ""


PHONE_AFTER_NAME = MAX_GAP  # a number this close after a person's name is read as that person's
PHONE_BEFORE_NAME = 120
# A line that says itself whose number it is: «Отдел продаж: +7 …», «Единый номер: …», «Офис: …».
GENERAL_PHONE_RE = re.compile(
    r"^(?:единый|единая|многоканальн\w+|общий|офис|центральный офис|горячая линия|колл-центр|call-центр|"
    r"справочная|по россии|бесплатн\w+|для заказов|заказ звонка)(?![\w-])", re.I)
FAX_BEFORE_RE = re.compile(r"факс\W*$", re.I)


def _persons_phone(doc: Doc, phone: Mention, names: list[Mention], headings: list[int]) -> bool:
    """Is this number printed as somebody's own: inside a person's card or right next to a name?"""
    atom = doc.atoms[phone.atom]
    if atom.chrome:
        return False  # the header or the footer of the site: the company's number
    named_here = any(n.atom == phone.atom for n in names)
    if not named_here and (DEPT_RE.match(atom.text) or GENERAL_PHONE_RE.match(atom.text)):
        return False  # the line names a department or the company, not a person
    anchor = next((el for el in reversed(atom.path) if doc.elem_names.get(el)), None)
    if anchor is not None and len(doc.elem_names[anchor]) == 1:
        lo, hi = doc.spans[anchor]
        if doc.atoms[hi].start + len(doc.atoms[hi].text) - doc.atoms[lo].start <= CARD_MAX_CHARS:
            return True  # a compact block around one person is that person's card: every number in it is theirs
    for name in names:
        if 0 <= phone.start - name.end <= PHONE_AFTER_NAME:
            lo, hi = name.end, phone.start
        elif 0 <= name.start - phone.end <= PHONE_BEFORE_NAME:
            lo, hi = phone.end, name.start
        else:
            continue
        if not any(lo <= start < hi for start in headings):
            return True  # the name and the number stand together, no heading between them
    return False


def company_phone(pages: list[SitePage]) -> str:
    """The company's general number, for the «Телефон» column and the "no lead" report.

    Only a number the page presents as the company's or a department's: printed in the header or
    footer of the site, on a line that names a department, or in a block that names nobody. A number
    from a person's card or next to a name is that person's and is never stored. Neither is any
    mobile number (code 9xx), wherever it stands: it may be somebody's personal phone.
    """
    for page in sorted(pages, key=lambda p: p.kind != "contacts"):
        doc = page.doc
        names = doc.of_kind("name")
        headings = [atom.start for atom in doc.atoms if atom.heading]
        for phone in doc.of_kind("phone"):
            atom = doc.atoms[phone.atom]
            if is_mobile(phone.text) or (page.kind == "person" and not atom.chrome):
                continue
            if FAX_BEFORE_RE.search(atom.text[:max(0, phone.start - atom.start)]):
                continue
            if not _persons_phone(doc, phone, names, headings):
                return phone.text
    return ""


def no_lead_reason(finals: list[tuple[Candidate, Verdict]], pages: list[SitePage], icp: ICP) -> str:
    """Why a company gave no lead: the candidate that got furthest explains it."""
    if not finals:
        if sum(len(p.doc.text) for p in pages) < 400:
            return "на открытых страницах почти нет текста (заставка или сайт, который рисуется скриптами)"
        return "на просмотренных страницах нет адресов e-mail"
    _, verdict = max(finals, key=lambda cv: cv[1].stage)
    if verdict.stage <= 2:
        title = named_decision_maker(pages, icp)
        if title:
            return f"ЛПР на сайте назван ({title}), но его адрес рядом не напечатан; есть только общие ящики"
        if verdict.reason in (R_GENERIC, R_FREE, R_DOMAIN):
            return "на сайте только общие ящики, людей с адресами нет"
    return verdict.reason + (f": {verdict.detail}" if verdict.detail else "")


def _rank(verdict: Verdict) -> tuple[int, int]:
    return verdict.stage, verdict.lead.confidence if verdict.lead else 0


def process_company(company: Company, ctx: RunCtx) -> CompanyResult:
    icp, cfg = ctx.cfg.icp, ctx.cfg
    result = CompanyResult(company=company, checked_on=P.today().isoformat())
    crawl = crawl_site(ctx.fetcher, company, icp)
    pages = crawl.pages
    result.pages, result.moved_to = len(pages), crawl.moved_to
    if not pages:
        if crawl.refused:  # the site answered, and the answer is final: nothing to retry
            reason = f"robots.txt закрывает сайт для обхода ({crawl.error})" if ROBOTS_DENIED in crawl.error \
                else crawl.error
            result.no_lead = _no_lead_row(company, "", "", reason, 0)
        else:
            result.status = "error"
            result.no_lead = _no_lead_row(company, "", "", f"сайт недоступен: {crawl.error}", 0)
        return result
    home = pages[0].doc
    if not company.segment:
        company.segment = icp.match_segment(f"{home.title} {home.text[:3000]}")[0]
    if not company.city:
        for page in sorted(pages, key=lambda p: p.kind != "contacts"):
            m = CITY_RE.search(page.doc.text)
            if m:
                company.city = m.group(1)
                break
    site = build_site_ctx(company, pages, icp)
    site.moved_to = crawl.moved_to
    head = norm(f"{home.title} {home.text[:1500]}")
    excluded = next((f"исключение ICP: «{kw}» на главной странице" for kw in icp.exclude_keywords
                     if norm(kw) and norm(kw) in head), "")
    if not excluded and icp.max_staff and site.staff_count > icp.max_staff:
        excluded = f"исключение ICP: на сайте заявлено {site.staff_count} сотрудников при пороге {icp.max_staff}"
    if not excluded:
        # The list of exclusions names a company by its domain. The company is the same one when that
        # domain is where its site lives now or where its people get mail: every address that can become
        # a lead stands on one of site.domains.
        for domain in sorted(site.domains):
            listed = icp.excluded_domain(domain)
            if listed:
                excluded = f"исключение ICP: домен {listed} (сайт или почта компании: {domain})"
                break

    found = [cand for page in pages for cand in extract_candidates(page.doc)]
    owners: dict[str, set[str]] = {}
    for cand in found:  # only the surest pairings count: the address inside a person's own card
        if cand.person is not None and cand.in_card and cand.person.surname:
            owners.setdefault(cand.email, set()).add(P.squash(cand.person.surname))  # Образцова = Obraztsova
    site.shared_emails = {email for email, keys in owners.items() if len(keys) > 1}  # a team box, not a person's
    best: dict[str, tuple[Candidate, Verdict]] = {}  # one verdict per address: the one that got furthest
    for cand in found:
        verdict = judge(cand, company, site, cfg)
        if cand.email not in best or _rank(verdict) > _rank(best[cand.email][1]):
            best[cand.email] = (cand, verdict)

    if ctx.backend is not None and not excluded:
        for email, (cand, verdict) in list(best.items()):
            unclear = verdict.reason in (R_AMBIGUOUS, R_NO_NAME) and verdict.address_type in (TYPE_PERSONAL, TYPE_ROLE)
            weak = verdict.reason == R_LOW or (not verdict.reason and verdict.lead is not None
                                               and verdict.lead.confidence < LLM_BELOW)
            if not (unclear or weak) or not cand.name_options:
                continue
            problem = adjudicate(cand, ctx.backend, cfg.max_gap)
            with ctx.lock:
                ctx.llm_calls += 1
                ctx.llm_accepted += not problem
            if not problem:
                best[email] = (cand, judge(cand, company, site, cfg))
            elif weak and problem == LLM_NO_NAME:  # the model read the block and could not tell whose address it is
                best[email] = (cand, Verdict(reason=R_AMBIGUOUS, detail="LLM не подтвердила привязку",
                                             address_type=verdict.address_type, role=verdict.role,
                                             stage=verdict.stage))
            else:
                log.debug("%s: ответ LLM по %s отклонён (%s)", company.domain, email, problem)

    finals = list(best.values())
    rejects = Counter(v.reason for _, v in finals if v.reason)
    accepted = [(c, v) for c, v in finals if v.lead is not None and not v.reason]
    # One lead per person. Of a person's several addresses the one PRINTED on the page wins over
    # the one that is only a link target, then the surer one.
    per_person: dict[str, tuple[Candidate, Verdict]] = {}
    for cand, verdict in accepted:
        held = per_person.get(cand.person.key)
        if held is not None:
            rejects[R_DUPLICATE] += 1
        if held is None or (cand.visible, verdict.lead.confidence) > (held[0].visible, held[1].lead.confidence):
            per_person[cand.person.key] = (cand, verdict)
    order = {role: i for i, role in enumerate(icp.roles)}
    ranked = sorted(per_person.values(), key=lambda cv: (order.get(cv[1].role, len(order)), -cv[1].lead.confidence))
    general_phone = company_phone(pages)
    for _cand, verdict in ranked:
        if excluded:
            rejects["исключение ICP"] += 1
        elif len(result.leads) >= icp.leads_per_company:
            rejects[R_LIMIT] += 1
        else:
            lead = verdict.lead
            if general_phone:
                lead.phone = general_phone
                lead.checks.append("телефон: общий номер компании с её сайта, не личный")
            result.leads.append(lead.row())
    result.rejects = dict(rejects)
    if result.leads:
        result.status = "lead"
        return result
    reason = excluded or no_lead_reason(finals, pages, icp)
    ban = f"{R_PD_BAN}: {site.pd_ban_url}" if site.pd_ban_url else \
        f"{R_REFUSAL}: {site.refusal_url}" if site.refusal_url else ""
    if ban and not excluded and not reason.startswith((R_PD_BAN, R_REFUSAL)):
        reason = f"{ban}; кроме того: {reason}"  # what the site forbids is said first, whoever was found on it
    if crawl.closed and not excluded:
        shown = ", ".join(urlsplit(u).path or "/" for u in crawl.closed[:3])
        reason += f"; страницы с людьми закрыты в robots.txt и не открывались: {shown}"
    if crawl.net_errors and not excluded:
        # part of the site was lost to the connection: "no lead" would be a guess, the site is asked again
        result.status = "error"
        reason = f"сайт недоступен: {crawl.net_errors} стр. не открылись из-за сбоя соединения; пока: {reason}"
    result.no_lead = _no_lead_row(company, best_generic_contact(finals, company), general_phone, reason, len(pages))
    return result


# --------------------------------------------------------------------------- #
# Output: CSV files, run summary, resumable state
# --------------------------------------------------------------------------- #

def _write_text(path: Path, text: str) -> None:
    """Atomic write: a crash never leaves a half-written file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, "utf-8")
    os.replace(tmp, path)


def summary_markdown(s: dict) -> str:
    def block(title: str, data: dict) -> list[str]:
        rows = [f"- {key}: {value}" for key, value in sorted(data.items(), key=lambda kv: -kv[1])]
        return [f"\n## {title}", *(rows or ["- нет"])]

    lines = [f"# Leadfinder: итог запуска {s['дата']}", "",
             f"ICP: {s['icp']}", "",
             f"- компаний в базе запуска: {s['компаний_просмотрено']} "
             f"(с лидом: {s['с_лидом']}, без лида: {s['без_лида']}, сайт недоступен: {s['сайт_недоступен']})",
             f"- страниц разобрано: {s['страниц_разобрано']}",
             f"- запросов в сеть за последний запуск: {s['запросов_в_сеть']}, взято из кэша: {s['из_кэша']}",
             f"- лидов: {s['лидов']}"]
    if s.get("вызовов_llm"):
        lines.append(f"- вызовов LLM: {s['вызовов_llm']}, принято ответов: {s['ответов_llm_принято']}")
    lines += block("Лиды по типу адреса", s["лиды_по_типу_адреса"])
    lines += block("Лиды по роли", s["лиды_по_роли"])
    lines += block("Отклонённые адреса по причинам", s["отклонено_по_причинам"])
    lines += block("Компании по источникам", s["компании_по_источникам"])
    lines += block("Отсеяно на этапе поиска компаний", s["отсеяно_на_поиске"])
    return "\n".join(lines) + "\n"


class Store:
    """Everything a run leaves on disk: leads.csv, no_lead.csv, summary and state.json.

    state.json keeps the result of every company, so a stopped run continues where it was:
    finished companies are not crawled again, unreachable sites are retried.
    """

    def __init__(self, out_dir: Path) -> None:
        self.dir = Path(out_dir)
        self.state_path = self.dir / "state.json"
        self.companies: dict[str, dict] = {}
        self.run: dict = {}
        try:
            data = json.loads(self.state_path.read_text("utf-8"))
        except (OSError, ValueError):
            data = {}
        if isinstance(data.get("companies"), dict):
            self.companies, self.run = data["companies"], data.get("run") or {}

    def done(self, domain: str) -> bool:
        return self.companies.get(domain, {}).get("status") in ("lead", "no_lead")

    def put(self, result: CompanyResult) -> None:
        self.companies[result.company.domain] = {
            **asdict(result.company), "status": result.status, "leads": result.leads, "no_lead": result.no_lead,
            "rejects": result.rejects, "pages": result.pages, "checked_on": result.checked_on,
            "moved_to": result.moved_to}

    def summary(self) -> dict:
        entries = list(self.companies.values())
        leads = [row for e in entries for row in e.get("leads") or []]
        rejects: Counter = Counter()
        for e in entries:
            rejects.update(e.get("rejects") or {})
        return {
            "дата": P.today().isoformat(),
            "icp": self.run.get("icp", ""),
            "компаний_просмотрено": len(entries),
            "с_лидом": sum(e.get("status") == "lead" for e in entries),
            "без_лида": sum(e.get("status") == "no_lead" for e in entries),
            "сайт_недоступен": sum(e.get("status") == "error" for e in entries),
            "страниц_разобрано": sum(int(e.get("pages") or 0) for e in entries),
            "запросов_в_сеть": self.run.get("network", 0),
            "из_кэша": self.run.get("cache", 0),
            "вызовов_llm": self.run.get("llm_calls", 0),
            "ответов_llm_принято": self.run.get("llm_accepted", 0),
            "лидов": len(leads),
            "лиды_по_типу_адреса": dict(Counter(row.get("тип_адреса", "") for row in leads)),
            "лиды_по_роли": dict(Counter(ROLE_LABELS.get(row.get("роль", ""), row.get("роль", "")) for row in leads)),
            "отклонено_по_причинам": dict(rejects),
            "компании_по_источникам": dict(Counter(e.get("source") or "?" for e in entries)),
            "отсеяно_на_поиске": dict(self.run.get("dropped") or {}),
        }

    def save(self) -> dict:
        self.dir.mkdir(parents=True, exist_ok=True)
        entries = list(self.companies.values())
        leads = [row for e in entries for row in e.get("leads") or []]
        no_leads = [e["no_lead"] for e in entries if e.get("status") != "lead" and e.get("no_lead")]
        P.write_rows(self.dir / "leads.csv", LEAD_COLUMNS, leads)
        P.write_rows(self.dir / "no_lead.csv", NO_LEAD_COLUMNS, no_leads)
        summary = self.summary()
        _write_text(self.dir / "summary.json", json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
        _write_text(self.dir / "summary.md", summary_markdown(summary))
        _write_text(self.state_path, json.dumps({"version": 1, "run": self.run, "companies": self.companies},
                                                ensure_ascii=False, indent=1) + "\n")
        return summary


# --------------------------------------------------------------------------- #
# Opt-out: --forget <domain|email>
# --------------------------------------------------------------------------- #

SUPPRESSION_FILE = "suppression.json"
SUPPRESSION_NOTE = ("SHA-256 адресов, доменов и людей (фамилия + компания), удалённых по запросу; "
                    "сами значения не хранятся")
TEXT_SUFFIXES = frozenset({".csv", ".json", ".jsonl", ".md", ".txt", ".log"})


def _read_hashes(path: Path) -> set[str]:
    try:
        data = json.loads(Path(path).read_text("utf-8"))
    except (OSError, ValueError):
        return set()
    return {str(h) for h in (data.get("sha256") or [])} if isinstance(data, dict) else set()


def _write_hashes(path: Path, hashes: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_text(path, json.dumps({"note": SUPPRESSION_NOTE, "sha256": sorted(hashes)}, ensure_ascii=False,
                                 indent=1) + "\n")


def suppression_files(out_dir: Path, cache_dir: Path | None = None) -> list[Path]:
    """Every place the opt-out list is kept. It is ONE list with copies: next to the page cache
    (any run reads it, whatever --out says), in the result directory and in its parent
    (leadfinder_out/ for a run kept in leadfinder_out/<name>/). Deleting one copy loses nothing."""
    out = Path(out_dir)
    files = [out / SUPPRESSION_FILE, out.parent / SUPPRESSION_FILE]
    if cache_dir is not None:
        files.append(Path(cache_dir) / SUPPRESSION_FILE)
    return files


def load_suppressed(out_dir: Path, cache_dir: Path | None = None) -> frozenset[str]:
    """The opt-out list a run must respect: the union of all its copies."""
    hashes: set[str] = set()
    for path in suppression_files(out_dir, cache_dir):
        hashes |= _read_hashes(path)
    return frozenset(hashes)


def run_dirs(root: Path) -> list[Path]:
    """Result directories under `root`, itself included: every directory that holds a state.json."""
    root = Path(root)
    found = {path.parent for path in root.rglob("state.json")} if root.is_dir() else set()
    return sorted(found | {root})


def forget(value: str, out_dir: Path, cache_dir: Path) -> dict[str, int]:
    """Remove a contact (an address or a whole domain) from EVERY result directory under
    `out_dir`, from the evidence fragments of other leads and from the page cache, and remember
    its hash so that no later run, into whatever directory, emits it again.

    The opt-out list keeps SHA-256 hashes only: the list itself holds no personal data.
    """
    value = (value or "").strip().lower()
    is_email = "@" in value
    target = value if is_email else P.normalize_domain(value)
    if not target or (is_email and not EMAIL_RE.fullmatch(target)):
        raise SystemExit(f"--forget: «{value}» не адрес и не домен")
    out_dir, cache_dir = Path(out_dir), Path(cache_dir)
    target_host = _ascii_host(target.rsplit("@", 1)[-1])
    target_site = site_of(target_host)
    stats = {"лидов_удалено": 0, "компаний_удалено": 0, "общих_контактов_удалено": 0, "страниц_кэша_удалено": 0,
             "упоминаний_затёрто": 0, "каталогов_результатов": 0}
    if is_email:
        mention_re = re.compile(rf"(?<![\w.+-]){re.escape(target)}(?![\w-])", re.I)
    else:  # any address on the domain or its subdomains
        mention_re = re.compile(
            rf"(?<![\w.+-])[a-z0-9._%+-]+@(?:[a-z0-9-]+\.)*{re.escape(target_site)}(?![\w.-])", re.I)

    def hit(address: str) -> bool:
        address = (address or "").strip().lower()
        if is_email:
            return address == target
        return "@" in address and site_of(address.rsplit("@", 1)[1]) == target_site

    def same_site(site: str) -> bool:
        return not is_email and site_of(host_of(site)) == target_site

    def scrub(text: str) -> str:
        cleaned, count = mention_re.subn(SCRUBBED, text or "")
        stats["упоминаний_затёрто"] += count
        return cleaned

    out_dir.mkdir(parents=True, exist_ok=True)
    people: set[str] = set()
    # Cached pages of the company's own site go too: the address may be printed there in a
    # form a plain search does not see («name [at] site.ru»).
    purge_sites: set[str] = set() if _is_free_mail(target_host) else {target_site}
    dirs = run_dirs(out_dir)
    for directory in dirs:
        store = Store(directory)
        if not store.companies and not store.state_path.exists():
            continue
        stats["каталогов_результатов"] += 1
        before = (stats["лидов_удалено"], stats["компаний_удалено"], stats["общих_контактов_удалено"],
                  stats["упоминаний_затёрто"])
        for domain, entry in list(store.companies.items()):
            if same_site(domain):
                stats["компаний_удалено"] += 1
                stats["лидов_удалено"] += len(entry.get("leads") or [])
                del store.companies[domain]
                continue
            kept = [row for row in entry.get("leads") or [] if not hit(row.get("Email", ""))]
            gone = [row for row in entry.get("leads") or [] if hit(row.get("Email", ""))]
            if gone:
                people |= {person_hash(row.get("Фамилия", ""), row.get("Имя", ""), domain) for row in gone}
                stats["лидов_удалено"] += len(gone)
                purge_sites.add(site_of(host_of(domain)))
                entry["leads"] = kept
                if not kept:
                    entry["status"] = "no_lead"
                    entry["no_lead"] = {"Компания": entry.get("name", ""), "site": entry.get("site", ""),
                                        "город": entry.get("city", ""), "сегмент": entry.get("segment", ""),
                                        "общий_контакт": "", "телефон": "", "причина": R_OPTOUT,
                                        "страниц_просмотрено": str(entry.get("pages", "")),
                                        "дата_проверки": P.today().isoformat(), "откуда": entry.get("why", "")}
            if hit((entry.get("no_lead") or {}).get("общий_контакт", "")):
                entry["no_lead"]["общий_контакт"] = ""
                stats["общих_контактов_удалено"] += 1
                purge_sites.add(site_of(host_of(domain)))
            # the address may also stand in the evidence of ANOTHER lead («Иванов …, Петров … ivanov@, petrov@»)
            for row in [*(entry.get("leads") or []), entry.get("no_lead") or {}]:
                for key, text in row.items():
                    if isinstance(text, str) and mention_re.search(text):
                        row[key] = scrub(text)
        if before != (stats["лидов_удалено"], stats["компаний_удалено"], stats["общих_контактов_удалено"],
                      stats["упоминаний_затёрто"]):
            store.save()  # a run that never held the contact is left as it is
        companies_csv = directory / "companies.csv"
        if not is_email and companies_csv.exists():
            rows = _read_csv(companies_csv)
            kept_rows = [row for row in rows if not same_site(row.get("site", ""))]
            if len(kept_rows) != len(rows):
                P.write_rows(companies_csv, list(rows[0]), kept_rows)

    # Whatever else lies under the result root (copies of earlier passes, logs, reports): rows about
    # the contact are dropped, mentions inside other rows and texts are blanked.
    for path in sorted(out_dir.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES or path.name == SUPPRESSION_FILE:
            continue
        try:
            text = path.read_text("utf-8-sig")
        except (OSError, UnicodeDecodeError):
            continue
        if not mention_re.search(text) and (is_email or target_site not in text):
            continue
        if path.suffix.lower() == ".csv":
            rows = _read_csv(path)
            if rows and ({"Email", "site"} & set(rows[0])):
                kept_rows = [row for row in rows
                             if not hit(row.get("Email", "")) and not same_site(row.get("site", ""))]
                stats["лидов_удалено"] += sum(1 for row in rows if hit(row.get("Email", "")))
                P.write_rows(path, list(rows[0]),
                             [{k: scrub(v) if isinstance(v, str) else v for k, v in row.items()} for row in kept_rows])
                continue
        if path.suffix.lower() == ".jsonl" and not is_email:
            text = "".join(line for line in text.splitlines(keepends=True) if target_site not in line)
        _write_text(path, scrub(text))

    hashes = {_hash(target)} | people
    for path in dict.fromkeys([*suppression_files(out_dir, cache_dir), *(d / SUPPRESSION_FILE for d in dirs)]):
        if path.parent == out_dir.parent and not path.exists():
            continue  # the parent of the result directory is read, never created by us
        _write_hashes(path, _read_hashes(path) | hashes)

    if cache_dir.is_dir():
        for path in cache_dir.glob("*.json"):
            if path.name in ("mx.json", SUPPRESSION_FILE):
                if path.name == "mx.json" and not is_email:
                    try:
                        data = json.loads(path.read_text("utf-8"))
                    except (OSError, ValueError):
                        continue
                    kept_mx = {k: v for k, v in data.items() if site_of(k) != target_site} if isinstance(data, dict) \
                        else data
                    if kept_mx != data:
                        _write_text(path, json.dumps(kept_mx, ensure_ascii=False))
                continue
            try:
                data = json.loads(path.read_text("utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            html = str(data.get("html") or "").lower()
            host = site_of(host_of(str(data.get("final_url") or data.get("url") or "")))
            if is_email:  # printed as is, or behind Cloudflare's email protection
                printed = target in html or any(P._decode_cfemail(a or b).lower() == target
                                                for a, b in P.CFEMAIL_RE.findall(html))
            else:
                printed = f"@{target_site}" in html
            if host in purge_sites or host_of(str(data.get("url") or "")) == target_host or printed:
                path.unlink(missing_ok=True)
                stats["страниц_кэша_удалено"] += 1
    return stats


def _read_csv(path: Path) -> list[dict[str, str]]:
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Leadfinder: фильтры ICP на входе, контакты ЛПР с доказательством на выходе.",
        epilog="Прокси для российских сайтов: переменная окружения POLZA_SOCKS=127.0.0.1:1080. "
               "Поиск через Serper включается переменной SERPER_API_KEY. robots.txt сайтов соблюдается всегда, "
               "ключа для его отключения нет.")
    p.add_argument("--icp", type=Path, help="файл ICP (.json; .yaml при установленном PyYAML)")
    p.add_argument("--seeds", type=Path, action="append", default=[], help="CSV со списком сайтов (можно несколько)")
    p.add_argument("--expocentr", action="append", default=[], metavar="ID",
                   help="id или адрес выставки в каталоге icatalog.expocentr.ru (можно несколько)")
    p.add_argument("--search", action="store_true", help="поиск компаний через Serper API (нужен SERPER_API_KEY)")
    p.add_argument("--limit", type=int, default=0,
                   help="сколько компаний взять (по умолчанию limits.companies из ICP; свой список --seeds "
                        "без этого ключа берётся целиком)")
    p.add_argument("--pages", type=int, default=0, help="бюджет страниц на сайт (по умолчанию из ICP)")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT, help="каталог результатов")
    p.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE, help="каталог кэша страниц")
    p.add_argument("--cache-days", type=float, default=CACHE_DAYS,
                   help="сколько дней страница живёт в кэше; потом она запрашивается заново")
    p.add_argument("--workers", type=int, default=4, help="сколько сайтов обходить одновременно")
    p.add_argument("--delay", type=float, default=1.0, help="пауза между запросами к одному сайту, с (не меньше 1)")
    p.add_argument("--timeout", type=float, default=20.0, help="таймаут запроса, с")
    p.add_argument("--retry-errors", type=int, default=1,
                   help="сколько раз за запуск повторить сайты, которые не ответили (по умолчанию 1)")
    p.add_argument("--retry-wait", type=float, default=20.0, help="пауза перед повтором не ответивших сайтов, с")
    p.add_argument("--llm", choices=("none", "claude"), default="none",
                   help="разбор спорных блоков через Claude Code CLI (по умолчанию выключен)")
    p.add_argument("--llm-model", default="sonnet")
    p.add_argument("--min-confidence", type=int, default=MIN_CONFIDENCE, help="порог уверенности для лида")
    p.add_argument("--mailto", choices=("none", "matched", "card", "all"), default="none",
                   help="адрес, который не напечатан, а стоит только в ссылке mailto: none - не лид (по умолчанию); "
                        "matched - принимать в карточке человека, если адрес совпадает с ФИО; card - любой в "
                        "карточке человека; all - везде")
    p.add_argument("--no-mx", action="store_true", help="не проверять MX домена")
    p.add_argument("--refresh", action="store_true",
                   help="заново обойти и готовые компании, страницы запросить из сети, а не из кэша")
    p.add_argument("--discover-only", action="store_true",
                   help="только найти компании (companies.csv), сайты не обходить")
    p.add_argument("--forget", metavar="АДРЕС|ДОМЕН",
                   help="отказ от обработки: удалить контакт из всех каталогов результатов внутри --out и из кэша "
                        "и больше не выдавать ни в одном запуске")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def proxy_reachable(proxy: str) -> bool:
    import socket

    parts = urlsplit(proxy)
    try:
        with socket.create_connection((parts.hostname or "", parts.port or 1080), timeout=3):
            return True
    except OSError:
        return False


def build_adapters(args: argparse.Namespace, icp: ICP, env: dict[str, str]) -> list:
    adapters: list = [SeedsAdapter(path) for path in args.seeds]
    adapters += [ExpocentrAdapter(item) for item in args.expocentr]
    for source in icp.sources if not (args.seeds or args.expocentr or args.search) else []:
        kind = str(source.get("type") or "")
        if kind == "seeds":
            adapters.append(SeedsAdapter(SCRIPT_DIR / str(source.get("path") or "")))
        elif kind == "expocentr":
            adapters.append(ExpocentrAdapter(str(source.get("exhibition") or ""),
                                             country=str(source.get("country") or icp.country)))
        elif kind == "search":
            if env.get("SERPER_API_KEY"):
                adapters.append(SerperAdapter(env["SERPER_API_KEY"], queries=source.get("queries")))
            else:
                log.info("источник search пропущен: нет SERPER_API_KEY")
        else:
            raise SystemExit(f"ICP: неизвестный источник «{kind}» (доступны seeds, expocentr, search)")
    if args.search:
        adapters.append(SerperAdapter(env.get("SERPER_API_KEY", "")))
    return adapters


COMPANY_COLUMNS = ["Компания", "site", "город", "сегмент", "источник", "почему", "переезд"]
TUNNEL_STREAK = 5  # unreachable sites in a row before the proxy itself is suspected
_DEFAULT = object()


def _write_companies(path: Path, companies: list[Company], store: Store | None = None) -> None:
    """companies.csv: what discovery returned, and where a company's site has moved (when the crawl saw it)."""
    def moved(company: Company) -> str:
        target = (store.companies.get(company.domain) or {}).get("moved_to", "") if store else ""
        return f"сайт перенаправляет на {target}, обход шёл там" if target else ""

    P.write_rows(path, COMPANY_COLUMNS,
                 [{"Компания": c.name, "site": c.site, "город": c.city, "сегмент": c.segment, "источник": c.source,
                   "почему": c.why, "переезд": moved(c)} for c in companies])


def run(argv: list[str] | None = None, *, fetcher: LeadFetcher | None = None, backend=_DEFAULT,
        mx: MXResolver | None = None, env: dict[str, str] | None = None) -> int:
    """CLI entry point; `fetcher`, `backend`, `mx` and `env` can be injected by tests."""
    args = parse_args(argv)
    env = dict(os.environ) if env is None else env
    logging.basicConfig(stream=sys.stderr, level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    if args.forget:
        stats = forget(args.forget, args.out, args.cache_dir)
        print("Удалено и добавлено в список отказов: " + ", ".join(f"{k}: {v}" for k, v in stats.items()))
        if not (stats["лидов_удалено"] or stats["компаний_удалено"] or stats["общих_контактов_удалено"]
                or stats["упоминаний_затёрто"]):
            print(f"ВНИМАНИЕ: «{args.forget}» не найден ни в одном каталоге результатов внутри {args.out}. "
                  "Хэш записан, новые запуски контакт не выдадут; если результаты лежат в другом месте, "
                  "повторите команду с --out <каталог>.")
        return 0

    icp = load_icp(args.icp)
    if args.pages:
        icp.pages_per_site = args.pages
    limit = args.limit or icp.max_companies
    suppressed = load_suppressed(args.out, args.cache_dir)
    proxy = normalize_proxy(env.get("POLZA_SOCKS", ""))
    if proxy and fetcher is None and not proxy_reachable(proxy):
        raise SystemExit(f"TUNNEL DOWN: прокси POLZA_SOCKS={env.get('POLZA_SOCKS')} не отвечает")
    if proxy.startswith("socks") and fetcher is None:
        try:
            import socksio  # noqa: F401  (httpx needs it for SOCKS proxies)
        except ImportError as exc:
            raise SystemExit("Для POLZA_SOCKS нужен пакет socksio: pip install 'httpx[socks]'") from exc
    try:
        adapters = build_adapters(args, icp, env)
    except DiscoveryError as exc:
        raise SystemExit(str(exc)) from exc
    if not adapters:
        raise SystemExit("Не задан источник компаний: --seeds CSV, --expocentr ID, --search или sources в ICP")

    own_fetcher = fetcher is None
    fetcher = fetcher or LeadFetcher(cache_dir=args.cache_dir, timeout=args.timeout, retries=1,
                                     delay=max(1.0, args.delay), proxy=proxy, cache_days=args.cache_days)
    fetcher.suppressed = frozenset(fetcher.suppressed) | suppressed  # an injected fetcher obeys the opt-out list too
    fetcher.refresh = bool(args.refresh)
    if backend is _DEFAULT:
        try:
            backend = P.ClaudeCLIBackend(model=args.llm_model) if args.llm == "claude" else None
        except P.LLMError as exc:
            raise SystemExit(f"LLM недоступна: {exc}") from exc
    if mx is None and not args.no_mx:
        mx = MXResolver(cache_path=args.cache_dir / "mx.json")
    cfg = Settings(icp=icp, min_confidence=args.min_confidence, mailto=args.mailto, mx=mx,
                   suppressed=suppressed)
    ctx = RunCtx(fetcher=fetcher, cfg=cfg, backend=backend)
    store = Store(args.out)
    started = time.monotonic()
    try:
        companies, dropped = discover_all(adapters, icp, fetcher, limit, suppressed, explicit_limit=bool(args.limit))
        if dropped.get(R_OVER_LIMIT):
            log.warning("список длиннее лимита: взято %d компаний, %d оставлено (ключ --limit)", len(companies),
                        dropped[R_OVER_LIMIT])
        args.out.mkdir(parents=True, exist_ok=True)
        if suppressed and suppressed != _read_hashes(args.out / SUPPRESSION_FILE):
            _write_hashes(args.out / SUPPRESSION_FILE, set(suppressed))  # every result directory carries the list
        _write_companies(args.out / "companies.csv", companies)
        todo = [c for c in companies if args.refresh or not store.done(c.domain)]
        for company in todo:  # sites that did not answer last time are asked again, whatever the cache remembers
            if store.companies.get(company.domain, {}).get("status") == "error":
                fetcher.forget_failures(company.domain)
        log.info("ICP «%s»: компаний найдено %d, отсеяно %d, уже готово %d, в работе %d", icp.name, len(companies),
                 sum(dropped.values()), len(companies) - len(todo), len(todo))

        def snapshot() -> dict:
            store.run = {"icp": icp.name, "dropped": dict(dropped), "network": fetcher.network_hits,
                         "cache": fetcher.cache_hits, "llm_calls": ctx.llm_calls, "llm_accepted": ctx.llm_accepted}
            return store.save()

        if args.discover_only:
            log.info("только поиск компаний: %s", args.out / "companies.csv")
            return 0

        def crawl_all(batch: list[Company], label: str) -> None:
            finished = streak = 0
            warned = False
            with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
                futures = {pool.submit(process_company, company, ctx): company for company in batch}
                for future in as_completed(futures):
                    company = futures[future]
                    finished += 1
                    try:
                        result = future.result()
                    except Exception as exc:  # one broken site must not kill the batch
                        log.exception("%s: обработка упала", company.domain)
                        result = CompanyResult(company=company, status="error", checked_on=P.today().isoformat(),
                                               no_lead=_no_lead_row(company, "", "", f"ошибка обработки: "
                                                                    f"{P._short_error(exc)}", 0))
                    store.put(result)
                    snapshot()  # progress is on disk after every company
                    note = f"лидов: {len(result.leads)}" if result.leads else result.no_lead.get("причина", "")[:90]
                    log.info("%s[%d/%d] %s (%s): %s", label, finished, len(batch), company.name[:40], company.domain,
                             note)
                    streak = streak + 1 if result.status == "error" else 0
                    if streak >= TUNNEL_STREAK and proxy and not warned:
                        if not proxy_reachable(proxy):
                            pool.shutdown(wait=False, cancel_futures=True)
                            raise SystemExit(f"TUNNEL DOWN: прокси POLZA_SOCKS={env.get('POLZA_SOCKS')} перестал "
                                             f"отвечать; сделанное сохранено в {args.out}, запустите команду ещё раз")
                        warned = True
                        log.warning("%d сайтов подряд не ответили: похоже, туннель подвис. Они будут повторены "
                                    "в конце запуска", streak)

        crawl_all(todo, "")
        for attempt in range(max(0, args.retry_errors)):
            failed = [c for c in todo if store.companies.get(c.domain, {}).get("status") == "error"]
            if not failed:
                break
            log.info("повтор %d: %d сайтов не ответили, пробую ещё раз через %.0f с", attempt + 1, len(failed),
                     args.retry_wait)
            if args.retry_wait > 0:
                time.sleep(args.retry_wait)
            for company in failed:
                fetcher.forget_failures(company.domain)
                moved = store.companies.get(company.domain, {}).get("moved_to")
                if moved:
                    fetcher.forget_failures(moved)
            crawl_all(failed, "повтор ")
        summary = snapshot()
        _write_companies(args.out / "companies.csv", companies, store)
    finally:
        if own_fetcher:
            fetcher.close()
    log.info("готово за %.0f с -> %s | компаний: %d, лидов: %d, без лида: %d, недоступно: %d",
             time.monotonic() - started, args.out, summary["компаний_просмотрено"], summary["лидов"],
             summary["без_лида"], summary["сайт_недоступен"])
    return 0


if __name__ == "__main__":
    sys.exit(run())
