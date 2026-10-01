from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".heic", ".heif"}
SAFE_NAME = re.compile(r'[^\w\- .()\[\]（）【】]+', re.UNICODE)


class LibraryError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LibraryImage:
    note_id: str
    filename: str
    path: Path

    @property
    def key(self) -> str:
        return f"{self.note_id}::{self.filename}"


@dataclass(frozen=True, slots=True)
class LibraryPost:
    note_id: str
    title: str
    author: str
    canonical_url: str
    saved_at: str
    folder: Path
    images: tuple[LibraryImage, ...]

    @property
    def cover(self) -> LibraryImage | None:
        return self.images[0] if self.images else None


def _read_meta(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}




def _first_text(meta: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = meta.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def legacy_meta_values(meta: dict[str, Any], folder: Path) -> tuple[str, str, str, str, str]:
    """Read current and pre-v0.5 metadata without mutating the library.

    v0.4 installations in the wild may have partial metadata. Disk layout stays
    authoritative and no migration is required merely to browse the old library.
    """
    fallback_id = folder.name.split("__", 1)[0].strip()
    note_id = _first_text(meta, "note_id", "noteId", "id") or fallback_id
    fallback_title = folder.name.split("__", 1)[-1].strip() if "__" in folder.name else folder.name
    title = _first_text(meta, "title", "display_title", "displayTitle", "name") or fallback_title or "（无标题）"
    author = _first_text(meta, "author", "nickname", "user_name", "userName") or "未知作者"
    canonical_url = _first_text(meta, "canonical_url", "canonicalUrl", "web_url", "webUrl", "url")
    if not canonical_url and note_id:
        canonical_url = f"https://www.xiaohongshu.com/explore/{note_id}"
    saved_at = _first_text(meta, "saved_at", "savedAt", "downloaded_at", "downloadedAt", "created_at", "createdAt")
    return note_id, title, author, canonical_url, saved_at


def find_existing_post_folder(root: Path, note_id: str) -> Path | None:
    """Find an existing v0.4/v0.5 folder for a note ID.

    Folder reuse is the key compatibility guarantee: title changes in a newer
    release must not create a second copy of a note already downloaded by v0.4.
    """
    root = root.resolve()
    note_id = str(note_id).strip()
    if not note_id or not root.exists():
        return None
    prefix = note_id + "__"
    candidates = [p for p in root.iterdir() if p.is_dir() and (p.name == note_id or p.name.startswith(prefix))]
    if candidates:
        return sorted(candidates, key=lambda p: p.name.casefold())[0].resolve()
    for folder in root.iterdir():
        if not folder.is_dir():
            continue
        meta = _read_meta(folder / "meta.json")
        legacy_id, *_ = legacy_meta_values(meta, folder)
        if legacy_id == note_id:
            return folder.resolve()
    return None

def _image_sort_key(path: Path) -> tuple[int, str]:
    match = re.match(r"^(\d+)", path.stem)
    return (int(match.group(1)) if match else 10**9, path.name.casefold())


def scan_library(root: Path) -> list[LibraryPost]:
    root = root.resolve()
    if not root.exists():
        return []
    posts: list[LibraryPost] = []
    for folder in sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name.casefold()):
        meta = _read_meta(folder / "meta.json")
        note_id, title, author, canonical_url, saved_at = legacy_meta_values(meta, folder)
        if not note_id:
            continue
        images = tuple(
            LibraryImage(note_id=note_id, filename=path.name, path=path.resolve())
            for path in sorted(
                (
                    path
                    for path in folder.iterdir()
                    if path.is_file()
                    and path.suffix.casefold() in IMAGE_SUFFIXES
                    and path.stat().st_size > 0
                ),
                key=_image_sort_key,
            )
        )
        # The library is for locally available images. A metadata-only folder is
        # intentionally hidden after all of its images were moved elsewhere.
        if not images:
            continue
        posts.append(
            LibraryPost(
                note_id=note_id,
                title=title,
                author=author,
                canonical_url=canonical_url,
                saved_at=saved_at,
                folder=folder.resolve(),
                images=images,
            )
        )
    posts.sort(key=lambda p: (p.saved_at, p.note_id), reverse=True)
    return posts


def authors_summary(posts: list[LibraryPost]) -> list[dict[str, Any]]:
    counts: dict[str, dict[str, int]] = {}
    for post in posts:
        entry = counts.setdefault(post.author, {"posts": 0, "images": 0})
        entry["posts"] += 1
        entry["images"] += len(post.images)
    return [
        {"author": author, **stats}
        for author, stats in sorted(counts.items(), key=lambda item: (-item[1]["posts"], item[0].casefold()))
    ]


def index_images(posts: list[LibraryPost]) -> dict[str, tuple[LibraryPost, LibraryImage]]:
    result: dict[str, tuple[LibraryPost, LibraryImage]] = {}
    for post in posts:
        for image in post.images:
            result[image.key] = (post, image)
    return result


def resolve_image(posts: list[LibraryPost], note_id: str, filename: str) -> Path:
    for post in posts:
        if post.note_id != note_id:
            continue
        for image in post.images:
            if image.filename == filename:
                return image.path
    raise LibraryError("图片不存在，可能已被移动或删除")


def image_content_type(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _sanitize_flat_name(value: str, limit: int = 70) -> str:
    value = SAFE_NAME.sub("_", value).strip(" ._")
    return (value[:limit].rstrip(" ._") or "untitled")


def _unique_target(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(2, 10_000):
        candidate = path.with_name(f"{stem} ({index}){suffix}")
        if not candidate.exists():
            return candidate
    raise LibraryError(f"目标目录同名文件过多：{path.name}")


def _destination_is_inside_library(destination: Path, root: Path) -> bool:
    try:
        destination.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _rewrite_meta_files(post: LibraryPost) -> None:
    meta_path = post.folder / "meta.json"
    if not meta_path.exists():
        return
    meta = _read_meta(meta_path)
    actual = [
        path.name
        for path in sorted(
            (
                p
                for p in post.folder.iterdir()
                if p.is_file() and p.suffix.casefold() in IMAGE_SUFFIXES and p.stat().st_size > 0
            ),
            key=_image_sort_key,
        )
    ]
    meta["files"] = actual
    meta["local_file_count"] = len(actual)
    try:
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        # Disk state remains authoritative even if best-effort metadata update fails.
        pass


def transfer_images(
    root: Path,
    image_keys: list[str],
    destination: Path,
    *,
    action: str,
    preserve_post_folders: bool = True,
) -> dict[str, Any]:
    if action not in {"copy", "move"}:
        raise LibraryError("操作必须是 copy 或 move")
    root = root.resolve()
    destination = destination.expanduser().resolve()
    if not destination.exists() or not destination.is_dir():
        raise LibraryError("目标目录不存在")
    if _destination_is_inside_library(destination, root):
        raise LibraryError("目标目录不能位于当前 downloads 图库内部")

    posts = scan_library(root)
    by_key = index_images(posts)
    clean_keys = list(dict.fromkeys(str(key).strip() for key in image_keys if str(key).strip()))
    if not clean_keys:
        raise LibraryError("没有选择图片")

    completed: list[dict[str, str]] = []
    failures: list[dict[str, str]] = []
    touched_posts: dict[str, LibraryPost] = {}

    for key in clean_keys:
        pair = by_key.get(key)
        if pair is None:
            failures.append({"key": key, "error": "图片已不存在或图库状态已变化"})
            continue
        post, image = pair
        touched_posts[post.note_id] = post
        try:
            if preserve_post_folders:
                target_dir = destination / post.folder.name
                target_dir.mkdir(parents=True, exist_ok=True)
                target = _unique_target(target_dir / image.filename)
            else:
                prefix = _sanitize_flat_name(f"{post.author}__{post.title}__{post.note_id}")
                target = _unique_target(destination / f"{prefix}__{image.filename}")
            if action == "copy":
                shutil.copy2(image.path, target)
            else:
                shutil.move(str(image.path), str(target))
            completed.append({"key": key, "target": str(target)})
        except (OSError, shutil.Error) as exc:
            failures.append({"key": key, "error": str(exc)})

    if action == "move":
        for post in touched_posts.values():
            _rewrite_meta_files(post)

    return {
        "action": action,
        "requested": len(clean_keys),
        "completed": len(completed),
        "failed": len(failures),
        "destination": str(destination),
        "items": completed,
        "failures": failures,
    }


def choose_folder_windows(title: str = "选择目标文件夹") -> str:
    if os.name != "nt":
        raise LibraryError("系统文件夹选择框目前仅支持 Windows")
    safe_title = title.replace("'", "''")
    command = (
        "$ErrorActionPreference='Stop';"
        "$shell=New-Object -ComObject Shell.Application;"
        f"$folder=$shell.BrowseForFolder(0,'{safe_title}',0,0);"
        "if($folder){[Console]::OutputEncoding=[Text.UTF8Encoding]::new();Write-Output $folder.Self.Path}"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LibraryError(f"无法打开文件夹选择框：{exc}") from exc
    if proc.returncode != 0:
        raise LibraryError((proc.stderr or "文件夹选择失败").strip())
    return proc.stdout.strip().splitlines()[-1].strip() if proc.stdout.strip() else ""
