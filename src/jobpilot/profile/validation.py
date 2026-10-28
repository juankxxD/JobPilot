"""Code-level validation of LLM output against the profile.

These helpers enforce the non-negotiable rules:

* Every id the LLM returns (achievements, projects) must exist in the profile.
* A text is only usable in a language if its translation is approved.
* Every technology mentioned in a cover letter or form answer must be present in
  the profile (skills or achievement/project tags).

They are shared by the document and form modules in later phases.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from jobpilot.config import Language
from jobpilot.profile.model import ProfileModel


class ProfileValidationError(ValueError):
    """Raised when LLM output references content not present in the profile."""


@dataclass(slots=True)
class ValidationIssues:
    """Collected problems; empty means valid."""

    unknown_achievement_ids: list[str] = field(default_factory=list)
    unknown_project_ids: list[str] = field(default_factory=list)
    unknown_skill_ids: list[str] = field(default_factory=list)
    unknown_summary_variant: str | None = None
    unapproved_language_ids: list[str] = field(default_factory=list)
    unknown_technologies: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (
            self.unknown_achievement_ids
            or self.unknown_project_ids
            or self.unknown_skill_ids
            or self.unknown_summary_variant
            or self.unapproved_language_ids
            or self.unknown_technologies
        )

    def as_message(self) -> str:
        parts: list[str] = []
        if self.unknown_achievement_ids:
            parts.append(f"unknown achievement ids: {self.unknown_achievement_ids}")
        if self.unknown_project_ids:
            parts.append(f"unknown project ids: {self.unknown_project_ids}")
        if self.unknown_skill_ids:
            parts.append(f"unknown skill ids: {self.unknown_skill_ids}")
        if self.unknown_summary_variant:
            parts.append(f"unknown summary variant: {self.unknown_summary_variant}")
        if self.unapproved_language_ids:
            parts.append(f"unapproved translations for: {self.unapproved_language_ids}")
        if self.unknown_technologies:
            parts.append(f"technologies not in profile: {self.unknown_technologies}")
        return "; ".join(parts)


def validate_selection(
    profile: ProfileModel,
    *,
    summary_variant: str,
    achievement_ids: list[str],
    project_ids: list[str],
    language: Language,
    highlighted_skill_ids: list[str] | None = None,
) -> ValidationIssues:
    """Validate an LLM content selection against the profile.

    Checks id existence (achievements, projects, highlighted skills), summary-variant
    existence, and that each referenced text has an approved translation in the target
    language. Highlighted skills are matched case-insensitively against the profile's
    declared skills; any that are not present are reported so the caller can retry/error
    instead of silently dropping them.
    """
    issues = ValidationIssues()

    known_ach = profile.achievement_ids()
    known_prj = profile.project_ids()
    known_skills = {s.lower() for s in profile.skills.all()}

    issues.unknown_achievement_ids = [i for i in achievement_ids if i not in known_ach]
    issues.unknown_project_ids = [i for i in project_ids if i not in known_prj]
    issues.unknown_skill_ids = [
        s for s in (highlighted_skill_ids or []) if s.lower() not in known_skills
    ]

    if summary_variant not in profile.summary_variants:
        issues.unknown_summary_variant = summary_variant

    for ach_id in achievement_ids:
        ach = profile.achievement_by_id(ach_id)
        if ach is not None and ach.approved_text(language) is None:
            issues.unapproved_language_ids.append(ach_id)
    for prj_id in project_ids:
        prj = profile.project_by_id(prj_id)
        if prj is not None and prj.approved_text(language) is None:
            issues.unapproved_language_ids.append(prj_id)

    return issues


def _profile_technologies(profile: ProfileModel) -> set[str]:
    techs: set[str] = {s.lower() for s in profile.skills.all()}
    for exp in profile.experience:
        for ach in exp.achievements:
            techs.update(t.lower() for t in ach.tags)
    for prj in profile.projects:
        techs.update(t.lower() for t in prj.tags)
    return techs


def find_unknown_technologies(profile: ProfileModel, technologies: list[str]) -> list[str]:
    """Return technologies not present in the profile (skills or tags)."""
    known = _profile_technologies(profile)
    return [t for t in technologies if t.lower() not in known]


def extract_mentioned_technologies(profile: ProfileModel, text: str) -> list[str]:
    """Return profile technologies that appear as whole words in ``text``.

    Used to confirm a generated cover letter only claims technologies the profile
    actually contains (it cannot invent new ones; it can only use known ones).
    """
    mentioned: list[str] = []
    for tech in sorted(_profile_technologies(profile)):
        pattern = r"\b" + re.escape(tech) + r"\b"
        if re.search(pattern, text.lower()):
            mentioned.append(tech)
    return mentioned


# A curated catalogue of common technology names. It lets the cover-letter
# validator detect a technology *claimed in free text that is NOT in the profile*
# (an invention), rather than only re-confirming technologies already in the
# profile. This is deliberately broad; missing entries only mean a fabricated name
# is not caught here, never that a legitimate profile technology is rejected.
_TECHNOLOGY_CATALOGUE: frozenset[str] = frozenset(
    {
        # languages
        "java",
        "python",
        "javascript",
        "typescript",
        "go",
        "golang",
        "rust",
        "c",
        "c++",
        "c#",
        "ruby",
        "php",
        "kotlin",
        "scala",
        "swift",
        "elixir",
        "perl",
        "r",
        "dart",
        "haskell",
        "clojure",
        "erlang",
        "objective-c",
        "cobol",
        "fortran",
        "lua",
        "julia",
        "sql",
        "bash",
        "powershell",
        # frameworks / libraries
        "spring",
        "spring boot",
        "django",
        "flask",
        "fastapi",
        "rails",
        "react",
        "angular",
        "vue",
        "svelte",
        "next.js",
        "nuxt",
        "express",
        "nestjs",
        "laravel",
        "symfony",
        "dotnet",
        ".net",
        "quarkus",
        "micronaut",
        "node",
        "node.js",
        "deno",
        "bun",
        "jquery",
        "ember",
        "backbone",
        "pytorch",
        "tensorflow",
        "keras",
        "scikit-learn",
        "pandas",
        "numpy",
        "spark",
        "hadoop",
        "kafka",
        "rabbitmq",
        "celery",
        "graphql",
        "grpc",
        # cloud / infra
        "aws",
        "azure",
        "gcp",
        "google cloud",
        "docker",
        "kubernetes",
        "k8s",
        "terraform",
        "ansible",
        "pulumi",
        "helm",
        "jenkins",
        "gitlab ci",
        "github actions",
        "circleci",
        "nginx",
        "apache",
        "cloudflare",
        "heroku",
        "lambda",
        "ec2",
        "s3",
        "ecs",
        "eks",
        "fargate",
        "dynamodb",
        "cloudformation",
        "serverless",
        "istio",
        "prometheus",
        "grafana",
        "datadog",
        # databases
        "postgresql",
        "postgres",
        "mysql",
        "mariadb",
        "mongodb",
        "redis",
        "cassandra",
        "elasticsearch",
        "sqlite",
        "oracle",
        "sql server",
        "cockroachdb",
        "neo4j",
        "couchbase",
        "influxdb",
        "snowflake",
    }
)


def find_invented_technologies(profile: ProfileModel, text: str) -> list[str]:
    """Return catalogue technologies mentioned in ``text`` but absent from the profile.

    Unlike :func:`extract_mentioned_technologies` (which only finds profile
    technologies and therefore can never surface an invention), this scans the text
    for *any* known technology name and flags those the profile does not contain.
    This is what enforces anti-invention on a generated cover letter: a claim of a
    technology the candidate has never listed is rejected.
    """
    known = _profile_technologies(profile)
    lowered = text.lower()
    invented: list[str] = []
    # Check longer names first so "spring boot" is matched before "spring", etc.
    for tech in sorted(_TECHNOLOGY_CATALOGUE, key=len, reverse=True):
        if tech in known:
            continue
        pattern = r"(?<![\w+#.])" + re.escape(tech) + r"(?![\w+#])"
        if re.search(pattern, lowered) and tech not in invented:
            invented.append(tech)
    return invented
