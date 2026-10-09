"""File conversions used by the /convert page.

Each function reads input files from a private working directory and returns
the path of the result inside that directory. Failures the user can fix are
raised as ConversionError with a readable message.
"""

import shutil
import subprocess
import zipfile
from pathlib import Path


class ConversionError(Exception):
    pass


WORD_EXTENSIONS = {".doc", ".docx", ".odt", ".rtf"}
PDF_EXTENSIONS = {".pdf"}
IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".heic", ".heif",
}

MAX_IMAGES = 100
MAX_PDF_PAGES = 300
PAGE_SIZES_MM = {"a4": (210, 297), "letter": (215.9, 279.4)}


def word_to_pdf(source, workdir):
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        raise ConversionError(
            "Word to PDF needs LibreOffice, which is not installed on this server. "
            "Run the app with Docker to use it."
        )
    out_dir = workdir / "out"
    # A private profile per conversion lets several conversions run at once.
    profile = (workdir / "lo-profile").as_uri()
    try:
        subprocess.run(
            [
                soffice,
                f"-env:UserInstallation={profile}",
                "--headless",
                "--norestore",
                "--convert-to",
                "pdf",
                "--outdir",
                str(out_dir),
                str(source),
            ],
            check=True,
            capture_output=True,
            timeout=180,
        )
    except subprocess.TimeoutExpired:
        raise ConversionError("The document took too long to convert.")
    except subprocess.CalledProcessError:
        raise ConversionError("LibreOffice could not convert this document.")
    result = out_dir / f"{source.stem}.pdf"
    if not result.exists():
        raise ConversionError("LibreOffice could not convert this document.")
    return result


def open_pdf(source):
    import pymupdf

    try:
        document = pymupdf.open(source)
    except Exception:
        raise ConversionError("This file is not a readable PDF.")
    if document.needs_pass:
        document.close()
        raise ConversionError("Password-protected PDFs are not supported.")
    if document.page_count == 0:
        document.close()
        raise ConversionError("This PDF has no pages.")
    return document


def pdf_to_word(source, workdir):
    from pdf2docx import Converter

    open_pdf(source).close()
    result = workdir / f"{source.stem}.docx"
    try:
        converter = Converter(str(source))
        try:
            converter.convert(str(result))
        finally:
            converter.close()
    except Exception:
        raise ConversionError("Could not convert this PDF to Word.")
    return result


def images_to_pdf(sources, workdir, page_size="a4"):
    import img2pdf
    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        from pillow_heif import register_heif_opener

        register_heif_opener()
    except ImportError:
        pass

    if len(sources) > MAX_IMAGES:
        raise ConversionError(f"Choose at most {MAX_IMAGES} images at a time.")

    # img2pdf embeds JPEG and PNG without re-encoding; other formats, transparency,
    # and rotated phone photos are normalised with Pillow first.
    prepared = []
    for index, source in enumerate(sources):
        try:
            with Image.open(source) as image:
                image_format = image.format
                rotated = image.getexif().get(0x0112, 1) not in (1, None)
                has_alpha = image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info
                if image_format in ("JPEG", "PNG") and not rotated and not has_alpha and image.mode != "P":
                    prepared.append(str(source))
                    continue
                image = ImageOps.exif_transpose(image)
                if image.mode in ("RGBA", "LA", "PA", "P"):
                    image = image.convert("RGBA")
                    background = Image.new("RGB", image.size, "white")
                    background.paste(image, mask=image.getchannel("A"))
                    image = background
                elif image.mode not in ("RGB", "L"):
                    image = image.convert("RGB")
                target = workdir / f"prepared-{index:03d}.png"
                image.save(target, "PNG")
                prepared.append(str(target))
        except (UnidentifiedImageError, Image.DecompressionBombError, OSError):
            raise ConversionError(f"“{source.name}” is not a supported image.")

    if page_size in PAGE_SIZES_MM:
        width, height = PAGE_SIZES_MM[page_size]
        layout = img2pdf.get_layout_fun(
            (img2pdf.mm_to_pt(width), img2pdf.mm_to_pt(height)),
            fit=img2pdf.FitMode.into,
            auto_orient=True,
        )
    else:
        layout = None

    result = workdir / "images.pdf"
    try:
        result.write_bytes(img2pdf.convert(prepared, layout_fun=layout))
    except Exception:
        raise ConversionError("Could not combine these images into a PDF.")
    return result


def pdf_to_images(source, workdir, image_format="png", dpi=150, name=None):
    stem = name or source.stem
    document = open_pdf(source)
    try:
        if document.page_count > MAX_PDF_PAGES:
            raise ConversionError(f"PDFs with more than {MAX_PDF_PAGES} pages are not supported.")
        extension = "jpg" if image_format == "jpg" else "png"
        out_dir = workdir / "pages"
        out_dir.mkdir()
        pages = []
        digits = max(3, len(str(document.page_count)))
        for number, page in enumerate(document, start=1):
            pixmap = page.get_pixmap(dpi=dpi, alpha=False)
            target = out_dir / f"{stem}-page-{number:0{digits}d}.{extension}"
            if extension == "jpg":
                pixmap.save(target, jpg_quality=90)
            else:
                pixmap.save(target)
            pages.append(target)
    finally:
        document.close()

    if len(pages) == 1:
        return pages[0]
    result = workdir / f"{stem}-images.zip"
    with zipfile.ZipFile(result, "w", zipfile.ZIP_STORED) as archive:
        for page in pages:
            archive.write(page, page.name)
    return result
