"""Probe one point of a bundle and print everything the bundle says about it.

The point of this tool is to end arguments about *which* patch of ground is being looked at.
You pick a spot in Blender, it prints a coordinate block, and that block is unambiguous.

TWO WAYS TO RUN IT
------------------

**In Blender, on the scene you already imported** (this is the one you want). Put the 3D
cursor on the spot — `Shift` + right-click — then either paste this file into the Scripting
tab and press Run, or from a terminal.

WHERE THE OUTPUT GOES. Run from the Scripting tab, `print` goes to the system console, which
Windows hides — so the report is ALSO copied to the clipboard (just Ctrl+V it) and written to
a text datablock called `carto_probe`, which you can open in the Text Editor. If you do want
the console: Window > Toggle System Console.

Terminal form:

    & "G:\\SteamLibrary\\steamapps\\common\\Blender\\blender.exe" scene.blend ^
        --python E:\\csii-world-bundle\\tools\\probe_point.py

It reports the 3D cursor position by default. `--at X Z` overrides it, and `--render` also
reports the colour actually on screen at that point.

`--render` gives FOUR numbers, and the distinctions are the point of it:

  * VIEWPORT and RENDER separately. They are different paths — anything living in the
    compositor shows in one and not the other — and the viewport is the one a person looks
    at. VIEWPORT needs a real Blender session; under `blender -b` there is no GL context and
    it says so instead of quietly reporting the other path.
  * a centre TEXEL and a patch MEAN for each. An in-game eyedropper reads one pixel; a mean
    over a patch averages chroma and always comes out less saturated. Comparing a mean to an
    eyedropper is comparing two different quantities, which is how a "4x saturation gap" got
    into this repo's notes for a while.

It also prints what `tools/colour_model.py` predicts for the same albedo, re-exposed to the
measured value — exposure is the one term the bundle cannot supply, so hue and saturation are
what a comparison should turn on.

**Standalone, no Blender**, when you already have a coordinate:

    python tools/probe_point.py --at 1234 -5678

OPTIONS
-------
    --at X Z              probe this bundle-frame point instead of the 3D cursor
    --bundle DIR          bundle root (default: the ModsData/Carto path)
    --render             (Blender only) also render a patch and report the on-screen colour
    --render-size M       patch width in metres for --render (default 8)
"""

import json
import os
import sys

try:
    import bpy

    IN_BLENDER = True
except ImportError:
    IN_BLENDER = False

DEFAULT_BUNDLE = os.path.join(
    os.path.expanduser("~"),
    "AppData",
    "LocalLow",
    "Colossal Order",
    "Cities Skylines II",
    "ModsData",
    "Carto",
)

# The terrain grid is built in bundle world units, so Blender (x, y) IS bundle (x, z).
# Stated here because it is the one assumption a reader should be able to check.
BLENDER_XY_IS_BUNDLE_XZ = True

_SPLAT_DEFAULT = {"R": "dirt", "G": "grass", "base": "rock"}

# B and A each pack TWO 4-bit weights, which is how four paintable extras fit the game's R8G8
# user splatmap: B.low/B.high = extra1/extra2, A.low/A.high = extra3/extra4. The four are the
# game's paint tools 01 Dry, 02 Lush, 03 Dirt, 04 Sand. A nibble normalises by 15, not 255.
_PACKED_EXTRAS = (
    (2, "low", "extra1"),
    (2, "high", "extra2"),
    (3, "low", "extra3"),
    (3, "high", "extra4"),
)


# -- small helpers --------------------------------------------------------------------------


def _srgb_to_linear(c):
    c = c / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _linear_to_srgb(c):
    c = max(0.0, min(1.0, c))
    return c * 12.92 if c <= 0.0031308 else 1.055 * (c ** (1 / 2.4)) - 0.055


def _hex(rgb01):
    return "#" + "".join(f"{int(round(max(0.0, min(1.0, v)) * 255)):02x}" for v in rgb01)


def _hsv(rgb01):
    r, g, b = rgb01
    mx, mn = max(rgb01), min(rgb01)
    d = mx - mn
    if d == 0:
        h = 0.0
    elif mx == r:
        h = (60 * ((g - b) / d)) % 360
    elif mx == g:
        h = 60 * (2 + (b - r) / d)
    else:
        h = 60 * (4 + (r - g) / d)
    return h, (0.0 if mx == 0 else d / mx), mx


def _load_png(path):
    """Read a PNG to (width, height, channels, array[h][w][c]) of ints.

    Pillow when it is there, otherwise a self-contained numpy decoder — **Blender ships numpy
    but not Pillow**, and this tool has to run inside Blender, which is the whole point of it.
    Deliberately not imported from `blender_import_bundle`: a diagnostic that only works when
    the thing it is diagnosing imports cleanly is not much of a diagnostic.
    """
    try:
        import numpy
        from PIL import Image

        Image.MAX_IMAGE_PIXELS = None
        a = numpy.asarray(Image.open(path))
        if a.ndim == 2:
            a = a[:, :, None]
        return a.shape[1], a.shape[0], a.shape[2], a
    except ImportError:
        pass

    import struct
    import zlib

    import numpy

    data = open(path, "rb").read()
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError(f"{path} is not a PNG")
    pos, idat = 8, []
    width = height = depth = colour = None
    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        tag = data[pos + 4 : pos + 8]
        payload = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if tag == b"IHDR":
            width, height = struct.unpack(">II", payload[:8])
            depth, colour = payload[8], payload[9]
        elif tag == b"IDAT":
            idat.append(payload)
        elif tag == b"IEND":
            break

    channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(colour)
    if channels is None or depth not in (8, 16):
        raise ValueError(f"{path}: unsupported PNG (colour type {colour}, depth {depth})")

    bpp = channels * (depth // 8)
    stride = width * bpp
    raw = zlib.decompress(b"".join(idat))
    out = numpy.zeros((height, stride), dtype=numpy.uint8)
    previous = numpy.zeros(stride, dtype=numpy.uint8)
    for y in range(height):
        off = y * (stride + 1)
        kind = raw[off]
        line = numpy.frombuffer(raw, dtype=numpy.uint8, count=stride, offset=off + 1).copy()
        if kind == 0:
            pass
        elif kind == 2:  # Up
            line += previous
        elif kind == 1:  # Sub
            lanes = line.reshape(-1, bpp).astype(numpy.uint32)
            line = numpy.cumsum(lanes, axis=0, dtype=numpy.uint32).astype(numpy.uint8).reshape(-1)
        else:  # Average(3) / Paeth(4), sequential
            buf = bytearray(line.tobytes())
            prev = bytes(previous.tobytes())
            for i in range(stride):
                left = buf[i - bpp] if i >= bpp else 0
                up = prev[i]
                if kind == 3:
                    buf[i] = (buf[i] + ((left + up) >> 1)) & 0xFF
                else:
                    corner = prev[i - bpp] if i >= bpp else 0
                    est = left + up - corner
                    dist = (abs(est - left), abs(est - up), abs(est - corner))
                    buf[i] = (buf[i] + (left, up, corner)[dist.index(min(dist))]) & 0xFF
            line = numpy.frombuffer(bytes(buf), dtype=numpy.uint8)
        out[y] = line
        previous = out[y]

    if depth == 16:
        arr = out.view(">u2" if sys.byteorder == "little" else "u2")
        arr = arr.reshape(height, width, channels).astype(numpy.uint16)
    else:
        arr = out.reshape(height, width, channels)
    return width, height, channels, arr


# -- the probe ------------------------------------------------------------------------------


class Bundle:
    def __init__(self, root):
        self.root = root
        with open(os.path.join(root, "terrain", "rasters.json"), encoding="utf-8") as fh:
            self.rasters = json.load(fh)
        self._cache = {}

    def raster(self, name):
        if name not in self._cache:
            self._cache[name] = _load_png(os.path.join(self.root, "terrain", name))
        return self._cache[name]

    def texel(self, block, x, z):
        """(col, row) for a bundle-frame point, using the block's texel-centre origin."""
        ox, _, oz = block["originWorld"]
        res = block.get("resolution", self.rasters.get("resolution"))
        rx, rz = res if isinstance(res, list) else [res, res]
        return int(round((x - ox) / rx)), int(round((oz - z) / rz))


def probe(bundle, x, z):
    out = {"x": x, "z": z}
    r = bundle.rasters

    # --- terrain elevation and water
    top = {"originWorld": r["originWorld"], "resolution": r["resolution"]}
    col, row = bundle.texel(top, x, z)
    out["texel_terrain"] = (col, row)
    w, h, _, hm = bundle.raster("heightmap.png")
    inside = (0 <= col < w) and (0 <= row < h)
    out["inside"] = inside
    if inside:
        raw = float(hm[row, col, 0])
        out["elevation"] = raw * r["elevationScale"] + r.get("elevationOffset", 0.0)
        _, _, _, wl = bundle.raster("water_level.png")
        out["water_level"] = float(wl[row, col, 0]) * r["elevationScale"]
        out["above_water"] = out["elevation"] - out["water_level"]
        try:
            _, _, _, si = bundle.raster("surface_index.png")
            idx = int(si[row, col, 0])
            legend = {}
            p = os.path.join(bundle.root, "terrain", "surfaces", "index.json")
            if os.path.isfile(p):
                with open(p, encoding="utf-8") as fh:
                    legend = json.load(fh)
            entry = legend.get(str(idx), {})
            out["surface_index"] = (idx, entry.get("name", "?"))
        except Exception:
            pass

    # --- splat
    sb = r.get("splatmap")
    if sb:
        scol, srow = bundle.texel(sb, x, z)
        sw, sh, _, sp = bundle.raster(os.path.basename(sb["file"]))
        out["texel_splat"] = (scol, srow)
        if 0 <= scol < sw and 0 <= srow < sh:
            rgba = [int(v) for v in sp[srow, scol][:4]]
            out["splat_rgba"] = rgba
            mapping = dict(_SPLAT_DEFAULT)
            published = sb.get("channels")
            if isinstance(published, dict):
                for k in mapping:
                    nm = published.get(k, mapping[k])
                    mapping[k] = None if (not isinstance(nm, str) or nm == "unknown") else nm
            out["channels"] = mapping
            mult = float(sb.get("splatMultiplier", 1.0))
            power = float(sb.get("splatPower", 1.0))
            out["splat_shape"] = (mult, power)

            stack = []
            for index, key in ((0, "R"), (1, "G")):
                ground = mapping.get(key)
                if ground:
                    op = min(rgba[index] / 255.0 * mult, 1.0) ** power
                    stack.append((key, ground, f"{rgba[index]}/255", op))
            for index, half, ground in _PACKED_EXTRAS:
                nib = (rgba[index] >> 4) if half == "high" else (rgba[index] & 15)
                op = min(nib / 15.0 * mult, 1.0) ** power
                stack.append((f"{'BA'[index - 2]}.{half}", ground, f"{nib}/15", op))
            out["stack"] = stack

            # The composite the importer builds: base, then each channel over it.
            # `base` is None when the producer wrote `channels.base: "unknown"`, and
            # `_ground_mean` returns None for a ground whose texture the bundle does not carry
            # (declared in warnings.jsonl). Either way there is no base colour to composite
            # over, and mid-grey is the honest stand-in — indexing None here killed the whole
            # report, after it had been built and before a line of it printed.
            base = mapping.get("base")
            colour = (_ground_mean(bundle, base) if base else None) or (0.5, 0.5, 0.5)
            contrib = []
            for _key, ground, _w, op in stack:
                gc = _ground_mean(bundle, ground)
                if gc is None:
                    continue
                colour = tuple(colour[i] * (1 - op) + gc[i] * op for i in range(3))
                contrib.append((ground, op))
            out["albedo_linear"] = colour
            out["albedo_hex"] = _hex(tuple(_linear_to_srgb(c) for c in colour))
            out["contrib"] = contrib

    return out


_GRADING_MODEL = {}


def _grading_model(root):
    """This bundle's grading, from the one shared derivation in `tools/colour_model.py`.

    Imported rather than reimplemented on purpose: the probe exists to end arguments about what
    is being compared, and it cannot do that if it carries its own private copy of the transform
    the importer applies.
    """
    if root not in _GRADING_MODEL:
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import colour_model

            _GRADING_MODEL[root] = colour_model.load(root)
        except Exception:
            _GRADING_MODEL[root] = None
    return _GRADING_MODEL[root]


_GROUND_MEAN = {}


def _ground_mean(bundle, ground):
    """Mean linear colour of a ground texture — a stand-in for its tiled detail."""
    if ground is None:
        return None
    if ground not in _GROUND_MEAN:
        path = os.path.join(bundle.root, "terrain", "surfaces", f"{ground}.png")
        if not os.path.isfile(path):
            _GROUND_MEAN[ground] = None
        else:
            try:
                _, _, _, a = _load_png(path)
                mean = a[:, :, :3].reshape(-1, 3).mean(0)
                _GROUND_MEAN[ground] = tuple(_srgb_to_linear(float(v)) for v in mean)
            except Exception:
                _GROUND_MEAN[ground] = None
    return _GROUND_MEAN[ground]


def areas_covering(bundle, x, z, limit=6):
    """Which areas.jsonl surfaces contain this point — reported even when --areas was not
    imported, because 'the beach is an area, not a ground' is exactly the kind of thing that
    otherwise gets argued about."""
    path = os.path.join(bundle.root, "areas.jsonl")
    if not os.path.isfile(path):
        return []
    hits = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            nodes = rec.get("nodes")
            if not nodes:
                continue
            xs = [n[0] for n in nodes]
            zs = [n[2] for n in nodes]
            if not (min(xs) - 1 <= x <= max(xs) + 1 and min(zs) - 1 <= z <= max(zs) + 1):
                continue
            for tri in rec.get("triangles", []):
                try:
                    a, b, c = (nodes[i] for i in tri)
                except (IndexError, ValueError):
                    continue
                if _in_tri(x, z, a, b, c):
                    hits.append((rec.get("prefab", "?"), rec.get("id")))
                    break
            if len(hits) >= limit:
                break
    return hits


def _in_tri(px, pz, a, b, c):
    d = (b[2] - c[2]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[2] - c[2])
    if abs(d) < 1e-12:
        return False
    u = ((b[2] - c[2]) * (px - c[0]) + (c[0] - b[0]) * (pz - c[2])) / d
    v = ((c[2] - a[2]) * (px - c[0]) + (a[0] - c[0]) * (pz - c[2])) / d
    return (u >= -1e-6) and (v >= -1e-6) and (u + v <= 1 + 1e-6)


# -- rendered-colour sampling (Blender only) ------------------------------------------------


class _NoRender(Exception):
    """This Blender cannot produce that particular picture (background mode has no GL context)."""


def sample_render(x, z, metres, viewport=False):
    """Render a small ortho patch centred on the point; return (centre texel, patch mean).

    TWO NUMBERS, BECAUSE THEY ARE DIFFERENT QUANTITIES. An in-game eyedropper reads ONE pixel.
    A patch mean averages chroma across many texels, and averaging always desaturates: reporting
    the mean and comparing it to an eyedropper reading is comparing two different measurements
    and calling the difference a finding. The centre texel is the eyedropper-comparable number;
    the mean is still worth having because it is stable against exactly where the point landed.

    `viewport=True` uses the OpenGL viewport render instead of the full render. That is the path
    a person actually looks at, and it is not the same path: the compositor runs on rendered
    output only, so anything living there is visible in one and invisible in the other.
    """
    import tempfile

    from mathutils import Vector

    scene = bpy.context.scene
    prev_cam, prev_path = scene.camera, scene.render.filepath
    prev_x, prev_y = scene.render.resolution_x, scene.render.resolution_y

    cam_data = bpy.data.cameras.new("probe_cam")
    cam_data.type = "ORTHO"
    cam_data.ortho_scale = metres
    cam_data.clip_start, cam_data.clip_end = 1.0, 40000.0
    cam = bpy.data.objects.new("probe_cam", cam_data)
    scene.collection.objects.link(cam)
    cam.location = Vector((x, z, 4000.0))
    cam.rotation_euler = (0.0, 0.0, 0.0)
    scene.camera = cam
    scene.render.resolution_x = scene.render.resolution_y = 64
    out = os.path.join(tempfile.gettempdir(), "carto_probe.png")
    scene.render.filepath = out
    # SIXTEEN BIT, NOT EIGHT, AND IT IS NOT ABOUT PRECISION. Blender hands back an 8-bit PNG's
    # `pixels` still sRGB-ENCODED (measured: a linear 0.25 reads back as 0.5333), while a 16-bit
    # one comes back linear (0.2500). Reading the 8-bit buffer and encoding it again double-
    # encodes: the result is too bright and too flat, which is exactly the shape of the
    # "our terrain is desaturated" reading this tool was built to settle.
    prev_depth = scene.render.image_settings.color_depth
    scene.render.image_settings.color_depth = "16"
    # Do NOT override the view transform here. Forcing "Standard" made this tool report the same
    # colour whatever the scene was set to, which silently invalidated a view-transform comparison
    # — a measurement tool must not change the thing being measured.
    try:
        if viewport:
            # Needs a real GL context, so this is the one number `blender -b` cannot produce.
            # Run the probe from the Scripting tab to get it.
            bpy.ops.render.opengl(write_still=True)
        else:
            bpy.ops.render.render(write_still=True)
    except RuntimeError as exc:
        scene.camera, scene.render.filepath = prev_cam, prev_path
        scene.render.resolution_x, scene.render.resolution_y = prev_x, prev_y
        scene.render.image_settings.color_depth = prev_depth
        bpy.data.objects.remove(cam, do_unlink=True)
        raise _NoRender(str(exc)) from exc

    img = bpy.data.images.load(out)
    px = list(img.pixels)
    n = len(px) // 4
    side = int(round(n**0.5))
    mean = [sum(px[i * 4 + k] for i in range(n)) / n for k in range(3)]
    centre = (side // 2) * side + (side // 2)
    texel = [px[centre * 4 + k] for k in range(3)]
    # Guard the same trap from the other side: encode only what came back linear.
    encode = _linear_to_srgb if img.is_float else (lambda c: c)

    scene.camera, scene.render.filepath = prev_cam, prev_path
    scene.render.resolution_x, scene.render.resolution_y = prev_x, prev_y
    scene.render.image_settings.color_depth = prev_depth
    bpy.data.objects.remove(cam, do_unlink=True)
    bpy.data.images.remove(img)
    return tuple(encode(c) for c in texel), tuple(encode(c) for c in mean)


# -- main -----------------------------------------------------------------------------------


def _argv():
    if "--" in sys.argv:
        return sys.argv[sys.argv.index("--") + 1 :]
    return [a for a in sys.argv[1:] if a.endswith((".py",)) is False]


def main():
    argv = _argv()
    bundle_dir, at, do_render, patch = DEFAULT_BUNDLE, None, False, 8.0
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--bundle":
            bundle_dir = argv[i + 1]
            i += 2
        elif a == "--at":
            at = (float(argv[i + 1]), float(argv[i + 2]))
            i += 3
        elif a == "--render":
            do_render = True
            i += 1
        elif a == "--render-size":
            patch = float(argv[i + 1])
            i += 2
        else:
            i += 1

    if at is None:
        if not IN_BLENDER:
            raise SystemExit("standalone mode needs --at X Z")
        cur = bpy.context.scene.cursor.location
        at = (cur.x, cur.y)
        cursor_z = cur.z
    else:
        cursor_z = None

    x, z = at
    bundle = Bundle(bundle_dir)
    info = probe(bundle, x, z)

    out = []

    def say(line=""):
        out.append(line)

    say()
    say("=" * 78)
    say("  CARTO POINT PROBE - paste this whole block back")
    say("=" * 78)
    if IN_BLENDER and cursor_z is not None:
        say(f"  Blender 3D cursor : ({x:.1f}, {z:.1f}, {cursor_z:.1f})")
    say(f"  Bundle frame      : x = {x:.1f}   z = {z:.1f}")
    if not info.get("inside"):
        say("  !! outside the terrain raster")
    else:
        say(
            f"  elevation         : {info['elevation']:.2f} m   "
            f"water {info['water_level']:.2f} m   "
            f"{info['above_water']:+.2f} m relative to water"
        )
        if "surface_index" in info:
            idx, name = info["surface_index"]
            say(f"  surface_index     : {idx} ({name})")
    say()
    if "splat_rgba" in info:
        r_, g_, b_, a_ = info["splat_rgba"]
        mult, power = info["splat_shape"]
        say(f"  splat texel       : {info['texel_splat']}   RGBA = ({r_}, {g_}, {b_}, {a_})")
        say(f"  splatMultiplier   : {mult}   splatPower: {power}")
        ch = info["channels"]
        say(f"  channel map       : R={ch.get('R')}  G={ch.get('G')}  base={ch.get('base')}")
        say("                      B/A pack extra1..extra4 as low:high nibbles")
        say()
        say("  ground stack, base first, each composited over the last:")
        # `ch["base"]` is None on a theme that binds no rock ground; `:<10` on None is a
        # TypeError, and because the report is buffered it took the whole tool with it.
        say(f"      {'base':<10} {ch.get('base') or 'unknown'!s:<10}")
        for key, ground, weight, op in info["stack"]:
            bar = "#" * int(round(op * 30))
            say(f"      {key:<8} {ground:<8} weight {weight:>7}  opacity {op:5.3f} |{bar}")
        say()
        hsv = _hsv(tuple(_linear_to_srgb(c) for c in info["albedo_linear"]))
        say(
            f"  composite ALBEDO  : {info['albedo_hex']}   "
            f"hue {hsv[0]:5.1f}  sat {hsv[1]:.3f}  val {hsv[2]:.3f}"
        )
        say("      (flat ground colour before lighting - NOT what an eyedropper on the")
        say("       viewport reads; use --render for that)")

    hits = areas_covering(bundle, x, z)
    say()
    if hits:
        say("  area surfaces here: " + ", ".join(f"{p} (id {i_})" for p, i_ in hits))
        say("      *** THE ALBEDO ABOVE IS THE GROUND *UNDER* THIS AREA, NOT WHAT THE GAME DRAWS")
        say("      HERE. The game paints the area surface on top; this importer only places it")
        say("      with --areas. For a colour comparison at this point use the area's own")
        say("      texture (terrain/surfaces/area_<name>.png), not the splat composite.")
    else:
        say("  area surfaces here: none - this point is plain terrain")

    grading = _grading_model(bundle.root)
    shown = None
    if grading is not None and "albedo_linear" in info:
        shown, used = grading.on_screen(info["albedo_linear"])
        say()
        say("  MODEL says         : this albedo, under this bundle's own grading")
        if shown:
            hsv = _hsv(shown)
            say(
                f"      on screen     : {_hex(shown)}   "
                f"hue {hsv[0]:5.1f}  sat {hsv[1]:.3f}  val {hsv[2]:.3f}   "
                f"through {used} at {grading.exposure_ev:+.2f} EV"
            )
        else:
            say(
                f"      graded linear : "
                f"{tuple(round(v, 4) for v in grading.apply(info['albedo_linear']))}   "
                f"(no PyOpenColorIO here, so no on-screen colour)"
            )
        say("      (exposure is the ONE term the bundle cannot supply - the game meters per")
        say("       frame - so compare HUE and SATURATION, and read VALUE as free)")

    if do_render and IN_BLENDER:
        settings = bpy.context.scene.view_settings
        for label, viewport in (("VIEWPORT", True), ("RENDER", False)):
            try:
                texel, mean = sample_render(x, z, patch, viewport=viewport)
            except _NoRender as exc:
                say()
                say(f"  {label} colour  : unavailable here - {exc}")
                say("      (run the probe from Blender's Scripting tab; the viewport path is the")
                say("       one a person looks at, so it is worth the extra step)")
                continue
            th, ts, tv = _hsv(texel)
            mh, ms, mv = _hsv(mean)
            say()
            say(
                f"  {label} colour  : {_hex(texel)}   "
                f"hue {th:5.1f}  sat {ts:.3f}  val {tv:.3f}   <- compare to an eyedropper"
            )
            say(
                f"      {patch:.0f} m patch mean: {_hex(mean)}   "
                f"hue {mh:5.1f}  sat {ms:.3f}  val {mv:.3f}"
            )
            # The scene's own light level is not the game's, so the model is re-exposed to the
            # measured VALUE before the comparison; what is being checked is hue and saturation.
            if grading is not None and shown is not None:
                ev, model, _ = grading.match_value(info["albedo_linear"], tv)
                if model:
                    hh, ss, vv = _hsv(model)
                    say(
                        f"      model at the same value ({ev:+.2f} EV): {_hex(model)}   "
                        f"hue {hh:5.1f}  sat {ss:.3f}  val {vv:.3f}"
                    )
                    say(f"      -> difference: hue {th - hh:+.1f}, saturation {ts - ss:+.3f}")
        say(
            f"      ({settings.view_transform} view transform, "
            f"exposure {settings.exposure:+.2f}, gamma {settings.gamma:.2f})"
        )
        say("      VIEWPORT is what you look at; RENDER additionally runs the compositor. If")
        say("      they differ, something is living in a path half the tools cannot see.")
    say("=" * 78)
    say()

    report = "\n".join(out)
    print(report)
    _deliver(report)


def _deliver(report):
    """Put the report somewhere findable.

    `print` alone is not good enough here: run from Blender's Scripting tab on Windows it
    goes to the system console, which is hidden by default (Window > Toggle System Console),
    so the tool looks like it did nothing. So the report also lands in a `carto_probe` text
    datablock — open it in the Text Editor — and on the clipboard, ready to paste.
    """
    if not IN_BLENDER:
        return
    try:
        name = "carto_probe"
        text = bpy.data.texts.get(name) or bpy.data.texts.new(name)
        text.clear()
        text.write(report)
    except Exception as exc:
        print(f"(could not write the carto_probe text datablock: {exc})")
    try:
        bpy.context.window_manager.clipboard = report
        print(
            "[probe] report copied to the clipboard, and written to the "
            "'carto_probe' text datablock (Text Editor). Ctrl+V to paste it."
        )
    except Exception as exc:
        print(f"(could not set the clipboard: {exc})")


if __name__ == "__main__":
    main()
