import tempfile
import unittest
from pathlib import Path
from unittest import mock

import xhs_pick


class CookieTests(unittest.TestCase):
    def test_redbook_command_uses_local_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_cli = Path(tmp) / "cli.js"
            fake_cli.write_text("", encoding="utf-8")
            with mock.patch.object(xhs_pick, "LOCAL_REDBOOK_CLI", fake_cli), \
                 mock.patch.object(xhs_pick, "command_exists", return_value=True):
                self.assertEqual(xhs_pick.redbook_command(), ["node", str(fake_cli)])

    def test_run_redbook_passes_cookie_file_via_environment(self):
        fake = mock.Mock(returncode=0, stdout='{"ok": true}', stderr='')
        with tempfile.TemporaryDirectory() as tmp:
            cookie_file = Path(tmp) / ".redbook-cookies.json"
            cookie_file.write_text('{"cookies":{"a1":"secret","web_session":"s"}}', encoding="utf-8")
            with mock.patch.object(xhs_pick, "COOKIE_FILE", cookie_file), \
                 mock.patch.object(xhs_pick, "redbook_command", return_value=["node", "fake-cli.js"]), \
                 mock.patch.object(xhs_pick.subprocess, "run", return_value=fake) as run:
                payload = xhs_pick.run_redbook_json(["favorites", "--all"])
        self.assertEqual(payload, {"ok": True})
        call = run.call_args
        command = call.args[0]
        self.assertNotIn("secret", " ".join(command))
        self.assertEqual(call.kwargs["env"]["REDBOOK_COOKIE_FILE"], str(cookie_file))
        self.assertEqual(call.kwargs["env"]["REDBOOK_PLATFORM"], "xhs")


class FavoriteTests(unittest.TestCase):
    def test_normalize_redbook_snake_case(self):
        payload = [
            {
                "note_id": "abc",
                "display_title": "标题",
                "webUrl": "https://www.xiaohongshu.com/explore/abc?xsec_token=t",
                "user": {"nickname": "作者"},
            }
        ]
        notes = xhs_pick.normalize_favorites(payload)
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].note_id, "abc")
        self.assertEqual(notes[0].title, "标题")
        self.assertEqual(notes[0].author, "作者")

    def test_normalize_extracts_cover_and_author_id(self):
        payload = [
            {
                "note_id": "abc",
                "display_title": "标题",
                "webUrl": "https://www.xiaohongshu.com/explore/abc?xsec_token=t",
                "cover": {"urlDefault": "https://sns-webpic-qc.xhscdn.com/cover-a"},
                "user": {"nickname": "作者", "user_id": "user-123"},
            }
        ]
        note = xhs_pick.normalize_favorites(payload)[0]
        self.assertEqual(note.author_id, "user-123")
        self.assertEqual(note.cover_url, "https://sns-webpic-qc.xhscdn.com/cover-a")

    def test_normalize_search_note_card(self):
        payload = {
            "items": [
                {
                    "webUrl": "https://www.xiaohongshu.com/explore/search-1?xsec_token=t",
                    "note_card": {
                        "note_id": "search-1",
                        "display_title": "搜索结果",
                        "cover": {"url": "https://sns-webpic-qc.xhscdn.com/c"},
                        "user": {"nickname": "A", "user_id": "u1"},
                    },
                }
            ]
        }
        note = xhs_pick.normalize_notes(payload, source="search", source_key="q::1")[0]
        self.assertEqual(note.source, "search")
        self.assertEqual(note.source_key, "q::1")
        self.assertEqual(note.author_id, "u1")

    def test_normalize_deduplicates(self):
        payload = [
            {"note_id": "abc", "webUrl": "https://www.xiaohongshu.com/explore/abc?a=1"},
            {"note_id": "abc", "webUrl": "https://www.xiaohongshu.com/explore/abc?a=2"},
        ]
        self.assertEqual(len(xhs_pick.normalize_favorites(payload)), 1)

    def test_fetch_user_posts_until_stops_when_targets_found(self):
        pages = [
            xhs_pick.UserPostsPage(notes=(xhs_pick.Favorite("n5", "T5", "A", "u5", author_id="u1"),), has_more=True, cursor="c1"),
            xhs_pick.UserPostsPage(notes=(xhs_pick.Favorite("n4", "T4", "A", "u4", author_id="u1"), xhs_pick.Favorite("n3", "T3", "A", "u3", author_id="u1")), has_more=True, cursor="c2"),
        ]
        with mock.patch.object(xhs_pick, "fetch_user_posts_page", side_effect=pages) as fetch:
            found, complete = xhs_pick.fetch_user_posts_until("u1", {"n5", "n3"}, delay_ms=0)
        self.assertEqual(set(found), {"n5", "n3"})
        self.assertTrue(complete)
        self.assertEqual(fetch.call_count, 2)


class CacheAndShareTests(unittest.TestCase):
    def test_favorites_cache_never_persists_xsec_token(self):
        note = xhs_pick.Favorite(
            note_id="n1", title="T", author="A",
            web_url="https://www.xiaohongshu.com/explore/n1?xsec_token=secret",
            cover_url="https://sns-webpic-qc.xhscdn.com/123456789012/0123456789abcdef0123456789abcdef/a!nd_dft_wlteh",
            author_id="u1",
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".favorites-cache.json"
            xhs_pick.save_favorites_cache([note], path)
            raw = path.read_text(encoding="utf-8")
            self.assertNotIn("secret", raw)
            self.assertNotIn("xsec_token", raw)
            notes, fetched_at = xhs_pick.load_favorites_cache(path)
            self.assertEqual(notes[0].note_id, "n1")
            self.assertTrue(fetched_at)
            self.assertEqual(notes[0].source_key, "cache")

    def test_extract_shared_url_rejects_non_xhs_domain(self):
        with self.assertRaises(xhs_pick.PickerError):
            xhs_pick.resolve_shared_url("https://example.com/test")

    def test_direct_xiaohongshu_link_does_not_need_redirect_request(self):
        url = "https://www.xiaohongshu.com/explore/abc?xsec_token=t"
        with mock.patch.object(xhs_pick.urllib.request, "urlopen") as opener:
            self.assertEqual(xhs_pick.resolve_shared_url(url), url)
            opener.assert_not_called()


class SafetyTests(unittest.TestCase):
    def test_risk_control_opens_circuit_and_redacts_tokens(self):
        fake = mock.Mock(returncode=1, stdout='', stderr='NeedVerify 300012 https://x/?xsec_token=secret')
        with mock.patch.object(xhs_pick, "redbook_command", return_value=["node", "fake.js"]), \
             mock.patch.object(xhs_pick.subprocess, "run", return_value=fake), \
             mock.patch.object(xhs_pick, "_MIN_REDBOOK_INTERVAL_SECONDS", 0):
            old_until = xhs_pick._RISK_BLOCK_UNTIL
            old_last = xhs_pick._LAST_REDBOOK_CALL
            try:
                xhs_pick._RISK_BLOCK_UNTIL = 0
                xhs_pick._LAST_REDBOOK_CALL = 0
                with self.assertRaises(xhs_pick.RiskControlError) as cm:
                    xhs_pick.run_redbook_json(["favorites"])
                self.assertNotIn("secret", str(cm.exception))
                self.assertGreater(xhs_pick._RISK_BLOCK_UNTIL, 0)
            finally:
                xhs_pick._RISK_BLOCK_UNTIL = old_until
                xhs_pick._LAST_REDBOOK_CALL = old_last


class SelectionTests(unittest.TestCase):
    def test_parse_selection(self):
        self.assertEqual(xhs_pick.parse_selection("1,3,5-7", 10), [0, 2, 4, 5, 6])

    def test_parse_selection_reversed_range(self):
        self.assertEqual(xhs_pick.parse_selection("4-2", 5), [1, 2, 3])

    def test_parse_selection_rejects_out_of_range(self):
        with self.assertRaises(xhs_pick.PickerError):
            xhs_pick.parse_selection("6", 5)


class ImageTests(unittest.TestCase):
    def test_extract_snake_case_images(self):
        note = {
            "image_list": [
                {"url_default": "https://example.com/a.jpg"},
                {"url": "https://example.com/b.jpg"},
            ]
        }
        self.assertEqual(
            xhs_pick.extract_image_urls(note),
            ["https://example.com/a.jpg", "https://example.com/b.jpg"],
        )

    def test_extract_camel_case_images(self):
        note = {
            "imageList": [
                {"urlDefault": "https://example.com/a.jpg"},
                {"urlPre": "https://example.com/b.jpg"},
            ]
        }
        self.assertEqual(len(xhs_pick.extract_image_urls(note)), 2)

    def test_extract_string_images(self):
        note = {"images": ["https://example.com/a.jpg"]}
        self.assertEqual(
            xhs_pick.extract_image_urls(note), ["https://example.com/a.jpg"]
        )

    def test_extract_info_list_fallback(self):
        note = {
            "image_list": [
                {
                    "info_list": [
                        {"image_scene": "WB_PRV", "url": "https://example.com/p.jpg"},
                        {"image_scene": "WB_DFT", "url": "https://example.com/d.jpg"},
                    ]
                }
            ]
        }
        self.assertEqual(xhs_pick.extract_image_urls(note), ["https://example.com/d.jpg"])

    def test_stable_image_url_strips_ephemeral_route_and_style(self):
        raw = "https://sns-webpic-qc.xhscdn.com/202609301234/0123456789abcdef0123456789abcdef/notes_pre_post/abc!nd_dft_wlteh_webp_3"
        self.assertEqual(
            xhs_pick.stable_image_url(raw),
            "https://sns-img-bd.xhscdn.com/notes_pre_post/abc",
        )

    def test_sanitize_component(self):
        self.assertEqual(xhs_pick.sanitize_component('a:b/c*?'), "a_b_c")


class DownloadTests(unittest.TestCase):
    def test_download_post_writes_image_and_metadata(self):
        favorite = xhs_pick.Favorite(
            note_id="abc123",
            title="测试标题",
            author="测试作者",
            web_url="https://www.xiaohongshu.com/explore/abc123?xsec_token=fresh",
        )
        detail = {
            "image_list": [
                {"url_default": "https://sns-webpic-qc.xhscdn.com/notes/a"}
            ]
        }
        jpeg = b"\xff\xd8\xff" + b"synthetic"
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(xhs_pick, "read_note", return_value=detail), mock.patch.object(
                xhs_pick, "_request_image", return_value=(jpeg, "image/jpeg")
            ):
                result = xhs_pick.download_post(favorite, Path(tmp), 1, 1)
            self.assertEqual(result.status, "ok")
            folder = Path(result.folder)
            self.assertTrue((folder / "01.jpg").exists())
            meta = (folder / "meta.json").read_text(encoding="utf-8")
            self.assertIn("abc123", meta)
            self.assertNotIn("xsec_token", meta)

    def test_download_reuses_v04_folder_and_existing_numbered_image(self):
        favorite = xhs_pick.Favorite(
            note_id="legacy123", title="新标题", author="作者",
            web_url="https://www.xiaohongshu.com/explore/legacy123?xsec_token=fresh",
        )
        detail = {"image_list": [{"url_default": "https://sns-webpic-qc.xhscdn.com/notes/a"}]}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "legacy123__旧标题"
            legacy.mkdir()
            (legacy / "1.jpg").write_bytes(b"old")
            (legacy / "meta.json").write_text('{"noteId":"legacy123","displayTitle":"旧标题"}', encoding="utf-8")
            with mock.patch.object(xhs_pick, "read_note", return_value=detail), mock.patch.object(
                xhs_pick, "_request_image"
            ) as request_image:
                result = xhs_pick.download_post(favorite, root, 1, 1)
            self.assertEqual(Path(result.folder), legacy)
            self.assertEqual(result.skipped, 1)
            request_image.assert_not_called()
            self.assertFalse((root / "legacy123__新标题").exists())



if __name__ == "__main__":
    unittest.main()
