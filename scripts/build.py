"""
Daily Instagram outlier dashboard for Drew.

  python scripts/build.py              # live run (needs APIFY_TOKEN env var)
  python scripts/build.py --bootstrap  # first run: pull 30 days so every page has a baseline
  python scripts/build.py --sample     # fake data, no API calls, for previewing the design

Pipeline: Apify scrape -> merge into data/history.json -> score each post against
its own page's normal -> cache thumbnails -> write docs/index.html (served by GitHub Pages).
"""
import argparse
import datetime as dt
import io
import json
import os
import pathlib
import random
import statistics
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config.json").read_text())
HISTORY_PATH = ROOT / "data" / "history.json"
DOCS = ROOT / "docs"
THUMBS = DOCS / "thumbs"
TEMPLATE = ROOT / "templates" / "dashboard.html"
APIFY = "https://api.apify.com/v2"
EARLY_MAX_HOURS = 36
NOW = dt.datetime.now(dt.timezone.utc)


# ---------------------------------------------------------------- helpers
def log(msg):
    print(f"[build] {msg}", flush=True)


def parse_ts(value):
    if not value:
        return None
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def all_handles():
    return [h.lower() for handles in CONFIG["groups"].values() for h in handles]


def group_lookup():
    return {h.lower(): g for g, handles in CONFIG["groups"].items() for h in handles}


# ---------------------------------------------------------------- Apify
def apify_request(method, path, token, body=None, params=None):
    url = f"{APIFY}{path}"
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())


def get_token():
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        sys.exit("APIFY_TOKEN is not set. Add it as a GitHub repository secret (see README).")
    return token


def fetch_existing_run(run_id):
    """Rebuild from a run that already finished, at no extra Apify cost."""
    token = get_token()
    run = apify_request("GET", f"/actor-runs/{run_id}", token)["data"]
    if run["status"] != "SUCCEEDED":
        sys.exit(f"Apify run {run_id} has status {run['status']}, so there's nothing to reuse.")
    items = apify_request(
        "GET", f"/datasets/{run['defaultDatasetId']}/items", token, params={"clean": "true", "format": "json"}
    )
    log(f"Reused Apify run {run_id}: {len(items)} posts, no new scrape")
    return items


def fetch_posts(lookback_days, max_posts):
    token = get_token()
    cfg = CONFIG["apify"]
    run_input = {
        "username": all_handles(),
        "resultsLimit": max_posts,
        "onlyPostsNewerThan": f"{lookback_days} days",
        "skipPinnedPosts": True,
        "dataDetailLevel": cfg["data_detail_level"],
    }
    params = {"maxTotalChargeUsd": cfg["max_charge_usd_per_run"]}
    log(f"Starting Apify run for {len(run_input['username'])} pages, last {lookback_days} days")
    run = apify_request("POST", f"/acts/{cfg['actor']}/runs", token, run_input, params)["data"]

    while run["status"] in ("READY", "RUNNING"):
        time.sleep(15)
        run = apify_request("GET", f"/actor-runs/{run['id']}", token, params={"waitForFinish": 60})["data"]
        log(f"Run status: {run['status']}")

    if run["status"] != "SUCCEEDED":
        sys.exit(f"Apify run ended with status {run['status']}. Check the run log in the Apify console.")

    items = apify_request(
        "GET", f"/datasets/{run['defaultDatasetId']}/items", token, params={"clean": "true", "format": "json"}
    )
    log(f"Apify returned {len(items)} posts")
    return items


# ---------------------------------------------------------------- normalise
def owner_of(item, lookup):
    owner = (item.get("ownerUsername") or "").lower()
    if owner in lookup:
        return owner
    # collab posts can list someone else as owner; fall back to the page we scraped
    input_url = (item.get("inputUrl") or "").rstrip("/").lower()
    handle = input_url.split("/")[-1] if input_url else ""
    return handle if handle in lookup else owner


def normalise(item, lookup):
    kind = {"Video": "reel", "Sidecar": "carousel", "Image": "image"}.get(item.get("type"))
    if kind is None or not item.get("shortCode") or not item.get("timestamp"):
        return None
    if kind == "image" and not CONFIG["scoring"]["include_single_images"]:
        return None
    owner = owner_of(item, lookup)
    if owner not in lookup:
        return None

    likes = item.get("likesCount")
    likes = likes if isinstance(likes, int) and likes >= 0 else None  # -1 means hidden
    views = item.get("videoPlayCount") or item.get("videoViewCount") or None
    slides = len(item.get("childPosts") or []) or len(item.get("images") or []) or None
    caption = (item.get("caption") or "").strip()

    return {
        "code": item["shortCode"],
        "url": item.get("url") or f"https://www.instagram.com/p/{item['shortCode']}/",
        "owner": owner,
        "group": lookup[owner],
        "kind": kind,
        "posted": item["timestamp"],
        "likes": likes,
        "comments": item.get("commentsCount") or 0,
        "views": views if kind == "reel" else None,
        "slides": slides if kind == "carousel" else None,
        "duration": round(item["videoDuration"]) if item.get("videoDuration") else None,
        "caption": caption[:300],
        "image": item.get("displayUrl"),
        "last_seen": NOW.isoformat(),
    }


# ---------------------------------------------------------------- history
def load_history():
    if HISTORY_PATH.exists():
        return json.loads(HISTORY_PATH.read_text())
    return {"posts": {}, "runs": []}


def merge(history, fresh):
    for post in fresh:
        merged = {**history["posts"].get(post["code"], {}), **post}
        age_h = (NOW - parse_ts(post["posted"])).total_seconds() / 3600
        if 12 <= age_h <= EARLY_MAX_HOURS:
            merged["early"] = metric(merged)[1]  # how it looked on day one
        history["posts"][post["code"]] = merged
    cutoff = NOW - dt.timedelta(days=CONFIG["scoring"]["baseline_days"] + 2)
    history["posts"] = {c: p for c, p in history["posts"].items() if parse_ts(p["posted"]) >= cutoff}
    return history


# ---------------------------------------------------------------- scoring
def metric(post):
    """What 'performing' means for this post. Reels: views. Carousels: likes (Drew's Viral Finder signal)."""
    if post["kind"] == "reel" and post.get("views"):
        return "views", post["views"]
    if post.get("likes") is not None:
        return "likes", post["likes"]
    return "comments", post["comments"]


def score_posts(history):
    """
    Multiplier = this post / that page's usual for the same post type.
    Posts older than 72h are compared with the page's settled posts.
    Younger posts are compared with how the page's other posts looked on day one,
    so a 10-hour-old reel isn't judged against posts that have had a month to grow.
    """
    s = CONFIG["scoring"]
    settled = dt.timedelta(hours=s["settled_after_hours"])
    need = s["min_baseline_posts"]
    pools = {}
    for p in history["posts"].values():
        name, value = metric(p)
        pool = pools.setdefault((p["owner"], p["kind"], name), {"settled": [], "early": [], "all": []})
        pool["all"].append((p["code"], value))
        if NOW - parse_ts(p["posted"]) >= settled:
            pool["settled"].append((p["code"], value))
        if p.get("early"):
            pool["early"].append((p["code"], p["early"]))

    window_start = NOW - dt.timedelta(days=7)
    scored = []
    for p in history["posts"].values():
        if parse_ts(p["posted"]) < window_start:
            continue
        name, value = metric(p)
        pool = pools[(p["owner"], p["kind"], name)]
        young = NOW - parse_ts(p["posted"]) < settled
        others = lambda key: [v for c, v in pool[key] if c != p["code"]]

        basis, sample = "settled", others("settled")
        if young and len(others("early")) >= need:
            basis, sample = "day_one", others("early")
        elif len(sample) < need:
            basis, sample = "all", others("all")
        baseline = statistics.median(sample) if len(sample) >= need else None

        scored.append({
            **p,
            "metric": name,
            "young": young,
            "basis": basis if baseline else None,
            "baseline": round(baseline) if baseline else None,
            "score": round(value / baseline, 2) if baseline else None,
        })
    return scored


# ---------------------------------------------------------------- thumbnails
def _download_thumb(image_url, target):
    from PIL import Image

    req = urllib.request.Request(image_url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = resp.read()
    img = Image.open(io.BytesIO(data)).convert("RGB")
    img.thumbnail((360, 640))
    img.save(target, "JPEG", quality=78, optimize=True)


def cache_thumbnails(posts, budget_seconds=300, workers=16):
    """Download small cover images in parallel. Anything not done within the budget
    shows a placeholder today and gets picked up on tomorrow's run."""
    from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

    THUMBS.mkdir(parents=True, exist_ok=True)
    keep = {f"{p['code']}.jpg" for p in posts}
    todo = [p for p in sorted(posts, key=lambda p: p.get("score") or 0, reverse=True)
            if p.get("image") and not (THUMBS / f"{p['code']}.jpg").exists()]
    log(f"Downloading {len(todo)} thumbnails ({len(posts) - len(todo)} already cached)")

    started, done, failed = time.time(), 0, 0
    pool = ThreadPoolExecutor(max_workers=workers)
    pending = {pool.submit(_download_thumb, p["image"], THUMBS / f"{p['code']}.jpg"): p for p in todo}
    while pending:
        remaining = budget_seconds - (time.time() - started)
        if remaining <= 0:
            log(f"Thumbnail time budget reached, {len(pending)} left for tomorrow")
            break
        finished, _ = wait(pending, timeout=min(remaining, 30), return_when=FIRST_COMPLETED)
        for f in finished:
            pending.pop(f)
            if f.exception():
                failed += 1
            else:
                done += 1
        if finished and (done + failed) % 50 < len(finished):
            log(f"Thumbnails: {done} saved, {failed} failed, {len(pending)} to go")
    pool.shutdown(wait=False, cancel_futures=True)
    log(f"Thumbnails finished: {done} saved, {failed} failed")

    for p in posts:
        target = THUMBS / f"{p['code']}.jpg"
        p["thumb"] = f"thumbs/{target.name}" if target.exists() else None
        p.pop("image", None)
    for f in THUMBS.glob("*.jpg"):
        if f.name not in keep:
            f.unlink()


# ---------------------------------------------------------------- sample data
SAMPLE_CAPTIONS = [
    "The most expensive things Mike Tyson ever bought",
    "Who becomes the best after these sports swaps?",
    "Athletes who went broke after making $100M+",
    "Rookie contracts vs second contracts, the jump is insane",
    "Every NBA MVP ranked by how they'd do in today's league",
    "What NFL players actually eat on game day",
    "The 5 craziest buzzer beaters nobody talks about",
    "How much each Super Bowl ring is really worth",
    "Stephen Curry's shooting routine, broken down",
    "Players who retired at their peak and why",
    "Historic stadiums then vs now",
    "Things only D1 athletes understand",
]


def sample_posts():
    rng = random.Random(7)
    lookup = group_lookup()
    posts = []
    for handle in all_handles():
        size = rng.choice([1, 3, 10, 30])
        for i in range(rng.randint(8, 18)):
            kind = rng.choice(["reel", "reel", "carousel"])
            posted = NOW - dt.timedelta(hours=rng.uniform(1, 24 * 30))
            base = rng.randint(2_000, 40_000) * size
            boost = rng.choice([1, 1, 1, 1, 1.4, 2, 3.5, 6]) * rng.uniform(0.6, 1.3)
            age_h = (NOW - posted).total_seconds() / 3600
            ramp = min(1.0, 0.25 + 0.75 * age_h / 72)  # posts keep growing for ~3 days
            likes = int(base * boost * ramp)
            posts.append({
                "code": f"S{handle[:4]}{i}{rng.randint(1000, 9999)}",
                "url": f"https://www.instagram.com/{handle}/",
                "owner": handle,
                "group": lookup[handle],
                "kind": kind,
                "posted": posted.isoformat(),
                "likes": likes,
                "comments": int(likes * rng.uniform(0.004, 0.03)),
                "views": int(likes * rng.uniform(12, 40)) if kind == "reel" else None,
                "slides": rng.randint(3, 10) if kind == "carousel" else None,
                "duration": rng.randint(8, 60) if kind == "reel" else None,
                "caption": rng.choice(SAMPLE_CAPTIONS),
                "image": None,
                "last_seen": NOW.isoformat(),
            })
            if age_h > EARLY_MAX_HOURS:
                p = posts[-1]
                p["early"] = int(metric(p)[1] / ramp * rng.uniform(0.45, 0.6))
    return posts


# ---------------------------------------------------------------- render
def render(posts, run_info):
    payload = {
        "title": CONFIG["dashboard_title"],
        "generated": NOW.isoformat(),
        "sample": run_info.get("sample", False),
        "groups": list(CONFIG["groups"].keys()),
        "own": [h.lower() for h in CONFIG.get("own_handles", [])],
        "pages_tracked": len(all_handles()),
        "silent_pages": run_info.get("silent_pages", []),
        "posts": posts,
    }
    html = TEMPLATE.read_text().replace("/*__DATA__*/null", json.dumps(payload, separators=(",", ":")))
    DOCS.mkdir(exist_ok=True)
    (DOCS / "index.html").write_text(html)
    (DOCS / ".nojekyll").write_text("")
    log(f"Wrote docs/index.html with {len(posts)} posts from the last 7 days")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bootstrap", action="store_true", help="pull 30 days to build baselines")
    ap.add_argument("--sample", action="store_true", help="use fake data, no API calls")
    ap.add_argument("--from-run", default="", help="rebuild from a finished Apify run ID instead of scraping")
    args = ap.parse_args()

    lookup = group_lookup()
    if args.sample:
        history = {"posts": {p["code"]: p for p in sample_posts()}, "runs": []}
        scored = score_posts(history)
        for p in scored:
            p["thumb"] = None
            p.pop("image", None)
        render(scored, {"sample": True})
        return

    cfg = CONFIG["apify"]
    first = args.bootstrap or not HISTORY_PATH.exists()
    days = cfg["bootstrap_lookback_days"] if first else cfg["daily_lookback_days"]
    if args.from_run.strip():
        raw = fetch_existing_run(args.from_run.strip())
    else:
        max_posts = cfg["bootstrap_max_posts_per_page"] if first else cfg["daily_max_posts_per_page"]
        raw = fetch_posts(days, max_posts)
    fresh = [p for p in (normalise(i, lookup) for i in raw) if p]
    returned = {p["owner"] for p in fresh}
    silent = sorted(h for h in all_handles() if h not in returned)
    if silent:
        log(f"No posts came back for: {', '.join(silent)}")

    history = merge(load_history(), fresh)
    history["runs"] = (history.get("runs", []) + [{
        "at": NOW.isoformat(), "lookback_days": days, "raw_items": len(raw), "kept": len(fresh),
    }])[-60:]
    HISTORY_PATH.parent.mkdir(exist_ok=True)
    HISTORY_PATH.write_text(json.dumps(history, indent=1))

    scored = score_posts(history)
    cache_thumbnails(scored)
    render(scored, {"silent_pages": silent})


if __name__ == "__main__":
    main()
    sys.stdout.flush()
    os._exit(0)  # don't wait on any thumbnail download that's still hanging
