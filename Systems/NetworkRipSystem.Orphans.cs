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
    /// An orphan node — a net node with no edge — and the two half caps its composition draws.
    /// </summary>
    public partial class NetworkRipSystem
    {
        /// <summary>
        /// Rip one orphan node — a node no edge connects to — as the two half-disc caps the game
        /// draws for it.
        /// The geometry is <c>CalculateOrphanParameters</c>, decompiled: each half is a quarter
        /// circle of béziers from one side of the node around to its apex, ruled against a
        /// degenerate curve at the node's centre, and the node composition's pieces bend across
        /// that exactly as they bend across a flare. The circle is built in world axes from the
        /// node position and the composition width, which is the game's own construction — the
        /// cap does not turn with the node.
        /// </summary>
        /// <param name="run">The rip in flight.</param>
        /// <param name="entity">The orphan node entity.</param>
        private void RipOrphan(NetworkRun run, Entity entity)
        {
            if (!EntityManager.Exists(entity) || !EntityManager.HasComponent<Game.Net.Orphan>(entity)
                || !EntityManager.HasComponent<Node>(entity))
            {
                run.SkippedStale++;
                return;
            }

            if (EntityManager.HasComponent<Game.Net.Marker>(entity))
            {
                run.SkippedMarker++;
                return;
            }

            Entity composition = EntityManager.GetComponentData<Game.Net.Orphan>(entity).m_Composition;
            if ((composition == Entity.Null) || !EntityManager.HasComponent<NetCompositionData>(composition))
            {
                run.SkippedDegenerate++;
                return;
            }

            float width = EntityManager.GetComponentData<NetCompositionData>(composition).m_Width;
            if (!(width > 0f))
            {
                run.SkippedNoWidth++;
                return;
            }

            float3 position = EntityManager.GetComponentData<Node>(entity).m_Position;

            MeshAssembly assembly = new();

            AppendOrphanHalf(composition, position, width, true, run.PieceCache, assembly);
            AppendOrphanHalf(composition, position, width, false, run.PieceCache, assembly);

            string assetId = $"orphan_{entity.Index}";
            MeshAsset assembled = Finish(assetId, assembly);
            if (assembled == null)
            {
                run.SkippedDegenerate++;
                return;
            }

            Enqueue(run, new NetworkItem { Mesh = assembled });
            run.Triangles += assembled.TriangleCount;
            _orphanCaps++;

            Entity prefab = EntityManager.GetComponentData<PrefabRef>(entity).m_Prefab;
            NetworkRecord record = new()
            {
                Id = entity.Index,
                Kind = "segment",
                AssetId = assetId,
                Position = position,
                Rotation = quaternion.identity
            };

            string asset = Instance.Name.GetPrefabName(prefab);
            if (!string.IsNullOrEmpty(asset)) record.Attributes["asset"] = asset;
            record.Attributes["category"] = GetCategory(prefab, composition);
            record.Attributes["form"] = "normal";
            record.Attributes["width"] = Math.Round((double)width, 3);

            run.Writer.Write(record);

            string category = record.Attributes["category"].ToString();
            run.ByCategory.TryGetValue(category, out int categoryCount);
            run.ByCategory[category] = categoryCount + 1;
            run.ByForm.TryGetValue("normal", out int formCount);
            run.ByForm["normal"] = formCount + 1;
        }

        /// <summary>
        /// One half of an orphan cap, built from the game's own synthetic segments.
        /// </summary>
        /// <param name="composition">The orphan's composition.</param>
        /// <param name="position">The node position.</param>
        /// <param name="width">The composition width.</param>
        /// <param name="primary">Whether this is the +z half.</param>
        /// <param name="cache">The piece mesh cache.</param>
        /// <param name="assembly">The shared arrays.</param>
        private void AppendOrphanHalf(Entity composition, float3 position, float width, bool primary,
                                      Dictionary<Entity, MeshAsset> cache, MeshAssembly assembly)
        {
            // The game's own `CalculateOrphanParameters`, ported in NetworkRipSystem.GameMesh.cs
            // — the four-cubic circle segments, corner rails and mapping tables come from there.
            //
            if ((composition == Entity.Null) || !EntityManager.HasComponent<NetCompositionData>(composition)) return;

            NetCompositionData data = EntityManager.GetComponentData<NetCompositionData>(composition);
            CompositionCurves curves = OrphanCurves(position, data, primary);
            BuildGameComposition(composition, curves, position, cache, assembly);
        }
    }
}
