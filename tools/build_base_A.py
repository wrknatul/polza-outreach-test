#!/usr/bin/env python3
"""Build and validate task1_base_A.csv (segment: IT / SaaS / B2B services).

Candidates were found by scanning the companies' own sites (homepage +
contact pages) and reading the pages by hand. This script does NOT invent
anything: tools/build_base_common.py re-fetches the pages and keeps a row
only if the email is on `email_source`, its domain has MX, the sales-signal
phrase is on `signal_url` and a non-generic contact_role is labelled on the
page (see ROLE_EVIDENCE). Rows are taken in priority order until TARGET rows
are collected; the rest are reserves. This validator works with the company
mailbox (sales@, info@, hello@ ...); the decision maker's name and job title
are added by tools/enrich_base.py from lpr/.

Since the base was rebuilt from leads (task1_leads.csv, tools/build_base_all.py), these
company mailboxes are the reserve: the rows live on in task1_reserve.csv, and this script
still re-validates them into task1_base_A.csv.

Usage: python3 tools/build_base_A.py [--out PATH]   (default: task1_base_A.csv)
"""
import sys
from pathlib import Path

from build_base_common import ROOT, validate, write_csv

# 27 here + 29 in part B = 56 rows of the first version of the base (three rows were swapped:
# weak-fit marketing rows here for two SaaS rows; part B took the third slot).
TARGET = 27
OUT = ROOT / "task1_base_A.csv"

# company, site, contact_role, email, email_source, segment,
# signal_text, signal_url, signal_check (phrase that must be on signal_url), city
CANDIDATES = [
    # --- CRM / IT integrators and sales automation ---
    ("Интерволга", "https://www.intervolga.ru",
     "Заказ услуг («Заказать наши услуги, проконсультироваться, предложить партнёрство»)", "sale@intervolga.ru",
     "https://www.intervolga.ru/contacts/",
     "Интеграторы и автоматизация: внедрение 1С-Битрикс, Битрикс24 и отечественного ПО",
     ("Отдельный ящик для новых клиентов: «Заказать наши услуги, проконсультироваться, предложить "
      "партнёрство»; в контактах указан руководитель коммерческого отдела"),
     "https://www.intervolga.ru/contacts/", "Заказать наши услуги, проконсультироваться", "Волгоград"),
    ("Аспро", "https://aspro.ru", "Общий адрес (отдел продаж — по телефону в шапке)", "info@aspro.ru",
     "https://aspro.ru/contact/",
     "Интеграторы и автоматизация: готовые решения на 1С-Битрикс, партнёрская сеть внедренцев",
     "В шапке сайта телефон «Отдел продаж», раздел «Стать партнером»",
     "https://aspro.ru/contact/", "Стать партнером", "Челябинск"),
    ("ГЕНЕЗИС", "https://gnzs.ru", "Общий адрес", "hello@gnzs.ru",
     "https://gnzs.ru/contact", "Интеграторы и автоматизация: внедрение amoCRM, виджеты и интеграции",
     "Форма для новых клиентов «Обсудим стратегию вашего роста», на сайте заявлен ТОП-10 среди интеграторов amoCRM",
     "https://gnzs.ru/company", "Обсудим стратегию вашего роста", "Нижегородская обл."),
    ("АйТи-Солюшн (IT-Solution)", "https://it-solution.ru", "Направление Битрикс24 (реализация лицензий)",
     "b24@it-solution.ru",
     "https://it-solution.ru/contacts/", "Интеграторы и автоматизация: Битрикс24 и 1С, платиновый партнёр 1С-Битрикс",
     "Лид-форма на главной: «Получите консультацию по подбору услуг»",
     "https://it-solution.ru/", "Получите консультацию по подбору услуг", "Санкт-Петербург"),
    ("Radist.Online", "https://radist.online", "Общий адрес (отдел продаж — тел. доб. 1)", "hello@radist.online",
     "https://radist.online/aboutus/", "Интеграторы и автоматизация: SaaS-интеграция мессенджеров с CRM",
     "На странице о компании выделен отдел продаж: «1 — отдел продаж»",
     "https://radist.online/aboutus/", "1 — отдел продаж", "Казань"),
    ("Клеверенс", "https://www.cleverence.ru", "Отдел продаж", "sales@cleverence.ru",
     "https://www.cleverence.ru/", "Интеграторы и автоматизация: ПО для склада, магазина и маркировки (ТСД)",
     "Партнёрская программа «Стать партнером», отдел продаж в контактах",
     "https://www.cleverence.ru/", "Стать партнером", "Москва"),
    # --- B2B SaaS ---
    ("Kaiten", "https://kaiten.ru", "Отдел продаж («Приобретение Кайтен»)", "sales@kaiten.ru",
     "https://kaiten.ru/contacts", "B2B SaaS: управление проектами и задачами",
     "Кнопка «Записаться на демо» на главной",
     "https://kaiten.ru/", "Записаться на демо", "Москва"),
    ("Shtab", "https://shtab.app", "Отдел продаж", "sales@shtab.app",
     "https://shtab.app/contacts/", "B2B SaaS: платформа совместной работы (задачи и проекты)",
     "Блок «Отдел продаж»: «Подбор тарифа, расчёт на команду и коммерческое предложение»",
     "https://shtab.app/contacts/", "Подбор тарифа, расчёт на команду и коммерческое предложение", "Санкт-Петербург"),
    ("Пачка", "https://pachca.com", "Вопросы покупки («Думаете о покупке? Обсудите с экспертом»)", "sales@pachca.com",
     "https://pachca.com/contacts", "B2B SaaS: корпоративный мессенджер",
     ("«Обсудите с экспертом специфику вашей компании: получите информацию о "
      "возможностях, стоимости или запросите демо»"),
     "https://pachca.com/contacts", "запросите демо", "Санкт-Петербург"),
    ("Okdesk", "https://okdesk.ru", "Менеджеры по продажам", "sales@okdesk.ru",
     "https://okdesk.ru/contacts/", "B2B SaaS: Service Desk, учёт заявок и выездного обслуживания",
     "Публичные тарифы; «Стоимость подписки на 6 месяцев уточняйте у менеджеров по продажам»",
     "https://okdesk.ru/contacts/", "уточняйте у менеджеров по продажам", "Пенза"),
    ("Скорозвон", "https://skorozvon.ru", "Служба продаж", "sales@skorozvon.ru",
     "https://skorozvon.ru/contact-information", "B2B SaaS: автоматизация звонков для отделов продаж",
     "Кнопка «Записаться на демо» на главной",
     "https://skorozvon.ru/", "Записаться на демо", "Екатеринбург"),
    ("Adesk", "https://adesk.ru", "Отдел продаж", "sales@adesk.ru",
     "https://adesk.ru/", "B2B SaaS: управленческий учёт и финансы для бизнеса",
     "В подвале «Отдел продаж: sales@adesk.ru», в шапке «Записаться на демо»",
     "https://adesk.ru/", "Записаться на демо", "Миасс (Челябинская обл.)"),
    ("Intradesk", "https://intradesk.ru", "Общий адрес", "information@intradesk.ru",
     "https://intradesk.ru/contacts/", "B2B SaaS: Service Desk / Help Desk для поддержки клиентов",
     "Бесплатное внедрение и демо: «позволяя увидеть выгоду уже на демо-доступе»",
     "https://intradesk.ru/", "увидеть выгоду уже на демо-доступе", "Москва"),
    ("TEAMLY", "https://teamly.ru", "Общий адрес", "info@teamly.ru",
     "https://teamly.ru/contacts/", "B2B SaaS: корпоративная база знаний",
     "Кнопка «Записаться на демо», партнёрская программа",
     "https://teamly.ru/", "Записаться на демо", "Москва"),
    ("UIS", "https://www.uiscom.ru", "Отдел продаж", "op@uiscom.ru",
     "https://www.uiscom.ru/kontakty/", "B2B SaaS: облачная телефония и коммуникации для бизнеса",
     "В контактах отдельный блок «Отдел продаж» с телефоном и e-mail, в шапке кнопка «Получить консультацию»",
     "https://www.uiscom.ru/kontakty/", "Получить консультацию", "Москва"),
    ("Chat2Desk", "https://chat2desk.com", "Общий адрес (в реквизитах компании)", "info@chat2desk.com",
     "https://chat2desk.com/o-nas", "B2B SaaS: агрегатор мессенджеров и чат-центр для бизнеса",
     ("Кнопка «Получить консультацию»: «Поможем с настройкой, покажем возможности, "
      "рассчитаем стоимость»; раздел «Стать партнёром»"),
     "https://chat2desk.com/", "рассчитаем стоимость", "Санкт-Петербург"),
    # --- Logistics / fulfilment for business ---
    ("Кактус (Kak2c)", "https://kak2c.ru", "Отдел продаж (для новых клиентов)", "sales@kak2c.ru",
     "https://kak2c.ru/contacts/", "Логистика: фулфилмент для e-commerce и маркетплейсов",
     "В контактах отдельный блок «Отдел продаж» с подписью «Для новых клиентов»",
     "https://kak2c.ru/contacts/", "Для новых клиентов", "Красногорск (Московская обл.)"),
    ("Logsis", "https://logsis.ru", "Коммерческий отдел", "sales@logsis.ru",
     "https://logsis.ru/contacts/", "Логистика: курьерская доставка для интернет-магазинов и юрлиц",
     "Блок «Коммерческий отдел» — «Заключение договоров, консультации по услугам, вопросы интеграции»",
     "https://logsis.ru/contacts/", "Заключение договоров, консультации по услугам", "Москва"),
    ("Dalli Service", "https://dalli-service.com", "Отдел продаж", "sale@dalli-service.com",
     "https://dalli-service.com/contacts/", "Логистика: курьерская доставка для бизнеса и e-commerce",
     "Первым блоком в контактах — «Отдел продаж» с телефоном по всей России",
     "https://dalli-service.com/contacts/", "Отдел продаж", "Москва"),
    ("TopDelivery", "https://www.topdelivery.ru", "Отдел продаж", "newclient@topdelivery.ru",
     "https://www.topdelivery.ru/contacts/", "Логистика: курьерская служба для интернет-магазинов",
     "Отдельный ящик newclient@ для новых клиентов, «Отдел продаж: доб 103, 116»",
     "https://www.topdelivery.ru/contacts/", "Отдел продаж: доб 103, 116", "Москва"),
    # --- B2B marketing ---
    # Sendsay and DashaMail (email services that mail only opted-in
    # lists) and CRM Group (email-marketing agency, a near-competitor of
    # Polza) were removed as weak-fit targets; UIS and Chat2Desk replace them.
    ("Callibri", "https://callibri.ru", "Общий адрес (ящик sale@)", "sale@callibri.ru",
     "https://callibri.ru/", "B2B-маркетинг: SaaS для лид-менеджмента, коллтрекинга и сквозной аналитики",
     "Партнёрский канал продаж: «Партнерская программа», «CRM-интеграторам»",
     "https://callibri.ru/", "CRM-интеграторам", "Екатеринбург"),
    ("Пиксель Плюс", "https://pixelplus.ru", "Общий адрес агентства (отдел продаж — по телефону)",
     "manager@pixelplus.ru",
     "https://pixelplus.ru/contact/", "B2B-маркетинг: digital-агентство (SEO, сайты, реклама)",
     "В контактах «Отдел продаж: +7 495 989-53-11», отделы продаж в нескольких городах",
     "https://pixelplus.ru/contact/", "Отдел продаж:", "Москва"),
    # --- HR-tech ---
    ("FriendWork", "https://friend.work", "Отдел продаж", "sales@friend.work",
     "https://friend.work/contacts", "HR-tech: ATS для автоматизации рекрутинга",
     "Кнопка «Получить демо-доступ», в подвале «Отдел продаж»",
     "https://friend.work/", "Получить демо-доступ", "Санкт-Петербург"),
    ("Happy Job", "https://happy-job.ru", "Отдел продаж", "sales@happy-job.ru",
     "https://happy-job.ru/contacts/sales/", "HR-tech: платформа опросов вовлечённости и eNPS",
     "Отдельная страница контактов отдела продаж и форма «Запросить демо»",
     "https://happy-job.ru/contacts/sales/", "Запросить демо", "Москва"),
    ("Teachbase", "https://teachbase.ru", "Общий адрес (сотрудничество и партнёрства)", "info@teachbase.ru",
     "https://teachbase.ru/contacts/", "HR-tech: LMS для корпоративного обучения",
     "Кнопка «Оставить заявку», публичные тарифы и партнёрская программа",
     "https://teachbase.ru/", "Оставить заявку", "Москва"),
    ("Skillaz", "https://skillaz.ru", "Общий адрес", "hello@skillaz.ru",
     "https://skillaz.ru/contacts", "HR-tech: платформа автоматизации подбора и управления персоналом",
     "Кнопка «Записаться на демо»",
     "https://skillaz.ru/", "Записаться на демо", "Москва"),
    ("Experium", "https://experium.ru", "Отдел продаж", "info@experium.ru",
     "https://experium.ru/contacts", "HR-tech: система автоматизации подбора персонала",
     "В контактах «Отдел продаж», кнопка «Заказать демо»",
     "https://experium.ru/contacts", "Заказать демо", "Москва"),
    # --- reserves (used only if a row above fails validation) ---
    # K50: MX is mx.yandex-team.ru, i.e. likely part of a large group -> reserve
    ("K50", "https://k50.ru", "Коммерческий отдел", "welcome@k50.ru",
     "https://k50.ru/contacts/", "B2B-маркетинг: SaaS для автоматизации контекстной рекламы",
     "В контактах выделен «Коммерческий отдел»",
     "https://k50.ru/contacts/", "Коммерческий отдел", "Москва"),
    ("Talk-Me", "https://talk-me.ru", "Общий адрес (ящик office@)", "office@talk-me.ru",
     "https://talk-me.ru/about", "B2B SaaS: онлайн-чат и омниканальная поддержка для сайтов",
     "Публичные тарифы и «Партнёрская программа»",
     "https://talk-me.ru/", "Партнёрская программа", "Калининград"),
]

# Label phrase that the email_source page puts next to the address, for every
# row whose contact_role is not «Общий адрес…» (checked by the validator).
ROLE_EVIDENCE = {
    "sale@intervolga.ru": "Заказать наши услуги",
    "b24@it-solution.ru": "реализация лицензий «Битрикс24»",
    "sales@cleverence.ru": "Отдел продаж",
    "sales@kaiten.ru": "Приобретение Кайтен",
    "sales@shtab.app": "Отдел продаж",
    "sales@pachca.com": "Думаете о покупке",
    "sales@okdesk.ru": "менеджеров по продажам",
    "sales@skorozvon.ru": "Служба продаж",
    "sales@adesk.ru": "Отдел продаж",
    "op@uiscom.ru": "Отдел продаж",
    "sales@kak2c.ru": "Для новых клиентов",
    "sales@logsis.ru": "Коммерческий отдел",
    "sale@dalli-service.com": "Отдел продаж",
    "newclient@topdelivery.ru": "Отдел продаж",
    "sales@friend.work": "Отдел продаж",
    "sales@happy-job.ru": "Отдел продаж",
    "info@experium.ru": "Отдел продаж",
    "welcome@k50.ru": "Коммерческий отдел",
}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    out = Path(argv[argv.index("--out") + 1]) if "--out" in argv else OUT
    kept, report = validate(CANDIDATES, ROLE_EVIDENCE, TARGET, label_width=30)
    write_csv(kept, out)
    print("\n".join(report))
    print(f"\nwritten {len(kept)} rows -> {out}")
    return 0 if len(kept) >= TARGET else 1


if __name__ == "__main__":
    sys.exit(main())
