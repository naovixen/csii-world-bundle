"""The Navigation section: the minimap, routes and fast travel.

What the game's own systems read rather than what you see.
"""

from .model import Setting

NAVIGATION_SETTINGS = (
    Setting(
        key="ai_roads",
        default=True,
        kind="bool",
        label="Minimap and routing",
        blurb="Lets the minimap, route markers and traffic use the roads. Off leaves the map blank.",
        help="The invisible road network the AI, the minimap and route-finding use. Off = no "
        "minimap and no routes",
        detail="The road network the game's own systems drive on: the minimap, the route line to "
        "a waypoint, the arrows on the ground and the traffic all read it and nothing "
        "else. With it off the minimap is blank, a big-map waypoint produces no route, and "
        "nothing but you uses the roads. It is invisible either way — it is not what you "
        "see, it is what the game knows.",
    ),
    Setting(
        key="navgraph_taxiways",
        default=False,
        kind="bool",
        label="Include airport taxiways",
        blurb="Let routes run across the apron and the taxiways, slowly.",
        help="Let routes run across the apron and the taxiways too, slowly",
        detail="Puts airport taxiways into the road network too, at a low speed so routes prefer "
        "real roads. Roads and highways are always in it; footpaths, trams, rails, "
        "waterways, cables and pipes never are.",
        subset_of="ai_roads",
    ),
    Setting(
        key="pois",
        default=True,
        kind="bool",
        label="Fast-travel points",
        blurb="Places you can jump straight to from the big map.",
        help="Fast-travel points on the big map, derived from the densest road cells",
        detail="The places you can jump straight to from the big map. Yours if you have named "
        "any, otherwise the busiest parts of your road network, named after the street "
        "they are on.",
    ),
    Setting(
        key="poi_file",
        default=None,
        kind="path",
        label="Your own fast-travel points",
        blurb="Places you name and put on the map yourself, instead of the ones picked for you.",
        help="The file holding the fast-travel points you named, instead of the ones picked for you",
        detail="The file the fast-travel points you place are kept in. The app writes it for you "
        "as you place them on the map; the setting is still a path, so you can point it at "
        "a list of your own or share one. Each point is put onto the nearest road wherever "
        "it started.",
        control="poi",
        needs="pois",
    ),
    Setting(
        key="poi_max",
        default=8,
        kind="int",
        label="How many to suggest",
        blurb="How many busy spots to pick out for you when you have named none of your own.",
        help="How many busy places to pick out for you when you have named fewer of your own",
        detail="How many busy places to pick out for you when you have named fewer of your own. "
        "The levels BeamNG ships carry between three and nine; a big map with forty "
        "markers on it is an index rather than a menu.",
        unit="points",
        subset_of="pois",
    ),
)
