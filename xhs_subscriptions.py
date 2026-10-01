from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable

import xhs_pick

_STORE_LOCK = threading.RLock()
STORE_VERSION = 2
MAX_SEEN_IDS = 5000


class SubscriptionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CheckAuthorResult:
    user_id: str
    name: str
    new_count: int
    pending_count: int
    pages: int
    boundary_found: bool
    complete: bool
    error: str = ""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _empty_store() -> dict[str, Any]:
    return {"version": STORE_VERSION, "authors": {}}


def _load_unlocked(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _empty_store()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SubscriptionError(f"订阅文件损坏：{exc}") from exc
    if not isinstance(data, dict):
        raise SubscriptionError("订阅文件格式异常")
    authors = data.get("authors")
    if not isinstance(authors, dict):
        authors = {}
    return {"version": STORE_VERSION, "authors": authors}


def load_store(path: Path) -> dict[str, Any]:
    with _STORE_LOCK:
        return _load_unlocked(path)


def _save_unlocked(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def save_store(path: Path, data: dict[str, Any]) -> None:
    with _STORE_LOCK:
        _save_unlocked(path, data)


def _note_meta(note: xhs_pick.Favorite, first_seen_at: str | None = None) -> dict[str, Any]:
    return {
        "note_id": note.note_id,
        "title": note.title,
        "author": note.author,
        "author_id": note.author_id or note.source_key,
        "cover_url": note.cover_url,
        "first_seen_at": first_seen_at or _now(),
    }


def subscribe(path: Path, user_id: str, name: str = "") -> dict[str, Any]:
    user_id = str(user_id).strip()
    if not user_id:
        raise SubscriptionError("缺少作者 user_id")
    try:
        page = xhs_pick.fetch_user_posts_page(user_id)
    except xhs_pick.PickerError as exc:
        raise SubscriptionError(str(exc)) from exc
    baseline = list(page.notes)
    resolved_name = name.strip() or (baseline[0].author if baseline else user_id)
    now = _now()
    with _STORE_LOCK:
        data = _load_unlocked(path)
        authors = data["authors"]
        existing = authors.get(user_id)
        if isinstance(existing, dict):
            existing["name"] = resolved_name
            existing["last_checked_at"] = now
            existing["last_checked_epoch"] = time.time()
            _save_unlocked(path, data)
            return existing
        record = {
            "user_id": user_id,
            "name": resolved_name,
            "subscribed_at": now,
            "last_checked_at": now,
            "last_checked_epoch": time.time(),
            "last_error": "",
            "seen_ids": [note.note_id for note in baseline][:MAX_SEEN_IDS],
            "pending": {},
        }
        authors[user_id] = record
        _save_unlocked(path, data)
        return record


def unsubscribe(path: Path, user_id: str) -> bool:
    with _STORE_LOCK:
        data = _load_unlocked(path)
        existed = data["authors"].pop(str(user_id), None) is not None
        if existed:
            _save_unlocked(path, data)
        return existed


def mark_handled(path: Path, note_ids: list[str]) -> int:
    targets = {str(note_id).strip() for note_id in note_ids if str(note_id).strip()}
    if not targets:
        return 0
    removed = 0
    with _STORE_LOCK:
        data = _load_unlocked(path)
        for record in data["authors"].values():
            if not isinstance(record, dict):
                continue
            pending = record.get("pending")
            if not isinstance(pending, dict):
                continue
            for note_id in list(targets):
                if note_id in pending:
                    pending.pop(note_id, None)
                    removed += 1
        if removed:
            _save_unlocked(path, data)
    return removed


def mark_author_handled(path: Path, user_id: str) -> int:
    with _STORE_LOCK:
        data = _load_unlocked(path)
        record = data["authors"].get(str(user_id))
        if not isinstance(record, dict):
            return 0
        pending = record.get("pending")
        count = len(pending) if isinstance(pending, dict) else 0
        record["pending"] = {}
        if count:
            _save_unlocked(path, data)
        return count


def mark_all_handled(path: Path) -> int:
    with _STORE_LOCK:
        data = _load_unlocked(path)
        count = 0
        for record in data["authors"].values():
            if not isinstance(record, dict):
                continue
            pending = record.get("pending")
            if isinstance(pending, dict):
                count += len(pending)
            record["pending"] = {}
        if count:
            _save_unlocked(path, data)
        return count


def _record_snapshot(path: Path, user_id: str) -> dict[str, Any] | None:
    with _STORE_LOCK:
        data = _load_unlocked(path)
        record = data["authors"].get(user_id)
        return json.loads(json.dumps(record, ensure_ascii=False)) if isinstance(record, dict) else None


def check_author(
    path: Path,
    user_id: str,
    *,
    max_pages: int = 8,
    delay_ms: int = 1500,
) -> tuple[CheckAuthorResult, list[xhs_pick.Favorite]]:
    record = _record_snapshot(path, user_id)
    if record is None:
        raise SubscriptionError("订阅不存在")

    name = str(record.get("name") or user_id)
    seen = {str(x) for x in record.get("seen_ids") or [] if str(x)}
    pending_raw = record.get("pending") if isinstance(record.get("pending"), dict) else {}
    pending_ids = set(pending_raw.keys())

    cursor = ""
    scanned: list[xhs_pick.Favorite] = []
    boundary_found = False
    exhausted = False
    pages = 0

    try:
        for page_index in range(max(1, max_pages)):
            page = xhs_pick.fetch_user_posts_page(user_id, cursor)
            pages += 1
            for note in page.notes:
                # Pending updates remain before the handled boundary and should be
                # refreshed rather than terminating the scan.
                if note.note_id in seen and note.note_id not in pending_ids:
                    boundary_found = True
                    break
                scanned.append(replace(note, source="subscription", source_key=user_id))
            if boundary_found:
                break
            if not page.has_more or not page.cursor or not page.notes:
                exhausted = True
                break
            cursor = page.cursor
            if delay_ms > 0 and page_index + 1 < max_pages:
                time.sleep(delay_ms / 1000)
    except xhs_pick.PickerError as exc:
        if isinstance(exc, xhs_pick.RiskControlError):
            raise SubscriptionError(str(exc)) from exc
        with _STORE_LOCK:
            data = _load_unlocked(path)
            current = data["authors"].get(user_id)
            if isinstance(current, dict):
                current["last_checked_at"] = _now()
                current["last_checked_epoch"] = time.time()
                current["last_error"] = str(exc)
                _save_unlocked(path, data)
        return (
            CheckAuthorResult(
                user_id=user_id,
                name=name,
                new_count=0,
                pending_count=len(pending_ids),
                pages=pages,
                boundary_found=False,
                complete=False,
                error=str(exc),
            ),
            [],
        )

    # If a pre-existing baseline cannot be found, do not classify a large body of
    # historical posts as new. This is intentionally conservative.
    if seen and not boundary_found:
        reason = "未在本次扫描中找到既有订阅基线，未自动把历史作品判定为更新"
        with _STORE_LOCK:
            data = _load_unlocked(path)
            current = data["authors"].get(user_id)
            if isinstance(current, dict):
                current["last_checked_at"] = _now()
                current["last_checked_epoch"] = time.time()
                current["last_error"] = reason
                _save_unlocked(path, data)
        return (
            CheckAuthorResult(
                user_id=user_id,
                name=name,
                new_count=0,
                pending_count=len(pending_ids),
                pages=pages,
                boundary_found=False,
                complete=exhausted,
                error=reason,
            ),
            scanned,
        )

    new_notes = [note for note in scanned if note.note_id not in seen]
    observed_pending = {note.note_id: note for note in scanned if note.note_id in pending_ids}
    now = _now()

    with _STORE_LOCK:
        data = _load_unlocked(path)
        current = data["authors"].get(user_id)
        if not isinstance(current, dict):
            raise SubscriptionError("订阅已被删除")
        current_pending = current.get("pending")
        if not isinstance(current_pending, dict):
            current_pending = {}
            current["pending"] = current_pending

        # A pending item that is no longer encountered before the handled
        # boundary was likely deleted/unavailable; drop the stale reminder.
        for stale_id in set(current_pending) - set(observed_pending) - {note.note_id for note in new_notes}:
            current_pending.pop(stale_id, None)

        # Refresh metadata for pending items seen in this scan.
        for note_id, note in observed_pending.items():
            first_seen = str((current_pending.get(note_id) or {}).get("first_seen_at") or now)
            current_pending[note_id] = _note_meta(note, first_seen)
        for note in new_notes:
            current_pending[note.note_id] = _note_meta(note, now)

        ordered_seen = [note.note_id for note in new_notes]
        ordered_seen.extend(str(x) for x in current.get("seen_ids") or [] if str(x))
        current["seen_ids"] = list(dict.fromkeys(ordered_seen))[:MAX_SEEN_IDS]
        current["name"] = (scanned[0].author if scanned else str(current.get("name") or name))
        current["last_checked_at"] = now
        current["last_checked_epoch"] = time.time()
        current["last_error"] = ""
        _save_unlocked(path, data)
        pending_count = len(current_pending)

    return (
        CheckAuthorResult(
            user_id=user_id,
            name=name,
            new_count=len(new_notes),
            pending_count=pending_count,
            pages=pages,
            boundary_found=boundary_found,
            complete=True,
        ),
        scanned,
    )


def check_all(
    path: Path,
    *,
    on_author: Callable[[CheckAuthorResult], None] | None = None,
    max_pages: int = 8,
    delay_ms: int = 1500,
    inter_author_delay_ms: int = 2500,
) -> tuple[list[CheckAuthorResult], list[xhs_pick.Favorite]]:
    store = load_store(path)
    user_ids = list(store["authors"].keys())
    results: list[CheckAuthorResult] = []
    fresh_notes: list[xhs_pick.Favorite] = []
    for index, user_id in enumerate(user_ids):
        result, notes = check_author(
            path,
            user_id,
            max_pages=max_pages,
            delay_ms=delay_ms,
        )
        results.append(result)
        fresh_notes.extend(notes)
        if on_author:
            on_author(result)
        if inter_author_delay_ms > 0 and index + 1 < len(user_ids):
            time.sleep(inter_author_delay_ms / 1000)
    return results, fresh_notes


def should_auto_check(path: Path, min_interval_seconds: int = 6 * 60 * 60) -> bool:
    """Avoid re-scanning every author on every application restart.

    Existing v0.5 records have no epoch and therefore receive one compatibility
    check after upgrading; subsequent launches within the interval stay local.
    """
    store = load_store(path)
    now = time.time()
    for record in store["authors"].values():
        if not isinstance(record, dict):
            continue
        try:
            last = float(record.get("last_checked_epoch") or 0)
        except (TypeError, ValueError):
            last = 0
        if not last or now - last >= max(60, int(min_interval_seconds)):
            return True
    return False


def pending_favorites(path: Path) -> list[xhs_pick.Favorite]:
    store = load_store(path)
    items: list[tuple[str, xhs_pick.Favorite]] = []
    for user_id, record in store["authors"].items():
        if not isinstance(record, dict):
            continue
        pending = record.get("pending")
        if not isinstance(pending, dict):
            continue
        for note_id, meta in pending.items():
            if not isinstance(meta, dict):
                continue
            first_seen = str(meta.get("first_seen_at") or "")
            items.append(
                (
                    first_seen,
                    xhs_pick.Favorite(
                        note_id=str(note_id),
                        title=str(meta.get("title") or "（无标题）"),
                        author=str(meta.get("author") or record.get("name") or "未知作者"),
                        web_url="",
                        cover_url=str(meta.get("cover_url") or ""),
                        author_id=str(meta.get("author_id") or user_id),
                        source="subscription",
                        source_key=str(user_id),
                    ),
                )
            )
    items.sort(key=lambda pair: pair[0], reverse=True)
    return [note for _, note in items]


def summary(path: Path) -> dict[str, Any]:
    store = load_store(path)
    authors: list[dict[str, Any]] = []
    total_pending = 0
    for user_id, record in store["authors"].items():
        if not isinstance(record, dict):
            continue
        pending = record.get("pending") if isinstance(record.get("pending"), dict) else {}
        count = len(pending)
        total_pending += count
        authors.append(
            {
                "user_id": user_id,
                "name": str(record.get("name") or user_id),
                "subscribed_at": str(record.get("subscribed_at") or ""),
                "last_checked_at": str(record.get("last_checked_at") or ""),
                "last_error": str(record.get("last_error") or ""),
                "pending_count": count,
            }
        )
    authors.sort(key=lambda item: (-item["pending_count"], item["name"].casefold()))
    return {
        "authors": authors,
        "author_count": len(authors),
        "pending_count": total_pending,
    }
