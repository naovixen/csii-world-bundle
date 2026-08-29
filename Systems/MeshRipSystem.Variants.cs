using Carto.Domain;
using Carto.IO;
using Carto.Utils;
using Colossal.Logging;
using Game;
using Game.Objects;
using Game.Prefabs;
using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Diagnostics;
using System.Text;
using System.Threading.Tasks;
using Unity.Collections;
using Unity.Entities;
using Unity.Mathematics;

namespace Carto.Systems
{
    /// <summary>
    /// The state variants a prefab ships, and which sub-meshes each one selects.
    /// </summary>
    /// <remarks>
    /// A `.glb` is ONE STATE of a prefab (§6.1/§9). A tree's `__elderly`, a stockpile's
    /// `__partial2` — each is its own file, and a variant file exists only where the prefab
    /// flags that state.
    /// </remarks>
    public partial class MeshRipSystem
    {
        /// <summary>
        /// Whether one sub-mesh entry belongs in the exported state.
        /// Public and static so <see cref="InstanceRipSystem"/> can apply the identical test when
        /// deciding whether a stacked prefab has untiled rest parts — the two layers must agree on
        /// which entries exist or the <c>__stack_rest</c> reference dangles.
        /// </summary>
        /// <param name="flags">The entry's flags.</param>
        /// <returns>Whether the entry is exported.</returns>
        public static bool ExportsSubMesh(SubMeshFlags flags) => ExportsSubMesh(flags, DefaultState);

        /// <summary>
        /// Whether one sub-mesh entry belongs in a given exported state.
        /// Mirrors the renderer's own test — <c>BatchInstanceSystem.BatchInstanceJob</c> line 522
        /// keeps an entry when <c>(subMesh.m_Flags &amp; state) == subMesh.m_Flags</c>, i.e. the
        /// state must satisfy every bit the entry asks for.
        /// </summary>
        /// <param name="flags">The entry's flags.</param>
        /// <param name="state">The state being exported.</param>
        /// <returns>Whether the entry is exported.</returns>
        private static bool ExportsSubMesh(SubMeshFlags flags, SubMeshFlags state)
            => ((flags & SubMeshFlags.OutlineOnly) == 0) && (((flags & RequireFlags) & ~state) == 0);

        /// <summary>
        /// One alternative state exported as a derived file alongside the prefab's own.
        /// The renderer picks a tree's silhouette from <c>Tree.m_State</c> and a stockpile's from
        /// <c>Quantity.m_Fullness</c> (<c>BatchDataHelpers.CalculateTreeSubMeshData</c> /
        /// <c>CalculateQuantitySubMeshData</c>), and a prefab that authors those alternatives loses
        /// them entirely when only <see cref="DefaultState"/> ships — every dead tree in the city
        /// draws in full leaf. Each variant is a <b>complete replacement</b> for the base file, not
        /// an overlay: its selection also picks up every unconditional part the prefab draws
        /// alongside the swapped one.
        /// </summary>
        private struct StateVariant
        {
            /// <summary>The suffix the derived <c>assetId</c> takes.</summary>
            public string Suffix;

            /// <summary>The flag an entry must carry for this variant to be authored at all.</summary>
            public SubMeshFlags Required;

            /// <summary>The default-state flag this variant stands in for, cleared from the state.</summary>
            public SubMeshFlags Replaces;

            /// <summary>Whether this is a tree state rather than a stockpile fullness step.</summary>
            public bool Tree;

            /// <summary>The state a sub-mesh entry is tested against for this variant.</summary>
            public SubMeshFlags State => (DefaultState & ~Replaces) | Required;
        }

        /// <summary>
        /// Every alternative state shipped as a derived file.
        /// The states the base file already covers are absent by construction: a healthy adult tree
        /// and an empty stockpile <i>are</i> <see cref="DefaultState"/>, so they need no suffix.
        /// <para>
        /// All four non-adult tree states ship (audit §5.1, P18). <c>Teen</c> and <c>Elderly</c>
        /// were left out when <c>__dead</c>/<c>__stump</c> were added, on the reasoning that the
        /// growth steps could wait — but <c>CalculateTreeSubMeshData</c> returns a distinct
        /// sub-mesh requirement for every one of the five states, and the two that were skipped are
        /// the two the city is full of: 119,237 Elderly and 33,581 Teen against 1,173 Dead and no
        /// Stumps at all, so 59.5% of stated trees were drawing in a silhouette the game would not
        /// draw. <c>RequireChild</c> is deliberately still absent, and is not a fourth row: the
        /// game's own switch has no Child case — a child tree is a <c>Teen</c> entry below growth
        /// 128, scaled by <c>m_ChildSize / m_TeenSize</c>, which the per-instance
        /// <c>growthScale</c> already carries.
        /// </para>
        /// </summary>
        private static readonly StateVariant[] StateVariants =
        {
            new() { Suffix = "__teen", Required = SubMeshFlags.RequireTeen, Replaces = SubMeshFlags.RequireAdult, Tree = true },
            new() { Suffix = "__elderly", Required = SubMeshFlags.RequireElderly, Replaces = SubMeshFlags.RequireAdult, Tree = true },
            new() { Suffix = "__dead", Required = SubMeshFlags.RequireDead, Replaces = SubMeshFlags.RequireAdult, Tree = true },
            new() { Suffix = "__stump", Required = SubMeshFlags.RequireStump, Replaces = SubMeshFlags.RequireAdult, Tree = true },
            new() { Suffix = "__partial1", Required = SubMeshFlags.RequirePartial1, Replaces = SubMeshFlags.RequireEmpty },
            new() { Suffix = "__partial2", Required = SubMeshFlags.RequirePartial2, Replaces = SubMeshFlags.RequireEmpty },
            new() { Suffix = "__full", Required = SubMeshFlags.RequireFull, Replaces = SubMeshFlags.RequireEmpty }
        };

        /// <summary>
        /// Choose which sub-mesh entries belong in the exported file.
        /// </summary>
        /// <param name="subMeshes">The prefab's sub-mesh entries.</param>
        /// <returns>The indices to export.</returns>
        private List<int> SelectSubMeshes(SubMesh[] subMeshes) => SelectSubMeshes(subMeshes, DefaultState, true);

        /// <summary>
        /// Choose which sub-mesh entries belong in one exported state.
        /// </summary>
        /// <param name="subMeshes">The prefab's sub-mesh entries.</param>
        /// <param name="state">The state being exported.</param>
        /// <param name="fallback">Whether an empty selection falls back to the first entry — right for the prefab's own file, wrong for a derived one, where an empty selection simply means the variant does not exist.</param>
        /// <returns>The indices to export.</returns>
        private List<int> SelectSubMeshes(SubMesh[] subMeshes, SubMeshFlags state, bool fallback)
        {
            List<int> selected = new();

            for (int i = 0; i < subMeshes.Length; i++)
            {
                // An `OutlineOnly` entry is never world geometry. The game clears every layer but
                // `Outline` for it (`BatchInstanceJob.UpdateObjectInstances` does
                // `layers &= ~Outline; if (OutlineOnly) layers &= Outline;`), so outside a tool's
                // selection highlight its layer mask is empty and it draws nothing at all. It is
                // the silhouette shell a highlight is drawn from, and exporting it buries the real
                // mesh inside a second copy of itself.
                //
                // Unconditional entries are always drawn; conditional ones only when everything
                // they ask for holds in the exported state.
                //
                if (!ExportsSubMesh(subMeshes[i].m_Flags, state)) continue;

                // The game replaces a part's batch layer with its `m_DefaultLayers` when set
                // (`UpdateObjectInstances`), so a part defaulting only to Pipeline/SubPipeline —
                // utility hookup furniture inside an otherwise visible prefab — never draws in
                // the world view. The per-part form of the §17.5 rule, which drops whole
                // instances when *every* part is such.
                //
                if (EntityManager.HasComponent<MeshData>(subMeshes[i].m_SubMesh))
                {
                    const MeshLayer underground = MeshLayer.Pipeline | MeshLayer.SubPipeline;
                    MeshLayer layers = EntityManager.GetComponentData<MeshData>(subMeshes[i].m_SubMesh).m_DefaultLayers;
                    if ((layers != 0) && ((layers & ~underground) == 0))
                    {
                        PipelinePartsSkipped++;
                        continue;
                    }
                }

                selected.Add(i);
            }

            // A prefab whose every entry is conditional — some props exist only as state variants —
            // would otherwise produce no file at all, breaking the contract's promise that every
            // referenced assetId resolves. Fall back to its first entry.
            //
            // The fallback skips outline shells for the same reason the loop does: falling back to
            // one would hand the bundle a silhouette in place of the object.
            //
            if (fallback && (selected.Count == 0))
            {
                for (int i = 0; i < subMeshes.Length; i++)
                {
                    if ((subMeshes[i].m_Flags & SubMeshFlags.OutlineOnly) != 0) continue;
                    selected.Add(i);
                    break;
                }
            }

            return selected;
        }
    }
}
