#!/usr/bin/env python3
"""
Data Exploration Script
========================
Explore village data: statistics, area ratio distribution, drift analysis,
and boundary hint quality. Builds intuition before writing the solver.

Usage:
  uv run explore.py data/34855_vadnerbhairav_chandavad_nashik
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import geopandas as gpd
from PIL import Image, ImageDraw

from bhume import load, patch_for_plot
from bhume.geo import open_imagery, geom_to_imagery_crs


def explore_village(village_dir: str) -> None:
    village = load(village_dir)

    plots = village.plots
    n = len(plots)

    print(f'=== {village.slug} ===')
    print(f'  Plots: {n}')
    print(f'  Example truths: {0 if village.example_truths is None else len(village.example_truths)}')
    print(f'  Boundaries: {"yes" if village.boundaries_path else "no"}')

    # Area statistics
    map_areas = plots['map_area_sqm'].dropna()
    print(f'\n── Map Area (sqm) ──')
    print(f'  Count: {len(map_areas)}')
    print(f'  Min: {map_areas.min():.1f}')
    print(f'  Median: {map_areas.median():.1f}')
    print(f'  Mean: {map_areas.mean():.1f}')
    print(f'  Max: {map_areas.max():.1f}')

    rec_areas = plots['recorded_area_sqm'].dropna()
    print(f'\n── Recorded Area (sqm) ──')
    print(f'  Count: {len(rec_areas)}  (null: {n - len(rec_areas)})')
    if len(rec_areas) > 0:
        print(f'  Min: {rec_areas.min():.1f}')
        print(f'  Median: {rec_areas.median():.1f}')
        print(f'  Mean: {rec_areas.mean():.1f}')
        print(f'  Max: {rec_areas.max():.1f}')

    # Area ratio analysis
    print(f'\n── Area Ratio (map_area / recorded_area) ──')
    ratios = []
    for pn in plots.index:
        row = plots.loc[pn]
        ma = row.get('map_area_sqm')
        ra = row.get('recorded_area_sqm')
        pk = row.get('pot_kharaba_ha')

        if ma is None or (ra is None and pk is None):
            continue

        total = 0
        if ra is not None and ra > 0:
            total += ra
        if pk is not None and pk > 0:
            total += pk * 10000

        if total > 0:
            ratios.append(ma / total)

    if ratios:
        ratios_arr = np.array(ratios)
        print(f'  Computed for: {len(ratios)} plots')
        print(f'  Min: {ratios_arr.min():.3f}')
        print(f'  Median: {np.median(ratios_arr):.3f}')
        print(f'  Mean: {ratios_arr.mean():.3f}')
        print(f'  Max: {ratios_arr.max():.3f}')

        # Distribution buckets
        bins = [0, 0.3, 0.5, 0.8, 1.0, 1.2, 1.5, 2.0, 3.5, 100]
        labels = ['<0.3 (way too small)', '0.3-0.5 (too small)', '0.5-0.8 (small)',
                  '0.8-1.0 (slightly small)', '1.0-1.2 (good match)',
                  '1.2-1.5 (slightly big)', '1.5-2.0 (too big)',
                  '2.0-3.5 (way too big)', '>3.5 (extreme)']
        print(f'\n  Distribution:')
        for i in range(len(bins) - 1):
            count = ((ratios_arr >= bins[i]) & (ratios_arr < bins[i + 1])).sum()
            pct = count / len(ratios_arr) * 100
            bar = '█' * int(pct / 2)
            print(f'    {labels[i]:25s}: {count:5d} ({pct:5.1f}%) {bar}')

    # Pot-kharaba analysis
    pk_vals = plots['pot_kharaba_ha'].dropna()
    print(f'\n── Pot-Kharaba ──')
    print(f'  Non-null: {len(pk_vals)} out of {n}')
    if len(pk_vals) > 0:
        print(f'  Non-zero: {(pk_vals > 0).sum()}')
        nz = pk_vals[pk_vals > 0]
        if len(nz) > 0:
            print(f'  Median (non-zero): {nz.median():.3f} ha')

    # Surveys analysis
    print(f'\n── Surveys Data ──')
    n_with_surveys = sum(1 for pn in plots.index
                        if plots.loc[pn, 'surveys'] and len(plots.loc[pn, 'surveys']) > 0)
    print(f'  Plots with survey data: {n_with_surveys} / {n}')

    # Example truths drift analysis
    if village.example_truths is not None:
        print(f'\n── Example Truths: Drift Analysis ──')
        utm = f'EPSG:{32600 + int((village.example_truths.geometry.iloc[0].centroid.x + 180) // 6) + 1}'
        official_u = plots.to_crs(utm)
        truth_u = village.example_truths.to_crs(utm)

        for pn in village.example_truths.index:
            if pn in official_u.index:
                o = official_u.loc[pn, 'geometry'].centroid
                t = truth_u.loc[pn, 'geometry'].centroid
                dx = t.x - o.x
                dy = t.y - o.y
                dist = np.sqrt(dx**2 + dy**2)
                
                # IoU
                o_geom = official_u.loc[pn, 'geometry']
                t_geom = truth_u.loc[pn, 'geometry']
                union = o_geom.union(t_geom).area
                iou = o_geom.intersection(t_geom).area / union if union > 0 else 0
                
                print(f'  {pn:>10s}: dx={dx:+7.1f}m  dy={dy:+7.1f}m  dist={dist:6.1f}m  IoU={iou:.3f}')

    # Save sample patches
    print(f'\n── Saving sample patches ──')
    diag_dir = Path(village_dir) / 'diagnostics'
    diag_dir.mkdir(exist_ok=True)

    with open_imagery(village.imagery_path) as src:
        sample_plots = list(plots.index[:6])
        if village.example_truths is not None:
            sample_plots = list(village.example_truths.index)

        for pn in sample_plots[:6]:
            try:
                geom = plots.loc[pn, 'geometry']
                patch = patch_for_plot(src, geom, pad_m=50)
                img = Image.fromarray(patch.image)
                
                # Draw polygon outline
                geom_crs = geom_to_imagery_crs(src, geom)
                from shapely.geometry import MultiPolygon
                if isinstance(geom_crs, MultiPolygon):
                    polys = list(geom_crs.geoms)
                    largest = max(polys, key=lambda p: p.area)
                else:
                    largest = geom_crs
                
                exterior = np.array(largest.exterior.coords)
                pixel_coords = []
                for x, y in exterior:
                    col = (x - patch.transform.c) / patch.transform.a
                    row = (y - patch.transform.f) / patch.transform.e
                    pixel_coords.append((float(col), float(row)))
                
                draw = ImageDraw.Draw(img)
                pts = pixel_coords + [pixel_coords[0]]
                draw.line(pts, fill='red', width=2)
                draw.text((5, 5), f'Plot {pn}', fill='white')
                
                out = diag_dir / f'explore_{pn}.png'
                img.save(str(out))
                print(f'  Saved {out}')
            except Exception as e:
                print(f'  Error for {pn}: {e}')


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('Usage: uv run explore.py data/<village_slug>')
        sys.exit(1)
    explore_village(sys.argv[1])
