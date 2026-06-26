from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

Outcome = Literal[
    "earnings_beat",
    "fda_approval",
    "military_escalation",
    "ceasefire_or_deescalation",
    "oil_supply_disruption",
    "rate_cut",
    "rate_hike",
    "inflation_above_expectation",
    "inflation_below_expectation",
]
AssetRole = Literal[
    "ambiguous",
    "direct_company",
    "defense_beneficiary",
    "energy_beneficiary",
    "defense_exposure",
    "duration_asset",
    "inflation_exposure",
]

SEMANTIC_GROUPING_VERSION = "semantic-outcome-role-v1"
LEGACY_GROUPING_VERSION = "legacy-symbol-archetype-v1"

PROPOSED_GROUPS = frozenset(
    {
        "earnings_beat+direct_company",
        "fda_approval+direct_company",
        "military_escalation+defense_beneficiary",
        "military_escalation+energy_beneficiary",
        "ceasefire_or_deescalation+defense_exposure",
        "oil_supply_disruption+energy_beneficiary",
        "rate_cut+duration_asset",
        "rate_hike+duration_asset",
        "inflation_above_expectation+inflation_exposure",
        "inflation_below_expectation+inflation_exposure",
    }
)

LONG_ELIGIBLE_GROUPS = frozenset(
    {
        "earnings_beat+direct_company",
        "fda_approval+direct_company",
        "military_escalation+defense_beneficiary",
        "military_escalation+energy_beneficiary",
        "oil_supply_disruption+energy_beneficiary",
        "rate_cut+duration_asset",
        "inflation_above_expectation+inflation_exposure",
    }
)
POSITIVE_LONG_OUTCOMES = frozenset(group.split("+", 1)[0] for group in LONG_ELIGIBLE_GROUPS)
NEGATIVE_LONG_OUTCOMES = frozenset(
    {
        "ceasefire_or_deescalation",
        "rate_hike",
        "inflation_below_expectation",
    }
)

DEFENSE_SYMBOLS = frozenset(
    {
        "AVAV",
        "BA",
        "BWXT",
        "CACI",
        "GD",
        "HII",
        "ITA",
        "KTOS",
        "LDOS",
        "LHX",
        "LMT",
        "NOC",
        "PLTR",
        "PPA",
        "RTX",
        "SAIC",
        "SHLD",
        "XAR",
    }
)
ENERGY_SYMBOLS = frozenset(
    {
        "BNO",
        "BP",
        "COP",
        "CVX",
        "EOG",
        "FANG",
        "HAL",
        "IEO",
        "IYE",
        "MPC",
        "OIH",
        "OXY",
        "PSX",
        "SHEL",
        "SLB",
        "UCO",
        "USO",
        "VDE",
        "VLO",
        "XLE",
        "XOM",
        "XOP",
    }
)
DURATION_SYMBOLS = frozenset(
    {
        "AGG",
        "BND",
        "EDV",
        "GOVT",
        "IEF",
        "IEI",
        "SCHQ",
        "SPTL",
        "TLT",
        "VGIT",
        "VGLT",
        "ZROZ",
    }
)
INFLATION_EXPOSURE_SYMBOLS = frozenset(
    {
        "BNO",
        "COMT",
        "COP",
        "CVX",
        "DBC",
        "EOG",
        "FANG",
        "GDX",
        "GDXJ",
        "GLD",
        "IAU",
        "IEO",
        "IYE",
        "OXY",
        "PDBC",
        "TIP",
        "USO",
        "VDE",
        "VTIP",
        "XLE",
        "XOM",
        "XOP",
    }
)

COMPANY_STOP_WORDS = frozenset(
    {
        "class",
        "common",
        "company",
        "corp",
        "corporation",
        "group",
        "holdings",
        "inc",
        "incorporated",
        "limited",
        "ltd",
        "ordinary",
        "shares",
        "stock",
        "therapeutics",
    }
)
ENERGY_CHANNEL_TERMS = (
    "energy",
    "hormuz",
    "iran",
    "iraq",
    "israel",
    "middle east",
    "oil",
    "opec",
    "petroleum",
    "red sea",
    "refinery",
    "saudi",
    "shipping",
)


@dataclass(frozen=True)
class QuestionClassification:
    outcome: Outcome | None
    confidence: Literal["high", "medium", "low"]
    reason: str


@dataclass(frozen=True)
class SemanticAssignment:
    outcome: Outcome | None
    asset_role: AssetRole
    group: str | None
    yes_outcome_polarity: Literal["positive", "negative", "ambiguous"]
    long_eligible: bool
    confidence: Literal["high", "medium", "low"]
    reason: str


def _text(question: str, event_title: str = "", tags: Iterable[str] = ()) -> str:
    return " ".join([event_title, question, *tags]).lower().replace("’", "'")


def _has_any(text: str, values: Iterable[str]) -> bool:
    return any(value in text for value in values)


def classify_question(
    question: str,
    *,
    event_title: str = "",
    tags: Iterable[str] = (),
) -> QuestionClassification:
    text = _text(question, event_title, tags)

    if (
        ("earnings" in text or "quarterly eps estimate" in text)
        and _has_any(text, (" beat ", " beat?", "exceed"))
        and not _has_any(text, ("not beat", "miss earnings", "miss its"))
    ):
        return QuestionClassification("earnings_beat", "high", "Explicit earnings-beat Yes outcome")

    if "fda" in text and _has_any(text, ("approve", "approval", "approves", "approved")):
        if _has_any(text, ("reject", "rejection", "not approve")):
            return QuestionClassification(None, "low", "FDA wording has conflicting polarity")
        return QuestionClassification("fda_approval", "high", "Explicit FDA-approval Yes outcome")

    if _has_any(text, ("interest rate", "interest rates", "fed ", "federal reserve")):
        if _has_any(text, ("no change", "unchanged", "not cut", "not decrease", "not lower")):
            return QuestionClassification(None, "low", "Rate question does not have a directional Yes outcome")
        if _has_any(text, ("cut rate", "cut interest", "rate cut", "decrease interest", "lower interest", "reduce interest")):
            return QuestionClassification("rate_cut", "high", "Explicit rate-cut Yes outcome")
        if _has_any(text, ("rate hike", "raise interest", "increase interest", "hike interest")):
            return QuestionClassification("rate_hike", "high", "Explicit rate-hike Yes outcome")

    if _has_any(text, ("cpi", "inflation")):
        if _has_any(text, ("between ", "range ", "exactly ")):
            return QuestionClassification(None, "low", "Inflation range has ambiguous directional polarity")
        if _has_any(text, ("above ", "higher than", "more than", "over ", ">")):
            return QuestionClassification(
                "inflation_above_expectation",
                "high",
                "Explicit above-threshold inflation Yes outcome",
            )
        if _has_any(text, ("below ", "lower than", "less than", "under ", "<")):
            return QuestionClassification(
                "inflation_below_expectation",
                "high",
                "Explicit below-threshold inflation Yes outcome",
            )

    if _has_any(text, ("ceasefire", "peace deal", "peace agreement", "end the war", "war end", "withdraw troops", "withdrawal")):
        if _has_any(text, ("break ceasefire", "violate ceasefire", "ceasefire collapse")):
            return QuestionClassification(None, "low", "Ceasefire wording implies renewed escalation")
        return QuestionClassification(
            "ceasefire_or_deescalation",
            "high",
            "Explicit ceasefire or de-escalation Yes outcome",
        )

    oil_context = _has_any(text, ("oil", "hormuz", "refinery", "opec", "petroleum"))
    oil_disruption = _has_any(
        text,
        (
            "blockade",
            "blocked",
            "close the strait",
            "close strait",
            "cut production",
            "disrupt",
            "embargo",
            "taken out",
            "take out",
            "shut down",
            "strike refinery",
        ),
    )
    if oil_context and oil_disruption:
        return QuestionClassification(
            "oil_supply_disruption",
            "high",
            "Explicit oil-supply disruption Yes outcome",
        )

    if _has_any(
        text,
        (
            " attack ",
            " attack?",
            "attacks ",
            "bomb ",
            "bombing",
            "declare war",
            "declaration of war",
            "invade",
            "invasion",
            "military action",
            "military strike",
            "strike ",
            "strikes ",
            "troops enter",
        ),
    ):
        if _has_any(text, ("not attack", "not invade", "avoid war", "without attacking")):
            return QuestionClassification(None, "low", "Military question has negative or ambiguous polarity")
        return QuestionClassification(
            "military_escalation",
            "high",
            "Explicit military-escalation Yes outcome",
        )

    return QuestionClassification(None, "low", "No conservative proposed outcome match")


def _direct_company(
    question: str,
    *,
    symbol: str,
    asset_name: str,
    event_title: str = "",
) -> bool:
    upper_symbol = symbol.upper()
    combined = f"{event_title} {question}"
    ticker_mentions = re.findall(r"[$(]([A-Z][A-Z0-9.\-]{0,9})\)?", combined.upper())
    if upper_symbol in ticker_mentions:
        return True
    normalized = _text(question, event_title)
    name_tokens = [
        token
        for token in re.findall(r"[a-z0-9]+", asset_name.lower())
        if len(token) >= 4 and token not in COMPANY_STOP_WORDS
    ]
    return bool(name_tokens) and name_tokens[0] in normalized


def classify_assignment(
    question: str,
    *,
    symbol: str,
    asset_name: str = "",
    event_title: str = "",
    tags: Iterable[str] = (),
) -> SemanticAssignment:
    classified = classify_question(question, event_title=event_title, tags=tags)
    outcome = classified.outcome
    role: AssetRole = "ambiguous"
    upper_symbol = symbol.upper()
    text = _text(question, event_title, tags)

    if outcome in {"earnings_beat", "fda_approval"} and _direct_company(
        question,
        symbol=upper_symbol,
        asset_name=asset_name,
        event_title=event_title,
    ):
        role = "direct_company"
    elif outcome == "military_escalation" and upper_symbol in DEFENSE_SYMBOLS:
        role = "defense_beneficiary"
    elif (
        outcome == "military_escalation"
        and upper_symbol in ENERGY_SYMBOLS
        and _has_any(text, ENERGY_CHANNEL_TERMS)
    ):
        role = "energy_beneficiary"
    elif outcome == "ceasefire_or_deescalation" and upper_symbol in DEFENSE_SYMBOLS:
        role = "defense_exposure"
    elif outcome == "oil_supply_disruption" and upper_symbol in ENERGY_SYMBOLS:
        role = "energy_beneficiary"
    elif outcome in {"rate_cut", "rate_hike"} and upper_symbol in DURATION_SYMBOLS:
        role = "duration_asset"
    elif (
        outcome in {"inflation_above_expectation", "inflation_below_expectation"}
        and upper_symbol in INFLATION_EXPOSURE_SYMBOLS
    ):
        role = "inflation_exposure"

    group = f"{outcome}+{role}" if outcome and role != "ambiguous" else None
    if group not in PROPOSED_GROUPS:
        group = None
    if group in LONG_ELIGIBLE_GROUPS:
        polarity: Literal["positive", "negative", "ambiguous"] = "positive"
    elif group in PROPOSED_GROUPS:
        polarity = "negative"
    else:
        polarity = "ambiguous"
    confidence = classified.confidence if group else "low"
    reason = (
        f"{classified.reason}; assigned role={role}"
        if group
        else f"{classified.reason}; no conservative supported asset role"
    )
    return SemanticAssignment(
        outcome=outcome,
        asset_role=role,
        group=group,
        yes_outcome_polarity=polarity,
        long_eligible=group in LONG_ELIGIBLE_GROUPS,
        confidence=confidence,
        reason=reason,
    )


def semantic_ml_group(
    question: str,
    *,
    symbol: str,
    asset_name: str = "",
    event_title: str = "",
    tags: Iterable[str] = (),
) -> str | None:
    return classify_assignment(
        question,
        symbol=symbol,
        asset_name=asset_name,
        event_title=event_title,
        tags=tags,
    ).group


def group_allows_long(group: str | None) -> bool:
    return group in LONG_ELIGIBLE_GROUPS


def question_yes_outcome_polarity(
    outcome: Outcome | None,
) -> Literal["positive", "negative", "ambiguous"]:
    if outcome in POSITIVE_LONG_OUTCOMES:
        return "positive"
    if outcome in NEGATIVE_LONG_OUTCOMES:
        return "negative"
    return "ambiguous"


def question_allows_long_consideration(
    question: str,
    *,
    event_title: str = "",
    tags: Iterable[str] = (),
) -> bool:
    classified = classify_question(question, event_title=event_title, tags=tags)
    return question_yes_outcome_polarity(classified.outcome) == "positive"
