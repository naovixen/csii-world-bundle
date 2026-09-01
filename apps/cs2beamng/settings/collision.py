"""The Collision section: what you can hit.

A preset over seven per-kind switches plus a footprint floor. Every one of these trades driving
against build cost and residency, so each says what it costs.
"""

from .model import Setting

COLLISION_SETTINGS = (
    Setting(
        key="collision_preset",
        default="simplified",
        kind="choice:exact|simplified|off",
        label="Detail",
        blurb="Exact shapes, simple boxes, or nothing solid at all. Boxes are far lighter.",
        help="Collision detail",
        control="segmented",
        parent_off="off",
    ),
    Setting(
        key="collision_buildings",
        default=True,
        kind="bool",
        label="Buildings",
        blurb="Cars stop at the walls instead of driving through them.",
        help="Buildings collide",
        subset_of="collision_preset",
    ),
    Setting(
        key="collision_props",
        default=True,
        kind="bool",
        label="Props",
        blurb="Benches, bins and signs are solid.",
        help="Props collide",
        subset_of="collision_preset",
    ),
    Setting(
        key="collision_trees",
        default=True,
        kind="bool",
        label="Trees",
        blurb="Trunks stop a car. The leaves never do.",
        help="Trees collide (trunk)",
        subset_of="collision_preset",
    ),
    Setting(
        key="collision_furniture",
        default=True,
        kind="bool",
        label="Fences and roadside detail",
        blurb="Barriers, hedges and cable poles stop a car.",
        help="Fences and lane furniture collide",
        subset_of="collision_preset",
    ),
    Setting(
        key="collision_skirts",
        default=True,
        kind="bool",
        label="Foundations",
        blurb="The strip under a plot is solid, so you cannot drive into the gap.",
        help="Foundation skirts collide",
        subset_of="collision_preset",
    ),
    Setting(
        key="collision_paint",
        default=False,
        kind="bool",
        label="Markings and painted ground",
        blurb="Paint is solid too. Usually a mistake: it sits just above the road surface.",
        help="Markings and painted surfaces collide (not recommended)",
        subset_of="collision_preset",
    ),
    # A8/P4: below this horizontal footprint a forest type ships collidable:false whatever its
    # height. See `stages.instances.FOREST_COLLIDABLE_MIN_FOOTPRINT`.
    Setting(
        key="collision_min_footprint",
        default=0.8,
        kind="float",
        label="Ignore objects narrower than",
        blurb="Anything thinner than this across is driven straight through.",
        help="Props narrower than this (metres) do not collide",
        unit="m",
        subset_of="collision_preset",
    ),
    Setting(
        key="collision_max_clearance_m",
        # DEFAULT OFF (Naomi, 2026-09-01): the rule ships, but the build that measures the shadow
        # far stop measures only that. 2.5 is the number to turn it on at - above a door head,
        # below a first-floor slab.
        default=0.0,
        kind="float",
        label="Ignore objects higher than",
        blurb="A prop this far above the ground loses its collider and its shadow. 0 = off.",
        help="Props whose base is this many metres above the terrain do not collide or cast "
        "shadows; 0 = off",
        detail="A planter on a roof, a light on a balcony, a sign under a bridge deck: none of "
        "them can ever be driven into, and the shadow each one casts lands on the roof it "
        "stands on. Both cost something every frame, so anything standing clear of the "
        "ground by more than this gives up both. Buildings are exempt, and so is the road "
        "surface itself — this only ever removes a prop's collider, never the ground under "
        "the car. The measurement is against the TERRAIN, so a prop on a high viaduct "
        "counts as elevated too. 0 turns it off.",
        unit="m",
        subset_of="collision_preset",
    ),
    Setting(
        key="collision_lights",
        default=False,
        kind="bool",
        label="Lampposts",
        blurb="A pole stops a car dead. Off by default because it is the thing you hit most.",
        help="Street lights and every other light-bearing prop collide. Off: a 10 m pole stops a "
        "car dead",
        detail="Lampposts, floodlights and every other prop that carries a light become solid. It "
        "is off by default because a lamppost is thin enough to slip past the "
        "narrow-object rule, and a 10 m pole that stops a car dead is the one piece of "
        "street furniture you hit most. Which props count is the game's own answer: the "
        "ones your city gives a real light to.",
        subset_of="collision_preset",
    ),
)
