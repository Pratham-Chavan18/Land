#!/usr/bin/env python3
"""
BhuMe Boundary Correction Pipeline
====================================
Corrects shifted cadastral plot boundaries by aligning them to satellite imagery.

Pipeline stages:
  1. Global median shift (baseline floor from example truths)
  2. Per-plot cross-correlation refinement against satellite edges
  3. Area-ratio analysis & flagging
  4. Multi-signal confidence scoring
  5. Control plot detection (restraint)

Usage:
  uv run solve.py data/34855_vadnerbhairav_chandavad_nashik
  uv run solve.py data/12429_malatavadi_chandgad_kolhapur
"""

from __future__ import annotations

import sys
import statistics
from pathlib import Path
from dataclasses import dataclass

import numpy as np
from scipy.ndimage import uniform_filter
from scipy.signal import fftconvolve
from PIL import Image, ImageDraw
import geopandas as gpd
from shapely.affinity import translate
from shapely.geometry import Polygon, MultiPolygon
from shapely.geometry.base import BaseGeometry

from bhume import load, patch_for_plot, score, write_predictions
from bhume.geo import open_imagery, geom_to_imagery_crs, lonlat_to_pixel, pixel_to_lonlat


# ─── Configuration ──────────────────────────────────────────────────────────

PAD_M = 80            # padding (metres) around plot for image extraction
SEARCH_RADIUS_PX = 40 # max search radius in pixels for cross-correlation
MIN_PLOT_PIXELS = 20  # minimum plot size in pixels to attempt correlation
EDGE_THRESHOLD = 30   # Canny-like gradient threshold for edge detection

# Area ratio thresholds for flagging
AREA_RATIO_LOW = 0.3
AREA_RATIO_HIGH = 3.5

# Confidence parameters
CONF_FLOOR = 0.15     # minimum confidence for any correction
CONF_CEIL = 0.95      # maximum confidence cap

# Control plot: if correction shift < this, keep original
CONTROL_SHIFT_M = 2.0


# ─── Helpers ────────────────────────────────────────────────────────────────

def _utm_for(geom: BaseGeometry) -> str:
    """Pick the right UTM zone for a geometry."""
    lon = geom.centroid.x
    return f'EPSG:{32600 + int((lon + 180) // 6) + 1}'


def compute_global_shift(village) -> tuple[float, float, str, float]:
    """Compute median (dx, dy) shift in UTM metres from example truths.

    Returns (median_dx, median_dy, utm_crs, shift_std) where shift_std is the
    standard deviation of the per-truth-plot shifts (higher = less reliable).
    """
    if village.example_truths is None:
        raise ValueError(f'{village.slug} has no example_truths.geojson')

    utm = _utm_for(village.example_truths.geometry.iloc[0])
    official_u = village.plots.to_crs(utm)
    truth_u = village.example_truths.to_crs(utm)

    dxs, dys = [], []
    for pn in village.example_truths.index:
        if pn in official_u.index:
            o = official_u.loc[pn, 'geometry'].centroid
            t = truth_u.loc[pn, 'geometry'].centroid
            dxs.append(t.x - o.x)
            dys.append(t.y - o.y)

    if not dxs:
        raise ValueError('no overlapping plots between truths and cadastre')

    mdx = statistics.median(dxs)
    mdy = statistics.median(dys)

    # Compute shift reliability: std of distance from median
    residuals = [np.sqrt((dx - mdx)**2 + (dy - mdy)**2) for dx, dy in zip(dxs, dys)]
    shift_std = float(np.std(residuals)) if len(residuals) > 1 else 0.0

    return mdx, mdy, utm, shift_std


def gradient_magnitude(gray: np.ndarray) -> np.ndarray:
    """Compute gradient magnitude (Sobel-like) from a grayscale image."""
    # Use simple Sobel-like gradients
    gy = np.diff(gray.astype(np.float32), axis=0)
    gx = np.diff(gray.astype(np.float32), axis=1)
    # Make them the same size by padding
    gy = np.pad(gy, ((0, 1), (0, 0)), mode='edge')
    gx = np.pad(gx, ((0, 0), (0, 1)), mode='edge')
    mag = np.sqrt(gx**2 + gy**2)
    return mag


def rgb_to_gray(rgb: np.ndarray) -> np.ndarray:
    """Convert RGB (H,W,3) to grayscale (H,W) float32."""
    return (0.2989 * rgb[:, :, 0] + 0.5870 * rgb[:, :, 1] + 0.1140 * rgb[:, :, 2]).astype(np.float32)


def render_polygon_mask(geom_px: np.ndarray, shape: tuple[int, int],
                        offset: tuple[float, float]) -> np.ndarray:
    """Render a polygon as a filled binary mask.

    geom_px: Nx2 array of (col, row) pixel coordinates
    shape: (H, W) of the output mask
    offset: (col_offset, row_offset) to subtract from coords
    """
    h, w = shape
    mask = Image.new('L', (w, h), 0)
    draw = ImageDraw.Draw(mask)

    coords = [(float(x - offset[0]), float(y - offset[1])) for x, y in geom_px]
    if len(coords) >= 3:
        draw.polygon(coords, fill=255)

    return np.array(mask, dtype=np.float32) / 255.0


def polygon_to_edge_mask(mask: np.ndarray) -> np.ndarray:
    """Convert a filled polygon mask to an edge-only mask."""
    # Erode by 1 pixel and subtract
    from scipy.ndimage import binary_erosion
    filled = mask > 0.5
    eroded = binary_erosion(filled, iterations=1)
    edge = filled.astype(np.float32) - eroded.astype(np.float32)
    return np.clip(edge, 0, 1)


def normalised_cross_correlation(template: np.ndarray, image: np.ndarray,
                                  search_radius: int) -> tuple[int, int, float]:
    """Find the best (dy, dx) shift of template within image using NCC.

    Returns (best_dy, best_dx, peak_score) where peak_score is in [-1, 1].
    """
    # Ensure both are float
    t = template.astype(np.float64)
    im = image.astype(np.float64)

    h, w = t.shape
    ih, iw = im.shape

    # template must fit inside image
    if h > ih or w > iw:
        return 0, 0, 0.0

    # Use FFT-based cross correlation for speed
    # Zero-mean the template
    t_mean = t.mean()
    t_zm = t - t_mean
    t_std = t_zm.std()
    if t_std < 1e-10:
        return 0, 0, 0.0

    t_norm = t_zm / (t_std * t_zm.size)

    # Pad template to image size for FFT
    t_padded = np.zeros_like(im)
    t_padded[:h, :w] = t_norm

    # Cross-correlation via FFT
    cc = fftconvolve(im, t_padded[::-1, ::-1], mode='same')

    # Normalize by local std of image
    im_sq = fftconvolve(im**2, np.ones_like(t_padded) / t_padded.size,
                        mode='same')
    im_mean = fftconvolve(im, np.ones_like(t_padded) / t_padded.size,
                          mode='same')
    im_var = im_sq - im_mean**2
    im_var = np.clip(im_var, 0, None)
    im_std = np.sqrt(im_var)

    # Avoid division by zero
    valid = im_std > 1e-10
    ncc = np.zeros_like(cc)
    ncc[valid] = cc[valid] / (im_std[valid] * t_padded.size)

    # Restrict search to within search_radius of centre
    cy, cx = ih // 2, iw // 2
    y0 = max(0, cy - search_radius)
    y1 = min(ih, cy + search_radius + 1)
    x0 = max(0, cx - search_radius)
    x1 = min(iw, cx + search_radius + 1)

    region = ncc[y0:y1, x0:x1]
    by, bx = np.unravel_index(region.argmax(), region.shape)

    best_dy = (y0 + by) - cy
    best_dx = (x0 + bx) - cx
    peak_score = float(region[by, bx])

    return best_dy, best_dx, peak_score


def cross_correlate_edges(plot_edge_mask: np.ndarray,
                          imagery_edges: np.ndarray,
                          search_radius: int) -> tuple[int, int, float]:
    """Cross-correlate plot edge mask against imagery edge map.

    This is a simpler, more robust approach: slide the edge mask
    over the edge map and find the position with maximum overlap.

    Returns (best_dy, best_dx, peak_score).
    """
    h, w = plot_edge_mask.shape
    ih, iw = imagery_edges.shape

    if h > ih or w > iw or h < 3 or w < 3:
        return 0, 0, 0.0

    # Ensure template has content
    if plot_edge_mask.sum() < 5:
        return 0, 0, 0.0

    # Cross-correlation via FFT (unnormalized but fast)
    cc = fftconvolve(imagery_edges, plot_edge_mask[::-1, ::-1], mode='same')

    # Self-correlation of template for normalization
    self_corr = float(np.sum(plot_edge_mask ** 2))
    if self_corr < 1e-10:
        return 0, 0, 0.0

    # Centre of the result corresponds to zero shift
    cy, cx = ih // 2, iw // 2

    # Restrict search region
    y0 = max(0, cy - search_radius)
    y1 = min(ih, cy + search_radius + 1)
    x0 = max(0, cx - search_radius)
    x1 = min(iw, cx + search_radius + 1)

    region = cc[y0:y1, x0:x1]
    by, bx = np.unravel_index(region.argmax(), region.shape)

    best_dy = (y0 + by) - cy
    best_dx = (x0 + bx) - cx

    # Normalize peak score: peak / self_corr gives [0, ...] where 1.0 = perfect
    peak_score = float(region[by, bx]) / self_corr
    peak_score = min(peak_score, 1.0)

    return best_dy, best_dx, peak_score


@dataclass
class PlotCorrection:
    """Result of correcting a single plot."""
    plot_number: str
    status: str               # 'corrected' or 'flagged'
    confidence: float
    geometry: BaseGeometry     # in EPSG:4326
    method_note: str
    shift_m: float             # total shift applied in metres
    corr_score: float          # cross-correlation score
    area_ratio: float | None   # map_area / recorded_area


def get_plot_area_ratio(row) -> float | None:
    """Compute map_area / total_recorded_area for a plot row."""
    map_area = row.get('map_area_sqm')
    rec_area = row.get('recorded_area_sqm')
    pot_kharaba = row.get('pot_kharaba_ha')

    if map_area is None or (rec_area is None and pot_kharaba is None):
        return None

    total_recorded = 0.0
    if rec_area is not None and rec_area > 0:
        total_recorded += rec_area
    if pot_kharaba is not None and pot_kharaba > 0:
        total_recorded += pot_kharaba * 10000  # ha to sqm

    if total_recorded <= 0:
        return None

    return map_area / total_recorded


def compute_confidence(corr_score: float, area_ratio: float | None,
                       plot_area_sqm: float, boundary_density: float,
                       edge_clarity: float, local_shift_m: float = 0.0) -> float:
    """Combine multiple signals into a confidence score.

    The confidence should track accuracy: high confidence means we expect
    this correction to be close to truth. We use:
      - corr_score: cross-correlation peak quality (higher = better edge match)
      - area_ratio: map_area / recorded_area, nearness to 1.0
      - plot_area_sqm: larger plots are easier to place
      - boundary_density: fraction of boundary hint pixels under plot
      - edge_clarity: mean gradient magnitude along plot edges
      - local_shift_m: magnitude of local correction vs global (smaller = better)
    """
    # 1. Correlation signal — rescale with sqrt to spread low values
    corr_signal = np.clip(np.sqrt(max(corr_score, 0)), 0, 1)

    # 2. Area agreement signal
    if area_ratio is not None and area_ratio > 0:
        log_ratio = abs(np.log(area_ratio))
        area_signal = np.exp(-log_ratio**2 / (2 * 0.4**2))
    else:
        area_signal = 0.4

    # 3. Plot size signal
    size_signal = np.clip((np.log10(max(plot_area_sqm, 100)) - 2.0) / 3.0, 0.1, 1.0)

    # 4. Boundary hint density
    hint_signal = np.clip(boundary_density * 1.5, 0, 1)

    # 5. Edge clarity
    clarity_signal = np.clip(edge_clarity / 50.0, 0, 1)

    # 6. Local shift agreement — small local corrections are MORE trustworthy
    # If local_shift_m is small, the global shift was already good for this plot
    # Decay: 0m → 1.0, 5m → 0.6, 10m → 0.37, 20m → 0.13
    shift_agreement = np.exp(-local_shift_m / 10.0)

    # Weighted combination — shift_agreement is a key calibration signal
    raw = (0.20 * corr_signal +
           0.20 * area_signal +
           0.10 * size_signal +
           0.10 * hint_signal +
           0.10 * clarity_signal +
           0.30 * shift_agreement)

    # Scale to fill more of the [0, 1] range
    # raw typically in [0.2, 0.7] → map to [0.15, 0.95]
    scaled = 0.15 + (raw - 0.15) * (0.80 / 0.55)
    confidence = np.clip(scaled, CONF_FLOOR, CONF_CEIL)

    return round(float(confidence), 3)


def correct_single_plot(pn: str, village, src_imagery, src_boundaries,
                        global_dx: float, global_dy: float,
                        utm: str, shift_std: float = 0.0) -> PlotCorrection:
    """Correct a single plot through the full pipeline.

    Steps:
      1. Apply global shift as starting point
      2. Extract satellite patch + boundary hints
      3. Cross-correlate plot edges against imagery edges
      4. Decide on correction quality and confidence
    """
    row = village.plots.loc[pn]
    official_geom_4326 = row['geometry']
    map_area = row.get('map_area_sqm', 0) or 0

    # Convert to UTM for metric operations
    official_u = gpd.GeoSeries([official_geom_4326], crs='EPSG:4326').to_crs(utm)
    official_utm = official_u.iloc[0]

    # Stage 1: Apply global shift
    shifted_utm = translate(official_utm, global_dx, global_dy)

    # Convert back to 4326 for patch extraction
    shifted_4326 = gpd.GeoSeries([shifted_utm], crs=utm).to_crs('EPSG:4326').iloc[0]

    # Stage 2: Extract imagery patch around globally-shifted plot
    try:
        patch = patch_for_plot(src_imagery, shifted_4326, pad_m=PAD_M)
    except (ValueError, Exception) as e:
        # Plot is outside imagery extent
        return PlotCorrection(
            plot_number=pn, status='flagged', confidence=0.0,
            geometry=official_geom_4326,
            method_note=f'outside imagery extent: {e}',
            shift_m=0.0, corr_score=0.0, area_ratio=get_plot_area_ratio(row)
        )

    rgb = patch.image
    if rgb.size == 0 or rgb.shape[0] < 5 or rgb.shape[1] < 5:
        return PlotCorrection(
            plot_number=pn, status='flagged', confidence=0.0,
            geometry=official_geom_4326,
            method_note='image patch too small',
            shift_m=0.0, corr_score=0.0, area_ratio=get_plot_area_ratio(row)
        )

    gray = rgb_to_gray(rgb)
    imagery_edges = gradient_magnitude(gray)

    # Get boundary hints if available
    boundary_density = 0.0
    if src_boundaries is not None:
        try:
            # boundaries.tif is single-band — read it directly with rasterio
            from pyproj import Transformer
            from rasterio.windows import from_bounds
            geom_bnd_crs = geom_to_imagery_crs(src_boundaries, shifted_4326)
            bminx, bminy, bmaxx, bmaxy = geom_bnd_crs.bounds
            bleft = bminx - PAD_M
            bbottom = bminy - PAD_M
            bright = bmaxx + PAD_M
            btop = bmaxy + PAD_M
            # clip to dataset extent
            dl, db, dr, dt = src_boundaries.bounds
            bleft = max(bleft, dl)
            bbottom = max(bbottom, db)
            bright = min(bright, dr)
            btop = min(btop, dt)
            if bright > bleft and btop > bbottom:
                bnd_window = from_bounds(bleft, bbottom, bright, btop, transform=src_boundaries.transform)
                bnd_arr = src_boundaries.read(1, window=bnd_window).astype(np.float32)
                if bnd_arr.size > 0 and bnd_arr.max() > 0:
                    bnd_norm = bnd_arr / bnd_arr.max()
                    # Resize to match imagery patch if needed
                    h_img, w_img = rgb.shape[:2]
                    if bnd_norm.shape != (h_img, w_img):
                        from PIL import Image as PILImage
                        bnd_pil = PILImage.fromarray(bnd_norm)
                        bnd_pil = bnd_pil.resize((w_img, h_img), PILImage.BILINEAR)
                        bnd_norm = np.array(bnd_pil)
                    # Weighted combination: 60% imagery edges, 40% boundary hints
                    combined_edges = 0.6 * (imagery_edges / max(imagery_edges.max(), 1)) + 0.4 * bnd_norm
                    boundary_density = float(np.mean(bnd_norm > 0.2))
                else:
                    combined_edges = imagery_edges / max(imagery_edges.max(), 1)
            else:
                combined_edges = imagery_edges / max(imagery_edges.max(), 1)
        except Exception:
            combined_edges = imagery_edges / max(imagery_edges.max(), 1)
    else:
        combined_edges = imagery_edges / max(imagery_edges.max(), 1)

    # Stage 3: Create edge mask of the shifted plot in pixel coords
    # Convert the shifted polygon to imagery pixel coordinates
    geom_img_crs = geom_to_imagery_crs(src_imagery, shifted_4326)

    # Get polygon exterior coords in image pixels
    if isinstance(geom_img_crs, MultiPolygon):
        polys = list(geom_img_crs.geoms)
    else:
        polys = [geom_img_crs]

    # Use the largest polygon
    largest = max(polys, key=lambda p: p.area)
    exterior_coords = np.array(largest.exterior.coords)

    # Convert to pixel coordinates relative to the patch
    patch_left, patch_bottom, patch_right, patch_top = patch.bounds
    patch_transform = patch.transform

    # Use the transform to convert from imagery CRS to pixel coords
    pixel_coords = []
    for x, y in exterior_coords:
        pcol = (x - patch_transform.c) / patch_transform.a
        prow = (y - patch_transform.f) / patch_transform.e
        pixel_coords.append((pcol, prow))

    pixel_coords = np.array(pixel_coords)

    # Check if plot is too small in pixels
    col_range = pixel_coords[:, 0].max() - pixel_coords[:, 0].min()
    row_range = pixel_coords[:, 1].max() - pixel_coords[:, 1].min()

    if col_range < MIN_PLOT_PIXELS or row_range < MIN_PLOT_PIXELS:
        # Too small for reliable cross-correlation
        area_ratio = get_plot_area_ratio(row)
        conf = compute_confidence(0.3, area_ratio, map_area, boundary_density, 0)
        shift_m = np.sqrt(global_dx**2 + global_dy**2)
        return PlotCorrection(
            plot_number=pn, status='corrected', confidence=conf,
            geometry=shifted_4326,
            method_note=f'global shift only (plot too small for xcorr, {col_range:.0f}x{row_range:.0f}px)',
            shift_m=shift_m, corr_score=0.3,
            area_ratio=area_ratio
        )

    # Render plot edge mask
    h, w = rgb.shape[:2]
    offset = (0, 0)
    plot_mask = render_polygon_mask(pixel_coords, (h, w), offset)
    plot_edges = polygon_to_edge_mask(plot_mask)

    # Compute edge clarity along the plot boundary in imagery
    edge_values_at_boundary = imagery_edges[plot_edges > 0.5] if np.any(plot_edges > 0.5) else np.array([0])
    edge_clarity = float(np.mean(edge_values_at_boundary))

    # Stage 4: Cross-correlate plot edges against combined edge map
    search_r = min(SEARCH_RADIUS_PX, min(h, w) // 4)
    if search_r < 3:
        search_r = 3

    best_dy, best_dx, corr_score = cross_correlate_edges(
        plot_edges, combined_edges, search_r
    )

    # Convert pixel shift to metres (imagery is in EPSG:3857, metres)
    pixel_size_x = abs(patch_transform.a)  # metres per pixel (x)
    pixel_size_y = abs(patch_transform.e)  # metres per pixel (y)

    local_dx_m = best_dx * pixel_size_x
    local_dy_m = -best_dy * pixel_size_y  # negative because image y is flipped
    local_shift_m = np.sqrt(local_dx_m**2 + local_dy_m**2)

    # Validate the local correction:
    # Only apply if (a) correlation is meaningful and (b) shift is plausible
    MIN_CORR_FOR_LOCAL = 0.15   # minimum correlation to trust the local shift
    MAX_LOCAL_SHIFT_M = 20.0    # maximum local correction in metres

    use_local_shift = (corr_score >= MIN_CORR_FOR_LOCAL and
                       local_shift_m <= MAX_LOCAL_SHIFT_M)

    if use_local_shift:
        # Total shift = global + local correction
        total_dx = global_dx + local_dx_m
        total_dy = global_dy + local_dy_m
        total_shift_m = np.sqrt(total_dx**2 + total_dy**2)

        # Apply the local correction to the globally-shifted UTM geometry
        final_utm = translate(shifted_utm, local_dx_m, local_dy_m)

        # Convert back to EPSG:4326
        final_4326 = gpd.GeoSeries([final_utm], crs=utm).to_crs('EPSG:4326').iloc[0]
    else:
        # Fall back to global shift only
        total_dx = global_dx
        total_dy = global_dy
        total_shift_m = np.sqrt(total_dx**2 + total_dy**2)
        final_utm = shifted_utm
        final_4326 = shifted_4326
        # Reduce correlation score to reflect we couldn't refine
        corr_score = corr_score * 0.5

    # Stage 5: Area ratio check
    area_ratio = get_plot_area_ratio(row)

    # Stage 6: Decide status (corrected vs flagged)
    should_flag = False
    flag_reason = ''

    # Flag if area ratio is way off
    if area_ratio is not None and (area_ratio < AREA_RATIO_LOW or area_ratio > AREA_RATIO_HIGH):
        should_flag = True
        flag_reason = f'area_ratio={area_ratio:.2f} out of range'

    # Flag if correlation is very poor AND shift is large
    if corr_score < 0.05 and total_shift_m > 50:
        should_flag = True
        flag_reason = f'poor correlation ({corr_score:.3f}) with large shift ({total_shift_m:.1f}m)'

    if should_flag:
        return PlotCorrection(
            plot_number=pn, status='flagged', confidence=0.0,
            geometry=official_geom_4326,
            method_note=f'flagged: {flag_reason}',
            shift_m=0.0, corr_score=corr_score, area_ratio=area_ratio
        )

    # Stage 7: Confidence scoring
    confidence = compute_confidence(
        corr_score, area_ratio, map_area, boundary_density, edge_clarity,
        local_shift_m=local_shift_m if use_local_shift else 0.0
    )

    # Apply global shift reliability penalty
    # If the global shift has high variance, reduce confidence for all plots
    # shift_std of 0 = perfect agreement, 5+ = high disagreement
    shift_reliability = np.exp(-shift_std / 8.0)  # 0→1.0, 3→0.69, 5→0.54, 10→0.29
    confidence = round(float(confidence * (0.5 + 0.5 * shift_reliability)), 3)

    # Stage 8: Control plot check — if the correction barely moves the plot,
    # still mark as corrected but keep original geometry (it was already right)
    orig_centroid = official_utm.centroid
    final_centroid = final_utm.centroid
    actual_shift = orig_centroid.distance(final_centroid)

    if actual_shift < CONTROL_SHIFT_M:
        # Plot was already close to correct — return original with high confidence
        return PlotCorrection(
            plot_number=pn, status='corrected', confidence=min(confidence + 0.1, CONF_CEIL),
            geometry=official_geom_4326,
            method_note=f'near-original (shift={actual_shift:.1f}m < {CONTROL_SHIFT_M}m)',
            shift_m=actual_shift, corr_score=corr_score, area_ratio=area_ratio
        )

    method = f'global+xcorr shift dx={total_dx:.1f}m dy={total_dy:.1f}m (corr={corr_score:.3f})'
    return PlotCorrection(
        plot_number=pn, status='corrected', confidence=confidence,
        geometry=final_4326, method_note=method,
        shift_m=total_shift_m, corr_score=corr_score, area_ratio=area_ratio
    )


# ─── Main Pipeline ──────────────────────────────────────────────────────────

def solve(village_dir: str) -> gpd.GeoDataFrame:
    """Run the full correction pipeline on a village."""
    village = load(village_dir)
    n_truth = 0 if village.example_truths is None else len(village.example_truths)
    print(f'Loaded {village.slug}')
    print(f'  {len(village.plots)} plots · {n_truth} example truths · '
          f'boundaries={"yes" if village.boundaries_path else "none"}')

    # Stage 1: Compute global shift
    print('\n── Stage 1: Computing global median shift ──')
    global_dx, global_dy, utm, shift_std = compute_global_shift(village)
    print(f'  Global shift: dx={global_dx:.1f}m  dy={global_dy:.1f}m  (std={shift_std:.1f}m)')

    # Open rasters
    print('\n── Stage 2-5: Per-plot correction ──')
    src_imagery = open_imagery(village.imagery_path)
    src_boundaries = None
    if village.boundaries_path:
        import rasterio
        src_boundaries = rasterio.open(village.boundaries_path)

    corrections: list[PlotCorrection] = []
    n_plots = len(village.plots)

    for i, pn in enumerate(village.plots.index):
        if (i + 1) % 100 == 0 or i == 0 or i == n_plots - 1:
            print(f'  Processing plot {i+1}/{n_plots}: {pn}')

        try:
            result = correct_single_plot(
                pn, village, src_imagery, src_boundaries,
                global_dx, global_dy, utm, shift_std
            )
            corrections.append(result)
        except Exception as e:
            # Fallback: flag the plot
            corrections.append(PlotCorrection(
                plot_number=pn, status='flagged', confidence=0.0,
                geometry=village.plots.loc[pn, 'geometry'],
                method_note=f'error: {str(e)[:100]}',
                shift_m=0.0, corr_score=0.0, area_ratio=None
            ))

    src_imagery.close()
    if src_boundaries is not None:
        src_boundaries.close()

    # Build predictions GeoDataFrame
    print('\n── Building predictions ──')
    n_corrected = sum(1 for c in corrections if c.status == 'corrected')
    n_flagged = sum(1 for c in corrections if c.status == 'flagged')
    print(f'  {n_corrected} corrected, {n_flagged} flagged')

    # Confidence distribution
    confs = [c.confidence for c in corrections if c.status == 'corrected']
    if confs:
        print(f'  Confidence: min={min(confs):.3f} median={sorted(confs)[len(confs)//2]:.3f} '
              f'max={max(confs):.3f} mean={sum(confs)/len(confs):.3f}')

    rows = []
    for c in corrections:
        rows.append({
            'plot_number': c.plot_number,
            'status': c.status,
            'confidence': c.confidence if c.status == 'corrected' else None,
            'method_note': c.method_note,
            'geometry': c.geometry,
        })

    preds = gpd.GeoDataFrame(rows, geometry='geometry', crs='EPSG:4326')
    preds = preds.set_index('plot_number', drop=False)

    return preds


def main(village_dir: str) -> None:
    """Full pipeline: solve → save → score."""
    preds = solve(village_dir)

    # Save predictions
    out_path = Path(village_dir) / 'predictions.geojson'
    out = write_predictions(out_path, preds)
    print(f'\n  Wrote {len(preds)} predictions → {out}')

    # Score against example truths
    village = load(village_dir)
    print('\n── Scoring ──')
    print(score(preds, village))


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('Usage: uv run solve.py data/<village_slug>')
        sys.exit(1)
    main(sys.argv[1])
