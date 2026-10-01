from __future__ import annotations

import concurrent.futures
import functools
import json
import os
import secrets
import threading
import time
import uuid
import webbrowser
from dataclasses import asdict, replace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import xhs_library
import xhs_pick
import xhs_subscriptions

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
OUTPUT_DIR = xhs_pick.ROOT_DIR / "downloads"
WEB_DIR = xhs_pick.ROOT_DIR / "web"
SUBSCRIPTION_FILE = xhs_pick.ROOT_DIR / ".subscriptions.json"
SESSION_TOKEN = secrets.token_urlsafe(32)


class WebState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.notes: dict[str, xhs_pick.Favorite] = {}
        self.jobs: dict[str, dict[str, Any]] = {}
        self.library_posts: list[xhs_library.LibraryPost] = []
        self.subscription_check: dict[str, Any] = {
            "status": "idle", "done": 0, "total": 0, "results": [], "error": ""
        }
        self.favorites_live_at = 0.0
        self.live_favorites: dict[str, xhs_pick.Favorite] = {}
        self.favorites_cache_fetched_at = ""

    def register(self, notes: list[xhs_pick.Favorite]) -> None:
        with self.lock:
            for note in notes:
                self.notes[note.note_id] = note

    def register_favorites(self, notes: list[xhs_pick.Favorite], *, live: bool, fetched_at: str = "") -> None:
        self.register(notes)
        with self.lock:
            if live:
                self.favorites_live_at = time.time()
                self.live_favorites = {note.note_id: note for note in notes}
            if fetched_at:
                self.favorites_cache_fetched_at = fetched_at

    def favorites_are_live(self, max_age_seconds: int = 10 * 60) -> bool:
        with self.lock:
            return bool(self.favorites_live_at and time.time() - self.favorites_live_at <= max_age_seconds)

    def live_favorites_snapshot(self) -> list[xhs_pick.Favorite]:
        with self.lock:
            return list(self.live_favorites.values())

    def get_notes(self, note_ids: list[str]) -> list[xhs_pick.Favorite]:
        with self.lock:
            return [self.notes[note_id] for note_id in note_ids if note_id in self.notes]

    def register_subscription_pending(self, notes: list[xhs_pick.Favorite]) -> None:
        with self.lock:
            for placeholder in notes:
                current = self.notes.get(placeholder.note_id)
                if current and current.web_url:
                    self.notes[placeholder.note_id] = replace(
                        current,
                        source="subscription",
                        source_key=placeholder.author_id or placeholder.source_key,
                        author_id=placeholder.author_id or current.author_id,
                    )
                else:
                    self.notes[placeholder.note_id] = placeholder

    def refresh_library(self) -> list[xhs_library.LibraryPost]:
        posts = xhs_library.scan_library(OUTPUT_DIR)
        with self.lock:
            self.library_posts = posts
        return posts

    def library_snapshot(self) -> list[xhs_library.LibraryPost]:
        with self.lock:
            return list(self.library_posts)


STATE = WebState()


def note_json(note: xhs_pick.Favorite) -> dict[str, Any]:
    return {
        "note_id": note.note_id,
        "title": note.title,
        "author": note.author,
        "author_id": note.author_id,
        "source": note.source,
        "source_key": note.source_key,
        "has_cover": bool(note.cover_url),
        "cover_url": f"/api/cover?id={quote(note.note_id)}" if note.cover_url else "",
        "canonical_url": f"https://www.xiaohongshu.com/explore/{note.note_id}",
    }


def result_json(result: xhs_pick.PostResult) -> dict[str, Any]:
    return asdict(result)


def library_post_json(post: xhs_library.LibraryPost) -> dict[str, Any]:
    images = [
        {
            "key": image.key,
            "filename": image.filename,
            "url": (
                "/api/library-image?note_id="
                + quote(post.note_id, safe="")
                + "&file="
                + quote(image.filename, safe="")
            ),
        }
        for image in post.images
    ]
    return {
        "note_id": post.note_id,
        "title": post.title,
        "author": post.author,
        "canonical_url": post.canonical_url,
        "saved_at": post.saved_at,
        "image_count": len(post.images),
        "cover_url": images[0]["url"] if images else "",
        "images": images,
    }


@functools.lru_cache(maxsize=160)
def fetch_cover_bytes(url: str) -> tuple[bytes, str]:
    try:
        return xhs_pick._request_image(url, timeout=30)
    except xhs_pick.PickerError:
        stable = xhs_pick.stable_image_url(url)
        if stable == url:
            raise
        return xhs_pick._request_image(stable, timeout=30)


def refresh_notes_for_download(
    notes: list[xhs_pick.Favorite],
) -> tuple[list[xhs_pick.Favorite], list[xhs_pick.PostResult]]:
    ready: list[xhs_pick.Favorite] = []
    missing: list[xhs_pick.PostResult] = []

    favorites = [note for note in notes if note.source == "favorites"]
    others = [note for note in notes if note.source != "favorites"]
    if favorites:
        if STATE.favorites_are_live():
            fresh = STATE.live_favorites_snapshot()
        else:
            fresh = xhs_pick.fetch_favorites()
            STATE.register_favorites(fresh, live=True)
        by_id = {note.note_id: note for note in fresh}
        for note in favorites:
            current = by_id.get(note.note_id)
            if current is None:
                missing.append(
                    xhs_pick.PostResult(
                        note_id=note.note_id,
                        title=note.title,
                        status="failed",
                        error="刷新收藏后未找到该帖子，未使用旧 token",
                    )
                )
            else:
                ready.append(current)

    author_groups: dict[str, list[xhs_pick.Favorite]] = {}
    direct: list[xhs_pick.Favorite] = []
    for note in others:
        if note.source in {"author", "subscription"}:
            user_id = note.author_id or note.source_key
            if user_id:
                author_groups.setdefault(user_id, []).append(note)
            else:
                direct.append(note)
        else:
            direct.append(note)

    for user_id, group in author_groups.items():
        target_ids = {note.note_id for note in group}
        found, _ = xhs_pick.fetch_user_posts_until(user_id, target_ids)
        STATE.register(list(found.values()))
        for note in group:
            current = found.get(note.note_id)
            if current is None:
                missing.append(
                    xhs_pick.PostResult(
                        note_id=note.note_id,
                        title=note.title,
                        status="failed",
                        error="刷新作者作品后未找到该帖子，可能已删除或超出扫描范围",
                    )
                )
            else:
                ready.append(current)

    for note in direct:
        if note.web_url:
            ready.append(note)
        else:
            missing.append(
                xhs_pick.PostResult(
                    note_id=note.note_id,
                    title=note.title,
                    status="failed",
                    error="帖子缺少可用访问 URL，请重新加载来源后再试",
                )
            )
    return ready, missing


def subscription_payload() -> dict[str, Any]:
    info = xhs_subscriptions.summary(SUBSCRIPTION_FILE)
    pending = xhs_subscriptions.pending_favorites(SUBSCRIPTION_FILE)
    STATE.register_subscription_pending(pending)
    with STATE.lock:
        current = {note.note_id: STATE.notes.get(note.note_id, note) for note in pending}
    info["updates"] = [note_json(current[note.note_id]) for note in pending]
    return info


def run_subscription_check(check_id: str) -> None:
    info = xhs_subscriptions.summary(SUBSCRIPTION_FILE)
    total = info["author_count"]
    with STATE.lock:
        STATE.subscription_check = {
            "check_id": check_id,
            "status": "running",
            "done": 0,
            "total": total,
            "results": [],
            "error": "",
            "started_at": time.time(),
            "finished_at": None,
        }

    def on_author(result: xhs_subscriptions.CheckAuthorResult) -> None:
        with STATE.lock:
            STATE.subscription_check["done"] += 1
            STATE.subscription_check["results"].append(asdict(result))

    try:
        results, fresh_notes = xhs_subscriptions.check_all(
            SUBSCRIPTION_FILE, on_author=on_author
        )
        STATE.register(fresh_notes)
        STATE.register_subscription_pending(xhs_subscriptions.pending_favorites(SUBSCRIPTION_FILE))
        with STATE.lock:
            STATE.subscription_check["status"] = "completed"
            STATE.subscription_check["finished_at"] = time.time()
            STATE.subscription_check["new_count"] = sum(result.new_count for result in results)
    except Exception as exc:
        with STATE.lock:
            STATE.subscription_check["status"] = "failed"
            STATE.subscription_check["error"] = str(exc)
            STATE.subscription_check["finished_at"] = time.time()


def start_subscription_check() -> str:
    with STATE.lock:
        if STATE.subscription_check.get("status") == "running":
            return str(STATE.subscription_check.get("check_id") or "running")
        check_id = uuid.uuid4().hex[:12]
        total = xhs_subscriptions.summary(SUBSCRIPTION_FILE)["author_count"]
        STATE.subscription_check = {
            "check_id": check_id,
            "status": "running",
            "done": 0,
            "total": total,
            "results": [],
            "error": "",
            "started_at": time.time(),
            "finished_at": None,
        }
    threading.Thread(target=run_subscription_check, args=(check_id,), daemon=True).start()
    return check_id


def run_download_job(job_id: str, notes: list[xhs_pick.Favorite], workers: int) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        notes, missing = refresh_notes_for_download(notes)
    except Exception as exc:
        with STATE.lock:
            job = STATE.jobs[job_id]
            job["status"] = "failed"
            job["error"] = f"刷新帖子访问地址失败：{exc}"
            job["finished_at"] = time.time()
        return

    with STATE.lock:
        job = STATE.jobs[job_id]
        job["results"].extend(result_json(item) for item in missing)
        job["done"] += len(missing)
        job["total"] = len(notes) + len(missing)
        job["status"] = "running"

    results: list[xhs_pick.PostResult] = list(missing)
    if notes:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(xhs_pick.download_post, note, OUTPUT_DIR, index, len(notes)): note
                for index, note in enumerate(notes, start=1)
            }
            for future in concurrent.futures.as_completed(futures):
                note = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = xhs_pick.PostResult(
                        note_id=note.note_id,
                        title=note.title,
                        status="failed",
                        error=f"未处理异常：{exc}",
                    )
                results.append(result)
                with STATE.lock:
                    job = STATE.jobs[job_id]
                    job["results"].append(result_json(result))
                    job["done"] += 1

    xhs_pick.save_summary(OUTPUT_DIR, results)
    STATE.refresh_library()
    with STATE.lock:
        ack_updates = bool(STATE.jobs[job_id].get("ack_subscription_updates"))
    if ack_updates:
        handled_ids = [
            result.note_id for result in results if result.status != "failed"
        ]
        if handled_ids:
            xhs_subscriptions.mark_handled(SUBSCRIPTION_FILE, handled_ids)
    with STATE.lock:
        job = STATE.jobs[job_id]
        job["status"] = "completed"
        job["finished_at"] = time.time()
        job["output"] = str(OUTPUT_DIR.resolve())


def start_download(
    note_ids: list[str],
    workers: int = 2,
    *,
    ack_subscription_updates: bool = False,
) -> str:
    seen: set[str] = set()
    clean_ids: list[str] = []
    for note_id in note_ids:
        note_id = str(note_id).strip()
        if note_id and note_id not in seen:
            seen.add(note_id)
            clean_ids.append(note_id)
    notes = STATE.get_notes(clean_ids)
    if not notes:
        raise xhs_pick.PickerError("没有可下载的已加载帖子")
    workers = max(1, min(int(workers), 4))
    job_id = uuid.uuid4().hex[:12]
    with STATE.lock:
        STATE.jobs[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "total": len(notes),
            "done": 0,
            "results": [],
            "error": "",
            "output": str(OUTPUT_DIR.resolve()),
            "started_at": time.time(),
            "finished_at": None,
            "ack_subscription_updates": bool(ack_subscription_updates),
        }
    threading.Thread(target=run_download_job, args=(job_id, notes, workers), daemon=True).start()
    return job_id


class Handler(BaseHTTPRequestHandler):
    server_version = "XHSPicker/0.6"

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _send_security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
        )

    def _check_local_host(self) -> None:
        client_ip = str(self.client_address[0] or "")
        if client_ip not in {"127.0.0.1", "::1"}:
            raise xhs_pick.PickerError("本地服务只允许回环地址访问")
        host = (self.headers.get("Host") or "").strip().casefold()
        host_name = host
        if host.startswith("[") and "]" in host:
            host_name = host.split("]", 1)[0] + "]"
        elif ":" in host:
            host_name = host.rsplit(":", 1)[0]
        if host_name not in {"127.0.0.1", "localhost", "[::1]"}:
            raise xhs_pick.PickerError("拒绝非 localhost Host，防止本地服务被跨站利用")

    def _check_api_access(self, *, allow_media_without_token: bool = False) -> None:
        if not allow_media_without_token:
            supplied = self.headers.get("X-XHS-Picker-Token", "")
            if not secrets.compare_digest(supplied, SESSION_TOKEN):
                raise xhs_pick.PickerError("本地会话令牌无效，请刷新页面后重试")
        origin = self.headers.get("Origin", "")
        if origin:
            allowed = {
                f"http://127.0.0.1:{self.server.server_address[1]}",
                f"http://localhost:{self.server.server_address[1]}",
            }
            if origin.rstrip("/") not in allowed:
                raise xhs_pick.PickerError("拒绝来自其他站点的本地 API 请求")

    def send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, message: str, status: int = 400) -> None:
        self.send_json({"error": message}, status)

    def send_static(self, path: Path, content_type: str, *, inject_session: bool = False) -> None:
        if not path.exists():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = path.read_bytes()
        if inject_session:
            text = body.decode("utf-8")
            marker = "</head>"
            meta = f'<meta name="xhs-picker-token" content="{SESSION_TOKEN}">\n'
            text = text.replace(marker, meta + marker, 1)
            body = text.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self, limit: int = 2_000_000) -> dict[str, Any]:
        content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise xhs_pick.PickerError("本地 API 仅接受 application/json")
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError as exc:
            raise xhs_pick.PickerError("无效的 Content-Length") from exc
        if length < 0 or length > limit:
            raise xhs_pick.PickerError("请求体过大")
        payload = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(payload, dict):
            raise xhs_pick.PickerError("请求 JSON 必须是对象")
        return payload

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            self._check_local_host()
            if parsed.path.startswith("/api/"):
                self._check_api_access(allow_media_without_token=parsed.path in {"/api/cover", "/api/library-image"})
            if parsed.path == "/":
                self.send_static(WEB_DIR / "index.html", "text/html; charset=utf-8", inject_session=True)
                return
            if parsed.path == "/style.css":
                self.send_static(WEB_DIR / "style.css", "text/css; charset=utf-8")
                return
            if parsed.path == "/app.js":
                self.send_static(WEB_DIR / "app.js", "text/javascript; charset=utf-8")
                return
            if parsed.path == "/api/favorites":
                force_refresh = (query.get("refresh") or ["0"])[0] in {"1", "true", "yes"}
                cached = False
                fetched_at = ""
                if not force_refresh:
                    notes, fetched_at = xhs_pick.load_favorites_cache()
                    cached = bool(notes)
                else:
                    notes = []
                if not notes:
                    notes = xhs_pick.fetch_favorites()
                    fetched_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
                    STATE.register_favorites(notes, live=True, fetched_at=fetched_at)
                else:
                    STATE.register_favorites(notes, live=False, fetched_at=fetched_at)
                self.send_json({
                    "items": [note_json(note) for note in notes],
                    "cached": cached,
                    "fetched_at": fetched_at,
                })
                return
            if parsed.path == "/api/author":
                user_id = (query.get("user_id") or [""])[0]
                cursor = (query.get("cursor") or [""])[0]
                page = xhs_pick.fetch_user_posts_page(user_id, cursor)
                notes = list(page.notes)
                STATE.register(notes)
                self.send_json({
                    "items": [note_json(note) for note in notes],
                    "has_more": page.has_more,
                    "cursor": page.cursor,
                })
                return
            if parsed.path == "/api/search":
                keyword = (query.get("q") or [""])[0]
                page = int((query.get("page") or ["1"])[0])
                notes = xhs_pick.search_posts(keyword, page)
                STATE.register(notes)
                self.send_json({"items": [note_json(note) for note in notes], "page": page})
                return
            if parsed.path == "/api/subscriptions":
                self.send_json(subscription_payload())
                return
            if parsed.path == "/api/subscription-check":
                with STATE.lock:
                    payload = dict(STATE.subscription_check)
                payload["subscriptions"] = subscription_payload()
                self.send_json(payload)
                return
            if parsed.path == "/api/cover":
                note_id = (query.get("id") or [""])[0]
                with STATE.lock:
                    note = STATE.notes.get(note_id)
                if not note or not note.cover_url:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                data, content_type = fetch_cover_bytes(note.cover_url)
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", content_type or "image/jpeg")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "private, max-age=600")
                self._send_security_headers()
                self.end_headers()
                self.wfile.write(data)
                return
            if parsed.path == "/api/job":
                job_id = (query.get("id") or [""])[0]
                with STATE.lock:
                    job = STATE.jobs.get(job_id)
                    payload = dict(job) if job else None
                if payload is None:
                    self.send_error_json("任务不存在", 404)
                else:
                    self.send_json(payload)
                return
            if parsed.path == "/api/library":
                posts = STATE.refresh_library()
                self.send_json(
                    {
                        "posts": [library_post_json(post) for post in posts],
                        "authors": xhs_library.authors_summary(posts),
                        "post_count": len(posts),
                        "image_count": sum(len(post.images) for post in posts),
                        "root": str(OUTPUT_DIR.resolve()),
                    }
                )
                return
            if parsed.path == "/api/library-image":
                note_id = (query.get("note_id") or [""])[0]
                filename = (query.get("file") or [""])[0]
                posts = STATE.library_snapshot() or STATE.refresh_library()
                try:
                    path = xhs_library.resolve_image(posts, note_id, filename)
                except xhs_library.LibraryError:
                    posts = STATE.refresh_library()
                    path = xhs_library.resolve_image(posts, note_id, filename)
                size = path.stat().st_size
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", xhs_library.image_content_type(path))
                self.send_header("Content-Length", str(size))
                self.send_header("Cache-Control", "private, max-age=120")
                self._send_security_headers()
                self.end_headers()
                with path.open("rb") as handle:
                    while True:
                        block = handle.read(256 * 1024)
                        if not block:
                            break
                        self.wfile.write(block)
                return
            self.send_error_json("Not found", 404)
        except (xhs_pick.PickerError, xhs_library.LibraryError, xhs_subscriptions.SubscriptionError) as exc:
            self.send_error_json(str(exc), 400)
        except Exception as exc:
            self.send_error_json(f"{type(exc).__name__}: {exc}", 500)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            self._check_local_host()
            self._check_api_access()
            if parsed.path == "/api/resolve-link":
                payload = self._read_json_body()
                note = xhs_pick.favorite_from_shared_link(str(payload.get("text") or ""))
                STATE.register([note])
                self.send_json({"item": note_json(note)})
                return
            if parsed.path == "/api/download":
                payload = self._read_json_body()
                note_ids = payload.get("note_ids") or []
                if not isinstance(note_ids, list) or len(note_ids) > 500:
                    raise xhs_pick.PickerError("选择列表无效或过大")
                job_id = start_download(
                    note_ids,
                    int(payload.get("workers", 2)),
                    ack_subscription_updates=bool(payload.get("ack_subscription_updates", False)),
                )
                self.send_json({"job_id": job_id}, 202)
                return
            if parsed.path == "/api/subscriptions/subscribe":
                payload = self._read_json_body()
                user_id = str(payload.get("user_id") or "").strip()
                name = str(payload.get("name") or "").strip()
                xhs_subscriptions.subscribe(SUBSCRIPTION_FILE, user_id, name)
                self.send_json(subscription_payload())
                return
            if parsed.path == "/api/subscriptions/unsubscribe":
                payload = self._read_json_body()
                user_id = str(payload.get("user_id") or "").strip()
                xhs_subscriptions.unsubscribe(SUBSCRIPTION_FILE, user_id)
                self.send_json(subscription_payload())
                return
            if parsed.path == "/api/subscriptions/check":
                check_id = start_subscription_check()
                self.send_json({"check_id": check_id}, 202)
                return
            if parsed.path == "/api/subscriptions/handled":
                payload = self._read_json_body()
                if payload.get("all"):
                    count = xhs_subscriptions.mark_all_handled(SUBSCRIPTION_FILE)
                elif payload.get("user_id"):
                    count = xhs_subscriptions.mark_author_handled(
                        SUBSCRIPTION_FILE, str(payload.get("user_id"))
                    )
                else:
                    note_ids = payload.get("note_ids") or []
                    if not isinstance(note_ids, list):
                        raise xhs_pick.PickerError("note_ids 必须是列表")
                    count = xhs_subscriptions.mark_handled(
                        SUBSCRIPTION_FILE, [str(x) for x in note_ids]
                    )
                self.send_json({"handled": count, **subscription_payload()})
                return
            if parsed.path == "/api/open-output":
                OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
                if os.name == "nt":
                    os.startfile(str(OUTPUT_DIR.resolve()))  # type: ignore[attr-defined]
                self.send_json({"ok": True, "path": str(OUTPUT_DIR.resolve())})
                return
            if parsed.path == "/api/pick-folder":
                path = xhs_library.choose_folder_windows()
                self.send_json({"path": path, "cancelled": not bool(path)})
                return
            if parsed.path == "/api/library-transfer":
                payload = self._read_json_body()
                image_keys = payload.get("image_keys") or []
                if not isinstance(image_keys, list) or len(image_keys) > 10_000:
                    raise xhs_library.LibraryError("图片选择列表无效或过大")
                destination = str(payload.get("destination") or "").strip()
                if not destination:
                    raise xhs_library.LibraryError("请选择目标目录")
                result = xhs_library.transfer_images(
                    OUTPUT_DIR,
                    [str(key) for key in image_keys],
                    Path(destination),
                    action=str(payload.get("action") or "copy"),
                    preserve_post_folders=bool(payload.get("preserve_post_folders", True)),
                )
                STATE.refresh_library()
                self.send_json(result)
                return
            self.send_error_json("Not found", 404)
        except (xhs_pick.PickerError, xhs_library.LibraryError, xhs_subscriptions.SubscriptionError) as exc:
            self.send_error_json(str(exc), 400)
        except Exception as exc:
            self.send_error_json(f"{type(exc).__name__}: {exc}", 500)


def find_server() -> ThreadingHTTPServer:
    for port in range(DEFAULT_PORT, DEFAULT_PORT + 20):
        try:
            return ThreadingHTTPServer((HOST, port), Handler)
        except OSError:
            continue
    raise RuntimeError("无法找到可用的本地端口")


def main() -> int:
    try:
        xhs_pick.redbook_command()
    except xhs_pick.PickerError as exc:
        print(f"错误：{exc}")
        print("请先运行 setup.bat。")
        return 2
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    STATE.refresh_library()
    server = find_server()
    port = server.server_address[1]
    url = f"http://{HOST}:{port}/"
    print(f"XHS 图片选择器 v{xhs_pick.APP_VERSION}")
    print(f"本地页面：{url}")
    print("关闭此窗口即可停止本地服务。")
    if (
        xhs_subscriptions.summary(SUBSCRIPTION_FILE)["author_count"]
        and xhs_subscriptions.should_auto_check(SUBSCRIPTION_FILE)
    ):
        start_subscription_check()
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
