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
import json

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
from reportlab import rl_config
from reportlab.lib.units import cm, inch
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

# Write binary JPEG streams directly (faster & ~20% smaller PDF)
rl_config.useA85 = 0

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
# ---- Shared PDF sheet constants (used by build_pdf AND the custom layout) ----
PAGE_WIDTH = 12 * inch
PAGE_HEIGHT = 18 * inch
MARGIN = 0.9 * cm
IMG_W = FULL_WIDTH_CM * cm
IMG_H = HEIGHT_CM * cm
GAP = 0.2 * cm
BG_CMYK = (0, 0, 0.22, 0.20)


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
    grid_w = cols * IMG_W + (cols - 1) * GAP
    x_offset = (PAGE_WIDTH - grid_w) / 2
    grid_h = rows * IMG_H + (rows - 1) * GAP
    y_offset = (PAGE_HEIGHT - grid_h) / 2
    total_unique = len(readers)
    total_pages = max(1, -(-total_unique // slots_per_page))


    c = canvas.Canvas(str(output_pdf_path), pagesize=(PAGE_WIDTH, PAGE_HEIGHT))

    # Register each unique cover once as a reusable Form XObject
    for i, reader in enumerate(readers):
        c.beginForm(f"cover_{i}", 0, 0, IMG_W, IMG_H)
        c.drawImage(reader, 0, 0, width=IMG_W, height=IMG_H, preserveAspectRatio=False)
        c.endForm()

    idx = 0
    for _ in range(total_pages):
        c.setFillColorCMYK(*BG_CMYK)
        c.rect(0, 0, PAGE_WIDTH, PAGE_HEIGHT, fill=1, stroke=0)
        for row in range(rows):
            for col in range(cols):
                form_name = f"cover_{idx % total_unique}"
                idx += 1
                x = x_offset + col * (IMG_W + GAP)
                y = PAGE_HEIGHT - y_offset - IMG_H - row * (IMG_H + GAP)
                c.saveState()
                c.translate(x, y)
                c.doForm(form_name)
                c.restoreState()
        c.showPage()

    c.save()
    return output_pdf_path

# ============================================================ CUSTOM LAYOUT ====
STAGING_FOLDERS = {
    "2part_output": TWOPART_OUT,
    "3part_output": THREEPART_OUT,
    "fullcover_output": FULLCOVER_OUT,
}
FOLDER_LABELS = {
    "2part_output": "2-part",
    "3part_output": "3-part",
    "fullcover_output": "Full cover",
}
CUSTOM_LAYOUT_PATH = DATA_DIR / "custom_layout.json"
MAP_WIDTH_PX = 360


def grid_dimensions() -> tuple[int, int]:
    """(columns, rows) that fit on the sheet. Same maths as build_pdf."""
    usable_w = PAGE_WIDTH - 2 * MARGIN
    usable_h = PAGE_HEIGHT - 2 * MARGIN
    cols = int((usable_w + GAP) // (IMG_W + GAP))
    rows = int((usable_h + GAP) // (IMG_H + GAP))
    return cols, rows


def default_custom_layout() -> dict:
    return {"mode": "rows", "placements": []}


def load_custom_layout() -> dict:
    try:
        data = json.loads(CUSTOM_LAYOUT_PATH.read_text(encoding="utf-8"))
        if data.get("mode") in ("rows", "columns") and isinstance(data.get("placements"), list):
            return data
    except (OSError, ValueError):
        pass
    return default_custom_layout()


def save_custom_layout(layout: dict) -> None:
    CUSTOM_LAYOUT_PATH.write_text(json.dumps(layout, indent=2), encoding="utf-8")


def prune_custom_layout(layout: dict) -> int:
    """Drop placements whose cover file no longer exists. Returns how many were removed."""
    kept = [
        p for p in layout["placements"]
        if p.get("folder") in STAGING_FOLDERS
        and (STAGING_FOLDERS[p["folder"]] / p.get("file", "")).is_file()
    ]
    removed = len(layout["placements"]) - len(kept)
    if removed:
        layout["placements"] = kept
        save_custom_layout(layout)
    return removed


def list_staged_covers() -> list[dict]:
    """All currently staged covers, in the same order the normal PDF uses."""
    covers = []
    for folder_key, folder_path in STAGING_FOLDERS.items():
        for name in list_images(folder_path):
            covers.append({
                "folder": folder_key,
                "file": name,
                "key": f"{folder_key}|{name}",
                "type_label": FOLDER_LABELS[folder_key],
            })
    return covers


def css_safe_url(url: str) -> str:
    """Make a URL safe inside CSS url('...')."""
    return url.replace("'", "%27").replace("(", "%28").replace(")", "%29")


def build_custom_pdf(layout: dict, fill_empty: str, output_pdf_path: Path) -> Path:
    cols, rows = grid_dimensions()
    mode = layout["mode"]

    # grid[row][col] -> cover path (or None = empty)
    grid = [[None] * cols for _ in range(rows)]
    for p in layout["placements"]:
        path = STAGING_FOLDERS[p["folder"]] / p["file"]
        if mode == "rows":
            for r in range(p["start"] - 1, p["end"]):
                for c in range(cols):
                    grid[r][c] = path
        else:
            for c in range(p["start"] - 1, p["end"]):
                for r in range(rows):
                    grid[r][c] = path

    if fill_empty == "cycle":
        staged_paths = [STAGING_FOLDERS[cv["folder"]] / cv["file"] for cv in list_staged_covers()]
        i = 0
        for r in range(rows):
            for c in range(cols):
                if grid[r][c] is None and staged_paths:
                    grid[r][c] = staged_paths[i % len(staged_paths)]
                    i += 1

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

    pdf = canvas.Canvas(str(output_pdf_path), pagesize=(PAGE_WIDTH, PAGE_HEIGHT))

    # Register each unique cover once as a reusable Form XObject
    form_names = {}
    for row in grid:
        for path in row:
            if path is not None and path not in form_names:
                name = f"custom_cover_{len(form_names)}"
                form_names[path] = name
                pdf.beginForm(name, 0, 0, IMG_W, IMG_H)
                pdf.drawImage(to_cmyk_reader(path), 0, 0, width=IMG_W, height=IMG_H, preserveAspectRatio=False)
                pdf.endForm()

    grid_w = cols * IMG_W + (cols - 1) * GAP
    grid_h = rows * IMG_H + (rows - 1) * GAP
    x_offset = (PAGE_WIDTH - grid_w) / 2
    y_offset = (PAGE_HEIGHT - grid_h) / 2

    pdf.setFillColorCMYK(*BG_CMYK)
    pdf.rect(0, 0, PAGE_WIDTH, PAGE_HEIGHT, fill=1, stroke=0)
    for r in range(rows):
        for c in range(cols):
            path = grid[r][c]
            if path is None:
                continue
            x = x_offset + c * (IMG_W + GAP)
            y = PAGE_HEIGHT - y_offset - IMG_H - r * (IMG_H + GAP)
            pdf.saveState()
            pdf.translate(x, y)
            pdf.doForm(form_names[path])
            pdf.restoreState()
    pdf.showPage()
    pdf.save()
    return output_pdf_path


@app.route("/custom")
def custom():
    layout = load_custom_layout()
    removed = prune_custom_layout(layout)
    if removed:
        flash(f"{removed} placement(s) were removed because their cover is no longer staged.", "error")

    cols, rows = grid_dimensions()
    mode = layout["mode"]
    limit = rows if mode == "rows" else cols
    staged = list_staged_covers()

    placed = {}
    blocks = []
    assigned_cells = 0
    for i, p in enumerate(layout["placements"]):
        n = p["end"] - p["start"] + 1
        if mode == "rows":
            top, height = (p["start"] - 1) / rows * 100, n / rows * 100
            left, width = 0, 100
            bg_w, bg_h = 100 / cols, 100 / n
            copies = n * cols
        else:
            left, width = (p["start"] - 1) / cols * 100, n / cols * 100
            top, height = 0, 100
            bg_w, bg_h = 100 / n, 100 / rows
            copies = n * rows
        assigned_cells += copies
        key = f"{p['folder']}|{p['file']}"
        placed.setdefault(key, []).append(f"{p['start']}-{p['end']}")
        blocks.append({
            "index": i,
            "folder": p["folder"],
            "file": p["file"],
            "url": css_safe_url(url_for("thumbnail", folder=p["folder"], filename=p["file"])),
            "start": p["start"],
            "end": p["end"],
            "copies": copies,
            "top": round(top, 4), "left": round(left, 4),
            "width": round(width, 4), "height": round(height, 4),
            "bg_w": round(bg_w, 4), "bg_h": round(bg_h, 4),
        })

    map_h = round(MAP_WIDTH_PX * (rows * (IMG_H + GAP)) / (cols * (IMG_W + GAP)))
    return render_template(
        "custom.html",
        staged=staged,
        placed=placed,
        blocks=blocks,
        mode=mode,
        locked=bool(layout["placements"]),
        unit_title="Rows" if mode == "rows" else "Columns",
        unit_lower="rows" if mode == "rows" else "columns",
        limit=limit,
        cols=cols,
        rows=rows,
        map_w=MAP_WIDTH_PX,
        map_h=map_h,
        assigned_cells=assigned_cells,
        total_cells=cols * rows,
        icc_ready=SRGB_PROFILE_PATH.exists() and CMYK_PROFILE_PATH.exists(),
        icc_folder=ICC_DIR,
    )


@app.route("/custom/mode", methods=["POST"])
def custom_mode():
    layout = load_custom_layout()
    new_mode = request.form.get("mode")
    if layout["placements"]:
        flash("Reset the layout before changing the fill mode.", "error")
    elif new_mode in ("rows", "columns"):
        layout["mode"] = new_mode
        save_custom_layout(layout)
    return redirect(url_for("custom"))


@app.route("/custom/add", methods=["POST"])
def custom_add():
    layout = load_custom_layout()
    cols, rows = grid_dimensions()
    mode = layout["mode"]
    limit = rows if mode == "rows" else cols
    unit = "rows" if mode == "rows" else "columns"

    folder_key, _, filename = request.form.get("cover", "").partition("|")
    if folder_key not in STAGING_FOLDERS or filename not in list_images(STAGING_FOLDERS[folder_key]):
        flash("Please select a cover first.", "error")
        return redirect(url_for("custom"))

    try:
        start = int(request.form.get("start", ""))
        end = int(request.form.get("end", ""))
    except ValueError:
        flash(f"Please enter a number for both ends of the {unit} range.", "error")
        return redirect(url_for("custom"))

    if not (1 <= start <= end <= limit):
        flash(f"Enter {unit} between 1 and {limit}, with 'from' not larger than 'to'.", "error")
        return redirect(url_for("custom"))

    for p in layout["placements"]:
        if start <= p["end"] and end >= p["start"]:
            lo, hi = max(start, p["start"]), min(end, p["end"])
            flash(
                f"{unit.capitalize()} {lo} to {hi} are already used by '{p['file']}'. "
                f"Pick free {unit} or remove that placement first.",
                "error",
            )
            return redirect(url_for("custom"))

    layout["placements"].append({"folder": folder_key, "file": filename, "start": start, "end": end})
    save_custom_layout(layout)
    flash(f"Added '{filename}' to {unit} {start}-{end}.", "success")
    return redirect(url_for("custom"))


@app.route("/custom/remove/<int:index>", methods=["POST"])
def custom_remove(index):
    layout = load_custom_layout()
    if 0 <= index < len(layout["placements"]):
        removed = layout["placements"].pop(index)
        save_custom_layout(layout)
        flash(f"Removed '{removed['file']}' from the layout.", "success")
    return redirect(url_for("custom"))


@app.route("/custom/reset", methods=["POST"])
def custom_reset():
    save_custom_layout(default_custom_layout())
    flash("Custom layout reset.", "success")
    return redirect(url_for("custom"))


@app.route("/custom/generate", methods=["POST"])
def custom_generate():
    if not (SRGB_PROFILE_PATH.exists() and CMYK_PROFILE_PATH.exists()):
        flash(f"Missing ICC profiles. Place sRGB.icc and FOGRA39L_coated.icc in {ICC_DIR}", "error")
        return redirect(url_for("custom"))

    layout = load_custom_layout()
    removed = prune_custom_layout(layout)
    if removed:
        flash(f"{removed} placement(s) were removed because their cover is no longer staged.", "error")
    if not layout["placements"]:
        flash("Add at least one book to the layout first.", "error")
        return redirect(url_for("custom"))

    fill_empty = "cycle" if request.form.get("fill_empty") == "cycle" else "blank"
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    out_path = FINAL_PDF_DIR / f"custom_layout_{timestamp}.pdf"
    build_custom_pdf(layout, fill_empty, out_path)
    return send_file(out_path, as_attachment=True, download_name=out_path.name)


if __name__ == "__main__":
    app.run(debug=True)
