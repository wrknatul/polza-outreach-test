#!/usr/bin/env python3
"""Build and validate task1_base_B.csv (segment: industrial B2B).

Every candidate row was researched by hand on the company's own website.
This script does NOT invent anything: tools/build_base_common.py re-fetches
the pages and keeps a row only if the email is on `email_source`, its domain
has MX, the sales-signal phrase is on `signal_url` and a non-generic
contact_role is labelled on the page (see ROLE_EVIDENCE). Rows are taken in
priority order until TARGET rows are collected; the rest are reserves.
This validator works with the company mailbox; the decision maker's name and
job title are added by tools/enrich_base.py from lpr/.

Fetching, decoding (lenient fallback for pages with mixed encodings), retries
and the DNS fallback live in build_base_common.py, shared with part A.

Since the base was rebuilt from leads (task1_leads.csv, tools/build_base_all.py), these
company mailboxes are the reserve: the rows live on in task1_reserve.csv, and this script
still re-validates them into task1_base_B.csv.

Usage: python3 tools/build_base_B.py [--out PATH]   (default: task1_base_B.csv)
"""
import sys
from pathlib import Path

from build_base_common import ROOT, validate, write_csv

# 29 here + 27 in part A = 56 rows of the first version of the base.
TARGET = 29
OUT = ROOT / "task1_base_B.csv"

# company, site, contact_role, email, email_source, segment,
# signal_text, signal_url, signal_check (phrase that must be on signal_url), city
CANDIDATES = [
    ("ГофроМир (ООО «Гофромир»)", "https://gofromir.ru", "Заказ упаковки («для заказа упаковки»)", "order@gofromir.ru",
     "https://gofromir.ru/contacts/", "Упаковка: гофротара, коробки с печатью",
     "На главной раздел «Для оптовых клиентов» и кнопка «Рассчитать оптовую стоимость»",
     "https://gofromir.ru/", "Рассчитать оптовую стоимость", "Москва"),
    ("Павлово-Посадский гофрокомбинат", "https://www.ppgk.ru", "Общий адрес (в форме есть пункт «Отдел продаж»)",
     "info@ppgk.ru",
     "https://www.ppgk.ru/", "Упаковка: гофрокартон и гофротара (производство)",
     ("В форме обращения отдельные пункты «Отдел продаж (вопросы по заказу)» и «Отдел "
      "продаж (запрос на разработку упаковки)»"),
     "https://www.ppgk.ru/", "Отдел продаж (запрос на разработку упаковки)", "Павловский Посад (МО)"),
    ("ПЗПИ (ООО «ПЗПИ»)", "https://pzpi.ru", "Отдел продаж", "zakaz@pzpi.ru",
     "https://pzpi.ru/services/contract-production/", "Упаковка: пластиковая тара и укупорка, контрактное производство",
     "Отдельная линия «Отдел продаж», «Крупный опт ПНД, ПЭТ тары и укупорки», услуга контрактного производства",
     "https://pzpi.ru/services/contract-production/", "Крупный опт ПНД", "Воскресенск (МО)"),
    ("Новастрейч", "https://novastretch.ru", "Приём заявок на продукцию", "sales@novastretch.ru",
     "https://novastretch.ru/contacts/", "Упаковка: стрейч-плёнка и клейкие ленты (производство)",
     "Открыта вакансия «Менеджер отдела продаж»",
     "https://novastretch.ru/jobs/", "Менеджер отдела продаж", "Санкт-Петербург"),
    ("IP GROUP (ООО «ИнтроПластика»)", "https://introplastik.com",
     "Общий адрес центрального офиса (через него оформляют оптовые заказы)", "info@introplastik.ru",
     "https://introplastik.com/strejch-plenka", "Упаковка: стрейч-плёнка (производство)",
     "«Мы подготовим для вас коммерческое предложение…», индивидуальные условия для оптовиков, сетей и производств",
     "https://introplastik.com/strejch-plenka", "Мы подготовим для вас коммерческое предложение", "Орёл"),
    ("Паклэнд", "https://pack-land.ru", "Общий адрес", "info@pack-land.ru",
     "https://pack-land.ru/proizvodstvo-strejch-plenki", "Упаковка: стрейч-плёнка и упаковочные материалы",
     "«Купить стрейч пленку от производителя оптом или в розницу» с доставкой по Москве и МО",
     "https://pack-land.ru/proizvodstvo-strejch-plenki", "от производителя оптом", "Москва"),
    ("PALLETOPTOM (ООО «Комплект»)", "https://palletoptom.ru", "Общий адрес («Написать нам»)", "msk@palletoptom.ru",
     "https://palletoptom.ru/", "Упаковка: деревянные поддоны (производство)",
     "Раздел «Запрос стоимости», поддоны «оптом и в розницу», доставка в любой регион России",
     "https://palletoptom.ru/", "Запрос стоимости", "Луховицы (МО)"),
    ("Паллетснаб (ООО «Паллетснаб»)", "https://palletsnab.ru", "Общий адрес", "hello@palletsnab.ru",
     "https://palletsnab.ru/proizvodstvo/", "Упаковка: деревянные поддоны (производство)",
     "Кнопка «Стать клиентом», продажа «в розницу, так и крупным оптом» по договору",
     "https://palletsnab.ru/proizvodstvo/", "крупным оптом", "Московская обл. (склады в Чехове и Ступине)"),
    ("Балт-Паллет", "https://www.balt-pallet.ru", "Общий адрес (официальный e-mail)", "info@balt-pallet.ru",
     "https://www.balt-pallet.ru/o-kompanii/index.htm", "Упаковка: деревянные поддоны (производство)",
     "Отдельный телефон отдела продаж; «для постоянных клиентов и оптовых покупателей есть гибкая система скидок»",
     "https://www.balt-pallet.ru/o-kompanii/index.htm", "оптовых покупателей", "Санкт-Петербург"),
    # Reserve promoted (replaces ТД «ГПА», see comment below)
    ("Картон-Сервис", "https://kartons.ru", "Общий адрес (заказы по России)", "info@kartons.ru",
     "https://kartons.ru/contacts/", "Упаковка: гофротара (производство и поставка)",
     ("Отдельные контакты для заказов по России, Москве и Калуге: «Любые размеры на заказ "
      "с доставкой по РФ»; кнопка «Оставить заявку»"),
     "https://kartons.ru/contacts/", "Любые размеры на заказ с доставкой по РФ", "Калуга"),
    ("Типография «Этикетка для Вас» (label4u)", "https://label4u.ru", "Общий адрес (ящик zakaz@)", "zakaz@label4u.ru",
     "https://label4u.ru/", "Упаковка: самоклеящиеся этикетки (флексопечать)",
     ("«Оформите заявку — мы свяжемся с вами и пришлём коммерческое предложение»; на сайте "
      "открыта вакансия «Менеджер по работе с клиентами»"),
     "https://label4u.ru/", "пришлём коммерческое предложение", "Москва"),
    ("Подольский завод оборудования (ПЗО)", "https://p-z-o.com", "Отдел продаж", "sell@p-z-o.com",
     "https://p-z-o.com/contacts", "Промоборудование: прессы, станки, металлообработка",
     "Открыта вакансия «Менеджер по продажам оборудования»; есть программа «Стать дилером»",
     "https://p-z-o.com/vakancii", "Менеджер по продажам оборудования", "Подольск (МО)"),
    ("Волгоградский завод весоизмерительной техники (ВЗВТ)", "https://tdvzvt.ru", "Отдел продаж", "sales@vzvt.ru",
     "https://tdvzvt.ru/contacts/", "Промоборудование: промышленные весы (производство)",
     "Страница «Как стать дилером завода»: «Станьте дилером ВЗВТ и зарабатывайте вместе с нами»",
     "https://tdvzvt.ru/company/stat-dilerom/", "Станьте дилером ВЗВТ", "Волгоград"),
    ("Мировое оборудование", "https://ok-stanok.ru", "Отдел продаж / работа с партнёрами", "sales@ok-stanok.ru",
     "https://ok-stanok.ru/page/31-dillers", "Промоборудование: переработка полимеров, чиллеры",
     ("Дилерская программа: «приглашает к сотрудничеству дилеров, агентов и "
      "отраслевых специалистов по всей России и СНГ»"),
     "https://ok-stanok.ru/page/31-dillers", "приглашает к сотрудничеству дилеров", "Москва"),
    ("Завод ЕКС (конвейерное оборудование)", "https://euroconveyor-st.ru", "Общий адрес (ящик sales@)",
     "sales@euroconveyor-st.ru",
     "https://euroconveyor-st.ru/", "Промоборудование: конвейеры (производство)",
     "Онлайн-калькулятор конвейера: «скачайте готовое коммерческое предложение»; раздел «Партнерам»",
     "https://euroconveyor-st.ru/", "скачайте готовое коммерческое предложение", "Москва"),
    ("ГК «Велунд Сталь»", "https://gkws.ru", "Московский офис (почта для заявок)", "moscow@gkws.ru",
     "https://gkws.ru/proizvodstvo-konvejernogo-oborudovaniya/", "Промоборудование: металлообработка, конвейеры",
     "Кнопка «Запросить КП» на странице услуги производства конвейеров",
     "https://gkws.ru/proizvodstvo-konvejernogo-oborudovaniya/", "Запросить КП", "Москва"),
    # Reserve promoted (replaces ТД «ГПА»: enerprom.com was not updated
    # since 2022 and its sales vacancy had no date, so the signal was unprovable)
    ("ООО «Траяна» (завод конвейеров)", "https://zavod-conveyer.ru", "Общий адрес (заявки на КП и смету)",
     "trayana@zavod-conveyer.ru",
     "https://zavod-conveyer.ru/", "Промоборудование: конвейеры (производство)",
     "Кнопка «Заказать расчёт конвейера» и форма «Получите КП и смету»; раздел «Партнёрам»",
     "https://zavod-conveyer.ru/", "Получите КП и смету", "Москва"),
    ("Донвард – Гидравлические системы", "https://donvard.ru", "Отдел продаж", "info@donvard.ru",
     "https://donvard.ru/contacts/", "Промоборудование: гидравлика (производство)",
     "Отдельный телефон отдела продаж, кнопка «Запросить прайс-лист», «Работаем по всей России»",
     "https://donvard.ru/", "Запросить прайс-лист", "Ижевск"),
    ("ЕВРОТЕК", "https://eurotechspb.com", "Общий адрес (ящик sales@)", "sales@eurotechspb.com",
     "https://eurotechspb.com/", "Промоборудование: гидравлика (производство и продажа)",
     "Открыта вакансия в отдел продаж: «Менеджер по работе с клиентами»",
     "https://eurotechspb.com/company/vacancy/", "Менеджер по работе с клиентами", "Санкт-Петербург"),
    ("Завод трубопроводной арматуры «Динамика»", "https://dinamika1.ru", "Отдел продаж", "info@dinamika1.ru",
     "https://dinamika1.ru/", "Промоборудование: трубопроводная арматура (производство)",
     "Email подписан «Отдел продаж»; продукция «по оптовым ценам в комплексной комплектации»",
     "https://dinamika1.ru/", "по оптовым ценам", "Казань"),
    ("Российский завод трубопроводной арматуры (РЗТА)", "https://rzta.ru", "Общий адрес", "info@rzta.ru",
     "https://rzta.ru/kontakty/", "Промоборудование: трубопроводная арматура",
     "«Оформите заявку и получите скидку до 10% на первый заказ» — активно привлекают новых клиентов",
     "https://rzta.ru/kontakty/", "скидку до 10% на первый заказ", "Люберцы (МО)"),
    ("Челнинский арматурный завод (ЧАЗ)", "https://chelaz.ru", "Отдел продаж (общий ящик)", "info@chelaz.ru",
     "https://chelaz.ru/contacts/", "Промоборудование: трубопроводная арматура (производство)",
     "Раздел «Как стать дилером» — завод развивает дилерскую сеть",
     "https://chelaz.ru/about/kak-stat-dilerom/", "Как стать Дилером", "Набережные Челны (пункт выдачи в Москве)"),
    ("ТД «Подшипник Трейд»", "https://podtrade.ru", "Общий адрес", "info@podtrade.ru",
     "https://podtrade.ru/contact/", "Промдистрибуция: подшипники",
     "Блок «Для юрлиц»: «Оптовая система скидок», B2B-кабинет, филиалы в регионах",
     "https://podtrade.ru/", "Оптовая система скидок", "Московская обл. (Томилино)"),
    ("ПК «Ленинградский Подшипник»", "https://ooo-lp.ru", "Отдел продаж", "info@ooo-lp.ru",
     "https://ooo-lp.ru/kontakty.html", "Промдистрибуция: подшипники (производство и опт)",
     "В шапке сайта «Оптовая продажа подшипников», email подписан «Отдел продаж»",
     "https://ooo-lp.ru/", "Оптовая продажа подшипников", "Санкт-Петербург"),
    ("10-ГПЗ (ООО «10-ГПЗ»)", "https://10-gpz.ru", "Общий адрес", "10-gpz@10-gpz.ru",
     "https://10-gpz.ru/", "Промдистрибуция: подшипники (производство и опт)",
     "«Обратитесь в отдел продаж… формировать даже самые крупные оптовые партии»",
     "https://10-gpz.ru/postavka_podshipnikov/", "крупные оптовые партии", "Ростов-на-Дону"),
    ("ПромБеринг", "https://www.prombearing.ru", "Общий адрес", "info@prombearing.ru",
     "https://www.prombearing.ru/contacts/", "Промдистрибуция: подшипники",
     "Отдельный B2B-раздел и кнопка «Получить оптовый прайс»",
     "https://www.prombearing.ru/b2b/", "Получить оптовый прайс", "Санкт-Петербург"),
    ("ОПМ (московское подразделение)", "https://msk.opm.ru", "Общий адрес московского подразделения", "msk@opm.ru",
     "https://msk.opm.ru/about/", "Промдистрибуция: крепёж и метизы",
     "Открыта вакансия «Менеджер по продажам»: оптовая торговля крепежом для промышленных предприятий",
     "https://msk.opm.ru/about/vacansy/", "Оптовая торговля крепежными изделиями", "Москва"),
    ("РСК «Роскрепеж»", "https://rskcorp.ru", "Общий адрес (ящик sales@)", "sales@rskg.ru",
     "https://rskcorp.ru/kontakty", "Промдистрибуция: крепёж и метизы",
     "Блок «Оптовикам и комплектовщикам»: особые условия сотрудничества для оптовиков",
     "https://rskcorp.ru/", "Оптовикам и комплектовщикам", "Санкт-Петербург"),
    ("ОПТИМА КРЕПЕЖ", "https://opt-krep.ru", "Приём заявок («Отправьте заявку на zakaz@…»)", "zakaz@opt-krep.ru",
     "https://opt-krep.ru/", "Промдистрибуция: крепёж, такелаж, электроинструмент",
     "«Оптовый поставщик крепежных изделий в России», кнопка «Отправить заявку»",
     "https://opt-krep.ru/", "Оптовый поставщик крепежных изделий", "Москва"),
    # --- reserves: used only if a row above fails validation ---
    ("КМЗКО (Курганский завод конвейерного оборудования)", "https://konmash.ru", "Общий адрес", "info@konmash.ru",
     "https://konmash.ru/", "Промоборудование: конвейеры (производство)",
     "Отдельный телефон отдела продаж, «Наши представительства в регионах»",
     "https://konmash.ru/", "Наши представительства в регионах", "Курган"),
]

# Label phrase that the email_source page puts next to the address, for every
# row whose contact_role is not «Общий адрес…» (checked by the validator).
ROLE_EVIDENCE = {
    "order@gofromir.ru": "для заказа упаковки",
    "zakaz@pzpi.ru": "Отдел продаж",
    "sales@novastretch.ru": "Прием заявок на нашу продукцию",
    "sell@p-z-o.com": "Отдел продаж",
    "sales@vzvt.ru": "отдел продаж",
    "sales@ok-stanok.ru": "по работе с партнёрами",
    "moscow@gkws.ru": "Оставьте заявку на сайте, почте",
    "info@donvard.ru": "Отдел продаж:",
    "info@dinamika1.ru": "Отдел продаж",
    "info@chelaz.ru": "Отдел продаж",
    "info@ooo-lp.ru": "Отдел продаж",
    "zakaz@opt-krep.ru": "Отправьте заявку на",
}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    out = Path(argv[argv.index("--out") + 1]) if "--out" in argv else OUT
    kept, report = validate(CANDIDATES, ROLE_EVIDENCE, TARGET, label_width=55)
    write_csv(kept, out)
    print("\n".join(report))
    print(f"\nwritten {len(kept)} rows -> {out}")
    return 0 if len(kept) >= TARGET else 1


if __name__ == "__main__":
    sys.exit(main())
