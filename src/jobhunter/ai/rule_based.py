"""Deterministic provider used when no API key is configured.

Guarantees the whole product works offline: scoring falls back to the rule
engine and cover letters are assembled from a template that can only mention
skills present in the profile.
"""

from __future__ import annotations

from jobhunter.ai.base import AIProvider
from jobhunter.domain.enums import Language
from jobhunter.domain.schemas import (
    CandidateSnapshot,
    ClassificationResult,
    MatchResult,
    NormalizedJob,
)
from jobhunter.matching.rules import ScoringConfig, expand_tech, score_job
from jobhunter.normalize.normalizer import detect_language

_TEMPLATES = {
    Language.EN: {
        "opening": "Dear Hiring Team,",
        "intro": "I am writing to apply for the {title} position{company}.",
        "background": "{summary}",
        "skills": "My hands-on experience covers {skills}.",
        "skills_relevant": "The role's focus on {matched} lines up directly with what I have been building.",
        "close": (
            "I would welcome the chance to discuss how I can contribute to your team. "
            "Thank you for your time and consideration."
        ),
        "sign": "Kind regards,\n{name}",
        "default_summary": "I am an early-career developer focused on building reliable, maintainable software",
    },
    Language.BG: {
        "opening": "Уважаеми колеги,",
        "intro": "Пиша Ви във връзка с обявата за позицията {title}{company}.",
        "background": "{summary}",
        "skills": "Практическият ми опит включва {skills}.",
        "skills_relevant": "Акцентът на позицията върху {matched} съвпада пряко с това, с което работя.",
        "close": (
            "Ще се радвам на възможността да обсъдим как мога да допринеса за екипа Ви. "
            "Благодаря Ви за отделеното време."
        ),
        "sign": "С уважение,\n{name}",
        "default_summary": "Аз съм начинаещ разработчик, фокусиран върху надеждни и поддържаеми решения",
    },
}


class RuleBasedProvider(AIProvider):
    name = "rule_based"

    def __init__(self, config: ScoringConfig | None = None) -> None:
        self.config = config or ScoringConfig()
        self.model = None

    def is_available(self) -> bool:
        return True

    def score_job(
        self,
        job: NormalizedJob,
        classification: ClassificationResult,
        candidate: CandidateSnapshot,
    ) -> MatchResult:
        return score_job(job, classification, candidate, self.config)

    def generate_cover_letter(
        self,
        job: NormalizedJob,
        candidate: CandidateSnapshot,
        *,
        language: Language = Language.EN,
        max_words: int = 180,
    ) -> str:
        """Assemble a letter from profile facts only — nothing is invented."""
        template = _TEMPLATES.get(language, _TEMPLATES[Language.EN])

        have = expand_tech(candidate.all_tech)
        job_tech = {t.lower() for t in job.tech_keywords}
        matched = sorted({t for t in job_tech if t in have or expand_tech({t}) & have})

        top_skills = [s for s in (candidate.frameworks + candidate.skills) if s][:6]

        company = f" at {job.company_name}" if job.company_name else ""
        if language is Language.BG and job.company_name:
            company = f" в {job.company_name}"

        parts = [
            template["opening"],
            "",
            template["intro"].format(title=job.title, company=company),
        ]

        # Only reuse the profile summary when it is written in the same language
        # as the letter; an English sentence inside a Bulgarian letter reads badly.
        summary = (candidate.summary or "").strip()
        summary_matches_language = bool(summary) and detect_language(summary) is language
        if summary_matches_language:
            first_sentence = summary.split(". ")[0].strip().rstrip(".")
            if 20 < len(first_sentence) < 300:
                parts.append(template["background"].format(summary=first_sentence + "."))
            else:
                parts.append(
                    template["background"].format(summary=template["default_summary"] + ".")
                )
        else:
            parts.append(template["background"].format(summary=template["default_summary"] + "."))

        if top_skills:
            parts.append(template["skills"].format(skills=_join(top_skills, language)))
        if matched:
            parts.append(template["skills_relevant"].format(matched=_join(matched[:5], language)))

        parts += [
            "",
            template["close"],
            "",
            template["sign"].format(name=candidate.full_name or ""),
        ]

        letter = "\n".join(parts).strip()
        return _truncate_words(letter, max_words)


# Canonical display casing for technologies that .title() would mangle.
DISPLAY_NAMES = {
    "php": "PHP",
    "sql": "SQL",
    "mysql": "MySQL",
    "postgresql": "PostgreSQL",
    "javascript": "JavaScript",
    "typescript": "TypeScript",
    "html": "HTML",
    "css": "CSS",
    "html5": "HTML5",
    "css3": "CSS3",
    "api": "API",
    "rest": "REST",
    "restful": "RESTful",
    "graphql": "GraphQL",
    "json": "JSON",
    "aws": "AWS",
    "gcp": "GCP",
    "ci/cd": "CI/CD",
    "qa": "QA",
    "etl": "ETL",
    "node.js": "Node.js",
    "vue.js": "Vue.js",
    "react": "React",
    "vue": "Vue",
    "alpine.js": "Alpine.js",
    "express.js": "Express.js",
    "next.js": "Next.js",
    "tailwind": "Tailwind CSS",
    "tailwindcss": "Tailwind CSS",
    "c#": "C#",
    "c++": "C++",
    ".net": ".NET",
    "dotnet": ".NET",
    "github": "GitHub",
    "gitlab": "GitLab",
    "mssql": "MSSQL",
    "devops": "DevOps",
    "ios": "iOS",
}


def display_tech(name: str) -> str:
    """Human-facing casing for a technology token."""
    lowered = name.strip().lower()
    if lowered in DISPLAY_NAMES:
        return DISPLAY_NAMES[lowered]
    if any(c.isupper() for c in name):
        return name
    return name.title()


def _join(items: list[str], language: Language) -> str:
    pretty = [display_tech(i) for i in items]
    if len(pretty) == 1:
        return pretty[0]
    conjunction = " и " if language is Language.BG else " and "
    return ", ".join(pretty[:-1]) + conjunction + pretty[-1]


def _truncate_words(text: str, max_words: int) -> str:
    """Trim to a word budget without cutting mid-sentence where avoidable."""
    words = text.split()
    if len(words) <= max_words:
        return text
    clipped = " ".join(words[:max_words])
    for terminator in (".", "!", "?"):
        idx = clipped.rfind(terminator)
        if idx > len(clipped) * 0.6:
            return clipped[: idx + 1]
    return clipped + "..."
