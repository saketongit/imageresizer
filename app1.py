"""
Book Cover Pipeline - Local Web UI (v1 with Previews, Batch Archiving & Original Filename Preservation)

A Flask app that wraps the cover-processing steps behind an interactive browser UI:
    - 2-part combine : back 1.2cm + front 1.0cm  = 2.2cm x 1.6cm
    - 3-part combine : back 1.0cm + spine 0.2cm + front 1.0cm = 2.2cm x 1.6cm
    - full cover resize : forced to 2.2cm x 1.6cm, 1016 DPI, sharpened PNG
    - final PDF : 12in x 18in grid, sRGB -> CMYK (FOGRA39) via ICC profiles

Features:
    - Retains original input filenames (and smart clean names for 2-part/3-part)
    - Thumbnail preview gallery for all queued covers
    - Single-item deletion and section clearing
    - Completed Batches: Automatically archives covers with clean filenames + PDF into timestamped folders upon PDF generation and resets staging folders.
"""

import io
import re
import shutil
from datetime import datetime
from pathlib import Path

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    send_file,
    send_from_directory,
)
from PIL import Image, ImageFilter, ImageCms
from reportlab.lib.units import cm, inch
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

TWOPART_OUT = DATA_DIR / "2part_output"
THREEPART_OUT = DATA_DIR / "3part_output"
FULLCOVER_OUT = DATA_DIR / "fullcover_output"
FINAL_PDF_DIR = DATA_DIR / "final_pdf"
ICC_DIR = DATA_DIR / "icc_profiles"
COMPLETED_BATCHES_DIR = DATA_DIR / "completed_batches"

for d in (TWOPART_OUT, THREEPART_OUT, FULLCOVER_OUT, FINAL_PDF_DIR, ICC_DIR, COMPLETED_BATCHES_DIR):
    d.mkdir(parents=True, exist_ok=True)

SRGB_PROFILE_PATH = ICC_DIR / "sRGB.icc"
CMYK_PROFILE_PATH = ICC_DIR / "FOGRA39L_coated.icc"

app = Flask(__name__, template_folder="templates1")
app.secret_key = "book-cover-pipeline-v1"

# ---- Shared sizing constants (cm) ----
HEIGHT_CM = 1.6
FULL_WIDTH_CM = 2.2
FRONT_WIDTH_CM = 1.0
BACK_WIDTH_CM_3PART = 1.0
SPINE_WIDTH_CM = 0.2
BACK_WIDTH_CM_2PART = 1.2

DPI_COMBINE = 300      # matches combine_split_images(_2part).py
DPI_FULLCOVER = 1016   # matches resize_book.py

SHARPEN_RADIUS = 0.6
SHARPEN_PERCENT = 60
SHARPEN_THRESHOLD = 2


def cm_to_px(value_cm: float, dpi: int) -> int:
    return round(value_cm / 2.54 * dpi)


def sanitize_filename(name: str) -> str:
    """Strip illegal characters for Windows/cross-platform filenames."""
    cleaned = re.sub(r'[\\/*?:"<>|]', "", name).strip()
    return cleaned or "cover"


def derive_book_name(front_filename: str, custom_name: str | None = None) -> str:
    """
    Derive a clean book name from the front image filename or custom input,
    stripping common suffixes like _front, -front, _f, _cover, etc.
    """
    if custom_name and custom_name.strip():
        return sanitize_filename(custom_name.strip())

    stem = Path(front_filename).stem
    # Remove common suffixes like _front, -front, _f, -f, _cover, _side, _part1 etc.
    cleaned = re.sub(r"(?i)[_\-\s]+(front|f|side|part[_\-\s]*\d+|cover)$", "", stem).strip()
    cleaned = sanitize_filename(cleaned)
    return cleaned or stem


def get_unique_filepath(folder: Path, base_name: str, extension: str) -> Path:
    """Ensure non-colliding filename inside the target folder."""
    base_name = sanitize_filename(base_name)
    if not extension.startswith("."):
        extension = f".{extension}"

    target = folder / f"{base_name}{extension}"
    if not target.exists():
        return target

    counter = 1
    while True:
        target = folder / f"{base_name}_{counter}{extension}"
        if not target.exists():
            return target
        counter += 1


def natural_sort_key(s: str):
    """Natural alphanumeric sort key (e.g. 1, 2, 10 instead of 1, 10, 2)."""
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r"(\d+)", s)]


def list_images(folder: Path, pattern: str = "*.*") -> list[str]:
    """Return filenames sorted naturally."""
    files = [f for f in folder.glob(pattern) if f.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}]
    files.sort(key=lambda p: natural_sort_key(p.name))
    return [f.name for f in files]


def get_recent_batches(limit: int = 5) -> list[dict]:
    """Return recent completed batch folders with summary metadata."""
    batches = []
    if not COMPLETED_BATCHES_DIR.exists():
        return batches

    for b in sorted(COMPLETED_BATCHES_DIR.iterdir(), reverse=True):
        if b.is_dir() and b.name.startswith("Batch_"):
            pdf_file = b / "cover_sheet_cmyk.pdf"
            image_count = len(list((b / "images").glob("*.*"))) if (b / "images").exists() else 0
            batches.append({
                "name": b.name,
                "path": str(b),
                "pdf_exists": pdf_file.exists(),
                "image_count": image_count,
            })
            if len(batches) >= limit:
                break
    return batches


def load_part(file_storage, width_cm, height_cm):
    img = Image.open(file_storage.stream).convert("RGB")
    w_px = cm_to_px(width_cm, DPI_COMBINE)
    h_px = cm_to_px(height_cm, DPI_COMBINE)
    return img.resize((w_px, h_px), Image.Resampling.LANCZOS), w_px, h_px


@app.context_processor
def inject_queue_info():
    """Make total queue count and batch folder available in all templates."""
    c2 = len(list_images(TWOPART_OUT))
    c3 = len(list_images(THREEPART_OUT))
    cf = len(list_images(FULLCOVER_OUT))
    return {
        "total_queued": c2 + c3 + cf,
        "completed_batches_dir": str(COMPLETED_BATCHES_DIR),
    }


# -------------------------------------------------------------- ASSETS & DELETION ----
@app.route("/thumb/<folder>/<filename>")
def thumbnail(folder, filename):
    allowed_folders = {
        "2part_output": TWOPART_OUT,
        "3part_output": THREEPART_OUT,
        "fullcover_output": FULLCOVER_OUT,
    }
    target_dir = allowed_folders.get(folder)
    if not target_dir:
        return "Folder not found", 404
    return send_from_directory(target_dir, filename)


@app.route("/delete/<folder>/<filename>", methods=["POST"])
def delete_file(folder, filename):
    allowed_folders = {
        "2part_output": TWOPART_OUT,
        "3part_output": THREEPART_OUT,
        "fullcover_output": FULLCOVER_OUT,
    }
    target_dir = allowed_folders.get(folder)
    if target_dir:
        file_path = target_dir / filename
        if file_path.exists():
            file_path.unlink()
            flash(f"Deleted '{filename}'.", "success")
        else:
            flash(f"File '{filename}' not found.", "error")
    return redirect(request.referrer or url_for("index"))


@app.route("/clear_section/<folder>", methods=["POST"])
def clear_section(folder):
    allowed_folders = {
        "2part_output": TWOPART_OUT,
        "3part_output": THREEPART_OUT,
        "fullcover_output": FULLCOVER_OUT,
    }
    if folder in allowed_folders:
        target_dir = allowed_folders[folder]
        count = 0
        for f in list(target_dir.iterdir()):
            if f.is_file():
                try:
                    f.unlink()
                    count += 1
                except Exception:
                    pass
        flash(f"Cleared {count} item(s) from {folder}.", "success")
    return redirect(request.referrer or url_for("index"))


@app.route("/clear_all", methods=["POST"])
def clear_all():
    count = 0
    for folder in (TWOPART_OUT, THREEPART_OUT, FULLCOVER_OUT):
        for f in list(folder.iterdir()):
            if f.is_file():
                try:
                    f.unlink()
                    count += 1
                except Exception:
                    pass
    flash(f"Cleared entire batch ({count} cover(s) removed).", "success")
    return redirect(request.referrer or url_for("index"))


# -------------------------------------------------------------- HOME ----
@app.route("/")
def index():
    counts = {
        "twopart": len(list_images(TWOPART_OUT)),
        "threepart": len(list_images(THREEPART_OUT)),
        "fullcover": len(list_images(FULLCOVER_OUT)),
    }
    icc_ready = SRGB_PROFILE_PATH.exists() and CMYK_PROFILE_PATH.exists()
    recent_batches = get_recent_batches(limit=5)
    return render_template(
        "index.html",
        counts=counts,
        icc_ready=icc_ready,
        icc_folder=ICC_DIR,
        recent_batches=recent_batches,
    )


# ---------------------------------------------------------------- 2-PART ----
@app.route("/twopart", methods=["GET", "POST"])
def twopart():
    if request.method == "POST":
        back = request.files.get("back")
        front = request.files.get("front")
        custom_name = request.form.get("book_name")
        if not back or not front or back.filename == "" or front.filename == "":
            flash("Please choose both a back and a front image.", "error")
            return redirect(url_for("twopart"))

        h_px = cm_to_px(HEIGHT_CM, DPI_COMBINE)
        back_img, back_w, _ = load_part(back, BACK_WIDTH_CM_2PART, HEIGHT_CM)
        front_img, front_w, _ = load_part(front, FRONT_WIDTH_CM, HEIGHT_CM)

        total_w = back_w + front_w
        combined = Image.new("RGB", (total_w, h_px), "white")
        combined.paste(back_img, (0, 0))
        combined.paste(front_img, (back_w, 0))

        base_name = derive_book_name(front.filename, custom_name)
        out_path = get_unique_filepath(TWOPART_OUT, base_name, ".jpg")
        combined.save(out_path, dpi=(DPI_COMBINE, DPI_COMBINE), quality=95)

        flash(f"Saved 2-part cover: '{out_path.name}'", "success")
        return redirect(url_for("twopart"))

    images = list_images(TWOPART_OUT)
    return render_template("twopart.html", images=images, out_folder=TWOPART_OUT)


# -------------------------------------------------------------- 3-PART -----
@app.route("/threepart", methods=["GET", "POST"])
def threepart():
    if request.method == "POST":
        back = request.files.get("back")
        front = request.files.get("front")
        spine = request.files.get("spine")
        custom_name = request.form.get("book_name")
        if not all([back, front, spine]) or "" in (back.filename, front.filename, spine.filename):
            flash("Please choose a back, front, AND spine image.", "error")
            return redirect(url_for("threepart"))

        h_px = cm_to_px(HEIGHT_CM, DPI_COMBINE)
        back_img, back_w, _ = load_part(back, BACK_WIDTH_CM_3PART, HEIGHT_CM)
        spine_img, spine_w, _ = load_part(spine, SPINE_WIDTH_CM, HEIGHT_CM)
        front_img, front_w, _ = load_part(front, FRONT_WIDTH_CM, HEIGHT_CM)

        total_w = back_w + spine_w + front_w
        combined = Image.new("RGB", (total_w, h_px), "white")
        x = 0
        combined.paste(back_img, (x, 0))
        x += back_w
        combined.paste(spine_img, (x, 0))
        x += spine_w
        combined.paste(front_img, (x, 0))

        base_name = derive_book_name(front.filename, custom_name)
        out_path = get_unique_filepath(THREEPART_OUT, base_name, ".jpg")
        combined.save(out_path, dpi=(DPI_COMBINE, DPI_COMBINE), quality=95)

        flash(f"Saved 3-part cover: '{out_path.name}'", "success")
        return redirect(url_for("threepart"))

    images = list_images(THREEPART_OUT)
    return render_template("threepart.html", images=images, out_folder=THREEPART_OUT)


# -------------------------------------------------------------- FULL COVER -
@app.route("/fullcover", methods=["GET", "POST"])
def fullcover():
    if request.method == "POST":
        files = [f for f in request.files.getlist("covers") if f and f.filename]
        if not files:
            flash("Please choose at least one image.", "error")
            return redirect(url_for("fullcover"))

        w_px = cm_to_px(FULL_WIDTH_CM, DPI_FULLCOVER)
        h_px = cm_to_px(HEIGHT_CM, DPI_FULLCOVER)

        saved = 0
        for f in files:
            try:
                img = Image.open(f.stream)
                icc_profile = img.info.get("icc_profile")
                if img.mode not in ("RGBA", "RGB"):
                    img = img.convert("RGBA" if "A" in img.mode or img.mode == "P" else "RGB")
                resized = img.resize((w_px, h_px), Image.Resampling.LANCZOS)
                resized = resized.filter(
                    ImageFilter.UnsharpMask(
                        radius=SHARPEN_RADIUS,
                        percent=SHARPEN_PERCENT,
                        threshold=SHARPEN_THRESHOLD,
                    )
                )

                orig_stem = Path(f.filename).stem
                out_path = get_unique_filepath(FULLCOVER_OUT, orig_stem, ".png")
                save_kwargs = {"dpi": (DPI_FULLCOVER, DPI_FULLCOVER), "compress_level": 0}
                if icc_profile:
                    save_kwargs["icc_profile"] = icc_profile
                resized.save(out_path, **save_kwargs)
                saved += 1
            except Exception as e:
                flash(f"Failed on {f.filename}: {e}", "error")

        flash(f"{saved} cover(s) resized and saved.", "success")
        return redirect(url_for("fullcover"))

    images = list_images(FULLCOVER_OUT)
    return render_template("fullcover.html", images=images, out_folder=FULLCOVER_OUT)


# -------------------------------------------------------------- PDF --------
@app.route("/pdf", methods=["GET", "POST"])
def pdf():
    twopart_imgs = list_images(TWOPART_OUT)
    threepart_imgs = list_images(THREEPART_OUT)
    fullcover_imgs = list_images(FULLCOVER_OUT)

    all_image_paths = (
        [TWOPART_OUT / name for name in twopart_imgs]
        + [THREEPART_OUT / name for name in threepart_imgs]
        + [FULLCOVER_OUT / name for name in fullcover_imgs]
    )
    icc_ready = SRGB_PROFILE_PATH.exists() and CMYK_PROFILE_PATH.exists()
    recent_batches = get_recent_batches(limit=5)

    if request.method == "POST":
        if not icc_ready:
            flash(
                f"Missing ICC profiles. Place sRGB.icc and FOGRA39L_coated.icc in {ICC_DIR}",
                "error",
            )
            return redirect(url_for("pdf"))
        if not all_image_paths:
            flash("No covers found yet. Add some via the steps above first.", "error")
            return redirect(url_for("pdf"))

        # 1. Create Timestamped Batch Directory
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        batch_dir = COMPLETED_BATCHES_DIR / f"Batch_{timestamp}"
        batch_images_dir = batch_dir / "images"
        batch_dir.mkdir(parents=True, exist_ok=True)
        batch_images_dir.mkdir(parents=True, exist_ok=True)

        # 2. Build PDF directly into the batch folder (and also copy to final_pdf)
        batch_pdf_path = batch_dir / "cover_sheet_cmyk.pdf"
        build_pdf(all_image_paths, batch_pdf_path)
        shutil.copyfile(batch_pdf_path, FINAL_PDF_DIR / "cover_sheet_cmyk.pdf")

        # 3. Move all processed covers into the batch folder preserving original/clean names
        def move_preserving_name(src_folder: Path, filename: str):
            src = src_folder / filename
            stem = Path(filename).stem
            ext = Path(filename).suffix
            dest = get_unique_filepath(batch_images_dir, stem, ext)
            shutil.move(str(src), str(dest))

        for name in twopart_imgs:
            move_preserving_name(TWOPART_OUT, name)

        for name in threepart_imgs:
            move_preserving_name(THREEPART_OUT, name)

        for name in fullcover_imgs:
            move_preserving_name(FULLCOVER_OUT, name)

        flash(
            f"Batch completed! PDF & {len(all_image_paths)} cover images saved to {batch_dir}. Staging folders are now clean.",
            "success",
        )

        return send_file(
            batch_pdf_path,
            as_attachment=True,
            download_name=f"cover_sheet_{timestamp}.pdf",
        )

    return render_template(
        "pdf.html",
        twopart_imgs=twopart_imgs,
        threepart_imgs=threepart_imgs,
        fullcover_imgs=fullcover_imgs,
        icc_ready=icc_ready,
        icc_folder=ICC_DIR,
        recent_batches=recent_batches,
    )


def build_pdf(image_paths, output_pdf_path: Path) -> Path:
    PAGE_WIDTH = 12 * inch
    PAGE_HEIGHT = 18 * inch
    MARGIN = 1 * cm
    IMG_W = FULL_WIDTH_CM * cm
    IMG_H = HEIGHT_CM * cm
    GAP = 0.2 * cm
    BG_CMYK = (0, 0, 22, 20)

    srgb_profile = ImageCms.getOpenProfile(str(SRGB_PROFILE_PATH))
    cmyk_profile = ImageCms.getOpenProfile(str(CMYK_PROFILE_PATH))

    def to_cmyk_reader(path):
        with Image.open(path) as img:
            img = img.convert("RGB")
            cmyk_img = ImageCms.profileToProfile(
                img,
                srgb_profile,
                cmyk_profile,
                renderingIntent=ImageCms.Intent.PERCEPTUAL,
                outputMode="CMYK",
            )
            buf = io.BytesIO()
            cmyk_img.save(buf, format="JPEG", quality=95)
            buf.seek(0)
            return ImageReader(buf)

    readers = [to_cmyk_reader(p) for p in image_paths]

    usable_w = PAGE_WIDTH - 2 * MARGIN
    usable_h = PAGE_HEIGHT - 2 * MARGIN
    cols = int((usable_w + GAP) // (IMG_W + GAP))
    rows = int((usable_h + GAP) // (IMG_H + GAP))
    slots_per_page = cols * rows
    total_unique = len(readers)
    total_pages = max(1, -(-total_unique // slots_per_page))

    c = canvas.Canvas(str(output_pdf_path), pagesize=(PAGE_WIDTH, PAGE_HEIGHT))

    idx = 0
    for _ in range(total_pages):
        c.setFillColorCMYK(*BG_CMYK)
        c.rect(0, 0, PAGE_WIDTH, PAGE_HEIGHT, fill=1, stroke=0)
        for row in range(rows):
            for col in range(cols):
                reader = readers[idx % total_unique]
                idx += 1
                x = MARGIN + col * (IMG_W + GAP)
                y = PAGE_HEIGHT - MARGIN - IMG_H - row * (IMG_H + GAP)
                c.drawImage(reader, x, y, width=IMG_W, height=IMG_H, preserveAspectRatio=False)
        c.showPage()

    c.save()
    return output_pdf_path


if __name__ == "__main__":
    app.run(debug=True)
