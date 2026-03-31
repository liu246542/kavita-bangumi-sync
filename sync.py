#!/usr/bin/env python3
"""
kavita-bangumi-sync: Fetch manga metadata from Bangumi and write to Kavita.

Usage:
    python3 sync.py                  # Sync all series
    python3 sync.py --dry-run        # Preview without writing
    python3 sync.py --series "名前"  # Sync a single series by name
"""

import json
import re
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
import argparse
from pathlib import Path


# ─── Config ──────────────────────────────────────────────────────────────────

def load_config():
    config_path = Path(__file__).parent / "config.json"
    if not config_path.exists():
        print("错误: config.json 不存在，请复制 config.example.json 并填写")
        sys.exit(1)
    with open(config_path) as f:
        return json.load(f)


def load_overrides():
    """Load manual series name → Bangumi ID mappings."""
    override_path = Path(__file__).parent / "overrides.json"
    if not override_path.exists():
        return {}
    with open(override_path) as f:
        data = json.load(f)
    # Filter out non-mapping keys like _comment
    return {k: v for k, v in data.items() if isinstance(v, int)}


# ─── Kavita API ──────────────────────────────────────────────────────────────

class KavitaClient:
    def __init__(self, base_url, username, password):
        self.base_url = base_url.rstrip("/")
        self.token = None
        self._login(username, password)

    def _login(self, username, password):
        data = json.dumps({"username": username, "password": password}).encode()
        req = urllib.request.Request(
            f"{self.base_url}/api/account/login",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req)
        result = json.loads(resp.read())
        self.token = result.get("token")
        if not self.token:
            raise RuntimeError("Kavita 登录失败: 未获取到 token")
        print(f"✓ Kavita 登录成功 (user: {username})")

    def _request(self, method, path, body=None):
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Content-Type", "application/json")
        try:
            resp = urllib.request.urlopen(req)
            content = resp.read()
            if not content:
                return None
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                return content.decode(errors="replace")
        except urllib.error.HTTPError as e:
            body_text = e.read().decode(errors="replace")
            raise RuntimeError(f"Kavita API {method} {path} → {e.code}: {body_text}")

    def get_all_series(self):
        """Get all series from Kavita using the series/v2 endpoint."""
        body = {
            "statements": [],
            "combination": 1,
            "sortOptions": {"sortField": 1, "isAscending": True},
            "limitTo": 0,
        }
        return self._request("POST", "/api/series/v2", body)

    def get_series_metadata(self, series_id):
        """Get metadata for a specific series."""
        return self._request("GET", f"/api/series/metadata?seriesId={series_id}")

    def update_series_metadata(self, metadata_dto):
        """Update series metadata."""
        body = {"seriesMetadata": metadata_dto}
        return self._request("POST", "/api/series/metadata", body)


# ─── Bangumi API ─────────────────────────────────────────────────────────────

class BangumiClient:
    def __init__(self, base_url, user_agent, rate_limit_delay=0.4):
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent
        self.delay = rate_limit_delay
        self._last_request = 0

    def _throttle(self):
        elapsed = time.time() - self._last_request
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        self._last_request = time.time()

    def _get(self, url):
        self._throttle()
        req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        try:
            resp = urllib.request.urlopen(req, timeout=15)
            return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise
        except Exception:
            return None

    @staticmethod
    def clean_title(title):
        """Strip edition/version/region suffixes that interfere with search."""
        cleaned = title
        # Remove parenthesized suffixes: （青文版）（境外版）（完全版）etc.
        cleaned = re.sub(r'[（(][^）)]*(?:版|篇|編)[）)]', '', cleaned)
        # Remove trailing -TW, -JP, -HK region tags
        cleaned = re.sub(r'[-\s]*(?:TW|JP|HK)$', '', cleaned)
        # Remove format/resolution tags: 8K重排版, 重排版
        cleaned = re.sub(r'\s*\d*[Kk]?重排版$', '', cleaned)
        # Remove edition tags at end: -愛藏版, 愛藏版
        cleaned = re.sub(r'[-\s]*愛藏版$', '', cleaned)
        # Remove trailing arc/part names: 公安篇, XX篇 (without parens)
        cleaned = re.sub(r'\s+\S*篇$', '', cleaned)
        return cleaned.strip()

    def search(self, title):
        """Search Bangumi for a manga by title. Returns best match or None."""
        # Try original title first, then cleaned version
        for query in dict.fromkeys([title, self.clean_title(title)]):
            if not query:
                continue
            encoded = urllib.parse.quote(query)
            url = f"{self.base_url}/search/subject/{encoded}?type=1&responseGroup=large&max_results=5"
            data = self._get(url)
            if not data or not data.get("list"):
                continue

            candidates = data["list"]
            # Prefer exact name_cn match
            for item in candidates:
                if item.get("name_cn") == query:
                    return item
            # Partial match — pick the one with most ratings (most popular)
            partial = [
                item for item in candidates
                if item.get("name_cn") and
                   (query in item["name_cn"] or item["name_cn"] in query)
            ]
            if partial:
                return max(partial, key=lambda x: x.get("rating", {}).get("total", 0))
            # First result
            return candidates[0]

        return None

    def get_subject(self, subject_id):
        """Get detailed subject info including tags."""
        url = f"{self.base_url}/v0/subjects/{subject_id}"
        return self._get(url)


# ─── Metadata Mapping ───────────────────────────────────────────────────────

def map_bangumi_to_kavita(bgm_search, bgm_detail, existing_metadata, force=False):
    """Map Bangumi data to Kavita's SeriesMetadataDto format."""
    meta = existing_metadata.copy()

    # Summary
    summary = bgm_detail.get("summary", "") if bgm_detail else ""
    if not summary:
        summary = bgm_search.get("summary", "")

    score = bgm_search.get("rating", {}).get("score")
    rank = bgm_search.get("rank")
    name_jp = bgm_search.get("name", "")
    name_cn = bgm_search.get("name_cn", "")
    bgm_id = bgm_search.get("id")

    # Build enhanced summary
    parts = []
    if summary:
        parts.append(summary)

    score_line = []
    if score:
        score_line.append(f"Bangumi 评分: {score}")
    if rank:
        score_line.append(f"排名: #{rank}")
    if score_line:
        parts.append("\n\n" + " | ".join(score_line))

    enhanced_summary = "".join(parts)

    if enhanced_summary and (force or not meta.get("summaryLocked")):
        meta["summary"] = enhanced_summary
        meta["summaryLocked"] = True

    # Tags from Bangumi
    if bgm_detail and (force or not meta.get("tagsLocked")):
        bgm_tags = bgm_detail.get("tags", [])
        # Take top 10 tags by count
        top_tags = sorted(bgm_tags, key=lambda t: t.get("count", 0), reverse=True)[:10]
        if top_tags:
            existing_tags = meta.get("tags", [])
            existing_names = {t.get("title", "").lower() for t in existing_tags}
            for tag in top_tags:
                tag_name = tag.get("name", "")
                if tag_name.lower() not in existing_names:
                    existing_tags.append({"id": 0, "title": tag_name})
            meta["tags"] = existing_tags
            meta["tagsLocked"] = False

    # Genres from Bangumi infobox
    if bgm_detail and (force or not meta.get("genresLocked")):
        # Map Bangumi type to genre
        bgm_type = bgm_detail.get("platform", "")
        if bgm_type and bgm_type not in [g.get("title") for g in meta.get("genres", [])]:
            genres = meta.get("genres", [])
            genres.append({"id": 0, "title": bgm_type})
            meta["genres"] = genres

    # WebLinks - add/replace Bangumi link
    if bgm_id:
        bgm_url = f"https://bgm.tv/subject/{bgm_id}"
        existing_links = meta.get("webLinks", "") or ""
        # Remove any old bgm.tv links first
        other_links = [l for l in existing_links.split(",") if l.strip() and "bgm.tv" not in l]
        other_links.append(bgm_url)
        meta["webLinks"] = ",".join(other_links)

    # Writers/staff from Bangumi detail
    if bgm_detail and (force or not meta.get("writerLocked")):
        infobox = bgm_detail.get("infobox", [])
        # When forcing, start fresh to remove stale/merged entries
        writers = [] if force else meta.get("writers", [])
        existing_writer_names = {w.get("name", "").lower() for w in writers}

        def _split_names(text):
            """Split author string by common separators: 、× / +"""
            return [n.strip() for n in re.split(r'[、×/+]', text) if n.strip()]

        def _add_writer(name):
            if name and name.lower() not in existing_writer_names:
                writers.append({"id": 0, "name": name})
                existing_writer_names.add(name.lower())

        for info in infobox:
            key = info.get("key", "")
            if key in ("作者", "原作", "脚本"):
                val = info.get("value", "")
                if isinstance(val, str):
                    for name in _split_names(val):
                        _add_writer(name)
                elif isinstance(val, list):
                    for v in val:
                        name = v.get("v", "") if isinstance(v, dict) else str(v)
                        for n in _split_names(name):
                            _add_writer(n)

        if writers:
            meta["writers"] = writers
            meta["writerLocked"] = False

    return meta


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Sync Bangumi metadata to Kavita")
    parser.add_argument("--dry-run", action="store_true", help="预览模式，不写入")
    parser.add_argument("--series", type=str, help="只同步指定名称的系列")
    parser.add_argument("--force", action="store_true", help="即使已有摘要也强制覆盖")
    args = parser.parse_args()

    config = load_config()
    kc = config["kavita"]
    bc = config["bangumi"]

    # Init clients
    kavita = KavitaClient(kc["base_url"], kc["username"], kc["password"])
    bangumi = BangumiClient(bc["base_url"], bc["user_agent"], bc.get("rate_limit_delay", 0.4))
    overrides = load_overrides()
    if overrides:
        print(f"已加载 {len(overrides)} 条手动映射")

    # Get all series
    print("\n获取 Kavita 系列列表...")
    all_series = kavita.get_all_series()
    print(f"共 {len(all_series)} 个系列")

    if args.series:
        all_series = [s for s in all_series if args.series in s.get("name", "")]
        print(f"筛选后: {len(all_series)} 个")

    # Process each series
    stats = {"updated": 0, "skipped": 0, "not_found": 0, "error": 0}
    results = []

    for i, series in enumerate(all_series):
        sid = series["id"]
        name = series.get("name", "")
        localized = series.get("localizedName", "")

        print(f"\n[{i+1}/{len(all_series)}] {name}", end="")

        # Get existing metadata
        try:
            meta = kavita.get_series_metadata(sid)
        except Exception as e:
            print(f" ✗ 获取元数据失败: {e}")
            stats["error"] += 1
            continue

        # Skip if already has summary (unless --force)
        if meta.get("summary") and not args.force:
            existing = meta["summary"]
            if "Bangumi" in existing or "bgm.tv" in (meta.get("webLinks") or ""):
                print(f" → 已有 Bangumi 数据，跳过")
                stats["skipped"] += 1
                continue

        # Check overrides first, then search Bangumi
        override_id = overrides.get(name)
        bgm_result = None
        if override_id:
            # Direct lookup by Bangumi ID
            bgm_detail_direct = bangumi.get_subject(override_id)
            if bgm_detail_direct:
                bgm_result = {
                    "id": override_id,
                    "name_cn": bgm_detail_direct.get("name_cn", ""),
                    "name": bgm_detail_direct.get("name", ""),
                    "rating": bgm_detail_direct.get("rating", {}),
                    "rank": bgm_detail_direct.get("rank"),
                    "summary": bgm_detail_direct.get("summary", ""),
                }
                print(f" [映射]", end="")

        if not bgm_result:
            bgm_result = bangumi.search(name)

        # If not found, try localized name
        if not bgm_result and localized and localized != name:
            bgm_result = bangumi.search(localized)

        if not bgm_result:
            print(f" → Bangumi 未找到")
            stats["not_found"] += 1
            results.append({"name": name, "status": "not_found"})
            continue

        bgm_cn = bgm_result.get("name_cn", "")
        bgm_score = bgm_result.get("rating", {}).get("score", "—")
        bgm_id = bgm_result.get("id")
        print(f" → {bgm_cn} (评分: {bgm_score})", end="")

        # Get detailed info
        bgm_detail = bangumi.get_subject(bgm_id) if bgm_id else None

        # Map metadata
        updated_meta = map_bangumi_to_kavita(bgm_result, bgm_detail, meta, force=args.force)

        if args.dry_run:
            print(f" [DRY RUN]")
            results.append({
                "name": name, "status": "would_update",
                "bgm_name": bgm_cn, "score": bgm_score
            })
        else:
            try:
                kavita.update_series_metadata(updated_meta)
                print(f" ✓")
                stats["updated"] += 1
                results.append({
                    "name": name, "status": "updated",
                    "bgm_name": bgm_cn, "score": bgm_score
                })
            except Exception as e:
                print(f" ✗ 更新失败: {e}")
                stats["error"] += 1
                results.append({"name": name, "status": "error", "error": str(e)})

    # Summary
    print(f"\n{'='*60}")
    print(f"完成!")
    print(f"  更新: {stats['updated']}")
    print(f"  跳过: {stats['skipped']}")
    print(f"  未找到: {stats['not_found']}")
    print(f"  错误: {stats['error']}")

    # Save results
    results_path = Path(__file__).parent / "last_sync_results.json"
    with open(results_path, "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n详细结果已保存到 {results_path}")


if __name__ == "__main__":
    main()
