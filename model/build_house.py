"""Build the Alder House model, render the room photos and export the web model.

Run from the website folder, after downloading the CC0 assets once:

    python3 model/fetch_assets.py
    blender -b --factory-startup -P model/build_house.py -- [--quick] [--skip-photos] [--skip-bake]

Steps:
1. Build the shell of the house from code (walls, windows, shutters, roof,
   gutters, porch) with PBR textures, then furnish it with Poly Haven models
   and dress the garden with trees, hedges and garden furniture.
2. Render one photo per room with Cycles into public/media/rooms/.
3. Bake the light of every part into its own texture (a lightmap), denoised.
4. Render the trees from the side and from above, for light "impostor" cards.
5. Export public/models/alder-house.glb with Draco geometry and WebP textures.

The website shows the baked textures without any lights, so the model looks
rendered on every device and costs little to draw. Room layout comes from
src/data/house.json, which the website also reads for its floor plan.
"""

import json
import math
import random
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import bmesh
import bpy
import numpy as np
from mathutils import Matrix, Vector

ROOT = Path(__file__).resolve().parent.parent
LAYOUT = json.loads((ROOT / "src/data/house.json").read_text())
ASSETS = ROOT / "model/assets"
MODEL_OUT = ROOT / "public/models/alder-house.glb"
PHOTO_DIR = ROOT / "public/media/rooms"
BAKE_DIR = ROOT / "model/.bake"

ARGS = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
QUICK = "--quick" in ARGS
SKIP_PHOTOS = "--skip-photos" in ARGS
# --photos=lounge,exterior renders only those photos
ONLY_PHOTOS = next((a.split("=", 1)[1].split(",") for a in ARGS if a.startswith("--photos=")), None)
SKIP_BAKE = "--skip-bake" in ARGS
# --rebake=walls,roof reuses the lightmaps in model/.bake/ for everything else
# Quick runs keep their lightmaps apart, so they never replace the full-quality ones
if QUICK:
    BAKE_DIR = ROOT / "model/.bake-quick"
REBAKE = next((a.split("=", 1)[1].split(",") for a in ARGS if a.startswith("--rebake=")), None)

H = LAYOUT["wallHeight"]
EXT = 0.3  # exterior wall thickness
INT = 0.12  # interior wall thickness
GROUND = 0.15  # garden level below the floors
FW, FD = LAYOUT["footprint"]["w"], LAYOUT["footprint"]["d"]
ROOMS = {r["slug"]: r for r in LAYOUT["rooms"]}
BATHS = LAYOUT["baths"]
BATH_T = 0.1  # bathroom partition thickness
HDRI = ASSETS / "hdri/kloofendal_48d_partly_cloudy_puresky.hdr"

# Assets drawn on the web with their own textures instead of a lightmap
FOLIAGE = {"potted_plant_01", "potted_plant_02", "potted_plant_04"}
# Trees stay at full detail: they only cast shadows and are rendered into cards
TREE_ASSETS = {"tree_small_02", "fir_tree_01"}
# Heavier props are simplified to roughly this many triangles
MAX_TRIS, MAX_FOLIAGE_TRIS = 12000, 30000

random.seed(7)


def log(message):
    print(message, flush=True)


# ---------------------------------------------------------------- scene setup


def reset_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    prefs = bpy.context.preferences.addons["cycles"].preferences
    try:
        prefs.compute_device_type = "METAL"
        prefs.refresh_devices()
        for device in prefs.devices:
            device.use = True
        scene.cycles.device = "GPU"
    except TypeError:
        scene.cycles.device = "CPU"
    scene.cycles.max_bounces = 8
    scene.cycles.diffuse_bounces = 4
    scene.cycles.glossy_bounces = 2
    scene.cycles.transmission_bounces = 4
    scene.cycles.transparent_max_bounces = 16
    scene.cycles.sample_clamp_indirect = 8.0
    return scene


# ------------------------------------------------------------------ materials


def _hex(value):
    value = value.lstrip("#")
    srgb = [int(value[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    return tuple(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in srgb)


def _principled(name, color, roughness=0.7):
    mat = bpy.data.materials.new(name)
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    bsdf.inputs["Base Color"].default_value = (*color, 1.0)
    bsdf.inputs["Roughness"].default_value = roughness
    return mat, bsdf


def _image(path, colorspace):
    image = bpy.data.images.load(str(path), check_existing=True)
    image.colorspace_settings.name = colorspace
    return image


def _textured(nodes, links, folder, scale, tint=None, bump=0.25, rotate=0.0, vector=None):
    """Box-projected PBR maps (colour, roughness, bump) in world-sized tiles of `scale` metres.

    Returns (colour output, roughness output, normal output). The geometry is
    built without UVs, so the textures are projected from the three axes.
    """
    if vector is None:
        coords = nodes.new("ShaderNodeTexCoord")
        vector = coords.outputs["Object"]
    mapping = nodes.new("ShaderNodeMapping")
    mapping.inputs["Scale"].default_value = (1 / scale, 1 / scale, 1 / scale)
    mapping.inputs["Rotation"].default_value = (0, 0, math.radians(rotate))
    links.new(vector, mapping.inputs["Vector"])

    def tex(name, colorspace):
        node = nodes.new("ShaderNodeTexImage")
        node.image = _image(ASSETS / "textures" / folder / f"{name}.jpg", colorspace)
        node.projection = "BOX"
        node.projection_blend = 0.3
        links.new(mapping.outputs["Vector"], node.inputs["Vector"])
        return node

    diff = tex("diff", "sRGB")
    rough = tex("rough", "Non-Color")
    colour = diff.outputs["Color"]
    if tint is not None:
        mix = nodes.new("ShaderNodeMixRGB")
        mix.blend_type = "MULTIPLY"
        mix.inputs["Fac"].default_value = 1.0
        mix.inputs["Color2"].default_value = (*tint, 1)
        links.new(colour, mix.inputs["Color1"])
        colour = mix.outputs["Color"]
    bump_node = nodes.new("ShaderNodeBump")
    bump_node.inputs["Strength"].default_value = bump
    bump_node.inputs["Distance"].default_value = 0.01
    links.new(diff.outputs["Color"], bump_node.inputs["Height"])
    return colour, rough.outputs["Color"], bump_node.outputs["Normal"]


def textured_material(name, folder, scale, tint=None, bump=0.25, rotate=0.0, roughness=None):
    mat = bpy.data.materials.new(name)
    nodes, links = mat.node_tree.nodes, mat.node_tree.links
    bsdf = nodes.get("Principled BSDF")
    colour, rough, normal = _textured(nodes, links, folder, scale, tint, bump, rotate)
    links.new(colour, bsdf.inputs["Base Color"])
    if roughness is None:
        links.new(rough, bsdf.inputs["Roughness"])
    else:
        bsdf.inputs["Roughness"].default_value = roughness
    links.new(normal, bsdf.inputs["Normal"])
    return mat


def wall_material():
    """Stucco on the outside of the house, painted plaster inside it.

    Walls are single boxes, so the shader picks the finish from the position:
    anything outside the footprint is the facade.
    """
    mat = bpy.data.materials.new("wall")
    nodes, links = mat.node_tree.nodes, mat.node_tree.links
    bsdf_out = nodes.get("Principled BSDF")
    output = nodes.get("Material Output")
    bsdf_in = nodes.new("ShaderNodeBsdfPrincipled")
    c_out, r_out, n_out = _textured(nodes, links, "white_stucco", 2.5, tint=_hex("#f2ece0"), bump=0.4)
    c_in, r_in, n_in = _textured(nodes, links, "white_stucco", 3.0, tint=_hex("#f3eee6"), bump=0.08)
    for bsdf, c, r, n in ((bsdf_out, c_out, r_out, n_out), (bsdf_in, c_in, r_in, n_in)):
        links.new(c, bsdf.inputs["Base Color"])
        links.new(r, bsdf.inputs["Roughness"])
        links.new(n, bsdf.inputs["Normal"])
    coords = nodes.new("ShaderNodeTexCoord")
    xyz = nodes.new("ShaderNodeSeparateXYZ")
    links.new(coords.outputs["Object"], xyz.inputs["Vector"])

    def inside(axis, low, high):
        a = nodes.new("ShaderNodeMath")
        a.operation = "GREATER_THAN"
        a.inputs[1].default_value = low
        links.new(xyz.outputs[axis], a.inputs[0])
        b = nodes.new("ShaderNodeMath")
        b.operation = "LESS_THAN"
        b.inputs[1].default_value = high
        links.new(xyz.outputs[axis], b.inputs[0])
        m = nodes.new("ShaderNodeMath")
        m.operation = "MULTIPLY"
        links.new(a.outputs[0], m.inputs[0])
        links.new(b.outputs[0], m.inputs[1])
        return m.outputs[0]

    both = nodes.new("ShaderNodeMath")
    both.operation = "MULTIPLY"
    links.new(inside("X", -0.01, FW + 0.01), both.inputs[0])
    links.new(inside("Y", -0.01, FD + 0.01), both.inputs[1])
    # Above the walls (gables) is always facade
    low = nodes.new("ShaderNodeMath")
    low.operation = "LESS_THAN"
    low.inputs[1].default_value = H + 0.01
    links.new(xyz.outputs["Z"], low.inputs[0])
    interior = nodes.new("ShaderNodeMath")
    interior.operation = "MULTIPLY"
    links.new(both.outputs[0], interior.inputs[0])
    links.new(low.outputs[0], interior.inputs[1])
    mix = nodes.new("ShaderNodeMixShader")
    links.new(interior.outputs[0], mix.inputs["Fac"])
    links.new(bsdf_out.outputs[0], mix.inputs[1])
    links.new(bsdf_in.outputs[0], mix.inputs[2])
    links.new(mix.outputs[0], output.inputs["Surface"])
    return mat


def foliage_material(name, base, dark):
    """Clipped box hedge: two greens broken up by Voronoi leaves, with bump."""
    mat, bsdf = _principled(name, base, 0.7)
    nodes, links = mat.node_tree.nodes, mat.node_tree.links
    coords = nodes.new("ShaderNodeTexCoord")
    voronoi = nodes.new("ShaderNodeTexVoronoi")
    voronoi.inputs["Scale"].default_value = 38.0
    links.new(coords.outputs["Object"], voronoi.inputs["Vector"])
    noise = nodes.new("ShaderNodeTexNoise")
    noise.inputs["Scale"].default_value = 2.0
    links.new(coords.outputs["Object"], noise.inputs["Vector"])
    ramp = nodes.new("ShaderNodeValToRGB")
    ramp.color_ramp.elements[0].color = (*dark, 1)
    ramp.color_ramp.elements[1].color = (*base, 1)
    mix = nodes.new("ShaderNodeMath")
    mix.operation = "MULTIPLY"
    links.new(voronoi.outputs["Distance"], mix.inputs[0])
    links.new(noise.outputs["Fac"], mix.inputs[1])
    links.new(mix.outputs[0], ramp.inputs["Fac"])
    links.new(ramp.outputs["Color"], bsdf.inputs["Base Color"])
    bump = nodes.new("ShaderNodeBump")
    bump.inputs["Strength"].default_value = 0.8
    links.new(voronoi.outputs["Distance"], bump.inputs["Height"])
    links.new(bump.outputs["Normal"], bsdf.inputs["Normal"])
    return mat


def fabric_material(name, color, roughness):
    """Plain woven fabric: flat colour with a fine weave bump and soft sheen."""
    mat, bsdf = _principled(name, color, roughness)
    nodes, links = mat.node_tree.nodes, mat.node_tree.links
    coords = nodes.new("ShaderNodeTexCoord")
    noise = nodes.new("ShaderNodeTexNoise")
    noise.inputs["Scale"].default_value = 180.0
    links.new(coords.outputs["Object"], noise.inputs["Vector"])
    bump = nodes.new("ShaderNodeBump")
    bump.inputs["Strength"].default_value = 0.15
    links.new(noise.outputs["Fac"], bump.inputs["Height"])
    links.new(bump.outputs["Normal"], bsdf.inputs["Normal"])
    bsdf.inputs["Sheen Weight"].default_value = 0.4
    return mat


def painting_material(name, palette):
    """A loose landscape in oils: colour bands broken up by noise, with brush-stroke bump."""
    mat, bsdf = _principled(name, _hex(palette[0]), 0.6)
    nodes, links = mat.node_tree.nodes, mat.node_tree.links
    coords = nodes.new("ShaderNodeTexCoord")
    noise = nodes.new("ShaderNodeTexNoise")
    noise.inputs["Scale"].default_value = 3.0
    noise.inputs["Detail"].default_value = 8.0
    links.new(coords.outputs["Object"], noise.inputs["Vector"])
    xyz = nodes.new("ShaderNodeSeparateXYZ")
    links.new(coords.outputs["Object"], xyz.inputs["Vector"])
    add = nodes.new("ShaderNodeMath")
    add.operation = "MULTIPLY_ADD"
    add.inputs[1].default_value = 0.5
    links.new(noise.outputs["Fac"], add.inputs[0])
    links.new(xyz.outputs["Z"], add.inputs[2])
    frac = nodes.new("ShaderNodeMath")
    frac.operation = "FRACT"
    links.new(add.outputs[0], frac.inputs[0])
    ramp = nodes.new("ShaderNodeValToRGB")
    ramp.color_ramp.interpolation = "CONSTANT"
    elements = ramp.color_ramp.elements
    elements[0].color = (*_hex(palette[0]), 1)
    elements[1].position = 0.25
    elements[1].color = (*_hex(palette[1]), 1)
    for position, colour in ((0.55, palette[2]), (0.8, palette[3])):
        e = elements.new(position)
        e.color = (*_hex(colour), 1)
    links.new(frac.outputs[0], ramp.inputs["Fac"])
    links.new(ramp.outputs["Color"], bsdf.inputs["Base Color"])
    strokes = nodes.new("ShaderNodeTexNoise")
    strokes.inputs["Scale"].default_value = 60.0
    links.new(coords.outputs["Object"], strokes.inputs["Vector"])
    bump = nodes.new("ShaderNodeBump")
    bump.inputs["Strength"].default_value = 0.3
    links.new(strokes.outputs["Fac"], bump.inputs["Height"])
    links.new(bump.outputs["Normal"], bsdf.inputs["Normal"])
    return mat


def make_materials():
    m = {}
    flat = {
        "trim": ("#f3efe6", 0.45),
        "frame": ("#46554a", 0.4),
        "door_paint": ("#5b6e5f", 0.35),
        "linen": ("#f2eee6", 0.85),
        "black_metal": ("#24231f", 0.35),
        "brass": ("#b8924f", 0.3),
        "ceramic": ("#f4f2ee", 0.12),
        "soot": ("#1b1816", 0.95),
        "radiator": ("#ecebe6", 0.3),
        "zinc": ("#8c918f", 0.4),
        "soil": ("#3d2f25", 0.95),
        "book_a": ("#7a3329", 0.6),
        "book_b": ("#2c4563", 0.6),
        "book_c": ("#b8924f", 0.6),
        "book_d": ("#55654a", 0.6),
        "book_e": ("#e3d9c6", 0.6),
        "book_f": ("#3b3530", 0.6),
        "paint_sage": ("#9fae97", 0.5),
        "paint_cream": ("#e6dcc8", 0.5),
    }
    for name, (color, rough) in flat.items():
        m[name], _ = _principled(name, _hex(color), rough)
    for name in ("brass", "black_metal", "zinc"):
        m[name].node_tree.nodes["Principled BSDF"].inputs["Metallic"].default_value = 1.0

    m["wall"] = wall_material()
    m["ceiling"] = textured_material("ceiling", "white_stucco", 3.0, tint=_hex("#f6f3ee"), bump=0.04)
    m["floor_oak"] = textured_material("floor_oak", "wood_floor", 2.2, bump=0.15)
    m["floor_herringbone"] = textured_material("floor_herringbone", "herringbone_parquet", 1.8, bump=0.15)
    m["floor_tiles"] = textured_material("floor_tiles", "terracotta_floor_tiles", 1.6, bump=0.3)
    m["roof_tiles"] = textured_material("roof_tiles", "clay_roof_tiles_02", 2.0, tint=_hex("#b9a497"), bump=0.8, rotate=90)
    m["stone"] = textured_material("stone", "old_stone_wall", 2.0, bump=0.6)
    m["sill"] = textured_material("sill", "old_stone_wall", 1.2, tint=_hex("#ece4d4"), bump=0.3)
    m["path"] = textured_material("path", "grey_stone_path", 2.5, bump=0.6)
    m["gravel"] = textured_material("gravel", "gravel_floor_02", 1.5, bump=0.6)
    m["patio"] = textured_material("patio", "patio_tiles", 2.0, bump=0.4)
    m["grass"] = textured_material("grass", "leafy_grass", 2.5, tint=_hex("#c9d7b4"), bump=0.5)
    m["far_grass"] = textured_material("far_grass", "leafy_grass", 6.0, tint=_hex("#a9bd8c"), bump=0.3)
    m["shutter"] = textured_material("shutter", "wood_shutter", 1.2, tint=_hex("#9db3a2"), bump=0.4)
    m["oak"] = textured_material("oak", "white_oak_veneer", 1.0, bump=0.05)
    m["walnut"] = textured_material("walnut", "wood_table_001", 1.2, tint=_hex("#9a7a62"), bump=0.05)
    for name, color, rough in (("cotton", "#f3f0ea", 0.9), ("duvet_sage", "#8fa385", 0.92), ("duvet_blue", "#7f98ad", 0.92),
                               ("duvet_clay", "#b9785a", 0.95), ("throw", "#a69b86", 0.95), ("curtain", "#efe8da", 0.9),
                               ("curtain_sage", "#bcc6b2", 0.9)):
        m[name] = fabric_material(name, _hex(color), rough)
    m["rug"] = textured_material("rug", "fabric_pattern_05", 1.6, tint=_hex("#ead8c4"), bump=0.2, roughness=1.0)
    m["rug_blue"] = textured_material("rug_blue", "fabric_pattern_05", 1.2, bump=0.2, roughness=1.0)
    m["art_a"] = painting_material("art_a", ["#2f4a5c", "#7f9c8f", "#d9c9a3", "#c27b4f"])
    m["art_b"] = painting_material("art_b", ["#3c4a35", "#8aa06a", "#e6dcc3", "#9fb7c8"])
    m["marble"] = textured_material("marble", "marble_01", 1.2, bump=0.02)
    m["bath_floor"] = textured_material("bath_floor", "long_white_tiles", 0.9, tint=_hex("#d7dbd4"), bump=0.25)
    m["mirror"], bsdf = _principled("mirror", _hex("#dfe4e3"), 0.04)
    bsdf.inputs["Metallic"].default_value = 1.0
    m["splash_tiles"] = textured_material("splash_tiles", "long_white_tiles", 1.0, bump=0.2)
    m["painted_wood"] = textured_material("painted_wood", "white_oak_veneer", 1.0, tint=_hex("#a9b8a0"), bump=0.05, roughness=0.5)
    m["picket"] = fabric_material("picket", _hex("#eeebe3"), 0.55)
    m["hedge"] = foliage_material("hedge", _hex("#4d6b35"), _hex("#22341a"))
    m["hedge_light"] = foliage_material("hedge_light", _hex("#6f8a45"), _hex("#334a22"))

    # Light sources: lamp shades, bulbs and the fire
    for name, color, strength in (("lampshade", "#f6e2bd", 3.0), ("bulb", "#ffd9a0", 30.0), ("embers", "#ff7a2e", 14.0)):
        mat, bsdf = _principled(name, _hex(color), 0.9)
        bsdf.inputs["Emission Color"].default_value = (*_hex(color), 1)
        bsdf.inputs["Emission Strength"].default_value = strength
        m[name] = mat

    mat, bsdf = _principled("glass", (0.95, 0.97, 1.0), 0.0)
    bsdf.inputs["Transmission Weight"].default_value = 1.0
    bsdf.inputs["IOR"].default_value = 1.45
    m["glass"] = mat
    return m


# ------------------------------------------------------------- mesh building


class Group:
    """Geometry collected into a single object, which gets one lightmap."""

    def __init__(self, name, materials):
        self.name = name
        self.bm = bmesh.new()
        self.materials = materials
        self.slots = []
        self.matrix = Matrix.Identity(4)

    def _slot(self, mat_name):
        if mat_name not in self.slots:
            self.slots.append(mat_name)
        return self.slots.index(mat_name)

    @contextmanager
    def at(self, x=0.0, y=0.0, z=0.0, rot=0.0):
        """Place the pieces added inside the block relative to (x, y, z), rotated in degrees."""
        saved = self.matrix
        self.matrix = saved @ Matrix.Translation((x, y, z)) @ Matrix.Rotation(math.radians(rot), 4, "Z")
        try:
            yield
        finally:
            self.matrix = saved

    def merge(self, piece, mat_name, bevel=0.0, segments=1, absolute=False):
        if bevel:
            bmesh.ops.bevel(
                piece, geom=list(piece.edges), offset=bevel, segments=segments, profile=0.5,
                affect="EDGES", clamp_overlap=True,
            )
        index = self._slot(mat_name)
        for face in piece.faces:
            face.material_index = index
            face.smooth = segments > 1
        if not absolute:
            bmesh.ops.transform(piece, matrix=self.matrix, verts=list(piece.verts))
        mesh = bpy.data.meshes.new("tmp")
        piece.to_mesh(mesh)
        piece.free()
        self.bm.from_mesh(mesh)
        bpy.data.meshes.remove(mesh)

    def box(self, x0, y0, z0, x1, y1, z1, mat, bevel=0.0, segments=1, rot=None):
        """Box from corner to corner (optionally rotated around its centre)."""
        piece = bmesh.new()
        size = (abs(x1 - x0), abs(y1 - y0), abs(z1 - z0))
        local = Matrix.Translation(((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2))
        if rot is not None:
            local = local @ rot
        local = local @ Matrix.Diagonal((*size, 1.0))
        bmesh.ops.create_cube(piece, size=1.0, matrix=local)
        self.merge(piece, mat, min(bevel, min(size) * 0.45) if bevel else 0.0, segments)

    def soft(self, x0, y0, z0, x1, y1, z1, mat, radius=0.05, segments=3):
        """Upholstery: a box with rounded edges."""
        self.box(x0, y0, z0, x1, y1, z1, mat, bevel=radius, segments=segments)

    def cylinder(self, x, y, z0, z1, r0, r1=None, mat="oak", segments=20, axis="Z"):
        piece = bmesh.new()
        depth = z1 - z0
        rotation = {"Z": Matrix.Identity(4), "X": Matrix.Rotation(math.pi / 2, 4, "Y"), "Y": Matrix.Rotation(math.pi / 2, 4, "X")}[axis]
        center = {"Z": (x, y, z0 + depth / 2), "X": (z0 + depth / 2, x, y), "Y": (x, z0 + depth / 2, y)}[axis]
        bmesh.ops.create_cone(
            piece, cap_ends=True, cap_tris=False, segments=segments, radius1=r0,
            radius2=r0 if r1 is None else r1, depth=depth, matrix=Matrix.Translation(center) @ rotation,
        )
        self.merge(piece, mat)

    def sphere(self, x, y, z, r, mat, squash=1.0, subdivisions=2, jitter=0.0):
        piece = bmesh.new()
        bmesh.ops.create_icosphere(
            piece, subdivisions=subdivisions, radius=r,
            matrix=Matrix.Translation((x, y, z)) @ Matrix.Diagonal((1, 1, squash, 1)),
        )
        if jitter:
            for v in piece.verts:
                v.co += v.normal * random.uniform(-jitter, jitter) * r
        for face in piece.faces:
            face.smooth = True
        self.merge(piece, mat, segments=2)

    def sheet(self, points, mat):
        """A single polygon (list of xyz points), used for curtains and gables."""
        piece = bmesh.new()
        verts = [piece.verts.new(p) for p in points]
        piece.faces.new(verts)
        self.merge(piece, mat)

    def build(self, collection):
        mesh = bpy.data.meshes.new(self.name)
        bmesh.ops.remove_doubles(self.bm, verts=list(self.bm.verts), dist=0.0002)
        self.bm.to_mesh(mesh)
        self.bm.free()
        for slot in self.slots:
            mesh.materials.append(self.materials[slot])
        obj = bpy.data.objects.new(self.name, mesh)
        collection.objects.link(obj)
        return obj


# --------------------------------------------------------------------- assets


class Library:
    """Poly Haven models, imported once and placed as copies."""

    def __init__(self, collection):
        self.collection = collection
        self.parts = {}
        self.placed = {}  # group name -> list of objects

    def load(self, name):
        if name in self.parts:
            return self.parts[name]
        before = set(bpy.data.objects)
        bpy.ops.import_scene.gltf(filepath=str(ASSETS / "models" / name / f"{name}.gltf"))
        imported = [o for o in bpy.data.objects if o not in before]
        meshes = []
        for obj in imported:
            if obj.type == "MESH":
                world = obj.matrix_world.copy()
                obj.parent = None
                obj.matrix_world = world
                for col in obj.users_collection:
                    col.objects.unlink(obj)
                self.collection.objects.link(obj)
                obj.hide_render = True
                meshes.append(obj)
        for obj in imported:
            if obj.type != "MESH":
                bpy.data.objects.remove(obj)
        limit = MAX_FOLIAGE_TRIS if name in FOLIAGE else MAX_TRIS
        for obj in meshes:
            # Bake the import transform into the mesh, so copies only need placing
            if obj.data.users > 1:
                obj.data = obj.data.copy()
            obj.data.transform(obj.matrix_world)
            obj.matrix_world = Matrix.Identity(4)
            tris = sum(len(p.vertices) - 2 for p in obj.data.polygons)
            if tris > limit and name not in TREE_ASSETS:
                decimate(obj, limit / tris)
        meshes.sort(key=lambda o: centre(o).x)
        self.parts[name] = meshes
        return meshes

    def place(self, group, name, x, y, z=0.0, face=90.0, scale=1.0, part=None):
        """Place an asset with its front facing `face` degrees (0 = +x, 90 = +y)."""
        meshes = self.load(name)
        chosen = meshes if part is None else [meshes[part]]
        offset = Vector((0, 0, 0))
        if part is not None:
            c = centre(meshes[part])
            offset = Vector((-c.x, -c.y, 0))
        # Poly Haven models face -y; turn them to face `face`
        placement = (
            Matrix.Translation((x, y, z))
            @ Matrix.Rotation(math.radians(face + 90), 4, "Z")
            @ Matrix.Diagonal((scale, scale, scale, 1))
            @ Matrix.Translation(offset)
        )
        copies = []
        for source in chosen:
            copy = source.copy()
            copy["asset"] = name
            copy.hide_render = False
            copy.matrix_world = placement
            bpy.context.scene.collection.objects.link(copy)
            copies.append(copy)
        self.placed.setdefault(group, []).extend(copies)
        return copies


def shelf_levels(meshes):
    """Heights of the shelves of a bookcase: large upward faces, ignoring the top."""
    levels = []
    for obj in meshes:
        for poly in obj.data.polygons:
            if poly.normal.z > 0.95 and poly.area > 0.05:
                z = round((obj.matrix_world @ poly.center).z, 2)
                if not any(abs(z - l) < 0.05 for l in levels):
                    levels.append(z)
    levels.sort()
    return [z for z in levels if 0.05 < z < levels[-1] - 0.1] if levels else []


def centre(obj):
    pts = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    return sum(pts, Vector()) / 8


def decimate(obj, ratio):
    modifier = obj.modifiers.new("decimate", "DECIMATE")
    modifier.ratio = max(ratio, 0.02)
    with bpy.context.temp_override(object=obj, active_object=obj):
        bpy.ops.object.modifier_apply(modifier=modifier.name)


def realise(objects, name):
    """Join placed copies (and their own mesh data) into one object."""
    for obj in objects:
        obj.data = obj.data.copy()
        obj.data.transform(obj.matrix_world)
        obj.matrix_world = Matrix.Identity(4)
    target = objects[0]
    if len(objects) > 1:
        with bpy.context.temp_override(active_object=target, selected_editable_objects=objects):
            bpy.ops.object.join()
    target.name = name
    target.data.name = name
    return target


# --------------------------------------------------------------------- walls


def wall(g, a, b, thick, openings=(), height=H, mat="wall"):
    """Axis-aligned wall from a to b (centre line) with openings.

    Each opening is (centre distance from a, width, bottom, top).
    """
    (ax, ay), (bx, by) = a, b
    horizontal = ay == by
    length = abs(bx - ax) if horizontal else abs(by - ay)
    start = min(ax, bx) if horizontal else min(ay, by)
    half = thick / 2

    def span(s0, s1, z0, z1):
        if s1 - s0 < 1e-4 or z1 - z0 < 1e-4:
            return
        if horizontal:
            g.box(start + s0, ay - half, z0, start + s1, ay + half, z1, mat)
        else:
            g.box(ax - half, start + s0, z0, ax + half, start + s1, z1, mat)

    cursor = 0.0
    for center, width, bottom, top in sorted(openings):
        o0, o1 = center - width / 2, center + width / 2
        span(cursor, o0, 0, height)
        span(o0, o1, 0, bottom)
        span(o0, o1, top, height)
        cursor = o1
    span(cursor, length, 0, height)


class Opening:
    """An opening in an exterior wall, with helpers to build in its local frame.

    Local frame: u runs along the wall, v points out of the house, z up.
    """

    def __init__(self, a, b, center, width, bottom, top):
        (ax, ay), (bx, by) = a, b
        self.horizontal = ay == by
        start = min(ax, bx) if self.horizontal else min(ay, by)
        self.u0, self.u1 = start + center - width / 2, start + center + width / 2
        self.line = ay if self.horizontal else ax
        # Outward direction: front and west walls face negative axes
        self.out = -1 if self.line <= 0 else 1
        self.width, self.bottom, self.top = width, bottom, top

    def box(self, g, u0, u1, z0, z1, v0, v1, mat, **kw):
        """Box between u0..u1 along the wall and v0..v1 outward from the wall centre line."""
        w0, w1 = self.line + self.out * v0, self.line + self.out * v1
        if self.horizontal:
            g.box(u0, min(w0, w1), z0, u1, max(w0, w1), z1, mat, **kw)
        else:
            g.box(min(w0, w1), u0, z0, max(w0, w1), u1, z1, mat, **kw)

    def point(self, u, v, z):
        w = self.line + self.out * v
        return (u, w, z) if self.horizontal else (w, u, z)

    @property
    def inward(self):
        """Unit vector pointing into the house."""
        return Vector((0, -self.out, 0)) if self.horizontal else Vector((-self.out, 0, 0))


def window(walls, glass, rooms_g, op, curtain_mat, dressed=True):
    """Sash window set back in the wall, with stone sill, shutters, curtains."""
    u0, u1, b, t = op.u0, op.u1, op.bottom, op.top
    f, d = 0.07, 0.02  # frame width, frame plane offset (towards the outside)
    # Reveal lining: the opening is plastered by the wall boxes; add the frame
    op.box(walls, u0, u0 + f, b, t, d - 0.05, d + 0.05, "frame")
    op.box(walls, u1 - f, u1, b, t, d - 0.05, d + 0.05, "frame")
    op.box(walls, u0, u1, b, b + f, d - 0.05, d + 0.05, "frame")
    op.box(walls, u0, u1, t - f, t, d - 0.05, d + 0.05, "frame")
    # Two sashes with glazing bars
    mid_z = (b + t) / 2 + 0.05
    op.box(walls, u0, u1, mid_z - 0.03, mid_z + 0.03, d - 0.04, d + 0.04, "frame")
    for frac in (1 / 3, 2 / 3):
        u = u0 + (u1 - u0) * frac
        op.box(walls, u - 0.015, u + 0.015, b, t, d - 0.02, d + 0.02, "frame")
    op.box(walls, u0, u1, (b + mid_z) / 2 - 0.015, (b + mid_z) / 2 + 0.015, d - 0.02, d + 0.02, "frame")
    op.box(walls, u0, u1, (mid_z + t) / 2 - 0.015, (mid_z + t) / 2 + 0.015, d - 0.02, d + 0.02, "frame")
    op.box(glass, u0 + f, u1 - f, b + f, t - f, d - 0.004, d + 0.004, "glass")
    # Stone sill outside and a painted board inside
    op.box(walls, u0 - 0.06, u1 + 0.06, b - 0.06, b + 0.015, 0.0, EXT / 2 + 0.07, "sill", bevel=0.01)
    op.box(walls, u0 - 0.04, u1 + 0.04, b - 0.03, b + 0.012, -EXT / 2 - 0.05, -0.02, "trim", bevel=0.005)
    # Stone lintel
    op.box(walls, u0 - 0.12, u1 + 0.12, t, t + 0.2, EXT / 2 - 0.02, EXT / 2 + 0.01, "sill")
    # Louvred shutters folded back against the facade
    if t - b > 1.0:
        sw = (u1 - u0) / 2
        for side in (-1, 1):
            s0 = u0 - sw - 0.04 if side < 0 else u1 + 0.04
            op.box(walls, s0, s0 + sw, b + 0.02, t - 0.02, EXT / 2 + 0.01, EXT / 2 + 0.05, "shutter", bevel=0.004)
            hinge_u = u0 - 0.03 if side < 0 else u1 + 0.03
            for z in (b + 0.25, t - 0.25):
                op.box(walls, hinge_u - 0.02, hinge_u + 0.02, z, z + 0.03, EXT / 2, EXT / 2 + 0.06, "black_metal")
    if not dressed:
        return
    # Curtains inside, gathered at both sides of the window
    curtain(rooms_g, op, curtain_mat)
    # Radiator under the window
    if b > 0.6:
        rw = min(u1 - u0 - 0.1, 1.2)
        um = (u0 + u1) / 2
        fins = int(rw / 0.06)
        for i in range(fins):
            u = um - rw / 2 + i * rw / fins
            op.box(rooms_g, u, u + 0.035, 0.15, b - 0.2, -EXT / 2 - 0.12, -EXT / 2 - 0.04, "radiator", bevel=0.008)
        op.box(rooms_g, um - rw / 2, um + rw / 2, 0.17, 0.21, -EXT / 2 - 0.11, -EXT / 2 - 0.05, "radiator")


def curtain(g, op, mat):
    """Two pleated linen panels hanging from a rod, gathered at the window sides."""
    v_wall = -EXT / 2 - 0.08
    top, bottom = op.top + 0.25, 0.04
    op.box(g, op.u0 - 0.4, op.u1 + 0.4, top + 0.02, top + 0.045, v_wall - 0.015, v_wall + 0.015, "black_metal")
    for side in (-1, 1):
        width = 0.42
        u_start = op.u0 - 0.36 if side < 0 else op.u1 + 0.36 - width
        steps = 18
        piece = bmesh.new()
        rows = []
        for z in (bottom, top):
            row = []
            for i in range(steps + 1):
                u = u_start + width * i / steps
                v = v_wall - 0.035 * (1 + math.sin(i / steps * math.pi * 6)) - 0.01
                row.append(piece.verts.new(op.point(u, v, z)))
            rows.append(row)
        for i in range(steps):
            piece.faces.new((rows[0][i], rows[0][i + 1], rows[1][i + 1], rows[1][i]))
        bmesh.ops.solidify(piece, geom=list(piece.faces), thickness=0.008)
        g.merge(piece, mat, absolute=True)


def front_door(walls, op):
    """Panelled front door, open into the lounge, with a frame, step and porch."""
    u0, u1, t = op.u0, op.u1, op.top
    op.box(walls, u0 - 0.08, u0, 0, t + 0.08, -EXT / 2 - 0.02, EXT / 2 + 0.02, "trim")
    op.box(walls, u1, u1 + 0.08, 0, t + 0.08, -EXT / 2 - 0.02, EXT / 2 + 0.02, "trim")
    op.box(walls, u0 - 0.08, u1 + 0.08, t, t + 0.08, -EXT / 2 - 0.02, EXT / 2 + 0.02, "trim")
    width = op.width
    # Door leaf, hinged on the left jamb and opened 75 degrees into the hall
    hinge = Vector(op.point(u0 + 0.02, -EXT / 2, 0))
    with walls.at(hinge.x, hinge.y, 0, rot=75):
        walls.box(0, 0, 0.01, width - 0.04, 0.05, t - 0.02, "door_paint", bevel=0.006)
        for z0, z1 in ((0.15, 0.95), (1.1, t - 0.15)):
            walls.box(0.12, 0.05, z0, width - 0.16, 0.065, z1, "door_paint", bevel=0.01)
            walls.box(0.12, -0.015, z0, width - 0.16, 0.0, z1, "door_paint", bevel=0.01)
        walls.cylinder(width - 0.14, 1.0, -0.06, 0.11, 0.022, mat="brass", axis="Y")
    # Step and threshold
    op.box(walls, u0 - 0.35, u1 + 0.35, -GROUND, -0.02, EXT / 2, EXT / 2 + 0.6, "stone", bevel=0.01)
    op.box(walls, u0, u1, -0.01, 0.015, -EXT / 2, EXT / 2, "oak")


def porch(roof, walls, op):
    """Small gabled canopy over the front door on two brackets, with wall lamps."""
    um = (op.u0 + op.u1) / 2
    z = op.top + 0.35
    depth, half = 1.0, 0.95
    rise = 0.5
    angle = math.atan2(rise, half)
    slab = half / math.cos(angle) + 0.08
    for side in (-1, 1):
        cu = um + side * half / 2
        cz = z + rise / 2 + 0.05
        local = Matrix.Translation(Vector(op.point(cu, EXT / 2 + depth / 2, cz))) @ Matrix.Rotation(side * angle, 4, "Y" if op.horizontal else "X")
        piece = bmesh.new()
        bmesh.ops.create_cube(piece, size=1.0, matrix=local @ Matrix.Diagonal((slab, depth + 0.1, 0.07, 1.0)))
        roof.merge(piece, "roof_tiles", absolute=True)
    # Gable board and brackets
    gable = [op.point(um - half, EXT / 2 + depth, z), op.point(um + half, EXT / 2 + depth, z), op.point(um, EXT / 2 + depth, z + rise)]
    roof.sheet(gable, "trim")
    for side in (-1, 1):
        u = um + side * (half - 0.08)
        op.box(roof, u - 0.04, u + 0.04, z - 0.05, z + 0.02, EXT / 2, EXT / 2 + depth, "trim")
        op.box(roof, u - 0.03, u + 0.03, z - 0.55, z - 0.05, EXT / 2, EXT / 2 + 0.08, "trim")
    # Wall lamps either side of the door
    for side in (-1, 1):
        u = (op.u0 - 0.35) if side < 0 else (op.u1 + 0.35)
        lib_lamps.append(op.point(u, EXT / 2 + 0.02, 1.85))


lib_lamps = []  # filled by porch(), placed once the library is ready


def build_shell(materials):
    walls = Group("walls", materials)
    glass = Group("glass", materials)
    roof = Group("roof", materials)
    room_bits = {slug: Group(f"room_{slug}", materials) for slug in ROOMS}
    win = (0.9, 2.25)
    door = (0.0, 2.2)
    e = EXT / 2

    def room_at(point):
        x, y = point[0], point[1]
        for slug, room in ROOMS.items():
            if room["x"] - 0.3 <= x <= room["x"] + room["w"] + 0.3 and room["y"] - 0.3 <= y <= room["y"] + room["d"] + 0.3:
                return slug
        return "lounge"

    curtains = {"lounge": "curtain", "breakfast-room": "curtain", "garden-room": "curtain_sage",
                "linen-room": "curtain_sage", "courtyard-suite": "curtain"}
    exterior = {
        "front": ((-e, 0), (FW + e, 0), [(1.6 + e, 1.2, *win), (3.9 + e, 1.1, *door),
                                         (7.6 + e, 1.3, *win), (9.6 + e, 1.0, *win), (13.5 + e, 2.0, 0.55, 2.25)]),
        "back": ((-e, FD), (FW + e, FD), [(2.2 + e, 1.3, *win), (6.6 + e, 1.3, *win),
                                          (11.2 + e, 1.2, *win), (14.2 + e, 1.2, *win)]),
        "west": ((0, -e), (0, FD + e), [(7.5 + e, 1.3, *win)]),
        "east": ((FW, -e), (FW, FD + e), [(2.5 + e, 1.3, *win), (7.5 + e, 1.2, *win)]),
    }
    # Windows with furniture right under them get no curtains or radiator
    crowded = {("front", 7.6), ("front", 9.6), ("back", 11.2), ("back", 14.2), ("east", 7.5)}
    door_op = None
    for side_name, (a, b, openings) in exterior.items():
        wall(walls, a, b, EXT, openings)
        for center, width, bottom, top in openings:
            op = Opening(a, b, center, width, bottom, top)
            if bottom > 0:
                slug = room_at(op.point((op.u0 + op.u1) / 2, -0.5, 1))
                dressed = (side_name, round(center - e, 1)) not in crowded
                window(walls, glass, room_bits[slug], op, curtains[slug], dressed)
            else:
                door_op = op
                front_door(walls, op)
    porch(roof, walls, door_op)

    # Interior walls stop at the inner face of the exterior walls
    wall(walls, (e, 5), (FW - e, 5), INT, [(3.0 - e, 0.95, *door), (10.0 - e, 0.95, *door)])
    wall(walls, (6, e), (6, 5 - INT / 2), INT, [(2.6 - e, 0.95, *door)])
    wall(walls, (11, e), (11, 5 - INT / 2), INT, [(2.6 - e, 0.95, *door)])
    wall(walls, (9, 5 + INT / 2), (9, FD - e), INT)
    # En-suite bathrooms: partitions on the sides that are not existing walls
    trims = [(3.0, 5, True, INT, 0.475), (10.0, 5, True, INT, 0.475), (6, 2.6, False, INT, 0.475), (11, 2.6, False, INT, 0.475)]
    for bath in BATHS:
        x0, y0 = bath["x"], bath["y"]
        x1, y1 = x0 + bath["w"], y0 + bath["d"]
        sides = {"south": ((x0, y0), (x1, y0)), "north": ((x0, y1), (x1, y1)),
                 "west": ((x0, y0), (x0, y1)), "east": ((x1, y0), (x1, y1))}
        for name, (a, b) in sides.items():
            line = a[1] if name in ("south", "north") else a[0]
            existing = (0, 5, FD) if name in ("south", "north") else (0, 6, 9, 11, FW)
            if any(abs(line - v) < 1e-6 for v in existing):
                continue
            openings = []
            if bath["door"]["side"] == name:
                start = a[0] if name in ("south", "north") else a[1]
                openings = [(bath["door"]["at"] - start, 0.75, 0.0, 2.05)]
                if name in ("south", "north"):
                    trims.append((bath["door"]["at"], line, True, BATH_T, 0.375))
                else:
                    trims.append((line, bath["door"]["at"], False, BATH_T, 0.375))
            wall(walls, a, b, BATH_T, openings)

    # Architraves around the interior doorways, on both faces of the wall
    top, t = door[1], 0.07
    for x, y, along_x, thick, half_w in trims:
        top = 2.05 if thick == BATH_T else door[1]
        for side in (-1, 1):
            o = y + side * (thick / 2 + 0.01) if along_x else x + side * (thick / 2 + 0.01)
            c = x if along_x else y
            for s0, s1, z0, z1 in ((c - half_w - t, c - half_w, 0, top + t), (c + half_w, c + half_w + t, 0, top + t),
                                   (c - half_w - t, c + half_w + t, top, top + t)):
                if along_x:
                    walls.box(s0, o - 0.01, z0, s1, o + 0.01, z1, "trim")
                else:
                    walls.box(o - 0.01, s0, z0, o + 0.01, s1, z1, "trim")

    # Skirting along the base of every room, and floors
    floors = {"lounge": "floor_herringbone", "breakfast-room": "floor_tiles"}
    for slug, room in ROOMS.items():
        x0, y0, x1, y1 = inner_rect(room)
        g = room_bits[slug]
        g.box(x0, y0, -0.05, x1, y1, 0.0, floors.get(slug, "floor_oak"))
        for (ax, ay, bx, by) in ((x0, y0, x1, y0 + 0.018), (x0, y1 - 0.018, x1, y1), (x0, y0, x0 + 0.018, y1), (x1 - 0.018, y0, x1, y1)):
            walls.box(ax, ay, 0, bx, by, 0.12, "trim")
    return walls, glass, roof, room_bits


def inner_rect(room):
    """Inner floor rectangle of a room, between the faces of its walls."""
    x0, y0 = room["x"], room["y"]
    x1, y1 = x0 + room["w"], y0 + room["d"]

    def inset(v, edge):
        return EXT / 2 if v in edge else INT / 2

    return (x0 + inset(x0, (0, FW)), y0 + inset(y0, (0, FD)), x1 - inset(x1, (0, FW)), y1 - inset(y1, (0, FD)))


# ---------------------------------------------------------------------- roof


def build_roof(roof, materials):
    eave, rise, over, thick = 0.5, 2.1, 0.45, 0.16
    angle = math.atan2(rise, FD / 2)
    run = FD / 2 + eave
    slab = run / math.cos(angle)
    length = FW + 2 * over + EXT
    for side in (1, -1):
        y_mid = FD / 2 - side * run / 2
        z_mid = H + rise - (run / 2) * math.tan(angle) + thick / 2 + 0.05
        local = Matrix.Translation((FW / 2, y_mid, z_mid)) @ Matrix.Rotation(side * angle, 4, "X")
        piece = bmesh.new()
        bmesh.ops.create_cube(piece, size=1.0, matrix=local @ Matrix.Diagonal((length, slab, thick, 1.0)))
        roof.merge(piece, "roof_tiles", absolute=True)
        # Fascia board, gutter and the soffit under the overhang
        y_edge = FD / 2 - side * run
        z_edge = H + rise - run * math.tan(angle) + 0.05
        y_out = y_edge - side * 0.02
        roof.box(-over - EXT / 2, min(y_out, y_out + side * 0.04), z_edge - 0.2, FW + over + EXT / 2,
                 max(y_out, y_out + side * 0.04), z_edge + 0.02, "trim")
        gy = y_edge - side * 0.1
        roof.cylinder(gy, z_edge - 0.12, -over - EXT / 2, FW + over + EXT / 2, 0.075, mat="zinc", segments=12, axis="X")
        wall_face = 0 - EXT / 2 if side > 0 else FD + EXT / 2
        roof.box(-over - EXT / 2, min(wall_face, y_edge), z_edge - 0.2, FW + over + EXT / 2, max(wall_face, y_edge), z_edge - 0.17, "trim")
        # Downpipes at both ends, against the wall, joined to the gutter
        y_pipe = wall_face - side * 0.08
        for x in (-0.05, FW + 0.05):
            roof.cylinder(x, y_pipe, -GROUND, z_edge - 0.2, 0.045, mat="zinc", segments=10)
            roof.cylinder(x, z_edge - 0.16, min(gy, y_pipe), max(gy, y_pipe), 0.04, mat="zinc", segments=10, axis="Y")
        # Barge boards along both verges of this slope
        for xb in (-over - EXT / 2 - 0.02, FW + over + EXT / 2 + 0.02):
            local = Matrix.Translation((xb, y_mid, z_mid + 0.02)) @ Matrix.Rotation(side * angle, 4, "X")
            piece = bmesh.new()
            bmesh.ops.create_cube(piece, size=1.0, matrix=local @ Matrix.Diagonal((0.04, slab, thick + 0.12, 1.0)))
            roof.merge(piece, "trim", absolute=True)
    # Gable ends above the side walls
    for x in (0, FW):
        piece = bmesh.new()
        t = EXT / 2
        tri = [(-EXT / 2, H), (FD + EXT / 2, H), (FD / 2, H + rise + 0.05)]
        a = [piece.verts.new((x - t, y, z)) for y, z in tri]
        b = [piece.verts.new((x + t, y, z)) for y, z in tri]
        piece.faces.new(a)
        piece.faces.new(list(reversed(b)))
        for i in range(3):
            j = (i + 1) % 3
            piece.faces.new((a[i], b[i], b[j], a[j]))
        bmesh.ops.recalc_face_normals(piece, faces=list(piece.faces))
        roof.merge(piece, "wall", absolute=True)
    # Ridge tiles and the chimney above the fireplace
    roof.cylinder(FD / 2, H + rise + 0.23, -over - EXT / 2, FW + over + EXT / 2, 0.13, mat="roof_tiles", segments=12, axis="X")
    roof.box(-0.25, 2.05, H, 0.8, 2.95, H + rise + 0.75, "stone", bevel=0.02)
    roof.box(-0.33, 1.97, H + rise + 0.75, 0.88, 3.03, H + rise + 0.85, "sill", bevel=0.01)
    for y in (2.3, 2.7):
        roof.cylinder(0.27, y, H + rise + 0.85, H + rise + 1.15, 0.09, 0.075, mat="roof_tiles", segments=14)


# ------------------------------------------------------------------ furniture


def bed(g, width, length, duvet, headboard="walnut", head_h=1.15):
    """Bed with its head against y=0, centred on x=0, made of soft shapes."""
    w, l = width / 2, length
    g.box(-w - 0.03, 0.04, 0.12, w + 0.03, l + 0.03, 0.34, "oak", bevel=0.015)
    for x in (-w + 0.04, w - 0.04):
        g.box(x - 0.035, l - 0.04, 0, x + 0.035, l + 0.03, 0.14, "oak")
    g.soft(-w - 0.06, 0, 0, w + 0.06, 0.09, head_h, headboard, radius=0.03, segments=2)
    g.soft(-w + 0.01, 0.1, 0.32, w - 0.01, l - 0.01, 0.56, "cotton", radius=0.06)
    g.soft(-w - 0.07, 0.78, 0.36, w + 0.07, l + 0.08, 0.63, duvet, radius=0.09, segments=5)
    g.soft(-w - 0.05, l - 0.6, 0.56, w + 0.05, l - 0.12, 0.645, "throw", radius=0.03)
    pillows = 2 if width > 1.2 else 1
    pw = (width - 0.16) / pillows
    for i in range(pillows):
        px = -w + 0.08 + pw * i
        with g.at(px + pw / 2, 0.3, 0.68):
            g.soft(-pw / 2 + 0.03, -0.18, -0.12, pw / 2 - 0.03, 0.18, 0.06, "cotton", radius=0.09, segments=5)


def lamp(g, x, y, z):
    g.cylinder(x, y, z, z + 0.05, 0.085, mat="ceramic", segments=24)
    g.cylinder(x, y, z + 0.05, z + 0.3, 0.07, 0.04, mat="ceramic", segments=24)
    g.cylinder(x, y, z + 0.3, z + 0.37, 0.012, mat="brass", segments=8)
    g.sphere(x, y, z + 0.38, 0.03, "bulb")
    g.cylinder(x, y, z + 0.3, z + 0.52, 0.17, 0.12, mat="lampshade", segments=28)


def floor_lamp(g, x, y):
    g.cylinder(x, y, 0, 0.03, 0.15, mat="black_metal")
    g.cylinder(x, y, 0.03, 1.45, 0.012, mat="brass", segments=8)
    g.sphere(x, y, 1.42, 0.03, "bulb")
    g.cylinder(x, y, 1.3, 1.62, 0.22, 0.16, mat="lampshade", segments=28)


def table(g, w, d, h=0.75, mat="oak", top=None):
    g.box(-w / 2, -d / 2, h - 0.04, w / 2, d / 2, h, top or mat, bevel=0.008)
    for sx in (-1, 1):
        for sy in (-1, 1):
            x, y = sx * (w / 2 - 0.06), sy * (d / 2 - 0.06)
            g.box(x - 0.025, y - 0.025, 0, x + 0.025, y + 0.025, h - 0.04, mat)


def painting(g, x, y, z, w, h, face, art):
    """A framed canvas hanging on a wall, its front facing `face` degrees."""
    with g.at(x, y, 0, rot=face - 90):
        g.box(-w / 2, 0, z - h / 2, w / 2, 0.035, z + h / 2, "walnut", bevel=0.006)
        g.box(-w / 2 + 0.05, 0.035, z - h / 2 + 0.05, w / 2 - 0.05, 0.04, z + h / 2 - 0.05, art)
        g.box(-w / 2 + 0.045, 0.034, z - h / 2 + 0.045, w / 2 - 0.045, 0.038, z + h / 2 - 0.045, "brass")


def rug(g, x0, y0, x1, y1, mat="rug"):
    g.box(x0, y0, 0, x1, y1, 0.012, mat, bevel=0.004)


def books(g, x0, x1, y0, y1, z):
    """A row of books standing between x0 and x1, spines facing -y."""
    x = x0
    while x < x1 - 0.06:
        bw = random.uniform(0.025, 0.055)
        bh = random.uniform(0.2, 0.3)
        if random.random() < 0.1:
            x += 0.07
            continue
        lean = random.random() < 0.06
        book = random.choice(("book_a", "book_b", "book_c", "book_d", "book_e", "book_f"))
        if lean:
            g.box(x, y0, z, x + bh, y1, z + bw, book)
            x += bh + 0.01
        else:
            g.box(x, y0, z, x + bw, y1 - random.uniform(0, 0.03), z + bh, book)
            x += bw + 0.002


def wc(g):
    """Close-coupled toilet with its back against y=0, facing +y."""
    g.soft(-0.19, 0.01, 0.42, 0.19, 0.18, 0.8, "ceramic", radius=0.03, segments=3)
    g.cylinder(0.07, 0.1, 0.8, 0.81, 0.025, mat="brass", segments=16)
    g.soft(-0.13, 0.12, 0.0, 0.13, 0.5, 0.36, "ceramic", radius=0.06, segments=3)
    g.soft(-0.19, 0.14, 0.28, 0.19, 0.66, 0.42, "ceramic", radius=0.12, segments=4)
    g.soft(-0.2, 0.15, 0.42, 0.2, 0.67, 0.45, "oak", radius=0.1, segments=4)


def bidet(g):
    """Floor-standing bidet with its back against y=0, facing +y."""
    g.soft(-0.13, 0.06, 0.0, 0.13, 0.46, 0.3, "ceramic", radius=0.06, segments=3)
    g.soft(-0.18, 0.03, 0.24, 0.18, 0.56, 0.4, "ceramic", radius=0.12, segments=4)
    g.soft(-0.12, 0.12, 0.36, 0.12, 0.48, 0.405, "radiator", radius=0.08, segments=3)
    g.cylinder(0, 0.08, 0.4, 0.5, 0.012, mat="brass", segments=10)
    g.cylinder(0, 0.49, 0.04, 0.13, 0.01, mat="brass", segments=10, axis="Y")


def basin(g, width=0.7):
    """Washstand with its back against y=0, facing +y: oak cabinet, marble top, basin, tap, mirror."""
    w = width / 2
    g.box(-w, 0, 0.12, w, 0.46, 0.8, "oak", bevel=0.008)
    for x in (-w + 0.06, w - 0.06):
        g.box(x - 0.02, 0.02, 0, x + 0.02, 0.44, 0.12, "oak")
    g.box(-w - 0.01, 0, 0.8, w + 0.01, 0.48, 0.83, "marble", bevel=0.004)
    g.soft(-0.21, 0.1, 0.83, 0.21, 0.42, 0.95, "ceramic", radius=0.06, segments=4)
    g.cylinder(0, 0.06, 0.83, 1.05, 0.014, mat="brass", segments=10)
    g.cylinder(0, 1.04, 0.05, 0.17, 0.012, mat="brass", segments=10, axis="Y")
    g.box(-w - 0.04, 0.002, 0.83, w + 0.04, 0.012, 1.08, "splash_tiles")
    g.box(-w + 0.04, 0, 1.18, w - 0.04, 0.03, 1.85, "walnut", bevel=0.006)
    g.box(-w + 0.07, 0.03, 1.21, w - 0.07, 0.034, 1.82, "mirror")


def towel_rail(g, length=0.6):
    """Brass rail with a folded towel, on a wall at y=0."""
    g.box(-length / 2, 0.05, 1.18, length / 2, 0.075, 1.2, "brass")
    for x in (-length / 2, length / 2):
        g.box(x - 0.012, 0, 1.17, x + 0.012, 0.075, 1.21, "brass")
    g.soft(-length / 2 + 0.06, 0.035, 0.75, length / 2 - 0.06, 0.09, 1.215, "cotton", radius=0.02, segments=2)


def shower(g, x0, y0, x1, y1, walls_on, glass_at):
    """Shower in the box x0..x1, y0..y1: tiled walls on the sides in walls_on, a glass screen on glass_at."""
    g.box(x0, y0, 0.0, x1, y1, 0.05, "ceramic", bevel=0.01)
    tile = 0.012
    for side in walls_on:
        if side == "north":
            g.box(x0, y1 - tile, 0, x1, y1, 2.1, "splash_tiles")
        elif side == "south":
            g.box(x0, y0, 0, x1, y0 + tile, 2.1, "splash_tiles")
        elif side == "west":
            g.box(x0, y0, 0, x0 + tile, y1, 2.1, "splash_tiles")
        else:
            g.box(x1 - tile, y0, 0, x1, y1, 2.1, "splash_tiles")
    if glass_at == "east":
        g.box(x1 - 0.008, y0, 0.05, x1, y1, 2.0, "glass")
        g.box(x1 - 0.012, y0, 1.98, x1 + 0.004, y1, 2.0, "brass")
    elif glass_at == "west":
        g.box(x0, y0, 0.05, x0 + 0.008, y1, 2.0, "glass")
        g.box(x0 - 0.004, y0, 1.98, x0 + 0.012, y1, 2.0, "brass")
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    g.cylinder(cx, cy, 2.02, 2.04, 0.11, mat="brass", segments=24)
    g.cylinder(cx, cy, 0.05, 0.056, 0.04, mat="black_metal", segments=12)


def bath_floor(g, bath):
    """Tiled floor inside a bathroom, between the faces of its walls."""
    x0, y0 = bath["x"], bath["y"]
    x1, y1 = x0 + bath["w"], y0 + bath["d"]

    def inset(v, edge):
        return EXT / 2 if any(abs(v - e) < 1e-6 for e in edge) else 0.06

    g.box(x0 + inset(x0, (0, FW)), y0 + inset(y0, (0, FD)), 0.0, x1 - inset(x1, (0, FW)), y1 - inset(y1, (0, FD)), 0.008, "bath_floor")


def kitchen(g):
    """Counter along a wall at y=0, facing +y: cabinets, marble top, sink, splashback."""
    w = 4.2
    g.box(-w / 2, 0, 0.1, w / 2, 0.6, 0.88, "painted_wood", bevel=0.006)
    g.box(-w / 2 + 0.02, 0.02, 0, w / 2 - 0.02, 0.55, 0.1, "black_metal")
    for i in range(7):
        x = -w / 2 + 0.03 + i * (w - 0.06) / 7
        g.box(x + 0.01, 0.6, 0.14, x + (w - 0.06) / 7 - 0.01, 0.615, 0.84, "painted_wood", bevel=0.004)
        g.box(x + (w - 0.06) / 14 - 0.05, 0.615, 0.74, x + (w - 0.06) / 14 + 0.05, 0.635, 0.755, "brass")
    g.box(-w / 2 - 0.02, 0, 0.88, w / 2 + 0.02, 0.64, 0.92, "marble", bevel=0.004)
    g.box(-1.25, 0.12, 0.7, -0.55, 0.52, 0.921, "ceramic", bevel=0.03, segments=2)
    g.cylinder(-0.9, 0.06, 0.92, 1.22, 0.015, mat="brass", segments=10)
    g.cylinder(-0.9, 1.21, 0.06, 0.24, 0.013, mat="brass", segments=10, axis="Y")


# ----------------------------------------------------------------------- rooms


def furnish(lib, room_bits):
    """Furniture: procedural pieces go into each room's Group, assets are placed copies."""
    g = room_bits["lounge"]
    # Fireplace on the west wall, facing east
    with g.at(EXT / 2, 2.5, 0, rot=-90):
        g.box(-0.95, 0, 0, 0.95, 0.42, 1.25, "stone", bevel=0.02)
        g.box(-0.55, 0.28, 0.08, 0.55, 0.421, 0.85, "soot")
        g.box(-1.05, -0.0, 1.25, 1.05, 0.5, 1.32, "walnut", bevel=0.01)
        g.box(-0.9, 0, 1.32, 0.9, 0.3, H, "wall")
        for y, z, r in ((0.3, 0.15, 0.06), (0.37, 0.15, 0.055), (0.33, 0.25, 0.05)):
            g.cylinder(y, z, -0.32, 0.32, r, mat="walnut", segments=10, axis="X")
        g.box(-0.35, 0.26, 0.09, 0.35, 0.36, 0.14, "embers")
        g.box(-1.0, 0.42, -0.0, 1.0, 0.9, 0.04, "sill", bevel=0.01)
    lib.place("lounge", "mantel_clock_01", EXT / 2 + 0.25, 2.5, 1.32, face=0)
    painting(g, EXT / 2 + 0.3, 2.5, 1.95, 0.9, 0.62, 0, "art_a")
    lib.place("lounge", "wicker_basket_01", EXT / 2 + 0.35, 1.25, 0, face=0)
    rug(g, 1.0, 1.1, 3.6, 3.9, "rug")
    lib.place("lounge", "sofa_03", 3.75, 2.5, face=180, scale=0.9)
    lib.place("lounge", "throw_pillows_01", 3.9, 2.5, 0.42, face=180, scale=0.8)
    lib.place("lounge", "ArmChair_01", 1.75, 0.95, face=60)
    lib.place("lounge", "ArmChair_01", 1.9, 3.95, face=-60)
    lib.place("lounge", "modern_coffee_table_01", 2.45, 2.5, face=90)
    lib.place("lounge", "side_table_01", 3.75, 3.95, face=180)
    lamp(g, 3.75, 3.95, 0.55)
    lib.place("lounge", "wooden_bookshelf_worn", 5.0, 5 - INT / 2 - 0.3, face=-90)
    for z in shelf_levels(lib.load("wooden_bookshelf_worn")):
        books(g, 4.42, 5.58, 4.42, 4.82, z + 0.004)
    lib.place("lounge", "book_encyclopedia_set_01", 2.45, 2.7, 0.39, face=0, scale=0.6)
    lib.place("lounge", "potted_plant_02", 5.45, 0.55, face=90)
    lib.place("lounge", "potted_plant_01", 0.55, 0.55, face=45)
    floor_lamp(g, 5.6, 3.85)

    g = room_bits["breakfast-room"]
    with g.at(8.5, EXT / 2):
        kitchen(g)
    lib.place("breakfast-room", "potted_plant_04", 9.9, 0.45, 0.92)
    lib.place("breakfast-room", "wooden_cutting_board", 7.2, 0.45, 0.92, face=80)
    lib.place("breakfast-room", "jug_01", 6.75, 0.45, 0.92)
    lib.place("breakfast-room", "dining_table", 8.5, 2.75, face=90, scale=0.95)
    for x in (-0.7, 0.0, 0.7):
        lib.place("breakfast-room", "painted_wooden_chair_02", 8.5 + x, 2.0, face=90)
        lib.place("breakfast-room", "painted_wooden_chair_02", 8.5 + x, 3.5, face=-90)
    lib.place("breakfast-room", "tea_set_01", 8.5, 2.75, 0.84, face=90, scale=0.9)
    lib.place("breakfast-room", "wooden_bowl_01", 9.3, 2.85, 0.84)
    lib.place("breakfast-room", "croissant", 9.25, 2.82, 0.88)
    lib.place("breakfast-room", "croissant", 9.35, 2.9, 0.88, face=30)
    lib.place("breakfast-room", "painted_wooden_cabinet", 7.4, 5 - INT / 2 - 0.33, face=-90)
    lib.place("breakfast-room", "ceramic_vase_02", 7.0, 4.62, 1.18)
    lib.place("breakfast-room", "jug_01", 7.8, 4.62, 1.18, face=-70)
    painting(g, 7.4, 5 - INT / 2, 1.85, 0.8, 0.55, -90, "art_b")
    for x in (7.9, 9.1):
        lib.place("breakfast-room", "modern_ceiling_lamp_01", x, 2.75, H - 0.95 - 0.22)
        g.cylinder(x, 2.75, H - 0.5, H, 0.005, mat="black_metal", segments=6)

    g = room_bits["garden-room"]
    rug(g, 12.9, 1.2, 15.3, 3.9, "rug_blue")
    with g.at(14.1, 5 - INT / 2, 0, rot=180):
        bed(g, 1.6, 2.05, "duvet_sage")
    for x in (12.9, 15.3):
        lib.place("garden-room", "ClassicNightstand_01", x, 4.7, face=-90, scale=0.85)
        lamp(g, x, 4.68, 0.6)
    painting(g, 14.1, 5 - INT / 2, 1.75, 1.0, 0.7, -90, "art_b")
    lib.place("garden-room", "painted_wooden_cabinet", FW - EXT / 2 - 0.32, 3.7, face=180, scale=0.9)
    lib.place("garden-room", "ornate_mirror_01", FW - EXT / 2 - 0.02, 3.7, 1.55, face=180)
    # En-suite: shower in the far corner, toilet on the back wall, basin by the door
    bath_floor(g, BATHS[0])
    shower(g, 11.06, 4.14, 11.86, 4.94, ("west", "north"), "east")
    with g.at(12.55, 4.55, 0, rot=90):
        wc(g)
    with g.at(12.55, 3.95, 0, rot=90):
        bidet(g)
    with g.at(11.06, 3.72, 0, rot=-90):
        basin(g, 0.6)
    with g.at(12.35, 3.3, 0):
        towel_rail(g, 0.3)
    lib.place("garden-room", "modern_arm_chair_01", 11.75, 1.1, face=45)
    lib.place("garden-room", "potted_plant_02", 15.45, 0.6, face=135)

    g = room_bits["linen-room"]
    rug(g, 10.0, 5.6, 13.8, 7.4, "rug")
    for x in (10.75, 13.35):
        with g.at(x, FD - EXT / 2, 0, rot=180):
            bed(g, 0.95, 2.0, "duvet_blue", headboard="painted_wood", head_h=0.85)
    lib.place("linen-room", "painted_wooden_nightstand", 12.05, FD - EXT / 2 - 0.28, face=-90)
    lamp(g, 12.05, FD - EXT / 2 - 0.28, 0.62)
    with g.at(FW - EXT / 2 - 0.3, 8.2, 0, rot=90):
        table(g, 1.1, 0.55, h=0.76, mat="walnut", top="oak")
    lib.place("linen-room", "painted_wooden_chair_01", FW - EXT / 2 - 0.8, 8.2, face=0)
    lib.place("linen-room", "standing_picture_frame_01", FW - EXT / 2 - 0.25, 8.6, 0.76, face=180)
    # En-suite in the corner by the door
    bath_floor(g, BATHS[1])
    shower(g, 15.05, 5.06, 15.85, 5.86, ("east", "south"), "west")
    with g.at(14.1, 6.25, 0, rot=-90):
        wc(g)
    with g.at(15.85, 6.3, 0, rot=90):
        bidet(g)
    with g.at(14.5, 5.06, 0):
        basin(g, 0.55)
    lib.place("linen-room", "vintage_wooden_drawer_01", 9 + INT / 2 + 0.25, 6.3, face=0)
    lib.place("linen-room", "wicker_basket_02", 9 + INT / 2 + 0.25, 6.2, 0.55)
    painting(g, 9 + INT / 2, 8.3, 1.55, 0.6, 0.8, 0, "art_a")
    lib.place("linen-room", "potted_plant_01", 13.65, 5.45, face=135)

    g = room_bits["courtyard-suite"]
    rug(g, 2.4, 6.0, 6.0, 8.8, "rug")
    with g.at(4.2, FD - EXT / 2, 0, rot=180):
        bed(g, 1.9, 2.1, "duvet_clay")
    for x in (2.85, 5.55):
        lib.place("courtyard-suite", "side_table_tall_01", x, FD - EXT / 2 - 0.25, scale=0.8)
        lamp(g, x, FD - EXT / 2 - 0.25, 0.61)
    painting(g, 4.2, FD - EXT / 2, 1.75, 1.3, 0.75, -90, "art_b")
    # En-suite with a freestanding bath under the back wall
    bath_floor(g, BATHS[2])
    with g.at(8.5, 8.75, 0, rot=90):
        g.soft(-0.85, -0.4, 0.08, 0.85, 0.4, 0.62, "ceramic", radius=0.2, segments=4)
        g.soft(-0.74, -0.29, 0.4, 0.74, 0.29, 0.63, "radiator", radius=0.14, segments=3)
        for sx in (-0.62, 0.62):
            for sy in (-0.27, 0.27):
                g.sphere(sx, sy, 0.05, 0.05, "brass")
    g.cylinder(8.5, 7.75, 0, 1.0, 0.022, mat="brass", segments=10)
    g.box(8.928, 7.8, 0, 8.94, FD - EXT / 2, 1.2, "splash_tiles")
    g.box(8.0, FD - EXT / 2 - 0.012, 0, 8.94, FD - EXT / 2, 1.2, "splash_tiles")
    with g.at(8.5, 6.35, 0):
        wc(g)
    with g.at(7.9, 6.35, 0):
        bidet(g)
    with g.at(7.4, 8.75, 0, rot=-90):
        basin(g, 0.9)
    with g.at(7.75, FD - EXT / 2, 0, rot=180):
        towel_rail(g, 0.5)
    lib.place("courtyard-suite", "sofa_02", 7.6, 5 + INT / 2 + 0.38, face=90, scale=0.95)
    lib.place("courtyard-suite", "vintage_wooden_drawer_01", 1.4, 5 + INT / 2 + 0.25, face=90)
    lamp(g, 1.4, 5 + INT / 2 + 0.25, 0.55)
    with g.at(EXT / 2 + 0.35, 8.8, 0, rot=-90):
        table(g, 1.0, 0.55, h=0.76, mat="walnut", top="oak")
    lib.place("courtyard-suite", "painted_wooden_chair_01", EXT / 2 + 0.85, 8.8, face=180)
    lib.place("courtyard-suite", "potted_plant_02", 6.2, 5.6, face=45)
    painting(g, 7.6, 5 + INT / 2, 1.65, 0.7, 0.9, 90, "art_a")


# ------------------------------------------------------------- garden & trees


def hedge_box(g, x0, y0, x1, y1, h, depth, mat="hedge"):
    """A clipped hedge: a subdivided block with its surface pushed in and out by noise."""
    from mathutils import noise
    length = math.hypot(x1 - x0, y1 - y0)
    along = Vector((x1 - x0, y1 - y0, 0)).normalized()
    pieces = max(1, int(length / 1.6))
    for i in range(pieces):
        a = Vector((x0, y0, 0)) + along * (length * i / pieces)
        b = Vector((x0, y0, 0)) + along * (length * (i + 1) / pieces + 0.02)
        mid = (a + b) / 2
        piece = bmesh.new()
        angle = math.atan2(along.y, along.x)
        local = Matrix.Translation((mid.x, mid.y, h / 2)) @ Matrix.Rotation(angle, 4, "Z") @ Matrix.Diagonal(((b - a).length, depth, h, 1))
        bmesh.ops.create_cube(piece, size=1.0, matrix=local)
        bmesh.ops.subdivide_edges(piece, edges=list(piece.edges), cuts=7, use_grid_fill=True)
        piece.normal_update()
        for v in piece.verts:
            if v.co.z < 0.02:
                continue
            offset = noise.noise(v.co * 2.2) * 0.07 + noise.noise(v.co * 7.0) * 0.025
            v.co += v.normal * offset
        g.merge(piece, mat, segments=2, absolute=True)


def boxwood_row(g, x0, x1, y, r, mat="hedge"):
    """Clipped boxwood balls along a bed."""
    n = max(1, int((x1 - x0) / (r * 2.6)))
    for i in range(n + 1):
        x = x0 + (x1 - x0) * i / max(n, 1)
        rr = r * random.uniform(0.85, 1.1)
        g.sphere(x, y, rr * 0.85, rr, mat, squash=0.9, subdivisions=3, jitter=0.06)


def picket_fence(g, x0, x1, y, skip=()):
    """White picket fence with posts every two metres; `skip` ranges leave gaps."""
    def open_at(x):
        return any(a <= x <= b for a, b in skip)
    x = x0
    while x <= x1:
        if not open_at(x):
            g.box(x - 0.04, y - 0.015, 0, x + 0.04, y + 0.015, 0.95, "picket", bevel=0.006)
            g.box(x - 0.028, y - 0.0151, 0.95, x + 0.028, y + 0.0151, 1.0, "picket", bevel=0.01)
        x += 0.13
    x = x0
    while x <= x1:
        if not open_at(x):
            g.box(x - 0.05, y - 0.07, 0, x + 0.05, y + 0.03, 1.05, "picket", bevel=0.008)
        x += 2.0
    edges = sorted([x0] + [v for r in skip for v in r] + [x1])
    for a, b in zip(edges[0::2], edges[1::2]):
        for z in (0.28, 0.72):
            g.box(a, y + 0.015, z, b, y + 0.045, z + 0.07, "picket")


def hedge(g, x0, y0, x1, y1, h, mat="hedge", detail=3):
    """A clipped hedge made of overlapping soft lumps."""
    length = math.hypot(x1 - x0, y1 - y0)
    n = max(2, int(length / 0.45))
    for i in range(n + 1):
        t = i / n
        x, y = x0 + (x1 - x0) * t, y0 + (y1 - y0) * t
        g.sphere(x, y, h * 0.55, h * 0.55, mat, squash=1.0, subdivisions=detail, jitter=0.12)


def build_grounds(materials, lib):
    g = Group("grounds", materials)
    # Stone plinth around the base of the walls
    o, z0, z1 = EXT / 2 + 0.05, -GROUND - 0.01, 0.12
    for box in ((-o, -o, FW + o, -EXT / 2 + 0.01), (-o, FD + EXT / 2 - 0.01, FW + o, FD + o),
                (-o, -o, -EXT / 2 + 0.01, FD + o), (FW + EXT / 2 - 0.01, -o, FW + o, FD + o)):
        g.box(box[0], box[1], z0, box[2], box[3], z1, "stone")
    with g.at(z=-GROUND):
        g.box(-12, -14, -0.3, 28, 20, 0.0, "grass")
        door_x = 3.9
        g.box(door_x - 0.75, -9.5, 0.0, door_x + 0.75, -EXT / 2 - 0.6, 0.03, "path", bevel=0.01)
        # Gravel drip strip along the front, with flower beds
        g.box(-0.6, -1.0, 0.0, FW + 0.6, -EXT / 2 - 0.05, 0.02, "gravel")
        g.box(-0.6, FD + EXT / 2, 0.0, 1.0, FD + 3.4, 0.02, "gravel")
        boxwood_row(g, -0.2, 3.0, -0.6, 0.32)
        boxwood_row(g, 4.9, 6.6, -0.6, 0.3)
        boxwood_row(g, 11.6, 12.4, -0.6, 0.28, "hedge_light")
        boxwood_row(g, 14.8, 16.2, -0.6, 0.3, "hedge_light")
        # Front boundary: picket fence with the gate left open
        picket_fence(g, -8, 24, -8.6, skip=((door_x - 0.95, door_x + 0.95),))
        with g.at(door_x - 0.9, -8.6, 0, rot=-65):
            for i in range(7):
                g.box(0.05 + i * 0.12, -0.015, 0.05, 0.13 + i * 0.12, 0.015, 0.9, "picket", bevel=0.006)
            for z in (0.25, 0.65):
                g.box(0.03, 0.015, z, 0.88, 0.045, z + 0.07, "picket")
        # Clipped hedges along the sides and the back of the garden
        hedge_box(g, -8, -8.3, -8, 19.5, 1.5, 0.9)
        hedge_box(g, 24, -8.3, 24, 19.5, 1.5, 0.9)
        hedge_box(g, -8, 19.5, 24, 19.5, 1.6, 0.9)
        # Round shrubs by the corners
        for x, y, r in ((-1.0, -0.7, 0.55), (17.0, -0.7, 0.6), (-1.0, FD + 0.8, 0.5), (17.0, FD + 0.9, 0.55)):
            g.sphere(x, y, r * 0.8, r, "hedge", squash=0.85, subdivisions=3, jitter=0.06)
    # Stone terrace at the back, level with the floors
    g.box(1.2, FD + EXT / 2, -GROUND, 8.5, FD + 3.6, 0.0, "patio", bevel=0.01)
    lib.place("grounds", "outdoor_table_chair_set_01", 3.5, FD + 1.9, face=0)
    lib.place("grounds", "outdoor_table_chair_set_01", 6.2, FD + 1.9, face=0)
    lib.place("grounds", "planter_box_01", 1.7, FD + 3.2, face=-90)
    boxwood_row(g, 1.4, 2.0, FD + 3.2, 0.2, "hedge_light")
    lib.place("grounds", "painted_wooden_bench", 11.0, -1.4, -GROUND, face=-90)
    lib.place("grounds", "watering_can_metal_01", 12.0, -1.3, -GROUND, face=-120)
    lib.place("grounds", "planter_box_01", 2.6, -1.25, -GROUND, face=-90, scale=0.8)
    for point in lib_lamps:
        lib.place("walls", "industrial_wall_lamp", point[0], point[1], point[2], face=-90)
    return g


TREES = [
    # asset, part, x, y, scale
    ("tree_small_02", None, -4.5, -3.0, 1.3), ("tree_small_02", None, 20.5, -2.0, 1.15),
    ("tree_small_02", None, 14.5, 19.5, 1.4), ("tree_small_02", None, -4.5, 17.0, 1.25),
    ("fir_tree_01", 0, -6.5, 6.0, 0.75), ("fir_tree_01", 1, 22.0, 7.0, 0.8),
    ("fir_tree_01", 2, 5.0, 21.5, 0.85), ("fir_tree_01", 0, 20.5, 19.5, 0.7),
    ("tree_small_02", None, 21.0, -7.0, 1.0),
]


def plant_trees(lib, collection):
    trees = []
    for asset, part, x, y, scale in TREES:
        meshes = lib.load(asset)
        chosen = meshes if part is None else [meshes[part]]
        c = centre(chosen[0]) if part is not None else Vector((0, 0, 0))
        for source in chosen:
            copy = source.copy()  # linked data: millions of triangles stay in memory once
            copy.hide_render = False
            rot = random.uniform(0, 360)
            copy.matrix_world = (
                Matrix.Translation((x, y, -GROUND)) @ Matrix.Rotation(math.radians(rot), 4, "Z")
                @ Matrix.Diagonal((scale, scale, scale, 1)) @ Matrix.Translation((-c.x, -c.y, 0))
            )
            collection.objects.link(copy)
            trees.append((asset, part, copy))
    return trees


# -------------------------------------------------------------------- lighting


def add_lights(collection, scene):
    world = bpy.data.worlds.new("sky")
    scene.world = world
    nodes, links = world.node_tree.nodes, world.node_tree.links
    bg = nodes["Background"]
    env = nodes.new("ShaderNodeTexEnvironment")
    env.image = bpy.data.images.load(str(HDRI))
    mapping = nodes.new("ShaderNodeMapping")
    coords = nodes.new("ShaderNodeTexCoord")
    links.new(coords.outputs["Generated"], mapping.inputs["Vector"])
    links.new(mapping.outputs["Vector"], env.inputs["Vector"])
    links.new(env.outputs["Color"], bg.inputs["Color"])
    bg.inputs["Strength"].default_value = 1.0
    # Turn the sky so the sun shines from the front left, into the front rooms
    w, h = env.image.size
    pixels = np.empty(w * h * 4, dtype=np.float32)
    env.image.pixels.foreach_get(pixels)
    lum = pixels.reshape(h, w, 4)[..., :3].sum(axis=2)
    v, u = np.unravel_index(np.argmax(lum), lum.shape)
    sun_azimuth = (0.5 - (u + 0.5) / w) * 2 * math.pi
    target = math.atan2(-1.0, -0.55)
    mapping.inputs["Rotation"].default_value = (0, 0, sun_azimuth - target)
    log(f"sun in sky at azimuth {math.degrees(sun_azimuth):.0f}, elevation {((v + 0.5) / h - 0.5) * 180:.0f}")

    # Portals in every window guide the sky light into the rooms
    for obj in [o for o in collection.objects if o.name == "glass"]:
        mesh = obj.data
        done = set()
        for poly in mesh.polygons:
            n = poly.normal
            if abs(n.z) > 0.5 or poly.area < 0.3:
                continue
            key = (round(poly.center.x, 1), round(poly.center.y, 1), round(poly.center.z, 1))
            if any(abs(key[0] - k[0]) < 0.05 and abs(key[1] - k[1]) < 0.05 for k in done):
                continue
            done.add(key)
            xs = [mesh.vertices[i].co for i in poly.vertices]
            size_u = max((a - b).length for a in xs for b in xs) / math.sqrt(2)
            data = bpy.data.lights.new("portal", "AREA")
            data.shape = "RECTANGLE"
            height = max(c.z for c in xs) - min(c.z for c in xs)
            width = max(abs(c.x - xs[0].x) + abs(c.y - xs[0].y) for c in xs)
            data.size, data.size_y = width, height
            try:
                data.cycles.is_portal = True
            except AttributeError:
                data.is_portal = True
            light = bpy.data.objects.new("portal", data)
            inward = Vector((0, 1, 0)) if abs(n.y) > 0.5 else Vector((1, 0, 0))
            centre_pt = poly.center
            # Point the portal into the house
            if (centre_pt.y < 1 and abs(n.y) > 0.5) or (centre_pt.x < 1 and abs(n.x) > 0.5):
                direction = inward
            else:
                direction = -inward
            light.location = centre_pt - direction * (EXT / 2 + 0.02)
            light.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
            collection.objects.link(light)

    for i, bath in enumerate(BATHS):
        data = bpy.data.lights.new(f"bath_{i}", "AREA")
        data.shape = "RECTANGLE"
        data.size, data.size_y = bath["w"] * 0.6, bath["d"] * 0.6
        data.energy = 9.0 * bath["w"] * bath["d"]
        data.color = (1.0, 0.92, 0.82)
        light = bpy.data.objects.new(f"bath_{i}", data)
        light.location = (bath["x"] + bath["w"] / 2, bath["y"] + bath["d"] / 2, H - 0.02)
        collection.objects.link(light)

    # Warm light from the lamps, and a little bounce that the ceilings would give
    for slug, room in ROOMS.items():
        x0, y0, x1, y1 = inner_rect(room)
        data = bpy.data.lights.new(f"fill_{slug}", "AREA")
        data.shape = "RECTANGLE"
        data.size, data.size_y = (x1 - x0) * 0.7, (y1 - y0) * 0.7
        data.energy = 6.0 * (x1 - x0) * (y1 - y0)
        data.color = (1.0, 0.86, 0.7)
        light = bpy.data.objects.new(f"fill_{slug}", data)
        light.location = ((x0 + x1) / 2, (y0 + y1) / 2, H - 0.02)
        collection.objects.link(light)


# ---------------------------------------------------------------------- photos


PHOTOS = {
    # slug: (camera position, look-at point, focal length)
    "exterior": ((-4.5, -7.6, 1.6), (7.0, 2.5, 2.4), 24),
    "lounge": ((0.45, 4.8, 1.75), (5.0, 1.0, 0.7), 17),
    "breakfast-room": ((10.65, 4.6, 1.6), (7.0, 1.0, 0.9), 18),
    "garden-room": ((11.5, 0.5, 1.55), (14.5, 4.4, 0.85), 18),
    "courtyard-suite": ((0.5, 5.5, 1.6), (6.0, 9.2, 0.8), 18),
    "linen-room": ((9.35, 5.4, 1.65), (14.3, 9.2, 0.8), 18),
}


def look_at(obj, target):
    direction = Vector(target) - obj.location
    obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()


def render_photos(scene, collection):
    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    scene.render.resolution_x, scene.render.resolution_y = (800, 500) if QUICK else (1600, 1000)
    scene.render.resolution_percentage = 100
    scene.cycles.samples = 48 if QUICK else 160
    scene.cycles.use_denoising = True
    scene.view_settings.view_transform = "AgX"
    scene.view_settings.look = "AgX - Medium High Contrast"
    scene.render.image_settings.file_format = "WEBP"
    scene.render.image_settings.quality = 84
    cam_data = bpy.data.cameras.new("photo")
    cam_data.sensor_width = 36
    cam = bpy.data.objects.new("photo", cam_data)
    collection.objects.link(cam)
    scene.camera = cam
    ceiling = bpy.data.objects["ceiling"]
    for slug, (pos, target, lens) in PHOTOS.items():
        if ONLY_PHOTOS is not None and slug not in ONLY_PHOTOS:
            continue
        cam.location = pos
        look_at(cam, target)
        cam_data.lens = lens
        ceiling.hide_render = slug == "exterior"
        scene.view_settings.exposure = 0.0 if slug == "exterior" else 1.4
        scene.render.filepath = str(PHOTO_DIR / f"{slug}.webp")
        started = time.time()
        bpy.ops.render.render(write_still=True)
        log(f"photo {slug}: {time.time() - started:.0f}s")
    ceiling.hide_render = False
    bpy.data.objects.remove(cam)


# ------------------------------------------------------------------------ bake


BAKE_SIZES = {"walls": 4096, "walls_in": 4096, "walls_top": 1024, "grounds": 4096, "roof": 2048}
ROOM_BAKE_SIZE = 4096


def extract_faces(obj, name, keep, collection):
    """Move the faces for which keep(face) is true into a new object called name."""
    part = obj.copy()
    part.data = obj.data.copy()
    part.name = part.data.name = name
    collection.objects.link(part)
    for target, wanted in ((obj, False), (part, True)):
        bm = bmesh.new()
        bm.from_mesh(target.data)
        doomed = [f for f in bm.faces if keep(f) != wanted]
        bmesh.ops.delete(bm, geom=doomed, context="FACES")
        bm.to_mesh(target.data)
        bm.free()
    return part


def is_top(face):
    """Faces at ceiling height facing up: black when baked under the ceiling, seen from above on the site."""
    return face.normal.z > 0.9 and face.calc_center_median().z > H - 0.02


def is_interior(face):
    c = face.calc_center_median()
    return 0.02 < c.x < FW - 0.02 and 0.02 < c.y < FD - 0.02 and c.z < H + 0.01


def lightmap_uv(obj, margin):
    mesh = obj.data
    render_uv = next((l.name for l in mesh.uv_layers if l.active_render), None)
    if render_uv is None and len(mesh.uv_layers):
        render_uv = mesh.uv_layers[0].name
    layer = mesh.uv_layers.new(name="Lightmap")
    mesh.uv_layers.active = layer
    if render_uv:
        # Asset textures keep reading their own UVs while baking
        mesh.uv_layers[render_uv].active_render = True
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.uv.smart_project(angle_limit=math.radians(60), island_margin=margin, area_weight=0.0, scale_to_bounds=False)
    bpy.ops.uv.pack_islands(margin=margin, rotate=True)
    bpy.ops.object.mode_set(mode="OBJECT")


def denoise(image):
    """Run Open Image Denoise on a float image through the compositor."""
    scene = bpy.context.scene
    tree = bpy.data.node_groups.new("denoise", "CompositorNodeTree")
    tree.interface.new_socket("Image", in_out="OUTPUT", socket_type="NodeSocketColor")
    source = tree.nodes.new("CompositorNodeImage")
    source.image = image
    node = tree.nodes.new("CompositorNodeDenoise")
    node.inputs["HDR"].default_value = True
    out = tree.nodes.new("NodeGroupOutput")
    tree.links.new(source.outputs[0], node.inputs["Image"])
    tree.links.new(node.outputs[0], out.inputs[0])
    saved = (scene.compositing_node_group, scene.render.engine, scene.render.resolution_x, scene.render.resolution_y,
             scene.view_settings.view_transform, scene.view_settings.look, scene.view_settings.exposure, scene.camera)
    cam = bpy.data.objects.new("denoise_cam", bpy.data.cameras.new("denoise_cam"))
    scene.collection.objects.link(cam)
    scene.camera = cam
    scene.compositing_node_group = tree
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.render.resolution_x, scene.render.resolution_y = image.size
    scene.render.use_compositing = True
    scene.view_settings.view_transform = "Raw"
    scene.view_settings.look = "None"
    scene.view_settings.exposure = 0.0
    hidden = [o for o in scene.objects if not o.hide_render]
    for o in hidden:
        o.hide_render = True
    bpy.ops.render.render()
    path = BAKE_DIR / f"{image.name}_dn.exr"
    scene.render.image_settings.file_format = "OPEN_EXR"
    bpy.data.images["Render Result"].save_render(str(path))
    for o in hidden:
        o.hide_render = False
    (scene.compositing_node_group, scene.render.engine, scene.render.resolution_x, scene.render.resolution_y,
     scene.view_settings.view_transform, scene.view_settings.look, scene.view_settings.exposure, scene.camera) = saved
    bpy.data.objects.remove(cam)
    bpy.data.node_groups.remove(tree)
    result = bpy.data.images.load(str(path))
    result.colorspace_settings.name = "Linear Rec.709"
    return result


def tonemap(pixels, exposure):
    """Exposure, a filmic ACES curve and the sRGB encoding, close to the photos."""
    rgb = pixels[..., :3] * (2.0 ** exposure)
    a, b, c, d, e = 2.51, 0.03, 2.43, 0.59, 0.14
    rgb = np.clip((rgb * (a * rgb + b)) / (rgb * (c * rgb + d) + e), 0.0, 1.0)
    rgb = np.where(rgb <= 0.0031308, rgb * 12.92, 1.055 * np.power(rgb, 1 / 2.4) - 0.055)
    pixels[..., :3] = rgb
    pixels[..., 3] = 1.0
    return pixels


def bake(scene, obj, size, exposure):
    started = time.time()
    margin = 4.0 / size
    lightmap_uv(obj, margin * 2)
    image = bpy.data.images.new(f"bake_{obj.name}", size, size, float_buffer=True, alpha=False)
    image.colorspace_settings.name = "Linear Rec.709"
    for index, mat in enumerate(obj.data.materials):
        local = mat.copy()
        obj.data.materials[index] = local
        node = local.node_tree.nodes.new("ShaderNodeTexImage")
        node.image = image
        local.node_tree.nodes.active = node
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    settings = scene.render.bake
    settings.use_pass_direct = settings.use_pass_indirect = True
    settings.use_pass_diffuse = settings.use_pass_transmission = settings.use_pass_emit = True
    settings.use_pass_glossy = False
    settings.margin = 8
    settings.margin_type = "EXTEND"
    bpy.ops.object.bake(type="COMBINED")
    BAKE_DIR.mkdir(parents=True, exist_ok=True)
    clean = denoise(image)
    pixels = np.empty(size * size * 4, dtype=np.float32)
    clean.pixels.foreach_get(pixels)
    pixels = tonemap(pixels.reshape(size, size, 4), exposure)
    out = bpy.data.images.new(f"lightmap_{obj.name}", size, size, alpha=False)
    out.colorspace_settings.name = "sRGB"
    out.pixels.foreach_set(pixels.ravel())
    out.filepath_raw = str(BAKE_DIR / f"{obj.name}.png")
    out.file_format = "PNG"
    out.save()
    bpy.data.images.remove(image)
    bpy.data.images.remove(clean)
    log(f"bake {obj.name} {size}px: {time.time() - started:.0f}s")
    return out


def use_lightmap(obj, image):
    """Replace every material of obj with one showing its lightmap, through the Lightmap UVs only."""
    mat = bpy.data.materials.new(f"baked_{obj.name}")
    nodes, links = mat.node_tree.nodes, mat.node_tree.links
    bsdf = nodes.get("Principled BSDF")
    tex = nodes.new("ShaderNodeTexImage")
    tex.image = image
    uv = nodes.new("ShaderNodeUVMap")
    uv.uv_map = "Lightmap"
    links.new(uv.outputs["UV"], tex.inputs["Vector"])
    links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
    bsdf.inputs["Roughness"].default_value = 1.0
    obj.data.materials.clear()
    obj.data.materials.append(mat)
    for poly in obj.data.polygons:
        poly.material_index = 0
    for layer in [l for l in obj.data.uv_layers if l.name != "Lightmap"]:
        obj.data.uv_layers.remove(layer)


# ----------------------------------------------------------------- impostors


def impostors(scene, trees, collection):
    """Render each tree once from the side and from above; place textured cards on the web model."""
    out_dir = BAKE_DIR / "trees"
    out_dir.mkdir(parents=True, exist_ok=True)
    kinds = {}
    for asset, part, obj in trees:
        kinds.setdefault((asset, part), []).append(obj)
    saved_hidden = {o: o.hide_render for o in scene.objects}
    scene.render.film_transparent = True
    scene.cycles.samples = 32 if QUICK else 128
    scene.cycles.use_denoising = True
    scene.view_settings.view_transform = "AgX"
    scene.view_settings.look = "AgX - Medium High Contrast"
    scene.view_settings.exposure = 0.0
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    cam_data = bpy.data.cameras.new("imp")
    cam_data.type = "ORTHO"
    cam = bpy.data.objects.new("imp", cam_data)
    collection.objects.link(cam)
    scene.camera = cam
    cards = []
    for (asset, part), objs in kinds.items():
        sample = objs[0]
        for o in scene.objects:
            o.hide_render = o is not sample and o is not cam
        pts = [sample.matrix_world @ Vector(c) for c in sample.bound_box]
        lo = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
        hi = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
        width = max(hi.x - lo.x, hi.y - lo.y)
        height = hi.z - lo.z
        mid = (lo + hi) / 2
        images = {}
        for view in ("side", "top"):
            if view == "side":
                scene.render.resolution_x, scene.render.resolution_y = 512, int(512 * height / width)
                cam_data.ortho_scale = max(width, height)
                cam.location = (mid.x, lo.y - 30, mid.z)
                cam.rotation_euler = (math.pi / 2, 0, 0)
            else:
                scene.render.resolution_x = scene.render.resolution_y = 512
                cam_data.ortho_scale = width
                cam.location = (mid.x, mid.y, hi.z + 30)
                cam.rotation_euler = (0, 0, 0)
            cam_data.clip_end = 200
            if QUICK:
                scene.render.resolution_x //= 2
                scene.render.resolution_y //= 2
            path = out_dir / f"{asset}_{part}_{view}.png"
            scene.render.filepath = str(path)
            bpy.ops.render.render(write_still=True)
            images[view] = bpy.data.images.load(str(path))
        mat = card_material(f"tree_{asset}_{part}_side", images["side"])
        top_mat = card_material(f"tree_{asset}_{part}_top", images["top"])
        for obj in objs:
            p = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
            olo = Vector((min(q.x for q in p), min(q.y for q in p), min(q.z for q in p)))
            ohi = Vector((max(q.x for q in p), max(q.y for q in p), max(q.z for q in p)))
            c = (olo + ohi) / 2
            ow, oh = max(ohi.x - olo.x, ohi.y - olo.y), ohi.z - olo.z
            spin = random.uniform(0, 45)
            for angle in (0, 90):
                cards.append(card(f"tree_{len(cards)}", mat, (c.x, c.y, olo.z + oh / 2), ow, oh, angle + spin))
            cards.append(card(f"tree_{len(cards)}", top_mat, (c.x, c.y, olo.z + oh * 0.62), ow * 0.95, ow * 0.95, 0, flat=True))
        log(f"impostor {asset} {part}")
    for o, hidden in saved_hidden.items():
        o.hide_render = hidden
    scene.render.film_transparent = False
    bpy.data.objects.remove(cam)
    for c in cards:
        collection.objects.link(c)
    return cards


def card_material(name, image):
    mat = bpy.data.materials.new(name)
    nodes, links = mat.node_tree.nodes, mat.node_tree.links
    bsdf = nodes.get("Principled BSDF")
    tex = nodes.new("ShaderNodeTexImage")
    tex.image = image
    links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
    links.new(tex.outputs["Alpha"], bsdf.inputs["Alpha"])
    return mat


def card(name, mat, center, width, height, angle, flat=False):
    mesh = bpy.data.meshes.new(name)
    w, h = width / 2, height / 2
    if flat:
        verts = [(-w, -w, 0), (w, -w, 0), (w, w, 0), (-w, w, 0)]
    else:
        verts = [(-w, 0, -h), (w, 0, -h), (w, 0, h), (-w, 0, h)]
    mesh.from_pydata(verts, [], [(0, 1, 2, 3)])
    uv = mesh.uv_layers.new(name="UVMap")
    for loop, (u, v) in zip(uv.data, ((0, 0), (1, 0), (1, 1), (0, 1))):
        loop.uv = (u, v)
    mesh.materials.append(mat)
    obj = bpy.data.objects.new(name, mesh)
    obj.location = center
    obj.rotation_euler = (0, 0, math.radians(angle))
    return obj


# --------------------------------------------------------------------- export


def export(objects, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.object.select_all(action="DESELECT")
    for obj in objects:
        obj.select_set(True)
    bpy.ops.export_scene.gltf(
        filepath=str(path),
        export_format="GLB",
        use_selection=True,
        export_apply=True,
        export_texcoords=True,
        export_normals=False,
        export_materials="EXPORT",
        export_image_format="WEBP",
        export_image_quality=80,
        export_draco_mesh_compression_enable=True,
        export_draco_mesh_compression_level=7,
        export_lights=False,
        export_cameras=False,
        export_yup=True,
    )
    log(f"exported {path.name} ({path.stat().st_size / 1e6:.1f} MB)")


# ------------------------------------------------------------------------ main


def main():
    started = time.time()
    scene = reset_scene()
    collection = scene.collection
    library_col = bpy.data.collections.new("library")
    scene.collection.children.link(library_col)
    materials = make_materials()
    lib = Library(library_col)

    walls, glass, roof, room_bits = build_shell(materials)
    build_roof(roof, materials)
    furnish(lib, room_bits)
    grounds = build_grounds(materials, lib)
    ceiling = Group("ceiling", materials)
    ceiling.box(-EXT / 2, -EXT / 2, H, FW + EXT / 2, FD + EXT / 2, H + 0.1, "ceiling")
    far = Group("far_ground", materials)
    far.box(-400, -400, -GROUND - 0.4, 400, 400, -GROUND - 0.03, "far_grass")

    objects = {"walls": walls.build(collection), "glass": glass.build(collection), "roof": roof.build(collection),
               "grounds": grounds.build(collection), "ceiling": ceiling.build(collection), "far_ground": far.build(collection)}
    for slug, group in room_bits.items():
        objects[f"room_{slug}"] = group.build(collection)

    # Fold placed assets into the object they belong to; potted plants stay apart
    plants = []
    for key, placed in lib.placed.items():
        target = objects[f"room_{key}"] if key in ROOMS else objects[key]
        solid = [o for o in placed if o.get("asset") not in FOLIAGE]
        plants += [o for o in placed if o not in solid]
        if solid:
            objects[target.name] = realise([target] + solid, target.name)
    plants = [realise([p], f"plant_{i}") for i, p in enumerate(plants)]
    trees = plant_trees(lib, collection)
    add_lights(collection, scene)
    log(f"built in {time.time() - started:.0f}s")

    if not SKIP_PHOTOS:
        render_photos(scene, collection)
    if SKIP_BAKE:
        return

    scene.cycles.samples = 32 if QUICK else 128
    scene.cycles.use_denoising = False
    objects["glass"].hide_render = True
    for plant in plants:
        plant.hide_render = True  # keep them out of the lightmaps; the site draws them with their textures
    tops = [extract_faces(objects[n], f"top_{n}", is_top, collection) for n in ["walls"] + [f"room_{s}" for s in ROOMS]]
    filled = [t for t in tops if len(t.data.polygons)]
    for empty in [t for t in tops if t not in filled]:
        bpy.data.objects.remove(empty)
    objects["walls_top"] = realise(filled, "walls_top")
    objects["walls_in"] = extract_faces(objects["walls"], "walls_in", is_interior, collection)
    bake_targets = ["walls", "walls_in", "walls_top", "grounds", "roof"] + [f"room_{slug}" for slug in ROOMS]
    for name in bake_targets:
        size = BAKE_SIZES.get(name, ROOM_BAKE_SIZE)
        if QUICK:
            size //= 4
        # Interior surfaces share one exposure, so walls and floors match
        exposure = 0.3 if name in {"grounds", "roof"} else 1.1
        if name == "walls":
            exposure = 0.6
        cached = BAKE_DIR / f"{name}.png"
        if REBAKE is not None and name not in REBAKE and cached.exists():
            # Same geometry gives the same UV layout, so the old lightmap still fits
            lightmap_uv(objects[name], 8.0 / size)
            image = bpy.data.images.load(str(cached))
            log(f"reused {name}")
        else:
            # Wall tops are lit as the site shows them: open to the sky
            hidden = [objects["ceiling"], objects["roof"]] if name == "walls_top" else []
            for o in hidden:
                o.hide_render = True
            image = bake(scene, objects[name], size, exposure)
            for o in hidden:
                o.hide_render = False
        use_lightmap(objects[name], image)
    for plant in plants:
        plant.hide_render = False

    # The site shows plants with their colour and cut-out only
    for plant in plants:
        for mat in plant.data.materials:
            bsdf = next((n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
            if bsdf is None:
                continue
            for socket in ("Roughness", "Metallic", "Normal", "Specular IOR Level"):
                for link in list(bsdf.inputs[socket].links):
                    mat.node_tree.links.remove(link)
    cards = impostors(scene, trees, collection)
    parts = [objects[n] for n in bake_targets] + [objects["glass"]] + plants + cards
    export(parts, MODEL_OUT.with_name("alder-house-hd.glb"))
    # A lighter file for phones: every lightmap at half size
    for name in bake_targets:
        mat = objects[name].data.materials[0]
        node = next(n for n in mat.node_tree.nodes if n.type == "TEX_IMAGE")
        small = node.image.copy()
        w, h = node.image.size
        small.scale(max(w // 2, 256), max(h // 2, 256))
        small.filepath_raw = str(BAKE_DIR / f"{name}_sd.png")
        small.file_format = "PNG"
        small.save()
        node.image = bpy.data.images.load(small.filepath_raw)
    export(parts, MODEL_OUT)
    log(f"done in {(time.time() - started) / 60:.1f} min")


main()
