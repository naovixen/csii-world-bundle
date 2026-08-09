using System.Collections.Generic;
using Unity.Mathematics;

namespace Carto.Domain
{
    /// <summary>
    /// One rigid part of a <see cref="PrefabMesh"/>: a mesh plus its fixed offset within the prefab.
    /// This comes from one entry of the prefab entity's <c>Game.Prefabs.SubMesh</c> buffer. The
    /// transform is <b>still in CS2's left-handed frame</b>; <see cref="Carto.IO.GltfWriter"/>
    /// mirrors it on write, exactly as it does the vertices.
    /// </summary>
    public class PrefabMeshPart
    {
        /// <summary>
        /// The part's geometry.
        /// </summary>
        public MeshAsset Mesh { get; set; }

        /// <summary>
        /// The part's offset from the prefab origin, in metres.
        /// </summary>
        public float3 Position { get; set; }

        /// <summary>
        /// The part's rotation about its own origin.
        /// </summary>
        public quaternion Rotation { get; set; } = quaternion.identity;

        /// <summary>
        /// The glTF node name for this part, unique within the prefab.
        /// </summary>
        public string NodeName { get; set; }

        /// <summary>
        /// The part's per-axis scale, or <c>null</c> for none.
        /// Only the obsolete-content placeholder uses this: the renderer draws that box stretched
        /// to the prefab's <c>ObjectGeometryData.m_Bounds</c>
        /// (<c>BatchDataSystem.UpdateObjectData</c>, the <c>DefaultMissingMesh</c> branch), and the
        /// stretch is a property of the prefab, so it bakes into the part's node.
        /// </summary>
        public float3? Scale { get; set; }

        /// <summary>
        /// Which role this part plays in a stacked prefab, or <c>null</c> for an ordinary part.
        /// CS2 does not store a tall pillar, a high wall or a long fence — it stores a foot, one
        /// repeating section and a cap, and repeats the middle at draw time to span whatever the
        /// instance needs (<c>Game.Objects.Stack.m_Range</c>). A prefab carrying these is therefore
        /// not one mesh but three, and the bundle has to be able to place them independently.
        /// </summary>
        public StackRole? StackRole { get; set; }
    }

    /// <summary>
    /// The three parts of a stacked prefab, mirroring <c>SubMeshFlags.IsStack*</c>.
    /// </summary>
    public enum StackRole
    {
        /// <summary>The foot, drawn once at the bottom.</summary>
        First,

        /// <summary>The repeating section.</summary>
        Middle,

        /// <summary>The cap, drawn once at the top.</summary>
        Last
    }

    /// <summary>
    /// One unique object prefab, assembled from every sub-mesh it draws, ready to be written as the
    /// single <c>&lt;assetId&gt;.glb</c> the bundle contract requires.
    /// <b>The prefab, not the mesh, is the unit of the bundle.</b> <c>instances.jsonl</c> references
    /// <c>NA_ResidentialMediumSignature01</c>, so that is the file name, and the render prefabs
    /// hanging off its <c>SubMesh</c> buffer become nodes inside it (contract §6). Phase 1's spike
    /// wrote one file per mesh entity instead, which is why its output did not join up.
    /// Sub-<i>objects</i> — a building's wings, street trees on a road — are a different thing and
    /// are <b>not</b> here: they are separate entities with their own world transforms and are
    /// emitted as their own instance records (contract §4.1).
    /// </summary>
    public class PrefabMesh
    {
        /// <summary>
        /// The sanitised prefab name, and the bundle's join key. Becomes
        /// <c>meshes/&lt;AssetId&gt;.glb</c>.
        /// </summary>
        public string AssetId { get; set; }

        /// <summary>
        /// The <c>assetId</c> of one part of a stacked prefab.
        /// A pure function of the prefab's id and the role, so that <see cref="Systems.MeshRipSystem"/>
        /// and <see cref="Systems.InstanceRipSystem"/> agree on the name without having to share
        /// state or ordering — the instance pass runs first and never sees the mesh pass's output.
        /// The result stays inside the contract's <c>assetId</c> pattern.
        /// </summary>
        /// <param name="assetId">The prefab's own id.</param>
        /// <param name="role">Which part.</param>
        /// <returns>The part's asset id.</returns>
        public static string StackPartId(string assetId, StackRole role) => role switch
        {
            StackRole.First => $"{assetId}__stack_first",
            StackRole.Middle => $"{assetId}__stack_mid",
            _ => $"{assetId}__stack_last"
        };

        /// <summary>
        /// The <c>assetId</c> of a stacked prefab's parts that carry no stack role, drawn once per
        /// instance rather than per tile. Same purity contract as <see cref="StackPartId"/>.
        /// </summary>
        /// <param name="assetId">The prefab's own id.</param>
        /// <returns>The rest-parts asset id.</returns>
        public static string StackRestId(string assetId) => $"{assetId}__stack_rest";

        /// <summary>
        /// The stack role a <c>SubMesh</c>'s flags declare, or <c>null</c> if it is an ordinary
        /// part.
        /// </summary>
        /// <param name="flags">The sub-mesh flags.</param>
        /// <returns>The role, or <c>null</c>.</returns>
        public static StackRole? RoleOf(Game.Prefabs.SubMeshFlags flags)
        {
            if ((flags & Game.Prefabs.SubMeshFlags.IsStackStart) != 0) return StackRole.First;
            if ((flags & Game.Prefabs.SubMeshFlags.IsStackMiddle) != 0) return StackRole.Middle;
            if ((flags & Game.Prefabs.SubMeshFlags.IsStackEnd) != 0) return StackRole.Last;
            return null;
        }

        /// <summary>
        /// The prefab's original, unsanitised name.
        /// </summary>
        public string SourceName { get; set; }

        /// <summary>
        /// The parts drawn together to make this prefab.
        /// </summary>
        public List<PrefabMeshPart> Parts { get; set; } = new();

        /// <summary>
        /// The distinct stack roles among this prefab's parts, in first-middle-last order, or
        /// empty if it is not a stacked prefab.
        /// </summary>
        /// <returns>The roles present.</returns>
        public IEnumerable<StackRole> StackRoles()
        {
            foreach (StackRole role in new[] { StackRole.First, StackRole.Middle, StackRole.Last })
            {
                foreach (PrefabMeshPart part in Parts)
                {
                    if (part.StackRole == role) { yield return role; break; }
                }
            }
        }

        /// <summary>
        /// How many placed objects in the city use this prefab. Used to rip the prefabs that cover
        /// the most of the city first when the run is capped.
        /// </summary>
        public int InstanceCount { get; set; }

        /// <summary>
        /// The number of sub-mesh entries skipped because they are alternative state variants
        /// (a dead tree, a left-hand-traffic marking) rather than parts drawn together.
        /// </summary>
        public int SkippedVariants { get; set; }

        /// <summary>
        /// The total triangles across every part.
        /// </summary>
        public int TriangleCount
        {
            get
            {
                int total = 0;
                foreach (PrefabMeshPart part in Parts) total += part.Mesh?.TriangleCount ?? 0;
                return total;
            }
        }
    }
}
