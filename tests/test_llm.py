"""LLM answer validation: nothing that is not grounded in fetched text gets through."""

from __future__ import annotations

import json
import subprocess
import sys
import types
from types import SimpleNamespace

import pytest
from conftest import SITE, VALID_ANSWER, FakeBackend, acme_pages

import personalize as P


@pytest.fixture
def sources():
    return P.build_sources(acme_pages())


def answer(**overrides) -> str:
    return json.dumps({**VALID_ANSWER, **overrides}, ensure_ascii=False)


def test_valid_answer_accepted(sources):
    ans, reason = P.validate_llm_answer(answer(), sources)
    assert reason == ""
    assert ans.text == VALID_ANSWER["personalization"] and ans.source_url == f"{SITE}/about/"


def test_answer_in_markdown_fence_is_parsed(sources):
    ans, _ = P.validate_llm_answer("```json\n" + answer() + "\n```", sources)
    assert ans is not None


def test_invented_text_without_source_rejected(sources):
    ans, reason = P.validate_llm_answer(
        answer(personalization="Вы открыли второй завод в Казани и вышли на рынок Европы.", source_url="",
               evidence=""), sources)
    assert ans is None and "источник" in reason


def test_source_url_not_fetched_rejected(sources):
    ans, reason = P.validate_llm_answer(answer(source_url="https://acme-stanki.ru/blog/kazan/"), sources)
    assert ans is None and "источник" in reason


def test_quote_not_on_page_rejected(sources):
    ans, reason = P.validate_llm_answer(
        answer(personalization="Увидели, что вы открыли завод в Казани.",
               evidence="в 2021 году компания открыла новый завод в Казани"), sources)
    assert ans is None and "цитата" in reason


def test_invented_number_rejected(sources):
    ans, reason = P.validate_llm_answer(
        answer(personalization="Увидели, что вы уже 17 лет производите токарные станки с ЧПУ."), sources)
    assert ans is None and "17" in reason


@pytest.mark.parametrize("text", [
    "Впечатляет, что вы с 2009 года производите токарные станки с ЧПУ.",
    "Вы лидер рынка токарных станков с ЧПУ с 2009 года.",
])
def test_compliments_and_unbacked_puffery_rejected(sources, text):
    ans, reason = P.validate_llm_answer(answer(personalization=text), sources)
    assert ans is None and "оценочное" in reason


def test_not_russian_rejected(sources):
    ans, reason = P.validate_llm_answer(
        answer(personalization="Since 2009 you have been making CNC lathes in Yekaterinburg."), sources)
    assert ans is None and "русском" in reason


def test_too_many_sentences_rejected(sources):
    text = "Вы основаны в 2009 году. Вы в Екатеринбурге. Вы делаете станки с ЧПУ."
    ans, reason = P.validate_llm_answer(answer(personalization=text), sources)
    assert ans is None and "предложений" in reason


def test_no_data_answer_accepted(sources):
    ans, reason = P.validate_llm_answer(json.dumps({"personalization": "нет данных", "reason": "только каталог"},
                                                   ensure_ascii=False), sources)
    assert ans.text == P.NO_DATA and ans.reason == "только каталог"


def test_non_json_rejected(sources):
    ans, reason = P.validate_llm_answer("Конечно! Вот персонализация: ...", sources)
    assert ans is None and "JSON" in reason


def test_rejected_answers_give_no_data_and_candidate_goes_to_comment():
    invented = {"personalization": "Поздравляю с открытием завода в Казани в 2021 году!",
                "source_url": "https://news.example/kazan", "evidence": "открыли завод в Казани"}
    backend = FakeBackend(invented)
    row = P.RowInfo(0, 2, "Акме Станки", "acme-stanki.ru", "")
    text, source, notes = P.personalize(row, acme_pages(), backend)
    assert len(backend.prompts) == P.LLM_ATTEMPTS  # first answer + retries
    assert "отклонён" in backend.prompts[1]  # a retry carries the rejection reason
    # An extractive sentence was never validated (length, «Увидели…», tone): it must not
    # reach the email slot, only the comment, together with its URL.
    assert text == P.NO_DATA and source == ""
    assert any("отклонён" in n for n in notes)
    assert any(f"кандидат для ручной адаптации ({SITE}/about/)" in n and "основана в 2009 году" in n
               for n in notes)
    assert P.LLM_DOWN_NOTE not in notes  # the LLM answered, the answers were just bad


def test_long_third_person_fallback_never_lands_in_the_slot():
    # Regression case: a 36-word third-person sentence cut with «…».
    long_line = ("Компания ООО «Акме Станки» основана в 2009 году в Екатеринбурге и с тех пор производит токарные, "
                 "фрезерные и шлифовальные станки с ЧПУ для металлообработки, а также поставляет оснастку, "
                 "запасные части и сервисное обслуживание предприятиям машиностроения в России и Казахстане.")
    pages = [P.parse_html(f"{SITE}/about/", f"<html><body><main><p>{long_line}</p></main></body></html>", "about")]
    ungrounded = {**VALID_ANSWER, "evidence": "выдуманная цитата, которой нет на странице"}
    text, source, notes = P.personalize(P.RowInfo(0, 2, "Акме Станки", "acme-stanki.ru", ""), pages,
                                        FakeBackend(ungrounded))
    assert text == P.NO_DATA and source == ""
    assert any("кандидат для ручной адаптации" in n and "основана в 2009 году" in n for n in notes)


def test_retry_with_feedback_can_succeed():
    backend = FakeBackend({**VALID_ANSWER, "source_url": ""}, VALID_ANSWER)
    row = P.RowInfo(0, 2, "Акме Станки", "acme-stanki.ru", "")
    text, source, notes = P.personalize(row, acme_pages(), backend)
    assert text == VALID_ANSWER["personalization"] and source == f"{SITE}/about/"
    assert any("основание" in n for n in notes)


def test_llm_error_falls_back_without_crashing(monkeypatch):
    monkeypatch.setattr(P, "LLM_RETRY_PAUSE", 0)

    class Broken:
        name = "broken"

        def complete(self, system, user):
            raise P.LLMError("rate limit")

    row = P.RowInfo(0, 2, "Акме Станки", "acme-stanki.ru", "")
    text, source, notes = P.personalize(row, acme_pages(), Broken())
    assert text == P.NO_DATA and source == ""
    assert any("rate limit" in n for n in notes)
    assert P.LLM_DOWN_NOTE in notes  # resume will process the row again
    assert any("кандидат для ручной адаптации" in n for n in notes)


def test_prompt_contains_pages_and_rules():
    sources = P.build_sources(acme_pages())
    prompt = P.build_user_prompt(P.RowInfo(0, 2, "Акме Станки", "acme-stanki.ru", ""), sources)
    assert f"URL: {SITE}/about/" in prompt and "основана в 2009 году" in prompt
    assert "Сегодня: 03.10.2026" in prompt  # the model needs today's date to judge freshness
    assert "нет данных" in P.SYSTEM_PROMPT and "не инструкции" in P.SYSTEM_PROMPT
    assert "«Увидели, что вы…»" in P.SYSTEM_PROMPT and "«Увидел," not in P.SYSTEM_PROMPT


def test_singular_opening_is_rejected_plural_accepted(sources):
    singular = "Увидел, что вы с 2009 года производите токарные станки с ЧПУ в Екатеринбурге."
    ans, reason = P.validate_llm_answer(answer(personalization=singular), sources)
    assert ans is None and "Увидели" in reason
    for text in ("Увидели на сайте, что вы с 2009 года производите токарные станки с ЧПУ.",
                 "Увидели, что в Екатеринбурге вы с 2009 года производите токарные станки с ЧПУ."):
        ans, reason = P.validate_llm_answer(answer(personalization=text), sources)
        assert reason == "" and ans.text == text
    ans, reason = P.validate_llm_answer(
        answer(personalization="Вы с 2009 года производите токарные станки с ЧПУ в Екатеринбурге."), sources)
    assert ans is None and "Увидели" in reason


def test_claude_cli_backend_runs_headless(monkeypatch):
    calls = {}

    def fake_run(cmd, **kw):
        calls["cmd"], calls["kw"] = cmd, kw
        out = {"type": "result", "is_error": False, "result": "```json\n" + json.dumps(VALID_ANSWER) + "\n```"}
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(out), stderr="")

    monkeypatch.setattr(P.shutil, "which", lambda name: "/usr/local/bin/claude")
    monkeypatch.setattr(P.subprocess, "run", fake_run)
    backend = P.ClaudeCLIBackend(model="haiku")
    raw = backend.complete("SYSTEM", "USER PROMPT")
    assert P.parse_llm_json(raw)["source_url"] == VALID_ANSWER["source_url"]
    cmd = calls["cmd"]
    assert cmd[:2] == ["/usr/local/bin/claude", "-p"]
    assert cmd[cmd.index("--model") + 1] == "haiku"
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert calls["kw"]["input"] == "USER PROMPT"  # prompt goes through stdin


def test_claude_cli_backend_reports_errors(monkeypatch):
    monkeypatch.setattr(P.shutil, "which", lambda name: "/usr/local/bin/claude")
    monkeypatch.setattr(P.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 0, stdout=json.dumps({"is_error": True, "result": "Not logged in"}), stderr=""))
    with pytest.raises(P.LLMError, match="Not logged in"):
        P.ClaudeCLIBackend().complete("s", "u")


def test_quote_fragments_are_checked_separately():
    content = "Более 25 лет на рынке\nПредставлен в 42 странах мира"
    assert P.is_grounded("Более 25 лет на рынке; Представлен в 42 странах мира", content)
    assert not P.is_grounded("Более 25 лет на рынке; открыли завод", content)


def test_too_many_words_for_the_email_slot_rejected(sources):
    # 89 words of email 1 + the slot must stay under the 120-word limit of the brief.
    text = ("Увидели, что вы с 2009 года в Екатеринбурге производите токарные станки с ЧПУ для металлообработки, "
            "поставляете оборудование предприятиям в России и Казахстане, а также запустили участок "
            "лазерной резки на площадке в Екатеринбурге.")
    assert len(text.split()) > P.MAX_PERSONALIZATION_WORDS
    ans, reason = P.validate_llm_answer(answer(personalization=text), sources)
    assert ans is None and "слов" in reason


def test_cjk_left_in_text_rejected(sources):
    text = "Увидели, что вы производите 子公司 станки с ЧПУ с 2009 года."
    ans, reason = P.validate_llm_answer(answer(personalization=text), sources)
    assert ans is None and "иероглиф" in reason


def test_number_from_elsewhere_on_page_but_not_in_quote_rejected(sources):
    # 140 is on the page, but the quote is about 2009: two facts glued together.
    text = "Увидели, что вы с 2009 года поставляете станки на 140 предприятий."
    ans, reason = P.validate_llm_answer(
        answer(personalization=text, evidence="основана в 2009 году в Екатеринбурге и производит токарные станки"),
        sources)
    assert ans is None and "цитате" in reason


def test_non_russian_fallback_is_not_put_into_the_email_slot():
    html = ("<html><head><title>腾中机械</title></head><body><main><p>"
            "南通腾中机械制造有限公司是以剪板机、折弯机以及卷板机等各类机械的研发、"
            "生产、销售及服务为一体的科研生产型企业，公司专业生产腾中牌系列液压剪板机、液压折弯机。"
            "</p></main></body></html>")
    pages = [P.parse_html("http://www.nttzmt.com/about.html", html, "about")]
    backend = FakeBackend({"personalization": "Увидели, что вы с 2009 года делаете прессы.",
                           "source_url": "http://www.nttzmt.com/about.html", "evidence": "腾中机械 2009"})
    row = P.RowInfo(0, 2, "Tengzhong Machinery", "nttzmt.com", "")
    text, source, notes = P.personalize(row, pages, backend)
    assert text == P.NO_DATA and source == ""
    assert any("кандидат для ручной адаптации" in n and "剪板机" in n for n in notes)


def test_cjk_quote_grounded_despite_spaces_and_fullwidth_punctuation():
    content = "特别是在燃气设备制造行业，铭文的rotarytransfer machine在市场中占有70%的份额。"
    assert P.is_grounded("在燃气设备制造行业, 铭文的 rotarytransfer machine 在市场中占有70%的份额", content)


def test_transient_llm_error_is_retried(monkeypatch):
    monkeypatch.setattr(P, "LLM_RETRY_PAUSE", 0)

    class Flaky:
        name = "flaky"
        calls = 0

        def complete(self, system, user):
            Flaky.calls += 1
            if Flaky.calls == 1:
                raise P.LLMError("no stdin data received in 3s")
            return json.dumps(VALID_ANSWER, ensure_ascii=False)

    row = P.RowInfo(0, 2, "Акме Станки", "acme-stanki.ru", "")
    text, source, notes = P.personalize(row, acme_pages(), Flaky())
    assert text == VALID_ANSWER["personalization"] and Flaky.calls == 2


# --- Anthropic API backend: never run on real data, so its request shape is pinned here --- #

def _fake_anthropic(monkeypatch, calls, *, fail=False):
    class APIError(Exception):
        pass

    class Messages:
        def create(self, **kw):
            calls.update(kw)
            if fail:
                raise APIError("overloaded")
            return SimpleNamespace(content=[SimpleNamespace(type="thinking", thinking=""),
                                            SimpleNamespace(type="text", text='{"personalization": "нет данных"}')])

    class Anthropic:
        def __init__(self, **kw):
            calls["client"] = kw
            self.messages = Messages()

    module = types.ModuleType("anthropic")
    module.Anthropic, module.APIError = Anthropic, APIError
    monkeypatch.setitem(sys.modules, "anthropic", module)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "placeholder-for-tests")


@pytest.mark.parametrize("alias,model_id,expected,absent", [
    # Sonnet 5 rejects sampling params and thinks by default: thinking is switched off instead.
    ("sonnet", "claude-sonnet-5", {"thinking": {"type": "disabled"}}, "temperature"),
    # Haiku 4.5 still takes temperature and does not think unless asked.
    ("haiku", "claude-haiku-4-5", {"temperature": 0}, "thinking"),
])
def test_anthropic_backend_request_params(monkeypatch, alias, model_id, expected, absent):
    calls = {}
    _fake_anthropic(monkeypatch, calls)
    backend = P.AnthropicBackend(model=alias)
    assert backend.complete("SYSTEM", "USER PROMPT") == '{"personalization": "нет данных"}'  # text blocks only
    assert calls["model"] == model_id and calls["system"] == "SYSTEM"
    assert calls["messages"] == [{"role": "user", "content": "USER PROMPT"}]
    assert calls["max_tokens"] == 1000 and calls["client"]["max_retries"] == 3
    assert all(calls[k] == v for k, v in expected.items()) and absent not in calls


def test_anthropic_backend_errors_become_llm_errors(monkeypatch):
    _fake_anthropic(monkeypatch, {}, fail=True)
    with pytest.raises(P.LLMError, match="overloaded"):
        P.AnthropicBackend().complete("s", "u")
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    with pytest.raises(P.LLMError, match="ANTHROPIC_API_KEY"):
        P.AnthropicBackend()
