"""S&P 500 constituent universe with a checked-in, validated fallback."""

from __future__ import annotations

import csv
from io import StringIO
from pathlib import Path
import re
from typing import Iterable

from .models import Provenance, UniverseMember, UniverseSnapshot, utc_now_iso


WIKIPEDIA_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
FALLBACK_AS_OF = "2026-07-19"
FALLBACK_RETRIEVED_AT = "2026-07-19T13:50:00Z"
_SYMBOL_RE = re.compile(r"^[A-Z]{1,5}(?:[.-][A-Z])?$")
_HEADER_TOKENS = {"SYMBOL", "TICKER"}


class UniverseError(RuntimeError):
    pass


class SP500Universe:
    """Return a current live roster when requested, otherwise the bundled one.

    ``force_refresh=True`` is an explicit network action.  If it fails, the
    full checked-in roster is returned with ``fallback_used=True`` and a
    warning.  Historical screens must not use this class as a historical
    membership database: either source describes only the then-current index.
    """

    def __init__(self, fallback_path: str | Path | None = None) -> None:
        self.fallback_path = Path(fallback_path) if fallback_path else (
            Path(__file__).resolve().parent / "data" / "sp500_constituents.csv"
        )
        self._snapshot: UniverseSnapshot | None = None

    def get_snapshot(self, force_refresh: bool = False) -> UniverseSnapshot:
        if force_refresh:
            try:
                self._snapshot = self._fetch_live()
            except Exception as exc:
                fallback = self._load_fallback()
                self._snapshot = UniverseSnapshot(
                    members=fallback.members,
                    provenance=fallback.provenance,
                    fallback_used=True,
                    warnings=(
                        f"Live constituent refresh failed ({type(exc).__name__}: {exc}); "
                        "using the checked-in roster.",
                        *fallback.warnings,
                    ),
                )
        elif self._snapshot is None:
            self._snapshot = self._load_fallback()
        return self._snapshot

    def members(self, force_refresh: bool = False) -> tuple[UniverseMember, ...]:
        return self.get_snapshot(force_refresh=force_refresh).members

    def symbols(self, force_refresh: bool = False) -> list[str]:
        return self.get_snapshot(force_refresh=force_refresh).symbols

    def get_symbols(self, force_refresh: bool = False) -> list[str]:
        return self.symbols(force_refresh=force_refresh)

    def get_member(self, symbol: str) -> UniverseMember | None:
        canonical = normalize_symbol(symbol)
        return next(
            (member for member in self.members() if member.symbol == canonical),
            None,
        )

    def _load_fallback(self) -> UniverseSnapshot:
        if not self.fallback_path.exists():
            raise UniverseError(f"fallback roster is missing: {self.fallback_path}")
        with self.fallback_path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        members = _members_from_rows(rows)
        _validate_members(members)
        provenance = Provenance(
            source="Wikipedia S&P 500 constituents (checked-in snapshot)",
            source_url=WIKIPEDIA_URL,
            retrieved_at=FALLBACK_RETRIEVED_AT,
            as_of=FALLBACK_AS_OF,
            basis="Current constituent table copied into the application fallback roster",
            official=False,
            notes=(
                "S&P Dow Jones Indices is the index authority; Wikipedia is a convenience source.",
                "Current-membership data creates survivorship bias in historical analysis.",
            ),
        )
        return UniverseSnapshot(
            members=members,
            provenance=provenance,
            fallback_used=True,
            warnings=(
                "Using a checked-in current-membership snapshot; refresh for later index changes.",
                "This is not a historical constituent database.",
            ),
        )

    def _fetch_live(self) -> UniverseSnapshot:
        # Imports are intentionally local: fallback operation needs only the
        # Python standard library.
        import pandas as pd
        import requests

        response = requests.get(
            WIKIPEDIA_URL,
            headers={
                "User-Agent": "sp500-quant-signal/1.0 (local research application)",
                "Accept": "text/html,application/xhtml+xml",
            },
            timeout=30,
        )
        response.raise_for_status()
        tables = pd.read_html(StringIO(response.text))
        frame = next(
            (
                table
                for table in tables
                if {"Symbol", "Security", "GICS Sector", "GICS Sub-Industry"}
                <= set(str(item) for item in table.columns)
            ),
            None,
        )
        if frame is None:
            raise UniverseError("constituent table with the expected schema was not found")
        rows = [
            {
                "Symbol": row.get("Symbol"),
                "Security": row.get("Security"),
                "GICS Sector": row.get("GICS Sector"),
                "GICS Sub-Industry": row.get("GICS Sub-Industry"),
                "CIK": row.get("CIK"),
            }
            for row in frame.to_dict(orient="records")
        ]
        members = _members_from_rows(rows)
        _validate_members(members)
        retrieved_at = utc_now_iso()
        return UniverseSnapshot(
            members=members,
            provenance=Provenance(
                source="Wikipedia S&P 500 constituents (live)",
                source_url=WIKIPEDIA_URL,
                retrieved_at=retrieved_at,
                as_of=retrieved_at[:10],
                basis="Current constituent table",
                official=False,
                notes=(
                    "S&P Dow Jones Indices is the index authority; Wikipedia is a convenience source.",
                    "Current-membership data creates survivorship bias in historical analysis.",
                ),
            ),
            fallback_used=False,
            warnings=("This is not a historical constituent database.",),
        )


def normalize_symbol(symbol: str) -> str:
    value = str(symbol).strip().upper().replace("-", ".")
    # Preserve ordinary hyphenated symbols if they are ever added, while
    # normalizing Yahoo's two known S&P share-class forms.
    if value not in {"BRK.B", "BF.B"}:
        value = str(symbol).strip().upper()
    return value


def _members_from_rows(rows: Iterable[dict[str, object]]) -> tuple[UniverseMember, ...]:
    members: list[UniverseMember] = []
    for row in rows:
        raw_symbol = str(row.get("Symbol", "")).strip().upper()
        if raw_symbol in _HEADER_TOKENS or not _SYMBOL_RE.fullmatch(raw_symbol):
            continue
        name = str(row.get("Security", "")).strip()
        sector = str(row.get("GICS Sector", "")).strip()
        sub_industry = str(row.get("GICS Sub-Industry", "")).strip()
        if not name or not sector or not sub_industry:
            continue
        raw_cik = str(row.get("CIK", "")).strip()
        cik = raw_cik.zfill(10) if raw_cik.isdigit() else None
        members.append(
            UniverseMember(
                symbol=raw_symbol,
                name=name,
                sector=sector,
                sub_industry=sub_industry,
                cik=cik,
            )
        )
    return tuple(members)


def _validate_members(members: tuple[UniverseMember, ...]) -> None:
    # 500 issuers can produce >500 securities because multiple share classes
    # are included.  A broad but strict range catches page-parser failures.
    if not 490 <= len(members) <= 520:
        raise UniverseError(
            f"implausible S&P 500 security count ({len(members)}; expected 490..520)"
        )
    symbols = [member.symbol for member in members]
    duplicates = sorted({item for item in symbols if symbols.count(item) > 1})
    if duplicates:
        raise UniverseError("duplicate constituent symbols: " + ", ".join(duplicates))
    if _HEADER_TOKENS.intersection(symbols):
        raise UniverseError("header/footer token was parsed as a constituent")
