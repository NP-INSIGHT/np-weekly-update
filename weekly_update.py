#!/usr/bin/env python3
"""
NP Family AI Hub — Notion 자동화
매주 AI 뉴스(RSS) + 유튜브 영상 수집 → 필터링 → 브리핑 생성 → Notion 메인/아카이브/툴박스 업데이트
- 투자/펀딩/M&A/실적 뉴스 제외, 순수 AI 기술 뉴스만
- YouTube 조회수 3만+ AI 영상 수집 (선택)
- Claude 미사용. OpenAI 또는 Gemini로 요약/임팩트 보강 (선택)
"""

import os
import re
import sys
import json
import time
from datetime import datetime, timedelta, timezone

# Windows 등 콘솔에서 이모지/한글 출력 시 인코딩 오류 방지
try:
    if hasattr(sys.stdout, "buffer"):
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass
from typing import Optional
from pathlib import Path

import feedparser
import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# 설정 (같은 폴더의 .env 파일에서 읽음. 메인 페이지만 쓰려면 아래 두 개만 넣으면 됨)
# ---------------------------------------------------------------------------
load_dotenv()

NOTION_API_KEY = (os.getenv("NOTION_API_KEY") or "").strip()
NOTION_MAIN_PAGE_ID = (os.getenv("NOTION_MAIN_PAGE_ID") or "").strip()
NOTION_ARCHIVE_DB_ID = os.getenv("NOTION_ARCHIVE_DB_ID")  # 미사용(메인만 쓸 땐 비워둬도 됨)
NOTION_ARCHIVE_PAGE_ID = (os.getenv("NOTION_ARCHIVE_PAGE_ID") or "3190f0d18c5a8011bef2d27bfdb38a01").strip()  # AI 트렌드 아카이브 페이지 ID
NOTION_TOOLBOX_DB_ID = os.getenv("NOTION_TOOLBOX_DB_ID")  # 미사용(메인만 쓸 땐 비워둬도 됨)
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL") or "claude-3-5-sonnet-latest"
GEMINI_MODEL = os.getenv("GEMINI_MODEL") or "gemini-2.5-flash"

NOTION_YOUTUBE_PAGE_ID = (os.getenv("NOTION_YOUTUBE_PAGE_ID") or "").strip()  # NP AI 자동화 추천 영상 페이지
YOUTUBE_PAGE_TITLE = "NP AI 자동화 추천 영상"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "weekly_update_state.json")
YOUTUBE_UPDATE_INTERVAL_DAYS = 30  # YouTube 업데이트 주기 (일)

NOTION_VERSION = "2022-06-28"
NOTION_RATE_LIMIT_SEC = 0.35
DAYS_BACK = 7
MAX_ARTICLES_PER_SOURCE = 25
MAX_NEWS_ITEMS = 10  # 브리핑에 포함할 뉴스 개수 (최대 10개)
YOUTUBE_MIN_VIEWS = 30_000
YOUTUBE_TOP_N = 20

# 제외 키워드 (제목에 포함되면 기사 제외)
EXCLUDE_KEYWORDS = [
    "raised", "funding", "series a", "series b", "series c", "valuation", "ipo",
    "acquisition", "acquires", "merger", "revenue", "earnings", "quarterly",
    "투자", "유치", "인수", "합병", "상장", "매출", "실적", "시가총액",
]

RSS_SOURCES = [
    ("AI타임스", "https://www.aitimes.com/rss/allArticle.xml"),
    ("전자신문", "https://rss.etnews.com/Section904.xml"),
    ("ZDNet Korea", "https://zdnet.co.kr/rss/AI_news.xml"),
    ("디지털투데이", "https://www.digitaltoday.co.kr/rss/allArticle.xml"),
    ("IT조선", "https://it.chosun.com/rss/it_news.xml"),
    ("한국경제 IT", "https://www.hankyung.com/feed/it"),
    ("블로터", "https://www.bloter.net/feed"),
    ("테크크런치코리아", "https://kr.techcrunch.com/feed/"),
]

YOUTUBE_QUERIES = [
    "AI 자동화",
    "AI 업무 자동화",
    "AI agent 자동화",
    "AI 자동화 도구",
    "AI 자동화 활용법",
]
YOUTUBE_DAYS_BACK = 90  # 3개월

# 태그 이모지: 모델/도구/활용/기술
TAG_EMOJI = {"모델": "🔵", "도구": "🟢", "활용": "🟣", "기술": "🟡"}
TAG_OPTIONS = ["모델", "도구", "활용", "기술"]

# ---------------------------------------------------------------------------
# 유틸
# ---------------------------------------------------------------------------
def _notion_sleep():
    time.sleep(NOTION_RATE_LIMIT_SEC)


def _notion_headers():
    return {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_VERSION,
    }


def _rich_text(content: str, bold: bool = False):
    return [{"type": "text", "text": {"content": content[:2000]}, "annotations": {"bold": bold}}]


def _rich_text_link(content: str, url: str):
    """클릭 가능한 링크용 rich_text. url이 있을 때만 link 속성 추가."""
    text = {"content": (content or "기사 보기")[:2000]}
    if (url or "").strip().startswith("http"):
        text["link"] = {"url": url.strip()}
    return [{"type": "text", "text": text, "annotations": {"bold": False}}]


AI_KEYWORDS = [
    "ai", "인공지능", "llm", "gpt", "claude", "gemini", "chatbot", "챗봇",
    "machine learning", "머신러닝", "딥러닝", "deep learning", "neural",
    "자동화", "automation", "agent", "에이전트", "생성형", "generative",
    "copilot", "cursor", "openai", "anthropic", "google ai", "meta ai",
    "프롬프트", "prompt", "rag", "파인튜닝", "fine-tun", "transformer",
    "diffusion", "multimodal", "멀티모달", "nlp", "자연어",
]


def _should_exclude_article(title: str, summary: str = "") -> bool:
    """제목에 투자/펀딩/M&A/실적 키워드가 있으면 True. AI 관련 없으면 True."""
    if not title:
        return True
    lower = title.lower().strip()
    for kw in EXCLUDE_KEYWORDS:
        if kw.lower() in lower:
            return True
    # AI 관련 키워드가 제목이나 요약에 없으면 제외
    text = (lower + " " + (summary or "").lower()).strip()
    if not any(kw in text for kw in AI_KEYWORDS):
        return True
    return False


# ---------------------------------------------------------------------------
# 1. 뉴스 수집 (RSS) + 필터링
# ---------------------------------------------------------------------------
def parse_rss_date(entry) -> Optional[datetime]:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        p = getattr(entry, key, None)
        if p and hasattr(p, "tm_year"):
            try:
                return datetime(*p[:6])
            except (TypeError, ValueError):
                pass
    return None


def _fetch_single_rss(name: str, url: str, cutoff: datetime) -> list[dict]:
    """단일 RSS 소스에서 기사 수집 (병렬 처리용)."""
    articles = []
    try:
        feed = feedparser.parse(url, request_headers={"User-Agent": "NP-AI-Hub/1.0"})
        count = 0
        for entry in feed.entries:
            if count >= MAX_ARTICLES_PER_SOURCE:
                break
            pub = parse_rss_date(entry)
            if pub and pub.replace(tzinfo=None) < cutoff:
                continue
            link = entry.get("link") or ""
            title = (entry.get("title") or "").strip()
            if not title or not link:
                continue
            summary = entry.get("summary") or entry.get("description") or ""
            if hasattr(summary, "replace"):
                summary = re.sub(r"<[^>]+>", "", summary)[:500]
            if _should_exclude_article(title, summary):
                continue
            if not re.search(r"[가-힣]", title):
                continue
            articles.append({
                "title": title,
                "link": link,
                "summary": summary,
                "source": name,
                "published": pub.isoformat() if pub else "",
            })
            count += 1
        print(f"  📡 [RSS] {name} → {count}건")
    except Exception as e:
        print(f"  ⚠️ [RSS] {name} 오류: {e}")
    return articles


def fetch_news() -> list[dict]:
    """RSS에서 최근 7일 기사 수집. 모든 소스 병렬 처리."""
    from concurrent.futures import ThreadPoolExecutor
    cutoff = (datetime.now(timezone.utc) - timedelta(days=DAYS_BACK)).replace(tzinfo=None)

    with ThreadPoolExecutor(max_workers=len(RSS_SOURCES)) as pool:
        futures = [pool.submit(_fetch_single_rss, name, url, cutoff) for name, url in RSS_SOURCES]
        all_articles = []
        for f in futures:
            all_articles.extend(f.result())

    print(f"  📡 총 수집(필터 후): {len(all_articles)}건")
    return all_articles


# ---------------------------------------------------------------------------
# 2. 유튜브 영상 수집 (조회수 3만+)
# ---------------------------------------------------------------------------
def fetch_youtube_videos() -> list[dict]:
    """최근 3개월, AI 자동화 영상 수집. 조회수 순 상위 N개.
    YOUTUBE_API_KEY 없으면 Gemini + Google Search로 대체."""
    if not YOUTUBE_API_KEY:
        print("  🎬 [YouTube] YOUTUBE_API_KEY 없음 → yt-dlp로 대체")
        return _fetch_youtube_via_ytdlp()

    published_after = (datetime.now(timezone.utc) - timedelta(days=YOUTUBE_DAYS_BACK)).strftime("%Y-%m-%dT00:00:00Z")
    seen_ids = set()
    candidates = []

    for q in YOUTUBE_QUERIES:
        try:
            r = requests.get(
                "https://www.googleapis.com/youtube/v3/search",
                params={
                    "key": YOUTUBE_API_KEY,
                    "part": "snippet",
                    "type": "video",
                    "order": "viewCount",
                    "publishedAfter": published_after,
                    "relevanceLanguage": "ko",
                    "q": q,
                    "maxResults": 15,
                },
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()
            ids = [it["id"]["videoId"] for it in data.get("items", []) if it.get("id", {}).get("videoId")]
            if not ids:
                continue
            # videos.list로 조회수/길이 등 조회
            r2 = requests.get(
                "https://www.googleapis.com/youtube/v3/videos",
                params={
                    "key": YOUTUBE_API_KEY,
                    "part": "snippet,contentDetails,statistics",
                    "id": ",".join(ids[:10]),
                },
                timeout=15,
            )
            r2.raise_for_status()
            for v in r2.json().get("items", []):
                vid = v.get("id")
                if vid in seen_ids:
                    continue
                stats = v.get("statistics", {})
                view_count = int(stats.get("viewCount") or 0)
                if view_count < YOUTUBE_MIN_VIEWS:
                    continue
                snippet = v.get("snippet", {})
                # 한국어 영상만 필터링 (제목에 한글 포함 여부)
                title_text = snippet.get("title") or ""
                if not re.search(r"[가-힣]", title_text):
                    continue
                seen_ids.add(vid)
                dur = v.get("contentDetails", {}).get("duration", "PT0M")
                minutes = _parse_iso_duration(dur)
                pub_date = (snippet.get("publishedAt") or "")[:10]  # YYYY-MM-DD
                candidates.append({
                    "id": vid,
                    "title": (snippet.get("title") or "")[:200],
                    "channel": (snippet.get("channelTitle") or "")[:100],
                    "views": view_count,
                    "duration_min": minutes,
                    "upload_date": pub_date,
                    "url": f"https://www.youtube.com/watch?v={vid}",
                    "summary_line": (snippet.get("description") or snippet.get("title") or "")[:150],
                })
        except Exception as e:
            print(f"  🎬 [YouTube] 쿼리 '{q[:20]}...' 오류: {e}")
            continue

    candidates.sort(key=lambda x: -x["views"])
    result = candidates[:YOUTUBE_TOP_N]
    print(f"  🎬 조회수 3만+ 영상 {len(result)}개 선정")
    return result


def _parse_iso_duration(dur: str) -> int:
    """PT1H2M3S → 분 단위 정수."""
    import re as re_inner
    m = re_inner.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", dur)
    if not m:
        return 0
    h, mn, s = m.group(1), m.group(2), m.group(3)
    return int(h or 0) * 60 + int(mn or 0) + (int(s or 0) // 60)


def _fetch_youtube_via_ytdlp() -> list[dict]:
    """yt-dlp로 YouTube 'AI 자동화' 영상 검색 (API 키 불필요).
    YOUTUBE_API_KEY 없을 때 대체 수단."""
    try:
        import yt_dlp
    except ImportError:
        print("  🎬 [YouTube-ytdlp] yt-dlp 미설치 → 스킵")
        return []

    cutoff = datetime.now() - timedelta(days=YOUTUBE_DAYS_BACK)
    cutoff_str = cutoff.strftime("%Y%m%d")
    seen_ids = set()
    candidates = []

    for q in YOUTUBE_QUERIES:
        try:
            print(f"  🎬 [YouTube-ytdlp] 검색: '{q}' ...")
            ydl_opts = {
                "quiet": True,
                "no_warnings": True,
                "extract_flat": False,
                "skip_download": True,
                "ignoreerrors": True,
                "default_search": "ytsearch15",
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                result = ydl.extract_info(f"ytsearch15:{q}", download=False)
                if not result or "entries" not in result:
                    continue
                for entry in result["entries"]:
                    if not entry:
                        continue
                    vid = entry.get("id") or ""
                    if not vid or vid in seen_ids:
                        continue
                    # 업로드일 필터 (YYYYMMDD 형식)
                    upload_date = entry.get("upload_date") or ""
                    if upload_date and upload_date < cutoff_str:
                        continue
                    # 한국어 영상만 필터링 (제목에 한글 포함 여부)
                    title_text = entry.get("title") or ""
                    if not re.search(r"[가-힣]", title_text):
                        continue
                    view_count = int(entry.get("view_count") or 0)
                    if view_count < YOUTUBE_MIN_VIEWS:
                        continue
                    seen_ids.add(vid)
                    duration_sec = int(entry.get("duration") or 0)
                    # upload_date: YYYYMMDD → YYYY-MM-DD
                    date_str = ""
                    if upload_date and len(upload_date) == 8:
                        date_str = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"
                    candidates.append({
                        "id": vid,
                        "title": (entry.get("title") or "")[:200],
                        "channel": (entry.get("uploader") or entry.get("channel") or "")[:100],
                        "views": view_count,
                        "duration_min": duration_sec // 60,
                        "upload_date": date_str,
                        "url": f"https://www.youtube.com/watch?v={vid}",
                        "summary_line": (entry.get("description") or entry.get("title") or "")[:150],
                    })
        except Exception as e:
            print(f"  🎬 [YouTube-ytdlp] 쿼리 '{q[:20]}...' 오류: {e}")
            continue

    candidates.sort(key=lambda x: -x["views"])
    result = candidates[:YOUTUBE_TOP_N]
    print(f"  🎬 [YouTube-ytdlp] 조회수 순 {len(result)}개 영상 선정")
    return result


def _summarize_youtube_batch(batch: list[dict], batch_idx: int) -> tuple[int, list[dict]]:
    """단일 YouTube 배치 요약 (병렬 처리용)."""
    n = len(batch)
    input_lines = []
    for i, v in enumerate(batch):
        input_lines.append(f"[{i+1}] 제목: {v.get('title','')}\n    설명: {v.get('summary_line','')}")
    input_text = "\n\n".join(input_lines)

    prompt = (
        "너는 한국어 AI 뉴스 에디터다. 과장 없이 핵심만 전달한다.\n"
        "아래 YouTube 영상들의 제목과 설명을 보고, 각 영상이 어떤 내용인지 한국어 2줄로 요약해줘.\n"
        "반드시 JSON 배열만 출력하고, 다른 텍스트는 절대 포함하지 않는다.\n"
        f"출력 형식: [{{'summary':'2줄 한국어 요약'}}] (총 {n}개, 입력 순서 동일)\n\n"
        f"{input_text}"
    )

    for attempt in range(1, 4):
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}",
                headers={"Content-Type": "application/json"},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0.2, "maxOutputTokens": 2000},
                },
                timeout=60,
            )
            r.raise_for_status()
            data = r.json()
            parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
            raw = "\n".join(p.get("text", "") for p in parts).strip()
            cleaned = _extract_json_text(raw)
            parsed = json.loads(cleaned)
            if isinstance(parsed, list) and len(parsed) == n:
                for i, v in enumerate(batch):
                    v["summary_line"] = parsed[i].get("summary", v.get("summary_line", ""))
                return batch_idx, batch
            else:
                print(f"  ⚠️ [Gemini] YouTube 요약 배열 길이 불일치 (batch {batch_idx}, attempt {attempt})")
        except Exception as e:
            print(f"  ⚠️ [Gemini] YouTube 요약 실패 (batch {batch_idx}, attempt {attempt}): {e}")
            if attempt < 3:
                wait = 15 if "429" in str(e) else 2
                time.sleep(wait)
    return batch_idx, batch


def _summarize_youtube_videos(videos: list[dict]) -> list[dict]:
    """Gemini로 YouTube 영상 제목+설명을 2줄 한국어 요약. 배치 병렬 처리."""
    from concurrent.futures import ThreadPoolExecutor
    if not videos or not GEMINI_API_KEY:
        return videos

    BATCH = 5
    batches = [videos[i:i + BATCH] for i in range(0, len(videos), BATCH)]

    with ThreadPoolExecutor(max_workers=len(batches)) as pool:
        futures = [pool.submit(_summarize_youtube_batch, batch, idx) for idx, batch in enumerate(batches)]
        for f in futures:
            f.result()

    success_count = sum(1 for v in videos if v.get("summary_line"))
    print(f"  🎬 [Gemini] YouTube 영상 요약 완료 ({success_count}/{len(videos)}개)")
    return videos


# ---------------------------------------------------------------------------
# 3. 브리핑 생성 (규칙 기반 + 선택적 LLM)
# ---------------------------------------------------------------------------
def _score_article(article: dict) -> tuple[float, str]:
    """스타트업에 직접 도움 되는 뉴스 우선: AI 활용법, 새 AI 도구/모델, 실전 사례."""
    title = (article.get("title") or "").lower()
    summary = (article.get("summary") or "").lower()
    text = title + " " + summary
    source = article.get("source") or ""

    score = 0.0
    tag = "도구"

    # 활용(사례·예시·워크플로우·자동화) — 최우선
    if any(k in text for k in (
        "use case", "workflow", "사례", "활용", "적용", "예시", "활용법", "활용 사례",
        "how to", "tutorial", "실전", "노하우", "팁", "tip", "automation", "자동화",
        "생산성", "productivity", "효율", "efficiency", "비용 절감", "cost",
    )):
        score += 4.0
        tag = "활용"
    # 새 AI 도구/서비스 출시 — 스타트업이 바로 써볼 수 있는 것
    if any(k in text for k in (
        "launch", "release", "출시", "공개", "새로운", "new", "update", "업데이트",
        "tool", "platform", "product", "도구", "서비스", "플랫폼",
        "cursor", "copilot", "agent", "에이전트", "chatbot", "챗봇",
    )):
        score += 3.5
        if tag != "활용":
            tag = "도구"
    # 새 모델/API — 기술 스택에 영향
    if any(k in text for k in ("model", "gpt", "claude", "gemini", "llm", "api", "pricing", "모델", "open source", "오픈소스")):
        score += 3.0
        if tag == "도구":
            tag = "모델"
    # 기술 트렌드 — 전략 수립 참고
    if any(k in text for k in (
        "rag", "multimodal", "트렌드", "trend", "파인튜닝", "fine-tun", "프롬프트", "prompt",
    )):
        score += 2.0
        if tag not in ("활용", "도구"):
            tag = "기술"
    # 정책/규제 — 낮은 우선순위
    if any(k in text for k in ("정책", "규제", "policy", "regulation", "eu")):
        score += 1.0
        if tag == "도구":
            tag = "기술"
    # 스타트업 직접 관련
    if any(k in text for k in ("startup", "enterprise", "스타트업", "중소기업", "smb", "soho")):
        score += 2.0
    if "aitimes" in source.lower() or "etnews" in source or "zdnet" in source.lower():
        score += 0.8
    if article.get("published"):
        score += 0.3

    return score, tag


def _default_np_impact(tag: str, article: dict) -> str:
    """한국 스타트업(10~100명) 관점 NP 패밀리 임팩트."""
    if tag == "모델":
        return "새 모델·API 변경은 NP 패밀리 제품의 기술 스택과 비용 구조에 영향을 줄 수 있습니다."
    if tag == "도구":
        return "실무에 바로 쓸 수 있는 도구 정보는 개발·운영 효율을 높이는 데 도움이 됩니다."
    if tag == "기술":
        return "규제·기술 트렌드는 한국 스타트업의 사업 환경과 도입 전략 수립에 참고가 됩니다."
    return "산업 사례와 활용 트렌드는 서비스 기획과 고객 니즈 파악에 참고가 됩니다."


def _pick_items_and_tool(articles: list[dict], n: int = MAX_NEWS_ITEMS) -> tuple[list[dict], dict]:
    """상위 N개 뉴스 선정. 활용·기술(트렌드) 우선, 태그 다양화. 링크 포함."""
    scored = []
    for a in articles:
        s, tag = _score_article(a)
        if s >= 2.0:  # 최소 점수 미달 기사 제외 (AI와 무관한 기사 필터)
            scored.append((s, tag, a))

    def sort_key(x):
        s, tag, _ = x
        order = {"활용": 0, "도구": 1, "모델": 2, "기술": 3}
        return (-s, order.get(tag, 4))

    scored.sort(key=sort_key)
    chosen = []
    seen_links = set()

    # 1차: 태그 다양화 우선 (각 태그 최대 ceil(n/4)개)
    tag_count: dict[str, int] = {}
    per_tag_limit = max(3, (n + 3) // 4)
    for s, tag, a in scored:
        if len(chosen) >= n:
            break
        link = a.get("link") or ""
        if link in seen_links:
            continue
        if tag_count.get(tag, 0) >= per_tag_limit:
            continue
        chosen.append({
            "tag": tag,
            "title": a["title"],
            "summary": a["summary"] or a["title"],
            "link": a["link"],
            "np_impact": _default_np_impact(tag, a),
        })
        seen_links.add(link)
        tag_count[tag] = tag_count.get(tag, 0) + 1

    # 2차: 부족하면 태그 제한 없이 채우기
    for s, tag, a in scored:
        if len(chosen) >= n:
            break
        link = a.get("link") or ""
        if link in seen_links:
            continue
        chosen.append({
            "tag": tag,
            "title": a["title"],
            "summary": a["summary"] or a["title"],
            "link": a["link"],
            "np_impact": _default_np_impact(tag, a),
        })
        seen_links.add(link)

    chosen = chosen[:n]

    tool_candidates = [a for _, tag, a in scored if tag in ("도구", "모델")]
    rec_tool = {
        "name": "AI 공식 블로그 / 검색",
        "difficulty": "초보",
        "price": "무료",
        "description": "OpenAI, Anthropic, Google AI 등 공식 블로그에서 최신 모델·API 소식을 확인하세요.",
        "tip": "API 가격·한도 변경은 스타트업 비용에 직결되므로 매주 한 번씩 체크하는 것을 권장합니다.",
        "url": "",
    }
    for a in tool_candidates[:5]:
        t = (a.get("title") or "").strip()
        if 2 <= len(t) <= 60 and "blog" not in t.lower():
            rec_tool["name"] = t[:50]
            rec_tool["description"] = (a.get("summary") or t)[:200]
            rec_tool["url"] = a.get("link") or ""
            break

    return chosen, rec_tool


def _this_week_label() -> str:
    now = datetime.now()
    # 1~7일=1주차, 8~14일=2주차, 15~21일=3주차, 22~28일=4주차, 29~=5주차
    week_of_month = (now.day - 1) // 7 + 1
    return f"{now.year}년 {now.month}월 {week_of_month}주차"


def _extract_json_text(raw: str) -> str:
    """API 응답에서 JSON 문자열만 추출. 마크다운 코드블록 제거."""
    raw = raw.strip()
    # ```json ... ``` 또는 ``` ... ``` 제거
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return raw.strip()


def _gemini_batch_summarize(items: list[dict]) -> list[dict]:
    """Gemini API로 전체 뉴스 아이템을 한 번에 배치 처리.
    반환: [{"summary": "...", "np_impact": "..."}, ...] (items 순서 동일)
    실패 시 빈 리스트 반환.
    """
    n = len(items)
    input_lines = []
    for i, item in enumerate(items):
        input_lines.append(
            f"[{i+1}] 제목: {item['title']}\n    원문: {(item['summary'] or '')[:400]}"
        )
    input_text = "\n\n".join(input_lines)

    prompt = (
        "너는 한국어 AI 뉴스 에디터다. 과장 없이 핵심만 전달한다.\n"
        "NP 패밀리는 한국 스타트업(10~100명 규모)이다.\n"
        "요약은 스타트업이 이 뉴스를 왜 알아야 하는지 중심으로 작성한다.\n"
        "특히 AI 활용법, 새로운 AI 도구/모델, 비용 절감, 생산성 향상 등 실질적 도움이 되는 포인트를 강조한다.\n"
        "반드시 JSON 배열만 출력하고, 다른 텍스트는 절대 포함하지 않는다.\n"
        "각 항목: {\"summary\": \"2~3문장 한국어 요약\", \"np_impact\": \"스타트업이 바로 활용할 수 있는 액션 포인트 1~2문장\"}\n\n"
        f"아래 {n}개 AI 뉴스 각각을 요약해줘.\n"
        f"출력 형식: JSON 배열, 입력 순서 동일, 배열 길이 반드시 입력과 같아야 함.\n"
        f"예시: [{{\"summary\":\"...\",\"np_impact\":\"...\"}}, ...] (총 {n}개)\n\n"
        f"{input_text}"
    )

    MAX_RETRY = 3
    for attempt in range(1, MAX_RETRY + 1):
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}",
                headers={"Content-Type": "application/json"},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0.2, "maxOutputTokens": min(4096, max(1500, n * 420))},
                },
                timeout=60,
            )
            r.raise_for_status()
            data = r.json()

            parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
            raw = "\n".join(p.get("text", "") for p in parts).strip()
            cleaned = _extract_json_text(raw)

            parsed = json.loads(cleaned)
            if isinstance(parsed, list) and len(parsed) == n:
                print(f"  🤖 [Gemini] 배치 요약 완료 ({n}개, attempt {attempt})")
                return parsed
            else:
                print(f"  ⚠️ [Gemini] 배열 길이 불일치 (기대 {n}, 실제 {len(parsed) if isinstance(parsed, list) else 'N/A'}), attempt {attempt}")
                if attempt < MAX_RETRY:
                    time.sleep(1.5)

        except json.JSONDecodeError as e:
            print(f"  ⚠️ [Gemini] JSON 파싱 실패 (attempt {attempt}): {e}")
            if attempt < MAX_RETRY:
                time.sleep(1.5)
        except requests.RequestException as e:
            print(f"  ⚠️ [Gemini] API 요청 실패 (attempt {attempt}): {e}")
            if attempt < MAX_RETRY:
                wait = 15 if "429" in str(e) else 2
                time.sleep(wait)

    return []


def _gemini_single_summarize(item: dict) -> dict:
    """단건 Gemini 요약 (배치 실패 시 폴백)."""
    prompt = (
        "한국어로 2~3문장 요약과 NP 패밀리(한국 스타트업 10~100명) 관점 임팩트 1~2문장.\n"
        "반드시 JSON만 출력 (다른 텍스트 없이): {\"summary\":\"...\", \"np_impact\":\"...\"}\n\n"
        f"제목: {item['title']}\n"
        f"원문 요약: {(item['summary'] or '')[:500]}\n"
    )
    for attempt in range(1, 4):
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}",
                headers={"Content-Type": "application/json"},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0.2, "maxOutputTokens": 600},
                },
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()
            parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
            raw = "\n".join(p.get("text", "") for p in parts).strip()
            cleaned = _extract_json_text(raw)
            obj = json.loads(cleaned)
            if isinstance(obj, dict) and obj.get("summary"):
                return obj
        except Exception as e:
            print(f"  ⚠️ [Gemini] 단건 요약 실패 (attempt {attempt}): {e}")
            if attempt < 3:
                wait = 15 if "429" in str(e) else 1.5
                time.sleep(wait)
    return {}


def _anthropic_batch_summarize(items: list[dict]) -> list[dict]:
    """Claude API로 전체 뉴스 아이템을 한 번에 배치 처리.
    반환: [{"summary": "...", "np_impact": "..."}, ...] (items 순서 동일)
    실패 시 빈 리스트 반환.
    """
    n = len(items)
    # 입력 목록 구성
    input_lines = []
    for i, item in enumerate(items):
        input_lines.append(
            f"[{i+1}] 제목: {item['title']}\n    원문: {(item['summary'] or '')[:400]}"
        )
    input_text = "\n\n".join(input_lines)

    # 토큰 여유 있게: 아이템당 약 350토큰(요약 200자 + 임팩트 150자) × n + 여유분
    max_tokens = min(4096, max(1500, n * 420))

    system_prompt = (
        "너는 한국어 AI 뉴스 에디터다. 과장 없이 핵심만 전달한다.\n"
        "NP 패밀리는 한국 스타트업(10~100명 규모)이다.\n"
        "반드시 JSON 배열만 출력하고, 다른 텍스트는 절대 포함하지 않는다.\n"
        "각 항목: {\"summary\": \"2~3문장 한국어 요약\", \"np_impact\": \"NP 패밀리 관점 임팩트 1~2문장\"}"
    )

    user_prompt = (
        f"아래 {n}개 AI 뉴스 각각을 요약해줘.\n"
        "출력 형식: JSON 배열, 입력 순서 동일, 배열 길이 반드시 입력과 같아야 함.\n"
        f"예시: [{{\"summary\":\"...\",\"np_impact\":\"...\"}}, ...] (총 {n}개)\n\n"
        f"{input_text}"
    )

    MAX_RETRY = 3
    for attempt in range(1, MAX_RETRY + 1):
        try:
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": ANTHROPIC_MODEL,
                    "max_tokens": max_tokens,
                    "temperature": 0.2,
                    "system": system_prompt,
                    "messages": [{"role": "user", "content": user_prompt}],
                },
                timeout=60,
            )
            r.raise_for_status()
            data = r.json()

            # stop_reason 확인: max_tokens면 잘린 것
            stop_reason = data.get("stop_reason", "")
            if stop_reason == "max_tokens":
                print(f"  ⚠️ [Claude] 배치 요약 응답 잘림 (max_tokens 도달, attempt {attempt}). 토큰 늘려 재시도...")
                max_tokens = min(8096, max_tokens + 1000)
                time.sleep(1)
                continue

            parts = data.get("content") or []
            raw = "\n".join(p.get("text") or "" for p in parts if isinstance(p, dict) and p.get("type") == "text").strip()
            cleaned = _extract_json_text(raw)

            parsed = json.loads(cleaned)
            if isinstance(parsed, list) and len(parsed) == n:
                print(f"  🤖 [Claude] 배치 요약 완료 ({n}개, attempt {attempt})")
                return parsed
            else:
                print(f"  ⚠️ [Claude] 배열 길이 불일치 (기대 {n}, 실제 {len(parsed) if isinstance(parsed, list) else 'N/A'}), attempt {attempt}")
                if attempt < MAX_RETRY:
                    time.sleep(1.5)

        except json.JSONDecodeError as e:
            print(f"  ⚠️ [Claude] JSON 파싱 실패 (attempt {attempt}): {e}")
            if attempt < MAX_RETRY:
                time.sleep(1.5)
        except requests.RequestException as e:
            print(f"  ⚠️ [Claude] API 요청 실패 (attempt {attempt}): {e}")
            if attempt < MAX_RETRY:
                time.sleep(2)

    return []


def _anthropic_single_summarize(item: dict) -> dict:
    """단건 Claude 요약 (배치 실패 시 폴백). max_tokens 충분히 확보."""
    prompt = (
        "한국어로 2~3문장 요약과 NP 패밀리(한국 스타트업 10~100명) 관점 임팩트 1~2문장.\n"
        "반드시 JSON만 출력 (다른 텍스트 없이): {\"summary\":\"...\", \"np_impact\":\"...\"}\n\n"
        f"제목: {item['title']}\n"
        f"원문 요약: {(item['summary'] or '')[:500]}\n"
    )
    for attempt in range(1, 4):
        try:
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": ANTHROPIC_MODEL,
                    "max_tokens": 600,  # 단건은 600으로 충분
                    "temperature": 0.2,
                    "system": "너는 한국어 AI 뉴스 에디터다. 과장 없이 핵심만. JSON만 출력.",
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()
            if data.get("stop_reason") == "max_tokens":
                print(f"  ⚠️ [Claude] 단건 응답 잘림, attempt {attempt}")
                time.sleep(1)
                continue
            parts = data.get("content") or []
            raw = "\n".join(p.get("text") or "" for p in parts if isinstance(p, dict) and p.get("type") == "text").strip()
            cleaned = _extract_json_text(raw)
            obj = json.loads(cleaned)
            if isinstance(obj, dict) and obj.get("summary"):
                return obj
        except Exception as e:
            print(f"  ⚠️ [Claude] 단건 요약 실패 (attempt {attempt}): {e}")
            if attempt < 3:
                time.sleep(1.5)
    return {}


def _enhance_with_llm(items: list[dict], rec_tool: dict) -> None:
    """Gemini / Anthropic / OpenAI로 요약/임팩트 보강. 실패 시 무시.

    우선순위: Gemini(있으면) → Anthropic → OpenAI
    Gemini: 전체 배치 1회 호출 → 실패 시 단건 폴백
    """
    if GEMINI_API_KEY:
        try:
            # 1차: 전체 배치 처리
            results = _gemini_batch_summarize(items)
            if results and len(results) == len(items):
                for item, res in zip(items, results):
                    if isinstance(res, dict):
                        if res.get("summary"):
                            item["summary"] = res["summary"]
                        if res.get("np_impact"):
                            item["np_impact"] = res["np_impact"]
                return
            # 2차: 배치 실패 시 단건 폴백
            print("  🔄 [Gemini] 배치 실패 → 단건 폴백 처리")
            for item in items:
                obj = _gemini_single_summarize(item)
                if obj.get("summary"):
                    item["summary"] = obj["summary"]
                if obj.get("np_impact"):
                    item["np_impact"] = obj["np_impact"]
                time.sleep(0.5)
            return
        except Exception as e:
            print(f"  ⚠️ [Gemini] 요약 전체 실패: {e}")

    if ANTHROPIC_API_KEY:
        try:
            results = _anthropic_batch_summarize(items)
            if results and len(results) == len(items):
                for item, res in zip(items, results):
                    if isinstance(res, dict):
                        if res.get("summary"):
                            item["summary"] = res["summary"]
                        if res.get("np_impact"):
                            item["np_impact"] = res["np_impact"]
                return
            print("  🔄 [Claude] 배치 실패 → 단건 폴백 처리")
            for item in items:
                obj = _anthropic_single_summarize(item)
                if obj.get("summary"):
                    item["summary"] = obj["summary"]
                if obj.get("np_impact"):
                    item["np_impact"] = obj["np_impact"]
                time.sleep(0.5)
            return
        except Exception as e:
            print(f"  ⚠️ [Claude] 요약 전체 실패: {e}")

    if OPENAI_API_KEY:
        try:
            import openai
            client = openai.OpenAI(api_key=OPENAI_API_KEY)
            for item in items:
                r = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {"role": "system", "content": "한국어로 2~3문장 요약과 NP 패밀리(한국 스타트업 10~100명 규모) 관점 임팩트 1~2문장만. JSON만: {\"summary\":\"...\", \"np_impact\":\"...\"}"},
                        {"role": "user", "content": f"제목: {item['title']}\n원문 요약: {item['summary']}"}
                    ],
                    max_tokens=600,
                )
                text = _extract_json_text(r.choices[0].message.content or "")
                obj = json.loads(text)
                if obj.get("summary"):
                    item["summary"] = obj["summary"]
                if obj.get("np_impact"):
                    item["np_impact"] = obj["np_impact"]
            return
        except Exception as e:
            print(f"  ⚠️ [OpenAI] 요약 실패: {e}")


def generate_briefing(articles: list[dict], youtube_videos: Optional[list[dict]] = None) -> dict:
    """브리핑 생성. LLM 키 있으면 요약/임팩트 보강."""
    items, rec_tool = _pick_items_and_tool(articles, n=MAX_NEWS_ITEMS)
    if ANTHROPIC_API_KEY or OPENAI_API_KEY or GEMINI_API_KEY:
        _enhance_with_llm(items, rec_tool)
    return {
        "items": items,
        "recommended_tool": rec_tool,
        "youtube_videos": youtube_videos or [],
        "week_label": _this_week_label(),
        "date": datetime.now().strftime("%Y-%m-%d"),
    }


# ---------------------------------------------------------------------------
# 4. Notion 메인 페이지 – This Week in AI 섹션 교체
# ---------------------------------------------------------------------------
def _build_this_week_blocks(briefing: dict) -> list[dict]:
    """메인 페이지에 넣을 블록: 날짜 Callout, 뉴스 최대 10개(링크 포함), 추천 도구, 유튜브 TOP."""
    blocks = []
    date_str = briefing["date"]
    items = briefing["items"]
    tool = briefing["recommended_tool"]
    videos = briefing.get("youtube_videos") or []

    # Callout: 날짜 (YYYY.MM.DD 형식)
    date_display = date_str.replace("-", ".")
    blocks.append({
        "object": "block",
        "type": "callout",
        "callout": {
            "rich_text": _rich_text(f"{date_display} 업데이트", bold=True) + _rich_text(" NEW · 매주 월요일 업데이트"),
            "icon": {"type": "emoji", "emoji": "📅"},
            "color": "blue_background",
        },
    })

    # 추천 도구 Callout
    tool_rt = (
        _rich_text("이번 주 추천 도구\n", bold=True)
        + _rich_text(f"{tool['name']}", bold=True)
        + _rich_text(f" · {tool.get('difficulty', '초보')} · {tool.get('price', '무료')}\n")
        + _rich_text((tool.get("description") or "")[:500] + "\n")
        + _rich_text("✨ 이렇게 써보세요: ", bold=True)
        + _rich_text((tool.get("tip") or "")[:500])
    )
    blocks.append({
        "object": "block",
        "type": "callout",
        "callout": {
            "rich_text": tool_rt,
            "icon": {"type": "emoji", "emoji": "🛠️"},
            "color": "green_background",
        },
    })

    blocks.append({"object": "block", "type": "divider", "divider": {}})

    # 유튜브 섹션 (링크 클릭 가능)
    blocks.append({
        "object": "block",
        "type": "heading_3",
        "heading_3": {"rich_text": _rich_text("🎬 이번 주 AI 영상 TOP")},
    })
    for v in videos[:YOUTUBE_TOP_N]:
        views_man = v.get("views", 0) // 10_000
        v_url = (v.get("url") or "").strip()
        blocks.append({
            "object": "block",
            "type": "callout",
            "callout": {
                "rich_text": (
                    _rich_text(f"{v.get('title', '')}\n", bold=True)
                    + _rich_text(f"{v.get('upload_date', '')} · {v.get('channel', '')} · 조회수 {views_man}만회 · {v.get('duration_min', 0)}분\n")
                    + _rich_text(f"{v.get('summary_line', '')}\n")
                    + _rich_text_link(v_url or "링크", v_url)
                ),
                "icon": {"type": "emoji", "emoji": "🎥"},
                "color": "gray_background",
            },
        })

    blocks.append({"object": "block", "type": "divider", "divider": {}})

    # 뉴스 최대 10개 (링크 포함)
    for i, item in enumerate(items, 1):
        emoji = TAG_EMOJI.get(item["tag"], "🔵")
        blocks.append({
            "object": "block",
            "type": "heading_3",
            "heading_3": {"rich_text": _rich_text(f"{emoji} #{i} | {item['tag']}")},
        })
        blocks.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": _rich_text(item["title"], bold=True)},
        })
        blocks.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": _rich_text((item["summary"] or "")[:2000])},
        })
        blocks.append({
            "object": "block",
            "type": "callout",
            "callout": {
                "rich_text": _rich_text("NP 패밀리 임팩트: ", bold=True) + _rich_text(item["np_impact"][:1500]),
                "icon": {"type": "emoji", "emoji": "💡"},
                "color": "blue_background",
            },
        })
        # 기사 링크 (클릭 가능)
        link_url = (item.get("link") or "").strip()
        if link_url:
            blocks.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": _rich_text("🔗 ", bold=False) + _rich_text_link("기사 보기", link_url),
                },
            })

    return blocks


def _archive_previous_youtube(section_blocks: list[dict]) -> bool:
    """메인 페이지에서 교체될 이전 주 YouTube 블록을 아카이브 서브 페이지로 저장."""
    if not NOTION_API_KEY or not NOTION_ARCHIVE_PAGE_ID:
        return False

    # YouTube callout 블록 (🎥 아이콘) 추출
    yt_blocks = []
    for b in section_blocks:
        if b.get("type") == "callout":
            icon = (b.get("callout") or {}).get("icon") or {}
            if icon.get("emoji") == "🎥":
                callout = b["callout"]
                yt_blocks.append({
                    "object": "block",
                    "type": "callout",
                    "callout": {
                        "rich_text": callout.get("rich_text", []),
                        "icon": {"type": "emoji", "emoji": "🎥"},
                        "color": callout.get("color", "gray_background"),
                    },
                })

    if not yt_blocks:
        return False

    # 날짜 callout (📅 아이콘)에서 날짜 추출
    date_label = ""
    for b in section_blocks:
        if b.get("type") == "callout":
            icon = (b.get("callout") or {}).get("icon") or {}
            if icon.get("emoji") == "📅":
                rt = (b.get("callout") or {}).get("rich_text") or []
                date_label = "".join(t.get("plain_text", "") for t in rt).split("업데이트")[0].strip()
                break
    if not date_label:
        date_label = (datetime.now() - timedelta(days=7)).strftime("%Y.%m.%d")

    page_title = f"🎬 {date_label} AI 추천 영상 아카이브"

    children = [
        {
            "object": "block",
            "type": "heading_2",
            "heading_2": {"rich_text": _rich_text(f"🎬 AI 영상 TOP ({date_label})")},
        },
    ] + yt_blocks

    try:
        r = requests.post(
            "https://api.notion.com/v1/pages",
            headers=_notion_headers(),
            json={
                "parent": {"page_id": NOTION_ARCHIVE_PAGE_ID},
                "properties": {
                    "title": {"title": [{"text": {"content": page_title}}]},
                },
                "children": children[:95],
            },
            timeout=30,
        )
        _notion_sleep()
        r.raise_for_status()
        print(f"  📂 [Notion] 이전 주 YouTube 영상 아카이브 완료: {page_title}")
        return True
    except Exception as e:
        print(f"  ⚠️ [Notion] YouTube 아카이브 실패: {e}")
        return False


def notion_replace_this_week_section(briefing: dict) -> bool:
    """메인 페이지 'This Week in AI' H2 아래 블록 삭제 후 새 블록 삽입."""
    if not NOTION_API_KEY or not NOTION_MAIN_PAGE_ID:
        print("  📝 [Notion] NOTION_API_KEY 또는 NOTION_MAIN_PAGE_ID 없음 → 스킵")
        return False
    try:
        blocks = []
        url = f"https://api.notion.com/v1/blocks/{NOTION_MAIN_PAGE_ID}/children?page_size=100"
        while url:
            r = requests.get(url, headers=_notion_headers())
            _notion_sleep()
            r.raise_for_status()
            data = r.json()
            blocks.extend(data.get("results", []))
            if data.get("has_more") and data.get("next_cursor"):
                url = f"https://api.notion.com/v1/blocks/{NOTION_MAIN_PAGE_ID}/children?page_size=100&start_cursor={data['next_cursor']}"
            else:
                url = None

        target_h2_id = None
        target_index = -1
        for i, b in enumerate(blocks):
            if b.get("type") != "heading_2":
                continue
            rt = (b.get("heading_2") or {}).get("rich_text") or []
            text = " ".join((t.get("plain_text") or "") for t in rt)
            norm = (text or "").lower()
            if "this week in ai" in norm or ("🔥" in text and "ai" in norm and "week" in norm):
                target_h2_id = b["id"]
                target_index = i
                break
        if not target_h2_id:
            # 없으면 자동 생성 후 다시 시도
            print("  📝 [Notion] 'This Week in AI' heading_2 블록 없음 → 자동 생성")
            r = requests.patch(
                f"https://api.notion.com/v1/blocks/{NOTION_MAIN_PAGE_ID}/children",
                headers=_notion_headers(),
                json={
                    "children": [
                        {
                            "type": "heading_2",
                            "heading_2": {"rich_text": _rich_text("🔥 This Week in AI")},
                        }
                    ]
                },
                timeout=20,
            )
            _notion_sleep()
            r.raise_for_status()
            created = (r.json().get("results") or [{}])[-1]
            target_h2_id = created.get("id")
            if not target_h2_id:
                print("  📝 [Notion] heading_2 자동 생성 실패 (응답에 id 없음)")
                return False

            # 새로 생성했으니 최신 children 다시 가져오기
            blocks = []
            url = f"https://api.notion.com/v1/blocks/{NOTION_MAIN_PAGE_ID}/children?page_size=100"
            while url:
                r2 = requests.get(url, headers=_notion_headers(), timeout=20)
                _notion_sleep()
                r2.raise_for_status()
                data2 = r2.json()
                blocks.extend(data2.get("results", []))
                if data2.get("has_more") and data2.get("next_cursor"):
                    url = f"https://api.notion.com/v1/blocks/{NOTION_MAIN_PAGE_ID}/children?page_size=100&start_cursor={data2['next_cursor']}"
                else:
                    url = None
            for i, b in enumerate(blocks):
                if b.get("id") == target_h2_id:
                    target_index = i
                    break

        # 이전 주 YouTube 블록 아카이브
        section_blocks = []
        for j in range(target_index + 1, len(blocks)):
            if blocks[j].get("type") == "heading_2":
                break
            section_blocks.append(blocks[j])
        _archive_previous_youtube(section_blocks)

        to_delete = [b["id"] for b in section_blocks]

        for bid in to_delete:
            requests.patch(
                f"https://api.notion.com/v1/blocks/{bid}",
                headers=_notion_headers(),
                json={"archived": True},
            )
            _notion_sleep()
        print(f"  📝 [Notion] 기존 블록 {len(to_delete)}개 삭제")

        new_blocks = _build_this_week_blocks(briefing)
        for blk in new_blocks:
            for k in list(blk.keys()):
                if k not in ("object", "type", "paragraph", "heading_3", "callout", "divider"):
                    blk.pop(k, None)

        # Notion API 최대 100블록 제한 → 배치로 나눠서 추가
        BATCH_SIZE = 95
        after_id = target_h2_id
        for batch_start in range(0, len(new_blocks), BATCH_SIZE):
            batch = new_blocks[batch_start:batch_start + BATCH_SIZE]
            body = {"children": batch, "after": after_id}
            r = requests.patch(
                f"https://api.notion.com/v1/blocks/{NOTION_MAIN_PAGE_ID}/children",
                headers=_notion_headers(),
                json=body,
                timeout=30,
            )
            _notion_sleep()
            r.raise_for_status()
            # 다음 배치는 이번 배치 마지막 블록 뒤에 추가
            results = r.json().get("results", [])
            if results:
                after_id = results[-1].get("id", after_id)
        print(f"  📝 [Notion] 메인 페이지 'This Week in AI' 섹션 업데이트 완료 ({len(new_blocks)}블록)")
        return True
    except requests.RequestException as e:
        print(f"  📝 [Notion] 메인 페이지 업데이트 실패: {e}")
        if getattr(e, "response", None) and e.response is not None:
            print("  응답:", e.response.text[:500])
        return False


# ---------------------------------------------------------------------------
# 5. 트렌드 아카이브 — 서브 페이지 생성 (NOTION_ARCHIVE_PAGE_ID)
# ---------------------------------------------------------------------------
def notion_create_archive_page(briefing: dict) -> bool:
    """아카이브 부모 페이지 아래에 이번 주 브리핑을 새 서브 페이지로 저장.
    NOTION_ARCHIVE_PAGE_ID: Notion '트렌드 아카이브' 페이지의 ID"""
    if not NOTION_API_KEY or not NOTION_ARCHIVE_PAGE_ID:
        print("  📂 [Notion] NOTION_ARCHIVE_PAGE_ID 없음 → 아카이브 서브 페이지 생성 스킵")
        return False
    try:
        items = briefing["items"]
        videos = briefing.get("youtube_videos") or []
        page_title = f"📋 {briefing['week_label']} AI 트렌드 브리핑"
        date_display = briefing["date"].replace("-", ".")

        children: list[dict] = []

        # 헤더 Callout
        children.append({
            "object": "block",
            "type": "callout",
            "callout": {
                "rich_text": _rich_text(f"{date_display} 기준 · {briefing['week_label']}", bold=True),
                "icon": {"type": "emoji", "emoji": "📅"},
                "color": "blue_background",
            },
        })
        children.append({"object": "block", "type": "divider", "divider": {}})

        # 뉴스 섹션 헤더
        children.append({
            "object": "block",
            "type": "heading_2",
            "heading_2": {"rich_text": _rich_text("🔥 이번 주 AI 뉴스")},
        })

        # 뉴스 아이템 (링크 포함)
        for i, item in enumerate(items, 1):
            emoji = TAG_EMOJI.get(item["tag"], "🔵")
            children.append({
                "object": "block",
                "type": "heading_3",
                "heading_3": {"rich_text": _rich_text(f"{emoji} #{i} | {item['tag']}")},
            })
            children.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": _rich_text(item["title"], bold=True)},
            })
            children.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": _rich_text((item["summary"] or "")[:2000])},
            })
            children.append({
                "object": "block",
                "type": "callout",
                "callout": {
                    "rich_text": _rich_text("NP 패밀리 임팩트: ", bold=True) + _rich_text(item["np_impact"][:1500]),
                    "icon": {"type": "emoji", "emoji": "💡"},
                    "color": "blue_background",
                },
            })
            link_url = (item.get("link") or "").strip()
            if link_url:
                children.append({
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": _rich_text("🔗 ", bold=False) + _rich_text_link("기사 보기", link_url),
                    },
                })

        children.append({"object": "block", "type": "divider", "divider": {}})

        # 추천 도구
        tool = briefing["recommended_tool"]
        tool_rt = (
            _rich_text("이번 주 추천 도구\n", bold=True)
            + _rich_text(f"{tool['name']}", bold=True)
            + _rich_text(f" · {tool.get('difficulty', '초보')} · {tool.get('price', '무료')}\n")
            + _rich_text((tool.get("description") or "")[:500] + "\n")
            + _rich_text("✨ 이렇게 써보세요: ", bold=True)
            + _rich_text((tool.get("tip") or "")[:500])
        )
        children.append({
            "object": "block",
            "type": "callout",
            "callout": {
                "rich_text": tool_rt,
                "icon": {"type": "emoji", "emoji": "🛠️"},
                "color": "green_background",
            },
        })

        # YouTube는 별도 페이지(NP AI 자동화 추천 영상)에서 관리 → 아카이브엔 링크만 표시
        if videos:
            children.append({"object": "block", "type": "divider", "divider": {}})
            children.append({
                "object": "block", "type": "callout",
                "callout": {
                    "rich_text": _rich_text(f"🎬 이번 달 AI 자동화 추천 영상 {len(videos)}개는 'NP AI 자동화 추천 영상' 페이지에서 확인하세요."),
                    "icon": {"type": "emoji", "emoji": "🎥"},
                    "color": "gray_background",
                },
            })

        # Notion API: 페이지 생성 시 한 번에 최대 100블록 → 배치 처리
        MAX_BLOCKS = 95
        first_batch = children[:MAX_BLOCKS]
        remaining = children[MAX_BLOCKS:]

        r = requests.post(
            "https://api.notion.com/v1/pages",
            headers=_notion_headers(),
            json={
                "parent": {"page_id": NOTION_ARCHIVE_PAGE_ID},
                "properties": {
                    "title": {"title": [{"text": {"content": page_title}}]},
                },
                "children": first_batch,
            },
            timeout=30,
        )
        _notion_sleep()
        r.raise_for_status()
        new_page_id = r.json().get("id")
        print(f"  📂 [Notion] 아카이브 서브 페이지 생성 완료: {page_title}")

        # 나머지 블록 추가
        if remaining and new_page_id:
            for i in range(0, len(remaining), MAX_BLOCKS):
                batch = remaining[i:i + MAX_BLOCKS]
                r2 = requests.patch(
                    f"https://api.notion.com/v1/blocks/{new_page_id}/children",
                    headers=_notion_headers(),
                    json={"children": batch},
                    timeout=30,
                )
                _notion_sleep()
                r2.raise_for_status()

        return True
    except requests.RequestException as e:
        print(f"  📂 [Notion] 아카이브 서브 페이지 생성 실패: {e}")
        if getattr(e, "response", None):
            print("  응답:", e.response.text[:500])
        return False


# ---------------------------------------------------------------------------
# 5b. YouTube 월간 업데이트 — 상태 관리 + "NP AI 자동화 추천 영상" 페이지 추가
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    """weekly_update_state.json에서 상태 읽기."""
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state: dict):
    """weekly_update_state.json에 상태 저장."""
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _should_update_youtube(state: dict) -> bool:
    """마지막 YouTube 업데이트로부터 30일 이상 지났으면 True."""
    last = state.get("last_youtube_update")
    if not last:
        return True
    try:
        last_dt = datetime.strptime(last, "%Y-%m-%d")
        return (datetime.now() - last_dt).days >= YOUTUBE_UPDATE_INTERVAL_DAYS
    except Exception:
        return True


def _get_or_create_youtube_page_id() -> str:
    """NOTION_YOUTUBE_PAGE_ID 없으면 메인 페이지 하위에서 찾거나 새로 생성."""
    if NOTION_YOUTUBE_PAGE_ID:
        return NOTION_YOUTUBE_PAGE_ID

    if not NOTION_API_KEY or not NOTION_MAIN_PAGE_ID:
        return ""

    # 메인 페이지 하위 블록에서 제목이 일치하는 child_page 찾기
    try:
        r = requests.get(
            f"https://api.notion.com/v1/blocks/{NOTION_MAIN_PAGE_ID}/children?page_size=100",
            headers=_notion_headers(), timeout=15,
        )
        _notion_sleep()
        r.raise_for_status()
        for block in r.json().get("results", []):
            if block.get("type") == "child_page":
                title = block.get("child_page", {}).get("title", "")
                if YOUTUBE_PAGE_TITLE in title:
                    return block["id"].replace("-", "")
    except Exception:
        pass

    # 없으면 새 페이지 생성
    try:
        r = requests.post(
            "https://api.notion.com/v1/pages",
            headers=_notion_headers(),
            json={
                "parent": {"page_id": NOTION_MAIN_PAGE_ID},
                "properties": {"title": {"title": [{"text": {"content": YOUTUBE_PAGE_TITLE}}]}},
                "children": [{
                    "object": "block", "type": "callout",
                    "callout": {
                        "rich_text": _rich_text("AI 자동화 관련 추천 유튜브 영상 (매월 업데이트)"),
                        "icon": {"type": "emoji", "emoji": "🎬"},
                        "color": "gray_background",
                    },
                }],
            },
            timeout=30,
        )
        _notion_sleep()
        r.raise_for_status()
        page_id = r.json().get("id", "").replace("-", "")
        print(f"  🎬 [Notion] '{YOUTUBE_PAGE_TITLE}' 페이지 생성 완료 (ID: {page_id})")
        return page_id
    except Exception as e:
        print(f"  ⚠️ [Notion] YouTube 페이지 생성 실패: {e}")
        return ""


def notion_append_youtube_monthly(videos: list[dict], week_label: str) -> bool:
    """'NP AI 자동화 추천 영상' 페이지에 이번 달 영상 섹션을 추가."""
    if not NOTION_API_KEY or not videos:
        return False

    page_id = _get_or_create_youtube_page_id()
    if not page_id:
        print("  ⚠️ [Notion] YouTube 페이지 ID 없음 → 스킵")
        return False

    now = datetime.now()
    month_label = f"{now.year}년 {now.month}월"

    blocks = [
        {"object": "block", "type": "divider", "divider": {}},
        {
            "object": "block", "type": "heading_2",
            "heading_2": {"rich_text": _rich_text(f"🗓️ {month_label} AI 자동화 추천 영상 TOP")},
        },
        {
            "object": "block", "type": "callout",
            "callout": {
                "rich_text": _rich_text(f"수집 기준: 최근 3개월 · 조회수 순 · {week_label} 업데이트"),
                "icon": {"type": "emoji", "emoji": "📅"},
                "color": "blue_background",
            },
        },
    ]

    for v in videos[:YOUTUBE_TOP_N]:
        views_man = v.get("views", 0) // 10_000
        v_url = (v.get("url") or "").strip()
        blocks.append({
            "object": "block", "type": "callout",
            "callout": {
                "rich_text": (
                    _rich_text(f"{v.get('title', '')}\n", bold=True)
                    + _rich_text(f"{v.get('upload_date', '')} · {v.get('channel', '')} · 조회수 {views_man}만회 · {v.get('duration_min', 0)}분\n")
                    + _rich_text(f"{v.get('summary_line', '')}\n")
                    + _rich_text_link(v_url or "링크", v_url)
                ),
                "icon": {"type": "emoji", "emoji": "🎥"},
                "color": "gray_background",
            },
        })

    try:
        MAX_BLOCKS = 95
        for i in range(0, len(blocks), MAX_BLOCKS):
            batch = blocks[i:i + MAX_BLOCKS]
            r = requests.patch(
                f"https://api.notion.com/v1/blocks/{page_id}/children",
                headers=_notion_headers(),
                json={"children": batch},
                timeout=30,
            )
            _notion_sleep()
            r.raise_for_status()
        print(f"  🎬 [Notion] '{YOUTUBE_PAGE_TITLE}' 페이지에 {month_label} 영상 {len(videos)}개 추가 완료")
        return True
    except Exception as e:
        print(f"  ⚠️ [Notion] YouTube 페이지 업데이트 실패: {e}")
        return False


# ---------------------------------------------------------------------------
# 5c. 트렌드 아카이브 DB (기존 DB 방식 — NOTION_ARCHIVE_DB_ID 있을 때만 사용)
def notion_add_archive_record(briefing: dict) -> bool:
    """아카이브 DB에 이번 주 레코드 추가. 본문에 핵심 뉴스 + 유튜브 TOP 목록."""
    if not NOTION_API_KEY or not NOTION_ARCHIVE_DB_ID:
        print("  📂 [Notion] 아카이브 DB ID 없음 → 스킵")
        return False
    try:
        items = briefing["items"]
        videos = briefing.get("youtube_videos") or []
        title_prop = briefing["week_label"]
        date_val = briefing["date"]
        tags = list({item["tag"] for item in items}) or ["기술"]
        summary_text = " / ".join(item["title"] for item in items) or "이번 주 수집 뉴스 없음"

        properties = {
            "제목": {"title": [{"text": {"content": title_prop}}]},
            "발행일": {"date": {"start": date_val}},
            "태그": {"multi_select": [{"name": t} for t in tags]},
            "요약": {"rich_text": [{"text": {"content": summary_text[:2000]}}]},
        }

        children = []
        for item in items:
            children.append({
                "object": "block",
                "type": "heading_3",
                "heading_3": {"rich_text": _rich_text(item["title"])},
            })
            children.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": _rich_text((item["summary"] or "")[:2000])},
            })
            children.append({
                "object": "block",
                "type": "callout",
                "callout": {
                    "rich_text": _rich_text(f"NP 패밀리 임팩트: {item['np_impact']}"),
                    "icon": {"type": "emoji", "emoji": "💡"},
                    "color": "blue_background",
                },
            })
        if videos:
            children.append({
                "object": "block",
                "type": "heading_3",
                "heading_3": {"rich_text": _rich_text("🎬 이번 주 AI 영상 TOP")},
            })
            for v in videos[:YOUTUBE_TOP_N]:
                children.append({
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {"rich_text": _rich_text(f"· {v.get('title', '')} | {v.get('url', '')}")},
                })

        r = requests.post(
            "https://api.notion.com/v1/pages",
            headers=_notion_headers(),
            json={
                "parent": {"database_id": NOTION_ARCHIVE_DB_ID},
                "properties": properties,
                "children": children,
            },
        )
        _notion_sleep()
        r.raise_for_status()
        print("  📂 [Notion] 트렌드 아카이브 DB 레코드 추가 완료")
        return True
    except requests.RequestException as e:
        print(f"  📂 [Notion] 아카이브 DB 추가 실패: {e}")
        if getattr(e, "response", None):
            print("  응답:", e.response.text[:500])
        return False


# ---------------------------------------------------------------------------
# 6. AI 툴박스 DB (중복 시 스킵)
# ---------------------------------------------------------------------------
def notion_add_tool_if_new(briefing: dict) -> bool:
    """추천 도구가 툴박스 DB에 없으면 추가. 도구명으로 검색."""
    if not NOTION_API_KEY or not NOTION_TOOLBOX_DB_ID:
        print("  🛠️ [Notion] 툴박스 DB ID 없음 → 스킵")
        return False
    try:
        tool = briefing["recommended_tool"]
        name = (tool.get("name") or "").strip()[:255]
        if not name:
            return False

        r = requests.post(
            f"https://api.notion.com/v1/databases/{NOTION_TOOLBOX_DB_ID}/query",
            headers=_notion_headers(),
            json={"filter": {"property": "도구명", "title": {"equals": name}}},
        )
        _notion_sleep()
        r.raise_for_status()
        if r.json().get("results"):
            print("  🛠️ [Notion] 툴박스 DB에 동일 도구명 존재 → 스킵")
            return True

        props = {
            "도구명": {"title": [{"text": {"content": name}}]},
            "난이도": {"select": {"name": tool.get("difficulty") or "초보"}},
            "가격": {"select": {"name": tool.get("price") or "무료"}},
            "한줄 설명": {"rich_text": [{"text": {"content": (tool.get("description") or "")[:2000]}}]},
            "추천 용도": {"rich_text": [{"text": {"content": (tool.get("tip") or "")[:2000]}}]},
            "NP패밀리 사용": {"checkbox": False},
        }
        url_val = (tool.get("url") or "").strip()
        if url_val:
            props["공식 링크"] = {"url": url_val}
        payload = {"parent": {"database_id": NOTION_TOOLBOX_DB_ID}, "properties": props}
        r = requests.post("https://api.notion.com/v1/pages", headers=_notion_headers(), json=payload)
        _notion_sleep()
        r.raise_for_status()
        print("  🛠️ [Notion] AI 툴박스 DB에 추천 도구 추가 완료")
        return True
    except requests.RequestException as e:
        print(f"  🛠️ [Notion] 툴박스 DB 추가 실패: {e}")
        if getattr(e, "response", None):
            print("  응답:", e.response.text[:500])
        return False


# ---------------------------------------------------------------------------
# 7. 마크다운 백업
# ---------------------------------------------------------------------------
def save_markdown_backup(briefing: dict) -> str:
    path = Path(__file__).parent / "backups"
    path.mkdir(exist_ok=True)
    safe_date = briefing["date"].replace("-", "")
    fpath = path / f"ai_briefing_{safe_date}.md"
    lines = [
        f"# {briefing['week_label']} AI 브리핑",
        f"업데이트: {briefing['date']}\n",
        "---\n",
    ]
    for i, item in enumerate(briefing["items"], 1):
        emoji = TAG_EMOJI.get(item["tag"], "🔵")
        lines.append(f"## {emoji} #{i} | {item['tag']}\n")
        lines.append(f"**{item['title']}**\n\n")
        lines.append(f"{item['summary']}\n\n")
        lines.append(f"**NP 패밀리 임팩트:** {item['np_impact']}\n\n")
        link = item.get("link") or ""
        lines.append(f"🔗 [기사 보기]({link})\n\n" if link else "")
    tool = briefing["recommended_tool"]
    lines.append("---\n## 🛠️ 이번 주 추천 도구\n\n")
    lines.append(f"**{tool['name']}** · {tool.get('difficulty', '초보')} · {tool.get('price', '무료')}\n\n")
    lines.append(f"{tool.get('description', '')}\n\n")
    lines.append(f"✨ 이렇게 써보세요: {tool.get('tip', '')}\n")
    videos = briefing.get("youtube_videos") or []
    if videos:
        lines.append("\n---\n## 🎬 이번 주 AI 영상 TOP\n\n")
        for v in videos[:YOUTUBE_TOP_N]:
            lines.append(f"- **{v.get('title', '')}** · {v.get('upload_date', '')} · {v.get('channel', '')} · 조회수 {v.get('views', 0)//10000}만회\n")
            lines.append(f"  {v.get('summary_line', '')}\n")
            lines.append(f"  {v.get('url', '')}\n")
    fpath.write_text("\n".join(lines), encoding="utf-8")
    print(f"  💾 [백업] {fpath}")
    return str(fpath)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    print("=== NP Family AI Hub 주간 업데이트 ===\n")

    # 1. RSS 뉴스 수집 + 필터링 (매주 실행)
    print("📡 1. RSS에서 AI 뉴스 수집 (투자/경영 뉴스 제외)")
    try:
        articles = fetch_news()
    except Exception as e:
        print(f"  ❌ [오류] 뉴스 수집 실패: {e}")
        articles = []

    if not articles:
        print("  ⚠️ 수집된 기사 없음 (RSS 오류 또는 전부 필터됨).")
        articles = []

    # 2. 유튜브 영상 수집 (매주 실행)
    youtube_videos = []
    print("\n🎬 2. YouTube 'AI 자동화' 영상 수집 (최근 3개월, 조회수 순)")
    try:
        youtube_videos = fetch_youtube_videos()
        if youtube_videos:
            youtube_videos = _summarize_youtube_videos(youtube_videos)
    except Exception as e:
        print(f"  ⚠️ [YouTube] 수집 실패: {e}")
        youtube_videos = []

    # 3. 브리핑 생성
    print(f"\n🤖 3. 브리핑 생성 (핵심 뉴스 최대 {MAX_NEWS_ITEMS}개 + 추천 도구 + 유튜브 TOP)")
    try:
        briefing = generate_briefing(articles, youtube_videos)
        print(f"   선정: {briefing['week_label']}, 뉴스 {len(briefing['items'])}개, 영상 {len(briefing.get('youtube_videos') or [])}개")
    except Exception as e:
        print(f"  ❌ [오류] 브리핑 생성 실패: {e}")
        raise

    # 4. 마크다운 백업 (항상)
    print("\n💾 4. 마크다운 백업 저장")
    try:
        save_markdown_backup(briefing)
    except Exception as e:
        print(f"  ⚠️ [백업] 저장 실패: {e}")

    # 5. 뉴스 아카이브 서브 페이지 생성 (매주 — AI 트렌드 아카이브)
    print("\n📂 5. 뉴스 아카이브 서브 페이지 생성 (AI 트렌드 아카이브)")
    if NOTION_API_KEY and NOTION_ARCHIVE_PAGE_ID:
        notion_create_archive_page(briefing)
    else:
        print("  📂 [Notion] NOTION_ARCHIVE_PAGE_ID 없음 → 스킵")

    # 6. Notion 메인 페이지 업데이트 (매주)
    print("\n📝 6. Notion 메인 페이지 업데이트")
    if NOTION_API_KEY and NOTION_MAIN_PAGE_ID:
        notion_replace_this_week_section(briefing)
    else:
        print("  📝 [Notion] NOTION_API_KEY 또는 NOTION_MAIN_PAGE_ID 없음 → 스킵")

    print("\n=== 완료 ===")


if __name__ == "__main__":
    main()
