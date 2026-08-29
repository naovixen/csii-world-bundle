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
    /// The §5B part catalogue: what each lane prefab is made of, and which of its parts are
    /// decals.
    /// </summary>
    /// <remarks>
    /// <c>decalRole</c> is what settles a decal part's lift. A part with no role takes the wear
    /// tint's lower lift and buries a marking, which is the exact failure the field was added to
    /// end — so the classification is here, once, rather than guessed per consumer.
    /// </remarks>
    public partial class NetworkRipSystem
    {
        /// <summary>
        /// Read a lane prefab's drawable parts once, through the same reader every building goes
        /// through, so their surfaces land in the same material table — write each distinct part
        /// mesh into <c>lanemeshes/</c> exactly once, and add the prefab's entry to the
        /// <c>laneprefabs.json</c> catalogue.
        /// </summary>
        /// <param name="run">The rip in flight.</param>
        /// <param name="prefab">The lane prefab entity.</param>
        /// <returns>The entry; its parts are empty when the prefab draws nothing.</returns>
        private LanePrefabEntry GetLanePrefab(NetworkRun run, Entity prefab)
        {
            if (run.LaneParts.TryGetValue(prefab, out LanePrefabEntry cached)) return cached;

            LanePrefabEntry entry = new();

            // Category and the hanging factor are prefab properties (`UtilityLaneData`), so they
            // live in the catalogue rather than in every record.
            //
            if (EntityManager.HasComponent<UtilityLaneData>(prefab))
            {
                UtilityLaneData utility = EntityManager.GetComponentData<UtilityLaneData>(prefab);

                if ((utility.m_UtilityTypes & (UtilityTypes.WaterPipe | UtilityTypes.SewagePipe | UtilityTypes.StormwaterPipe)) != 0)
                    entry.Category = "pipe";
                else if ((utility.m_UtilityTypes & (UtilityTypes.LowVoltageLine | UtilityTypes.HighVoltageLine | UtilityTypes.Catenary)) != 0)
                    entry.Category = "cable";
                else if ((utility.m_UtilityTypes & UtilityTypes.Fence) != 0)
                    entry.Category = "fence";

                entry.Hanging = utility.m_Hanging;
            }

            List<object> catalogueParts = new();

            if (EntityManager.HasBuffer<SubMesh>(prefab))
            {
                DynamicBuffer<SubMesh> buffer = EntityManager.GetBuffer<SubMesh>(prefab, true);

                // Copy out before reading geometry: obtaining a mesh loads an asset, and a
                // dynamic buffer is only valid until the entity's chunk moves.
                //
                SubMesh[] subMeshes = new SubMesh[buffer.Length];
                for (int i = 0; i < buffer.Length; i++) subMeshes[i] = buffer[i];

                foreach (SubMesh subMesh in subMeshes)
                {
                    Entity meshEntity = subMesh.m_SubMesh;
                    if ((meshEntity == Entity.Null) || !EntityManager.HasComponent<MeshData>(meshEntity)) continue;

                    MeshData meshData = EntityManager.GetComponentData<MeshData>(meshEntity);

                    // Parts whose default layers hold no world-view bit are never drawn in the
                    // world (`SearchSystem.GetLayers` hands `m_DefaultLayers` back verbatim
                    // except the exact Pipeline|SubPipeline pair, and `GetBoundsMask` counts
                    // only Default/Moving/Tunnel/Marker as `NormalLayers`) — underground pipes,
                    // cables and waterway overlays stay excluded. Tunnel-layer parts are world
                    // geometry — the game draws them on its Tunnel cutaway layer — and are
                    // kept, as the bundle renders tunnels in full. Decal-state
                    // parts are NOT excluded — they are how the game paints its junction
                    // interiors: `TerrainSystem` flattens the ground under a junction
                    // (`ShiftTerrain`/`ClipTerrain`) and the drive lanes crossing it carry
                    // decal sub-meshes that project asphalt and markings onto that ground. A
                    // bundle without them shows exactly the petal-shaped holes between the
                    // corner sweeps that three rounds of junction "fill" tried to invent
                    // geometry for. They join the catalogue tagged `decal`, and the consumer
                    // bends them like any part and lifts them off the surface.
                    //
                    bool decal = (meshData.m_State & MeshFlags.Decal) != 0;
                    if (decal)
                    {
                        run.SkippedLaneDecal++;

                        // The advanced toggle: markings and wear can be deselected outright, in
                        // which case the part never enters the catalogue — same shape as the
                        // pipeline-layer exclusion below.
                        if (!run.IncludeLaneDecals) continue;
                    }

                    const MeshLayer world = MeshLayer.Default | MeshLayer.Moving | MeshLayer.Tunnel | MeshLayer.Marker;
                    if ((meshData.m_DefaultLayers != 0) && ((meshData.m_DefaultLayers & world) == 0))
                    {
                        run.SkippedLanePipeline++;
                        continue;
                    }

                    if (!run.LaneMeshIds.TryGetValue(meshEntity, out string assetId))
                    {
                        string name = Instance.Name.GetPrefabName(meshEntity);
                        if (string.IsNullOrEmpty(name)) name = $"lane_mesh_{meshEntity.Index}";

                        // Unique over the whole run: two distinct mesh entities sharing a name
                        // must not share a file — the join key's uniqueness is assigned over the
                        // full set, never per name.
                        //
                        assetId = RipAssetIds.SanitizeAssetId(name);
                        if (!run.LaneIdsUsed.Add(assetId))
                        {
                            assetId = $"{assetId}_{meshEntity.Index}";
                            run.LaneIdsUsed.Add(assetId);
                        }

                        MeshAsset mesh = Instance.MeshRip.ReadMesh(meshEntity, assetId, name);
                        if (mesh == null) continue;

                        // Classified while the mesh is in hand — it is a property of the part's
                        // materials, so it is settled once per part mesh and reused by every
                        // prefab that draws it.
                        //
                        string role = ClassifyDecalRole(run, mesh);
                        if (role != null) run.LaneDecalRoles[assetId] = role;

                        run.LaneMeshIds[meshEntity] = assetId;
                        Enqueue(run, new NetworkItem { Mesh = mesh, IsLane = true });
                    }

                    entry.Parts.Add(new LanePartRef { Flags = subMesh.m_Flags, Index = catalogueParts.Count });
                    Dictionary<string, object> cataloguePart;
                    catalogueParts.Add(cataloguePart = new Dictionary<string, object>
                    {
                        ["assetId"] = assetId,
                        ["pos"] = new[]
                        {
                            Math.Round((double)subMesh.m_Position.x, 4),
                            Math.Round((double)subMesh.m_Position.y, 4),
                            Math.Round((double)subMesh.m_Position.z, 4)
                        },
                        ["rot"] = new[]
                        {
                            Math.Round((double)subMesh.m_Rotation.value.x, 6),
                            Math.Round((double)subMesh.m_Rotation.value.y, 6),
                            Math.Round((double)subMesh.m_Rotation.value.z, 6),
                            Math.Round((double)subMesh.m_Rotation.value.w, 6)
                        },
                        ["inverted"] = (meshData.m_State & MeshFlags.Invert) != 0,
                        ["tilingCount"] = meshData.m_TilingCount,
                        ["geometryTiling"] = (meshData.m_State & MeshFlags.Tiling) != 0,
                        ["boundsMin"] = new[]
                        {
                            Math.Round((double)meshData.m_Bounds.min.x, 4),
                            Math.Round((double)meshData.m_Bounds.min.y, 4),
                            Math.Round((double)meshData.m_Bounds.min.z, 4)
                        },
                        ["boundsMax"] = new[]
                        {
                            Math.Round((double)meshData.m_Bounds.max.x, 4),
                            Math.Round((double)meshData.m_Bounds.max.y, 4),
                            Math.Round((double)meshData.m_Bounds.max.z, 4)
                        }
                    });

                    if (decal)
                    {
                        cataloguePart["decal"] = true;

                        // Which of the two things a decal part is (§5B). Optional by design: a
                        // part whose materials say neither carries no key at all rather than a
                        // guess a consumer would trust.
                        //
                        if (run.LaneDecalRoles.TryGetValue(assetId, out string role))
                        {
                            cataloguePart["decalRole"] = role;
                            run.LaneDecalsByRole.TryGetValue(role, out int roleCount);
                            run.LaneDecalsByRole[role] = roleCount + 1;
                        }
                        else
                        {
                            run.SkippedLaneDecalUnclassified++;
                        }
                    }
                }
            }

            if (entry.Parts.Count > 0)
            {
                string prefabName = Instance.Name.GetPrefabName(prefab);
                string key = RipAssetIds.SanitizeAssetId(
                    string.IsNullOrEmpty(prefabName) ? $"lane_prefab_{prefab.Index}" : prefabName);
                if (run.LaneCatalogue.ContainsKey(key)) key = $"{key}_{prefab.Index}";

                entry.Key = key;
                run.LaneCatalogue[key] = new Dictionary<string, object>
                {
                    ["category"] = entry.Category,
                    ["hanging"] = Math.Round((double)entry.Hanging, 5),
                    ["parts"] = catalogueParts
                };
            }

            run.LaneParts[prefab] = entry;
            return entry;
        }

        /// <summary>
        /// Which of the two things a lane decal part is, from the material table alone.
        /// §5B names them: the <b>wear</b> tint — a ~12%-opacity near-black shade the game lays
        /// over a drive lane, whose texture never reaches opacity (measured ~(18,18,18), α ≤ 113)
        /// under a half-strength material tint — and the <b>marking</b>: crosswalks, lane lines,
        /// bus-lane paint, rail covers, all of which paint a fully opaque base where they paint at
        /// all. A consumer needs the distinction because it decides the draw order (§8b: markings
        /// go a hair above the wear tint so the two never z-fight) and because a wear tint is
        /// modulated by <c>wear</c> while a marking is not.
        /// <b>What separates them in the data</b> is the material's own tint alpha. The wear
        /// surfaces carry <c>tint.a = 128/255</c> — a factor that caps the part's opacity below
        /// full however opaque its texture is — and the marking surfaces carry no tint at all
        /// (white, opaque). Texture pixels would be the more direct test but they never reach this
        /// stage: a texture is read back and encoded on the writer thread, and the material table
        /// is what the catalogue can see. So a tint that can never resolve opaque is read as wear,
        /// an unattenuated one as marking, and a part whose materials resolve to nothing at all is
        /// left unclassified — <c>decalRole</c> is optional and an absent key is the honest answer.
        /// The mesh's own name is deliberately NOT consulted: it is the same
        /// <c>*_Lane_*_Mesh</c>/<c>*_Line_Mesh</c> vocabulary for both, and a name-based rule would
        /// misfile every modded part.
        /// </summary>
        /// <param name="run">The rip in flight.</param>
        /// <param name="mesh">The part mesh, freshly read.</param>
        /// <returns><c>"wear"</c>, <c>"marking"</c>, or <c>null</c> when nothing decides.</returns>
        private static string ClassifyDecalRole(NetworkRun run, MeshAsset mesh)
        {
            if ((mesh == null) || (run.Materials == null)) return null;

            bool found = false;
            float minimumAlpha = float.MaxValue;

            foreach (MeshPrimitive primitive in mesh.Primitives)
            {
                if (string.IsNullOrEmpty(primitive.MaterialId)) continue;

                MaterialDescription material = run.Materials.Describe(primitive.MaterialId);
                if (!material.Found) continue;

                found = true;
                minimumAlpha = math.min(minimumAlpha, material.Tint.w);
            }

            if (!found) return null;
            return (minimumAlpha < 0.99f) ? "wear" : "marking";
        }
    }
}
