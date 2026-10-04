"""Consistency checks: the planted traps of the test base."""

from __future__ import annotations

import pytest
from conftest import acme_pages

import personalize as P


def row(index, company, site, email=""):
    return P.RowInfo(index=index, line=index + 2, company=company, site=site, email=email)


# --- email domain ---------------------------------------------------------- #

def test_email_domain_mismatch():
    res = P.check_email("sales01@nttzmt.com", "uncomtech.ru", [])
    assert res.status.startswith(P.STATUS_MISMATCH)
    assert "nttzmt.com" in res.status and "uncomtech.ru" in res.status


def test_email_same_domain_is_ok():
    assert P.check_email("sales@hnc.su", "hnc.su", []).status == P.STATUS_OK
    assert P.check_email("Sales@Mail.HNC.su", "https://www.hnc.su/about", []).status == P.STATUS_OK


def test_email_alias_published_on_site_is_ok():
    page = P.parse_html("https://internor-mach.com/", "<html><body><p>Email: sales@internor.com.cn</p></body></html>")
    res = P.check_email("sales@internor.com.cn", "internor-mach.com", [page])
    assert res.status == P.STATUS_OK
    assert any("алиас" in n for n in res.notes)


def test_email_spelling_variant_needs_review():
    res = P.check_email("sales@jatcarbide.com", "jat-carbide.com", [])
    assert res.status.startswith(P.STATUS_REVIEW)


@pytest.mark.parametrize("email", ["swyct@126.com", "boss@gmail.com", "a@mail.ru", "b@qq.com", "c@yandex.ru"])
def test_free_mailbox_flagged(email):
    res = P.check_email(email, "saintymachine.com", [])
    assert res.status.startswith(P.STATUS_MISMATCH)
    assert "бесплатный" in res.status


def test_free_mailbox_published_on_site_is_only_review():
    page = P.parse_html("https://x.cn/", "<html><body><p>Contact: swyct@126.com</p></body></html>")
    res = P.check_email("swyct@126.com", "x.cn", [page])
    assert res.status.startswith(P.STATUS_REVIEW)


def test_invalid_email_is_mismatch():
    assert P.check_email("sales@", "hnc.su", []).status.startswith(P.STATUS_MISMATCH)


def test_role_emails_only_never_personal():
    html = "<p>sales@acme.ru info@acme.ru ivan.petrov@acme.ru logo@2x.png</p>"
    page = P.parse_html("https://acme.ru/", f"<html><body>{html}</body></html>")
    assert P.site_role_emails([page]) == ["sales@acme.ru", "info@acme.ru"]


# --- company name vs site ---------------------------------------------------- #

def test_brand_mismatch_points_to_real_owner():
    page = P.parse_html("https://www.uncomtech.ru/", "<html><head><title>Главная страница</title></head>"
                                                     "<body><p>Станки с ЧПУ</p></body></html>")
    others = [row(4, "ТД Ункомтех", "saintymachine.com")]
    res = P.check_brand("Tengzhong Machinery", "uncomtech.ru", [page], others)
    assert res.brand_ok is False
    assert res.status.startswith(P.STATUS_MISMATCH)
    assert "ТД Ункомтех" in res.status  # owner found through transliteration of the domain


@pytest.mark.parametrize("company,domain", [
    ("ТД Ункомтех", "uncomtech.ru"),
    ("РИЦ Техносфера", "technosphera.ru"),
    ("Искролайн", "iskroline.ru"),
    ("Howfit Science", "howfit-press.com"),
    ("HNC", "hnc.su"),
])
def test_name_matches_domain_across_scripts(company, domain):
    assert P.name_matches_domain(company, domain)


def test_brand_confirmed_by_title_in_cyrillic():
    res = P.check_brand("Акме Станки", "acme-stanki.ru", acme_pages(), [])
    assert res.brand_ok is True and res.status == P.STATUS_OK


def test_generic_industry_words_do_not_prove_ownership():
    # "Cemented Carbide" appears on the site, but the brand "JAT" does not.
    page = P.parse_html("https://www.jillionsupply.com/",
                        "<html><head><title>Jillion Supply | Cemented Carbide Inserts</title></head>"
                        "<body><p>We supply cemented carbide rods and inserts worldwide since 2010.</p></body></html>")
    res = P.check_brand("JAT Cemented Carbide", "jillionsupply.com", [page], [])
    assert res.brand_ok is False
    assert res.status.startswith(P.STATUS_MISMATCH)


def test_short_or_partial_tokens_do_not_match_inside_words():
    assert not P._token_in_text("rogen", "hydrogen storage tanks", allow_concat=False)
    assert not P._token_in_text("jat", "jatropha oil", allow_concat=False)
    assert P._token_in_text("jat", "JAT cemented carbide", allow_concat=False)


def test_name_only_in_body_text_needs_review():
    page = P.parse_html("https://dealer.ru/", "<html><head><title>Дилер Станков</title></head><body>"
                                              "<p>Мы официальный дилер Tesid в России с 2015 года.</p></body></html>")
    res = P.check_brand("Tesid Equipment", "dealer.ru", [page], [])
    assert res.brand_ok is True
    assert res.status.startswith(P.STATUS_REVIEW)


def test_unreachable_site_whose_domain_names_another_company():
    others = [row(5, "JAT Cemented Carbide", "jillionsupply.com")]
    res = P.check_brand("Rogen Technologies", "jat-carbide.com", [], others)
    assert res.brand_ok is False
    assert res.status.startswith(P.STATUS_MISMATCH) and "JAT Cemented Carbide" in res.status


def test_unreachable_site_with_matching_domain_is_review():
    res = P.check_brand("Jimmy CNC Tool", "jimmytool.com", [], [])
    assert res.brand_ok is None and res.status.startswith(P.STATUS_REVIEW)


# --- file-level traps -------------------------------------------------------- #

def test_cross_row_duplicates_and_shift_hints():
    rows = [
        row(0, "Tengzhong Machinery", "uncomtech.ru", "sales01@nttzmt.com"),
        row(1, "ТД Ункомтех", "saintymachine.com", "sales@thebestcnc.com"),
        row(2, "Shixinghong Precision", "saintymachine.com", "swyct@126.com"),
        row(3, "HNC", "hnc.su", "sales@hnc.su"),
    ]
    issues, hints = P.cross_row_checks(rows)
    assert any("saintymachine.com" in t for _, t in issues[1])
    assert any("saintymachine.com" in t for _, t in issues[2])
    assert issues[3] == [] and hints[3] == []
    assert any("ТД Ункомтех" in h for h in hints[0])  # uncomtech.ru looks like row 1's company
    assert any("uncomtech.ru" in h for h in hints[1])


def test_domain_helpers():
    assert P.normalize_domain("https://WWW.Site.ru/about?x=1") == "site.ru"
    assert P.normalize_domain("sales@site.ru") == "site.ru"
    assert P.registrable_domain("shop.internor.com.cn") == "internor.com.cn"
    assert P.domain_core("www.jat-carbide.com") == "jat-carbide"


# --- email not published on a confirmed site -------------------------------- #

def _page(url, body):
    return P.parse_html(url, f"<html><body>{body}</body></html>")


def test_email_typo_against_published_address_needs_review():
    page = _page("http://www.nttzmt.com/", "<p>E-mail: sale01@nttzmt.com</p>")
    res = P.check_email("sales01@nttzmt.com", "nttzmt.com", [page])
    assert res.status.startswith(P.STATUS_REVIEW)
    assert "sale01@nttzmt.com" in res.status and "опечатка" in res.status


def test_other_numbered_mailbox_is_not_a_typo():
    page = _page("https://mgw.test/", "<p>sales02@mgw.test</p>")
    res = P.check_email("sales01@mgw.test", "mgw.test", [page])
    assert res.status == P.STATUS_OK
    assert any("не найден" in n and "sales02@mgw.test" in n for n in res.notes)


@pytest.mark.parametrize("local,published,expected", [
    ("sales01", "sale01", True), ("sales", "sale", True), ("sales01", "sales02", False),
    ("info", "sales", False), ("sales", "sales", False), ("export", "expert", True),
])
def test_looks_like_typo(local, published, expected):
    assert P.looks_like_typo(local, published) is expected


def test_chinese_only_site_is_review_not_mismatch():
    html = ("<html><head><title>南通腾中机械制造有限公司</title></head><body><main>"
            "<p>南通腾中机械制造有限公司成立于2009年，生产剪板机、折弯机和卷板机。</p></main></body></html>")
    page = P.parse_html("http://www.nttzmt.com/", html)
    res = P.check_brand("Tengzhong Machinery", "nttzmt.com", [page], [])
    assert res.status.startswith(P.STATUS_REVIEW) and "иероглифами" in res.status
    assert res.brand_ok is None  # not confirmed, but not a foreign site either


def test_foreign_latin_site_is_still_mismatch():
    page = P.parse_html("https://saintymachine.com/", "<html><head><title>Sainty Machinery</title></head>"
                        "<body><h1>Sainty Machinery</h1><p>Food machines since 1998.</p></body></html>")
    res = P.check_brand("Shixinghong Precision", "saintymachine.com", [page], [])
    assert res.status.startswith(P.STATUS_MISMATCH) and res.brand_ok is False


def test_entity_encoded_and_cloudflare_emails_are_found():
    encoded = "".join(f"&#{ord(c)};" for c in "info@ooo-lp.ru")
    page = _page("https://ooo-lp.ru/kontakty.html", f'<p>Отдел продаж: <a href="mailto:{encoded}">почта</a></p>')
    assert P.check_email("info@ooo-lp.ru", "ooo-lp.ru", [page]).notes == ["email указан на сайте"]
    key = 0x42
    cf = f"{key:02x}" + "".join(f"{ord(c) ^ key:02x}" for c in "sales@acme.ru")
    page = _page("https://acme.ru/", f'<a class="__cf_email__" data-cfemail="{cf}">[email&#160;protected]</a>')
    assert "sales@acme.ru" in P.email_blob([page])
    assert P.site_role_emails([page]) == ["sales@acme.ru"]


def test_contacts_link_is_strict():
    html = ('<a href="/crm/">Контакты клиентов в CRM</a> <a href="/solutions/dozvon">Больше контактов</a>'
            '<a href="/contact-information">Связаться с нами</a>')
    home = P.parse_html("https://skorozvon.test/", f"<html><body><nav>{html}</nav></body></html>")
    assert P.find_contacts_url(home) == "https://skorozvon.test/contact-information"
    none = P.parse_html("https://x.test/", '<html><body><a href="/crm/">Контакты клиентов</a></body></html>')
    assert P.find_contacts_url(none) == ""
