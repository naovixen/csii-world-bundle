using Carto.Domain;
using Carto.IO;
using Carto.Utils;
using Colossal.Logging;
using Colossal.Mathematics;
using Game;
using Game.Net;
using Game.Prefabs;
using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Text;
using System.Threading.Tasks;
using Unity.Collections;
using Unity.Entities;
using Unity.Mathematics;

namespace Carto.Systems
{
    /// <summary>
    /// The §10 gaps this stage declares, and how a network asset is named in one.
    /// </summary>
    public partial class NetworkRipSystem
    {
        /// <summary>
        /// Declare this stage's gaps in the bundle's <c>warnings.jsonl</c> (contract §10) — the
        /// lane-part meshes that would not read, the surfaces and textures that produced no
        /// material entry, and a heightmap that could not be captured. They reached the report and
        /// stopped there until now (infra F5), so a bundle's own record of what the network stage
        /// lost was empty however much it lost.
        /// The ownership-merge scheme of the other passes, with kinds of this stage's own: the
        /// instance pass owns <c>prefab</c>/<c>instance</c>, the mesh pass
        /// <c>mesh</c>/<c>material</c>/<c>texture</c>, the lane pass <c>lane</c>, and every kind
        /// this pass writes is prefixed <c>network</c> — a network run replaces exactly those and
        /// keeps every other line, so the stages can run in any order without erasing each other.
        /// The prefix matters: a network piece's mesh warning is about a cross-section piece, which
        /// is not an <c>assetId</c> in <c>meshes/</c> at all, so filing it as <c>mesh</c> would
        /// both mislead a consumer and let the next mesh rip delete it.
        /// </summary>
        /// <param name="run">The finished run.</param>
        /// <param name="report">The report to note the outcome in.</param>
        private void WriteWarnings(NetworkRun run, StringBuilder report)
        {
            int kept = WarningsFile.Rewrite(
                run.OutputDirectory,
                line => WarningsFile.IsKindPrefixed(line, WarningsFile.NetworkKindPrefix),
                NetworkDeclarations(run));
            if (kept < 0) return;

            int written = run.Skipped.Count + ((_unconformedPieces > 0) ? 1 : 0);
            report.AppendLine($"Recorded {written} gap(s) in {WarningsFile.FileName}, "
                            + $"keeping {kept} from the other passes");
        }

        /// <summary>
        /// This stage's §10 declarations, in write order.
        /// </summary>
        /// <param name="run">The finished run.</param>
        /// <returns>One record per gap.</returns>
        private IEnumerable<Dictionary<string, object>> NetworkDeclarations(NetworkRun run)
        {
            foreach (RipWarning skipped in run.Skipped)
            {
                yield return WarningsFile.ByAsset(NetworkKind(skipped.Kind), skipped.AssetId,
                                                  skipped.Reason);
            }

            // N-F2: pieces the game conforms to the terrain but this rip could not, because
            // the heightmap would not come back. They ship as authored — a trench wall
            // standing on nothing, a portal face too short to close its mouth — and the
            // bundle now says so instead of claiming they were conformed.
            //
            if (_unconformedPieces > 0)
            {
                yield return WarningsFile.ByAsset("networkConform", "networks/",
                    $"terrain heightmap unavailable; {_unconformedPieces} LowerBottomToTerrain/"
                    + "RaiseTopToTerrain piece placement(s) shipped unconformed");
            }
        }

        /// <summary>
        /// This stage's kind for a warning raised by a shared reader — see
        /// <see cref="WriteWarnings"/> for why they are not filed under the mesh pass's kinds.
        /// </summary>
        /// <param name="kind">The kind the reader raised it under.</param>
        /// <returns>The network stage's equivalent.</returns>
        private static string NetworkKind(string kind) => kind switch
        {
            "mesh" => "networkMesh",
            "material" => "networkMaterial",
            "texture" => "networkTexture",
            _ => "network"
        };

        // ====================================================================================
        // Lane-drawn geometry — hanging cables, fences, hedges (bundle layer `lanemeshes/`).
        //
        // ====================================================================================

        /// <summary>
        /// Record one lane's drawn geometry, if its prefab draws any, as a parameter record in
        /// <c>lanemeshes.jsonl</c> referencing the prefab's straight part meshes (contract §5B
        /// v2). The consumer bends; the algorithm is the contract's, reference-implemented in the
        /// Blender harness.
        /// The selection reproduces <c>BatchInstanceSystem.UpdateLaneInstances</c>: the sub-mesh
        /// test is the <b>inverted</b> one recorded in audit §12 — the accumulated flags are an
        /// exclusion mask, <c>(flags &amp; state) != 0 → skip</c> — with the per-lane bits (unsafe,
        /// level crossing, switch) read off the lane's own components. The record carries what
        /// <c>BatchDataSystem.UpdateLaneData</c> hands the vertex shader: the bézier's control
        /// points, the <c>CutRange</c> spans, the <c>NodeLane.m_WidthOffset</c> width scale, and
        /// the <c>HangingLane.m_Distances</c> end drops (the prefab-level
        /// <c>UtilityLaneData.m_Hanging</c> factor lives in the catalogue).
        /// Deliberate exclusions, each counted: tunnel-owned lanes here; <c>Pipeline</c>-layer and
        /// decal-flagged parts at the catalogue (they are properties of the part, not the lane).
        /// </summary>
        /// <summary>
        /// Whether a lane's owning net entity is underground — the game's
        /// <c>BatchInstanceSystem.IsNetOwnerTunnel</c>: the owner's own <c>Elevation</c>, or, for
        /// node owners (which carry none), the elevation of any edge connected to the node.
        /// </summary>
        private bool IsNetOwnerTunnel(Entity owner)
        {
            if (EntityManager.HasComponent<Game.Net.Elevation>(owner)
                && (math.cmin(EntityManager.GetComponentData<Game.Net.Elevation>(owner).m_Elevation) < 0f))
                return true;

            if (EntityManager.HasBuffer<Game.Net.ConnectedEdge>(owner))
            {
                DynamicBuffer<Game.Net.ConnectedEdge> edges = EntityManager.GetBuffer<Game.Net.ConnectedEdge>(owner, true);
                for (int i = 0; i < edges.Length; i++)
                {
                    Entity edge = edges[i].m_Edge;
                    if (EntityManager.HasComponent<Game.Net.Elevation>(edge)
                        && (math.cmin(EntityManager.GetComponentData<Game.Net.Elevation>(edge).m_Elevation) < 0f))
                        return true;
                }
            }

            return false;
        }
    }
}
