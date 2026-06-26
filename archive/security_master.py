from __future__ import annotations

import asyncio
import csv
import io
import re
from dataclasses import dataclass
from typing import Iterable

import httpx

NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"

EXCHANGE_NAMES = {
    "A": "NYSE American",
    "N": "NYSE",
    "P": "NYSE Arca",
    "Z": "Cboe BZX",
    "V": "IEX",
}

SECURITY_SUFFIXES = (
    "COMMON STOCK",
    "CLASS A COMMON STOCK",
    "CLASS B COMMON STOCK",
    "CLASS C COMMON STOCK",
    "ORDINARY SHARES",
    "ORDINARY SHARE",
    "AMERICAN DEPOSITARY SHARES",
    "AMERICAN DEPOSITARY SHARE",
    "AMERICAN DEPOSITARY RECEIPTS",
    "AMERICAN DEPOSITARY RECEIPT",
    "DEPOSITARY SHARES",
    "DEPOSITARY SHARE",
    "ADS",
    "ADR",
)

COMPANY_SUFFIXES = (
    "INCORPORATED",
    "CORPORATION",
    "COMPANY",
    "LIMITED",
    "HOLDINGS",
    "HOLDING",
    "INC",
    "CORP",
    "LTD",
    "PLC",
    "LP",
)

HISTORICAL_SYMBOL_ALIASES = {
    "FB": ("META", {"FACEBOOK", "META PLATFORMS"}),
    "PCLN": ("BKNG", {"PRICELINE", "BOOKING HOLDINGS"}),
    "FISV": ("FI", {"FISERV"}),
}


@dataclass(frozen=True)
class SecurityMasterEntry:
    official_symbol: str
    yfinance_symbol: str
    security_name: str
    exchange: str
    is_etf: bool
    source: str


@dataclass(frozen=True)
class SecurityResolution:
    original_symbol: str
    entry: SecurityMasterEntry | None
    match_method: str | None
    rejection_reason: str | None


def parse_pipe_file(text: str) -> list[dict[str, str]]:
    rows = list(csv.DictReader(io.StringIO(text), delimiter="|"))
    return [
        row
        for row in rows
        if row
        and not any((value or "").startswith("File Creation Time") for value in row.values())
    ]


def yfinance_symbol(official_symbol: str) -> str:
    return (
        official_symbol.upper()
        .replace("^", "-P")
        .replace("$", "-P")
        .replace(".", "-")
        .replace("/", "-")
        .replace(" ", "-")
    )


def normalize_name(value: str) -> str:
    return " ".join(re.findall(r"[A-Z0-9]+", value.upper()))


def security_name_aliases(security_name: str) -> set[str]:
    aliases: set[str] = set()
    current = normalize_name(security_name)
    if not current:
        return aliases
    aliases.add(current)
    suffixes = SECURITY_SUFFIXES + COMPANY_SUFFIXES
    changed = True
    while changed:
        changed = False
        for suffix in suffixes:
            if current == suffix:
                continue
            marker = f" {suffix}"
            if current.endswith(marker):
                current = current[: -len(marker)].strip()
                if current:
                    aliases.add(current)
                changed = True
                break
    return aliases


def entries_from_text(nasdaq_text: str, other_text: str) -> list[SecurityMasterEntry]:
    entries: dict[str, SecurityMasterEntry] = {}
    for row in parse_pipe_file(nasdaq_text):
        symbol = (row.get("Symbol") or "").strip().upper()
        if not symbol or row.get("Test Issue") == "Y":
            continue
        entries[symbol] = SecurityMasterEntry(
            official_symbol=symbol,
            yfinance_symbol=yfinance_symbol(symbol),
            security_name=(row.get("Security Name") or "").strip(),
            exchange="NASDAQ",
            is_etf=row.get("ETF") == "Y",
            source="nasdaqlisted",
        )
    for row in parse_pipe_file(other_text):
        symbol = (row.get("ACT Symbol") or "").strip().upper()
        if not symbol or row.get("Test Issue") == "Y":
            continue
        exchange_code = (row.get("Exchange") or "").strip().upper()
        entries[symbol] = SecurityMasterEntry(
            official_symbol=symbol,
            yfinance_symbol=yfinance_symbol(symbol),
            security_name=(row.get("Security Name") or "").strip(),
            exchange=EXCHANGE_NAMES.get(exchange_code, exchange_code or "OTHER"),
            is_etf=row.get("ETF") == "Y",
            source="otherlisted",
        )
    return sorted(entries.values(), key=lambda entry: entry.official_symbol)


async def download_security_master_entries() -> list[SecurityMasterEntry]:
    async with httpx.AsyncClient(timeout=httpx.Timeout(60)) as client:
        nasdaq_response, other_response = await asyncio.gather(
            client.get(NASDAQ_LISTED_URL),
            client.get(OTHER_LISTED_URL),
        )
    nasdaq_response.raise_for_status()
    other_response.raise_for_status()
    return entries_from_text(nasdaq_response.text, other_response.text)


class SecurityMaster:
    def __init__(self, entries: Iterable[SecurityMasterEntry]) -> None:
        self.entries = tuple(entries)
        self.by_official_symbol = {
            entry.official_symbol.upper(): entry for entry in self.entries
        }
        self.by_yfinance_symbol = {
            entry.yfinance_symbol.upper(): entry for entry in self.entries
        }
        name_candidates: dict[str, list[SecurityMasterEntry]] = {}
        for entry in self.entries:
            for alias in security_name_aliases(entry.security_name):
                name_candidates.setdefault(alias, []).append(entry)
        self.by_unique_name = {
            name: matches[0]
            for name, matches in name_candidates.items()
            if len({match.official_symbol for match in matches}) == 1
        }

    def _symbol_match(self, value: str) -> SecurityMasterEntry | None:
        candidate = value.strip().upper()
        return self.by_official_symbol.get(candidate) or self.by_yfinance_symbol.get(candidate)

    def resolve(
        self,
        original_symbol: str,
        *,
        asset_names: Iterable[str] = (),
    ) -> SecurityResolution:
        upper = original_symbol.strip().upper()
        normalized_asset_names = {
            normalize_name(name) for name in asset_names if normalize_name(name)
        }
        historical_alias = HISTORICAL_SYMBOL_ALIASES.get(upper)
        if historical_alias is not None:
            current_symbol, expected_names = historical_alias
            if any(
                expected in asset_name or asset_name in expected
                for asset_name in normalized_asset_names
                for expected in expected_names
            ):
                match = self._symbol_match(current_symbol)
                if match:
                    return SecurityResolution(
                        original_symbol,
                        match,
                        "historical_symbol_alias",
                        None,
                    )

        direct = self._symbol_match(original_symbol)
        if direct:
            name_matches = {
                match.official_symbol: match
                for name in normalized_asset_names
                if (match := self.by_unique_name.get(name)) is not None
            }
            if len(name_matches) == 1 and direct.official_symbol not in name_matches:
                return SecurityResolution(
                    original_symbol,
                    next(iter(name_matches.values())),
                    "asset_name_overrode_reused_symbol",
                    None,
                )
            return SecurityResolution(original_symbol, direct, "exact_symbol", None)

        for parenthetical in re.findall(r"\(([A-Z][A-Z0-9.^$/-]{1,19})\)", upper):
            match = self._symbol_match(parenthetical)
            if match:
                return SecurityResolution(original_symbol, match, "parenthetical_symbol", None)

        reuters_match = re.fullmatch(r"([A-Z][A-Z0-9-]{0,9})\.(?:O|N|A|P|K)", upper)
        if reuters_match:
            match = self._symbol_match(reuters_match.group(1))
            if match:
                return SecurityResolution(original_symbol, match, "vendor_symbol", None)

        name_candidates = [original_symbol]
        if "(" in original_symbol:
            name_candidates.append(original_symbol.split("(", 1)[0])
        for name in name_candidates:
            match = self.by_unique_name.get(normalize_name(name))
            if match:
                return SecurityResolution(original_symbol, match, "exact_security_name", None)

        plausible_ticker = re.fullmatch(r"[A-Z][A-Z0-9.^$/-]{0,9}", upper)
        if not plausible_ticker:
            asset_name_matches = {
                match.official_symbol: match
                for name in asset_names
                if (match := self.by_unique_name.get(normalize_name(name))) is not None
            }
            if len(asset_name_matches) == 1:
                return SecurityResolution(
                    original_symbol,
                    next(iter(asset_name_matches.values())),
                    "asset_name",
                    None,
                )
            return SecurityResolution(
                original_symbol,
                None,
                None,
                "invalid_symbol_format",
            )

        return SecurityResolution(
            original_symbol,
            None,
            None,
            "not_in_us_security_master",
        )
