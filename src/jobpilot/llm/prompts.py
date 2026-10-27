"""Prompt templates. Every prompt embeds the anti-invention rules."""

from __future__ import annotations

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
