"""rip-movie command-line entrypoint."""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from . import __version__
from .config import Config, ConfigError

OK = "\033[32m✓\033[0m"
BAD = "\033[31m✗\033[0m"
WARN = "\033[33m!\033[0m"


def _load_secrets() -> None:
    """Load config/secrets.env (gitignored) into the environment before config expansion."""
    p = Path(__file__).resolve().parent.parent / "config" / "secrets.env"
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def _load(args) -> Config:
    return Config.load(args.config)


def cmd_config_check(args) -> int:
    try:
        cfg = _load(args)
    except ConfigError as e:
        print(f"{BAD} config: {e}")
        return 1
    print(f"{OK} config loaded from {cfg.path}")

    rc = 0
    # Tools
    for key, name in [("paths.makemkvcon", "makemkvcon"), ("paths.handbrakecli", "HandBrakeCLI"),
                      ("paths.ffmpeg", "ffmpeg"), ("paths.rclone", "rclone")]:
        target = cfg.get(key, name)
        found = shutil.which(target) or (Path(target).exists() and target)
        print(f"  {OK if found else BAD} {name}: {found or 'NOT FOUND'}")
        rc |= 0 if found else 1

    # TMDb key
    tmdb = cfg.get("identify.tmdb_api_key", "")
    print(f"  {OK if tmdb else WARN} TMDb api key: {'set' if tmdb else 'MISSING (identify will fail)'}")

    # Cluster
    from . import kube
    ctx = cfg.get("deliver.kubectl.context")
    if kube.reachable(ctx):
        print(f"  {OK} cluster reachable (context={ctx})")
        for ns_key, sel_key, label in [
            ("deliver.kubectl.nextcloud_namespace", "deliver.kubectl.nextcloud_pod_selector", "nextcloud pod"),
            ("jellyfin.namespace", "jellyfin.pod_selector", "jellyfin pod"),
        ]:
            try:
                pod = kube.pod_name(cfg.get(ns_key), cfg.get(sel_key), context=ctx)
                print(f"  {OK} {label}: {pod}")
            except kube.KubeError as e:
                print(f"  {BAD} {label}: {e}")
                rc |= 1
    else:
        print(f"  {BAD} cluster not reachable (context={ctx})")
        rc |= 1

    print("OK" if rc == 0 else "issues found")
    return rc


def cmd_identify(args) -> int:
    cfg = _load(args)
    from .disc import scan_disc, select_titles, DiscError
    from .identify import identify, IdentifyError
    from .library import check_exists

    try:
        scan = scan_disc(cfg)
    except DiscError as e:
        print(f"{BAD} {e}")
        return 1

    print(f"Disc label: {scan.label or '(none)'}   ({len(scan.titles)} titles)")
    sel = select_titles(scan, cfg)
    print("\nTitles (eligible, longest first):")
    for t in sel.eligible[:12]:
        star = "*" if sel.main_feature and t.index == sel.main_feature.index else " "
        print(f"  {star} #{t.index:<3} {t.hms:>8}  {t.chapters:>3}ch  {t.size_gib:6.1f} GiB  {t.source_file}")

    if sel.ambiguous:
        print(f"\n{WARN} AMBIGUOUS: {sel.reason}")
        print("    -> would go to the review queue for a manual pick.")
    else:
        print(f"\n{OK} main feature: #{sel.main_feature.index} ({sel.main_feature.hms})")

    try:
        match = identify(scan, cfg)
    except IdentifyError as e:
        print(f"{BAD} identify: {e}")
        return 1
    if not match:
        print(f"{WARN} no TMDb match for label {scan.label!r}")
        return 1
    genre = ", ".join(match.genres) or "?"
    print(f"\n{OK} TMDb match: {match.folder}  [{genre}]  (id={match.tmdb_id})")
    print(f"    engine (auto): {'realesrgan/animevideov3' if match.is_animation else 'realesrgan/x4plus (or topaz)'}")

    try:
        hit = check_exists(cfg, match.folder)
        if hit.exists:
            print(f"\n{OK} ALREADY IN LIBRARY as {hit.matched!r} -> nothing to do.")
        else:
            print(f"\n{WARN} not in library ({len(hit.listing or [])} movies present) -> would rip.")
    except Exception as e:  # noqa: BLE001 - surface any kube error without crashing
        print(f"{WARN} library check skipped: {e}")
    return 0


def cmd_search(args) -> int:
    cfg = _load(args)
    from .library import search
    query = " ".join(args.title)
    try:
        results = search(cfg, query)
    except Exception as e:  # noqa: BLE001
        print(f"{BAD} library search failed: {e}")
        return 1
    exact = [r for r in results if r.score == 100]
    if not exact:
        _tmdb_line(cfg, query, owned=bool(results))
    if not results:
        return 0
    print(f"'{query}':")
    for r in results[:10]:
        mark = OK if r.score == 100 else WARN
        how = "" if r.score == 100 else f"   (~{r.match} match)"
        print(f"  {mark} {r.folder}{how}")
        for f in r.files:
            print(f"        {f.name}   [{f.size_gib:.1f} GiB]")
        notes = []
        if r.best_height and r.best_height < 1080:
            notes.append(f"only {r.best_height}p")
        if r.files and not r.has_directplay:
            notes.append("no Apple direct-play copy (MPEG-2/VC-1)")
        if notes:
            print(f"        -> {WARN} {', '.join(notes)} — candidate for upscale/transcode")
    return 0 if exact else 0


def cmd_push(args) -> int:
    cfg = _load(args)
    from .naming import target, NamingError
    from .deliver import push
    from .identify import search_tmdb
    from .library import list_movie_tree

    local = args.file
    if not Path(local).is_file():
        print(f"{BAD} not a file: {local}")
        return 1

    title, year = args.title, args.year
    key = cfg.get("identify.tmdb_api_key", "")
    if key and title:                       # canonicalize (gets 'WALL·E' / correct year)
        m = search_tmdb(title, key, year)
        if m:
            title, year = m.title, (m.year or year)
    if not title:
        print(f"{BAD} could not determine title — pass --title")
        return 1

    try:
        t = target(cfg, local, title, year)
    except NamingError as e:
        print(f"{BAD} {e}")
        return 1

    i = t["info"]
    print(f"source : {local}")
    print(f"probed : {i['width']}x{i['height']} {i['codec']} ({i['format_name']})")
    print(f"dest   : Movies/{t['folder']}/{t['filename']}")

    tree = list_movie_tree(cfg)
    if t["filename"] in [f.name for f in tree.get(t["folder"], [])]:
        print(f"{WARN} identical name already in library — skipping (nothing to do).")
        return 0

    res = push(cfg, local, t["rel"], dry_run=args.dry_run,
               scan=not args.no_scan, refresh=not args.no_refresh)
    if args.dry_run:
        print("DRY RUN — would run:")
        for s in res["steps"]:
            print(f"   - {s}")
        return 0
    print(f"{OK} delivered -> {res['dest']}")
    if "scan" in res:
        tail = " ".join(res["scan"].split())[-160:]
        print(f"    occ scan: {tail}")
    print(f"    jellyfin: {res.get('jellyfin', '(skipped)')}")
    return 0


def cmd_enhance(args) -> int:
    cfg = _load(args)
    from .enhance import enhance, EnhanceError
    inp = args.file
    if not Path(inp).is_file():
        print(f"{BAD} not a file: {inp}")
        return 1

    if args.animation:
        is_anim = True
    elif args.live:
        is_anim = False
    elif args.title:
        from .identify import search_tmdb
        m = search_tmdb(args.title, cfg.get("identify.tmdb_api_key", ""))
        is_anim = bool(m and m.is_animation)
        print(f"TMDb: {m.folder if m else '(no match)'}  animation={is_anim}")
    else:
        is_anim = False

    out = args.out or f"{Path(inp).with_suffix('')}_upscaled.mp4"
    try:
        res = enhance(cfg, inp, out, is_anim, model=args.model,
                      sample_seconds=args.sample, sample_start=args.sample_start,
                      progress=lambda s: print(f"  {s}"))
    except EnhanceError as e:
        print(f"{BAD} enhance failed: {e}")
        return 1
    print(f"{OK} enhanced -> {res['output']}  (engine={res['model']} {res['scale']}x -> "
          f"{res['target_height']}p, {res['chunks']} chunks)")
    return 0


def cmd_run(args) -> int:
    """Full pipeline for one finished/ripped file: enhance -> name -> deliver -> identify."""
    cfg = _load(args)
    from .identify import search_tmdb
    from .enhance import enhance, EnhanceError
    from .naming import target, NamingError
    from .library import search
    from .deliver import push
    from . import jellyfin

    inp = args.file
    if not Path(inp).is_file():
        print(f"{BAD} not a file: {inp}")
        return 1

    # 1. resolve movie + genre (drives engine choice + Jellyfin identity)
    key = cfg.get("identify.tmdb_api_key", "")
    m = search_tmdb(args.title, key, args.year) if (key and args.title) else None
    title = (m.title if m else args.title)
    year = (m.year if m else args.year)
    tmdb_id = (m.tmdb_id if m else None)
    is_anim = args.animation or (bool(m and m.is_animation) if not args.live else False)
    if not title:
        print(f"{BAD} need --title (no TMDb match)")
        return 1
    print(f"{OK} {title} ({year})  animation={is_anim}  tmdb={tmdb_id or '?'}")

    # 2. enhance -> temp upscaled file (the long part)
    work = cfg.path_for("paths.work_dir")
    work.mkdir(parents=True, exist_ok=True)
    upscaled = work / f"{Path(inp).stem}_upscaled.mp4"
    print("enhancing (AI upscale — this is the slow stage)...")
    try:
        enhance(cfg, inp, str(upscaled), is_anim,
                sample_seconds=args.sample, progress=lambda s: print(f"  {s}"))
    except EnhanceError as e:
        print(f"{BAD} enhance failed: {e}")
        return 1

    # 3. name to schema + 4. library collision check
    try:
        t = target(cfg, str(upscaled), title, year)
    except NamingError as e:
        print(f"{BAD} {e}")
        return 1
    print(f"{OK} -> Movies/{t['folder']}/{t['filename']}")
    present = [f.name for r in search(cfg, title) if r.folder == t["folder"] for f in r.files]
    if t["filename"] in present:
        print(f"{WARN} already in library — skipping delivery.")
        return 0

    if args.dry_run:
        print("DRY RUN — enhanced file kept, delivery skipped:", upscaled)
        return 0

    # 5. deliver + 6. force-identify
    res = push(cfg, str(upscaled), t["rel"])
    print(f"{OK} delivered -> {res['dest']}")
    print(f"    jellyfin: {res.get('jellyfin')}; {jellyfin.force_identify(cfg, t['folder'], tmdb_id)}")
    if not args.keep_intermediate:
        os.remove(upscaled)
    return 0


def _tmdb_line(cfg, query, owned) -> None:
    """Resolve a query to its canonical TMDb title when we don't have an exact library hit."""
    key = cfg.get("identify.tmdb_api_key", "")
    if not key:
        return
    from .identify import search_tmdb, IdentifyError
    try:
        m = search_tmdb(query, key)
    except IdentifyError:
        return
    if not m:
        if not owned:
            print(f"{WARN} '{query}' — not in your library, and no TMDb match.")
        return
    genre = ", ".join(m.genres) or "?"
    tail = "close matches below" if owned else "not in your library"
    print(f"TMDb: {m.folder}  [{genre}]  — {tail}")


def _not_yet(name):
    def run(args):
        print(f"'{name}' is not implemented yet (see README roadmap). "
              f"Working today: config-check, identify.")
        return 2
    return run


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rip-movie", description="Disc -> Jellyfin pipeline.")
    p.add_argument("--version", action="version", version=f"rip-movie {__version__}")
    p.add_argument("-c", "--config", help="path to rip-movie.toml")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("config-check", help="validate config, tools, cluster").set_defaults(fn=cmd_config_check)
    sp = sub.add_parser("search", help="check the library for a title (no disc needed)")
    sp.add_argument("title", nargs="+", help="movie title, e.g. wall-e")
    sp.set_defaults(fn=cmd_search)
    sub.add_parser("identify", help="scan the current disc and print the match").set_defaults(fn=cmd_identify)
    sp = sub.add_parser("push", help="push a finished file into the library (name+deliver+scan)")
    sp.add_argument("file", help="local video file to push")
    sp.add_argument("--title", help="movie title (canonicalized via TMDb)")
    sp.add_argument("--year", type=int, help="release year hint")
    sp.add_argument("--dry-run", action="store_true", help="show what would happen, do nothing")
    sp.add_argument("--no-scan", action="store_true", help="skip Nextcloud occ files:scan")
    sp.add_argument("--no-refresh", action="store_true", help="skip Jellyfin library refresh")
    sp.set_defaults(fn=cmd_push)
    sp = sub.add_parser("enhance", help="AI-upscale a video (denoise -> Real-ESRGAN -> 1080p)")
    sp.add_argument("file", help="input video")
    sp.add_argument("-o", "--out", help="output path (default: <name>_upscaled.mp4)")
    sp.add_argument("--title", help="resolve genre via TMDb to auto-pick the engine")
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--animation", action="store_true", help="use the animation engine (animevideov3)")
    g.add_argument("--live", action="store_true", help="use the live-action engine (remacri)")
    sp.add_argument("--model", help="pin a specific model (e.g. remacri-4x, ultrasharp-4x)")
    sp.add_argument("--sample", type=float, help="only process N seconds (for testing)")
    sp.add_argument("--sample-start", type=float, default=0.0,
                    help="start the sample at this second (e.g. 2100 = 35 min in)")
    sp.set_defaults(fn=cmd_enhance)
    sp = sub.add_parser("run", help="upscale a file and deliver it to the library (enhance→name→push→identify)")
    sp.add_argument("file", help="ripped/finished video to upscale + deliver")
    sp.add_argument("--title", help="movie title (canonicalized via TMDb)")
    sp.add_argument("--year", type=int)
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--animation", action="store_true", help="force animation engine")
    g.add_argument("--live", action="store_true", help="force live-action engine")
    sp.add_argument("--sample", type=float, help="only process N seconds (test)")
    sp.add_argument("--dry-run", action="store_true", help="enhance + name, skip delivery")
    sp.add_argument("--keep-intermediate", action="store_true", help="keep the temp upscaled file")
    sp.set_defaults(fn=cmd_run)
    for name, help_ in [("watch", "daemon: auto-process discs"),
                        ("review", "resolve ambiguous discs"), ("status", "show jobs")]:
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--dry-run", action="store_true")
        sp.set_defaults(fn=_not_yet(name))
    return p


def main(argv=None) -> int:
    _load_secrets()
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except ConfigError as e:
        print(f"{BAD} {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
