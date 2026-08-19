"""Identify a disc/file as a TMDb movie.

TMDb text search is picky: stylized titles (e.g. WALL·E) are only returned when the query
contains the exact separator, and popularity ordering can float an unrelated recent film to the
top. So we (1) query several spelling variants to make TMDb *return* the right candidate, and
(2) pick the result whose title is most similar to the query rather than trusting result[0].
stdlib-only.
"""
from __future__ import annotations

import difflib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

from .config import Config
from .disc import DiscScan

TMDB = "https://api.themoviedb.org/3"


class IdentifyError(Exception):
    pass


@dataclass
class Match:
    title: str
    year: Optional[int]
    tmdb_id: int
    genres: list[str] = field(default_factory=list)
    original_title: str = ""
    overview: str = ""
    runtime: Optional[int] = None       # minutes, from TMDb — used to pick the right disc title
    score: float = 0.0

    @property
    def is_animation(self) -> bool:
        return "Animation" in self.genres

    @property
    def folder(self) -> str:
        return f"{self.title} ({self.year})" if self.year else self.title


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s.lower())).strip()


def clean_label(label: str, strip_tokens: list[str]) -> tuple[str, Optional[int]]:
    """'THE_SANTA_CLAUSE_2_DISC1' -> ('The Santa Clause 2', None)."""
    year = None
    ym = re.search(r"(19|20)\d{2}", label)
    if ym:
        year = int(ym.group(0))
    text = re.sub(r"[._]+", " ", label)
    strip = {t.upper() for t in strip_tokens}
    kept = []
    for tok in text.split():
        up = tok.upper()
        if up in strip or re.fullmatch(r"DISC\d+|D\d+|CD\d+|(19|20)\d{2}", up):
            continue
        kept.append(tok)
    name = " ".join(kept).strip()
    if name.isupper() or name.islower():
        name = name.title()
    return name, year


def _query_variants(name: str) -> list[str]:
    """Spelling variants to coax TMDb into returning stylized titles (WALL-E -> WALL·E)."""
    base = name.strip()
    spaced = re.sub(r"[._\-]+", " ", base).strip()
    variants = [base, spaced, re.sub(r"\s+", "", spaced)]
    for sep in ("·", "."):                       # stylized separators
        variants.append(re.sub(r"[\s._\-]+", sep, spaced))
    seen, out = set(), []
    for v in variants:
        if v and v.lower() not in seen:
            seen.add(v.lower())
            out.append(v)
    return out


def _get(url: str, timeout: int = 20) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _raw_search(query: str, api_key: str, year: Optional[int]) -> list[dict]:
    params = {"api_key": api_key, "query": query, "include_adult": "false"}
    if year:
        params["year"] = str(year)
    try:
        data = _get(f"{TMDB}/search/movie?" + urllib.parse.urlencode(params))
    except urllib.error.HTTPError as e:
        raise IdentifyError(f"TMDb search failed ({e.code}); check tmdb_api_key") from e
    results = data.get("results") or []
    if not results and year:                     # retry without the year filter
        params.pop("year")
        results = (_get(f"{TMDB}/search/movie?" + urllib.parse.urlencode(params)).get("results")
                   or [])
    return results


def _similarity(qn: str, r: dict) -> float:
    cands = [_norm(r.get("title", "")), _norm(r.get("original_title", ""))]
    return max((difflib.SequenceMatcher(None, qn, c).ratio() for c in cands if c), default=0.0)


def search_tmdb(name: str, api_key: str, year: Optional[int] = None,
                threshold: float = 0.6) -> Optional[Match]:
    """Best-scoring TMDb match across spelling variants, or None if nothing is confident."""
    if not name:
        return None
    qn = _norm(name)
    best: Optional[dict] = None
    best_score = 0.0
    seen_ids: set[int] = set()
    for variant in _query_variants(name):
        for r in _raw_search(variant, api_key, year):
            if r["id"] in seen_ids:
                continue
            seen_ids.add(r["id"])
            sc = _similarity(qn, r)
            if sc > best_score or (
                abs(sc - best_score) < 1e-6 and best is not None
                and r.get("popularity", 0) > best.get("popularity", 0)
            ):
                best, best_score = r, sc
        if best_score >= 0.995:                  # exact normalized title -> stop early
            break
    if best and best_score >= threshold:
        m = _details(best["id"], api_key, fallback=best)
        m.score = round(best_score, 3)
        return m
    return None


def _details(tmdb_id: int, api_key: str, fallback: dict) -> Match:
    try:
        d = _get(f"{TMDB}/movie/{tmdb_id}?api_key={api_key}")
    except urllib.error.HTTPError:
        d = fallback
    rel = d.get("release_date") or fallback.get("release_date") or ""
    year = int(rel[:4]) if rel[:4].isdigit() else None
    genres = [g["name"] for g in d.get("genres", [])] if d.get("genres") else []
    return Match(
        title=d.get("title") or fallback.get("title", ""),
        year=year,
        tmdb_id=tmdb_id,
        genres=genres,
        original_title=d.get("original_title", ""),
        overview=(d.get("overview") or "")[:200],
        runtime=d.get("runtime") or None,
    )


def identify(scan: DiscScan, cfg: Config) -> Optional[Match]:
    api_key = cfg.get("identify.tmdb_api_key", "")
    if not api_key:
        raise IdentifyError("identify.tmdb_api_key is not set in config")
    name, year = clean_label(scan.label, cfg.get("identify.strip_label_tokens", []))
    return search_tmdb(name, api_key, year)
