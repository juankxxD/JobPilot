"""Prompt templates. Every prompt embeds the anti-invention rules."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jobpilot.config import ScoringWeights

ANTI_INVENTION_RULES = """\
STRICT RULES — follow all of them:
- Use ONLY facts present in the provided source. Never add information that is not
  there.
- If a datum does not appear in the source, leave it empty (null) or omit it. Do
  NOT guess, infer, or fabricate names, numbers, dates, roles, or companies.
- Do NOT change figures, dates, job titles, or company names.
- Do NOT claim proficiency in a technology that is not in the source.
- Do NOT translate facts on your own beyond what is explicitly requested.
- Respond ONLY with JSON that matches the required schema. No prose, no markdown.
"""


CV_IMPORT_PROMPT = """\
You are converting a candidate's resume into a structured bilingual (Spanish/English)
draft profile. Extract only what the resume contains.

{anti_invention}

Instructions:
- Assign a stable, descriptive id to every experience, achievement, and project
  (e.g. "exp_acme_2022", "ach_acme_latency", "prj_openbudget"). Ids must be unique,
  lowercase, snake_case, and derived from the content.
- Split each role's accomplishments into individual achievements, one id each.
- For every localized text (achievements, projects, summaries), provide BOTH "es"
  and "en". If the resume is written in only one language, translate to the other
  and set "translation_status": "pending_review". Keep the original-language text
  as-is and set its own translation_status to "pending_review" as well, so the
  owner reviews everything.
- Propose three summary_variants keyed "backend_java", "fullstack", "cloud", each
  with "es" and "en", derived ONLY from the resume's real content. If the resume
  does not support a variant, leave its texts empty.
- In "uncertain_fields", list any field you could not confidently extract or that
  looks ambiguous (e.g. "basics.phone", "experience[1].end").

Resume text:
---
{resume_text}
---

Return JSON for this schema (fields may be null/empty when absent):
{schema}
"""


def build_cv_import_prompt(resume_text: str, schema_json: str) -> str:
    return CV_IMPORT_PROMPT.format(
        anti_invention=ANTI_INVENTION_RULES,
        resume_text=resume_text,
        schema=schema_json,
    )


# --------------------------------------------------------------------------- #
# Phase 3 — analysis, scoring and language confirmation
# --------------------------------------------------------------------------- #

ANALYZE_JOB_PROMPT = """\
You are analysing a single job offer and extracting its structured facts.

{anti_invention}

Instructions:
- Extract ONLY what the offer text states. Leave a field null when the offer does
  not state it. Never invent a stack item, seniority, salary, or timezone.
- "required_stack" lists technologies the offer presents as required; put clearly
  optional ones in "nice_to_have_stack". Use the exact technology names the offer
  uses.
- "years_required" is an integer only when the offer gives a number; otherwise null.
- "language" is the language the offer is WRITTEN in ("es" or "en").
- "red_flags" captures concerning signals literally present in the text (e.g. unpaid,
  vague scope); do not editorialise beyond the text.

Job offer:
---
Title: {title}
Company: {company}
Location: {location}
Remote: {remote}
Salary: {salary}
---
{description}
---

Return JSON for this schema (fields may be null/empty when absent):
{schema}
"""

SCORE_JOB_PROMPT = """\
You are scoring how well a candidate fits a job offer, from 0 to 100.

{anti_invention}

Scoring model — rate the candidate's fit for EACH dimension from 0 to 100 in
"dimensions". The overall score is computed outside this call as a weighted sum of
your per-dimension scores; the weights (in points, summing to 100) are shown only
so you understand their relative importance:
- required stack match ("dimensions.required_stack"): {w_required_stack}
- seniority and years of experience ("dimensions.seniority_and_years"):
  {w_seniority_and_years}
- modality and timezone fit ("dimensions.modality_and_timezone"):
  {w_modality_and_timezone}
- offer language vs the candidate's accepted languages ("dimensions.language"):
  {w_language}
- salary and conditions ("dimensions.salary_and_conditions"):
  {w_salary_and_conditions}
- other signals ("dimensions.other"): {w_other}

Instructions:
- Judge fit ONLY from the candidate profile and the analysed offer below. Never
  credit a technology, role, or achievement the profile does not contain.
- Fill every "dimensions" field with a 0-100 score. Do NOT compute an overall
  score yourself; leave "score" at 0.
- "met_requirements": for every offer requirement the candidate genuinely meets,
  add an entry whose "achievement_id" is an EXISTING achievement id from the
  profile (listed under "Profile achievement ids"). Never invent an id. If you
  cannot cite a real achievement id for a requirement, do NOT list it as met.
- "missing_requirements": offer requirements the profile does not support.
- "recommendation" is one of: apply, apply_with_reservations, discard.

Candidate profile:
---
{profile}
---
Profile achievement ids (use ONLY these for met_requirements.achievement_id):
{achievement_ids}
---
Analysed offer:
---
{analysis}
---

Return JSON for this schema:
{schema}
"""

CONFIRM_LANGUAGE_PROMPT = """\
You are identifying the natural language a job offer is written in.

{anti_invention}

Instructions:
- Answer with the dominant language of the text: "es" (Spanish), "en" (English),
  or "other" if the text is written in neither Spanish nor English.
- Never force an offer written in another language into "es" or "en"; answer
  "other" in that case so it is routed to a manual choice.
- "confidence" is a number between 0 and 1 reflecting how sure you are.
- Decide only from the text below; do not guess from the company or location.

Job offer text:
---
{text}
---

Return JSON for this schema:
{schema}
"""


def build_analyze_job_prompt(
    *,
    title: str,
    company: str,
    location: str | None,
    remote: bool,
    salary: str | None,
    description: str,
    schema_json: str,
) -> str:
    return ANALYZE_JOB_PROMPT.format(
        anti_invention=ANTI_INVENTION_RULES,
        title=title,
        company=company,
        location=location or "unspecified",
        remote=remote,
        salary=salary or "unspecified",
        description=description,
        schema=schema_json,
    )


def build_score_job_prompt(
    *,
    profile_json: str,
    achievement_ids: list[str],
    analysis_json: str,
    weights: ScoringWeights,
    schema_json: str,
) -> str:
    return SCORE_JOB_PROMPT.format(
        anti_invention=ANTI_INVENTION_RULES,
        w_required_stack=weights.required_stack,
        w_seniority_and_years=weights.seniority_and_years,
        w_modality_and_timezone=weights.modality_and_timezone,
        w_language=weights.language,
        w_salary_and_conditions=weights.salary_and_conditions,
        w_other=weights.other,
        profile=profile_json,
        achievement_ids=", ".join(sorted(achievement_ids)) or "(none)",
        analysis=analysis_json,
        schema=schema_json,
    )


def build_confirm_language_prompt(*, text: str, schema_json: str) -> str:
    return CONFIRM_LANGUAGE_PROMPT.format(
        anti_invention=ANTI_INVENTION_RULES,
        text=text,
        schema=schema_json,
    )


# --------------------------------------------------------------------------- #
# Phase 4 — document content selection
# --------------------------------------------------------------------------- #

SELECT_CONTENT_PROMPT = """\
You are tailoring a candidate's CV for a single job offer by SELECTING which of the
candidate's existing content to include and in what order. You never write new
content: you only pick ids that already exist in the profile.

{anti_invention}

Instructions:
- "summary_variant": choose exactly one key from the profile's summary_variants
  (listed under "Summary variant keys"). Never invent a key.
- "ordered_achievement_ids": pick the most relevant achievement ids for THIS offer,
  most relevant first. Use ONLY ids from "Profile achievement ids". Never invent an
  id and never include one that is not listed.
- "project_ids": pick the relevant project ids, or leave empty. Use ONLY ids from
  "Profile project ids".
- "highlighted_skill_ids": pick the skills to highlight using ONLY skill names that
  appear in "Profile skills"; leave empty if unsure.
- "language": set it to "{language}" (the language the documents will be written
  in). Do not choose a different language.
- Prefer content whose claims the offer actually values; do NOT include content
  just to fill space.
{adjustment}
Candidate profile:
---
{profile}
---
Summary variant keys (choose one): {summary_variants}
Profile achievement ids (use ONLY these): {achievement_ids}
Profile project ids (use ONLY these): {project_ids}
Profile skills (use ONLY these): {skills}
---
Analysed offer:
---
{analysis}
---

Return JSON for this schema:
{schema}
"""

_ADJUSTMENT_INSTRUCTION = """\
- The owner asked for an adjustment to the previous selection. Honour this
  instruction by RE-SELECTING which existing ids to include and in what order; it
  changes only your choice of ids, never the wording of any content, and the
  instruction text must NOT appear in the output: "{instruction}"
"""


def build_select_content_prompt(
    *,
    profile_json: str,
    summary_variants: list[str],
    achievement_ids: list[str],
    project_ids: list[str],
    skills: list[str],
    analysis_json: str,
    language: str,
    schema_json: str,
    instruction: str | None = None,
) -> str:
    adjustment = (
        _ADJUSTMENT_INSTRUCTION.format(instruction=instruction.strip())
        if instruction and instruction.strip()
        else ""
    )
    return SELECT_CONTENT_PROMPT.format(
        anti_invention=ANTI_INVENTION_RULES,
        adjustment=adjustment,
        profile=profile_json,
        summary_variants=", ".join(sorted(summary_variants)) or "(none)",
        achievement_ids=", ".join(sorted(achievement_ids)) or "(none)",
        project_ids=", ".join(sorted(project_ids)) or "(none)",
        skills=", ".join(sorted(skills)) or "(none)",
        analysis=analysis_json,
        language=language,
        schema=schema_json,
    )
