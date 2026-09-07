"""Тесты fulltext-сверки: поднятие веб-источников до verified по полному тексту."""

from __future__ import annotations

import pytest

from src.legal import fulltext
from src.legal.fulltext import (
    _extract_requirement_keys,
    _match_count,
    _normalize,
    verify_source_fulltext,
)
from src.legal.models import LegalSource


def _web_source(url: str = "https://www.vsrf.ru/documents/plenum/14458/") -> LegalSource:
    return LegalSource(
        id="WEB-001",
        source_type="case_law",
        title="Постановление Пленума ВС РФ от 28.06.2012 № 17",
        authority="Верховный Суд РФ",
        citation="Постановление Пленума ВС РФ № 17 от 28.06.2012",
        excerpt="",
        official_url=url,
        verified=False,
        verification_status="partially_verified",
        provider="sonar_web_search",
        relevance_score=5.0,
    )


PAGE_TEXT = (
    "Постановление Пленума Верховного Суда Российской Федерации № 17 "
    "г. Москва, 28 июня 2012 г. О рассмотрении судами гражданских дел по спорам "
    "о защите прав потребителей. Суд разъясняет, что потребитель вправе "
    "потребовать возврат уплаченной суммы. " * 5
)


def test_normalize():
    assert _normalize("Статья  18 Ёлка") == "статья 18 елка"


def test_extract_requirement_keys():
    keys = _extract_requirement_keys(_web_source())
    assert "28.06.2012" in keys
    assert "№ 17" in keys
    assert len(keys) >= 2


def test_match_count():
    keys = {"№ 17", "28.06.2012", "ст 999"}
    assert sorted(_match_count(keys, _normalize(PAGE_TEXT))) == sorted(["№ 17", "28.06.2012"])


def test_verify_official_domain_becomes_verified(monkeypatch):
    monkeypatch.setattr(fulltext, "_fetch_page_text", lambda url: PAGE_TEXT)
    result = verify_source_fulltext(_web_source())
    assert result.verification_status == "verified"
    assert result.verified is True
    assert "потребитель" in result.excerpt
    assert result.warning is None


def test_verify_unofficial_domain_stays_partial(monkeypatch):
    monkeypatch.setattr(fulltext, "_fetch_page_text", lambda url: PAGE_TEXT)
    source = _web_source(url="https://sudact.ru/regular/doc/XYZ/")
    result = verify_source_fulltext(source)
    assert result.verification_status == "partially_verified"
    assert result.verified is False
    assert result.excerpt  # выдержка заполнена
    assert "неофициальн" in (result.warning or "")


def test_verify_mismatch_keeps_source(monkeypatch):
    monkeypatch.setattr(
        fulltext, "_fetch_page_text", lambda url: "Совершенно другой текст без реквизитов. " * 20
    )
    source = _web_source()
    result = verify_source_fulltext(source)
    assert result.verification_status == "partially_verified"
    assert result.excerpt == ""
    assert "не найдены" in (result.warning or "")


def test_verify_network_error_keeps_source(monkeypatch):
    def _boom(url):
        raise RuntimeError("сеть недоступна")

    monkeypatch.setattr(fulltext, "_fetch_page_text", _boom)
    result = verify_source_fulltext(_web_source())
    assert result.verification_status == "partially_verified"
    assert "не сверён" in (result.warning or "")


def test_verify_skips_without_url_or_verified():
    no_url = _web_source(url="")  # URL пуст
    no_url.official_url = None
    assert verify_source_fulltext(no_url) is no_url

    already = _web_source()
    already.verification_status = "verified"
    already.verified = True
    already.excerpt = "уже есть"
    assert verify_source_fulltext(already) is already


def test_verify_skips_insufficient_requirements(monkeypatch):
    called = {"n": 0}

    def _spy(url):
        called["n"] += 1
        return PAGE_TEXT

    monkeypatch.setattr(fulltext, "_fetch_page_text", _spy)
    source = LegalSource(
        id="WEB-009", source_type="case_law", title="Обзор практики судебной коллегии",
        authority="ВС", citation="Обзор", excerpt="",
        official_url="https://www.vsrf.ru/documents/all/33968",
    )
    result = verify_source_fulltext(source)
    assert called["n"] == 0  # сетевой вызов не выполнялся
    assert result is source