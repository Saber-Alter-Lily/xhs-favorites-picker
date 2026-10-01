from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse, urlsplit


APP_VERSION = "0.6.2"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)
REFERER = "https://www.xiaohongshu.com/"
EPHEMERAL_IMAGE_ROUTE_PREFIX = re.compile(
    r"^\d{12}/[0-9a-f]{32}/", re.IGNORECASE
)
INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_PRINT_LOCK = threading.Lock()
ROOT_DIR = Path(__file__).resolve().parent
COOKIE_FILE = ROOT_DIR / ".redbook-cookies.json"
FAVORITES_CACHE_FILE = ROOT_DIR / ".favorites-cache.json"
LOCAL_REDBOOK_CLI = ROOT_DIR / ".runtime" / "node_modules" / "@lucasygu" / "redbook" / "dist" / "cli.js"


class PickerError(RuntimeError):
    pass


class RiskControlError(PickerError):
    """Raised when the upstream platform asks for verification or rate limiting."""


_REDBOOK_GATE = threading.Lock()
_LAST_REDBOOK_CALL = 0.0
_MIN_REDBOOK_INTERVAL_SECONDS = 0.9
_RISK_BLOCK_UNTIL = 0.0
_RISK_BLOCK_SECONDS = 15 * 60
_RISK_MARKERS = (
    "needverify", "captcha", "300012", "验证码", "风控",
    "访问频繁", "操作频繁", "too many requests", "rate limit",
)


def _sanitize_error_text(value: str) -> str:
    value = re.sub(r"([?&](?:xsec_token|xsec_source|token)=)[^&\s]+", r"\1<redacted>", value, flags=re.I)
    value = re.sub(r"(?i)(web_session|a1)=([^;\s]+)", r"\1=<redacted>", value)
    return value[:1200]


def _risk_block_message() -> str:
    remaining = max(0, int(_RISK_BLOCK_UNTIL - time.monotonic()))
    minutes = max(1, (remaining + 59) // 60)
    return f"检测到平台验证/限流信号，已自动暂停联网请求约 {minutes} 分钟。请不要连续重试；可稍后重新打开程序。"


def _looks_like_risk_control(text: str) -> bool:
    lowered = text.casefold()
    return any(marker.casefold() in lowered for marker in _RISK_MARKERS)


def _pace_redbook_requests() -> None:
    global _LAST_REDBOOK_CALL
    now = time.monotonic()
    if now < _RISK_BLOCK_UNTIL:
        raise RiskControlError(_risk_block_message())
    wait = _MIN_REDBOOK_INTERVAL_SECONDS - (now - _LAST_REDBOOK_CALL)
    if wait > 0:
        time.sleep(wait)
    _LAST_REDBOOK_CALL = time.monotonic()


@dataclass(frozen=True, slots=True)
class Favorite:
    note_id: str
    title: str
    author: str
    web_url: str
    cover_url: str = ""
    author_id: str = ""
    source: str = "favorites"
    source_key: str = ""


@dataclass(frozen=True, slots=True)
class PostResult:
    note_id: str
    title: str
    status: str
    image_count: int = 0
    downloaded: int = 0
    skipped: int = 0
    error: str = ""
    folder: str = ""


@dataclass(frozen=True, slots=True)
class UserPostsPage:
    notes: tuple[Favorite, ...]
    has_more: bool
    cursor: str


def eprint(message: str) -> None:
    print(message, file=sys.stderr)


def safe_print(message: str) -> None:
    with _PRINT_LOCK:
        print(message, flush=True)


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def redbook_command() -> list[str]:
    """Return the project-local redbook command when available."""
    if LOCAL_REDBOOK_CLI.exists():
        if not command_exists("node"):
            raise PickerError("未找到 Node.js。请重新运行 setup.bat。")
        return ["node", str(LOCAL_REDBOOK_CLI)]
    if command_exists("redbook"):
        return ["redbook"]
    raise PickerError("未找到 redbook。请先运行 setup.bat。")


def run_redbook_json(args: list[str], timeout: int = 180) -> Any:
    global _RISK_BLOCK_UNTIL, _LAST_REDBOOK_CALL
    cmd = [*redbook_command(), *args, "--json"]
    env = os.environ.copy()
    if COOKIE_FILE.exists():
        # Use redbook's saved-cookie-file interface. Cookie values stay out of
        # command-line arguments, browser-visible JSON and our metadata/logs.
        env["REDBOOK_COOKIE_FILE"] = str(COOKIE_FILE)
        env["REDBOOK_PLATFORM"] = "xhs"
    with _REDBOOK_GATE:
        _pace_redbook_requests()
        try:
            proc = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise PickerError(f"redbook 超时：{' '.join(args[:2])}") from exc
        finally:
            # Count failed calls too; rapid retries after errors are undesirable.
            _LAST_REDBOOK_CALL = time.monotonic()
    if proc.returncode != 0:
        detail = _sanitize_error_text((proc.stderr or proc.stdout or "unknown error").strip())
        if _looks_like_risk_control(detail):
            _RISK_BLOCK_UNTIL = time.monotonic() + _RISK_BLOCK_SECONDS
            raise RiskControlError(_risk_block_message())
        raise PickerError(f"redbook 执行失败：{detail}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        preview = _sanitize_error_text(proc.stdout[:300].replace("\n", " "))
        raise PickerError(f"redbook 返回了无法解析的 JSON：{preview}") from exc


def _extract_note_id(item: dict[str, Any]) -> str:
    for key in ("note_id", "noteId", "id"):
        value = item.get(key)
        if value:
            return str(value)
    card = item.get("note_card") or item.get("noteCard")
    if isinstance(card, dict):
        return _extract_note_id(card)
    return ""


def _extract_url(item: dict[str, Any]) -> str:
    for key in ("webUrl", "web_url", "url", "share_url", "shareUrl"):
        value = item.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value
    card = item.get("note_card") or item.get("noteCard")
    if isinstance(card, dict):
        return _extract_url(card)
    return ""


def _extract_title(item: dict[str, Any]) -> str:
    for key in ("display_title", "displayTitle", "title", "name"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    card = item.get("note_card") or item.get("noteCard")
    if isinstance(card, dict):
        return _extract_title(card)
    return ""


def _extract_author(item: dict[str, Any]) -> str:
    for key in ("user", "author", "user_info", "userInfo"):
        value = item.get(key)
        if isinstance(value, dict):
            for name_key in ("nickname", "nickName", "name", "user_name", "userName"):
                name = value.get(name_key)
                if isinstance(name, str) and name.strip():
                    return name.strip()
    card = item.get("note_card") or item.get("noteCard")
    if isinstance(card, dict):
        return _extract_author(card)
    return ""


def _extract_author_id(item: dict[str, Any]) -> str:
    for key in ("user", "author", "user_info", "userInfo"):
        value = item.get(key)
        if isinstance(value, dict):
            for id_key in ("user_id", "userId", "id", "userid"):
                author_id = value.get(id_key)
                if author_id:
                    return str(author_id)
    card = item.get("note_card") or item.get("noteCard")
    if isinstance(card, dict):
        return _extract_author_id(card)
    return ""


def _extract_cover(item: dict[str, Any]) -> str:
    cover = item.get("cover")
    if isinstance(cover, str) and cover.startswith(("http://", "https://")):
        return cover
    if isinstance(cover, dict):
        url = _candidate_url(cover)
        if url:
            return url

    for key in ("image_list", "imageList", "images"):
        images = item.get(key)
        if isinstance(images, list) and images:
            first = images[0]
            if isinstance(first, str) and first.startswith(("http://", "https://")):
                return first
            if isinstance(first, dict):
                url = _candidate_url(first)
                if url:
                    return url

    card = item.get("note_card") or item.get("noteCard")
    if isinstance(card, dict):
        return _extract_cover(card)
    return ""


def _note_id_from_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if "explore" in parts:
        index = parts.index("explore")
        if index + 1 < len(parts):
            return parts[index + 1]
    query = parse_qs(parsed.query)
    for key in ("note_id", "noteId"):
        values = query.get(key)
        if values:
            return values[0]
    return ""


def normalize_notes(
    payload: Any,
    *,
    source: str = "favorites",
    source_key: str = "",
) -> list[Favorite]:
    if isinstance(payload, dict):
        for key in ("notes", "items", "data", "favorites"):
            value = payload.get(key)
            if isinstance(value, list):
                payload = value
                break
    if not isinstance(payload, list):
        raise PickerError("帖子列表 JSON 结构异常：未找到列表")

    result: list[Favorite] = []
    seen: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        url = _extract_url(item)
        note_id = _extract_note_id(item) or _note_id_from_url(url)
        if not note_id or not url or note_id in seen:
            continue
        seen.add(note_id)
        result.append(
            Favorite(
                note_id=note_id,
                title=_extract_title(item) or "（无标题）",
                author=_extract_author(item) or "未知作者",
                web_url=url,
                cover_url=_extract_cover(item),
                author_id=_extract_author_id(item),
                source=source,
                source_key=source_key,
            )
        )
    return result


def normalize_favorites(payload: Any) -> list[Favorite]:
    return normalize_notes(payload, source="favorites")


def _favorite_cache_item(note: Favorite) -> dict[str, Any]:
    # Never persist tokenized note URLs. Cached favorites are browsing metadata
    # only; a live favorites refresh is performed before downloading when needed.
    return {
        "note_id": note.note_id,
        "title": note.title,
        "author": note.author,
        "author_id": note.author_id,
        "canonical_url": f"https://www.xiaohongshu.com/explore/{note.note_id}",
        "cover_url": stable_image_url(note.cover_url) if note.cover_url else "",
    }


def save_favorites_cache(notes: list[Favorite], path: Path | None = None) -> Path:
    path = path or FAVORITES_CACHE_FILE
    payload = {
        "version": 1,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "items": [_favorite_cache_item(note) for note in notes],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)
    return path


def load_favorites_cache(path: Path | None = None) -> tuple[list[Favorite], str]:
    path = path or FAVORITES_CACHE_FILE
    if not path.exists():
        return [], ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], ""
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        return [], ""
    notes: list[Favorite] = []
    for item in payload["items"]:
        if not isinstance(item, dict):
            continue
        note_id = str(item.get("note_id") or "").strip()
        if not note_id:
            continue
        notes.append(Favorite(
            note_id=note_id,
            title=str(item.get("title") or "（无标题）"),
            author=str(item.get("author") or "未知作者"),
            web_url=str(item.get("canonical_url") or f"https://www.xiaohongshu.com/explore/{note_id}"),
            cover_url=str(item.get("cover_url") or ""),
            author_id=str(item.get("author_id") or ""),
            source="favorites",
            source_key="cache",
        ))
    return notes, str(payload.get("fetched_at") or "")


def fetch_favorites() -> list[Favorite]:
    notes = normalize_favorites(run_redbook_json(["favorites", "--all"], timeout=300))
    try:
        save_favorites_cache(notes)
    except OSError:
        pass
    return notes


def fetch_user_posts_page(user_id: str, cursor: str = "") -> UserPostsPage:
    user_id = str(user_id).strip()
    if not user_id:
        raise PickerError("缺少作者 user_id")
    args = ["user-posts", user_id]
    if cursor:
        args.extend(["--cursor", cursor])
    payload = run_redbook_json(args, timeout=180)
    notes = normalize_notes(payload, source="author", source_key=user_id)
    has_more = bool(payload.get("has_more")) if isinstance(payload, dict) else False
    next_cursor = str(payload.get("cursor") or "") if isinstance(payload, dict) else ""
    return UserPostsPage(notes=tuple(notes), has_more=has_more, cursor=next_cursor)


def fetch_user_posts(
    user_id: str,
    *,
    all_posts: bool = False,
    delay_ms: int = 1500,
) -> list[Favorite]:
    user_id = str(user_id).strip()
    if not user_id:
        raise PickerError("缺少作者 user_id")
    if not all_posts:
        return list(fetch_user_posts_page(user_id).notes)
    args = ["user-posts", user_id, "--all", "--delay", str(max(300, delay_ms))]
    payload = run_redbook_json(args, timeout=600)
    return normalize_notes(payload, source="author", source_key=user_id)


def fetch_user_posts_until(
    user_id: str,
    target_ids: set[str],
    *,
    max_pages: int = 8,
    delay_ms: int = 1500,
) -> tuple[dict[str, Favorite], bool]:
    """Fetch recent author pages until every requested note is found or paging ends."""
    remaining = {str(note_id) for note_id in target_ids if str(note_id)}
    found: dict[str, Favorite] = {}
    cursor = ""
    complete = False
    for page_index in range(max(1, max_pages)):
        page = fetch_user_posts_page(user_id, cursor)
        for note in page.notes:
            if note.note_id in remaining:
                found[note.note_id] = note
                remaining.discard(note.note_id)
        if not remaining:
            complete = True
            break
        if not page.has_more or not page.cursor or not page.notes:
            complete = True
            break
        cursor = page.cursor
        if delay_ms > 0 and page_index + 1 < max_pages:
            time.sleep(delay_ms / 1000)
    return found, complete


def search_posts(keyword: str, page: int = 1) -> list[Favorite]:
    keyword = keyword.strip()
    if not keyword:
        raise PickerError("搜索关键词不能为空")
    page = max(1, int(page))
    payload = run_redbook_json(
        ["search", keyword, "--page", str(page)], timeout=180
    )
    return normalize_notes(
        payload, source="search", source_key=f"{keyword}::{page}"
    )


ALLOWED_SHARE_HOSTS = {"xiaohongshu.com", "www.xiaohongshu.com", "xhslink.com", "www.xhslink.com", "xhslink.cn", "www.xhslink.cn"}


def _allowed_share_host(hostname: str) -> bool:
    host = hostname.casefold().strip(".")
    return host in ALLOWED_SHARE_HOSTS or host.endswith(".xiaohongshu.com")


def extract_shared_url(text: str) -> str:
    match = re.search(r"https?://[^\s<>\"']+", str(text or ""))
    if not match:
        raise PickerError("没有识别到小红书分享链接")
    return match.group(0).rstrip(".,;，。；）)]}")


def resolve_shared_url(text: str, timeout: int = 15) -> str:
    url = extract_shared_url(text)
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not _allowed_share_host(parsed.hostname or ""):
        raise PickerError("只允许解析小红书官方域名或 xhslink 分享短链")
    if (parsed.hostname or "").casefold().endswith("xiaohongshu.com"):
        return url
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.8"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            final_url = response.geturl()
            response.read(1024)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
        raise PickerError(f"分享短链解析失败：{exc}") from exc
    final = urlparse(final_url)
    if final.scheme not in {"http", "https"} or not _allowed_share_host(final.hostname or ""):
        raise PickerError("分享短链跳转到了非小红书域名，已拒绝继续访问")
    return final_url


def favorite_from_shared_link(text: str) -> Favorite:
    resolved = resolve_shared_url(text)
    detail = read_note(resolved)
    note_id = _extract_note_id(detail) or _note_id_from_url(resolved)
    if not note_id:
        raise PickerError("已打开分享链接，但没有识别到帖子 ID")
    return Favorite(
        note_id=note_id,
        title=_extract_title(detail) or "（无标题）",
        author=_extract_author(detail) or "未知作者",
        web_url=resolved,
        cover_url=_extract_cover(detail),
        author_id=_extract_author_id(detail),
        source="direct",
        source_key="share-link",
    )


def read_note(web_url: str) -> dict[str, Any]:
    payload = run_redbook_json(["read", web_url], timeout=120)
    if not isinstance(payload, dict):
        raise PickerError("帖子详情 JSON 结构异常")
    return payload


def _candidate_url(image: dict[str, Any]) -> str:
    for key in (
        "url_default",
        "urlDefault",
        "url_pre",
        "urlPre",
        "url",
        "original_url",
        "originalUrl",
    ):
        value = image.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value

    for key in ("info_list", "infoList"):
        infos = image.get(key)
        if isinstance(infos, list):
            preferred: list[str] = []
            fallback: list[str] = []
            for info in infos:
                if not isinstance(info, dict):
                    continue
                url = info.get("url")
                if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                    continue
                scene = str(info.get("image_scene") or info.get("imageScene") or "")
                if "DFT" in scene.upper() or "LARGE" in scene.upper():
                    preferred.append(url)
                else:
                    fallback.append(url)
            if preferred:
                return preferred[0]
            if fallback:
                return fallback[0]
    return ""


def extract_image_urls(note: dict[str, Any]) -> list[str]:
    images: Any = None
    for key in ("image_list", "imageList", "images"):
        value = note.get(key)
        if isinstance(value, list):
            images = value
            break

    if not isinstance(images, list):
        card = note.get("note_card") or note.get("noteCard")
        if isinstance(card, dict):
            return extract_image_urls(card)
        return []

    urls: list[str] = []
    seen: set[str] = set()
    for item in images:
        if isinstance(item, str):
            url = item if item.startswith(("http://", "https://")) else ""
        elif isinstance(item, dict):
            url = _candidate_url(item)
        else:
            continue
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def stable_image_url(raw_url: str) -> str:
    """Normalize the image resource key using the same strategy as xhs-downloader.

    This only transforms an image URL already exposed by the page/API response; it does
    not decrypt protected media or create access to content the logged-in account cannot view.
    """
    decoded = raw_url.replace("\\u002F", "/").replace("\\/", "/").replace("\\u0026", "&")
    parsed = urlsplit(decoded)
    hostname = (parsed.hostname or "").lower()
    if not (
        hostname == "xhscdn.com"
        or hostname.endswith(".xhscdn.com")
        or hostname == "xiaohongshu.com"
        or hostname.endswith(".xiaohongshu.com")
    ):
        return decoded
    path = parsed.path.lstrip("/")
    if not path:
        return decoded
    stable_path = EPHEMERAL_IMAGE_ROUTE_PREFIX.sub("", path, count=1)
    token = stable_path.partition("!")[0]
    if not token:
        return decoded
    return f"https://sns-img-bd.xhscdn.com/{token}"


def sanitize_component(value: str, max_length: int = 70) -> str:
    value = INVALID_FILENAME.sub("_", value).strip(" ._")
    value = re.sub(r"\s+", " ", value)
    return (value[:max_length].rstrip(" ._") or "untitled")


def parse_selection(text: str, count: int) -> list[int]:
    text = text.strip()
    if not text:
        raise PickerError("没有输入选择")
    if text.lower() == "all":
        return list(range(count))

    selected: set[int] = set()
    for part in re.split(r"[,，\s]+", text):
        if not part:
            continue
        if "-" in part:
            bits = part.split("-", 1)
            if len(bits) != 2 or not all(bit.strip().isdigit() for bit in bits):
                raise PickerError(f"无法识别的范围：{part}")
            start, end = (int(bit.strip()) for bit in bits)
            if start > end:
                start, end = end, start
            if start < 1 or end > count:
                raise PickerError(f"范围超出当前列表：{part}")
            selected.update(range(start - 1, end))
        elif part.isdigit():
            index = int(part)
            if index < 1 or index > count:
                raise PickerError(f"序号超出当前列表：{part}")
            selected.add(index - 1)
        else:
            raise PickerError(f"无法识别的输入：{part}")
    return sorted(selected)


def _truncate(value: str, width: int) -> str:
    value = value.replace("\r", " ").replace("\n", " ").strip()
    if len(value) <= width:
        return value
    return value[: width - 1] + "…"


def choose_notes(notes: list[Favorite], initial_show: int = 80) -> list[Favorite]:
    if not notes:
        raise PickerError("收藏列表为空，或 redbook 没有返回可识别的 webUrl")

    visible = notes
    show_limit = max(10, initial_show)
    query = ""

    while True:
        print()
        header = f"共读取 {len(notes)} 条收藏"
        if query:
            header += f"；搜索“{query}”得到 {len(visible)} 条"
        print(header)
        if not visible:
            print("没有匹配项。输入 /新的关键词，或输入 /clear 清除搜索。")
        else:
            shown = visible[:show_limit]
            for index, note in enumerate(shown, start=1):
                print(
                    f"{index:>4}. {_truncate(note.title, 54):<55} "
                    f"@{_truncate(note.author, 22)}"
                )
            if len(visible) > len(shown):
                print(f"... 还有 {len(visible) - len(shown)} 条未显示，输入 more 查看更多")

        print()
        raw = input(
            "输入序号(如 1,3,5-10)；/关键词 搜索；more 更多；/clear 清除；q 退出\n> "
        ).strip()
        if raw.lower() in {"q", "quit", "exit"}:
            return []
        if raw.lower() == "more":
            show_limit = min(len(visible), show_limit + 100)
            continue
        if raw.lower() == "/clear":
            query = ""
            visible = notes
            show_limit = max(10, initial_show)
            continue
        if raw.startswith("/"):
            query = raw[1:].strip()
            if not query:
                continue
            needle = query.casefold()
            visible = [
                note
                for note in notes
                if needle in note.title.casefold() or needle in note.author.casefold()
            ]
            show_limit = max(10, initial_show)
            continue
        if not visible:
            continue
        try:
            indexes = parse_selection(raw, min(len(visible), show_limit))
        except PickerError as exc:
            print(f"输入错误：{exc}")
            continue
        return [visible[index] for index in indexes]


def _sniff_extension(data: bytes, content_type: str, url: str) -> str:
    content_type = content_type.split(";", 1)[0].strip().lower()
    by_type = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "image/avif": ".avif",
        "image/heic": ".heic",
        "image/heif": ".heif",
    }
    if content_type in by_type:
        return by_type[content_type]
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if len(data) >= 12 and data[0:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in {
        b"avif",
        b"avis",
        b"heic",
        b"heix",
        b"hevc",
        b"hevx",
        b"mif1",
        b"msf1",
    }:
        brand = data[8:12]
        return ".avif" if brand in {b"avif", b"avis"} else ".heic"
    suffix = Path(urlsplit(url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".heic", ".heif"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    return ".jpg"


def _request_image(url: str, timeout: int = 45) -> tuple[bytes, str]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Referer": REFERER,
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read()
            content_type = response.headers.get("Content-Type", "")
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
        raise PickerError(str(exc)) from exc
    if not data:
        raise PickerError("服务器返回空文件")
    lowered = content_type.lower()
    if "text/html" in lowered or data[:32].lstrip().startswith(b"<"):
        raise PickerError("返回内容不是图片，可能是访问凭据已失效")
    return data, content_type


def download_image(raw_url: str, folder: Path, index: int) -> tuple[str, bool]:
    patterns = [f"{index:02d}.*", f"{index}.*"]
    existing: list[Path] = []
    for pattern in patterns:
        existing.extend(
            path for path in folder.glob(pattern)
            if path.is_file() and path.suffix != ".part" and path.stat().st_size > 0
        )
    existing = sorted(set(existing), key=lambda path: path.name.casefold())
    if existing:
        return existing[0].name, True

    stable = stable_image_url(raw_url)
    candidates = [stable]
    if raw_url not in candidates:
        candidates.append(raw_url)

    errors: list[str] = []
    for url in candidates:
        try:
            data, content_type = _request_image(url)
            extension = _sniff_extension(data, content_type, url)
            target = folder / f"{index:02d}{extension}"
            fd, temp_name = tempfile.mkstemp(prefix=f".{index:02d}-", suffix=".part", dir=folder)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_name, target)
            except Exception:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass
                raise
            return target.name, False
        except (PickerError, OSError) as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
    raise PickerError("；".join(errors[-2:]))


def _post_folder(root: Path, note: Favorite) -> Path:
    # v0.4 compatibility: never create a second folder merely because a note's
    # title changed. Reuse any existing folder whose prefix or metadata matches
    # this note_id.
    try:
        import xhs_library
        existing = xhs_library.find_existing_post_folder(root, note.note_id)
    except Exception:
        existing = None
    if existing is not None:
        return existing
    title = sanitize_component(note.title)
    return root / f"{note.note_id}__{title}"


def download_post(note: Favorite, root: Path, ordinal: int, total: int) -> PostResult:
    label = _truncate(note.title, 34)
    safe_print(f"[{ordinal}/{total}] 解析：{label}")
    try:
        detail = read_note(note.web_url)
        image_urls = extract_image_urls(detail)
        if not image_urls:
            return PostResult(
                note_id=note.note_id,
                title=note.title,
                status="no_images",
                error="帖子详情中没有可下载图片（可能是纯视频或详情解析失败）",
            )

        folder = _post_folder(root, note)
        folder.mkdir(parents=True, exist_ok=True)
        downloaded = 0
        skipped = 0
        files: list[str] = []
        image_errors: list[str] = []

        for image_index, image_url in enumerate(image_urls, start=1):
            try:
                filename, was_skipped = download_image(image_url, folder, image_index)
                files.append(filename)
                skipped += int(was_skipped)
                downloaded += int(not was_skipped)
            except PickerError as exc:
                image_errors.append(f"图 {image_index}: {exc}")

        meta_path = folder / "meta.json"
        legacy_meta: dict[str, Any] = {}
        if meta_path.exists():
            try:
                loaded = json.loads(meta_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    legacy_meta = loaded
            except (OSError, json.JSONDecodeError):
                legacy_meta = {}
        metadata = dict(legacy_meta)
        metadata.update({
            "note_id": note.note_id,
            "title": note.title,
            "author": note.author,
            "author_id": note.author_id or str(legacy_meta.get("author_id") or legacy_meta.get("authorId") or ""),
            "canonical_url": f"https://www.xiaohongshu.com/explore/{note.note_id}",
            "image_count": len(image_urls),
            "files": files,
            "errors": image_errors,
            # Preserve the original timestamp when upgrading an existing v0.4
            # folder; record the current touch separately.
            "saved_at": str(legacy_meta.get("saved_at") or legacy_meta.get("savedAt") or time.strftime("%Y-%m-%dT%H:%M:%S%z")),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "library_schema": 2,
        })
        meta_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        if not files:
            return PostResult(
                note_id=note.note_id,
                title=note.title,
                status="failed",
                image_count=len(image_urls),
                error="；".join(image_errors) or "所有图片下载失败",
                folder=str(folder),
            )
        status = "partial" if image_errors else "ok"
        return PostResult(
            note_id=note.note_id,
            title=note.title,
            status=status,
            image_count=len(image_urls),
            downloaded=downloaded,
            skipped=skipped,
            error="；".join(image_errors),
            folder=str(folder),
        )
    except (PickerError, OSError, json.JSONDecodeError) as exc:
        return PostResult(
            note_id=note.note_id,
            title=note.title,
            status="failed",
            error=str(exc),
        )


def refresh_selected(selected: list[Favorite]) -> tuple[list[Favorite], list[PostResult]]:
    fresh = fetch_favorites()
    by_id = {note.note_id: note for note in fresh}
    refreshed: list[Favorite] = []
    missing: list[PostResult] = []
    for note in selected:
        current = by_id.get(note.note_id)
        if current is None:
            missing.append(
                PostResult(
                    note_id=note.note_id,
                    title=note.title,
                    status="failed",
                    error="第二次刷新收藏后未找到该帖子，未使用旧 token",
                )
            )
        else:
            refreshed.append(current)
    return refreshed, missing


def save_summary(root: Path, results: list[PostResult]) -> Path:
    payload = {
        "version": APP_VERSION,
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "results": [
            {
                "note_id": item.note_id,
                "title": item.title,
                "status": item.status,
                "image_count": item.image_count,
                "downloaded": item.downloaded,
                "skipped": item.skipped,
                "error": item.error,
                "folder": item.folder,
            }
            for item in results
        ],
    }
    path = root / "download_summary.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xhs_pick.py",
        description="从当前登录账号的小红书收藏中选择少量帖子并下载图文图片。",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path.cwd() / "downloads",
        help="输出目录，默认 ./downloads",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=2,
        help="同时处理的帖子数，默认 2；遇到风控可改为 1",
    )
    parser.add_argument(
        "--show",
        type=int,
        default=80,
        help="首次显示多少条收藏，默认 80",
    )
    parser.add_argument(
        "--selection",
        help="非交互选择，例如 1,3,5-10；主要用于快速测试",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {APP_VERSION}",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    workers = max(1, min(int(args.workers), 4))

    try:
        redbook_command()
    except PickerError as exc:
        eprint(f"错误：{exc}")
        return 2

    print("读取收藏列表……")
    try:
        notes = fetch_favorites()
    except PickerError as exc:
        eprint(f"读取收藏失败：{exc}")
        eprint("请先双击 login.bat 配置登录，或确认 redbook whoami 能正常返回账号。")
        return 3

    if args.selection:
        shown = notes[: max(1, args.show)]
        try:
            indexes = parse_selection(args.selection, len(shown))
        except PickerError as exc:
            eprint(f"选择错误：{exc}")
            return 4
        selected = [shown[index] for index in indexes]
    else:
        try:
            selected = choose_notes(notes, args.show)
        except PickerError as exc:
            eprint(f"选择失败：{exc}")
            return 4

    if not selected:
        print("未选择帖子，结束。")
        return 0

    print()
    print(f"已选择 {len(selected)} 条。为避免 xsec_token 过期，正在重新刷新收藏 URL……")
    try:
        selected, missing = refresh_selected(selected)
    except PickerError as exc:
        eprint(f"刷新收藏失败：{exc}")
        return 5

    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)

    results: list[PostResult] = list(missing)
    if selected:
        print(f"开始下载到：{output}")
        print(f"并发帖子数：{workers}")
        print()
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            future_map = {
                pool.submit(download_post, note, output, index, len(selected)): note
                for index, note in enumerate(selected, start=1)
            }
            for future in concurrent.futures.as_completed(future_map):
                note = future_map[future]
                try:
                    result = future.result()
                except Exception as exc:  # last-resort isolation per post
                    result = PostResult(
                        note_id=note.note_id,
                        title=note.title,
                        status="failed",
                        error=f"未处理异常：{exc}",
                    )
                results.append(result)
                marker = {
                    "ok": "✓",
                    "partial": "△",
                    "no_images": "-",
                    "failed": "✗",
                }.get(result.status, "?")
                detail = f"{result.downloaded} 新下载 / {result.skipped} 已存在"
                if result.status in {"failed", "no_images"}:
                    detail = result.error
                elif result.status == "partial":
                    detail += f"；部分失败：{result.error}"
                safe_print(f"{marker} {_truncate(result.title, 45)} — {detail}")

    summary_path = save_summary(output, results)
    ok = sum(item.status == "ok" for item in results)
    partial = sum(item.status == "partial" for item in results)
    no_images = sum(item.status == "no_images" for item in results)
    failed = sum(item.status == "failed" for item in results)
    images = sum(item.downloaded for item in results)
    skipped_images = sum(item.skipped for item in results)

    print()
    print("完成")
    print(f"  成功帖子：{ok}")
    print(f"  部分成功：{partial}")
    print(f"  无图片：  {no_images}")
    print(f"  失败：    {failed}")
    print(f"  新下载图片：{images}")
    print(f"  已存在图片：{skipped_images}")
    print(f"  汇总：{summary_path}")

    if failed:
        print("\n失败项目：")
        for item in results:
            if item.status == "failed":
                print(f"- {_truncate(item.title, 50)} [{item.note_id}]：{item.error}")
    return 0 if failed == 0 else 6


if __name__ == "__main__":
    raise SystemExit(main())
