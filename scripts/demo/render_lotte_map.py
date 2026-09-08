#!/usr/bin/env python3
"""Render the actual Lotte point-visibility raster and mapped example locations.

This is a scientific map made from native computation and source geometry, not
an AI anticipated view. Rendering is separate from all query timing. The input
report must already contain finalized selected_examples; this script does not
rank, invent, or promote examples into confirmed recommendations.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.collections import LineCollection
from matplotlib.colors import BoundaryNorm, ListedColormap
from matplotlib.font_manager import FontProperties, fontManager
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
import numpy as np
from pyproj import Transformer
from shapely.geometry import shape
from shapely.ops import transform as transform_geometry

from seoul_visibility import State, TargetPoint, VisibilityEngine


def render(report_path: Path, context_path: Path, manifest_path: Path, output: Path) -> dict:
    report = json.loads(report_path.read_text())
    context_bytes = context_path.read_bytes()
    if hashlib.sha256(context_bytes).hexdigest() != report["context_sha256"]:
        raise ValueError("Map context fingerprint differs from the finalized example report")
    context = json.loads(context_bytes)
    examples = report["selected_examples"]
    if not examples:
        raise ValueError("No finalized examples to display")
    landmark = context["landmarks"][0]
    target = TargetPoint(landmark["lon"], landmark["lat"], landmark["height_m"], "agl")
    radius = float(context["query_radius_m"])
    eye_height = float(report["request"]["eye_height_m"])
    curvature = float(report["reference_metadata"]["curvature_coefficient"])
    with VisibilityEngine.from_manifest(manifest_path) as engine:
        visibility = engine.visible_from_target(target, radius_m=radius, eye_height_m=eye_height,
            resolution_m=5, curvature_coefficient=curvature, use_cache=False)
    if visibility.metadata["source_data_version"] != report["reference_metadata"]["source_data_version"]:
        raise ValueError("Prepared data version differs from the finalized report")
    actual_target = visibility.metadata["target"]
    expected_target = report["reference_metadata"]["target"]
    if actual_target != expected_target:
        raise ValueError("Target geometry differs from finalized reference checks")
    xy = Transformer.from_crs(4326, 5186, always_xy=True)
    origin = (actual_target["effective"]["x"], actual_target["effective"]["y"])
    gt = visibility.transform
    rows, cols = visibility.states.shape
    extent = [gt[0] - origin[0], gt[0] + cols * gt[1] - origin[0],
              gt[3] + rows * gt[5] - origin[1], gt[3] - origin[1]]
    if not np.allclose([extent[0]+origin[0], extent[2]+origin[1],
                        extent[1]+origin[0], extent[3]+origin[1]], visibility.bounds):
        raise ValueError("Raster bounds/transform mismatch")

    korean_font = Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc")
    if korean_font.exists():
        # The font may have been installed after Matplotlib's disk cache was
        # created; register this known local file without rebuilding global caches.
        fontManager.addfont(korean_font)
        plt.rcParams["font.family"] = FontProperties(fname=korean_font).get_name()
    plt.rcParams.update({"font.size": 11, "axes.unicode_minus": False, "figure.facecolor": "#fcfcfa"})
    figure = plt.figure(figsize=(15, 10), dpi=160)
    axis = figure.add_axes([0.07, 0.19, 0.56, 0.70])
    side = figure.add_axes([0.69, 0.18, 0.29, 0.70])
    side.axis("off")
    colors = ["#dfb3a8", "#a9d8ad", "#e6e8ec", "#a896bb"]
    axis.imshow(visibility.states, origin="upper", extent=extent,
        cmap=ListedColormap(colors), norm=BoundaryNorm(np.arange(-0.5, 4.5), 4),
        interpolation="nearest", zorder=1)
    segments = []
    for edge in context["graph"]["edges"]:
        coordinates = np.asarray(edge.get("geometry", []), dtype=float)
        if len(coordinates) < 2:
            continue
        ex, ey = xy.transform(coordinates[:, 0], coordinates[:, 1])
        if (np.max(ex) < origin[0]-radius or np.min(ex) > origin[0]+radius
                or np.max(ey) < origin[1]-radius or np.min(ey) > origin[1]+radius):
            continue
        segments.append(np.column_stack([ex-origin[0], ey-origin[1]]))
    axis.add_collection(LineCollection(segments, colors="#505862", linewidths=0.45, alpha=0.55, zorder=3))
    for area in context["areas"]:
        if area.get("kind") != "water":
            continue
        geometry = transform_geometry(xy.transform, shape(area["geometry"]))
        polygons = list(geometry.geoms) if geometry.geom_type == "MultiPolygon" else [geometry]
        for polygon in polygons:
            if polygon.geom_type != "Polygon":
                continue
            for ring in [polygon.exterior, *polygon.interiors]:
                coords = np.asarray(ring.coords)
                axis.plot(coords[:, 0]-origin[0], coords[:, 1]-origin[1],
                          color="#087fa3", linewidth=1.75, zorder=5)
    axis.scatter([0], [0], marker="*", s=310, c="#f5c451", edgecolors="#573c0e", linewidths=1.3, zorder=10)
    axis.annotate("롯데월드타워\n상단 대표점", (0, 0), xytext=(16, 21), textcoords="offset points",
                  fontsize=12, weight="bold", bbox={"facecolor":"#ffffff", "alpha":0.9, "edgecolor":"none", "pad":4}, zorder=11)
    example_colors = ["#24458a", "#9a4c13", "#763b83"]
    for number, example in enumerate(examples, 1):
        ex, ey = xy.transform(example["lon"], example["lat"])
        location = [ex-origin[0], ey-origin[1]]
        color = example_colors[(number-1) % len(example_colors)]
        axis.plot([location[0], 0], [location[1], 0], color=color, linestyle=(0,(4,4)),
                  linewidth=1.1, alpha=0.85, zorder=6)
        axis.scatter([location[0]], [location[1]], s=340, facecolors="white", edgecolors=color, linewidths=2.1, zorder=12)
        axis.text(*location, str(number), color=color, ha="center", va="center", fontsize=13, weight="bold", zorder=13)
    start = report["request"]["start"]
    sx, sy = xy.transform(start["lon"], start["lat"])
    axis.scatter([sx-origin[0]], [sy-origin[1]], marker="s", s=55, c="#242b2f", edgecolors="white", zorder=8)
    axis.annotate("출발", (sx-origin[0], sy-origin[1]), xytext=(8,-13), textcoords="offset points", fontsize=10, zorder=9)
    axis.set_xlim(-radius-15, radius+15)
    axis.set_ylim(-radius-15, radius+15)
    axis.set_aspect("equal")
    axis.set_xlabel("모델 타워점에서 동서 거리 (m)  ·  EPSG:5186")
    axis.set_ylabel("모델 타워점에서 남북 거리 (m)")
    axis.grid(color="white", alpha=0.65, linewidth=0.55)
    axis.set_axisbelow(False)
    axis.annotate("N", xy=(850, 900), xytext=(850, 740), ha="center", fontsize=13, weight="bold",
        arrowprops={"arrowstyle":"-|>", "color":"#263338", "lw":1.8}, zorder=15)
    scale_x, scale_y = -850, -860
    axis.plot([scale_x, scale_x+250], [scale_y, scale_y], color="#263338", linewidth=3, zorder=15)
    for xx in (scale_x, scale_x+250):
        axis.plot([xx,xx], [scale_y-12,scale_y+12], color="#263338", linewidth=1.5, zorder=15)
    axis.text(scale_x+125, scale_y+32, "250 m", ha="center", fontsize=10,
              bbox={"facecolor":"white", "alpha":0.8, "edgecolor":"none", "pad":1}, zorder=16)

    side.text(0, 1.0, "실제 지도 좌표의 잠정 예시", fontsize=16, weight="bold", va="top", color="#183326")
    side.text(0, 0.94, f"확정 추천 0곳  ·  선택 예시 {len(examples)}곳\n개방·접근성은 아직 확인되지 않았습니다.",
              fontsize=11.5, va="top", linespacing=1.55)
    ypos = 0.82
    for number, example in enumerate(examples, 1):
        color = example_colors[(number-1) % len(example_colors)]
        side.text(0, ypos, f"{number}  {example['case_label']}", fontsize=13, weight="bold", color=color, va="top")
        detail = (f"{example['lat']:.6f}°N, {example['lon']:.6f}°E\n"
                  f"바라볼 방향 {example['bearing_deg']:.1f}°  ·  타워까지 {example['target_distance_m']:.0f} m\n"
                  f"도보 추정 {example['route']['travel_minutes']:.1f}분  ·  실제 지도 경로 기준\n"
                  f"격자 가시 / 별도 LOS 가시\n"
                  f"지도점과 계산 셀 중심 차이 {example['visibility_evidence']['snap_distance_m']:.2f} m")
        side.text(0, ypos-0.05, detail, fontsize=10.8, va="top", linespacing=1.5)
        ypos -= 0.26
    side.text(0, ypos+0.005, "계산 조건", fontsize=13, weight="bold", va="top")
    side.text(0, ypos-0.04,
        f"반경 {radius:,.0f} m  ·  지형/건물 격자 5 m\n"
        f"눈높이 {eye_height:g} m  ·  곡률계수 {curvature:.6f}\n"
        f"타워점: 지형 + {target.height_m:g} m (근사)\n"
        "2023 지형 + 대부분 2018/19 추정 건물고\n"
        "건물 전체·호수·반사의 가시성은 미검증",
        fontsize=10.8, va="top", linespacing=1.55)
    legend = [Patch(facecolor=colors[i], edgecolor="none", label=label)
              for i,label in enumerate(["차폐 blocked", "가시 visible", "제외 excluded", "미확인 unknown"])]
    legend += [Line2D([0],[0], color="#087fa3", linewidth=1.8, label="OSM 물 경계 (지도 위치)"),
               Line2D([0],[0], color="#505862", linewidth=0.8, label="OSM 경로")]
    figure.legend(handles=legend, loc="lower left", bbox_to_anchor=(0.073,0.075), ncol=3, frameon=False, fontsize=10.5,
                  columnspacing=2.1, handlelength=2)
    figure.text(0.07,0.953,"롯데월드타워가 보이는 위치 — 실제 입력으로 계산한 지도", fontsize=22, weight="bold", color="#183326")
    figure.text(0.07,0.918,"상단 대표점 하나의 기하학적 가시성 · 사진/AI 이미지가 아닌 계산 결과", fontsize=12, color="#47584e")
    figure.text(0.07,0.035,
        "녹색은 접근 가능한 장소나 수면 가시성을 뜻하지 않습니다. 파란 호수 윤곽은 지도 위치입니다. 나무·난간·공사와 전체 타워 형상은 미반영.\n"
        "자료: 서울시/NGII 지형, GBA 추정 건물고, © OpenStreetMap contributors (ODbL). 모델은 타워 장애물 높이를 과소표현하며 현장 가시성을 보증하지 않습니다.",
        fontsize=9.5, color="#4a5358", linespacing=1.45)
    output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output, dpi=160, facecolor=figure.get_facecolor())
    plt.close(figure)
    native_states = [example["visibility"] for example in examples]
    reference_states = [example["reference_visibility"] for example in examples]
    return {"output":str(output), "dimensions_px":[2400,1600], "bytes":output.stat().st_size,
            "selected_count":len(examples), "native_reference_selected_disagreements":sum(a!=b for a,b in zip(native_states,reference_states)),
            "shortlist_reference_states":report["reference_states"],
            "raster_state_counts":{state.name.lower():int((visibility.states==state).sum()) for state in State},
            "native_query_timings_s":visibility.timings, "scope":"Plot only; shortlist checks do not measure false-blocked rate."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=ROOT/"data/demo/lotte-run/lotte-example.json")
    parser.add_argument("--context", type=Path, default=ROOT/"data/demo/jamsil/context.json")
    parser.add_argument("--manifest", type=Path, default=ROOT/"data/seoul/processed/jamsil-gba-maximum/manifest.json")
    parser.add_argument("--output", type=Path, default=ROOT/"data/demo/lotte-run/visibility-map.png")
    arguments = parser.parse_args()
    print(json.dumps(render(arguments.report,arguments.context,arguments.manifest,arguments.output),ensure_ascii=False,indent=2))
