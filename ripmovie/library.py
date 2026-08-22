"""Library membership: search the Nextcloud Movies folder by title (no disc needed).

Lists the Movies tree inside the Nextcloud pod (no credentials) and fuzzy-matches a typed
title against stored folder names, tolerating punctuation/spacing drift (e.g. `wall-e`,
`walle`, `WALL E` all match `WALL·E (2008)`).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import kube
from .config import Config

_RES_RE = re.compile(r"(\d{3,4}p)", re.I)
_YEAR_RE = re.compile(r"^(.*?)\s*\((\d{4})\)\s*$")
# Codec tokens in priority order (most-specific/direct-play first). A tag like
# "1080p Microsoft HEVC" must resolve to HEVC, not Microsoft.
_CODEC_PRIORITY = ["HEVC", "H265", "X265", "AVC", "H264", "X264",
                   "MPEG2", "MPEG", "VC1", "MICROSOFT", "WMV"]
# Codecs Apple devices decode natively -> a "present" rip that already direct-plays.
DIRECTPLAY_CODECS = {"HEVC", "H265", "X265", "AVC", "H264", "X264"}


def _classify(stem: str) -> tuple[str, str]:
    """('WALL·E (2008) - 480p MPEG') -> ('480p', 'MPEG')."""
    rm = _RES_RE.search(stem)
    res = rm.group(1).lower() if rm else ""
    up = stem.upper()
    codec = next((c for c in _CODEC_PRIORITY if c in up), "")
    return res, codec


@dataclass
class MovieFile:
    name: str
    size_bytes: int = 0
    res: str = ""        # e.g. 1080p
    codec: str = ""      # e.g. HEVC / MPEG / AVC

    @property
    def size_gib(self) -> float:
        return self.size_bytes / (1024 ** 3)


@dataclass
class SearchResult:
    folder: str
    title: str
    year: str
    files: list[MovieFile] = field(default_factory=list)
    score: int = 0
    match: str = ""      # how it matched (exact / token / substring)

    @property
    def best_height(self) -> int:
        hs = [int(f.res[:-1]) for f in self.files if f.res[:-1].isdigit()]
        return max(hs) if hs else 0

    @property
    def has_directplay(self) -> bool:
        return any(f.codec in DIRECTPLAY_CODECS for f in self.files)


@dataclass
class ShowResult:
    folder: str
    title: str
    year: str
    seasons: int = 0
    episodes: int = 0
    score: int = 0
    match: str = ""


@dataclass
class LibraryHit:
    exists: bool
    folder: str
    matched: str = ""
    listing: list[str] = field(default_factory=list)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s.lower())).strip()


def _nospace(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def _strip_year(folder: str) -> tuple[str, str]:
    m = _YEAR_RE.match(folder)
    return (m.group(1).strip(), m.group(2)) if m else (folder, "")


def movies_dir(cfg: Config) -> str:
    data = cfg.require("deliver.kubectl.data_path").rstrip("/")
    sub = cfg.get("library.movies_subpath", "Videos/Movies").strip("/")
    return f"{data}/{sub}"


def list_movie_tree(cfg: Config) -> dict[str, list[MovieFile]]:
    """{folder_name: [MovieFile, ...]} via one `find` in the Nextcloud pod."""
    k = cfg.get("deliver.kubectl", {})
    ns = k.get("nextcloud_namespace", "nextcloud")
    pod = kube.pod_name(ns, k.get("nextcloud_pod_selector", "app.kubernetes.io/name=nextcloud"),
                        context=k.get("context"))
    out = kube.exec_in(
        ns, pod,
        ["find", movies_dir(cfg), "-mindepth", "1", "-maxdepth", "2", "-printf", r"%y\t%s\t%P\n"],
        container=k.get("nextcloud_container", "nextcloud"), context=k.get("context"),
    )
    tree: dict[str, list[MovieFile]] = {}
    for line in out.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        ftype, size, rel = parts
        if "/" not in rel:                       # top-level entry
            if ftype == "d":
                tree.setdefault(rel, [])
            continue
        folder, _, fname = rel.partition("/")
        if ftype != "f":
            continue
        mf = MovieFile(name=fname, size_bytes=int(size) if size.isdigit() else 0)
        mf.res, mf.codec = _classify(fname.rsplit(".", 1)[0])
        tree.setdefault(folder, []).append(mf)
    return tree


def _height(res: str) -> int:
    return int(res[:-1]) if res[:-1].isdigit() else 0


@dataclass
class UpscaleCandidate:
    folder: str
    title: str
    year: str
    best_height: int
    source_file: str        # the SD master to upscale (filename inside the folder)
    source_codec: str
    size_gib: float
    status: str             # "candidate" (SD, no HD copy) | "done" (already has 1080p+) | "hd"


def upscale_candidates(cfg: Config, tree: dict[str, list[MovieFile]] | None = None
                       ) -> list[UpscaleCandidate]:
    """Classify every library movie for the upscale viewer. A folder is a candidate when it has an
    SD (<=576p) file and does NOT already carry a >=1080p direct-play rendition; the largest SD file
    is the master we'd re-upscale. Candidates sort first."""
    if tree is None:
        tree = list_movie_tree(cfg)
    sd_max = int(cfg.get("upscale.dvd.sd_max_height", 576))
    out: list[UpscaleCandidate] = []
    for folder, files in tree.items():
        if not files:
            continue
        title, year = _strip_year(folder)
        best = max((_height(f.res) for f in files), default=0)
        sd = [f for f in files if 0 < _height(f.res) <= sd_max]
        hd_dp = [f for f in files if _height(f.res) >= 1080 and f.codec in DIRECTPLAY_CODECS]
        if sd and not hd_dp:
            status, src = "candidate", max(sd, key=lambda f: f.size_bytes)
        elif sd and hd_dp:
            status, src = "done", None
        else:
            status, src = "hd", None
        out.append(UpscaleCandidate(
            folder, title, year, best,
            src.name if src else "", src.codec if src else "",
            round(src.size_gib if src else max((f.size_gib for f in files), default=0.0), 1),
            status))
    out.sort(key=lambda c: (c.status != "candidate", c.title.lower()))
    return out


def search(cfg: Config, query: str, tree: dict[str, list[MovieFile]] | None = None) -> list[SearchResult]:
    if tree is None:
        tree = list_movie_tree(cfg)
    q = query.strip()
    q = _YEAR_RE.match(q).group(1).strip() if _YEAR_RE.match(q) else q
    qn, qns = _norm(q), _nospace(q)
    qtok = set(qn.split())

    results: list[SearchResult] = []
    for folder, files in tree.items():
        title, year = _strip_year(folder)
        fn, fns = _norm(title), _nospace(title)
        ftok = set(fn.split())
        score, how = 0, ""
        if qn == fn or qns == fns:
            score, how = 100, "exact"
        elif qtok and qtok <= ftok:
            score, how = 70, "title"
        elif qn and qn in fn:
            score, how = 50, "substring"
        elif qns and qns in fns:
            score, how = 45, "substring"
        if score:
            results.append(SearchResult(folder, title, year,
                                        sorted(files, key=lambda f: -f.size_bytes),
                                        score, how))
    results.sort(key=lambda r: (-r.score, r.title.lower()))
    return results


def shows_dir(cfg: Config) -> str:
    data = cfg.require("deliver.kubectl.data_path").rstrip("/")
    sub = cfg.get("library.shows_subpath", "Videos/TV_Shows").strip("/")
    return f"{data}/{sub}"


_EP_RE = re.compile(r"\.(mkv|mp4|m4v|avi|ts|mov)$", re.I)


def list_show_tree(cfg: Config) -> dict[str, dict]:
    """{show_folder: {'seasons': int, 'episodes': int}} via one `find` in the Nextcloud pod."""
    k = cfg.get("deliver.kubectl", {})
    ns = k.get("nextcloud_namespace", "nextcloud")
    pod = kube.pod_name(ns, k.get("nextcloud_pod_selector", "app.kubernetes.io/name=nextcloud"),
                        context=k.get("context"))
    try:
        out = kube.exec_in(
            ns, pod,
            ["find", shows_dir(cfg), "-mindepth", "1", "-maxdepth", "3", "-printf", r"%y\t%P\n"],
            container=k.get("nextcloud_container", "nextcloud"), context=k.get("context"),
        )
    except kube.KubeError:
        return {}
    shows: dict[str, dict] = {}
    for line in out.splitlines():
        ftype, _, rel = line.partition("\t")
        if not rel:
            continue
        parts = rel.split("/")
        show = parts[0]
        s = shows.setdefault(show, {"seasons": set(), "episodes": 0})
        if len(parts) == 2 and ftype == "d":
            s["seasons"].add(parts[1])
        elif ftype == "f" and _EP_RE.search(parts[-1]):
            s["episodes"] += 1
    return {name: {"seasons": len(v["seasons"]), "episodes": v["episodes"]}
            for name, v in shows.items()}


def search_shows(cfg: Config, query: str,
                 tree: dict[str, dict] | None = None) -> list[ShowResult]:
    if tree is None:
        tree = list_show_tree(cfg)
    q = query.strip()
    q = _YEAR_RE.match(q).group(1).strip() if _YEAR_RE.match(q) else q
    qn, qns = _norm(q), _nospace(q)
    qtok = set(qn.split())
    results: list[ShowResult] = []
    for folder, info in tree.items():
        title, year = _strip_year(folder)
        fn, fns = _norm(title), _nospace(title)
        ftok = set(fn.split())
        score, how = 0, ""
        if qn == fn or qns == fns:
            score, how = 100, "exact"
        elif qtok and qtok <= ftok:
            score, how = 70, "title"
        elif qn and (qn in fn or qns in fns):
            score, how = 50, "substring"
        if score:
            results.append(ShowResult(folder, title, year, info["seasons"], info["episodes"],
                                      score, how))
    results.sort(key=lambda r: (-r.score, r.title.lower()))
    return results


def check_exists(cfg: Config, folder: str) -> LibraryHit:
    tree = list_movie_tree(cfg)
    hits = [r for r in search(cfg, _strip_year(folder)[0], tree) if r.score == 100]
    if hits:
        return LibraryHit(True, folder, matched=hits[0].folder, listing=list(tree))
    return LibraryHit(False, folder, listing=list(tree))
