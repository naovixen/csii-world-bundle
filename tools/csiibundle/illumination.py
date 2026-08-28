"""The illumination the grading sits on — `grading.json`'s `lighting` block.

Everything in `csiibundle.colour` is a transform applied to radiance. This is where the
radiance comes from, and it is the half a consumer is most likely to invent instead of read: a
consumer that reproduces the albedo and the grading but lights the scene white still misses the
game's colour — the bundle's own semantics string says exactly that, and the reference city's
sun is 110,000 lux at 66 degrees, not a default tilt.
"""

import math


class DirectionalLight:
    """The sun or the moon, from `grading.json`'s `lighting` block.

    **`direction` is where the light GOES, not where it is** — the unit vector it travels along,
    in the bundle's left-handed Y-up frame. Negate it to point at the light. Getting this
    backwards puts the sun underground, which reads as "the import is too dark" rather than as a
    sign error.

    **The effective colour is a PRODUCT of two terms.** `color` is the light's authored filter;
    when `use_color_temperature` is set, HDRP multiplies it by the blackbody colour of
    `temperature` Kelvin. Both are needed. On the reference city the flag is off and the filter
    is white, which is why the warm cast there comes from the grading rather than from the light
    — but that is a property of one save, not of the format.

    `intensity` is **lux** for a directional light. `initial_intensity` is the authored value
    before the game scales it down toward the horizon (`PlanetarySystem` multiplies it by a
    smoothstep on the sun's height), so `intensity` is what was actually lighting the city at rip
    time and is the one to use.
    """

    def __init__(self, block, which):
        block = block or {}
        self.which = which
        self.raw = block
        self.enabled = bool(block.get("enabled", False))
        self.color = tuple(float(c) for c in (block.get("color") or (1.0, 1.0, 1.0))[:3])
        self.use_color_temperature = bool(block.get("useColorTemperature", False))
        self.temperature = float(block.get("colorTemperature", 6500.0) or 6500.0)
        self.intensity = float(block.get("intensity", 0.0) or 0.0)
        self.initial_intensity = float(block.get("initialIntensity", self.intensity) or 0.0)
        self.intensity_units = str(block.get("intensityUnits", "") or "")
        travel = block.get("direction")
        self.travel = tuple(float(v) for v in travel[:3]) if travel and len(travel) >= 3 else None
        self.elevation_degrees = block.get("elevationDegrees")
        self.azimuth_degrees = block.get("azimuthDegrees")
        self.shadows = str(block.get("shadows", "") or "")

    @property
    def toward(self):
        """The unit vector pointing AT the light, bundle frame, or `None`.

        `None` also when the light is at or below the horizon — a night rip lights nothing, and
        a consumer placing a sun there gets a scene lit from underneath.
        """
        if not self.travel:
            return None
        x, y, z = (-v for v in self.travel)
        length = math.sqrt(x * x + y * y + z * z)
        if length < 1e-6 or y <= 0.0:
            return None
        return (x / length, y / length, z / length)

    def __repr__(self):
        return (
            f"<{self.which} {'on' if self.enabled else 'off'} "
            f"{self.intensity:g} {self.intensity_units.split()[0] if self.intensity_units else ''} "
            f"elev {self.elevation_degrees}>"
        )


class Sky:
    """HDRP's PhysicallyBasedSky — the AMBIENT half, and what fills every shadow.

    `exposure` is EV on the sky's own contribution and the three tints multiply it. A consumer
    that lights only with the sun gets black shadows where the game has blue-grey ones, which is
    the second most common reason a correct import looks wrong (the first being white light).
    """

    def __init__(self, block):
        block = block or {}
        self.raw = block
        self.exposure = float(block.get("exposure", 0.0) or 0.0)
        self.zenith_tint = tuple(float(c) for c in (block.get("zenithTint") or (1, 1, 1))[:3])
        self.horizon_tint = tuple(float(c) for c in (block.get("horizonTint") or (1, 1, 1))[:3])
        self.ground_tint = tuple(float(c) for c in (block.get("groundTint") or (1, 1, 1))[:3])


class Lighting:
    """`grading.json`'s `lighting` block: sun, moon, sky, indirect multipliers and the clock.

    `time` matters more than it looks: the sun's angle and colour follow the in-game clock and
    the map's latitude/longitude, so a reference screenshot taken at another hour is not
    describing this grading and cannot be used to check it.
    """

    def __init__(self, block):
        block = block or {}
        self.raw = block
        self.sun = DirectionalLight(block.get("sun"), "sun")
        self.moon = DirectionalLight(block.get("moon"), "moon")
        self.sky = Sky(block.get("sky"))
        indirect = block.get("indirect") or {}
        self.indirect_diffuse = float(indirect.get("diffuseMultiplier", 1.0) or 1.0)
        self.indirect_reflection = float(indirect.get("reflectionMultiplier", 1.0) or 1.0)
        self.time = block.get("time") or {}

    @property
    def key_light(self):
        """Whichever of sun/moon is actually lighting the scene, or `None` for a dark rip."""
        for light in (self.sun, self.moon):
            if light.enabled and light.intensity > 0.0 and light.toward is not None:
                return light
        return None

    def present(self):
        """Whether the bundle carried a lighting block at all (older rips did not)."""
        return bool(self.raw)
