# BhuMe Boundary Correction — Walkthrough

## Overview

Built and debugged a 7-stage cadastral boundary correction pipeline (`solve.py`) that uses satellite imagery cross-correlation to fix shifted plot boundaries. Tested on two villages: Vadnerbhairav and Malatavadi.

## Bugs Found & Fixed

### 1. Variable Name Collision (Critical)
**File**: [solve.py](file:///d:/Bhume/bhume-starter-kit/solve.py#L452-L455)

The variable `row` (pandas Series from `village.plots.loc[pn]`) was being **overwritten** in the pixel coordinate conversion loop:
```python
# Before (bug): 'row' shadows the pandas Series
for x, y in exterior_coords:
    col = (x - patch_transform.c) / patch_transform.a
    row = (y - patch_transform.f) / patch_transform.e  # ← overwrites!
```
**Impact**: Every single plot threw `'numpy.float64' has no attribute 'get'`, caught silently by `except Exception`, and got flagged. Result: **0 corrected, 2457 flagged**.

**Fix**: Renamed to `pcol`/`prow`.

---

### 2. Boundary Raster Single-Band
**File**: [solve.py](file:///d:/Bhume/bhume-starter-kit/solve.py#L393-L425)

`boundaries.tif` is a single-band raster, but `patch_for_plot()` tries to read bands `[1, 2, 3]`. This crashed silently, falling back to imagery-only edges.

**Fix**: Read band 1 directly via rasterio with proper windowing and resize to match imagery patch.

---

### 3. Local Shift Validation
**File**: [solve.py](file:///d:/Bhume/bhume-starter-kit/solve.py#L498-L530)

Cross-correlation was finding spurious matches with huge shifts (e.g., dx=26.9m for Malatavadi plot 1763), causing IoU to plummet from 0.675 → 0.134.

**Fix**: Added validation — only apply local correction if:
- `corr_score >= 0.15` (minimum correlation quality)
- `local_shift_m <= 20.0` (maximum plausible correction)

---

### 4. Confidence Calibration
**File**: [solve.py](file:///d:/Bhume/bhume-starter-kit/solve.py#L286-L340)

Original confidence used `corr_score` as primary signal (45% weight), but raw values were all ~0.15-0.35 and didn't track accuracy. Key improvements:

- **sqrt rescaling** of corr_score to spread values
- **shift_agreement signal** (30% weight): small local corrections → higher confidence (global shift was already good for this plot)
- **Global shift reliability penalty**: uses `shift_std` from example truth variance to scale down confidence when the global shift is uncertain

**Result**: Spearman went from **-0.200 → +0.371** on Vadnerbhairav.

## Results

### Vadnerbhairav (6 example truths)

| Metric | Baseline | Our Solution | Change |
|--------|----------|-------------|--------|
| Median IoU | 0.713 | **0.879** | +0.166 |
| Improvement over official | +0.112 | **+0.231** | 2x |
| Centroid error | 8.835m | **3.799m** | -57% |
| Accurate (IoU≥0.5) | 1.000 | **1.000** | — |
| Spearman(conf,IoU) | — | **+0.371** | ✅ |
| Corrected/Flagged | 2457/0 | 2385/72 | selective |

### Malatavadi (3 example truths)

| Metric | Baseline | Our Solution | Change |
|--------|----------|-------------|--------|
| Median IoU | 0.588 | **0.588** | same |
| Improvement over official | +0.090 | **+0.286** | 3x |
| Centroid error | 7.897m | **4.351m** | -45% |
| Accurate (IoU≥0.5) | 1.000 | **0.667** | -0.333 |
| Spearman(conf,IoU) | — | **-1.000** | ⚠️ |
| Corrected/Flagged | 2508/0 | 2464/44 | selective |

### Malatavadi Limitation

Plot 1177 needs dx=+0.7m but global shift applies dx=+9.6m, harming it (IoU 0.675→0.188). With only 3 truth plots having very different required shifts (std=2.3m), the global shift is unreliable for some plots. The Spearman=-1.000 is driven by this single outlier with n=3.

## Diagnostic Images Generated

- `data/34855_vadnerbhairav_chandavad_nashik/diagnostics/comparison_*.png` (6 images)
- `data/12429_malatavadi_chandgad_kolhapur/diagnostics/comparison_*.png` (3 images)

## Files Modified

- [solve.py](file:///d:/Bhume/bhume-starter-kit/solve.py) — All bug fixes and improvements
