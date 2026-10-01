import json
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import xhs_web


class WebLibrarySmokeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "downloads"
        self.root.mkdir()
        folder = self.root / "n1__Title"
        folder.mkdir()
        (folder / "meta.json").write_text(
            json.dumps({"note_id": "n1", "title": "Title", "author": "Author", "files": ["01.jpg"]}),
            encoding="utf-8",
        )
        (folder / "01.jpg").write_bytes(b"fake-jpeg")
        self.output_patch = mock.patch.object(xhs_web, "OUTPUT_DIR", self.root)
        self.output_patch.start()
        self.subscription_file = Path(self.tmp.name) / ".subscriptions.json"
        self.subscription_patch = mock.patch.object(xhs_web, "SUBSCRIPTION_FILE", self.subscription_file)
        self.subscription_patch.start()
        xhs_web.STATE.refresh_library()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), xhs_web.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.output_patch.stop()
        self.subscription_patch.stop()
        self.tmp.cleanup()

    def get_json(self, path):
        req = urllib.request.Request(
            self.base + path,
            headers={"X-XHS-Picker-Token": xhs_web.SESSION_TOKEN},
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def post_json(self, path, payload):
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-XHS-Picker-Token": xhs_web.SESSION_TOKEN,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_library_endpoint_and_image_endpoint(self):
        data = self.get_json("/api/library")
        self.assertEqual(data["post_count"], 1)
        self.assertEqual(data["image_count"], 1)
        image_url = data["posts"][0]["images"][0]["url"]
        with urllib.request.urlopen(self.base + image_url, timeout=5) as response:
            self.assertEqual(response.read(), b"fake-jpeg")

    def test_static_page_is_served(self):
        with urllib.request.urlopen(self.base + "/", timeout=5) as response:
            html = response.read().decode("utf-8")
        self.assertIn("本地阅读", html)
        self.assertIn("libraryBadge", html)
        self.assertIn("libraryBanner", html)
        self.assertIn("app.js", html)
        self.assertIn('name="xhs-picker-token"', html)

    def test_resolve_link_endpoint_registers_direct_item(self):
        note = xhs_web.xhs_pick.Favorite(
            note_id="direct1", title="Direct", author="Author",
            web_url="https://www.xiaohongshu.com/explore/direct1?xsec_token=t",
            source="direct", source_key="share-link",
        )
        with mock.patch.object(xhs_web.xhs_pick, "favorite_from_shared_link", return_value=note):
            data = self.post_json("/api/resolve-link", {"text": "share"})
        self.assertEqual(data["item"]["note_id"], "direct1")
        self.assertEqual(data["item"]["source"], "direct")

    def test_subscription_endpoint_subscribe_and_unsubscribe(self):
        note = xhs_web.xhs_pick.Favorite(
            note_id="n2", title="New", author="Author",
            web_url="https://www.xiaohongshu.com/explore/n2?xsec_token=t",
            author_id="u1", source="author", source_key="u1",
        )
        page = xhs_web.xhs_pick.UserPostsPage(notes=(note,), has_more=False, cursor="")
        with mock.patch.object(xhs_web.xhs_pick, "fetch_user_posts_page", return_value=page):
            data = self.post_json("/api/subscriptions/subscribe", {"user_id": "u1", "name": "Author"})
        self.assertEqual(data["author_count"], 1)
        self.assertEqual(data["pending_count"], 0)
        data = self.post_json("/api/subscriptions/unsubscribe", {"user_id": "u1"})
        self.assertEqual(data["author_count"], 0)

    def test_rejects_non_localhost_host_header(self):
        req = urllib.request.Request(self.base + "/", headers={"Host": "evil.example"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(cm.exception.code, 400)

    def test_mutating_api_rejects_missing_local_session_token(self):
        req = urllib.request.Request(
            self.base + "/api/open-output",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(cm.exception.code, 400)

    def test_subscription_static_ui_is_present(self):
        with urllib.request.urlopen(self.base + "/", timeout=5) as response:
            html = response.read().decode("utf-8")
        self.assertIn("订阅作者", html)
        self.assertIn("下载全部更新", html)


if __name__ == "__main__":
    unittest.main()
