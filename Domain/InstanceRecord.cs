using System.Collections.Generic;
using Unity.Mathematics;

namespace Carto.Domain
{
    /// <summary>
    /// One placed object in the world — a single line of the bundle's <c>instances.jsonl</c>.
    /// Positions and rotations are raw CS2 world values: left-handed, Y-up, X east and Z north,
    /// in metres. The bundle contract deliberately keeps these unconverted so that a position here
    /// equals the position the game itself reports, which is what makes spot-checking possible.
    /// Only mesh payloads are converted, and only on write.
    /// </summary>
    public class InstanceRecord
    {
        /// <summary>
        /// The producer-local unique id, taken from the ECS entity index.
        /// </summary>
        public int Id { get; set; }

        /// <summary>
        /// The <see cref="Id"/> of the owning instance, when this object came from an owner's
        /// sub-object buffer. <c>null</c> for free-standing objects.
        /// A consumer may ignore this entirely: <see cref="Position"/> and <see cref="Rotation"/>
        /// are world-space regardless, so parentage is reassembly information, not placement
        /// information.
        /// </summary>
        public int? Parent { get; set; }

        /// <summary>
        /// The sanitised prefab name, resolving to <c>meshes/&lt;AssetId&gt;.glb</c>.
        /// </summary>
        public string AssetId { get; set; }

        /// <summary>
        /// The contract's coarse class: <c>building</c>, <c>prop</c>, <c>tree</c>, <c>fence</c>,
        /// <c>decal</c> or <c>poi</c>.
        /// </summary>
        public string Kind { get; set; }

        /// <summary>
        /// The world position in metres.
        /// </summary>
        public float3 Position { get; set; }

        /// <summary>
        /// The world rotation as a unit quaternion.
        /// </summary>
        public quaternion Rotation { get; set; }

        /// <summary>
        /// Per-axis scale, or <c>null</c> when the object is at its authored size.
        /// Contract §4 makes <c>scale</c> optional and an absent value means <c>[1,1,1]</c>, so it
        /// is written only where it carries information. The one producer that sets it is the stack
        /// expansion, where CS2 stretches a repeating section along one axis to make the sections
        /// span exactly the gap the instance needs.
        /// </summary>
        public float3? Scale { get; set; }

        /// <summary>
        /// Semantic attributes. Consumers ignore keys they do not recognise.
        /// </summary>
        public Dictionary<string, object> Attributes { get; set; } = new();
    }
}
