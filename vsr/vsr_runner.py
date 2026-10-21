"""Headless runner for video-subtitle-remover 1.4.0. Runs with the VSR venv's python; VSR_SRC = its source folder.

Usage: python vsr_runner.py --input IN.mp4 --output OUT.mp4 --mode sttn-auto|sttn-det|lama

- Whole-frame handling: sttn-det / lama use VSR's own full-frame OCR detection (no area).
  sttn-auto does no detection itself, so we first run VSR's detector over the whole frame,
  merge the detected text boxes into areas, and pass those areas to sttn-auto.
- Tall text blocks: VSR erases only one fixed-height strip per block, so we wrap its strip
  function to add strips until the whole block is covered (see cover_tall_islands).
- propainter / opencv are refused.
- Does not modify repo code; config lives in <cwd>/config/config.json (run dir).
"""
import argparse
import multiprocessing
import os
import sys
import time

VSR_SRC = os.environ.get("VSR_SRC") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
ALLOWED = ("sttn-auto", "sttn-det", "lama")
PAD = int(os.environ.get("VSR_PAD", "12"))  # px added around detected text for sttn-auto areas
STRIP_MARGIN = 10  # context rows kept above/below the text inside a strip
DETECT_STEP = 6    # sttn-auto: run text detection on every 6th frame (~5 a second, so brief word-by-word captions are still caught)


def merge_boxes(boxes, w, h, pad):
    """boxes: (xmin,xmax,ymin,ymax). Returns areas (ymin,ymax,xmin,xmax) of merged overlapping padded boxes."""
    rects = [[max(0, x0 - pad), min(w, x1 + pad), max(0, y0 - pad), min(h, y1 + pad)] for x0, x1, y0, y1 in boxes]
    changed = True
    while changed:
        changed = False
        out = []
        for r in rects:
            for o in out:
                if r[0] <= o[1] and o[0] <= r[1] and r[2] <= o[3] and o[2] <= r[3]:
                    o[0], o[1], o[2], o[3] = min(o[0], r[0]), max(o[1], r[1]), min(o[2], r[2]), max(o[3], r[3])
                    changed = True
                    break
            else:
                out.append(list(r))
        rects = out
    return [(r[2], r[3], r[0], r[1]) for r in rects]


def cover_tall_islands(orig):
    """VSR's get_inpaint_area_by_mask gives each text block ONE full-width strip of fixed height
    (split_h = W*3/16) centred on it, so a block taller than that (e.g. a 7-line caption) only gets its
    middle erased. Wrap it: when VSR's strips leave masked rows uncovered, re-tile all masked rows with the
    fewest strips of the same height (each strip costs a full inpaint pass, so fewer = faster), each strip
    centred on the rows it is responsible for so the text has context above and below."""
    import numpy as np

    def wrapped(W, H, h, mask, multiple=1):
        areas = list(orig(W, H, h, mask, multiple))
        rows = np.flatnonzero(np.asarray(mask).reshape(mask.shape[0], -1).any(axis=1))
        covered = np.zeros(H, bool)
        for a in areas:
            covered[a[0]:a[1]] = True
        if multiple > 1 or covered[rows].all():
            return areas
        tiles, i = [], 0
        while i < len(rows):
            y0, j = int(rows[i]), i
            while j + 1 < len(rows) and rows[j + 1] < y0 + h - 2 * STRIP_MARGIN:
                j += 1
            centre = (y0 + int(rows[j])) // 2
            ymin = max(0, centre - h // 2)
            ymax = ymin + h
            if ymax > H:
                ymax, ymin = H, max(0, H - h)
            tiles.append((ymin, ymax, 0, W))
            i = j + 1
        print(f"[vsr_cli] text taller than one strip ({h}px): {len(areas)} strip(s) -> {len(tiles)}: {tiles}", flush=True)
        return tiles

    return wrapped


GROW_MAX = int(os.environ.get("VSR_GROW", "60"))  # px a text box may grow into its solid background
GROW_STD = 14      # a strip counts as "solid" when every colour channel varies less than this
GROW_DIFF = 30     # ... and its colour is this close to the colour first found next to the text


def grow_box(img, box):
    """TikTok captions often sit on a solid box or bubble (black, white, orange). The detector only boxes the
    letters, so the shape behind them would survive. Grow each side of the text box while the pixel strip just
    outside it is one flat colour; plain text over a real scene stops growing at once."""
    import numpy as np
    h, w = img.shape[:2]
    x0, x1, y0, y1 = (int(v) for v in box)
    x0, x1, y0, y1 = max(0, x0), min(w, x1), max(0, y0), min(h, y1)
    if x1 - x0 < 4 or y1 - y0 < 4:
        return box

    def strip(side, d):
        if side == "top":
            return img[max(0, y0 - d - 2):max(0, y0 - d), x0:x1]
        if side == "bottom":
            return img[min(h, y1 + d):min(h, y1 + d + 2), x0:x1]
        if side == "left":
            return img[y0:y1, max(0, x0 - d - 2):max(0, x0 - d)]
        return img[y0:y1, min(w, x1 + d):min(w, x1 + d + 2)]

    grown = {}
    for side in ("top", "bottom", "left", "right"):
        ref, d = None, 0
        while d < GROW_MAX:
            s = strip(side, d)
            if s.size == 0:
                break
            px = s.reshape(-1, s.shape[-1]).astype(np.float32)
            if px.std(axis=0).max() > GROW_STD:
                break
            mean = px.mean(axis=0)
            if ref is None:
                ref = mean
            elif np.abs(mean - ref).max() > GROW_DIFF:
                break
            d += 2
        grown[side] = d
    return (max(0, x0 - grown["left"]), min(w, x1 + grown["right"]),
            max(0, y0 - grown["top"]), min(h, y1 + grown["bottom"]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--mode", default="sttn-auto")
    a = ap.parse_args()
    if a.mode not in ALLOWED:
        print(f"ERROR: mode '{a.mode}' not allowed (allowed: {', '.join(ALLOWED)})", flush=True)
        return 2

    sys.path.insert(0, VSR_SRC)
    os.makedirs("config", exist_ok=True)

    try:  # AMD/Intel on Windows; NVIDIA, Mac and CPU installs don't have it
        import torch_directml
        print(f"[vsr_cli] torch_directml device: {torch_directml.device_name(torch_directml.default_device())}", flush=True)
    except ImportError:
        import torch
        dev = "cuda" if torch.cuda.is_available() else ("mps" if torch.backends.mps.is_available() else "cpu")
        print(f"[vsr_cli] torch device: {dev}", flush=True)

    from backend.config import config, tr
    from backend.tools.constant import InpaintMode
    from backend.main import SubtitleRemover
    from backend.tools.subtitle_detect import SubtitleDetect
    import backend.inpaint.sttn_auto_inpaint as m_auto
    import backend.inpaint.sttn_det_inpaint as m_det
    import backend.inpaint.lama_inpaint as m_lama
    for m in (m_auto, m_det, m_lama):  # all three import the same strip function by name
        m.get_inpaint_area_by_mask = cover_tall_islands(m.get_inpaint_area_by_mask)
    if GROW_MAX > 0:
        _detect = SubtitleDetect.detect_subtitle
        SubtitleDetect.detect_subtitle = lambda self, img: [grow_box(img, b) for b in _detect(self, img)]

    config.set(config.interface, "en")
    tr.read(os.path.join(VSR_SRC, "backend", "interface", "en.ini"), encoding="utf-8")
    config.set(config.hardwareAcceleration, True)
    config.inpaintMode.value = InpaintMode(a.mode)
    # margin around each detected text box in the masked modes (sttn-det, lama); TikTok captions often sit on a
    # solid box or have a thick outline that reaches well past the letters, so the default 10 px leaves it behind
    config.subtitleAreaDeviationPixel.value = int(os.environ.get("VSR_MARGIN", "22"))

    sr = SubtitleRemover(os.path.abspath(a.input))
    sr.video_out_path = os.path.abspath(a.output)
    print(f"[vsr_cli] accelerator: {sr.hardware_accelerator.accelerator_name}; "
          f"inpaint device: {sr.hardware_accelerator.device}", flush=True)

    if a.mode == "sttn-auto":
        det = SubtitleDetect(sr.video_path, [])
        # sttn-auto fills the same areas on every frame, so only WHERE text ever appears matters, not
        # on which frames: reading ~3 frames a second finds it at a fraction of the cost
        det.SAMPLE_STEP = max(det.SAMPLE_STEP, DETECT_STEP)
        found = det.find_subtitle_frame_no(sub_remover=sr)
        # same sanity filter VSR uses in its detection modes: drop degenerate boxes and
        # boxes much taller than wide (subtitleYXAxisDifferencePixel)
        yx = config.subtitleYXAxisDifferencePixel.value
        boxes = {b for v in found.values() for b in v
                 if b[1] > b[0] and b[3] > b[2] and (b[3] - b[2]) - (b[1] - b[0]) <= yx}
        if not boxes:
            print("[vsr_cli] no text detected anywhere; copying input unchanged", flush=True)
            import shutil
            sr.video_cap.release()
            sr.video_writer.release()
            shutil.copy2(sr.video_path, sr.video_out_path)
            return 0
        areas = merge_boxes(boxes, sr.frame_width, sr.frame_height, PAD)
        print(f"[vsr_cli] auto areas (ymin,ymax,xmin,xmax): {areas}", flush=True)
        sr.sub_areas = areas
        del det
    else:
        sr.sub_areas = []  # VSR full-frame detection

    sr.run()
    return 0 if os.path.exists(sr.video_out_path) else 1


if __name__ == "__main__":
    multiprocessing.set_start_method("spawn")
    t = time.time()
    rc = main()
    print(f"[vsr_cli] python elapsed {time.time() - t:.1f}s rc={rc}", flush=True)
    sys.exit(rc)
