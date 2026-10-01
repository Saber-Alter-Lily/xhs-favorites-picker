import json
import tempfile
import unittest
from pathlib import Path

import xhs_library


class LibraryScanTests(unittest.TestCase):
    def make_post(self, root: Path, note_id: str, title: str, author: str, files: list[str]) -> Path:
        folder = root / f"{note_id}__{title}"
        folder.mkdir(parents=True)
        (folder / "meta.json").write_text(
            json.dumps(
                {
                    "note_id": note_id,
                    "title": title,
                    "author": author,
                    "canonical_url": f"https://www.xiaohongshu.com/explore/{note_id}",
                    "saved_at": "2026-09-30T10:00:00+0800",
                    "files": files,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        for name in files:
            (folder / name).write_bytes(b"synthetic-image")
        return folder

    def test_scan_groups_images_by_post_and_author(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_post(root, "n1", "标题一", "作者A", ["01.jpg", "02.png"])
            self.make_post(root, "n2", "标题二", "作者B", ["01.webp"])
            posts = xhs_library.scan_library(root)
            self.assertEqual(len(posts), 2)
            by_id = {p.note_id: p for p in posts}
            self.assertEqual(len(by_id["n1"].images), 2)
            authors = {x["author"]: x for x in xhs_library.authors_summary(posts)}
            self.assertEqual(authors["作者A"]["images"], 2)
            self.assertEqual(authors["作者A"]["posts"], 1)

    def test_scan_reads_v04_legacy_metadata_aliases(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "legacy-note__旧标题"
            folder.mkdir()
            (folder / "meta.json").write_text(
                json.dumps({
                    "noteId": "legacy-note",
                    "displayTitle": "旧版标题",
                    "nickname": "旧版作者",
                    "webUrl": "https://www.xiaohongshu.com/explore/legacy-note",
                    "savedAt": "2026-08-01T00:00:00+0800",
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            (folder / "1.jpg").write_bytes(b"legacy")
            post = xhs_library.scan_library(root)[0]
            self.assertEqual(post.note_id, "legacy-note")
            self.assertEqual(post.title, "旧版标题")
            self.assertEqual(post.author, "旧版作者")
            self.assertEqual(post.images[0].filename, "1.jpg")
            self.assertEqual(xhs_library.find_existing_post_folder(root, "legacy-note"), folder.resolve())

    def test_scan_hides_metadata_only_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_post(root, "n1", "标题", "作者", [])
            self.assertEqual(xhs_library.scan_library(root), [])


class LibraryTransferTests(unittest.TestCase):
    def make_post(self, root: Path, note_id: str = "n1") -> Path:
        folder = root / f"{note_id}__title"
        folder.mkdir(parents=True)
        (folder / "meta.json").write_text(
            json.dumps({"note_id": note_id, "title": "Title", "author": "Author", "files": ["01.jpg", "02.jpg"]}),
            encoding="utf-8",
        )
        (folder / "01.jpg").write_bytes(b"one")
        (folder / "02.jpg").write_bytes(b"two")
        return folder

    def test_copy_preserves_post_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "downloads"
            dest = base / "dest"
            root.mkdir(); dest.mkdir()
            folder = self.make_post(root)
            post = xhs_library.scan_library(root)[0]
            result = xhs_library.transfer_images(
                root, [post.images[0].key], dest, action="copy", preserve_post_folders=True
            )
            self.assertEqual(result["completed"], 1)
            self.assertTrue((dest / folder.name / "01.jpg").exists())
            self.assertTrue((folder / "01.jpg").exists())

    def test_move_updates_disk_and_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "downloads"
            dest = base / "dest"
            root.mkdir(); dest.mkdir()
            folder = self.make_post(root)
            post = xhs_library.scan_library(root)[0]
            result = xhs_library.transfer_images(
                root, [post.images[0].key], dest, action="move", preserve_post_folders=False
            )
            self.assertEqual(result["completed"], 1)
            self.assertFalse((folder / "01.jpg").exists())
            remaining = xhs_library.scan_library(root)[0]
            self.assertEqual([i.filename for i in remaining.images], ["02.jpg"])
            meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["files"], ["02.jpg"])
            self.assertEqual(meta["local_file_count"], 1)

    def test_rejects_destination_inside_library(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "downloads"
            root.mkdir()
            self.make_post(root)
            post = xhs_library.scan_library(root)[0]
            inner = root / "export"
            inner.mkdir()
            with self.assertRaises(xhs_library.LibraryError):
                xhs_library.transfer_images(root, [post.images[0].key], inner, action="copy")


if __name__ == "__main__":
    unittest.main()
