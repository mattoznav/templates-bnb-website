"""Download the CC0 assets the house is dressed with, from Poly Haven.

    python3 model/fetch_assets.py

Files land in model/assets/ (ignored by git) and are skipped when already
present. Every asset is CC0: https://polyhaven.com/license
Only needed to rebuild the model; the website ships the baked result.
"""

import json
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

API = "https://api.polyhaven.com"
OUT = Path(__file__).resolve().parent / "assets"
HEADERS = {"User-Agent": "alder-house-template (model/fetch_assets.py)"}

# Furniture, props and plants, as glTF at 1k textures
MODELS = [
    # Lounge
    "sofa_03", "ArmChair_01", "modern_coffee_table_01", "wooden_bookshelf_worn", "book_encyclopedia_set_01",
    "potted_plant_02", "mantel_clock_01", "fancy_picture_frame_01", "throw_pillows_01", "side_table_01",
    "wicker_basket_01",
    # Breakfast room
    "dining_table", "painted_wooden_chair_02", "tea_set_01", "wooden_bowl_01", "jug_01", "ceramic_vase_02",
    "wooden_cutting_board", "croissant", "potted_plant_04", "modern_ceiling_lamp_01",
    # Bedrooms
    "ClassicNightstand_01", "vintage_cabinet_01", "ornate_mirror_01", "hanging_picture_frame_01",
    "hanging_picture_frame_02", "painted_wooden_nightstand", "painted_wooden_cabinet", "small_wooden_table_01",
    "painted_wooden_chair_01", "standing_picture_frame_01", "wicker_basket_02", "sofa_02", "side_table_tall_01",
    "vintage_wooden_drawer_01", "calathea_orbifolia_01", "potted_plant_01", "modern_arm_chair_01",
    # Garden
    "tree_small_02", "fir_tree_01", "shrub_02", "shrub_03", "shrub_04", "outdoor_table_chair_set_01",
    "planter_box_01", "painted_wooden_bench", "industrial_wall_lamp", "watering_can_metal_01",
]

# Surfaces, as 2k JPG maps: colour, normal (OpenGL) and roughness
TEXTURES = [
    "white_stucco", "beige_wall_001", "wood_floor", "herringbone_parquet", "terracotta_floor_tiles",
    "clay_roof_tiles_02", "old_stone_wall", "grey_stone_path", "gravel_floor_02", "patio_tiles", "leafy_grass",
    "cotton_jersey", "wool_boucle", "fabric_pattern_07", "fabric_pattern_05", "wood_table_001", "white_oak_veneer",
    "long_white_tiles", "marble_01", "wood_shutter", "painted_plaster_wall",
]
TEXTURE_MAPS = {"Diffuse": "diff", "nor_gl": "nor_gl", "Rough": "rough"}

HDRI = "kloofendal_48d_partly_cloudy_puresky"


def fetch_json(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS)) as response:
        return json.load(response)


def download(url, target):
    if target.exists():
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS)) as response:
        tmp.write_bytes(response.read())
    tmp.rename(target)
    return target.stat().st_size


def jobs():
    for name in MODELS:
        gltf = fetch_json(f"{API}/files/{name}")["gltf"]["1k"]["gltf"]
        folder = OUT / "models" / name
        yield gltf["url"], folder / f"{name}.gltf"
        for relative, item in gltf["include"].items():
            yield item["url"], folder / relative
    for name in TEXTURES:
        files = fetch_json(f"{API}/files/{name}")
        # Patterned fabrics come in colour variants instead of a single "Diffuse"
        colour = "Diffuse" if "Diffuse" in files else sorted(k for k in files if k.startswith("col_"))[0]
        for key, suffix in {**TEXTURE_MAPS, "Diffuse": "diff"}.items():
            source = files[colour if key == "Diffuse" else key]
            yield source["2k"]["jpg"]["url"], OUT / "textures" / name / f"{suffix}.jpg"
    hdri = fetch_json(f"{API}/files/{HDRI}")["hdri"]["2k"]["hdr"]
    yield hdri["url"], OUT / "hdri" / f"{HDRI}.hdr"


def main():
    todo = list(jobs())
    print(f"{len(todo)} files")
    with ThreadPoolExecutor(max_workers=8) as pool:
        total = sum(pool.map(lambda job: download(*job), todo))
    print(f"downloaded {total / 1e6:.0f} MB into {OUT}")


if __name__ == "__main__":
    sys.exit(main())
