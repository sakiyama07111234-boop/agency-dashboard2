#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
代理店案件スクレイパー v2
対象サイト: b-seeds.com, dairitenboshu.com, kkhashi.com, fc-hikaku.net
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, fields
from datetime import datetime, UTC
from typing import List, Optional, Set
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ── 設定 ─────────────────────────────────────────
DB_PATH = "agency_jobs.db"
CSV_PATH = "agency_jobs_latest.csv"

REQUEST_TIMEOUT = 25
REQUEST_SLEEP_SEC = 0.6
MAX_DETAIL_PAGES = 500          # 1サイトあたり最大
MAX_LIST_PAGES = 30             # ページネーション上限

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)


# ── ユーティリティ ────────────────────────────────
def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_space(text: str | None) -> str:
    if not text:
        return ""
    text = re.sub(r"[\u3000\t\r\n]+", " ", str(text))
    return re.sub(r"\s+", " ", text).strip()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def clean_url(url: str) -> str:
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc, p.path, "", "", ""))


def extract_text(el) -> str:
    return normalize_space(el.get_text(" ", strip=True)) if el else ""


def clean_company_name(text: str) -> str:
    """会社名を正規化。法人名を優先抽出し、長文やキーワード汚染を除外。"""
    if not text:
        return ""
    text = normalize_space(text)

    # ラベル除去
    for label in ["募集企業", "会社名", "企業名", "事業者名", "運営会社"]:
        text = re.sub(rf"^{re.escape(label)}\s*[:：]?\s*", "", text)
    text = normalize_space(text)

    if len(text) > 60:
        return ""

    # 括弧内の法人名
    for p in [r"（((?:株式|合同|有限)会社[^）]{1,40})）",
              r"\(((?:株式|合同|有限)会社[^)]{1,40})\)"]:
        m = re.search(p, text)
        if m:
            return normalize_space(m.group(1))

    # 法人名パターン
    corp = [
        r"(株式会社[^\s、。|｜]{1,40})", r"(合同会社[^\s、。|｜]{1,40})",
        r"(有限会社[^\s、。|｜]{1,40})", r"([^\s、。|｜]{1,40}株式会社)",
        r"([^\s、。|｜]{1,40}合同会社)", r"([^\s、。|｜]{1,40}有限会社)",
    ]
    for p in corp:
        m = re.search(p, text)
        if m:
            return normalize_space(m.group(1))

    # NGキーワード含有チェック
    ng = ["代理店", "パートナー", "募集", "報酬", "手数料", "インセンティブ",
          "契約", "営業", "ストック", "収益", "ビジネス", "ご案内", "提供", "実績"]
    if any(k in text for k in ng):
        return ""

    return text if len(text) <= 30 else ""


# ── データモデル ──────────────────────────────────
@dataclass
class JobRecord:
    site_name: str
    source_url: str
    listing_url: str
    detail_url: str
    title: str = ""
    company_name: str = ""
    category: str = ""
    contract_type: str = ""
    reward_text: str = ""
    initial_cost: str = ""
    recurring_revenue: str = ""
    area: str = ""
    target_persona: str = ""
    summary: str = ""
    features: str = ""
    published_at: str = ""
    raw_json: str = ""
    status: str = "active"
    hash_key: str = ""
    content_hash: str = ""
    first_seen_at: str = ""
    last_seen_at: str = ""
    scraped_at: str = ""

    def normalize(self) -> "JobRecord":
        text_fields = [
            "site_name", "source_url", "listing_url", "detail_url", "title",
            "company_name", "category", "contract_type", "reward_text",
            "initial_cost", "recurring_revenue", "area", "target_persona",
            "summary", "features", "published_at", "raw_json", "status",
        ]
        for key in text_fields:
            setattr(self, key, normalize_space(getattr(self, key, "")))

        self.company_name = clean_company_name(self.company_name)

        now = utc_now_iso()
        self.scraped_at = self.scraped_at or now
        self.first_seen_at = self.first_seen_at or now
        self.last_seen_at = self.last_seen_at or now

        self.hash_key = sha256_text(
            "||".join([self.site_name, self.detail_url or self.title,
                       self.title, self.company_name])
        )
        self.content_hash = sha256_text(json.dumps({
            "title": self.title, "company_name": self.company_name,
            "category": self.category, "contract_type": self.contract_type,
            "reward_text": self.reward_text, "initial_cost": self.initial_cost,
            "recurring_revenue": self.recurring_revenue, "area": self.area,
            "target_persona": self.target_persona, "summary": self.summary,
            "features": self.features, "published_at": self.published_at,
            "detail_url": self.detail_url, "status": self.status,
        }, ensure_ascii=False, sort_keys=True))
        return self


# ── HTTP ─────────────────────────────────────────
class HttpClient:
    def __init__(self) -> None:
        self.session = requests.Session()
        retry = Retry(total=4, backoff_factor=1.0,
                      status_forcelist=(429, 500, 502, 503, 504),
                      allowed_methods=frozenset(["GET"]),
                      raise_on_status=False)
        adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        self.session.headers.update(HEADERS)

    def get(self, url: str) -> str:
        logger.info("GET %s", url)
        r = self.session.get(url, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        if not r.encoding:
            r.encoding = r.apparent_encoding or "utf-8"
        time.sleep(REQUEST_SLEEP_SEC)
        return r.text

    def soup(self, url: str) -> BeautifulSoup:
        return BeautifulSoup(self.get(url), "html.parser")


# ── DB ───────────────────────────────────────────
class JobRepository:
    COLUMNS = [
        "hash_key", "site_name", "source_url", "listing_url", "detail_url",
        "title", "company_name", "category", "contract_type", "reward_text",
        "initial_cost", "recurring_revenue", "area", "target_persona",
        "summary", "features", "published_at", "raw_json", "status",
        "content_hash", "first_seen_at", "last_seen_at", "scraped_at",
    ]

    def __init__(self, db_path: str) -> None:
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._create()

    def _create(self) -> None:
        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS agency_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hash_key TEXT NOT NULL UNIQUE,
            site_name TEXT NOT NULL,
            source_url TEXT NOT NULL,
            listing_url TEXT NOT NULL,
            detail_url TEXT NOT NULL,
            title TEXT, company_name TEXT, category TEXT,
            contract_type TEXT, reward_text TEXT, initial_cost TEXT,
            recurring_revenue TEXT, area TEXT, target_persona TEXT,
            summary TEXT, features TEXT, published_at TEXT,
            raw_json TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            content_hash TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            scraped_at TEXT NOT NULL
        )""")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agency_jobs_site ON agency_jobs(site_name)")
        self.conn.commit()

    def upsert(self, job: JobRecord) -> None:
        row = self.conn.execute(
            "SELECT first_seen_at FROM agency_jobs WHERE hash_key = ?",
            (job.hash_key,)).fetchone()

        vals = [getattr(job, c) for c in self.COLUMNS]
        if row is None:
            ph = ",".join("?" * len(self.COLUMNS))
            self.conn.execute(
                f"INSERT INTO agency_jobs ({','.join(self.COLUMNS)}) VALUES ({ph})", vals)
        else:
            sets = ",".join(f"{c}=?" for c in self.COLUMNS if c != "hash_key")
            update_vals = [getattr(job, c) if c != "first_seen_at" else row["first_seen_at"]
                           for c in self.COLUMNS if c != "hash_key"]
            update_vals.append(job.hash_key)
            self.conn.execute(
                f"UPDATE agency_jobs SET {sets} WHERE hash_key=?", update_vals)
        self.conn.commit()

    def mark_missing_inactive(self, site_name: str, active_keys: List[str]) -> None:
        if not active_keys:
            return
        ph = ",".join("?" * len(active_keys))
        self.conn.execute(
            f"UPDATE agency_jobs SET status='inactive' "
            f"WHERE site_name=? AND hash_key NOT IN ({ph}) AND status!='inactive'",
            [site_name] + active_keys)
        self.conn.commit()

    def export_csv(self, csv_path: str) -> None:
        cols = [c for c in self.COLUMNS if c not in ("hash_key", "content_hash", "raw_json")]
        rows = self.conn.execute(
            f"SELECT {','.join(cols)} FROM agency_jobs ORDER BY site_name, title"
        ).fetchall()
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for r in rows:
                w.writerow(dict(r))

    def close(self) -> None:
        self.conn.close()


# ── ヘルパー: 一覧→詳細リンク収集 ──────────────────
def collect_links(client: HttpClient,
                  start_urls: List[str],
                  detail_pattern: re.Pattern,
                  base_domain: str,
                  next_page_pattern: Optional[re.Pattern] = None,
                  max_pages: int = MAX_LIST_PAGES,
                  max_links: int = MAX_DETAIL_PAGES) -> List[str]:
    """一覧ページからdetail_patternにマッチするリンクを重複除去で収集"""
    seen: Set[str] = set()
    result: List[str] = []
    queue = list(start_urls)
    pages_fetched = 0

    while queue and pages_fetched < max_pages and len(result) < max_links:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        pages_fetched += 1

        try:
            soup = client.soup(url)
        except Exception as e:
            logger.warning("Skip list page %s: %s", url, e)
            continue

        for a in soup.find_all("a", href=True):
            href = urljoin(url, a["href"])
            href = clean_url(href)
            if base_domain not in href:
                continue
            if detail_pattern.search(href) and href not in seen:
                result.append(href)
                seen.add(href)
                if len(result) >= max_links:
                    break

        # ページネーション
        if next_page_pattern:
            for a in soup.find_all("a", href=True):
                href = urljoin(url, a["href"])
                href = clean_url(href)
                if next_page_pattern.search(href) and href not in seen:
                    queue.append(href)

    logger.info("Collected %d detail links from %s", len(result), base_domain)
    return result


# ── テーブルからキー:値を抽出 ──────────────────────
def parse_table_pairs(soup: BeautifulSoup) -> dict:
    """th/td ペアをdictで返す"""
    pairs = {}
    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            th = tr.find("th")
            td = tr.find("td")
            if th and td:
                key = extract_text(th)
                val = extract_text(td)
                if key:
                    pairs[key] = val
    return pairs


def find_text_by_label(soup: BeautifulSoup, *labels: str) -> str:
    """ページ内からlabelに近いテキストを探す"""
    for label in labels:
        # dt/dd パターン
        for dt in soup.find_all(["dt", "th", "label", "span", "div", "p"]):
            if label in extract_text(dt):
                nxt = dt.find_next_sibling(["dd", "td", "span", "div", "p"])
                if nxt:
                    return extract_text(nxt)
    return ""


# ══════════════════════════════════════════════════
# サイト別アダプター
# ══════════════════════════════════════════════════

class BaseSiteAdapter:
    site_name: str = ""

    def __init__(self, client: HttpClient):
        self.client = client

    def run(self) -> List[JobRecord]:
        raise NotImplementedError


# ── b-seeds.com ──────────────────────────────────
class BSeedsAdapter(BaseSiteAdapter):
    site_name = "b-seeds"
    START_URLS = [
        "https://b-seeds.com/",
        "https://b-seeds.com/user-type/individual",
        "https://b-seeds.com/region/all",
    ]
    DETAIL_RE = re.compile(r"^https://b-seeds\.com/[a-z0-9]+-[a-z0-9]+")
    # b-seedsの詳細URLは /XX-SLUG 形式 (2文字プレフィックス + ハイフン + スラッグ)
    DETAIL_RE2 = re.compile(r"https://b-seeds\.com/[a-z]{1,4}-[a-zA-Z0-9]")
    SKIP_PATHS = {"user-edit", "user-login", "request-list", "contact",
                  "introduce", "customervoice", "concierge", "expo",
                  "news", "top20", "search", "genre", "service",
                  "user-type", "region", "style", "feature"}

    def _is_detail_url(self, href: str) -> bool:
        parsed = urlparse(href)
        if parsed.netloc and "b-seeds.com" not in parsed.netloc:
            return False
        path = parsed.path.strip("/")
        if not path or "/" in path:
            return False
        if path.split("-")[0] in self.SKIP_PATHS:
            return False
        # 詳細は通常 2~4文字プレフィックス + ハイフン + 英数字slug
        if "-" not in path:
            return False
        return True

    def run(self) -> List[JobRecord]:
        logger.info("START %s", self.site_name)
        seen: Set[str] = set()
        detail_urls: List[str] = []

        for start_url in self.START_URLS:
            try:
                soup = self.client.soup(start_url)
            except Exception as e:
                logger.warning("Skip %s: %s", start_url, e)
                continue

            for a in soup.find_all("a", href=True):
                href = urljoin(start_url, a["href"])
                href = clean_url(href)
                if self._is_detail_url(href) and href not in seen:
                    seen.add(href)
                    detail_urls.append(href)

        logger.info("%s: %d detail links", self.site_name, len(detail_urls))

        jobs: List[JobRecord] = []
        for url in detail_urls[:MAX_DETAIL_PAGES]:
            try:
                soup = self.client.soup(url)
            except Exception as e:
                logger.warning("Skip detail %s: %s", url, e)
                continue

            title = extract_text(soup.find("h1")) or extract_text(soup.find("title"))
            if not title or len(title) < 5:
                continue

            page_text = extract_text(soup)
            pairs = parse_table_pairs(soup)

            # 会社名: 専用クラス → テーブル → ラベル検索の優先順で抽出
            company = ""
            # 1. p.agency-fv__doc--company ("募集企業：XXX" 形式)
            company_el = soup.find("p", class_="agency-fv__doc--company")
            if company_el:
                company_text = extract_text(company_el)
                company = re.sub(r"^募集企業\s*[：:]\s*", "", company_text)
            # 2. テーブルから (企業名 / 募集企業 / 会社名)
            if not company:
                company = (pairs.get("企業名", "") or pairs.get("募集企業", "") or
                           pairs.get("会社名", ""))
            # 3. ラベル近傍テキスト
            if not company:
                company = find_text_by_label(soup, "企業名", "募集企業", "会社名")

            category = pairs.get("商材", "") or pairs.get("業種", "")
            contract_type = pairs.get("契約形態", "") or pairs.get("募集形態", "")
            reward = pairs.get("報酬", "") or pairs.get("収益", "")
            initial_cost = pairs.get("初期費用", "")
            area = pairs.get("募集地域", "") or pairs.get("対象エリア", "")
            target = pairs.get("募集対象", "")

            # 概要テキスト抽出
            summary = ""
            for tag in soup.find_all(["p", "div"], class_=lambda c: c and "description" in str(c).lower()):
                summary = extract_text(tag)
                if summary:
                    break
            if not summary:
                meta = soup.find("meta", attrs={"name": "description"})
                if meta and meta.get("content"):
                    summary = normalize_space(meta["content"])

            recurring = ""
            if re.search(r"(ストック|継続|毎月|月額)", f"{reward} {page_text[:500]}"):
                recurring = "あり"

            job = JobRecord(
                site_name=self.site_name,
                source_url="https://b-seeds.com/",
                listing_url="https://b-seeds.com/",
                detail_url=url,
                title=title,
                company_name=company,
                category=category,
                contract_type=contract_type,
                reward_text=reward[:300],
                initial_cost=initial_cost[:100],
                recurring_revenue=recurring,
                area=area[:100],
                target_persona=target[:100],
                summary=summary[:500],
            ).normalize()
            jobs.append(job)

        logger.info("%s: %d jobs scraped", self.site_name, len(jobs))
        return jobs


# ── dairitenboshu.com ────────────────────────────
class DairitenboshuAdapter(BaseSiteAdapter):
    site_name = "dairitenboshu"
    START_URLS = [
        "https://dairitenboshu.com/",
        "https://dairitenboshu.com/search/oubos_100",
        "https://dairitenboshu.com/search/area_tokyo",
    ]
    # 詳細ページは /detail/数字 パターン
    DETAIL_RE = re.compile(r"/detail/\d+")
    NEXT_RE = re.compile(r"/search/.*page=\d+")

    def run(self) -> List[JobRecord]:
        logger.info("START %s", self.site_name)
        seen: Set[str] = set()
        detail_urls: List[str] = []
        queue = list(self.START_URLS)
        pages_fetched = 0

        while queue and pages_fetched < MAX_LIST_PAGES:
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            pages_fetched += 1

            try:
                soup = self.client.soup(url)
            except Exception as e:
                logger.warning("Skip %s: %s", url, e)
                continue

            for a in soup.find_all("a", href=True):
                href = urljoin(url, a["href"])
                href = clean_url(href)
                if "dairitenboshu.com" not in href:
                    continue

                if self.DETAIL_RE.search(href) and href not in seen:
                    # 除外: /column/detail/ は記事
                    if "/column/" not in href:
                        seen.add(href)
                        detail_urls.append(href)

                # ページネーション
                if self.NEXT_RE.search(a["href"]) and href not in seen:
                    queue.append(href)

        logger.info("%s: %d detail links", self.site_name, len(detail_urls))

        jobs: List[JobRecord] = []
        for url in detail_urls[:MAX_DETAIL_PAGES]:
            try:
                soup = self.client.soup(url)
            except Exception as e:
                logger.warning("Skip detail %s: %s", url, e)
                continue

            title = extract_text(soup.find("h1")) or extract_text(soup.find("title"))
            if not title or len(title) < 5:
                continue

            pairs = parse_table_pairs(soup)
            page_text = extract_text(soup)

            company = (pairs.get("募集企業", "") or pairs.get("会社名", "") or
                       find_text_by_label(soup, "募集企業", "会社名"))
            category = pairs.get("業種", "") or pairs.get("カテゴリ", "")
            area = pairs.get("募集地域", "") or pairs.get("対象エリア", "")
            target = pairs.get("募集対象", "")
            initial_cost = pairs.get("初期費用", "")
            contract_type = pairs.get("募集形態", "") or pairs.get("契約形態", "")
            reward = pairs.get("報酬", "") or pairs.get("収益", "")

            summary = ""
            meta = soup.find("meta", attrs={"name": "description"})
            if meta and meta.get("content"):
                summary = normalize_space(meta["content"])

            recurring = ""
            if re.search(r"(ストック|継続|毎月|月額)", f"{reward} {page_text[:500]}"):
                recurring = "あり"

            features_parts = []
            for badge in soup.find_all(class_=lambda c: c and any(
                    x in str(c).lower() for x in ["tag", "badge", "merit", "label"])):
                t = extract_text(badge)
                if t and len(t) < 30:
                    features_parts.append(t)

            job = JobRecord(
                site_name=self.site_name,
                source_url="https://dairitenboshu.com/",
                listing_url="https://dairitenboshu.com/",
                detail_url=url,
                title=title,
                company_name=company,
                category=category,
                contract_type=contract_type,
                reward_text=reward[:300],
                initial_cost=initial_cost[:100],
                recurring_revenue=recurring,
                area=area[:100],
                target_persona=target[:100],
                summary=summary[:500],
                features=" / ".join(features_parts[:10]),
            ).normalize()
            jobs.append(job)

        logger.info("%s: %d jobs scraped", self.site_name, len(jobs))
        return jobs


# ── kkhashi.com (カケハシ) ───────────────────────
class KakehashiAdapter(BaseSiteAdapter):
    site_name = "kkhashi"
    START_URLS = [
        "https://www.kkhashi.com/matters",
    ]
    DETAIL_RE = re.compile(r"/matters/detail/\d+")

    def run(self) -> List[JobRecord]:
        logger.info("START %s", self.site_name)
        seen: Set[str] = set()
        detail_urls: List[str] = []
        queue = list(self.START_URLS)
        pages_fetched = 0

        while queue and pages_fetched < MAX_LIST_PAGES:
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            pages_fetched += 1

            try:
                soup = self.client.soup(url)
            except Exception as e:
                logger.warning("Skip %s: %s", url, e)
                continue

            for a in soup.find_all("a", href=True):
                href = urljoin(url, a["href"])
                href = clean_url(href)
                if "kkhashi.com" not in href:
                    continue
                if self.DETAIL_RE.search(href) and href not in seen:
                    seen.add(href)
                    detail_urls.append(href)

            # ページネーション: ?page=N
            for a in soup.find_all("a", href=True):
                href = urljoin(url, a["href"])
                if "page=" in href and "kkhashi.com/matters" in href and href not in seen:
                    queue.append(clean_url(href))

        logger.info("%s: %d detail links", self.site_name, len(detail_urls))

        jobs: List[JobRecord] = []
        for url in detail_urls[:MAX_DETAIL_PAGES]:
            try:
                soup = self.client.soup(url)
            except Exception as e:
                logger.warning("Skip detail %s: %s", url, e)
                continue

            title = ""
            for h in soup.find_all(["h1", "h2"]):
                t = extract_text(h)
                if t and len(t) >= 10 and "カケハシ" not in t and "KAKEHASHI" not in t:
                    title = t
                    break
            if not title:
                title = extract_text(soup.find("title"))
            if not title or len(title) < 5:
                continue

            pairs = parse_table_pairs(soup)
            page_text = extract_text(soup)

            company = (pairs.get("募集企業", "") or pairs.get("会社名", "") or
                       find_text_by_label(soup, "募集企業", "会社名", "企業名"))
            category = pairs.get("業種", "") or pairs.get("カテゴリ", "")
            contract_type = ""
            for kw in ["FC", "フランチャイズ", "代理店", "取次店", "業務委託", "副業"]:
                if kw in page_text[:1000]:
                    contract_type = (contract_type + " / " + kw).strip(" / ")

            reward = pairs.get("報酬", "") or pairs.get("収益", "")
            initial_cost = pairs.get("初期費用", "") or pairs.get("加盟金", "")
            area = pairs.get("募集地域", "") or pairs.get("対象エリア", "")
            target = pairs.get("募集対象", "")

            # 概要
            summary = ""
            for div in soup.find_all(["div", "section"]):
                cls = " ".join(div.get("class", []))
                if any(x in cls.lower() for x in ["detail", "overview", "description", "content"]):
                    summary = extract_text(div)[:500]
                    break
            if not summary:
                meta = soup.find("meta", attrs={"name": "description"})
                if meta and meta.get("content"):
                    summary = normalize_space(meta["content"])[:500]

            recurring = ""
            if re.search(r"(ストック|継続|毎月|月額)", f"{reward} {page_text[:500]}"):
                recurring = "あり"

            job = JobRecord(
                site_name=self.site_name,
                source_url="https://www.kkhashi.com/matters",
                listing_url="https://www.kkhashi.com/matters",
                detail_url=url,
                title=title,
                company_name=company,
                category=category,
                contract_type=contract_type,
                reward_text=reward[:300],
                initial_cost=initial_cost[:100],
                recurring_revenue=recurring,
                area=area[:100],
                target_persona=target[:100],
                summary=summary[:500],
            ).normalize()
            jobs.append(job)

        logger.info("%s: %d jobs scraped", self.site_name, len(jobs))
        return jobs


# ── fc-hikaku.net ────────────────────────────────
class FcHikakuAdapter(BaseSiteAdapter):
    site_name = "fc-hikaku"
    START_URLS = [
        "https://www.fc-hikaku.net/search/",
        "https://www.fc-hikaku.net/search/all",
    ]
    # 詳細は /{slug}_fc 形式 (例: /familymart2_fc, /amau_fc)
    DETAIL_RE = re.compile(r"fc-hikaku\.net/[a-zA-Z0-9_]+-?[a-zA-Z0-9_]*_fc(?:/|$)")
    # 除外パス
    SKIP_PATHS = {"search", "seminar", "column", "ranking", "about",
                  "contact", "privacy", "terms", "sitemap", "login"}

    def run(self) -> List[JobRecord]:
        logger.info("START %s", self.site_name)
        seen: Set[str] = set()
        detail_urls: List[str] = []
        queue = list(self.START_URLS)
        pages_fetched = 0

        while queue and pages_fetched < MAX_LIST_PAGES:
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            pages_fetched += 1

            try:
                soup = self.client.soup(url)
            except Exception as e:
                logger.warning("Skip %s: %s", url, e)
                continue

            for a in soup.find_all("a", href=True):
                href = urljoin(url, a["href"])
                href = clean_url(href)
                if "fc-hikaku.net" not in href:
                    continue
                # _fc で終わるスラッグを詳細ページとして収集
                parsed = urlparse(href)
                path = parsed.path.strip("/")
                # /slug_fc (サブパス無し) のみ収集、/slug_fc/data 等は除外
                if (path.endswith("_fc") and "/" not in path
                        and path.split("_")[0] not in self.SKIP_PATHS
                        and href not in seen):
                    seen.add(href)
                    detail_urls.append(href)

            # ページネーション
            for a in soup.find_all("a", href=True):
                href = urljoin(url, a["href"])
                if "fc-hikaku.net/search" in href and "page=" in href and href not in seen:
                    queue.append(clean_url(href))

        logger.info("%s: %d detail links", self.site_name, len(detail_urls))

        jobs: List[JobRecord] = []
        for url in detail_urls[:MAX_DETAIL_PAGES]:
            try:
                soup = self.client.soup(url)
            except Exception as e:
                logger.warning("Skip detail %s: %s", url, e)
                continue

            title = ""
            for h in soup.find_all(["h1", "h2"]):
                t = extract_text(h)
                if t and len(t) >= 5 and "フランチャイズ比較" not in t:
                    title = t
                    break
            if not title:
                title = extract_text(soup.find("title"))
            if not title or len(title) < 5:
                continue

            pairs = parse_table_pairs(soup)
            page_text = extract_text(soup)

            # 会社名: ヘッダー p.sub_text → テーブル → ラベル検索
            company = ""
            # 1. p.sub_text ("会社名 株式会社XXX" 形式)
            sub_text_el = soup.find("p", class_="sub_text")
            if sub_text_el:
                sub_text = extract_text(sub_text_el)
                company = re.sub(r"^会社名\s*", "", sub_text)
            # 2. テーブルから
            if not company:
                company = (pairs.get("会社名", "") or pairs.get("企業名", "") or
                           pairs.get("募集企業", ""))
            # 3. h3 に "の会社概要" を含む見出しから会社名を抽出
            if not company:
                for h3 in soup.find_all("h3"):
                    h3_text = extract_text(h3)
                    m = re.match(r"(.+?)の会社概要", h3_text)
                    if m:
                        company = m.group(1)
                        break
            # 4. ラベル近傍テキスト
            if not company:
                company = find_text_by_label(soup, "会社名", "企業名", "募集企業")

            category = pairs.get("業種", "") or pairs.get("カテゴリ", "")
            initial_cost = (pairs.get("開業資金", "") or pairs.get("初期費用", "") or
                            pairs.get("加盟金", ""))
            area = pairs.get("募集エリア", "") or pairs.get("対象エリア", "")
            contract_type = "FC"

            summary = ""
            meta = soup.find("meta", attrs={"name": "description"})
            if meta and meta.get("content"):
                summary = normalize_space(meta["content"])[:500]

            recurring = ""
            if re.search(r"(ストック|継続|ロイヤリティ|毎月|月額)", page_text[:1000]):
                recurring = "あり"

            reward = pairs.get("収益モデル", "") or pairs.get("報酬", "")

            job = JobRecord(
                site_name=self.site_name,
                source_url="https://www.fc-hikaku.net/search/",
                listing_url="https://www.fc-hikaku.net/search/",
                detail_url=url,
                title=title,
                company_name=company,
                category=category,
                contract_type=contract_type,
                reward_text=reward[:300],
                initial_cost=initial_cost[:100],
                recurring_revenue=recurring,
                area=area[:100],
                summary=summary[:500],
            ).normalize()
            jobs.append(job)

        logger.info("%s: %d jobs scraped", self.site_name, len(jobs))
        return jobs


# ── メイン ───────────────────────────────────────
def main() -> int:
    client = HttpClient()
    repo = JobRepository(DB_PATH)

    adapters: List[BaseSiteAdapter] = [
        BSeedsAdapter(client),
        DairitenboshuAdapter(client),
        KakehashiAdapter(client),
        FcHikakuAdapter(client),
    ]

    try:
        for adapter in adapters:
            try:
                jobs = adapter.run()
                active_keys = []
                for job in jobs:
                    repo.upsert(job)
                    active_keys.append(job.hash_key)
                repo.mark_missing_inactive(adapter.site_name, active_keys)
                logger.info("%s: %d jobs upserted", adapter.site_name, len(jobs))
            except Exception as e:
                logger.error("FAILED %s: %s", adapter.site_name, e, exc_info=True)

        repo.export_csv(CSV_PATH)
        logger.info("DONE -> %s / %s", DB_PATH, CSV_PATH)
        return 0
    finally:
        repo.close()


if __name__ == "__main__":
    sys.exit(main())
