import io
import json
import re
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock
import zipfile
from pathlib import Path
from urllib.parse import unquote

THAI_FONTS = "/usr/share/fonts/truetype/tlwg"

import chat
import telegram_bot
from app import create_app


class FileManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "files"
        self.app = create_app(
            {
                "TESTING": True,
                "FILES_ROOT": str(self.root),
                "NOTES_DATABASE": str(Path(self.temp_dir.name) / "notes.sqlite3"),
                "FEED_MEDIA": str(Path(self.temp_dir.name) / "feed"),
                "SECRET_KEY": "test-secret",
            }
        )
        self.client = self.app.test_client()
        # Every page needs a signed-in user; the tests use an administrator.
        self.admin_id = self.app.extensions["users"].add("admin", "admin-password", is_admin=True)
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.admin_id

    def tearDown(self):
        self.temp_dir.cleanup()

    def _csrf(self):
        self.client.get("/")
        with self.client.session_transaction() as sess:
            return sess["_csrf_token"]

    def test_create_folder_upload_and_download_file(self):
        token = self._csrf()
        response = self.client.post(
            "/folder",
            data={"_csrf_token": token, "path": "", "name": "docs"},
        )
        self.assertEqual(response.status_code, 302)
        response = self.client.post(
            "/upload",
            data={
                "_csrf_token": token,
                "path": "docs",
                "files": (io.BytesIO(b"hello"), "hello.txt"),
                "folder_files": (io.BytesIO(b"image"), "photos/picture.jpg"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 302)
        response = self.client.get("/download/docs/hello.txt")
        self.assertEqual(response.data, b"hello")
        self.assertEqual(response.headers["Content-Disposition"], "attachment; filename=hello.txt")
        response.close()
        self.assertEqual((self.root / "docs" / "photos" / "picture.jpg").read_bytes(), b"image")

    def test_folder_download_is_zip(self):
        (self.root / "folder").mkdir()
        (self.root / "folder" / "note.txt").write_text("hello")
        response = self.client.get("/download/folder")
        self.assertEqual(response.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            self.assertEqual(archive.namelist(), ["folder/note.txt"])
            self.assertEqual(archive.read("folder/note.txt"), b"hello")
        response.close()

    def test_no_login_is_required_and_path_traversal_is_rejected(self):
        self.assertEqual(self.client.get("/files").status_code, 200)
        self.assertEqual(self.client.get("/files?path=../outside").status_code, 400)

    def test_feed_is_the_home_page_and_files_sits_below_it(self):
        (self.root / "docs").mkdir()
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn('class="sidebar-link" href="/feed" title="ฟีด" aria-current="page"', page)
        self.assertIn('class="sidebar-brand" href="/feed"', page)
        menu = page.split('<nav class="sidebar-nav"')[1].split("</nav>")[0]
        self.assertEqual(re.findall(r'class="sidebar-link" href="([^"]+)"', menu)[:2], ["/feed", "/files"])
        self.assertIn("Shared files", self.client.get("/files").get_data(as_text=True))
        # Old links to a folder still open it in Files.
        response = self.client.get("/?path=docs")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/files?path=docs")

    def test_feed_dashboard_shows_wallet_and_missions(self):
        token = self._csrf()
        wallet = self.app.extensions["wallet"]
        missions = self.app.extensions["missions"]
        wallet.deposit(self.admin_id, 1250, "Start", self.admin_id)
        page = self.client.get("/").get_data(as_text=True)
        dashboard = page.split('<section class="feed-dashboard"')[1].split("</section>")[0]
        self.assertIn("1,250 <small>points</small>", dashboard)
        self.assertIn("↓ 1,250 in", dashboard)
        self.assertIn("You're all caught up.", dashboard)
        self.assertIn("<strong>0</strong><small>to do</small>", dashboard)

        self.client.post("/missions", data={"_csrf_token": token, "title": "Tidy Projects", "reward": "250"})
        mission_id = missions.all()[0]["id"]
        dashboard = self.client.get("/").get_data(as_text=True).split('<section class="feed-dashboard"')[1]
        self.assertIn("<strong>1</strong><small>to do</small>", dashboard)
        self.assertIn(f'href="/missions#mission-{mission_id}"', dashboard)
        self.assertIn("Tidy Projects", dashboard)
        self.assertIn("🪙 250", dashboard)

        # Once your work is sent in, the mission leaves your to-do list.
        self.client.post(f"/missions/{mission_id}/submit", data={"_csrf_token": token, "body": "Done"})
        dashboard = self.client.get("/").get_data(as_text=True).split('<section class="feed-dashboard"')[1]
        self.assertIn("<strong>0</strong><small>to do</small>", dashboard)

        # Profiles, single posts and older pages don't show it.
        self.assertNotIn("feed-dashboard", self.client.get("/feed/u/admin").get_data(as_text=True))
        self.assertNotIn("feed-dashboard", self.client.get("/feed?before=999").get_data(as_text=True))

    def test_update_log_page_lists_versions_and_changes(self):
        response = self.client.get("/updates?per=0")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Update log", response.data)
        self.assertIn(b"Version 1.3.0", response.data)
        self.assertIn(b"Version 1.2.0", response.data)
        self.assertIn(b"rename actions", response.data)
        self.assertIn(b"does not automatically check", response.data)

    def test_update_log_search_filters_entries(self):
        response = self.client.get("/updates?q=redis")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'name="q"', response.data)
        self.assertIn(b"Version 1.7.0", response.data)
        self.assertNotIn(b"Version 1.0.0", response.data)
        self.assertNotIn(b"Notes are now stored in PostgreSQL", response.data)

        response = self.client.get("/updates?q=1.2.0")
        self.assertIn(b"Version 1.2.0", response.data)
        self.assertIn(b"Added confirmed deletion", response.data)

        response = self.client.get("/updates?q=nothing-matches-this")
        self.assertIn(b"No updates match", response.data)

    def test_update_log_pages_and_remembers_page_size(self):
        import app as app_module

        newest, total = app_module.UPDATE_LOG[0]["version"], len(app_module.UPDATE_LOG)
        page = self.client.get("/updates").get_data(as_text=True)
        self.assertIn(f"Version {newest}<", page)
        self.assertNotIn("Version 1.0.0<", page)
        self.assertIn(f"1–10 of {total} versions", page)
        self.assertIn('aria-current="page">1<', page)
        self.assertIn("page=2#patch-notes", page)

        # The last page holds the oldest version; a page past the end shows the last page.
        last = -(-total // 10)
        for url in (f"/updates?page={last}", "/updates?page=999", "/updates?page=nope"):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)
        self.assertIn("Version 1.0.0<", self.client.get("/updates?page=999").get_data(as_text=True))
        self.assertIn(f"Version {newest}<", self.client.get("/updates?page=nope").get_data(as_text=True))

        # The chosen size sticks for later visits; sizes that aren't offered are ignored.
        page = self.client.get("/updates?per=5").get_data(as_text=True)
        self.assertIn(f"1–5 of {total} versions", page)
        self.assertIn(f"1–5 of {total} versions", self.client.get("/updates").get_data(as_text=True))
        self.assertIn(f"1–5 of {total} versions", self.client.get("/updates?per=7").get_data(as_text=True))
        page = self.client.get("/updates?per=0").get_data(as_text=True)
        self.assertIn(f"1–{total} of {total} versions", page)
        self.assertIn("Version 1.0.0<", page)
        self.assertNotIn("page=2#patch-notes", page)

    def test_update_log_requests_come_in_pages(self):
        token = self._csrf()
        store = self.app.extensions["requests"]
        ids = [store.add("feature", f"Request number {n:02d}", "", "member") for n in range(12)]
        page = self.client.get("/updates?rper=5").get_data(as_text=True)
        self.assertIn("1–5 of 12 requests", page)
        self.assertEqual(page.count('class="request-item'), 5)
        page = self.client.get("/updates?rpage=3").get_data(as_text=True)
        self.assertIn("11–12 of 12 requests", page)
        self.assertEqual(page.count('class="request-item'), 2)
        # Other lists' pages and the search stay in the links.
        self.assertIn('name="rper"', page)
        self.assertIn("rpage=2", self.client.get("/updates?rpage=3&page=2").get_data(as_text=True))

        # Changing a request's status brings you back to the same page.
        response = self.client.post(f"/updates/requests/{ids[0]}/status", data={
            "_csrf_token": token, "status": "done", "shown_page": "3",
        })
        self.assertIn("rpage=3", response.headers["Location"])

    def test_pages_include_mobile_viewport(self):
        for url in ("/", "/updates", "/notes"):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertIn(
                    b'<meta name="viewport" content="width=device-width, initial-scale=1',
                    response.data,
                )

    def test_every_page_shares_the_macos_stylesheet(self):
        for url in ("/", "/updates", "/notes", "/chat", "/convert", "/feed", "/settings", "/missions", "/wallet"):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertIn(b"static/style.css", response.data)

    def test_stylesheets_use_the_macos_design_tokens(self):
        static = Path(__file__).parent / "static"
        style = (static / "style.css").read_text()
        for token in ("--mac-accent:", "--mac-surface:", "--mac-window:", "--mac-text:", "--mac-font:", "--mac-radius-lg:"):
            self.assertIn(token, style)
        # Dark Mode redefines the same tokens for every page.
        dark = style.split("@media (prefers-color-scheme: dark)", 1)[1].split("\n}\n", 1)[0]
        self.assertIn("--mac-surface:", dark)
        self.assertIn("--mac-text:", dark)
        # The old one-off palette is gone, so every page draws from the same colors.
        for css in ("style.css", "pdf-editor.css"):
            text = (static / css).read_text().lower()
            for old in ("#155eef", "#182230", "#667085", "#e4e7ec", "#e1a500"):
                with self.subTest(css=css, color=old):
                    self.assertNotIn(old, text)

    def test_notes_can_be_created_updated_and_deleted(self):
        token = self._csrf()
        response = self.client.get("/notes")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"No notes yet", response.data)

        response = self.client.post(
            "/notes/create",
            data={
                "_csrf_token": token,
                "title": "Shopping",
                "content": "Apples\nBread",
            },
        )
        self.assertEqual(response.status_code, 302)
        response = self.client.get("/notes")
        self.assertIn(b"Shopping", response.data)
        self.assertIn(b"Apples\nBread", response.data)
        connection = sqlite3.connect(self.app.config["NOTES_DATABASE"])
        try:
            note_id = connection.execute("SELECT id FROM notes").fetchone()[0]
        finally:
            connection.close()

        response = self.client.post(
            f"/notes/{note_id}/edit",
            data={
                "_csrf_token": token,
                "title": "Shopping list",
                "content": "Apples\nBread\nTea",
            },
        )
        self.assertEqual(response.status_code, 302)
        response = self.client.get("/notes")
        self.assertIn(b"Shopping list", response.data)
        self.assertIn(b"Apples\nBread\nTea", response.data)

        response = self.client.post(
            f"/notes/{note_id}/delete", data={"_csrf_token": token}
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn(b"No notes yet", self.client.get("/notes").data)

    def test_notes_list_selection_search_and_new_note(self):
        token = self._csrf()
        response = self.client.get("/notes")
        self.assertIn(b"Create note", response.data)

        response = self.client.post(
            "/notes/create",
            data={"_csrf_token": token, "title": "Groceries", "content": "Milk"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/notes?id=", response.headers["Location"])
        self.client.post(
            "/notes/create",
            data={"_csrf_token": token, "title": "Work", "content": "Send report"},
        )
        connection = sqlite3.connect(self.app.config["NOTES_DATABASE"])
        try:
            groceries_id = connection.execute(
                "SELECT id FROM notes WHERE title = 'Groceries'"
            ).fetchone()[0]
        finally:
            connection.close()

        response = self.client.get(f"/notes?id={groceries_id}")
        self.assertIn(b'class="notes-app has-selection"', response.data)
        self.assertIn(f"/notes/{groceries_id}/edit".encode(), response.data)
        self.assertIn(b"2 notes", response.data)

        response = self.client.get("/notes?q=report")
        self.assertIn(b"Send report", response.data)
        self.assertNotIn(b"Groceries", response.data)

        response = self.client.get("/notes?q=nothing-here")
        self.assertIn(b"No notes match", response.data)

        self.client.post(
            "/notes/create",
            data={"_csrf_token": token, "title": "Line one\nLine two", "content": "Body"},
        )
        connection = sqlite3.connect(self.app.config["NOTES_DATABASE"])
        try:
            self.assertIsNotNone(
                connection.execute(
                    "SELECT id FROM notes WHERE title = 'Line one Line two'"
                ).fetchone()
            )
        finally:
            connection.close()

        response = self.client.get("/notes?new=1")
        self.assertIn(b"Create note", response.data)
        self.assertEqual(self.client.get("/notes?id=999").status_code, 404)

    def test_notes_require_title_and_content(self):
        response = self.client.post(
            "/notes/create",
            data={"_csrf_token": self._csrf(), "title": "Title", "content": ""},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn(b"Enter both a title and note text", self.client.get("/notes").data)

    def test_renames_file_and_folder(self):
        (self.root / "old.txt").write_text("contents")
        (self.root / "old-folder").mkdir()
        token = self._csrf()

        response = self.client.post(
            "/rename",
            data={"_csrf_token": token, "path": "old.txt", "name": "new.txt"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse((self.root / "old.txt").exists())
        self.assertEqual((self.root / "new.txt").read_text(), "contents")

        response = self.client.post(
            "/rename",
            data={
                "_csrf_token": token,
                "path": "old-folder",
                "name": "new-folder",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue((self.root / "new-folder").is_dir())

    def test_delete_file_and_folder(self):
        (self.root / "remove.txt").write_text("contents")
        nested = self.root / "remove-folder" / "nested"
        nested.mkdir(parents=True)
        (nested / "file.txt").write_text("contents")
        token = self._csrf()

        for path in ("remove.txt", "remove-folder"):
            response = self.client.post(
                "/delete", data={"_csrf_token": token, "path": path}
            )
            self.assertEqual(response.status_code, 303)
            self.assertFalse((self.root / path).exists())

    def test_deleting_an_item_that_is_already_gone_returns_to_its_folder(self):
        (self.root / "docs").mkdir()
        (self.root / "docs" / "ไฟล์.txt").write_text("contents")
        token = self._csrf()
        first = self.client.post("/delete", data={"_csrf_token": token, "path": "docs/ไฟล์.txt"})
        self.assertEqual(first.status_code, 303)
        # A double click or Reload sends the same delete again.
        again = self.client.post("/delete", data={"_csrf_token": token, "path": "docs/ไฟล์.txt"})
        self.assertEqual(again.status_code, 303)
        self.assertIn("path=docs", again.headers["Location"])
        page = self.client.get(again.headers["Location"]).get_data(as_text=True)
        self.assertIn("That item no longer exists", page)

        renamed = self.client.post("/rename", data={"_csrf_token": token, "path": "gone/old.txt", "name": "new.txt"})
        self.assertEqual(renamed.status_code, 303)
        self.assertNotIn("path=", renamed.headers["Location"])  # Its folder is gone too: back to the top.

    def test_rename_rejects_existing_destination_and_traversal(self):
        (self.root / "source.txt").write_text("source")
        (self.root / "existing.txt").write_text("existing")
        token = self._csrf()

        response = self.client.post(
            "/rename",
            data={
                "_csrf_token": token,
                "path": "source.txt",
                "name": "existing.txt",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual((self.root / "source.txt").read_text(), "source")

        response = self.client.post(
            "/rename",
            data={"_csrf_token": token, "path": "source.txt", "name": "../outside"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse((self.root.parent / "outside").exists())
        self.assertTrue((self.root / "source.txt").exists())


    def _png_bytes(self, color="red", size=(40, 30)):
        from PIL import Image

        buffer = io.BytesIO()
        Image.new("RGB", size, color).save(buffer, "PNG")
        return buffer.getvalue()

    def _pdf_bytes(self, pages=1):
        import pymupdf

        document = pymupdf.open()
        for number in range(pages):
            document.new_page().insert_text((72, 72), f"Hello page {number + 1}")
        data = document.tobytes()
        document.close()
        return data

    def test_convert_page_lists_all_tools(self):
        response = self.client.get("/convert")
        self.assertEqual(response.status_code, 200)
        for title in (b"Word to PDF", b"PDF to Word", b"Image to PDF", b"PDF to Image", b"Merge PDFs", b"Edit PDF"):
            self.assertIn(title, response.data)

    def test_image_to_pdf_combines_images(self):
        import pymupdf

        response = self.client.post(
            "/convert/image2pdf",
            data={
                "_csrf_token": self._csrf(),
                "page_size": "a4",
                "files": [
                    (io.BytesIO(self._png_bytes("red")), "รูปภาพ.png"),
                    (io.BytesIO(self._png_bytes("blue", (30, 60))), "second.png"),
                ],
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/pdf")
        self.assertIn("filename*=UTF-8''%E0%B8%A3", response.headers["Content-Disposition"])
        document = pymupdf.open(stream=response.data, filetype="pdf")
        self.assertEqual(document.page_count, 2)
        # A4 pages, turned to landscape for the wide first image.
        first, second = document[0].rect, document[1].rect
        self.assertAlmostEqual(first.width, 841.9, delta=1)
        self.assertAlmostEqual(first.height, 595.3, delta=1)
        self.assertAlmostEqual(second.width, 595.3, delta=1)
        response.close()

    def test_merge_pdfs_keeps_upload_order(self):
        import pymupdf

        token = self._csrf()
        response = self.client.post(
            "/convert/pdfmerge",
            data={
                "_csrf_token": token,
                "files": [
                    (io.BytesIO(self._pdf_bytes(2)), "สัญญา.pdf"),
                    (io.BytesIO(self._pdf_bytes(1)), "appendix.pdf"),
                    (io.BytesIO(self._pdf_bytes(3)), "photos.pdf"),
                ],
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/pdf")
        self.assertIn("-merged.pdf", response.headers["Content-Disposition"])
        document = pymupdf.open(stream=response.data, filetype="pdf")
        self.assertEqual(document.page_count, 6)
        self.assertEqual(
            [(title, page) for _, title, page in document.get_toc()],
            [("สัญญา", 1), ("appendix", 3), ("photos", 4)],
        )
        self.assertIn("Hello page 1", document[2].get_text())
        document.close()
        response.close()

        one = self.client.post(
            "/convert/pdfmerge",
            data={"_csrf_token": token, "files": (io.BytesIO(self._pdf_bytes(1)), "only.pdf")},
            content_type="multipart/form-data",
            headers={"X-Requested-With": "fetch"},
        )
        self.assertEqual(one.status_code, 400)
        self.assertIn("at least two", one.get_json()["error"])

        broken = self.client.post(
            "/convert/pdfmerge",
            data={"_csrf_token": token, "files": [
                (io.BytesIO(self._pdf_bytes(1)), "good.pdf"),
                (io.BytesIO(b"not a pdf"), "bad.pdf"),
            ]},
            content_type="multipart/form-data",
            headers={"X-Requested-With": "fetch"},
        )
        self.assertEqual(broken.status_code, 400)
        self.assertIn("“bad”", broken.get_json()["error"])

    def _open_in_editor(self, token, pages=2):
        response = self.client.post(
            "/convert/edit/open",
            data={"_csrf_token": token, "files": (io.BytesIO(self._pdf_bytes(pages)), "สัญญา.pdf")},
            content_type="multipart/form-data",
            headers={"X-Requested-With": "fetch"},
        )
        self.assertEqual(response.status_code, 200)
        return response.get_json()["url"]

    def test_pdf_editor_edits_text_adds_items_and_reorders_pages(self):
        import base64
        import pymupdf
        import pdf_editor

        with mock.patch.object(pdf_editor, "WORKSPACE_ROOT", Path(self.temp_dir.name) / "edit"):
            token = self._csrf()
            url = self._open_in_editor(token)
            page = self.client.get(url)
            self.assertEqual(page.status_code, 200)
            self.assertIn(b"pe-data", page.data)

            image = self.client.get(url + "/page/0.png?w=400")
            self.assertTrue(image.data.startswith(b"\x89PNG"))
            self.assertEqual(self.client.get(url + "/page/9.png").status_code, 404)
            lines = self.client.get(url + "/page/0/text").get_json()["lines"]
            self.assertEqual(lines[0]["text"], "Hello page 1")

            line = lines[0]
            png = "data:image/png;base64," + base64.b64encode(self._png_bytes("red")).decode()
            state = {
                "pages": [
                    {"src": 1, "rotate": 90, "objects": []},
                    {"src": 0, "rotate": 0, "objects": [
                        {**line, "type": "textedit", "orig": dict(line), "text": "แก้ไขแล้ว Changed"},
                        {"type": "text", "x": 72, "y": 200, "w": 200, "h": 20, "text": "Added note", "size": 12},
                        {"type": "image", "x": 72, "y": 260, "w": 50, "h": 50, "data": png},
                        {"type": "ink", "points": [[[10, 10], [40, 40]]], "color": "#ff0000", "width": 2},
                        {"type": "highlight", "x": 72, "y": 195, "w": 80, "h": 18},
                        {"type": "note", "x": 300, "y": 300, "text": "Check this"},
                    ]},
                    {"src": None, "w": 300, "h": 400, "objects": []},
                ],
                "watermark": {"enabled": True, "text": "DRAFT", "size": 50, "color": "#ff0000", "opacity": 0.2, "angle": 45},
                "pageNumbers": {"enabled": True, "format": "Page {n} of {total}", "position": "bottom-right", "size": 9},
            }
            headers = {"X-CSRF-Token": token}
            self.assertEqual(self.client.post(url + "/state", json={"version": 1, **state}).status_code, 400)
            self.assertEqual(self.client.post(url + "/state", json={"version": 1, **state}, headers=headers).status_code, 200)
            self.assertIn(b"DRAFT", self.client.get(url).data)  # Saved edits come back on reload.

            response = self.client.post(url + "/save", json={"state": state}, headers=headers)
            self.assertEqual(response.status_code, 200, response.data[:200])
            self.assertIn("-edited.pdf", response.headers["Content-Disposition"])
            document = pymupdf.open(stream=response.data, filetype="pdf")
            self.assertEqual(document.page_count, 3)
            self.assertEqual(document[0].rotation, 90)
            self.assertIn("Hello page 2", document[0].get_text())
            text = document[1].get_text()
            self.assertNotIn("Hello page 1", text)
            self.assertIn("Changed", text)
            self.assertIn("Added note", text)
            self.assertIn("Page 2 of 3", text)
            self.assertIn("DRAFT", text)
            self.assertEqual(len(document[1].get_images()), 1)
            self.assertEqual(sorted(annot.type[1] for annot in document[1].annots()), ["Highlight", "Text"])
            self.assertEqual(document[2].rect.width, 300)
            document.close()

            response = self.client.post(url + "/save", json={"state": state, "target": "files"}, headers=headers)
            self.assertEqual(response.get_json()["saved"], "สัญญา-edited.pdf")
            self.assertTrue((self.root / "สัญญา-edited.pdf").is_file())

    def test_pdf_editor_redaction_removes_text_and_rejects_bad_input(self):
        import pymupdf
        import pdf_editor

        with mock.patch.object(pdf_editor, "WORKSPACE_ROOT", Path(self.temp_dir.name) / "edit"):
            token = self._csrf()
            url = self._open_in_editor(token, pages=1)
            line = self.client.get(url + "/page/0/text").get_json()["lines"][0]
            state = {"pages": [{"src": 0, "objects": [{"type": "redact", **{k: line[k] for k in "xywh"}}]}]}
            response = self.client.post(url + "/save", json={"state": state}, headers={"X-CSRF-Token": token})
            document = pymupdf.open(stream=response.data, filetype="pdf")
            self.assertNotIn("Hello", document[0].get_text())
            document.close()

            bad = self.client.post(url + "/save", json={"state": {"pages": []}}, headers={"X-CSRF-Token": token})
            self.assertEqual(bad.status_code, 400)
            self.assertIn("at least one page", bad.get_json()["error"])
            self.assertEqual(self.client.get("/convert/edit/not-a-real-document-id/page/0/text").status_code, 404)
            self.assertEqual(self.client.get("/convert/edit/not-a-real-document-id").status_code, 302)

            not_pdf = self.client.post(
                "/convert/edit/open",
                data={"_csrf_token": token, "files": (io.BytesIO(b"hello"), "notes.txt")},
                content_type="multipart/form-data",
                headers={"X-Requested-With": "fetch"},
            )
            self.assertEqual(not_pdf.status_code, 400)

    def test_pdf_to_image_returns_png_or_zip(self):
        token = self._csrf()
        response = self.client.post(
            "/convert/pdf2image",
            data={"_csrf_token": token, "image_format": "png", "dpi": "96",
                  "files": (io.BytesIO(self._pdf_bytes(1)), "one.pdf")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data.startswith(b"\x89PNG"))
        response.close()

        response = self.client.post(
            "/convert/pdf2image",
            data={"_csrf_token": token, "image_format": "jpg", "dpi": "96",
                  "files": (io.BytesIO(self._pdf_bytes(3)), "three.pdf")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            self.assertEqual(
                archive.namelist(),
                ["three-page-001.jpg", "three-page-002.jpg", "three-page-003.jpg"],
            )
        response.close()

    def test_pdf_to_word_returns_docx(self):
        response = self.client.post(
            "/convert/pdf2word",
            data={"_csrf_token": self._csrf(), "files": (io.BytesIO(self._pdf_bytes(1)), "letter.pdf")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            self.assertIn(b"Hello page 1", archive.read("word/document.xml"))
        response.close()

    def _thai_pdf_bytes(self, scanned=False):
        import pymupdf

        document = pymupdf.open()
        page = document.new_page()
        page.insert_htmlbox(
            pymupdf.Rect(60, 60, 540, 400),
            "<p><b>รายงานประจำปี Annual Report</b></p>"
            "<p>บริษัทได้ดำเนินโครงการพัฒนาระบบจัดการเอกสาร (Document Management System) สำเร็จตามแผน</p>",
            css="* { font-family: sans-serif; font-size: 18px; } p { margin-bottom: 12px; }",
        )
        if scanned:
            # A picture of the page with no text layer, like a scan.
            picture = page.get_pixmap(dpi=300, colorspace=pymupdf.csGRAY)
            document = pymupdf.open()
            document.new_page().insert_image(pymupdf.Rect(0, 0, 595, 842), pixmap=picture)
        data = document.tobytes()
        document.close()
        return data

    @unittest.skipUnless(Path(THAI_FONTS, "Laksaman-Bold.ttf").exists(), "TLWG Thai fonts not installed")
    def test_pdf_to_word_keeps_thai_fonts_sizes_and_bold(self):
        import pymupdf

        # Laksaman is TLWG's TH Sarabun, the usual font of Thai Word documents.
        document = pymupdf.open()
        page = document.new_page()
        page.insert_font(fontname="lak-b", fontfile=str(Path(THAI_FONTS, "Laksaman-Bold.ttf")))
        page.insert_font(fontname="lak", fontfile=str(Path(THAI_FONTS, "Laksaman.ttf")))
        page.insert_text((72, 90), "รายงานประจำปี Annual Report", fontname="lak-b", fontsize=20)
        page.insert_text((72, 130), "บริษัทได้ดำเนินโครงการพัฒนาระบบ (Document System)", fontname="lak", fontsize=16)
        data = document.tobytes()
        document.close()

        response = self.client.post(
            "/convert/pdf2word",
            data={"_csrf_token": self._csrf(), "files": (io.BytesIO(data), "รายงาน.pdf")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            xml = archive.read("word/document.xml").decode()
            styles = archive.read("word/styles.xml").decode()
        response.close()
        self.assertIn("ดำเนินโครงการ", xml)
        runs = [run for run in xml.split("<w:r>") if "รายงาน" in run or "บริษัท" in run]
        self.assertEqual(len(runs), 2)
        for run in runs:
            self.assertIn('w:cs="TH Sarabun New"', run)  # Not the Linux name, Laksaman.
            self.assertIn("<w:szCs ", run)
            self.assertIn('w:bidi="th-TH"', run)
        title = [run for run in runs if "รายงาน" in run][0]
        self.assertIn("<w:b/><w:bCs/>", title)  # Word needs bCs to show Thai in bold.
        self.assertIn('<w:sz w:val="40"/><w:szCs w:val="40"/>', title)
        self.assertIn('w:cs="TH Sarabun New"', styles)

    def test_thai_text_repairs_and_font_names(self):
        import pdf_word

        # Private-use glyphs from legacy Thai fonts, a split sara am, and a tone before its vowel.
        self.assertEqual(pdf_word.normalize_thai("\u0e1b\uf70b"), "\u0e1b\u0e49")
        self.assertEqual(pdf_word.normalize_thai("\u0e19\uf70f"), "\u0e19\u0e0d")
        self.assertEqual(pdf_word.normalize_thai("ดํา"), "ดำ")
        self.assertEqual(pdf_word.normalize_thai("น\u0e4d\u0e49\u0e32"), "น\u0e49\u0e33")
        self.assertEqual(pdf_word.normalize_thai("ก\u0e48\u0e34"), "ก\u0e34\u0e48")
        self.assertEqual(pdf_word.normalize_thai("Hello ไทย"), "Hello ไทย")
        self.assertEqual(pdf_word.word_font("BAAAAA+Laksaman-Bold"), "TH Sarabun New")
        self.assertEqual(pdf_word.word_font("THSarabunPSK"), "TH SarabunPSK")
        self.assertEqual(pdf_word.word_font("AngsanaNew-Italic"), "Angsana New")
        self.assertEqual(pdf_word.word_font("Calibri"), "Calibri")
        self.assertEqual(pdf_word.word_font("Nimbus Sans Bold"), "Arial")

    @unittest.skipUnless(__import__("pdf_word").ocr_available(), "Tesseract with Thai not installed")
    def test_scanned_pdf_to_word_uses_thai_ocr(self):
        import difflib
        import docx

        response = self.client.post(
            "/convert/pdf2word",
            data={"_csrf_token": self._csrf(), "files": (io.BytesIO(self._thai_pdf_bytes(scanned=True)), "scan.pdf")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("OCR", unquote(response.headers["X-Convert-Note"]))
        text = "\n".join(p.text for p in docx.Document(io.BytesIO(response.data)).paragraphs)
        response.close()
        expected = "รายงานประจำปี Annual Report บริษัทได้ดำเนินโครงการพัฒนาระบบจัดการเอกสาร (Document Management System) สำเร็จตามแผน"
        ratio = difflib.SequenceMatcher(None, expected.replace(" ", ""), text.replace(" ", "").replace("\n", "")).ratio()
        self.assertGreater(ratio, 0.9, text)
        self.assertIn("Document Management System", text)

    @unittest.skipUnless(Path(THAI_FONTS, "Laksaman.ttf").exists(), "TLWG Thai fonts not installed")
    def test_scanned_thai_form_keeps_marks_fill_lines_and_tick_boxes(self):
        import docx
        import pymupdf

        # A scanned government-style form: fill-in dots, tick boxes, circles, and Thai
        # with tone marks above the letters.
        document = pymupdf.open()
        page = document.new_page()
        page.insert_htmlbox(
            pymupdf.Rect(90, 60, 560, 400),
            "<p>เรียน นายทะเบียน</p>"
            f"<p>ข้าพเจ้า{'.' * 50}สัญชาติ{'.' * 20}</p>"
            "<p>ขอรับใบอนุญาต</p>"
            "<p>รถยนต์ส่วนบุคคลชั่วคราว</p>",
            css="* { font-family: sans-serif; font-size: 16px; } p { margin: 0 0 14px; }",
        )
        # Lines 3 and 4 start with a circle and a tick box.
        page.draw_circle((78, 142), 5, width=0.8)
        page.draw_rect(pymupdf.Rect(73, 169, 83, 179), width=0.8)
        picture = page.get_pixmap(dpi=300, colorspace=pymupdf.csGRAY)
        scan = pymupdf.open()
        scan.new_page().insert_image(pymupdf.Rect(0, 0, 595, 842), pixmap=picture)
        data = scan.tobytes()

        response = self.client.post(
            "/convert/pdf2word",
            data={"_csrf_token": self._csrf(), "files": (io.BytesIO(data), "form.pdf"), "ocr": "on"},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)
        text = "\n".join(p.text for p in docx.Document(io.BytesIO(response.data)).paragraphs)
        response.close()
        for word in ("เรียน", "นายทะเบียน", "ข้าพเจ้า", "สัญชาติ", "ขอรับใบอนุญาต", "ชั่วคราว"):
            self.assertIn(word, text.replace(" ", ""))
        self.assertIsNone(re.search(r"\d{5,}", text), text)  # Dots are not read as digits...
        self.assertIn(".....", text)  # ...they come back as a fill line.
        self.assertIn("\u2610", text)
        self.assertIn("\u25cb", text)

    def test_polish_fixes_pdf2docx_links_and_table_widths(self):
        import pdf_word

        w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
        xml = f"""<w:document {w}><w:body>
          <w:p><w:r><w:rPr><w:u w:val="single"/></w:rPr><w:hyperlink><w:r><w:t>Linked text</w:t></w:r></w:hyperlink></w:r></w:p>
          <w:tbl><w:tblPr><w:tblInd w:w="1.9999999999998863" w:type="dxa"/></w:tblPr>
            <w:tblGrid><w:gridCol w:w="2418"/><w:gridCol w:w="2418"/></w:tblGrid>
            <w:tr><w:tc><w:tcPr><w:tcW w:type="dxa" w:w="720"/></w:tcPr></w:tc>
                  <w:tc><w:tcPr><w:tcW w:type="dxa" w:w="5668"/></w:tcPr></w:tc></w:tr></w:tbl>
        </w:body></w:document>""".encode()
        polished = pdf_word._polish_part(xml)[0].decode()
        # The link sits in the paragraph (not inside a run) and keeps the underline.
        self.assertRegex(polished, r"<w:p><w:hyperlink><w:r><w:rPr><w:u w:val=\"single\"/></w:rPr><w:t>Linked text")
        self.assertIn('<w:gridCol w:w="720"/><w:gridCol w:w="5668"/>', polished)
        self.assertIn('<w:tblInd w:w="2"', polished)

    def test_pdf_to_word_keeps_vector_charts_as_pictures(self):
        import docx
        import pymupdf

        document = pymupdf.open()
        page = document.new_page()
        page.insert_text((72, 80), "Sales by quarter", fontsize=14)
        colours = [(0, 0.27, 0.53), (1, 0.26, 0.05), (1, 0.83, 0.13)]
        # Axes and grid lines, like a chart exported from a spreadsheet.
        for y in range(160, 301, 35):
            page.draw_line((110, y), (440, y), color=(0.7, 0.7, 0.7), width=0.5)
        page.draw_line((110, 160), (110, 300), color=(0.7, 0.7, 0.7), width=0.5)
        for quarter in range(4):
            for series, colour in enumerate(colours):
                x = 120 + quarter * 80 + series * 18
                height = 40 + (quarter * 3 + series * 7) % 9 * 10
                page.draw_rect(pymupdf.Rect(x, 300 - height, x + 16, 300), color=None, fill=colour)
            page.insert_text((120 + quarter * 80, 316), f"Q{quarter + 1}", fontsize=9)
        for series in range(3):
            page.insert_text((470, 200 + series * 14), f"Series {series + 1}", fontsize=9)
        page.insert_text((72, 360), "The chart above shows each quarter.", fontsize=11)
        data = document.tobytes()

        response = self.client.post(
            "/convert/pdf2word",
            data={"_csrf_token": self._csrf(), "files": (io.BytesIO(data), "chart.pdf")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)
        result = docx.Document(io.BytesIO(response.data))
        response.close()
        text = "\n".join(p.text for p in result.paragraphs)
        self.assertEqual(len(result.inline_shapes), 1)  # The chart, labels and all, as one picture.
        self.assertIn("Sales by quarter", text)
        self.assertIn("The chart above shows each quarter.", text)
        self.assertNotIn("Series 1", text)  # Its labels are in the picture, not scattered as text.

    @unittest.skipUnless(shutil.which("soffice") or shutil.which("libreoffice"), "LibreOffice not installed")
    def test_word_to_pdf_uses_libreoffice(self):
        import docx

        buffer = io.BytesIO()
        document = docx.Document()
        document.add_paragraph("Converted by LibreOffice")
        document.save(buffer)
        buffer.seek(0)
        response = self.client.post(
            "/convert/word2pdf",
            data={"_csrf_token": self._csrf(), "files": (buffer, "report.docx")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200, response.data[:200])
        self.assertTrue(response.data.startswith(b"%PDF"))
        self.assertIn("report.pdf", response.headers["Content-Disposition"])
        response.close()

    def test_convert_rejects_wrong_type_and_bad_files(self):
        token = self._csrf()
        response = self.client.post(
            "/convert/pdf2word",
            data={"_csrf_token": token, "files": (io.BytesIO(b"hello"), "notes.txt")},
            content_type="multipart/form-data",
            headers={"X-Requested-With": "fetch"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("not a supported file type", response.get_json()["error"])

        response = self.client.post(
            "/convert/pdf2image",
            data={"_csrf_token": token, "files": (io.BytesIO(b"not a pdf"), "fake.pdf")},
            content_type="multipart/form-data",
            headers={"X-Requested-With": "fetch"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("not a readable PDF", response.get_json()["error"])

        response = self.client.post(
            "/convert/image2pdf",
            data={"_csrf_token": token},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn(b"Choose a file to convert", self.client.get("/convert").data)
        self.assertEqual(self.client.post("/convert/nope", data={"_csrf_token": token}).status_code, 404)

    def test_convert_log_is_per_user_and_deletable(self):
        token = self._csrf()
        users = self.app.extensions["users"]
        member_id = users.add("member", "member-password")
        log = self.app.extensions["convert_log"]

        # A successful and a failed conversion are both logged for the person who ran them.
        with self.client.session_transaction() as sess:
            sess["user_id"] = member_id
        response = self.client.post(
            "/convert/image2pdf",
            data={"_csrf_token": token, "files": (io.BytesIO(self._png_bytes("red")), "รูปภาพ.png")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)
        response.close()
        self.client.post(
            "/convert/pdf2image",
            data={"_csrf_token": token, "files": (io.BytesIO(b"not a pdf"), "fake.pdf")},
            content_type="multipart/form-data",
            headers={"X-Requested-With": "fetch"},
        )
        entries = log.entries(member_id)
        self.assertEqual([entry["tool"] for entry in entries], ["pdf2image", "image2pdf"])
        self.assertEqual(entries[0]["ok"], 0)
        self.assertEqual(entries[1]["files"], "รูปภาพ.png")
        self.assertEqual(entries[1]["result"], "รูปภาพ.pdf")
        log.add(self.admin_id, "word2pdf", ["admin-report.docx"], "admin-report.pdf")
        admin_entry = log.entries(self.admin_id)[0]["id"]

        # People see only their own log and can't delete other people's entries.
        page = self.client.get("/convert").get_data(as_text=True)
        self.assertIn("Conversion log", page)
        self.assertIn("รูปภาพ.png", page)
        self.assertNotIn("admin-report", page)
        self.assertIn("fake.pdf", self.client.get("/convert/log").get_data(as_text=True))
        response = self.client.post(f"/convert/log/{admin_entry}/delete", data={"_csrf_token": token})
        self.assertEqual(response.status_code, 404)
        response = self.client.post(f"/convert/log/{entries[0]['id']}/delete", data={"_csrf_token": token})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(len(log.entries(member_id)), 1)
        # Clearing as a member clears only their own log, even when asking for everyone's.
        self.client.post("/convert/log/clear", data={"_csrf_token": token, "scope": "all"})
        self.assertEqual(log.entries(member_id), [])
        self.assertEqual(len(log.entries(self.admin_id)), 1)

        # Administrators see everyone's conversions and can delete any of them.
        log.add(member_id, "pdf2word", ["member-scan.pdf"], "member-scan.docx")
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.admin_id
        page = self.client.get("/convert").get_data(as_text=True)
        self.assertIn("member-scan.pdf", page)
        self.assertIn("admin-report.docx", page)
        member_entry = log.entries(member_id)[0]["id"]
        self.client.post(f"/convert/log/{member_entry}/delete", data={"_csrf_token": token})
        self.assertEqual(log.entries(member_id), [])
        self.client.post("/convert/log/clear", data={"_csrf_token": token, "scope": "all"})
        self.assertEqual(log.entries(), [])
        self.assertIn("No conversions yet", self.client.get("/convert").get_data(as_text=True))


    def _fake_reply(self, *chunks):
        def stream_reply(history, for_telegram=False):
            self.seen_history = history
            for chunk in chunks:
                yield {"type": "text", "text": chunk}
            return "".join(chunks)

        return stream_reply

    def _stream_lines(self, response):
        return [json.loads(line) for line in response.get_data(as_text=True).splitlines() if line]

    def test_chat_streams_reply_and_saves_conversation(self):
        token = self._csrf()
        self.assertEqual(self.client.get("/chat").status_code, 200)
        with mock.patch.object(chat, "stream_reply", self._fake_reply("สวัสดี", " ครับ")):
            response = self.client.post("/chat/send", data={"_csrf_token": token, "message": "Hello Claude"})
            events = self._stream_lines(response)
        self.assertEqual(response.mimetype, "application/x-ndjson")
        self.assertEqual([event["type"] for event in events], ["start", "text", "text", "done"])
        self.assertEqual(self.seen_history[-1]["content"], "Hello Claude")
        conversation_id = events[0]["conversation_id"]

        page = self.client.get(f"/chat?id={conversation_id}").get_data(as_text=True)
        self.assertIn("Hello Claude", page)
        self.assertIn("สวัสดี ครับ", page)

        with mock.patch.object(chat, "stream_reply", self._fake_reply("Second answer")):
            events = self._stream_lines(self.client.post(
                "/chat/send",
                data={"_csrf_token": token, "message": "Again", "conversation_id": conversation_id},
            ))
        self.assertEqual(events[0]["conversation_id"], conversation_id)
        self.assertEqual(
            [item["content"] for item in self.seen_history],
            ["Hello Claude", "สวัสดี ครับ", "Again"],
        )

        after = events[0]["user_message_id"]
        data = self.client.get(f"/chat/{conversation_id}/messages?after={after}").get_json()
        self.assertEqual([item["content"] for item in data["messages"]], ["Second answer"])

        response = self.client.post(f"/chat/{conversation_id}/delete", data={"_csrf_token": token})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get(f"/chat?id={conversation_id}").status_code, 404)

    def test_chat_reports_errors_without_saving_a_reply(self):
        token = self._csrf()

        def failing(history, for_telegram=False):
            raise chat.ChatError("Chat is not set up yet")
            yield

        with mock.patch.object(chat, "stream_reply", failing):
            events = self._stream_lines(self.client.post("/chat/send", data={"_csrf_token": token, "message": "Hi"}))
        self.assertEqual(events[-1], {"type": "error", "error": "Chat is not set up yet"})
        store = self.app.extensions["chat"]
        self.assertEqual([m["role"] for m in store.messages(events[0]["conversation_id"])], ["user"])

        response = self.client.post("/chat/send", data={"_csrf_token": token, "message": "  "})
        self.assertEqual(response.status_code, 400)

    def test_telegram_bot_only_answers_allowed_users(self):
        store = self.app.extensions["chat"]
        telegram = mock.Mock()
        message = {"chat": {"id": 5}, "from": {"id": 999}, "text": "hello"}

        telegram_bot.handle(telegram, store, message, allowed={111})
        self.assertIn("999", telegram.send.call_args[0][1])
        self.assertEqual(store.conversations(self.admin_id), [])

        # Telegram chats belong to the first administrator and are logged.
        message["from"]["id"] = 111
        activity = self.app.extensions["activity"]
        with mock.patch.object(chat, "complete_reply", return_value="Hi from Claude") as complete:
            telegram_bot.handle(telegram, store, message, allowed={111}, activity=activity)
        self.assertTrue(complete.call_args.kwargs["for_telegram"])
        telegram.send.assert_called_with(5, "Hi from Claude")
        conversation_id = store.latest_id(self.admin_id)
        self.assertEqual(activity.search()[0]["action"], "sent a message from Telegram")
        self.assertEqual(
            [(m["role"], m["source"]) for m in store.messages(conversation_id)],
            [("user", "telegram"), ("assistant", "telegram")],
        )

        telegram_bot.handle(telegram, store, {**message, "text": "/new"}, allowed={111})
        self.assertNotEqual(store.latest_id(self.admin_id), conversation_id)

    def test_settings_page_saves_values_and_hides_secrets(self):
        token = self._csrf()
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(self.client.get("/settings").status_code, 200)
            self.assertEqual(self.client.get("/settings/ai").status_code, 200)
            response = self.client.post("/settings/ai", data={
                "_csrf_token": token,
                "ANTHROPIC_API_KEY": "test-anthropic-key-0123456789WXYZ",
                "CHAT_MODEL": "claude-sonnet-5-5",
                "CHAT_EFFORT": "high",
                "CHAT_INSTRUCTIONS": "Always reply in Thai.",
            })
            self.assertEqual(response.status_code, 303)
            self.client.post("/settings/telegram", data={"_csrf_token": token, "TELEGRAM_ALLOWED_USERS": "111, 222"})
            self.assertEqual(chat.setting("CHAT_MODEL"), "claude-sonnet-5-5")
            self.assertEqual(chat.setting("CHAT_EFFORT"), "high")
            self.assertTrue(chat.is_configured())
            self.assertIn("Always reply in Thai.", chat.system_prompt())
            self.assertEqual(telegram_bot.allowed_users(), {111, 222})

            page = self.client.get("/settings/ai").get_data(as_text=True)
            self.assertNotIn("test-anthropic-key-0123456789WXYZ", page)
            self.assertIn("…WXYZ", page)

            # A blank secret keeps the saved one; defaults are not pinned.
            self.client.post("/settings/ai", data={
                "_csrf_token": token, "CHAT_MODEL": "claude-opus-5-5", "CHAT_EFFORT": "high",
            })
            self.client.post("/settings/telegram", data={"_csrf_token": token, "TELEGRAM_ALLOWED_USERS": ""})
            settings = self.app.extensions["settings"].all()
            self.assertEqual(settings["ANTHROPIC_API_KEY"], "test-anthropic-key-0123456789WXYZ")
            self.assertNotIn("CHAT_MODEL", settings)
            self.assertNotIn("TELEGRAM_ALLOWED_USERS", settings)

            self.client.post("/settings/ai", data={
                "_csrf_token": token, "CHAT_PROVIDER": "anthropic", "CHAT_EFFORT": "high",
                "clear_ANTHROPIC_API_KEY": "1",
            })
            self.assertFalse(chat.is_configured())

    def test_settings_rejects_invalid_values_without_saving(self):
        token = self._csrf()
        with mock.patch.dict("os.environ", {}, clear=True):
            response = self.client.post("/settings/ai", data={
                "_csrf_token": token,
                "CHAT_EFFORT": "extreme",
                "CHAT_MODEL": "claude-haiku-5-5",
            })
            self.assertEqual(response.status_code, 400)
            page = response.get_data(as_text=True)
            self.assertIn("Choose low, medium, or high.", page)
            self.assertIn('value="claude-haiku-5-5"', page)
            response = self.client.post("/settings/telegram", data={"_csrf_token": token, "TELEGRAM_ALLOWED_USERS": "me"})
            self.assertIn("numeric Telegram user IDs", response.get_data(as_text=True))
            self.assertEqual(self.app.extensions["settings"].all(), {})

            bot_token = "123456:" + "A" * 35
            with mock.patch("app.verify_telegram_token", side_effect=ValueError("Telegram did not accept it.")):
                response = self.client.post("/settings/telegram", data={"_csrf_token": token, "TELEGRAM_BOT_TOKEN": bot_token})
            self.assertEqual(response.status_code, 400)
            with mock.patch("app.verify_telegram_token", return_value="netappz_bot"):
                response = self.client.post("/settings/telegram", data={"_csrf_token": token, "TELEGRAM_BOT_TOKEN": bot_token})
            self.assertEqual(response.status_code, 303)
            self.assertEqual(chat.setting("TELEGRAM_BOT_TOKEN"), bot_token)

    def test_saved_settings_override_environment(self):
        with mock.patch.dict("os.environ", {"CHAT_MODEL": "env-model", "TELEGRAM_ALLOWED_USERS": "5"}):
            self.assertEqual(chat.setting("CHAT_MODEL"), "env-model")
            self.app.extensions["settings"].save({"CHAT_MODEL": "claude-haiku-5-5", "TELEGRAM_ALLOWED_USERS": ""})
            self.assertEqual(chat.setting("CHAT_MODEL"), "claude-haiku-5-5")
            self.assertEqual(telegram_bot.allowed_users(), set())
            self.app.extensions["settings"].save({"CHAT_MODEL": None})
            self.assertEqual(chat.setting("CHAT_MODEL"), "env-model")

    def test_telegram_messages_get_plain_text_instruction(self):
        messages = chat.to_api_messages([{"role": "user", "content": "hi"}], for_telegram=True)
        self.assertEqual(messages[-1]["role"], "system")
        self.assertEqual(chat.to_api_messages([{"role": "user", "content": "hi"}]), [{"role": "user", "content": "hi"}])

    def test_think_filter_drops_reasoning_split_across_chunks(self):
        think = chat.ThinkFilter()
        chunks = ["<thi", "nk>plan the", " answer</th", "ink>\n\nHel", "lo <", "b>there</b>"]
        text = "".join(think.feed(chunk) for chunk in chunks) + think.flush()
        self.assertEqual(text, "Hello <b>there</b>")

    def test_local_provider_streams_openai_compatible_reply(self):
        self.app.extensions["settings"].save({
            "CHAT_PROVIDER": "local", "LOCAL_AI_URL": "https://ai.example.test/api/v1/",
            "LOCAL_AI_API_KEY": "sk-local-123",
        })
        events = [
            {"choices": [{"delta": {"content": "<think>hmm</think>สวัส"}}]},
            {"choices": [{"delta": {"content": "ดี"}, "finish_reason": "stop"}]},
        ]
        body = [f"data: {json.dumps(e)}\n".encode() for e in events] + [b"data: [DONE]\n"]

        class Response(list):
            headers = {"Content-Type": "text/event-stream"}
            def __enter__(self): return self
            def __exit__(self, *args): return False

        with mock.patch("urllib.request.urlopen", return_value=Response(body)) as urlopen:
            reply = chat.complete_reply([{"role": "user", "content": "hi"}], for_telegram=True)
        self.assertEqual(reply, "สวัสดี")
        request = urlopen.call_args[0][0]
        self.assertEqual(request.full_url, "https://ai.example.test/api/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer sk-local-123")
        self.assertNotIn("Python-urllib", request.get_header("User-agent"))
        sent = json.loads(request.data)
        self.assertEqual(sent["model"], chat.DEFAULT_LOCAL_MODEL)
        self.assertEqual(sent["messages"][0]["role"], "system")
        self.assertIn("Telegram", sent["messages"][0]["content"])
        self.assertEqual(sent["messages"][-1], {"role": "user", "content": "hi"})

    def test_settings_validate_local_ai_fields(self):
        token = self._csrf()
        response = self.client.post("/settings/ai", data={
            "_csrf_token": token, "CHAT_PROVIDER": "local", "LOCAL_AI_URL": "ai.example.test",
            "CHAT_EFFORT": "medium",
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("http:// or https://", response.get_data(as_text=True))
        response = self.client.post("/settings/ai", data={
            "_csrf_token": token, "CHAT_PROVIDER": "local", "LOCAL_AI_URL": chat.DEFAULT_LOCAL_URL,
            "LOCAL_AI_MODEL": chat.DEFAULT_LOCAL_MODEL, "CHAT_EFFORT": "medium",
        })
        self.assertEqual(response.status_code, 303)
        self.assertEqual(chat.provider(), "local")
        self.assertTrue(chat.is_configured())
        self.assertEqual(chat.assistant_name(), chat.DEFAULT_LOCAL_MODEL)

    def test_settings_add_local_model_and_use_it(self):
        token = self._csrf()
        page = self.client.get("/settings/ai").get_data(as_text=True)
        self.assertIn('<option value="qwen3/qwen3.8-27b"', page)
        response = self.client.post("/settings/ai", data={
            "_csrf_token": token, "CHAT_EFFORT": "medium", "LOCAL_AI_MODEL": "qwen3/qwen3.8-27b",
            "LOCAL_AI_MODELS": "qwen3/qwen3.6-35b\r\nqwen3/qwen3.8-27b\r\ngemma4:12b\r\n",
        })
        self.assertEqual(response.status_code, 303)
        self.assertEqual(chat.local_models(), ["qwen3/qwen3.6-35b", "qwen3/qwen3.8-27b", "gemma4:12b"])
        self.assertEqual(chat.assistant_name(), "qwen3/qwen3.8-27b")
        self.assertIn('<option value="gemma4:12b"', self.client.get("/settings/ai").get_data(as_text=True))

        # The model to use must be in the list.
        response = self.client.post("/settings/ai", data={
            "_csrf_token": token, "CHAT_EFFORT": "medium", "LOCAL_AI_MODEL": "gemma4:12b",
            "LOCAL_AI_MODELS": "qwen3/qwen3.6-35b",
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("Choose a model from the Models list.", response.get_data(as_text=True))

    def test_settings_show_telegram_bot_status(self):
        settings = self.app.extensions["settings"]
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertIn("No bot token is set", self.client.get("/settings/telegram").get_data(as_text=True))
            settings.save({"TELEGRAM_BOT_TOKEN": "123456:" + "a" * 35})
            self.assertIn("Waiting for the bot to connect", self.client.get("/settings/telegram").get_data(as_text=True))

            error = telegram_bot.urllib.error.HTTPError("url", 409, "Conflict", {}, None)
            telegram_bot.StatusReporter(settings)(*telegram_bot.polling_problem(error))
            page = self.client.get("/settings/telegram").get_data(as_text=True)
            self.assertIn("settings-status is-conflict", page)
            self.assertIn("Claude Code Telegram plugin", page)

            telegram_bot.StatusReporter(settings)("running", "Connected as @netappz_bot.")
            self.assertIn("Connected as @netappz_bot.", self.client.get("/settings/telegram").get_data(as_text=True))

    CODE_REPLY = (
        "Here you go:\n\n```python app.py\nprint('hi')\n```\n\n"
        "```html\n<!-- templates/index.html -->\n<h1>Hi</h1>\n```\n\n"
        "```bash\npip install flask\n```\n\n```python ../../etc/passwd\nx = 1\n```\n\n"
        "```python app.py\nprint('again')\n```\n"
    )

    def test_code_blocks_become_named_files(self):
        files = chat.code_files(self.CODE_REPLY)
        self.assertEqual(
            [name for name, _ in files],
            ["app.py", "templates/index.html", "snippet-3.sh", "snippet-4.py", "app-2.py"],
        )
        self.assertEqual(files[0][1], "print('hi')\n")
        self.assertEqual(chat.code_files("No code here, just `inline`."), [])

    def test_chat_reply_code_downloads_as_zip(self):
        store = self.app.extensions["chat"]
        conversation_id = store.create(self.admin_id)
        message_id = store.add(conversation_id, "assistant", self.CODE_REPLY, "web")
        response = self.client.get(f"/chat/message/{message_id}/code.zip")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/zip")
        archive = zipfile.ZipFile(io.BytesIO(response.data))
        self.assertIn("templates/index.html", archive.namelist())
        self.assertEqual(archive.read("app.py"), b"print('hi')\n")

        plain_id = store.add(conversation_id, "assistant", "No code.", "web")
        self.assertEqual(self.client.get(f"/chat/message/{plain_id}/code.zip").status_code, 404)
        user_id = store.add(conversation_id, "user", self.CODE_REPLY, "web")
        self.assertEqual(self.client.get(f"/chat/message/{user_id}/code.zip").status_code, 404)

    def test_telegram_reply_with_code_sends_zip(self):
        store = self.app.extensions["chat"]
        telegram = mock.Mock()
        message = {"chat": {"id": 5}, "from": {"id": 111}, "text": "write code"}
        with mock.patch.object(chat, "complete_reply", return_value=self.CODE_REPLY):
            telegram_bot.handle(telegram, store, message, allowed={111})
        chat_id, filename, data = telegram.send_document.call_args[0]
        self.assertEqual(chat_id, 5)
        self.assertTrue(filename.endswith(".zip"))
        self.assertIn("app.py", zipfile.ZipFile(io.BytesIO(data)).namelist())
        self.assertIn("app.py", telegram.send_document.call_args.kwargs["caption"])

    def test_note_editor_offers_links(self):
        page = self.client.get("/notes?new=1").get_data(as_text=True)
        self.assertIn('id="notes-insert-link"', page)
        self.assertIn('id="notes-body-links"', page)
        self.assertIn('id="notes-link-open"', page)

    def test_notes_can_be_pinned_to_the_top(self):
        token = self._csrf()
        for title in ("Older", "Newer"):
            self.client.post("/notes/create", data={"_csrf_token": token, "title": title, "content": "Body"})
        connection = sqlite3.connect(self.app.config["NOTES_DATABASE"])
        try:
            older_id = connection.execute("SELECT id FROM notes WHERE title = 'Older'").fetchone()[0]
            before = connection.execute("SELECT updated_at FROM notes WHERE id = ?", (older_id,)).fetchone()[0]
        finally:
            connection.close()
        page = self.client.get("/notes").get_data(as_text=True)
        self.assertLess(page.index(">Newer<"), page.index(">Older<"))
        self.assertNotIn('data-group="pinned"', page)

        response = self.client.post(f"/notes/{older_id}/pin", data={"_csrf_token": token, "pinned": "1", "q": "Old"})
        self.assertEqual(response.status_code, 302)
        self.assertIn(f"id={older_id}", response.headers["Location"])
        self.assertIn("q=Old", response.headers["Location"])
        page = self.client.get(f"/notes?id={older_id}").get_data(as_text=True)
        self.assertIn('data-group="pinned"', page)
        self.assertIn('data-group="notes"', page)
        self.assertLess(page.index(">Older<"), page.index(">Newer<"))
        self.assertIn("Unpin note", page)
        connection = sqlite3.connect(self.app.config["NOTES_DATABASE"])
        try:
            row = connection.execute("SELECT pinned, updated_at FROM notes WHERE id = ?", (older_id,)).fetchone()
        finally:
            connection.close()
        self.assertEqual(row, (1, before))  # pinning is not an edit

        self.client.post(f"/notes/{older_id}/pin", data={"_csrf_token": token, "pinned": "0"})
        page = self.client.get("/notes").get_data(as_text=True)
        self.assertNotIn('data-group="pinned"', page)
        self.assertLess(page.index(">Newer<"), page.index(">Older<"))

        # Only the owner can pin a note.
        other_id = self.app.extensions["users"].add("someone", "someone-password")
        with self.client.session_transaction() as sess:
            sess["user_id"] = other_id
        response = self.client.post(f"/notes/{older_id}/pin", data={"_csrf_token": token, "pinned": "1"})
        self.assertEqual(response.status_code, 404)
        connection = sqlite3.connect(self.app.config["NOTES_DATABASE"])
        try:
            self.assertEqual(connection.execute("SELECT pinned FROM notes WHERE id = ?", (older_id,)).fetchone()[0], 0)
        finally:
            connection.close()

    def test_note_editor_can_find_text_in_the_note(self):
        token = self._csrf()
        self.client.post("/notes/create", data={"_csrf_token": token, "title": "Trip", "content": "Pack the tent"})
        page = self.client.get("/notes?new=1").get_data(as_text=True)
        self.assertIn('id="notes-find-toggle"', page)
        self.assertIn('id="notes-find-input"', page)
        # A list search carries into the note's find box so the words are highlighted.
        page = self.client.get("/notes?q=tent").get_data(as_text=True)
        self.assertIn('id="notes-find-input" type="search" placeholder="Find in note" aria-label="Find in note"', page)
        self.assertRegex(page, r'id="notes-find-input"[^>]*value="tent"')

    def test_healthz_reports_ok(self):
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"status": "ok"})

    def test_notes_and_chats_are_private_and_logged(self):
        token = self._csrf()
        users = self.app.extensions["users"]
        member_id = users.add("member", "member-password")
        self.client.post("/notes/create", data={"_csrf_token": token, "title": "Admin plan", "content": "Secret"})

        # Another person sees none of the administrator's notes or chats, and can't open or change them.
        store = self.app.extensions["chat"]
        admin_chat = store.create(self.admin_id)
        store.add(admin_chat, "user", "Admin question", "web")
        with sqlite3.connect(self.app.config["NOTES_DATABASE"]) as db:
            note_id = db.execute("SELECT id FROM notes WHERE title = 'Admin plan'").fetchone()[0]
        with self.client.session_transaction() as sess:
            sess["user_id"] = member_id
        page = self.client.get("/notes").get_data(as_text=True)
        self.assertNotIn("Admin plan", page)
        self.assertIn("only you can see them", page)
        self.assertNotIn("Secret", self.client.get(f"/notes?id={note_id}").get_data(as_text=True))
        self.assertEqual(self.client.post(f"/notes/{note_id}/edit", data={
            "_csrf_token": token, "title": "Taken", "content": "Over"}).status_code, 404)
        self.assertEqual(self.client.post(f"/notes/{note_id}/delete", data={"_csrf_token": token}).status_code, 404)
        self.assertNotIn("Admin question", self.client.get("/chat").get_data(as_text=True))
        self.assertEqual(self.client.get(f"/chat?id={admin_chat}").status_code, 404)
        self.assertEqual(self.client.get(f"/chat/{admin_chat}/messages").status_code, 404)
        self.assertEqual(self.client.post(f"/chat/{admin_chat}/delete", data={"_csrf_token": token}).status_code, 404)
        self.client.post("/notes/create", data={"_csrf_token": token, "title": "Member list", "content": "Mine"})
        self.assertIn("Member list", self.client.get("/notes").get_data(as_text=True))
        self.assertEqual(self.client.get(f"/notes?id={note_id}").status_code, 404)
        self.assertEqual(self.client.get("/settings/activity").status_code, 403)

        # The administrator's notes are untouched, and the log shows who did what.
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.admin_id
        page = self.client.get("/notes").get_data(as_text=True)
        self.assertIn("Admin plan", page)
        self.assertNotIn("Member list", page)
        page = self.client.get("/settings/activity").get_data(as_text=True)
        self.assertIn("Member list", page)
        self.assertIn("Admin plan", page)
        page = self.client.get("/settings/activity?user=member").get_data(as_text=True)
        self.assertIn("Member list", page)
        self.assertNotIn("Admin plan", page)
        self.assertIn("Nothing matches",
                      self.client.get("/settings/activity?area=chat&q=zzz").get_data(as_text=True))

        # Deleting the account removes its notes and chats but keeps its log entries.
        member_chat = store.create(member_id)
        self.client.post(f"/settings/users/{member_id}/delete", data={"_csrf_token": token})
        self.assertEqual(store.conversations(member_id), [])
        self.assertIsNone(store.conversation(member_chat, member_id))
        with sqlite3.connect(self.app.config["NOTES_DATABASE"]) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM notes WHERE title = 'Member list'").fetchone()[0], 0)
        self.assertIn("member", self.app.extensions["activity"].usernames())

    def test_system_checks_and_backups_in_settings(self):
        import os
        import time

        backups = Path(self.temp_dir.name) / "backups"
        backups.mkdir()
        app = create_app({
            "TESTING": True, "FILES_ROOT": str(self.root), "SECRET_KEY": "test-secret",
            "NOTES_DATABASE": str(Path(self.temp_dir.name) / "notes.sqlite3"),
            "FEED_MEDIA": str(Path(self.temp_dir.name) / "feed"), "BACKUP_DIR": str(backups),
        })
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = self.admin_id
        client.get("/")
        with client.session_transaction() as sess:
            token = sess["_csrf_token"]
        notifications = app.extensions["notifications"]

        # No backups yet is a problem, and administrators are told once.
        page = client.get("/settings/system").get_data(as_text=True)
        self.assertIn("There are no backups yet", page)
        runner = app.test_cli_runner()
        self.assertIn("Problem  Backups", runner.invoke(args=["system-check"]).output)
        runner.invoke(args=["system-check"])
        alerts = [n for n in notifications.for_user(self.admin_id) if n["kind"] == "system"]
        self.assertEqual(len(alerts), 1)
        self.assertIn("Backups", alerts[0]["message"])

        # A fresh dump is OK again, and listed for download; a stale one is late.
        dump = backups / "notes-20261014-032151Z.dump"
        dump.write_bytes(b"PGDMP")
        (backups / "notes.txt").write_text("not a dump")
        runner.invoke(args=["system-check"])
        self.assertIn("Backups is OK again", notifications.for_user(self.admin_id)[0]["message"])
        page = client.get("/settings/system").get_data(as_text=True)
        self.assertIn("notes-20261014-032151Z.dump", page)
        self.assertNotIn("notes.txt", page)
        response = client.get("/settings/system/backups/notes-20261014-032151Z.dump")
        self.assertEqual((response.status_code, response.data), (200, b"PGDMP"))
        response.close()
        for name in ("notes.txt", "..%2Fnotes.sqlite3", "missing.dump"):
            self.assertEqual(client.get(f"/settings/system/backups/{name}").status_code, 404)
        old = time.time() - 9 * 3600
        os.utime(dump, (old, old))
        self.assertIn("The next one is late", client.get("/settings/system").get_data(as_text=True))
        (backups / ".last-error").write_text("2026-10-14 03:00 UTC:\npg_dump: connection refused")
        self.assertIn("The last backup failed", client.get("/settings/system").get_data(as_text=True))

        # Back up now leaves a request for the backup service; the schedule is validated.
        client.post("/settings/system/backup", data={"_csrf_token": token})
        self.assertTrue((backups / ".backup-now").exists())
        self.assertIn("Backing up…", client.get("/settings/system").get_data(as_text=True))
        client.post("/settings/system/schedule", data={"_csrf_token": token, "interval_hours": "0", "keep": "5"})
        self.assertFalse((backups / ".schedule").exists())
        client.post("/settings/system/schedule", data={"_csrf_token": token, "interval_hours": "12", "keep": "14"})
        self.assertEqual((backups / ".schedule").read_text(), "interval_hours=12\nkeep=14\n")
        self.assertIn('value="12"', client.get("/settings/system").get_data(as_text=True))

        # Members can't see or use any of it.
        member_id = app.extensions["users"].add("member", "member-password")
        with client.session_transaction() as sess:
            sess["user_id"] = member_id
        self.assertEqual(client.get("/settings/system").status_code, 403)
        self.assertEqual(client.get("/settings/system/backups/notes-20261014-032151Z.dump").status_code, 403)
        self.assertEqual(client.post("/settings/system/backup", data={"_csrf_token": token}).status_code, 403)

    def test_only_administrators_send_requests_to_claude(self):
        import json
        import app as app_module

        token = self._csrf()
        store = self.app.extensions["requests"]
        users = self.app.extensions["users"]
        request_id = store.add("feature", "Dark mode", "", "member")
        member_id = users.add("member", "member-password")
        self.assertIn('class="request-claude"', self.client.get("/updates").get_data(as_text=True))

        # A member can't send a request to Claude, even by posting the status.
        with self.client.session_transaction() as sess:
            sess["user_id"] = member_id
        self.assertNotIn('class="request-claude"', self.client.get("/updates").get_data(as_text=True))
        self.client.post(f"/updates/requests/{request_id}/status", data={"_csrf_token": token, "status": "queued"})
        self.assertEqual(store.get(request_id)["status"], "open")

        with self.client.session_transaction() as sess:
            sess["user_id"] = self.admin_id
        self.client.post(f"/updates/requests/{request_id}/status", data={"_csrf_token": token, "status": "queued"})
        item = store.get(request_id)
        self.assertEqual((item["status"], item["sent_by"]), ("queued", "admin"))

        # The worker reads the queue and reports back through the CLI.
        runner = self.app.test_cli_runner()
        queued = json.loads(runner.invoke(args=["claude-next"]).output)
        self.assertEqual((queued["id"], queued["title"]), (request_id, "Dark mode"))
        runner.invoke(args=["claude-status", str(request_id), "done", "--note", "Added a dark theme."])
        item = store.get(request_id)
        self.assertEqual((item["status"], item["done_version"], item["note"]),
                         ("done", app_module.APP_VERSION, "Added a dark theme."))
        self.assertIn("Added a dark theme.", self.client.get("/updates").get_data(as_text=True))
        self.assertIn("Dark mode", self.app.extensions["notifications"].for_user(member_id)[0]["message"])
        self.assertEqual(runner.invoke(args=["claude-next"]).output, "")

        # A request sent by someone who is no longer an administrator goes back to Waiting.
        store.set_status(request_id, "queued", sent_by="member")
        self.assertEqual(runner.invoke(args=["claude-next"]).output, "")
        self.assertEqual(store.get(request_id)["status"], "open")

    def test_missions_are_set_submitted_and_approved(self):
        token = self._csrf()
        store = self.app.extensions["missions"]
        users = self.app.extensions["users"]
        notifications = self.app.extensions["notifications"]
        member_id = users.add("member", "member-password")
        other_id = users.add("other", "other-password")

        # Administrators set missions; a title is needed.
        self.client.post("/missions", data={"_csrf_token": token, "title": " ", "details": "Keep me"})
        page = self.client.get("/missions").get_data(as_text=True)
        self.assertIn("Give the mission a short title.", page)
        self.assertIn("Keep me", page)
        response = self.client.post("/missions", data={"_csrf_token": token, "title": "Tidy Projects",
                                                       "details": "Move old files to Archive"})
        self.assertEqual(response.status_code, 303)
        mission_id = store.all()[0]["id"]
        self.assertIn("Tidy Projects", notifications.for_user(member_id)[0]["message"])

        with self.client.session_transaction() as sess:
            sess["user_id"] = member_id
        page = self.client.get("/missions").get_data(as_text=True)
        self.assertIn("Move old files to Archive", page)
        self.assertNotIn("Waiting for approval</h2>", page)

        # A member submits once; a second submission waits for the first to be reviewed.
        self.client.post(f"/missions/{mission_id}/submit", data={"_csrf_token": token, "body": " "})
        self.assertEqual(store.all()[0]["submissions"], [])
        self.client.post(f"/missions/{mission_id}/submit", data={"_csrf_token": token, "body": "Moved 40 files"})
        self.client.post(f"/missions/{mission_id}/submit", data={"_csrf_token": token, "body": "Again"})
        submissions = store.all()[0]["submissions"]
        self.assertEqual([(s["body"], s["status"]) for s in submissions], [("Moved 40 files", "pending")])
        self.assertIn("member submitted work", notifications.for_user(self.admin_id)[0]["message"])
        submission_id = submissions[0]["id"]

        # Members can't review, and others don't see someone else's work.
        self.assertEqual(self.client.post(f"/missions/submissions/{submission_id}/review",
                                          data={"_csrf_token": token, "decision": "approved"}).status_code, 403)
        with self.client.session_transaction() as sess:
            sess["user_id"] = other_id
        self.assertNotIn("Moved 40 files", self.client.get("/missions").get_data(as_text=True))
        self.assertEqual(self.client.post(f"/missions/submissions/{submission_id}/delete",
                                          data={"_csrf_token": token}).status_code, 403)

        # Sending back needs a note; then the member can submit again and it's approved.
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.admin_id
        self.assertIn("Moved 40 files", self.client.get("/missions").get_data(as_text=True))
        self.client.post(f"/missions/submissions/{submission_id}/review",
                         data={"_csrf_token": token, "decision": "returned"})
        self.assertEqual(store.submission(submission_id)["status"], "pending")
        self.client.post(f"/missions/submissions/{submission_id}/review",
                         data={"_csrf_token": token, "decision": "returned", "note": "Archive the PDFs too"})
        self.assertEqual(store.submission(submission_id)["status"], "returned")
        self.assertIn("Archive the PDFs too", notifications.for_user(member_id)[0]["message"])

        with self.client.session_transaction() as sess:
            sess["user_id"] = member_id
        self.assertIn("Archive the PDFs too", self.client.get("/missions").get_data(as_text=True))
        self.client.post(f"/missions/{mission_id}/submit", data={"_csrf_token": token, "body": "PDFs done"})
        second_id = store.all()[0]["submissions"][-1]["id"]
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.admin_id
        self.client.post(f"/missions/submissions/{second_id}/review",
                         data={"_csrf_token": token, "decision": "approved"})
        item = store.submission(second_id)
        self.assertEqual((item["status"], item["reviewer"]), ("approved", "admin"))
        self.assertIn("1 approved", self.client.get("/missions").get_data(as_text=True))

        # Approved work can't be withdrawn by the member; a closed mission takes no new work.
        with self.client.session_transaction() as sess:
            sess["user_id"] = member_id
        self.assertEqual(self.client.post(f"/missions/submissions/{second_id}/delete",
                                          data={"_csrf_token": token}).status_code, 403)
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.admin_id
        self.client.post(f"/missions/{mission_id}/open", data={"_csrf_token": token, "open": "0"})
        self.assertFalse(store.mission(mission_id)["is_open"])
        with self.client.session_transaction() as sess:
            sess["user_id"] = other_id
        self.client.post(f"/missions/{mission_id}/submit", data={"_csrf_token": token, "body": "Late"})
        self.assertEqual(len(store.all()[0]["submissions"]), 2)

        # Deleting an account removes its work; deleting a mission removes everything submitted for it.
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.admin_id
        self.client.post(f"/settings/users/{member_id}/delete", data={"_csrf_token": token})
        self.assertEqual(store.all()[0]["submissions"], [])
        self.client.post(f"/missions/{mission_id}/delete", data={"_csrf_token": token})
        self.assertEqual(store.all(), [])

    def test_members_propose_missions_that_administrators_approve(self):
        token = self._csrf()
        store = self.app.extensions["missions"]
        users = self.app.extensions["users"]
        notifications = self.app.extensions["notifications"]
        member_id = users.add("member", "member-password")
        other_id = users.add("other", "other-password")

        def as_user(user_id):
            with self.client.session_transaction() as sess:
                sess["user_id"] = user_id

        # A member's mission is a proposal: only they and administrators see it, and no work can be submitted yet.
        as_user(member_id)
        self.assertIn("Propose a mission", self.client.get("/missions").get_data(as_text=True))
        self.client.post("/missions", data={"_csrf_token": token, "title": "Clean the kitchen",
                                            "details": "Every Friday", "reward": "50"})
        mission = store.all()[0]
        self.assertEqual((mission["approval"], mission["reward"]), ("proposed", 50))
        self.assertIn("member proposed a mission", notifications.for_user(self.admin_id)[0]["message"])
        self.assertIn("Your proposed missions", self.client.get("/missions").get_data(as_text=True))
        self.client.post(f"/missions/{mission['id']}/submit", data={"_csrf_token": token, "body": "Done"})
        self.assertEqual(store.all()[0]["submissions"], [])
        self.assertEqual(self.client.post(f"/missions/{mission['id']}/decide", data={
            "_csrf_token": token, "decision": "approved", "reward": "50"}).status_code, 403)
        as_user(other_id)
        self.assertNotIn("Clean the kitchen", self.client.get("/missions").get_data(as_text=True))
        self.assertEqual(self.client.post(f"/missions/{mission['id']}/delete", data={"_csrf_token": token}).status_code, 403)

        # Declining needs a note; approving sets the reward and opens it to everyone.
        as_user(self.admin_id)
        page = self.client.get("/missions").get_data(as_text=True)
        self.assertIn("Proposed missions", page)
        self.assertIn("Clean the kitchen", page)
        self.client.post(f"/missions/{mission['id']}/decide", data={"_csrf_token": token, "decision": "declined", "reward": "50"})
        self.assertEqual(store.mission(mission["id"])["approval"], "proposed")
        self.client.post(f"/missions/{mission['id']}/decide", data={
            "_csrf_token": token, "decision": "approved", "reward": "30"})
        mission = store.mission(mission["id"])
        self.assertEqual((mission["approval"], mission["reward"]), ("approved", 30))
        self.assertIn("approved your mission", notifications.for_user(member_id)[0]["message"])
        self.assertIn("Clean the kitchen", notifications.for_user(other_id)[0]["message"])
        as_user(other_id)
        self.client.post(f"/missions/{mission['id']}/submit", data={"_csrf_token": token, "body": "Cleaned it"})
        approved = next(m for m in store.all() if m["id"] == mission["id"])
        self.assertEqual([item["body"] for item in approved["submissions"]], ["Cleaned it"])
        # Once approved, the member can't remove it any more.
        as_user(member_id)
        self.assertEqual(self.client.post(f"/missions/{mission['id']}/delete", data={"_csrf_token": token}).status_code, 403)

        # A declined proposal shows the note to the person who proposed it, who can remove it.
        self.client.post("/missions", data={"_csrf_token": token, "title": "Free lunch"})
        lunch = next(m for m in store.all() if m["title"] == "Free lunch")
        as_user(self.admin_id)
        self.client.post(f"/missions/{lunch['id']}/decide", data={
            "_csrf_token": token, "decision": "declined", "note": "Not a task", "reward": "0"})
        self.assertIn("declined your mission", notifications.for_user(member_id)[0]["message"])
        as_user(member_id)
        page = self.client.get("/missions").get_data(as_text=True)
        self.assertIn("Not a task", page)
        self.client.post(f"/missions/{lunch['id']}/delete", data={"_csrf_token": token})
        self.assertIsNone(store.mission(lunch["id"]))

    def test_missions_run_between_their_dates(self):
        from datetime import datetime, timedelta, timezone
        token = self._csrf()
        store = self.app.extensions["missions"]
        member_id = self.app.extensions["users"].add("member", "member-password")
        today = (datetime.now(timezone.utc) + timedelta(hours=7)).date()
        day = lambda offset: (today + timedelta(days=offset)).isoformat()

        def add(title, starts, ends):
            self.client.post("/missions", data={"_csrf_token": token, "title": title, "starts_on": starts, "ends_on": ends})
            return next((m for m in store.all() if m["title"] == title), None)

        self.assertIsNone(add("Backwards", day(3), day(1)))
        self.assertIn("be before the start date", self.client.get("/missions").get_data(as_text=True))
        self.assertIsNone(add("Bad date", "14/10/2026", ""))
        later, past, now = add("Later", day(2), day(9)), add("Past", day(-5), day(-1)), add("Now", day(-1), day(0))
        self.assertEqual((later["starts_on"], later["ends_on"]), (day(2), day(9)))

        with self.client.session_transaction() as sess:
            sess["user_id"] = member_id
        page = self.client.get("/missions").get_data(as_text=True)
        self.assertIn("Starts in 2 days", page)
        self.assertIn("Last day today", page)
        self.assertIn("Ended", page)
        for mission in (later, past, now):
            self.client.post(f"/missions/{mission['id']}/submit", data={"_csrf_token": token, "body": "Did it"})
        submitted = {m["title"]: len(m["submissions"]) for m in store.all()}
        self.assertEqual((submitted["Later"], submitted["Past"], submitted["Now"]), (0, 0, 1))
        self.assertEqual(self.client.post(f"/missions/{later['id']}/dates", data={
            "_csrf_token": token, "starts_on": day(0), "ends_on": ""}).status_code, 403)

        # An administrator can move the dates; no dates means it runs until it's closed.
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.admin_id
        self.client.post(f"/missions/{later['id']}/dates", data={"_csrf_token": token, "starts_on": "", "ends_on": ""})
        self.assertEqual(store.mission(later["id"])["starts_on"], "")
        with self.client.session_transaction() as sess:
            sess["user_id"] = member_id
        self.client.post(f"/missions/{later['id']}/submit", data={"_csrf_token": token, "body": "Now I can"})
        self.assertEqual(len(next(m for m in store.all() if m["title"] == "Later")["submissions"]), 1)

    def test_missions_have_a_promotional_picture(self):
        from PIL import Image
        token = self._csrf()
        store = self.app.extensions["missions"]
        member_id = self.app.extensions["users"].add("member", "member-password")

        def picture(size=(2400, 1200), color="orange"):
            data = io.BytesIO()
            Image.new("RGB", size, color).save(data, "PNG")
            data.seek(0)
            return data

        # A proposed mission keeps its picture, resized, and shows it as the card's cover.
        with self.client.session_transaction() as sess:
            sess["user_id"] = member_id
        self.client.post("/missions", data={"_csrf_token": token, "title": "Clean-up day", "image": (picture(), "promo.png")},
                         content_type="multipart/form-data")
        mission = store.all()[0]
        self.assertRegex(mission["image"], r"^[0-9a-f]{32}\.jpg$")
        page = self.client.get("/missions").get_data(as_text=True)
        self.assertIn(f"/missions/images/{mission['image']}", page)
        response = self.client.get(f"/missions/images/{mission['image']}")
        self.assertEqual(response.mimetype, "image/jpeg")
        with Image.open(io.BytesIO(response.data)) as image:
            self.assertEqual(image.size, (1600, 800))
        response.close()
        self.assertEqual(self.client.get("/missions/images/nothing.jpg").status_code, 404)

        # A file that isn't a picture is refused and nothing is saved.
        self.client.post("/missions", data={"_csrf_token": token, "title": "Bad picture",
                                            "image": (io.BytesIO(b"not an image"), "fake.png")},
                         content_type="multipart/form-data")
        self.assertIn("not a picture the server can read", self.client.get("/missions").get_data(as_text=True))
        self.assertEqual([m["title"] for m in store.all()], ["Clean-up day"])
        self.assertEqual(self.client.post(f"/missions/{mission['id']}/image", data={"_csrf_token": token, "remove": "1"}).status_code, 403)

        # An administrator can replace or remove it; the old file goes.
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.admin_id
        folder = Path(self.temp_dir.name) / "feed" / "missions"
        old = mission["image"]
        self.client.post(f"/missions/{mission['id']}/image", data={"_csrf_token": token, "image": (picture((300, 300), "blue"), "new.png")},
                         content_type="multipart/form-data")
        new = store.mission(mission["id"])["image"]
        self.assertNotEqual(new, old)
        self.assertFalse((folder / old).exists())
        self.client.post(f"/missions/{mission['id']}/image", data={"_csrf_token": token, "remove": "1"})
        self.assertEqual(store.mission(mission["id"])["image"], "")
        self.assertFalse((folder / new).exists())

        # Deleting a mission deletes its picture.
        self.client.post("/missions", data={"_csrf_token": token, "title": "With picture", "image": (picture(), "p.png")},
                         content_type="multipart/form-data")
        with_picture = next(m for m in store.all() if m["title"] == "With picture")
        self.client.post(f"/missions/{with_picture['id']}/delete", data={"_csrf_token": token})
        self.assertFalse((folder / with_picture["image"]).exists())

    def test_daily_quests_count_actions_and_pay_after_approval(self):
        token = self._csrf()
        quests = self.app.extensions["quests"]
        users = self.app.extensions["users"]
        notifications = self.app.extensions["notifications"]
        member_id = users.add("member", "member-password")

        def as_user(user_id):
            with self.client.session_transaction() as sess:
                sess["user_id"] = user_id

        # Administrators set quests; members can't.
        self.client.post("/quests", data={"_csrf_token": token, "action": "nope", "target": "1", "reward": "5"})
        self.assertEqual(quests.all(), [])
        self.client.post("/quests", data={"_csrf_token": token, "action": "note", "target": "2", "reward": "20",
                                          "needs_approval": "1"})
        self.client.post("/quests", data={"_csrf_token": token, "action": "upload", "target": "1", "reward": "5"})
        note_quest, upload_quest = quests.all()
        self.assertEqual((note_quest["title"], note_quest["needs_approval"]), ("Write a note 2 times", 1))
        self.assertEqual(upload_quest["needs_approval"], 0)
        self.assertIn("New daily quest", notifications.for_user(member_id)[0]["message"])
        as_user(member_id)
        self.assertEqual(self.client.post("/quests", data={"_csrf_token": token, "action": "note"}).status_code, 403)

        # Progress counts today's actions; claiming early does nothing.
        self.client.post(f"/quests/{note_quest['id']}/claim", data={"_csrf_token": token})
        self.assertEqual(quests.pending(), [])
        for title in ("One", "Two", "Three"):
            self.client.post("/notes/create", data={"_csrf_token": token, "title": title, "content": "x"})
        page = self.client.get("/missions").get_data(as_text=True)
        self.assertIn("2 / 2 today", page)
        self.assertIn("0 / 1 today", page)

        # A quest that needs approval waits, can be claimed once a day, and pays when approved.
        self.client.post(f"/quests/{note_quest['id']}/claim", data={"_csrf_token": token})
        self.client.post(f"/quests/{note_quest['id']}/claim", data={"_csrf_token": token})
        pending = quests.pending()
        self.assertEqual([(c["author"], c["status"]) for c in pending], [("member", "pending")])
        self.assertIn("finished the daily quest", notifications.for_user(self.admin_id)[0]["message"])

        # Uploading pays the other quest straight away.
        self.client.post("/upload", data={"_csrf_token": token, "path": "",
                                          "files": (io.BytesIO(b"hi"), "quest.txt")},
                         content_type="multipart/form-data")
        self.client.post(f"/quests/{upload_quest['id']}/claim", data={"_csrf_token": token})
        from_wallet = lambda: self.client.get("/wallet").get_data(as_text=True)
        self.assertIn("Daily quest: Upload files to Network Files", from_wallet())

        as_user(self.admin_id)
        self.assertIn("Quest rewards to approve (1)", self.client.get("/missions").get_data(as_text=True))
        self.client.post(f"/quests/claims/{pending[0]['id']}/review", data={"_csrf_token": token, "decision": "declined"})
        self.assertEqual(len(quests.pending()), 1)
        self.client.post(f"/quests/claims/{pending[0]['id']}/review", data={"_csrf_token": token, "decision": "approved"})
        self.assertEqual(quests.pending(), [])
        self.assertIn("approved your daily quest", notifications.for_user(member_id)[0]["message"])
        as_user(member_id)
        page = from_wallet()
        self.assertIn("Daily quest: Write a note 2 times", page)
        self.assertIn("25", page)

        # The activity log now shows uploads too.
        as_user(self.admin_id)
        self.assertIn("1 file(s) to /", self.client.get("/settings/activity?area=files").get_data(as_text=True))

    def test_wallet_link_before_log_out_shows_points_unless_hidden(self):
        token = self._csrf()
        self.app.extensions["wallet"].deposit(self.admin_id, 1250, "Start", self.admin_id)
        page = self.client.get("/files").get_data(as_text=True)
        self.assertEqual(page.count("href=\"/wallet\""), 1)
        self.assertLess(page.index('class="topbar-link topbar-user"'), page.index("topbar-wallet"))
        self.assertLess(page.index("topbar-wallet"), page.index("ออกจากระบบ"))  # Log out, in Thai.
        self.assertIn('<small class="topbar-coins">🪙 1,250</small>', page)
        self.assertIn('href="/wallet" aria-current="page"', self.client.get("/wallet").get_data(as_text=True))
        self.assertIn('name="show_balance" value="1" checked',
                      self.client.get("/settings/notifications").get_data(as_text=True))

        # Turned off under Settings → Notifications, the link stays but the points don't show.
        self.client.post("/settings/notifications", data={"_csrf_token": token, "popups": "1"})
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("topbar-wallet", page)
        self.assertNotIn("topbar-coins", page)
        self.client.post("/settings/notifications", data={"_csrf_token": token, "show_balance": "1"})
        self.assertIn("topbar-coins", self.client.get("/").get_data(as_text=True))

    def test_side_menu_icons_and_light_dark_theme_setting(self):
        token = self._csrf()
        page = self.client.get("/notes").get_data(as_text=True)
        self.assertIn('<header class="sidebar">', page)
        self.assertNotIn('<header class="topbar">', page)
        self.assertIn('class="sidebar-link" href="/notes" title="โน้ต" aria-current="page"', page)
        self.assertIn('class="sidebar-link" href="/feed" title="ฟีด">', page)
        self.assertIn('<html lang="th" data-theme="auto">', page)
        self.assertIn('href="/settings/appearance"', self.client.get("/settings").get_data(as_text=True))
        self.assertIn('name="theme" value="auto" checked',
                      self.client.get("/settings/appearance").get_data(as_text=True))

        response = self.client.post("/settings/appearance", data={"_csrf_token": token, "theme": "dark"})
        self.assertEqual(response.status_code, 303)
        self.assertIn('data-theme="dark"', self.client.get("/").get_data(as_text=True))
        self.assertIn('name="theme" value="dark" checked',
                      self.client.get("/settings/appearance").get_data(as_text=True))

        # Saving notification choices keeps the theme; an unknown theme is refused.
        self.client.post("/settings/notifications", data={"_csrf_token": token, "popups": "1"})
        self.client.post("/settings/appearance", data={"_csrf_token": token, "theme": "purple"})
        page = self.client.get("/settings/appearance").get_data(as_text=True)
        self.assertIn('data-theme="dark"', page)
        self.client.post("/settings/appearance", data={"_csrf_token": token, "theme": "light"})
        self.assertIn('data-theme="light"', self.client.get("/wallet").get_data(as_text=True))

    def test_language_setting_translates_the_menu_and_top_bar(self):
        import app as app_module

        token = self._csrf()
        # Thai is the main language until someone picks another.
        page = self.client.get("/settings/appearance").get_data(as_text=True)
        self.assertIn('<html lang="th"', page)
        self.assertIn('name="language" value="th" checked', page)
        for key in ("th", "en", "lo", "zh", "ru", "es", "la"):
            self.assertIn(f'name="language" value="{key}"', page)
        self.assertIn("<span>ตั้งค่า</span>", page)

        response = self.client.post("/settings/appearance/language", data={"_csrf_token": token, "language": "en"})
        self.assertEqual(response.status_code, 303)
        page = self.client.get("/notes").get_data(as_text=True)
        self.assertIn('<html lang="en"', page)
        self.assertIn('class="sidebar-link" href="/notes" title="Notes" aria-current="page"', page)
        self.assertIn("<span>Log out</span>", page)

        self.client.post("/settings/appearance/language", data={"_csrf_token": token, "language": "ru"})
        page = self.client.get("/settings/appearance").get_data(as_text=True)
        self.assertIn("Язык изменён: Русский.", page)
        self.assertIn("<span>Выйти</span>", page)
        self.assertIn('title="Справка"', page)
        # The theme is kept, and an unknown language is refused.
        self.client.post("/settings/appearance", data={"_csrf_token": token, "theme": "dark"})
        self.client.post("/settings/appearance/language", data={"_csrf_token": token, "language": "xx"})
        page = self.client.get("/settings/appearance").get_data(as_text=True)
        self.assertIn('<html lang="ru" data-theme="dark">', page)
        self.assertIn('name="language" value="ru" checked', page)
        self.assertIn("Выберите язык из списка.", page)

        # Every word has every language, and words without a translation fall back to English.
        for words in app_module.TRANSLATIONS.values():
            self.assertEqual(set(words), set(app_module.LANGUAGES) - {"en"})
        self.assertEqual(app_module.translate("Not translated yet", "lo"), "Not translated yet")
        self.assertEqual(app_module.translate("{count} points", "es", count="1,250"), "1,250 puntos")

    def test_help_page_covers_every_part_of_the_app(self):
        import app as app_module

        # The help must be updated with every version.
        self.assertEqual(app_module.HELP_VERSION, app_module.APP_VERSION,
                         "Update HELP_TOPICS for this change, then set HELP_VERSION to the new version.")
        page = self.client.get("/help").get_data(as_text=True)
        self.assertIn('class="sidebar-link" href="/help" title="ช่วยเหลือ" aria-current="page"', page)
        self.assertIn(f"Up to date with version {app_module.APP_VERSION}", page)
        # Every page in the side menu and every Settings category has its help.
        menu = self.client.get("/").get_data(as_text=True).split('<nav class="sidebar-nav"')[1].split("</nav>")[0]
        for href in re.findall(r'class="sidebar-link" href="([^"]+)"', menu):
            if href != "/help":
                self.assertIn(f'class="button secondary help-open" href="{href}"', page)
        for section in app_module.SETTINGS_SECTIONS:
            self.assertIn(section["title"].split(" & ")[0], page)
        self.assertIn("For administrators", page)

        # People who aren't administrators get only the parts they can use.
        member_id = self.app.extensions["users"].add("member", "member-password")
        with self.client.session_transaction() as sess:
            sess["user_id"] = member_id
        page = self.client.get("/help").get_data(as_text=True)
        self.assertIn("Submit for approval", page)
        self.assertNotIn("For administrators", page)

    def test_wallet_deposits_transfers_and_mission_rewards(self):
        token = self._csrf()
        wallet = self.app.extensions["wallet"]
        missions = self.app.extensions["missions"]
        users = self.app.extensions["users"]
        notifications = self.app.extensions["notifications"]
        member_id = users.add("member", "member-password")
        other_id = users.add("other", "other-password")

        # Administrators deposit points and can take them back, but never below zero.
        self.client.post("/wallet/deposit", data={"_csrf_token": token, "user_id": member_id, "amount": "abc"})
        self.assertEqual(wallet.balance(member_id), 0)
        self.client.post("/wallet/deposit", data={"_csrf_token": token, "user_id": member_id,
                                                  "amount": "1,500", "note": "Welcome"})
        self.assertEqual(wallet.balance(member_id), 1500)
        self.assertIn("deposited 1,500 points", notifications.for_user(member_id)[0]["message"])
        self.client.post("/wallet/deposit", data={"_csrf_token": token, "user_id": member_id,
                                                  "amount": "2000", "action": "take"})
        self.assertIn("don&#39;t have that many points", self.client.get("/wallet").get_data(as_text=True))
        self.client.post("/wallet/deposit", data={"_csrf_token": token, "user_id": member_id,
                                                  "amount": "500", "action": "take"})
        self.assertEqual(wallet.balance(member_id), 1000)
        page = self.client.get("/wallet").get_data(as_text=True)
        self.assertIn("Everyone's balance", page)
        self.assertIn("Welcome", page)

        # Members can't deposit; they send from their own points only, never to themselves.
        with self.client.session_transaction() as sess:
            sess["user_id"] = member_id
        self.assertEqual(self.client.post("/wallet/deposit", data={
            "_csrf_token": token, "user_id": member_id, "amount": "5"}).status_code, 403)
        page = self.client.get("/wallet").get_data(as_text=True)
        self.assertIn("1,000", page)
        self.assertNotIn("Everyone's balance", page)
        for amount, to in (("5000", "other"), ("0", "other"), ("-5", "other"), ("10", "member"), ("10", "nobody")):
            self.client.post("/wallet/transfer", data={"_csrf_token": token, "to": to, "amount": amount})
        self.assertEqual((wallet.balance(member_id), wallet.balance(other_id)), (1000, 0))
        self.client.post("/wallet/transfer", data={"_csrf_token": token, "to": "other", "amount": "300",
                                                   "note": "Lunch"})
        self.assertEqual((wallet.balance(member_id), wallet.balance(other_id)), (700, 300))
        self.assertIn("member sent you 300 points: Lunch", notifications.for_user(other_id)[0]["message"])
        self.assertEqual([e["kind"] for e in wallet.history(member_id)], ["transfer_out", "withdraw", "deposit"])

        # Approving work for a mission with a reward pays it once.
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.admin_id
        self.client.post("/missions", data={"_csrf_token": token, "title": "Bad reward", "reward": "lots"})
        self.assertEqual(missions.all(), [])
        self.client.post("/missions", data={"_csrf_token": token, "title": "Tidy Projects", "reward": "250"})
        mission_id = missions.all()[0]["id"]
        self.assertIn("250 points", self.client.get("/missions").get_data(as_text=True))
        with self.client.session_transaction() as sess:
            sess["user_id"] = other_id
        self.client.post(f"/missions/{mission_id}/submit", data={"_csrf_token": token, "body": "Done"})
        submission_id = missions.all()[0]["submissions"][0]["id"]
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.admin_id
        for _ in range(2):
            self.client.post(f"/missions/submissions/{submission_id}/review",
                             data={"_csrf_token": token, "decision": "approved"})
        self.assertEqual(wallet.balance(other_id), 550)
        self.assertEqual(wallet.history(other_id)[0]["note"], "Tidy Projects")
        self.assertIn("250 points went to your wallet", notifications.for_user(other_id)[0]["message"])

        # Deleting an account removes its points; the other side keeps theirs.
        self.client.post(f"/settings/users/{member_id}/delete", data={"_csrf_token": token})
        self.assertEqual(wallet.history(member_id), [])
        self.assertEqual(wallet.balance(other_id), 550)
        self.assertIn("from a deleted account", self.client.get("/wallet").get_data(as_text=True))

    def test_wallet_dashboard_exchange_and_supply_demand(self):
        token = self._csrf()
        wallet = self.app.extensions["wallet"]
        users = self.app.extensions["users"]
        notifications = self.app.extensions["notifications"]
        member_id = users.add("member", "member-password")
        wallet.deposit(member_id, 1000, "Start", self.admin_id)

        page = self.client.get("/wallet").get_data(as_text=True)
        for text in ("Available balance", 'href="#deposit"', 'href="#transfer"', 'href="#exchange"',
                     "Coin supply and demand", "Nothing to exchange yet.", "Add an offer"):
            self.assertIn(text, page)

        # Administrators add offers; bad ones are refused.
        for form in ({"title": "", "price": "10"}, {"title": "Mug", "price": "0"}, {"title": "Mug", "price": "10",
                                                                                   "stock": "x"}):
            self.client.post("/wallet/offers", data={"_csrf_token": token, **form})
        self.assertEqual(wallet.offers(), [])
        self.client.post("/wallet/offers", data={"_csrf_token": token, "title": "Coffee", "price": "400", "stock": "1"})
        self.client.post("/wallet/offers", data={"_csrf_token": token, "title": "Holiday", "price": "5000"})
        coffee, holiday = wallet.offers()
        self.assertEqual((coffee["stock"], holiday["stock"]), (1, -1))

        # Members exchange points; the stock runs out and the balance can't go below zero.
        with self.client.session_transaction() as sess:
            sess["user_id"] = member_id
        self.assertEqual(self.client.post("/wallet/offers", data={
            "_csrf_token": token, "title": "Free", "price": "1"}).status_code, 403)
        page = self.client.get("/wallet").get_data(as_text=True)
        self.assertIn("Coffee", page)
        self.assertNotIn("Add an offer", page)
        self.client.post(f"/wallet/offers/{holiday['id']}/exchange", data={"_csrf_token": token})
        self.assertEqual(wallet.balance(member_id), 1000)
        self.client.post(f"/wallet/offers/{coffee['id']}/exchange", data={"_csrf_token": token})
        self.assertEqual(wallet.balance(member_id), 600)
        self.assertIn("member exchanged 400 points for “Coffee”", notifications.for_user(self.admin_id)[0]["message"])
        self.client.post(f"/wallet/offers/{coffee['id']}/exchange", data={"_csrf_token": token})
        self.assertIn("sold out", self.client.get("/wallet").get_data(as_text=True))
        self.assertEqual(wallet.balance(member_id), 600)
        self.assertEqual(wallet.history(member_id)[0]["kind"], "exchange")

        # Turned-off offers are hidden from members and can't be exchanged.
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.admin_id
        self.client.post(f"/wallet/offers/{holiday['id']}/active", data={"_csrf_token": token, "active": "0"})
        self.assertIn("is off", self.client.get("/wallet").get_data(as_text=True))
        with self.client.session_transaction() as sess:
            sess["user_id"] = member_id
        self.assertNotIn("Holiday", self.client.get("/wallet").get_data(as_text=True))
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.admin_id
        self.client.post(f"/wallet/offers/{holiday['id']}/delete", data={"_csrf_token": token})
        self.assertEqual([offer["title"] for offer in wallet.offers()], ["Coffee"])

        # Supply and demand: 1,000 created, 400 used up, 100 changed hands.
        self.client.post("/wallet/transfer", data={"_csrf_token": token, "to": "member", "amount": "100"})
        wallet.deposit(self.admin_id, 200, "", self.admin_id)
        self.client.post("/wallet/transfer", data={"_csrf_token": token, "to": "member", "amount": "100"})
        economy = wallet.economy()
        self.assertEqual((economy["supply"], economy["created"], economy["used"], economy["moved"]), (800, 1200, 400, 100))
        self.assertEqual((economy["holders"], economy["top_share"], economy["net"]), (2, 88, 800))
        self.assertEqual(economy["reading"], "supply")
        self.assertEqual(len(economy["trend"]), 14)
        self.assertEqual((economy["trend"][-1]["created"], economy["trend"][-1]["used"]), (1200, 400))
        self.assertIn("Supply is high", self.client.get("/wallet").get_data(as_text=True))

    def test_mission_reward_goals(self):
        token = self._csrf()
        wallet = self.app.extensions["wallet"]
        users = self.app.extensions["users"]
        member_id = users.add("member", "member-password")
        wallet.deposit(member_id, 1500, "Start", self.admin_id)

        page = self.client.get("/missions").get_data(as_text=True)
        for text in ("Reward goals", "No rewards yet.", "＋ New reward"):
            self.assertIn(text, page)

        # Administrators add rewards with details from the Missions page and come back to it.
        response = self.client.post("/wallet/offers", data={
            "_csrf_token": token, "back": "missions", "title": "Travel ticket", "price": "3,000", "stock": "2",
            "details": "Return flight to Chiang Mai"})
        self.assertTrue(response.headers["Location"].endswith("/missions#goals"))
        self.client.post("/wallet/offers", data={"_csrf_token": token, "title": "Coffee", "price": "500"})
        ticket, coffee = sorted(wallet.offers(), key=lambda offer: -offer["price"])
        self.assertEqual(ticket["details"], "Return flight to Chiang Mai")

        # A member picks a goal and sees how far along they are.
        with self.client.session_transaction() as sess:
            sess["user_id"] = member_id
        page = self.client.get("/missions").get_data(as_text=True)
        self.assertNotIn("＋ New reward", page)
        self.assertIn("pick a reward to save for", page)
        self.client.post("/missions/goal", data={"_csrf_token": token, "offer_id": "999"})
        self.assertIsNone(wallet.goal(member_id))
        self.client.post("/missions/goal", data={"_csrf_token": token, "offer_id": str(ticket["id"])})
        self.assertEqual(wallet.goal(member_id), ticket["id"])
        page = self.client.get("/missions").get_data(as_text=True)
        self.assertIn("50%", page)
        self.assertIn("1,500 more points needed", page)
        self.assertIn("Return flight to Chiang Mai", page)
        self.assertLess(page.index("Travel ticket</h3>"), page.index("Coffee</h3>"))

        # Not enough points yet; once there are, exchanging reaches the goal and clears it.
        response = self.client.post(f"/wallet/offers/{ticket['id']}/exchange",
                                    data={"_csrf_token": token, "back": "missions"})
        self.assertTrue(response.headers["Location"].endswith("/missions#goals"))
        self.assertEqual(wallet.balance(member_id), 1500)
        wallet.reward(member_id, 1500, "Mission", self.admin_id)
        self.assertIn("You have enough points!", self.client.get("/missions").get_data(as_text=True))
        self.client.post(f"/wallet/offers/{ticket['id']}/exchange", data={"_csrf_token": token, "back": "missions"})
        self.assertEqual(wallet.balance(member_id), 0)
        self.assertIsNone(wallet.goal(member_id))
        self.assertEqual(wallet.offer(ticket["id"])["stock"], 1)

        # Removing a goal, and deleting a reward someone is saving for, both clear it.
        self.client.post("/missions/goal", data={"_csrf_token": token, "offer_id": str(coffee["id"])})
        self.client.post("/missions/goal", data={"_csrf_token": token, "offer_id": ""})
        self.assertIsNone(wallet.goal(member_id))
        wallet.set_goal(member_id, coffee["id"])
        wallet.delete_offer(coffee["id"])
        self.assertIsNone(wallet.goal(member_id))

    def test_rewards_have_a_picture_on_their_card(self):
        from PIL import Image
        token = self._csrf()
        wallet = self.app.extensions["wallet"]
        folder = Path(self.temp_dir.name) / "feed" / "missions"

        def picture(size=(2400, 1200), color="teal"):
            data = io.BytesIO()
            Image.new("RGB", size, color).save(data, "PNG")
            data.seek(0)
            return data

        page = self.client.get("/missions").get_data(as_text=True)
        self.assertIn('id="goal-image"', page)
        self.assertIn('id="offer-image"', self.client.get("/wallet").get_data(as_text=True))

        # A new reward keeps its picture, resized, and both pages show it across the top of the card.
        response = self.client.post("/wallet/offers", data={
            "_csrf_token": token, "back": "missions", "title": "Travel ticket", "price": "3000",
            "image": (picture(), "trip.png")}, content_type="multipart/form-data")
        self.assertTrue(response.headers["Location"].endswith("/missions#goals"))
        offer = wallet.offers()[0]
        self.assertRegex(offer["image"], r"^[0-9a-f]{32}\.jpg$")
        with Image.open(folder / offer["image"]) as image:
            self.assertEqual(image.size, (1600, 800))
        for url in ("/missions", "/wallet"):
            page = self.client.get(url).get_data(as_text=True)
            self.assertIn(f"/missions/images/{offer['image']}", page)
            self.assertIn("has-cover", page)

        # A file that isn't a picture is refused and the reward isn't added.
        self.client.post("/wallet/offers", data={"_csrf_token": token, "title": "Bad", "price": "10",
                                                 "image": (io.BytesIO(b"not an image"), "fake.png")},
                         content_type="multipart/form-data")
        self.assertEqual([o["title"] for o in wallet.offers()], ["Travel ticket"])

        # Only administrators change pictures.
        member_id = self.app.extensions["users"].add("member", "member-password")
        with self.client.session_transaction() as sess:
            sess["user_id"] = member_id
        self.assertEqual(self.client.post(f"/wallet/offers/{offer['id']}/image",
                                          data={"_csrf_token": token, "remove": "1"}).status_code, 403)
        self.assertNotIn("Change picture", self.client.get("/missions").get_data(as_text=True))
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.admin_id

        # Replacing or removing the picture deletes the old file; so does deleting the reward.
        old = offer["image"]
        response = self.client.post(f"/wallet/offers/{offer['id']}/image", data={
            "_csrf_token": token, "back": "missions", "image": (picture((300, 300)), "new.png")},
            content_type="multipart/form-data")
        self.assertTrue(response.headers["Location"].endswith("/missions#goals"))
        new = wallet.offer(offer["id"])["image"]
        self.assertNotEqual(new, old)
        self.assertFalse((folder / old).exists())
        self.client.post(f"/wallet/offers/{offer['id']}/image", data={"_csrf_token": token, "remove": "1"})
        self.assertEqual(wallet.offer(offer["id"])["image"], "")
        self.assertFalse((folder / new).exists())
        self.client.post(f"/wallet/offers/{offer['id']}/image", data={
            "_csrf_token": token, "image": (picture(), "again.png")}, content_type="multipart/form-data")
        again = wallet.offer(offer["id"])["image"]
        self.client.post(f"/wallet/offers/{offer['id']}/delete", data={"_csrf_token": token})
        self.assertIsNone(wallet.offer(offer["id"]))
        self.assertFalse((folder / again).exists())

    def test_announcements_float_on_every_page_colored_by_level(self):
        token = self._csrf()
        store = self.app.extensions["announcements"]
        users = self.app.extensions["users"]
        member_id = users.add("member", "member-password")

        # Administrators post them; a bad level or an empty message is refused.
        self.client.post("/settings/announcements/add", data={"_csrf_token": token, "message": "  ", "level": "info"})
        self.client.post("/settings/announcements/add", data={"_csrf_token": token, "message": "Hi", "level": "loud"})
        self.assertEqual(store.all(), [])
        self.client.post("/settings/announcements/add", data={"_csrf_token": token, "message": "x" * 301,
                                                              "level": "info"})
        self.assertEqual(store.all(), [])
        self.client.post("/settings/announcements/add", data={"_csrf_token": token,
                                                              "message": "New  printer on floor 2", "level": "info"})
        self.client.post("/settings/announcements/add", data={"_csrf_token": token,
                                                              "message": "Server restarts at 22:00", "level": "urgent"})
        self.assertEqual([item["message"] for item in store.all()],
                         ["Server restarts at 22:00", "New printer on floor 2"])
        self.assertEqual([item["level"] for item in store.active()], ["urgent", "info"])
        self.assertIn("2 showing", self.client.get("/settings").get_data(as_text=True))

        # Everyone sees them floating on every page, in their level's color; members can't manage them.
        with self.client.session_transaction() as sess:
            sess["user_id"] = member_id
        for path in ("/", "/feed", "/notes", "/wallet"):
            page = self.client.get(path).get_data(as_text=True)
            self.assertIn('class="announcements"', page, path)
            self.assertIn('announcement is-urgent', page)
            self.assertIn("Server restarts at 22:00", page)
        urgent_id = store.all()[0]["id"]
        for path in ("/settings/announcements/add", f"/settings/announcements/{urgent_id}/toggle",
                     f"/settings/announcements/{urgent_id}/delete"):
            self.assertEqual(self.client.post(path, data={"_csrf_token": token, "message": "Mine",
                                                          "level": "info"}).status_code, 403)
        self.assertEqual(self.client.get("/settings/announcements").status_code, 403)
        self.assertEqual(len(store.all()), 2)

        # Turned off, it stops showing; turned back on, it returns; deleted, it's gone.
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.admin_id
        self.client.post(f"/settings/announcements/{urgent_id}/toggle", data={"_csrf_token": token})
        self.assertNotIn("Server restarts at 22:00", self.client.get("/").get_data(as_text=True))
        self.assertIn("Server restarts at 22:00", self.client.get("/settings/announcements").get_data(as_text=True))
        self.client.post(f"/settings/announcements/{urgent_id}/toggle", data={"_csrf_token": token})
        self.assertIn("Server restarts at 22:00", self.client.get("/").get_data(as_text=True))
        for item in store.all():
            self.client.post(f"/settings/announcements/{item['id']}/delete", data={"_csrf_token": token})
        self.assertEqual(store.all(), [])
        self.assertNotIn('class="announcements"', self.client.get("/").get_data(as_text=True))

    def test_user_requests_can_be_added_tracked_and_closed(self):
        import app as app_module

        token = self._csrf()
        page = self.client.get("/updates").get_data(as_text=True)
        self.assertIn("No requests yet", page)

        # A request needs a title; what was typed stays in the form.
        response = self.client.post("/updates/requests", data={
            "_csrf_token": token, "kind": "feature", "title": " ", "details": "Keep me",
        })
        self.assertEqual(response.status_code, 303)
        page = self.client.get("/updates").get_data(as_text=True)
        self.assertIn("Give the request a short title.", page)
        self.assertIn("Keep me", page)

        for kind, title in (("feature", "แสดงขนาดไฟล์เป็น MB"), ("problem", "Delete shows Not Found")):
            response = self.client.post("/updates/requests", data={
                "_csrf_token": token, "kind": kind, "title": title, "details": "Details", "requested_by": "Karn",
            })
            self.assertEqual(response.status_code, 303)
        store = self.app.extensions["requests"]
        items = {item["title"]: item for item in store.all()}
        self.assertEqual(items["Delete shows Not Found"]["kind"], "problem")
        self.assertEqual(items["แสดงขนาดไฟล์เป็น MB"]["status"], "open")

        # Marking a request done records the app version that has the change.
        done_id = items["Delete shows Not Found"]["id"]
        self.client.post(f"/updates/requests/{done_id}/status", data={"_csrf_token": token, "status": "done"})
        done = store.get(done_id)
        self.assertEqual((done["status"], done["done_version"]), ("done", app_module.APP_VERSION))
        page = self.client.get("/updates?status=done").get_data(as_text=True)
        self.assertIn("Delete shows Not Found", page)
        self.assertNotIn("แสดงขนาดไฟล์เป็น MB", page)
        self.assertIn(f"version {app_module.APP_VERSION}", page)
        self.assertIn("1 waiting · 0 in progress · 1 done", page)

        # Reopening clears the version; search finds requests too.
        self.client.post(f"/updates/requests/{done_id}/status", data={"_csrf_token": token, "status": "open"})
        self.assertEqual(store.get(done_id)["done_version"], "")
        self.assertIn("แสดงขนาดไฟล์เป็น MB", self.client.get("/updates?q=MB").get_data(as_text=True))
        self.assertEqual(
            self.client.post(f"/updates/requests/{done_id}/status", data={"_csrf_token": token, "status": "nope"}).status_code,
            303,
        )
        self.assertEqual(store.get(done_id)["status"], "open")

        self.client.post(f"/updates/requests/{done_id}/delete", data={"_csrf_token": token})
        self.assertIsNone(store.get(done_id))

    def _fresh_client(self):
        client = self.app.test_client()
        client.get("/login")
        with client.session_transaction() as sess:
            return client, sess["_csrf_token"]

    def test_pages_need_sign_in_and_wrong_passwords_are_refused(self):
        client, token = self._fresh_client()
        response = client.get("/notes?q=x")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login?next=/notes?q%3Dx", response.headers["Location"])
        self.assertEqual(client.get("/healthz").status_code, 200)  # Docker's health check stays open.
        # Scripts get an error they can show instead of the sign-in page.
        self.assertEqual(client.post("/chat/send", data={"_csrf_token": token, "message": "hi"}).status_code, 401)

        response = client.post("/login", data={"_csrf_token": token, "username": "admin", "password": "nope"})
        self.assertEqual(response.status_code, 401)
        self.assertIn("Wrong username or password.", response.get_data(as_text=True))

        response = client.post("/login", data={
            "_csrf_token": token, "username": "ADMIN", "password": "admin-password", "next": "/notes",
        })
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["Location"], "/notes")
        page = client.get("/").get_data(as_text=True)
        self.assertIn("<span>admin</span></a>", page)  # Name and Log out in the top bar.
        self.assertIn("ออกจากระบบ", page)

        # Links back to another site are ignored after signing in.
        client2, token2 = self._fresh_client()
        response = client2.post("/login", data={
            "_csrf_token": token2, "username": "admin", "password": "admin-password", "next": "//evil.example/",
        })
        self.assertEqual(response.headers["Location"], "/feed")

        with client.session_transaction() as sess:
            token = sess["_csrf_token"]
        client.post("/logout", data={"_csrf_token": token})
        self.assertEqual(client.get("/").status_code, 302)

    def test_too_many_wrong_passwords_are_slowed_down(self):
        client, token = self._fresh_client()
        for _ in range(8):
            client.post("/login", data={"_csrf_token": token, "username": "admin", "password": "wrong"})
        response = client.post("/login", data={"_csrf_token": token, "username": "admin", "password": "admin-password"})
        self.assertIn("Too many wrong passwords", response.get_data(as_text=True))

    def test_first_visit_creates_the_administrator(self):
        import tempfile as temp

        folder = temp.mkdtemp()
        app = create_app({"TESTING": True, "FILES_ROOT": folder, "NOTES_DATABASE": folder + "/n.sqlite3",
                          "SECRET_KEY": "x"})
        client = app.test_client()
        self.assertEqual(client.get("/").headers["Location"], "/setup")
        with client.session_transaction() as sess:
            token = sess.get("_csrf_token") or ""
        client.get("/setup")
        with client.session_transaction() as sess:
            token = sess["_csrf_token"]
        response = client.post("/setup", data={"_csrf_token": token, "username": "karn", "password": "short", "confirm": "short"})
        self.assertIn("at least 8 characters", response.get_data(as_text=True))
        response = client.post("/setup", data={
            "_csrf_token": token, "username": "karn", "password": "long-enough", "confirm": "long-enough",
        })
        self.assertEqual(response.status_code, 303)
        self.assertTrue(app.extensions["users"].by_name("karn")["is_admin"])
        self.assertEqual(client.get("/").status_code, 200)  # Signed in straight away.
        self.assertEqual(client.get("/setup").status_code, 302)  # Setup can't run twice.

    def test_admin_manages_users_and_people_change_their_password(self):
        token = self._csrf()
        users = self.app.extensions["users"]
        self.client.post("/settings/users", data={"_csrf_token": token, "username": "somchai", "password": "first-pass"})
        somchai = users.by_name("somchai")
        self.assertFalse(somchai["is_admin"])
        self.assertIn("somchai", self.client.get("/settings/users").get_data(as_text=True))

        # A regular user can't open Settings, but can change their own password.
        client, token2 = self._fresh_client()
        client.post("/login", data={"_csrf_token": token2, "username": "somchai", "password": "first-pass"})
        self.assertEqual(client.get("/settings/users").status_code, 403)
        self.assertEqual(client.get("/settings/ai").status_code, 403)
        page = client.get("/settings").get_data(as_text=True)
        self.assertIn("Profile &amp; password", page)
        self.assertNotIn("Administration", page)
        with client.session_transaction() as sess:
            token2 = sess["_csrf_token"]
        response = client.post("/account", data={"_csrf_token": token2, "current": "wrong", "password": "new-pass-1", "confirm": "new-pass-1"})
        self.assertIn("current password is not right", response.get_data(as_text=True))
        client.post("/account", data={"_csrf_token": token2, "current": "first-pass", "password": "new-pass-1", "confirm": "new-pass-1"})

        # The administrator resets it, and can't delete the last administrator (themselves).
        self.client.post(f"/settings/users/{somchai['id']}/password", data={"_csrf_token": token, "password": "reset-pass"})
        from werkzeug.security import check_password_hash
        self.assertTrue(check_password_hash(users.get(somchai["id"])["password_hash"], "reset-pass"))
        self.client.post(f"/settings/users/{self.admin_id}/delete", data={"_csrf_token": token})
        self.assertIsNotNone(users.get(self.admin_id))
        self.client.post(f"/settings/users/{somchai['id']}/delete", data={"_csrf_token": token})
        self.assertIsNone(users.get(somchai["id"]))
        self.assertEqual(client.get("/").status_code, 302)  # A deleted account is signed out.


    def _client_for(self, username, password="member-pass"):
        client, token = self._fresh_client()
        client.post("/login", data={"_csrf_token": token, "username": username, "password": password})
        client.get("/feed")  # Signing in starts a new session, with a new form token.
        with client.session_transaction() as sess:
            return client, sess["_csrf_token"]

    def test_sign_up_waits_for_approval_then_can_be_opened_or_turned_off(self):
        users = self.app.extensions["users"]
        client, token = self._fresh_client()
        self.assertIn("Create an account", client.get("/login").get_data(as_text=True))
        response = client.post("/register", data={
            "_csrf_token": token, "username": "nok", "password": "member-pass", "confirm": "member-pass"})
        self.assertEqual(response.status_code, 303)
        self.assertFalse(users.by_name("nok")["is_active"])
        response = client.post("/login", data={"_csrf_token": token, "username": "nok", "password": "member-pass"})
        self.assertIn("waiting for an administrator", response.get_data(as_text=True))
        self.assertEqual(client.get("/feed").status_code, 302)

        # The administrator is told, and approves the account.
        alerts = self.app.extensions["notifications"].for_user(self.admin_id)
        self.assertIn("nok signed up", alerts[0]["message"])
        admin_token = self._csrf()
        self.assertIn("Waiting for approval", self.client.get("/settings/users").get_data(as_text=True))
        self.client.post(f"/settings/users/{users.by_name('nok')['id']}/approve", data={"_csrf_token": admin_token})
        nok, _ = self._client_for("nok")
        self.assertEqual(nok.get("/feed").status_code, 200)

        # Open sign-up signs people in straight away; "off" hides the page.
        self.client.post("/settings/signup", data={"_csrf_token": admin_token, "mode": "open"})
        client, token = self._fresh_client()
        response = client.post("/register", data={
            "_csrf_token": token, "username": "dao", "password": "member-pass", "confirm": "member-pass"})
        self.assertEqual(response.headers["Location"], "/feed")
        self.assertTrue(users.by_name("dao")["is_active"])
        self.assertEqual(client.get("/register").status_code, 302)  # Already signed in.
        self.client.post("/settings/signup", data={"_csrf_token": admin_token, "mode": "off"})
        client, token = self._fresh_client()
        self.assertNotIn("Create an account", client.get("/login").get_data(as_text=True))
        self.assertEqual(client.get("/register").status_code, 303)

    def test_feed_posts_photos_videos_likes_comments_and_notifications(self):
        from PIL import Image

        users = self.app.extensions["users"]
        notifications = self.app.extensions["notifications"]
        bee_id = users.add("bee", "member-pass")
        bee, bee_token = self._client_for("bee")
        token = self._csrf()

        photo = io.BytesIO()
        Image.new("RGB", (40, 30), "red").save(photo, "PNG")
        video = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64
        response = self.client.post("/feed/posts", data={
            "_csrf_token": token, "body": "Hello @bee, see https://example.com/a?b=1&c=2",
            "media": [(io.BytesIO(photo.getvalue()), "red.png"), (io.BytesIO(video), "clip.mp4")],
        }, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 303)
        page = bee.get("/feed").get_data(as_text=True)
        self.assertIn('<span class="feed-mention">@bee</span>', page)
        self.assertIn('href="https://example.com/a?b=1&amp;c=2"', page)
        media_ids = [int(m) for m in re.findall(r"/feed/media/(\d+)", page)]
        image = bee.get(f"/feed/media/{media_ids[0]}")
        self.assertEqual(image.mimetype, "image/png")
        image.close()
        clip = bee.get(f"/feed/media/{media_ids[-1]}", headers={"Range": "bytes=0-9"})
        self.assertEqual(clip.status_code, 206)  # Videos can seek.
        clip.close()

        # Bee was mentioned (and not told twice); the admin isn't told about their own post.
        alerts = notifications.for_user(bee_id)
        self.assertEqual([a["kind"] for a in alerts], ["mention"])
        self.assertEqual(notifications.for_user(self.admin_id), [])
        self.assertIn('class="topbar-badge" >1<', bee.get("/feed").get_data(as_text=True).replace("\n", ""))

        post_id = alerts[0]["post_id"]
        response = bee.post(f"/feed/posts/{post_id}/like", data={"_csrf_token": bee_token},
                            headers={"X-Requested-With": "fetch"})
        self.assertEqual(response.get_json(), {"liked": True, "count": 1, "likers": ["bee"]})
        bee.post(f"/feed/posts/{post_id}/comments", data={"_csrf_token": bee_token, "body": "Nice <b>photo</b>"})
        page = self.client.get(f"/feed/posts/{post_id}").get_data(as_text=True)
        self.assertIn("Nice &lt;b&gt;photo&lt;/b&gt;", page)
        self.assertEqual(notifications.unread_count(self.admin_id), 0)  # Opening the post read its alerts.
        kinds = [a["kind"] for a in notifications.for_user(self.admin_id)]
        self.assertEqual(sorted(kinds), ["comment", "like"])

        # Reading: open one, mark unread, then read all; the bell polls the count.
        alert = notifications.for_user(bee_id)[0]
        response = bee.get(f"/notifications/{alert['id']}/open")
        self.assertEqual(response.headers["Location"], f"/feed/posts/{post_id}")
        self.assertEqual(notifications.unread_count(bee_id), 0)
        bee.post(f"/notifications/{alert['id']}/read", data={"_csrf_token": bee_token, "read": "0"})
        self.assertEqual(bee.get("/notifications/poll?after=0").get_json()["unread"], 1)
        self.client.post(f"/feed/posts/{post_id}/comments", data={"_csrf_token": token, "body": "Thanks!"})
        poll = bee.get(f"/notifications/poll?after={alert['id']}").get_json()
        self.assertEqual(poll["unread"], 2)
        self.assertIn("admin also commented", poll["fresh"][0]["message"])
        self.assertIn("is-unread", bee.get("/notifications").get_data(as_text=True))
        bee.post("/notifications/read-all", data={"_csrf_token": bee_token})
        self.assertEqual(notifications.unread_count(bee_id), 0)

        # Only the author (or an administrator) deletes a post; its files go with it.
        self.assertEqual(bee.post(f"/feed/posts/{post_id}/delete", data={"_csrf_token": bee_token}).status_code, 403)
        media_dir = Path(self.temp_dir.name) / "feed"
        self.assertEqual(len([f for f in media_dir.iterdir() if f.is_file()]), 2)
        self.client.post(f"/feed/posts/{post_id}/delete", data={"_csrf_token": token})
        self.assertEqual([f for f in media_dir.iterdir() if f.is_file()], [])
        self.assertEqual(notifications.for_user(bee_id), [])

    def test_feed_rejects_fake_and_unsupported_files(self):
        token = self._csrf()
        for name, data in (("fake.jpg", b"not an image"), ("fake.mp4", b"not a video at all"), ("page.svg", b"<svg/>")):
            response = self.client.post("/feed/posts", data={
                "_csrf_token": token, "body": "x", "media": [(io.BytesIO(data), name)],
            }, content_type="multipart/form-data", headers={"X-Requested-With": "fetch"})
            self.assertEqual(response.status_code, 400, name)
        self.assertEqual([f for f in (Path(self.temp_dir.name) / "feed").iterdir() if f.is_file()], [])
        response = self.client.post("/feed/posts", data={"_csrf_token": token, "body": "  "})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.app.extensions["feed"].posts(self.admin_id), [])

    def test_profile_photo_notification_choices_and_editing_posts(self):
        from PIL import Image

        users = self.app.extensions["users"]
        notifications = self.app.extensions["notifications"]
        bee_id = users.add("bee", "member-pass")
        bee, bee_token = self._client_for("bee")
        token = self._csrf()

        # A profile photo is cropped to a square JPEG and shown on the profile page.
        photo = io.BytesIO()
        Image.new("RGB", (300, 200), "green").save(photo, "PNG")
        response = bee.post("/settings/account/profile", data={
            "_csrf_token": bee_token, "bio": "  Design   team ", "avatar": (io.BytesIO(photo.getvalue()), "me.png"),
        }, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 303)
        bee_user = users.get(bee_id)
        self.assertEqual(bee_user["bio"], "Design team")
        avatar = bee.get(f"/avatars/{bee_user['avatar']}")
        self.assertEqual(Image.open(io.BytesIO(avatar.data)).size, (320, 320))
        avatar.close()
        page = self.client.get("/feed/u/BEE").get_data(as_text=True)
        self.assertIn("Design team", page)
        self.assertIn(f"/avatars/{bee_user['avatar']}", page)
        self.assertEqual(bee.get("/avatars/../app.py").status_code, 404)

        # Bee turns off alerts for new posts but still hears about mentions.
        bee.post("/settings/notifications", data={
            "_csrf_token": bee_token, "kind_mention": "1", "kind_comment": "1", "kind_like": "1", "popups": "1"})
        self.client.post("/feed/posts", data={"_csrf_token": token, "body": "Lunch is here"})
        self.assertEqual(notifications.for_user(bee_id), [])
        self.client.post("/feed/posts", data={"_csrf_token": token, "body": "@bee come too"})
        self.assertEqual([a["kind"] for a in notifications.for_user(bee_id)], ["mention"])
        self.assertIn("3 of 7 kinds on", bee.get("/settings").get_data(as_text=True))

        # Only the author edits a post; the reply has the new text ready to show.
        post_id = self.app.extensions["feed"].posts(self.admin_id)[0]["id"]
        response = bee.post(f"/feed/posts/{post_id}/edit", data={"_csrf_token": bee_token, "body": "hacked"},
                            headers={"X-Requested-With": "fetch"})
        self.assertEqual(response.status_code, 400)
        response = self.client.post(f"/feed/posts/{post_id}/edit", data={"_csrf_token": token, "body": "@bee see https://x.io"},
                                    headers={"X-Requested-With": "fetch"})
        self.assertIn('<span class="feed-mention">@bee</span>', response.get_json()["html"])

        # Comments sent from the page come back as HTML; more posts load as you scroll.
        response = bee.post(f"/feed/posts/{post_id}/comments", data={"_csrf_token": bee_token, "body": "On my way"},
                            headers={"X-Requested-With": "fetch"})
        self.assertIn("On my way", response.get_json()["html"])
        partial = bee.get(f"/feed?before={post_id + 1}&partial=1").get_data(as_text=True)
        self.assertIn(f'id="post-{post_id}"', partial)
        self.assertNotIn("<html", partial)

        # Administrators can make someone an administrator, but can't demote themselves.
        self.client.post(f"/settings/users/{bee_id}/admin", data={"_csrf_token": token, "is_admin": "1"})
        self.assertTrue(users.get(bee_id)["is_admin"])
        self.client.post(f"/settings/users/{self.admin_id}/admin", data={"_csrf_token": token, "is_admin": "0"})
        self.assertTrue(users.get(self.admin_id)["is_admin"])

        # Deleting the account removes the profile photo too.
        self.client.post(f"/settings/users/{bee_id}/delete", data={"_csrf_token": token})
        self.assertFalse((Path(self.temp_dir.name) / "feed" / "avatars" / bee_user["avatar"]).exists())

if __name__ == "__main__":
    unittest.main()
