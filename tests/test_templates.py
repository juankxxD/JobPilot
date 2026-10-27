"""Jinja2 template rendering (no WeasyPrint): localized headers and no tables/images."""

from __future__ import annotations

from jobpilot.config import Language
from jobpilot.documents.render import render_cover_html, render_cv_html
from jobpilot.models import ContentSelection
from jobpilot.profile.model import ProfileModel

ES_HEADERS = ["Resumen", "Experiencia", "Educación", "Habilidades", "Certificaciones", "Proyectos"]
EN_HEADERS = ["Summary", "Experience", "Education", "Skills", "Certifications", "Projects"]


def _selection(language: Language) -> ContentSelection:
    return ContentSelection(
        summary_variant="backend_java",
        ordered_achievement_ids=["ach_acme_latency", "ach_acme_events"],
        project_ids=["prj_openbudget"],
        highlighted_skill_ids=["Java", "AWS"],
        language=language,
    )


def test_cv_es_has_localized_headers(example_profile: ProfileModel) -> None:
    html = render_cv_html(example_profile, _selection(Language.es))
    for header in ES_HEADERS:
        assert f">{header}</h2>" in html
    # Spanish summary text present, single-column (no table/image/icon markup).
    assert "Ingeniera backend" in html
    assert "<table" not in html
    assert "<img" not in html


def test_cv_en_has_localized_headers(example_profile: ProfileModel) -> None:
    html = render_cv_html(example_profile, _selection(Language.en))
    for header in EN_HEADERS:
        assert f">{header}</h2>" in html
    assert "Backend engineer" in html
    assert "<table" not in html
    assert "<img" not in html


def test_cv_respects_achievement_order(example_profile: ProfileModel) -> None:
    selection = _selection(Language.en).model_copy(
        update={"ordered_achievement_ids": ["ach_acme_events", "ach_acme_latency"]}
    )
    html = render_cv_html(example_profile, selection)
    events = html.index("event-driven Kafka")
    latency = html.index("p95 latency")
    assert events < latency


def test_cover_es_and_en_render(example_profile: ProfileModel) -> None:
    body_es = "Estimado equipo,\n\nMe entusiasma postularme para este rol."
    html_es = render_cover_html(example_profile, language=Language.es, body=body_es, company="ACME")
    assert "ACME" in html_es
    assert "Me entusiasma" in html_es
    assert example_profile.basics.name in html_es

    body_en = "Dear team,\n\nI am excited to apply for this role."
    html_en = render_cover_html(example_profile, language=Language.en, body=body_en, company="ACME")
    assert "excited to apply" in html_en
