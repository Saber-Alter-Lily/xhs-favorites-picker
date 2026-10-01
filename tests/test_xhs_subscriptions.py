import tempfile
import unittest
from pathlib import Path
from unittest import mock

import xhs_pick
import xhs_subscriptions


def fav(note_id: str, author_id: str = "u1", author: str = "作者") -> xhs_pick.Favorite:
    return xhs_pick.Favorite(
        note_id=note_id,
        title=f"标题 {note_id}",
        author=author,
        web_url=f"https://www.xiaohongshu.com/explore/{note_id}?xsec_token=t",
        cover_url=f"https://example.com/{note_id}.jpg",
        author_id=author_id,
        source="author",
        source_key=author_id,
    )


class SubscriptionTests(unittest.TestCase):
    def test_subscribe_establishes_baseline_without_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".subscriptions.json"
            page = xhs_pick.UserPostsPage(notes=(fav("n3"), fav("n2"), fav("n1")), has_more=False, cursor="")
            with mock.patch.object(xhs_pick, "fetch_user_posts_page", return_value=page):
                xhs_subscriptions.subscribe(path, "u1", "作者")
            data = xhs_subscriptions.summary(path)
            self.assertEqual(data["author_count"], 1)
            self.assertEqual(data["pending_count"], 0)
            store = xhs_subscriptions.load_store(path)
            self.assertEqual(store["authors"]["u1"]["seen_ids"], ["n3", "n2", "n1"])

    def test_check_finds_new_posts_and_keeps_them_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".subscriptions.json"
            baseline = xhs_pick.UserPostsPage(notes=(fav("n3"), fav("n2")), has_more=False, cursor="")
            update = xhs_pick.UserPostsPage(notes=(fav("n5"), fav("n4"), fav("n3")), has_more=False, cursor="")
            with mock.patch.object(xhs_pick, "fetch_user_posts_page", return_value=baseline):
                xhs_subscriptions.subscribe(path, "u1", "作者")
            with mock.patch.object(xhs_pick, "fetch_user_posts_page", return_value=update):
                result, notes = xhs_subscriptions.check_author(path, "u1")
            self.assertEqual(result.new_count, 2)
            self.assertEqual(result.pending_count, 2)
            self.assertEqual([n.note_id for n in notes], ["n5", "n4"])
            pending = xhs_subscriptions.pending_favorites(path)
            self.assertEqual({n.note_id for n in pending}, {"n4", "n5"})

    def test_pending_survives_next_check_until_handled(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".subscriptions.json"
            baseline = xhs_pick.UserPostsPage(notes=(fav("n3"), fav("n2")), has_more=False, cursor="")
            update = xhs_pick.UserPostsPage(notes=(fav("n4"), fav("n3")), has_more=False, cursor="")
            with mock.patch.object(xhs_pick, "fetch_user_posts_page", return_value=baseline):
                xhs_subscriptions.subscribe(path, "u1", "作者")
            with mock.patch.object(xhs_pick, "fetch_user_posts_page", return_value=update):
                xhs_subscriptions.check_author(path, "u1")
                again, _ = xhs_subscriptions.check_author(path, "u1")
            self.assertEqual(again.new_count, 0)
            self.assertEqual(again.pending_count, 1)
            self.assertEqual(xhs_subscriptions.mark_handled(path, ["n4"]), 1)
            self.assertEqual(xhs_subscriptions.summary(path)["pending_count"], 0)

    def test_unsubscribe_removes_pending_and_author(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".subscriptions.json"
            page = xhs_pick.UserPostsPage(notes=(fav("n1"),), has_more=False, cursor="")
            with mock.patch.object(xhs_pick, "fetch_user_posts_page", return_value=page):
                xhs_subscriptions.subscribe(path, "u1", "作者")
            self.assertTrue(xhs_subscriptions.unsubscribe(path, "u1"))
            self.assertEqual(xhs_subscriptions.summary(path)["author_count"], 0)

    def test_auto_check_is_rate_limited_after_recent_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".subscriptions.json"
            page = xhs_pick.UserPostsPage(notes=(fav("n1"),), has_more=False, cursor="")
            with mock.patch.object(xhs_pick, "fetch_user_posts_page", return_value=page):
                xhs_subscriptions.subscribe(path, "u1", "作者")
            self.assertFalse(xhs_subscriptions.should_auto_check(path, min_interval_seconds=3600))
            store = xhs_subscriptions.load_store(path)
            store["authors"]["u1"].pop("last_checked_epoch", None)
            xhs_subscriptions.save_store(path, store)
            self.assertTrue(xhs_subscriptions.should_auto_check(path, min_interval_seconds=3600))

    def test_missing_baseline_is_conservative(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".subscriptions.json"
            baseline = xhs_pick.UserPostsPage(notes=(fav("old"),), has_more=False, cursor="")
            no_anchor = xhs_pick.UserPostsPage(notes=(fav("a"), fav("b")), has_more=False, cursor="")
            with mock.patch.object(xhs_pick, "fetch_user_posts_page", return_value=baseline):
                xhs_subscriptions.subscribe(path, "u1", "作者")
            with mock.patch.object(xhs_pick, "fetch_user_posts_page", return_value=no_anchor):
                result, _ = xhs_subscriptions.check_author(path, "u1")
            self.assertEqual(result.new_count, 0)
            self.assertTrue(result.error)
            self.assertEqual(xhs_subscriptions.summary(path)["pending_count"], 0)


if __name__ == "__main__":
    unittest.main()
