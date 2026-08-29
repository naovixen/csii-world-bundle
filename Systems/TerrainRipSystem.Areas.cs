using Carto.IO;
using Colossal.IO.AssetDatabase;
using Carto.Utils;
using Colossal.Mathematics;
using Game;
using Game.Net;
using Game.Prefabs;
using Game.Simulation;
using Newtonsoft.Json;
using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using Unity.Entities;
using Unity.Mathematics;
using UnityEngine;

namespace Carto.Systems
{
    /// <summary>
    /// The §8b surface areas and the §-less district names — the two layers this stage writes
    /// that have nothing to do with terrain.
    /// </summary>
    /// <remarks>
    /// They ride the terrain stage because they share its writer task and its one toggle, and
    /// they are a partial file rather than their own system because everything here is the ECS
    /// surface of one: two <c>EntityQuery</c> fields built in <c>OnCreate</c>, and
    /// <c>EntityManager</c> buffers on every line. Refactor Phase 9 names an <c>AreaRipSystem</c>
    /// for them; that is a change to the rip's stage list and to the texture hand-off, not a
    /// move, and it is recorded in the plan rather than folded in here.
    /// </remarks>
    public partial class TerrainRipSystem
    {
        /// <summary>
        /// Rip the player's named districts into <c>districts.jsonl</c>. Contract §8d.
        /// <b>Why this layer exists.</b> A CS2 city has exactly one source of PLACE names, and
        /// this is it: `attrs.name` on a network segment is a STREET name, `attrs.brand` on a
        /// building is a business, and neither says where in the city you are. A district is a
        /// player-drawn polygon with a player-typed label, and the game itself uses it that way —
        /// <c>Address</c> resolves a building's district through <c>CurrentDistrict</c> and
        /// <c>NameSystem.GetRenderedLabelName</c>, which is what the vanilla vector export
        /// already ships (<c>Systems/AreaSystem.cs</c>). It never reached the bundle because
        /// <c>areas.jsonl</c>'s query demands <c>Game.Areas.Surface</c> and a district paints
        /// nothing.
        /// <para>
        /// Emitted with the same geometry encoding as §8b — world-position nodes and index
        /// triangles off the game's own area grid — plus a centroid, so a consumer that only
        /// wants "where is Downtown" does not have to triangulate anything.
        /// </para>
        /// </summary>
        /// <param name="outputDirectory">The bundle root.</param>
        /// <param name="report">The report to note results in.</param>
        private void RipDistricts(string outputDirectory, StringBuilder report)
        {
            var districts = _districtQuery.ToEntityArray(Unity.Collections.Allocator.TempJob);
            int written = 0, skippedEmpty = 0, unnamed = 0;

            try
            {
                using var writer = new Carto.IO.RecordWriter(
                    IOUtils.CombinePath(outputDirectory, "districts.jsonl"));

                foreach (Entity entity in districts)
                {
                    DynamicBuffer<Game.Areas.Node> nodes = EntityManager.GetBuffer<Game.Areas.Node>(entity, true);
                    DynamicBuffer<Game.Areas.Triangle> triangles = EntityManager.GetBuffer<Game.Areas.Triangle>(entity, true);
                    if ((nodes.Length < 3) || (triangles.Length == 0))
                    {
                        skippedEmpty++;
                        continue;
                    }

                    // The label the PLAYER typed, through the game's own name system — the same
                    // call the vanilla vector export makes for a district.
                    string name = Instance.Name.GetLabelName(entity);
                    if (string.IsNullOrEmpty(name)) unnamed++;

                    var record = new Dictionary<string, object>
                    {
                        ["id"] = entity.Index,
                        ["name"] = name ?? string.Empty
                    };

                    double[][] nodeArray = new double[nodes.Length][];
                    double cx = 0.0, cy = 0.0, cz = 0.0;
                    for (int i = 0; i < nodes.Length; i++)
                    {
                        float3 position = nodes[i].m_Position;
                        nodeArray[i] = BundlePrecision.Vector(position);
                        cx += position.x;
                        cy += position.y;
                        cz += position.z;
                    }
                    record["nodes"] = nodeArray;
                    record["centroid"] = new[]
                    {
                        Math.Round(cx / nodes.Length, 3),
                        Math.Round(cy / nodes.Length, 3),
                        Math.Round(cz / nodes.Length, 3)
                    };

                    int[][] triangleArray = new int[triangles.Length][];
                    for (int i = 0; i < triangles.Length; i++)
                    {
                        int3 indices = triangles[i].m_Indices;
                        triangleArray[i] = new[] { indices.x, indices.y, indices.z };
                    }
                    record["triangles"] = triangleArray;

                    writer.Write(record);
                }

                // The count comes off the writer rather than a local: a loop with two
                // `continue`s in it and a hand-kept tally is how a manifest ends up disagreeing
                // with its own layer.
                written = writer.Count;
            }
            catch (Exception ex)
            {
                _log.Error(ex);
                report.AppendLine($"Districts failed: {ex.Message}");
                return;
            }
            finally
            {
                districts.Dispose();
            }

            report.AppendLine($"Districts: {written} written to districts.jsonl"
                              + ((skippedEmpty > 0) ? $", {skippedEmpty} with no geometry skipped" : string.Empty)
                              + ((unnamed > 0) ? $", {unnamed} carry no label" : string.Empty)
                              + ((written == 0) ? " — this city has no districts drawn" : string.Empty));
        }

        /// <summary>
        /// Rip every rendered surface area — driveways, parking-lot pavement, park paths, the
        /// painted patches under meshless service buildings — into <c>areas.jsonl</c> +
        /// <c>areaprefabs.json</c>. Contract §8b.
        /// <b>Why this layer exists.</b> A parking lot is a <i>meshless</i> building prefab: its
        /// instance record places nothing visible, its markings are §4.2a decal instances, and the
        /// pavement itself is a <c>SurfacePrefab</c> area the building spawns as a sub-area —
        /// triangulated on the area grid (<c>Game.Areas.Node</c>/<c>Triangle</c> buffers) and drawn
        /// by <c>AreaBatchSystem</c> with the prefab's <c>RenderedArea.m_Material</c> (falling back
        /// to the global <c>AreasConfigurationPrefab.m_RenderedAreaMaterial</c>). House driveways
        /// are the same mechanism. Without this layer those surfaces simply do not exist in the
        /// bundle, which is why every reconstruction showed grass where the game shows pavement.
        /// </summary>
        /// <param name="outputDirectory">The bundle root.</param>
        /// <param name="terrainDirectory">The absolute <c>terrain/</c> directory, for textures.</param>
        /// <param name="report">The report to note results in.</param>
        private void RipSurfaceAreas(string outputDirectory, string terrainDirectory, StringBuilder report)
        {
            var areas = _surfaceAreaQuery.ToEntityArray(Unity.Collections.Allocator.TempJob);
            int written = 0, skippedEmpty = 0, skippedUnpainted = 0;
            Dictionary<Entity, string> prefabKeys = new();
            HashSet<string> usedKeys = new();
            Dictionary<string, object> catalogue = new();
            _pendingAreaTextures = new List<KeyValuePair<string, PendingTexture>>();

            try
            {
                using var writer = new Carto.IO.RecordWriter(
                    IOUtils.CombinePath(outputDirectory, "areas.jsonl"));

                foreach (Entity entity in areas)
                {
                    DynamicBuffer<Game.Areas.Node> nodes = EntityManager.GetBuffer<Game.Areas.Node>(entity, true);
                    DynamicBuffer<Game.Areas.Triangle> triangles = EntityManager.GetBuffer<Game.Areas.Triangle>(entity, true);
                    if ((nodes.Length < 3) || (triangles.Length == 0))
                    {
                        skippedEmpty++;
                        continue;
                    }

                    Entity prefab = EntityManager.GetComponentData<PrefabRef>(entity).m_Prefab;
                    if (!prefabKeys.TryGetValue(prefab, out string key))
                    {
                        key = CatalogueAreaPrefab(prefab, terrainDirectory, usedKeys, catalogue);
                        prefabKeys[prefab] = key;
                    }
                    if (key == null)
                    {
                        skippedUnpainted++;
                        continue;
                    }

                    var record = new Dictionary<string, object>
                    {
                        ["id"] = entity.Index,
                        ["prefab"] = key
                    };

                    if (EntityManager.HasComponent<Game.Common.Owner>(entity))
                    {
                        Entity owner = EntityManager.GetComponentData<Game.Common.Owner>(entity).m_Owner;
                        if (owner != Entity.Null) record["owner"] = owner.Index;
                    }

                    double[][] nodeArray = new double[nodes.Length][];
                    for (int i = 0; i < nodes.Length; i++)
                    {
                        nodeArray[i] = BundlePrecision.Vector(nodes[i].m_Position);
                    }
                    record["nodes"] = nodeArray;

                    int[][] triangleArray = new int[triangles.Length][];
                    double[][] rangeArray = new double[triangles.Length][];
                    bool anyRange = false;
                    for (int i = 0; i < triangles.Length; i++)
                    {
                        Game.Areas.Triangle triangle = triangles[i];
                        int3 indices = triangle.m_Indices;
                        triangleArray[i] = new[] { indices.x, indices.y, indices.z };

                        // The game's own answer to "how far does the ground depart from this
                        // flat triangle": GeometrySystem.GetHeightRange walks every heightmap
                        // cell the triangle covers and takes min/max RELATIVE TO ITS PLANE,
                        // water included. AreaRenderSystem then draws the triangle as a prism
                        // spanning it and projects the surface material onto the terrain
                        // inside — the area is paint, not a plane (contract §8b, audit §30).
                        // Free to carry: the buffer is already in hand.
                        //
                        Bounds1 range = triangle.m_HeightRange;
                        rangeArray[i] = new[]
                        {
                            Math.Round((double)range.min, 3),
                            Math.Round((double)range.max, 3)
                        };
                        anyRange |= (range.min != 0f) || (range.max != 0f);
                    }
                    record["triangles"] = triangleArray;
                    if (anyRange) record["heightRanges"] = rangeArray;

                    writer.Write(record);
                }

                // See RipDistricts: the count is what the writer wrote, not a hand-kept tally.
                written = writer.Count;
            }
            catch (Exception ex)
            {
                _log.Error(ex);
                report.AppendLine($"Surface areas failed: {ex.Message}");
                return;
            }
            finally
            {
                areas.Dispose();
            }

            try
            {
                Carto.IO.JsonFile.Write(IOUtils.CombinePath(outputDirectory, "areaprefabs.json"), catalogue);
            }
            catch (Exception ex)
            {
                _log.Error(ex);
                report.AppendLine($"Could not write areaprefabs.json: {ex.Message}");
            }

            int unresolvedPrefabs = 0, normalMaps = 0, maskMaps = 0, prioritised = 0;
            foreach (object value in catalogue.Values)
            {
                if (!(value is Dictionary<string, object> catalogued)) continue;
                if (catalogued.ContainsKey("unresolved")) unresolvedPrefabs++;
                if (catalogued.ContainsKey("normalMap")) normalMaps++;
                if (catalogued.ContainsKey("maskMap")) maskMaps++;
                if (catalogued.TryGetValue("rendererPriority", out object priority)
                    && (priority is int order) && (order != 0)) prioritised++;
            }

            report.AppendLine($"Area materials: {normalMaps} normal map(s), {maskMaps} mask map(s), "
                            + $"{prioritised} prefab(s) with a non-zero rendererPriority (overlap draw order).");

            report.AppendLine($"Surface areas: {written} area(s) over {catalogue.Count} prefab(s); "
                            + $"{_pendingAreaTextures.Count} texture(s) queued for the writer task"
                            + ((skippedEmpty > 0) ? $"; {skippedEmpty} empty skipped" : "")
                            + ((skippedUnpainted > 0)
                                ? $"; {skippedUnpainted} unpainted area(s) excluded (prefab has no RenderedArea — "
                                + "the game never draws them; their triangles are in clipvolumes.jsonl)"
                                : "")
                            + ((unresolvedPrefabs > 0)
                                ? $"; {unresolvedPrefabs} prefab(s) UNRESOLVED — asset not loaded this session, "
                                + "the game drew those areas grey (Missing Area); enable the packs and re-rip"
                                : ""));
        }

    }
}
