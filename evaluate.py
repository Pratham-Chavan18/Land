#!/usr/bin/env python3
"""
Evaluation & Diagnostics
=========================
Detailed scoring, confidence analysis, and visual comparison of predictions
against example truths.

Usage:
  uv run evaluate.py data/34855_vadnerbhairav_chandavad_nashik
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont
import geopandas as gpd

from bhume import load, patch_for_plot, score, write_predictions
from bhume.geo import open_imagery, geom_to_imagery_crs
from bhume.io import read_predictions


def draw_polygon_on_image(img: Image.Image, coords_px: list[tuple],
                           color: str, width: int = 2) -> None:
    """Draw a polygon outline on a PIL image."""
    draw = ImageDraw.Draw(img)
    if len(coords_px) >= 3:
        # Close the polygon
        pts = list(coords_px) + [coords_px[0]]
        draw.line(pts, fill=color, width=width)


def geom_to_patch_pixels(src, geom_4326, patch) -> list[tuple]:
    """Convert a lon/lat geometry to pixel coordinates within a patch."""
    geom_crs = geom_to_imagery_crs(src, geom_4326)
    
    from shapely.geometry import MultiPolygon
    if isinstance(geom_crs, MultiPolygon):
        polys = list(geom_crs.geoms)
        largest = max(polys, key=lambda p: p.area)
    else:
        largest = geom_crs
    
    exterior = np.array(largest.exterior.coords)
    patch_transform = patch.transform
    
    pixel_coords = []
    for x, y in exterior:
        col = (x - patch_transform.c) / patch_transform.a
        row = (y - patch_transform.f) / patch_transform.e
        pixel_coords.append((float(col), float(row)))
    
    return pixel_coords


def visualize_comparison(village, preds_gdf, output_dir: Path) -> None:
    """Generate before/after comparison images for example truth plots."""
    if village.example_truths is None:
        print('  No example truths to compare against.')
        return
    
    output_dir.mkdir(exist_ok=True)
    
    with open_imagery(village.imagery_path) as src:
        for pn in village.example_truths.index:
            truth_geom = village.example_truths.loc[pn, 'geometry']
            official_geom = village.plots.loc[pn, 'geometry']
            
            # Get predicted geometry if available
            pred_geom = None
            pred_conf = None
            if pn in preds_gdf.index:
                pred_row = preds_gdf.loc[pn]
                if pred_row.get('status') == 'corrected':
                    pred_geom = pred_row['geometry']
                    pred_conf = pred_row.get('confidence')
            
            # Extract patch centred on truth
            try:
                patch = patch_for_plot(src, truth_geom, pad_m=60)
            except Exception:
                continue
            
            img = Image.fromarray(patch.image)
            
            # Draw boundaries
            # Red = official (shifted)
            official_px = geom_to_patch_pixels(src, official_geom, patch)
            draw_polygon_on_image(img, official_px, color='red', width=2)
            
            # Green = truth
            truth_px = geom_to_patch_pixels(src, truth_geom, patch)
            draw_polygon_on_image(img, truth_px, color='lime', width=2)
            
            # Blue/Cyan = our prediction
            if pred_geom is not None:
                pred_px = geom_to_patch_pixels(src, pred_geom, patch)
                draw_polygon_on_image(img, pred_px, color='cyan', width=2)
            
            # Add labels
            draw = ImageDraw.Draw(img)
            try:
                font = ImageFont.load_default()
            except Exception:
                font = None
            
            y_pos = 5
            draw.text((5, y_pos), f'Plot {pn}', fill='white', font=font)
            y_pos += 15
            draw.text((5, y_pos), 'Red=Official', fill='red', font=font)
            y_pos += 15
            draw.text((5, y_pos), 'Green=Truth', fill='lime', font=font)
            y_pos += 15
            if pred_geom is not None:
                conf_str = f'Cyan=Pred (conf={pred_conf:.2f})' if pred_conf else 'Cyan=Pred'
                draw.text((5, y_pos), conf_str, fill='cyan', font=font)
            
            out_path = output_dir / f'comparison_{pn}.png'
            img.save(str(out_path))
            print(f'  Saved {out_path}')


def print_detailed_scores(village, preds_gdf) -> None:
    """Print detailed per-plot scoring info."""
    if village.example_truths is None:
        return
    
    utm_crs = f'EPSG:{32600 + int((village.example_truths.geometry.iloc[0].centroid.x + 180) // 6) + 1}'
    truth_u = village.example_truths.to_crs(utm_crs)
    official_u = village.plots.to_crs(utm_crs)
    pred_u = preds_gdf.to_crs(utm_crs)
    
    print('\n  Per-plot details:')
    print(f'  {"Plot":>10s}  {"Status":>10s}  {"Conf":>6s}  {"IoU_off":>7s}  {"IoU_pred":>8s}  {"Improve":>7s}  {"CentErr":>7s}  Note')
    print(f'  {"─"*10}  {"─"*10}  {"─"*6}  {"─"*7}  {"─"*8}  {"─"*7}  {"─"*7}  {"─"*30}')
    
    for pn in village.example_truths.index:
        t = truth_u.loc[pn, 'geometry']
        o = official_u.loc[pn, 'geometry']
        
        iou_off = t.intersection(o).area / t.union(o).area if t.union(o).area > 0 else 0
        
        status = '—'
        conf = '—'
        iou_pred = '—'
        improvement = '—'
        cent_err = '—'
        note = ''
        
        if pn in preds_gdf.index:
            row = preds_gdf.loc[pn]
            status = str(row.get('status', '—'))
            
            if status == 'corrected':
                conf = f"{row.get('confidence', 0):.2f}"
                p = pred_u.loc[pn, 'geometry']
                iou_p = t.intersection(p).area / t.union(p).area if t.union(p).area > 0 else 0
                iou_pred = f'{iou_p:.3f}'
                improvement = f'{iou_p - iou_off:+.3f}'
                cent_err = f'{p.centroid.distance(t.centroid):.1f}m'
                note = str(row.get('method_note', ''))[:40]
        
        print(f'  {pn:>10s}  {status:>10s}  {conf:>6s}  {iou_off:7.3f}  {iou_pred:>8s}  {improvement:>7s}  {cent_err:>7s}  {note}')


def main(village_dir: str) -> None:
    """Run full evaluation."""
    village = load(village_dir)
    
    pred_path = Path(village_dir) / 'predictions.geojson'
    if not pred_path.exists():
        print(f'No predictions found at {pred_path}. Run solve.py first.')
        sys.exit(1)
    
    preds = read_predictions(pred_path)
    
    print(f'=== Evaluation: {village.slug} ===')
    print(f'  Predictions: {len(preds)} total')
    print(f'  Corrected: {sum(preds["status"] == "corrected")}')
    print(f'  Flagged: {sum(preds["status"] == "flagged")}')
    
    # Score
    print('\n── Official Scoring ──')
    sc = score(preds, village)
    print(sc)
    
    # Detailed per-plot breakdown
    print('\n── Per-Plot Analysis ──')
    print_detailed_scores(village, preds)
    
    # Confidence distribution
    corrected = preds[preds['status'] == 'corrected']
    if 'confidence' in corrected.columns and len(corrected) > 0:
        confs = corrected['confidence'].dropna()
        print(f'\n── Confidence Distribution ({len(confs)} corrected plots) ──')
        bins = [0, 0.2, 0.4, 0.6, 0.8, 1.01]
        for i in range(len(bins) - 1):
            count = ((confs >= bins[i]) & (confs < bins[i+1])).sum()
            bar = '█' * (count // max(len(confs) // 50, 1))
            print(f'  [{bins[i]:.1f}, {bins[i+1]:.1f}): {count:5d}  {bar}')
    
    # Generate comparison images
    print('\n── Generating comparison images ──')
    diag_dir = Path(village_dir) / 'diagnostics'
    visualize_comparison(village, preds, diag_dir)
    
    print('\nDone.')


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('Usage: uv run evaluate.py data/<village_slug>')
        sys.exit(1)
    main(sys.argv[1])
