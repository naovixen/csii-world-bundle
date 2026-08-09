using System.Collections.Generic;
using Unity.Mathematics;

namespace Carto.Domain
{
    /// <summary>
    /// One network segment — a single line of the bundle's <c>networks.jsonl</c>.
    /// The contract allows two geometry forms, a ripped mesh or a centerline the consumer extrudes.
    /// This producer writes the centerline form, because CS2 has no per-segment road mesh to rip:
    /// it draws a road by deforming a straight composition mesh along the edge's curve in the
    /// vertex shader. See <see cref="Carto.Systems.NetworkRipSystem"/> for the whole argument.
    /// Coordinates are raw CS2 world values, exactly as in <see cref="InstanceRecord"/>:
    /// left-handed, Y-up, X east and Z north, in metres.
    /// </summary>
    public class NetworkRecord
    {
        /// <summary>
        /// The producer-local unique id, taken from the ECS entity index.
        /// </summary>
        public int Id { get; set; }

        /// <summary>
        /// The contract's <c>kind</c>: <c>segment</c> or <c>node</c>.
        /// </summary>
        public string Kind { get; set; } = "segment";

        /// <summary>
        /// Mesh form only. The id of the assembled mesh under the bundle's network mesh layer.
        /// When this is set the record carries no <see cref="Centerline"/>, which the schema
        /// enforces: a record is one form or the other, never both.
        /// </summary>
        public string AssetId { get; set; }

        /// <summary>
        /// Mesh form only. Where the mesh sits in the world, in metres.
        /// </summary>
        public float3 Position { get; set; }

        /// <summary>
        /// Mesh form only. The mesh's world rotation.
        /// Always identity: a network mesh is assembled in world space and then shifted to its
        /// own origin, so there is no orientation left to express.
        /// </summary>
        public quaternion Rotation { get; set; } = quaternion.identity;

        /// <summary>
        /// Centerline form only. The world-space polyline from the start node to the end node,
        /// already tessellated.
        /// </summary>
        public List<float3> Centerline { get; set; } = new();

        /// <summary>
        /// The total width in metres, to be extruded symmetrically about the centerline.
        /// </summary>
        public float Width { get; set; }

        /// <summary>
        /// Semantic attributes. Consumers ignore keys they do not recognise.
        /// </summary>
        public Dictionary<string, object> Attributes { get; set; } = new();
    }
}
