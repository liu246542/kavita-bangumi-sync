#!/usr/bin/env python3
"""
kavita-bangumi-sync: Fetch manga metadata from Bangumi and write to Kavita.

Usage:
    python3 sync.py                  # Sync all series
    python3 sync.py --dry-run        # Preview without writing
    python3 sync.py --series "名前"  # Sync a single series by name
"""

import argparse
import base64
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HTTP_TIMEOUT = 15

try:
    from opencc import OpenCC
    _t2s = OpenCC("t2s")
    def to_simplified(text):
        return _t2s.convert(text)
except ImportError:
    def to_simplified(text):
        return text


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
        resp = urllib.request.urlopen(req, timeout=HTTP_TIMEOUT)
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
            resp = urllib.request.urlopen(req, timeout=HTTP_TIMEOUT)
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

    def get_volumes(self, series_id):
        """Get all volumes (with chapters) for a series."""
        return self._request("GET", f"/api/series/volumes?seriesId={series_id}")

    def _fetch_image_b64(self, image_url):
        req = urllib.request.Request(image_url, headers={"User-Agent": "kavita-bangumi-sync/1.0"})
        resp = urllib.request.urlopen(req, timeout=HTTP_TIMEOUT)
        return base64.b64encode(resp.read()).decode()

    def upload_series_cover(self, series_id, image_url):
        """Upload a cover image for a series. Kavita expects pure base64, no data: prefix."""
        body = {"id": series_id, "url": self._fetch_image_b64(image_url), "lockCover": True}
        return self._request("POST", "/api/upload/series", body)

    def upload_chapter_cover(self, chapter_id, image_url):
        """Upload a cover image for a chapter/volume."""
        body = {"id": chapter_id, "url": self._fetch_image_b64(image_url), "lockCover": True}
        return self._request("POST", "/api/upload/chapter", body)

    def update_series_metadata(self, metadata_dto):
        """Update series metadata."""
        body = {"seriesMetadata": metadata_dto}
        return self._request("POST", "/api/series/metadata", body)

    def update_series_name(self, series_dto, localized_name):
        """Set localizedName (locked). Preserves sortName/coverImageLocked so the
        server's unconditional assignments don't wipe them."""
        body = {
            "id": series_dto["id"],
            "localizedName": localized_name,
            "localizedNameLocked": True,
            "sortName": series_dto.get("sortName"),
            "sortNameLocked": bool(series_dto.get("sortNameLocked", False)),
            "coverImageLocked": bool(series_dto.get("coverImageLocked", False)),
        }
        return self._request("POST", "/api/series/update", body)


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
            resp = urllib.request.urlopen(req, timeout=HTTP_TIMEOUT)
            return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            print(f"  ! Bangumi HTTP {e.code}: {url}", file=sys.stderr)
            return None
        except Exception as e:
            print(f"  ! Bangumi request failed ({type(e).__name__}): {url}", file=sys.stderr)
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
        # Remove edition/format suffixes at end
        cleaned = re.sub(r'[-\s]*(?:愛藏版|爱藏版|典藏版|完全版|新装版|新裝版|数码全彩|全彩)$', '', cleaned)
        # Remove trailing arc/part names: 公安篇, XX篇 (without parens)
        cleaned = re.sub(r'\s+\S*篇$', '', cleaned)
        return cleaned.strip()

    @staticmethod
    def normalize(text):
        """统一转简体 + 全角标点转半角 + 去末尾标点，用于比较。"""
        s = to_simplified(text)
        # 全角标点 → 半角
        s = s.translate(str.maketrans(
            "\uff01\uff1f\u3002\uff0c\u3001\uff1b\uff1a\u201c\u201d\u2018\u2019\uff08\uff09\u3010\u3011",
            '!?.,,;:""' + "''" + "()[]",
        ))
        # 去末尾标点
        s = re.sub(r'[!?.\s]+$', '', s)
        return s

    def search(self, title, strict=False):
        """Search Bangumi for a manga by title. Returns (match, confidence) or (None, None).

        confidence: "exact", "partial", "first" (only when not strict)
        In strict mode, "first" results are skipped (returns None).
        """
        # 统一转简体后搜索，去重保留顺序
        s_title = to_simplified(title)
        s_cleaned = to_simplified(self.clean_title(title))
        # 去掉末尾标点（Bangumi 搜索对 ! ? 等敏感）
        s_stripped = re.sub(r'[!?！？。.~～]+$', '', s_title)
        queries = dict.fromkeys([s_title, s_cleaned, s_stripped])
        for query in queries:
            if not query:
                continue
            encoded = urllib.parse.quote(query)
            url = f"{self.base_url}/search/subject/{encoded}?type=1&responseGroup=large&max_results=5"
            data = self._get(url)
            if not data or not data.get("list"):
                continue

            candidates = data["list"]
            query_n = self.normalize(query)

            # Prefer exact match (normalized)
            for item in candidates:
                cn_n = self.normalize(item.get("name_cn", ""))
                if cn_n == query_n:
                    return item, "exact"

            # Partial match — normalized comparison
            partial = []
            for item in candidates:
                cn_n = self.normalize(item.get("name_cn", ""))
                if not cn_n:
                    continue
                if query_n in cn_n or cn_n in query_n:
                    # Reject short queries (< 3 chars)
                    if len(query_n) < 3:
                        continue
                    # When query is substring of result, only accept prefix/suffix
                    if query_n in cn_n and cn_n not in query_n:
                        if not (cn_n.startswith(query_n) or cn_n.endswith(query_n)):
                            continue
                    partial.append(item)
            if partial:
                return max(partial, key=lambda x: x.get("rating", {}).get("total", 0)), "partial"

            # First result — low confidence
            if not strict:
                return candidates[0], "first"

        return None, None

    def get_subject(self, subject_id):
        """Get detailed subject info including tags."""
        url = f"{self.base_url}/v0/subjects/{subject_id}"
        return self._get(url)

    def get_volumes(self, subject_id):
        """Get list of related 单行本 (volumes) for a subject, sorted by volume number."""
        url = f"{self.base_url}/v0/subjects/{subject_id}/subjects"
        data = self._get(url)
        if not data:
            return []
        volumes = [item for item in data if item.get("relation") == "单行本"]
        # Sort by volume number extracted from name like "大ダーク (1)" or "大ダーク（1）"
        def vol_num(item):
            name = item.get("name", "") or item.get("name_cn", "")
            m = re.search(r'[(（](\d+)[)）]', name)
            return int(m.group(1)) if m else 0
        volumes.sort(key=vol_num)
        return volumes


# ─── Metadata Mapping ───────────────────────────────────────────────────────

# Bangumi tags that don't help readers (years, publishers, meta categories)
_NOISE_TAG_RE = re.compile(
    r'^\d{4}$'
    r'|^漫画$|^漫画系列$|^manga$'
    r'|^日本$|^中国$'
    r'|^集英社$|^講談社$|^小學館$|^角川$|^スクエニ$'
    r'|^少年漫画$|^少女漫画$|^青年漫画$'
    r'|^已完结$|^连载中$',
    re.IGNORECASE,
)


def map_bangumi_to_kavita(bgm_search, bgm_detail, existing_metadata, force=False):
    """Map Bangumi data to Kavita's SeriesMetadataDto format."""
    meta = existing_metadata.copy()

    # Summary
    summary = bgm_detail.get("summary", "") if bgm_detail else ""
    if not summary:
        summary = bgm_search.get("summary", "")

    score = bgm_search.get("rating", {}).get("score")
    rank = bgm_search.get("rank")
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

    # Tags from Bangumi (filtered)
    if bgm_detail and (force or not meta.get("tagsLocked")):
        bgm_tags = bgm_detail.get("tags", [])
        filtered = [t for t in bgm_tags if not _NOISE_TAG_RE.match(t.get("name", ""))]
        top_tags = sorted(filtered, key=lambda t: t.get("count", 0), reverse=True)[:10]
        if top_tags:
            # When forcing, start fresh to remove old noise tags
            existing_tags = [] if force else meta.get("tags", [])
            # Match Kavita's normalization: keep \p{L} + 0-9 + special, lowercase
            _keep = set('+!＊！＋')
            def _norm_tag(s):
                return ''.join(c for c in s if c.isalpha() or c.isdigit() or c in _keep).lower()
            existing_names = {_norm_tag(t.get("title", "")) for t in existing_tags}
            for tag in top_tags:
                tag_name = tag.get("name", "")
                if _norm_tag(tag_name) not in existing_names:
                    existing_tags.append({"id": 0, "title": tag_name})
                    existing_names.add(_norm_tag(tag_name))
            meta["tags"] = existing_tags
            meta["tagsLocked"] = True

    # Genres from Bangumi infobox
    if bgm_detail and (force or not meta.get("genresLocked")):
        # Map Bangumi type to genre
        bgm_type = bgm_detail.get("platform", "")
        if bgm_type and bgm_type not in [g.get("title") for g in meta.get("genres", [])]:
            genres = meta.get("genres", [])
            genres.append({"id": 0, "title": bgm_type})
            meta["genres"] = genres
            meta["genresLocked"] = True

    # Release year
    if bgm_detail and (force or not meta.get("releaseYearLocked")):
        date_str = bgm_detail.get("date", "")
        if date_str:
            try:
                meta["releaseYear"] = int(date_str[:4])
                meta["releaseYearLocked"] = True
            except (ValueError, IndexError):
                pass

    # Publication status from infobox
    # publicationStatus: 0=Ongoing, 1=Hiatus, 2=Completed, 3=Cancelled, 4=Ended
    # Only lock Completed when end date is explicitly present — Ongoing is a guess
    # (Bangumi often omits 结束 for finished works), so leave unlocked for Kavita/user.
    if bgm_detail and (force or not meta.get("publicationStatusLocked")):
        infobox = bgm_detail.get("infobox", [])
        end_date = None
        for info in infobox:
            if info.get("key") == "结束":
                end_date = info.get("value")
                break
        if end_date:
            meta["publicationStatus"] = 2
            meta["publicationStatusLocked"] = True
        elif meta.get("publicationStatus") is None:
            meta["publicationStatus"] = 0

    # Age rating from nsfw flag
    if bgm_detail:
        if bgm_detail.get("nsfw"):
            meta["ageRating"] = 4  # X18+
            meta["ageRatingLocked"] = True

    # WebLinks - add/replace Bangumi link.
    # Note: Kavita's SeriesMetadataDto has no webLinksLocked field, but the
    # scan loop doesn't overwrite webLinks (they aren't read from ComicInfo).
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
            meta["writerLocked"] = True

    return meta


# ─── Main ────────────────────────────────────────────────────────────────────

def _vol_sort_key(v):
    name = v.get("name", "")
    return int(name) if name.isdigit() else 0


def _filter_to_overrides(all_series, overrides):
    """Keep only series whose name is an overrides.json key.

    Warn about override keys that don't match any Kavita series — these are
    usually typos or series that were renamed on the Kavita side.
    """
    override_names = set(overrides.keys())
    kavita_names = {s.get("name", "") for s in all_series}
    missing = sorted(override_names - kavita_names)
    if missing:
        print(f"\n⚠ overrides.json 中有 {len(missing)} 条未在 Kavita 中找到对应系列:")
        for name in missing:
            print(f"  - {name}")
        print(f"  (可能是拼写错误，或 Kavita 里已重命名；这些条目会被跳过)")
    return [s for s in all_series if s.get("name", "") in override_names]


def _print_review_summary(results):
    """Highlight entries that need manual verification:
    - first-confidence matches (Bangumi's top search hit, semantically unchecked)
    - not_found entries (no match at all — candidate for overrides.json)
    """
    first_matches = [r for r in results if r.get("confidence") == "first"]
    not_found = [r for r in results if r.get("status") == "not_found"]

    if not first_matches and not not_found:
        return

    print(f"\n需要人工核对:")
    for r in first_matches:
        print(f"  ⚠ 低置信度  {r['name']}")
        print(f"              → {r.get('bgm_name', '')}  {r.get('bgm_url', '')}")
    for r in not_found:
        print(f"  ? 未找到    {r['name']}")

    print(f"\n  不对/未找到的条目，手动在 Bangumi 查到 subject id 后加入 overrides.json，")
    print(f"  然后用 --force --series \"<名称>\" 单独重跑该条。")


def _do_cover_update(args, kavita, bangumi, overrides):
    """Handle --cover and/or --cover-volumes. Both share the Bangumi lookup."""
    if not args.series:
        print("错误: --cover / --cover-volumes 需要配合 --series 使用")
        sys.exit(1)

    matches = [s for s in kavita.get_all_series() if args.series in s.get("name", "")]
    if not matches:
        print(f"未找到匹配 \"{args.series}\" 的系列")
        sys.exit(1)

    for series in matches:
        name = series.get("name", "")
        sid = series["id"]
        print(f"[{name}]")

        # Resolve Bangumi subject id (override takes precedence over strict search)
        override_id = overrides.get(name)
        if override_id:
            bgm_id = override_id
        else:
            bgm_result, _ = bangumi.search(name, strict=True)
            if not bgm_result:
                print(f"  → Bangumi 未找到")
                continue
            bgm_id = bgm_result["id"]

        bgm_detail = bangumi.get_subject(bgm_id)

        if args.cover:
            images = bgm_detail.get("images", {}) if bgm_detail else {}
            cover_url = images.get("large") or images.get("common")
            if not cover_url:
                print(f"  [封面] 无封面图")
            elif args.dry_run:
                print(f"  [封面] → {cover_url} [DRY RUN]")
            else:
                try:
                    kavita.upload_series_cover(sid, cover_url)
                    print(f"  [封面] ✓ 已更新")
                except Exception as e:
                    print(f"  [封面] ✗ {e}")

        if args.cover_volumes:
            bgm_volumes = bangumi.get_volumes(bgm_id)
            if not bgm_volumes:
                print(f"  [卷封面] Bangumi 没有单行本数据")
                continue

            kavita_volumes = sorted(kavita.get_volumes(sid), key=_vol_sort_key)
            print(f"  [卷封面] Bangumi {len(bgm_volumes)} 卷, Kavita {len(kavita_volumes)} 卷")

            for i, kv in enumerate(kavita_volumes):
                if i >= len(bgm_volumes):
                    break
                bv = bgm_volumes[i]
                bv_detail = bangumi.get_subject(bv["id"])
                if not bv_detail:
                    continue
                images = bv_detail.get("images", {})
                cover_url = images.get("large") or images.get("common")
                if not cover_url:
                    print(f"    Vol.{kv.get('name')} → 无封面")
                    continue

                chapters = kv.get("chapters", [])
                if not chapters:
                    continue
                chapter_id = chapters[0]["id"]

                if args.dry_run:
                    print(f"    Vol.{kv.get('name')} ← {bv.get('name')} [DRY RUN]")
                    continue

                try:
                    kavita.upload_chapter_cover(chapter_id, cover_url)
                    print(f"    Vol.{kv.get('name')} ✓ ← {bv.get('name')}")
                except Exception as e:
                    print(f"    Vol.{kv.get('name')} ✗ {e}")


def _resolve_bangumi(name, localized, overrides, bangumi, strict):
    """Return (bgm_result, bgm_detail, confidence) or (None, None, None).

    Override path fetches get_subject once and reuses it as both summary
    and detail — the v0 endpoint is a superset of what search returns.
    """
    override_id = overrides.get(name)
    if override_id:
        detail = bangumi.get_subject(override_id)
        if detail:
            return detail, detail, "override"

    result, confidence = bangumi.search(name, strict=strict)
    if not result and localized and localized != name:
        result, confidence = bangumi.search(localized, strict=strict)
    if not result:
        return None, None, None

    detail = bangumi.get_subject(result["id"]) if result.get("id") else None
    return result, detail, confidence


def _do_metadata_sync(args, kavita, bangumi, overrides):
    """Default metadata sync flow over all (or --series-filtered) Kavita series."""
    print("\n获取 Kavita 系列列表...")
    all_series = kavita.get_all_series()
    print(f"共 {len(all_series)} 个系列")

    if args.overrides_only:
        all_series = _filter_to_overrides(all_series, overrides)
        print(f"overrides 过滤后: {len(all_series)} 个")

    if args.series:
        all_series = [s for s in all_series if args.series in s.get("name", "")]
        print(f"--series 筛选后: {len(all_series)} 个")

    stats = {"updated": 0, "skipped": 0, "not_found": 0, "error": 0}
    results = []

    for i, series in enumerate(all_series):
        sid = series["id"]
        name = series.get("name", "")
        localized = series.get("localizedName", "")

        print(f"\n[{i+1}/{len(all_series)}] {name}", end="")

        try:
            meta = kavita.get_series_metadata(sid)
        except Exception as e:
            print(f" ✗ 获取元数据失败: {e}")
            stats["error"] += 1
            continue

        # Skip already-synced series (bgm.tv link in webLinks is the reliable marker)
        if not args.force and "bgm.tv" in (meta.get("webLinks") or ""):
            print(f" → 已有 Bangumi 数据，跳过")
            stats["skipped"] += 1
            continue

        bgm_result, bgm_detail, confidence = _resolve_bangumi(
            name, localized, overrides, bangumi, strict=args.strict
        )
        if not bgm_result:
            print(f" → Bangumi 未找到")
            stats["not_found"] += 1
            results.append({"name": name, "status": "not_found"})
            continue

        if confidence == "override":
            print(f" [映射]", end="")
        bgm_cn = bgm_result.get("name_cn", "")
        bgm_score = bgm_result.get("rating", {}).get("score", "—")
        bgm_id_val = bgm_result.get("id")
        conf_tag = f" [{confidence}]" if confidence not in ("exact", "override") else ""
        print(f" → {bgm_cn} (评分: {bgm_score}){conf_tag}", end="")

        updated_meta = map_bangumi_to_kavita(bgm_result, bgm_detail, meta, force=args.force)

        entry = {
            "name": name,
            "bgm_name": bgm_cn,
            "bgm_id": bgm_id_val,
            "bgm_url": f"https://bgm.tv/subject/{bgm_id_val}" if bgm_id_val else None,
            "confidence": confidence,
            "score": bgm_score,
        }
        if args.dry_run:
            print(f" [DRY RUN]")
            entry["status"] = "would_update"
        else:
            try:
                kavita.update_series_metadata(updated_meta)
                name_cn = (bgm_result.get("name_cn") or "").strip()
                if name_cn and name_cn != (series.get("localizedName") or ""):
                    try:
                        kavita.update_series_name(series, name_cn)
                    except Exception as e:
                        print(f" ⚠ localizedName 失败: {e}", end="")
                print(f" ✓")
                stats["updated"] += 1
                entry["status"] = "updated"
            except Exception as e:
                print(f" ✗ 更新失败: {e}")
                stats["error"] += 1
                entry["status"] = "error"
                entry["error"] = str(e)
        results.append(entry)

    print(f"\n{'='*60}")
    print(f"完成!")
    print(f"  更新: {stats['updated']}")
    print(f"  跳过: {stats['skipped']}")
    print(f"  未找到: {stats['not_found']}")
    print(f"  错误: {stats['error']}")

    _print_review_summary(results)

    # Preserve the last real sync's audit record when doing a dry-run
    if args.dry_run:
        print(f"\n[DRY RUN] 未写入 last_sync_results.json")
    else:
        results_path = Path(__file__).parent / "last_sync_results.json"
        with open(results_path, "w") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"\n详细结果已保存到 {results_path}")


def _do_review():
    """Read last_sync_results.json and print the review summary, no network."""
    results_path = Path(__file__).parent / "last_sync_results.json"
    if not results_path.exists():
        print(f"没有 {results_path.name}，请先运行一次 sync")
        sys.exit(1)
    with open(results_path) as f:
        results = json.load(f)
    print(f"读取 {len(results)} 条结果 from {results_path.name}")
    _print_review_summary(results)


def main():
    parser = argparse.ArgumentParser(description="Sync Bangumi metadata to Kavita")
    parser.add_argument("--dry-run", action="store_true", help="预览模式，不写入")
    parser.add_argument("--series", type=str, help="只同步指定名称的系列")
    parser.add_argument("--force", action="store_true", help="即使已有摘要也强制覆盖")
    parser.add_argument("--strict", action="store_true", help="只写入精确/部分匹配，跳过低置信度结果")
    parser.add_argument("--cover", action="store_true", help="用 Bangumi 封面替换指定系列的封面（需配合 --series）")
    parser.add_argument("--cover-volumes", action="store_true", help="用 Bangumi 单行本封面替换每卷封面（需配合 --series）")
    parser.add_argument("--review", action="store_true", help="读取上次同步结果，列出需人工核对的条目（不请求网络）")
    parser.add_argument("--overrides-only", action="store_true", help="只同步 overrides.json 里列出的系列（适合批量修正误匹配）")
    args = parser.parse_args()

    if args.review:
        _do_review()
        return

    if args.overrides_only and (args.cover or args.cover_volumes):
        print("错误: --overrides-only 只用于元数据同步，不能与 --cover / --cover-volumes 组合")
        sys.exit(1)

    config = load_config()
    kavita = KavitaClient(config["kavita"]["base_url"],
                          config["kavita"]["username"],
                          config["kavita"]["password"])
    bangumi = BangumiClient(config["bangumi"]["base_url"],
                            config["bangumi"]["user_agent"],
                            config["bangumi"].get("rate_limit_delay", 0.4))
    overrides = load_overrides()
    if overrides:
        print(f"已加载 {len(overrides)} 条手动映射")

    if args.overrides_only and not overrides:
        print("错误: --overrides-only 需要非空的 overrides.json")
        sys.exit(1)

    if args.cover or args.cover_volumes:
        _do_cover_update(args, kavita, bangumi, overrides)
    else:
        _do_metadata_sync(args, kavita, bangumi, overrides)


if __name__ == "__main__":
    main()
