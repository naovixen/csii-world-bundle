using System.Collections.Generic;
using Unity.Mathematics;

namespace Carto.Domain
{
    /// <summary>
    /// One lane of the drivable, walkable or railed network.
    /// A bundle's <c>networks/</c> layer is the road <i>surface</i> — where the asphalt is. This is
    /// the road <i>graph</i> — where a vehicle may go, in which direction, at what speed. They are
    /// different questions and a level needs both: the surface makes a city look right, the graph
    /// makes it drivable. CS2 keeps the graph as ordinary ECS data with no readback risk, so the
    /// only reason it was missing is that nothing had asked for it.
    /// </summary>
    public class LaneRecord
    {
        /// <summary>The lane entity's index, unique in the bundle.</summary>
        public int Id { get; set; }

        /// <summary>
        /// The edge or node that owns this lane, matching a <c>networks.jsonl</c> <c>id</c> where
        /// the owner is an edge.
        /// </summary>
        public int? Owner { get; set; }

        /// <summary>
        /// What may use it: <c>car</c>, <c>track</c>, <c>pedestrian</c>, <c>parking</c>,
        /// <c>connection</c> or <c>other</c>.
        /// </summary>
        public string Kind { get; set; }

        /// <summary>
        /// The lane's path, world space, already tessellated from CS2's bézier.
        /// </summary>
        public List<float3> Path { get; set; } = new();

        /// <summary>The curve's length in metres.</summary>
        public float Length { get; set; }

        /// <summary>Speed limit in km/h, where the lane type has one.</summary>
        public float? SpeedLimit { get; set; }

        /// <summary>
        /// How much the lane bends, as CS2 scores it — its own measure, carried rather than
        /// recomputed.
        /// </summary>
        public float? Curviness { get; set; }

        /// <summary>
        /// Whether this is a secondary lane — one CS2 derives from a master lane rather than
        /// placing in its own right.
        /// </summary>
        public bool Secondary { get; set; }

        /// <summary>
        /// The lane-type flags, as names. Open: a consumer ignores what it does not know.
        /// </summary>
        public List<string> Flags { get; set; } = new();
    }
}
