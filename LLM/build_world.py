from __future__ import annotations

from datetime import datetime
import re
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from main_backtesting.models import Asset, IBTradableAsset, SourceMarket


class AssetCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    symbol: str = Field(min_length=1, max_length=20)
    asset_name: str = Field(min_length=1, max_length=260)
    asset_class: Literal["stock", "etf"]
    relationship_type: Literal[
        "direct_company",
        "customer",
        "supplier",
        "distributor",
        "partner",
        "competitor",
        "substitute",
        "complement",
        "creditor",
        "investor",
        "landlord_tenant",
        "sector_etf",
        "country_etf",
        "commodity_proxy",
        "other_specific",
    ]
    reason: str = Field(
        min_length=20,
        max_length=500,
        description=(
            "Specific causal economic relationship to the exact question. Generic claims "
            "that an asset may be affected are insufficient."
        ),
    )
    connection_strength: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Mechanical exposure strength in [0, 1]; legacy one-pass worlds default to 1.0.",
    )

    @field_validator("symbol")
    @classmethod
    def uppercase_symbol(cls, value: str) -> str:
        return value.upper()


class AssetWorld(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    universe_name: str = Field(min_length=1, max_length=200)
    universe_reason: str = Field(min_length=20, max_length=700)
    assets: list[AssetCandidate] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def require_unique_symbols(self) -> AssetWorld:
        symbols = [
            asset.symbol.strip().upper().replace(".", " ").replace("$", " ")
            for asset in self.assets
        ]
        if len(symbols) != len(set(symbols)):
            raise ValueError("Asset world contains duplicate symbols")
        return self


class BatchedAssetWorld(AssetWorld):
    request_id: str
    # Pass-1 score for the market question itself. Multiplied by each asset's connection_strength
    # to form the final relevance. Defaults to 1.0 for legacy one-pass worlds (unscored).
    question_relevance: float = 1.0


class CompactAssetWorld(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    request_id: str
    symbols: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("symbols")
    @classmethod
    def normalize_symbols(cls, values: list[str]) -> list[str]:
        return [value.upper() for value in values]


class CompactAssetWorlds(BaseModel):
    model_config = ConfigDict(extra="forbid")
    worlds: list[CompactAssetWorld]


# Below this question-relevance score the YES outcome has no real mechanical channel to US
# equities; we skip the (expensive) stock-mapping pass and emit an empty world.
QUESTION_RELEVANCE_FLOOR = 0.10


class RelevanceDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    request_id: str
    question_relevance: float = Field(
        ge=0.0,
        le=1.0,
        description="How mechanically a YES outcome reprices US-listed equities, in [0, 1].",
    )
    reason: str = Field(min_length=20, max_length=500)


class RelevanceGateBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decisions: list[RelevanceDecision]


class TightAssetWorld(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    request_id: str
    universe_name: str = Field(min_length=1, max_length=200)
    universe_reason: str = Field(min_length=20, max_length=700)
    assets: list[AssetCandidate] = Field(default_factory=list, max_length=20)


class TightAssetWorlds(BaseModel):
    model_config = ConfigDict(extra="forbid")
    worlds: list[TightAssetWorld]


GEMINI_RELEVANCE_GATE_PROMPT = """
For each prediction-market question, rate question_relevance in [0, 1]: how mechanically a YES
outcome would move US-listed equities or ETFs. Judge the QUESTION itself, not any single stock.

Weigh four things:
1. Directness -- does YES hit a US company's cash flow/value directly (a US name's earnings,
   FDA, M&A), a US-traded commodity or rate (oil, Treasuries), or only a foreign/indirect channel?
2. Breadth x magnitude -- does it move one stock, a sector, or the whole US market?
3. Surprise -- is YES a genuine market-moving surprise, or a low bar already priced in?
4. US proximity -- US government / Federal Reserve action > a major US ally's action > a distant
   regional actor with no US transmission. Use your world knowledge of whether this TYPE of event
   has historically repriced US stocks.

Calibration anchors (generalize the principle; do not pattern-match the exact wording):
  ~1.0  a US company's own earnings / FDA / M&A; a Fed rate decision or emergency/surprise action
  ~0.8  direct US military action; a major OPEC / Strait-of-Hormuz oil-supply shock; a US tariff on a named sector
  ~0.5  an ally or regional conflict with a real oil-supply channel but no US actor
  ~0.3  a routine macro print near consensus (e.g. CPI a few tenths from expectations); a modest, expected policy move
  ~0.2  foreign/regional events US markets routinely shrug off (e.g. recurring Houthi strikes on Israel)
  ~0.0  no mechanical US-equity channel at all: speech-word counts, celebrity, sports, pure narrative

Do not choose assets in this pass. Do not predict the outcome or trading direction. Return only
request_id, question_relevance, and a one-line reason that names the channel (or its absence).
""".strip()


GEMINI_TIGHT_MAPPING_PROMPT = """
Build the tightest possible US-listed equity/ETF asset world for every supplied request.

Selection rules:
- If no liquid US equity/ETF mechanically reprices on YES, return an empty assets list.
- Earnings / merger / named-company: return ONLY the single named US company. Never add
  competitors, suppliers, customers, or sector peers -- they do not mechanically reprice on THIS
  company's result.
- FDA / drug-approval / PDUFA: the single named company, but ONLY when the drug is material to
  its value -- a small/mid-cap biotech where the drug IS the company (connection_strength ~1.0).
  For a diversified large-cap pharma (e.g. Sanofi, Pfizer, J&J) a single approval barely moves the
  stock: give connection_strength <= 0.3 or return empty.
- Geopolitical / military / supply-shock: map the disrupted COMMODITY through its direct US fund.
  For oil-supply threats use the crude OIL funds (USO, BNO, UCO) -- do NOT use energy-equity ETFs
  (XLE, XOP) or oil majors (XOM, CVX): on geopolitical risk energy equities sell off with the
  market while crude itself rises. Only when the actor/region mechanically affects supply (OPEC,
  Strait of Hormuz, a major producer). Do NOT add defense names (LMT, RTX, ITA) on conflict
  sentiment -- include defense only if the outcome changes US procurement or budgets.
- Macro / rates / inflation: return rate-sensitive US equities across BOTH ends of the rate
  channel -- rate-level names (financials XLF/KRE, homebuilders ITB, REITs) AND duration /
  borrowing-sensitive names (long-duration tech and growth via QQQ, and heavy borrowers such as
  TSLA: cheaper money lifts them, dearer money sinks them) -- plus the rate instrument (TLT).
  Let the downstream model's debt features sort the leverage cross-section. Never fall back to
  SPY or "the whole market".
- Tariffs / sanctions / policy: return the specifically named exposed importers, exporters, or sector ETF.

connection_strength in [0, 1]: 1.0 = the asset IS the subject or the direct instrument of the
outcome; 0.6-0.9 = a strong, specific mechanical channel; 0.3-0.5 = real but indirect. Exclude
anything tied only by narrative or risk-off sentiment. Keep worlds small -- prefer 1-6 names.

This is asset selection only. Do not predict Yes/No, direction, sizing, or expected return.
Return request_id, universe_name, universe_reason, and assets using the schema.
""".strip()


# Backward-compatible import name used by older stage metadata. The implementation below
# is now two-pass: relevance gate, then tight mapping.
GEMINI_ONE_CALL_PROMPT = GEMINI_TIGHT_MAPPING_PROMPT


CATALOG_TOKEN_RE = re.compile(r"[A-Z0-9]+")
CATALOG_STOP_WORDS = {
    "A",
    "AN",
    "AND",
    "ARE",
    "CLASS",
    "CO",
    "COMMON",
    "COMPANY",
    "CORP",
    "CORPORATION",
    "ETF",
    "FUND",
    "HOLDINGS",
    "INC",
    "INCORPORATED",
    "LTD",
    "OF",
    "ORDINARY",
    "SHARE",
    "SHARES",
    "STOCK",
    "THE",
}


class IBAssetCatalogIndex:
    def __init__(self, assets: list[IBTradableAsset]) -> None:
        self.assets = tuple(assets)
        self.by_symbol = {ib_symbol_key(asset.symbol): asset for asset in assets}
        self.name_text: dict[str, str] = {}
        self.name_tokens: dict[str, set[str]] = {}
        self.metadata_tokens: dict[str, set[str]] = {}
        self.name_token_symbols: dict[str, set[str]] = {}
        self.metadata_token_symbols: dict[str, set[str]] = {}
        for asset in assets:
            key = ib_symbol_key(asset.symbol)
            self.name_text[key] = normalized_catalog_text(asset.asset_name)
            name_tokens = catalog_tokens(asset.symbol, asset.asset_name)
            metadata_tokens = catalog_tokens(
                asset.industry,
                asset.category,
                asset.subcategory,
            )
            self.name_tokens[key] = name_tokens
            self.metadata_tokens[key] = metadata_tokens
            for token in name_tokens:
                self.name_token_symbols.setdefault(token, set()).add(key)
            for token in metadata_tokens:
                self.metadata_token_symbols.setdefault(token, set()).add(key)


def ib_asset_catalog_index(
    assets: list[IBTradableAsset] | IBAssetCatalogIndex,
) -> IBAssetCatalogIndex:
    if isinstance(assets, IBAssetCatalogIndex):
        return assets
    return IBAssetCatalogIndex(assets)


def gemini_world_payload(
    requests: list[tuple[str, SourceMarket, datetime]],
) -> dict[str, list[dict[str, object]]]:
    return {
        "requests": [
            {
                "request_id": request_id,
                "event_title": market.event_title,
                "market_question": market.question,
                "tags": market.tags,
                "market_created_at": market.created_at,
                "market_end_at": market.end_at,
                "historical_as_of": as_of,
            }
            for request_id, market, as_of in requests
        ]
    }


def canonicalize_compact_gemini_world(
    compact_world: CompactAssetWorld,
    market: SourceMarket,
    catalog: IBAssetCatalogIndex,
) -> BatchedAssetWorld:
    selected: list[tuple[IBTradableAsset, str]] = []
    seen: set[str] = set()
    for symbol in compact_world.symbols:
        key = ib_symbol_key(symbol)
        asset = catalog.by_symbol.get(key)
        if asset is None or key in seen:
            continue
        seen.add(key)
        selected.append((asset, "gemini"))
    if len(selected) < 4:
        # No fallback. Gemini must return at least four IB-tradable symbols; if it
        # does not, crash loudly instead of padding with hardcoded local symbols.
        raise ValueError(
            "Gemini returned fewer than four IB-tradable symbols for request "
            f"{compact_world.request_id}: {list(compact_world.symbols)}"
        )
    assets = [
        AssetCandidate(
            symbol=asset.symbol,
            asset_name=asset.asset_name,
            asset_class=asset.asset_class,
            relationship_type="sector_etf" if asset.asset_class == "etf" else "other_specific",
            reason=(
                f"Gemini 3.5 Flash selected {asset.asset_name} as economically related "
                "to the supplied prediction-market question."
            ),
        )
        for asset, _source in selected[:20]
    ]
    universe_name = (market.event_title or market.question or "Gemini asset world")[:200]
    return BatchedAssetWorld(
        request_id=compact_world.request_id,
        universe_name=universe_name,
        universe_reason=(
            "Gemini 3.5 Flash selected this locally IB-verified cross-sectional "
            "research world."
        ),
        assets=assets,
    )


def _single_named_entity_market(market: SourceMarket) -> bool:
    text = " ".join(
        [
            market.event_title or "",
            market.question or "",
            " ".join(market.tags or []),
        ]
    ).lower()
    needles = (
        "earnings",
        "eps",
        "revenue",
        "fda",
        "pdufa",
        "drug approval",
        "approval",
        "merger",
        "acquisition",
        "takeover",
    )
    return any(needle in text for needle in needles)


def _reason(value: str | None, fallback: str) -> str:
    text = (value or "").strip()
    if len(text) >= 20:
        return text[:700]
    return fallback


def empty_world(
    request_id: str,
    market: SourceMarket,
    reason: str | None = None,
    *,
    question_relevance: float = 0.0,
) -> BatchedAssetWorld:
    return BatchedAssetWorld(
        request_id=request_id,
        universe_name=(market.event_title or market.question or "No liquid equity world")[:200],
        universe_reason=_reason(
            reason,
            "Relevance gate scored this question below the floor for mechanical US-equity repricing.",
        ),
        assets=[],
        question_relevance=question_relevance,
    )


def canonicalize_tight_gemini_world(
    tight_world: TightAssetWorld,
    market: SourceMarket,
    catalog: IBAssetCatalogIndex,
    *,
    question_relevance: float = 1.0,
) -> BatchedAssetWorld:
    selected: list[AssetCandidate] = []
    seen: set[str] = set()
    for item in tight_world.assets:
        key = ib_symbol_key(item.symbol)
        asset = catalog.by_symbol.get(key)
        if asset is None or key in seen:
            continue
        seen.add(key)
        selected.append(
            AssetCandidate(
                symbol=asset.symbol,
                asset_name=asset.asset_name,
                asset_class=asset.asset_class,
                relationship_type=item.relationship_type,
                reason=item.reason,
                connection_strength=item.connection_strength,
            )
        )
    if _single_named_entity_market(market) and len(selected) > 1:
        selected = selected[:1]
    if tight_world.assets and not selected:
        # The LLM named only assets we cannot trade on IB (e.g. a small-cap biotech not in the
        # catalog). Emit an empty world for this market rather than crashing the whole run --
        # we simply have no tradeable exposure here. No hardcoded fallback symbols.
        print(
            f"[world] no IB-tradable symbol for {tight_world.request_id}: "
            f"{[asset.symbol for asset in tight_world.assets]} -> empty world"
        )
        return empty_world(
            tight_world.request_id,
            market,
            f"Mapped names are not IB-tradable: {[asset.symbol for asset in tight_world.assets]}",
            question_relevance=question_relevance,
        )
    return BatchedAssetWorld(
        request_id=tight_world.request_id,
        universe_name=(tight_world.universe_name or market.event_title or market.question)[:200],
        universe_reason=_reason(
            tight_world.universe_reason,
            "Tight mapping selected assets with concrete mechanical exposure to the prediction-market YES outcome.",
        ),
        assets=selected,
        question_relevance=question_relevance,
    )


async def build_gemini_asset_worlds(
    gemini: object,
    requests: list[tuple[str, SourceMarket, datetime]],
    *,
    tradable_assets: list[IBTradableAsset] | IBAssetCatalogIndex,
) -> list[BatchedAssetWorld]:
    if not requests:
        return []
    gate_response = await gemini.structured(  # type: ignore[attr-defined]
        system_prompt=GEMINI_RELEVANCE_GATE_PROMPT,
        payload=gemini_world_payload(requests),
        response_model=RelevanceGateBatch,
        max_tokens=max(800, len(requests) * 80),
        prefer_prompt_schema=True,
    )
    catalog = ib_asset_catalog_index(tradable_assets)
    expected = {request_id for request_id, _, _ in requests}
    decisions = {
        decision.request_id: decision
        for decision in gate_response.decisions
        if decision.request_id in expected
    }
    missing_gate = expected - set(decisions)
    if missing_gate:
        raise ValueError(f"Gemini relevance gate omitted requests: {sorted(missing_gate)}")

    worlds: dict[str, BatchedAssetWorld] = {}
    relevant_requests: list[tuple[str, SourceMarket, datetime]] = []
    relevance_by_request: dict[str, float] = {}
    for request_id, market, _ in requests:
        decision = decisions[request_id]
        relevance_by_request[request_id] = decision.question_relevance
        if decision.question_relevance >= QUESTION_RELEVANCE_FLOOR:
            relevant_requests.append((request_id, market, _))
        else:
            worlds[request_id] = empty_world(
                request_id,
                market,
                decision.reason,
                question_relevance=decision.question_relevance,
            )

    if relevant_requests:
        mapping_response = await gemini.structured(  # type: ignore[attr-defined]
            system_prompt=GEMINI_TIGHT_MAPPING_PROMPT,
            payload=gemini_world_payload(relevant_requests),
            response_model=TightAssetWorlds,
            max_tokens=max(1200, len(relevant_requests) * 220),
            prefer_prompt_schema=True,
        )
        expected_mapping = {request_id for request_id, _, _ in relevant_requests}
        mapped_by_request = {
            world.request_id: world
            for world in mapping_response.worlds
            if world.request_id in expected_mapping
        }
        missing_mapping = expected_mapping - set(mapped_by_request)
        if missing_mapping:
            raise ValueError(f"Gemini tight mapping omitted requests: {sorted(missing_mapping)}")
        for request_id, market, _ in relevant_requests:
            worlds[request_id] = canonicalize_tight_gemini_world(
                mapped_by_request[request_id],
                market,
                catalog,
                question_relevance=relevance_by_request[request_id],
            )
    return [worlds[request_id] for request_id, _, _ in requests]


def ib_symbol_key(symbol: str) -> str:
    return symbol.strip().upper().replace(".", " ").replace("$", " ")


def normalized_catalog_text(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(CATALOG_TOKEN_RE.findall(value.upper()))


def catalog_tokens(*values: str | None) -> set[str]:
    return {
        token
        for value in values
        for token in CATALOG_TOKEN_RE.findall((value or "").upper())
        if len(token) > 1 and token not in CATALOG_STOP_WORDS
    }


def assets_from_world(world: AssetWorld) -> list[Asset]:
    # Persist the FINAL relevance = question_relevance (pass 1) x connection_strength (pass 2),
    # per Liran's design. Raw question_relevance is also kept in the world's llm_output JSON.
    question_relevance = getattr(world, "question_relevance", 1.0)
    return [
        Asset(
            symbol=item.symbol,
            asset_name=item.asset_name,
            asset_class=item.asset_class,
            reason=f"[{item.relationship_type}] {item.reason}",
            connection_strength=(
                item.connection_strength * question_relevance
                if item.connection_strength is not None
                else question_relevance
            ),
        )
        for item in world.assets
    ]


