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
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/?path=../outside").status_code, 400)

    def test_update_log_page_lists_versions_and_changes(self):
        response = self.client.get("/updates")
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

    def test_pages_include_mobile_viewport(self):
        for url in ("/", "/updates", "/notes"):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertIn(
                    b'<meta name="viewport" content="width=device-width, initial-scale=1',
                    response.data,
                )

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
        self.assertEqual(store.conversations(), [])

        message["from"]["id"] = 111
        with mock.patch.object(chat, "complete_reply", return_value="Hi from Claude") as complete:
            telegram_bot.handle(telegram, store, message, allowed={111})
        self.assertTrue(complete.call_args.kwargs["for_telegram"])
        telegram.send.assert_called_with(5, "Hi from Claude")
        conversation_id = store.latest_id()
        self.assertEqual(
            [(m["role"], m["source"]) for m in store.messages(conversation_id)],
            [("user", "telegram"), ("assistant", "telegram")],
        )

        telegram_bot.handle(telegram, store, {**message, "text": "/new"}, allowed={111})
        self.assertNotEqual(store.latest_id(), conversation_id)

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
        conversation_id = store.create()
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

    def test_healthz_reports_ok(self):
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"status": "ok"})

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
        self.assertIn("Log out", page)

        # Links back to another site are ignored after signing in.
        client2, token2 = self._fresh_client()
        response = client2.post("/login", data={
            "_csrf_token": token2, "username": "admin", "password": "admin-password", "next": "//evil.example/",
        })
        self.assertEqual(response.headers["Location"], "/")

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
        self.assertIn("3 of 5 kinds on", bee.get("/settings").get_data(as_text=True))

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
