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
    /// The §4.2a decal flatten, and the sub-object exclusions that keep a rip to the city.
    /// </summary>
    /// <remarks>
    /// A decal ships as the flat quad it paints rather than the projector volume it paints
    /// through. The exclusions are the other half of the same question: a utility marker and a
    /// vehicle's cargo are both sub-objects of something placed, and neither is scenery.
    /// </remarks>
    public partial class MeshRipSystem
    {
        /// <summary>
        /// Replace a decal's projector volume with the quad it actually paints.
        /// <b>Why the volume alone is useless.</b> A decal's mesh is not its artwork — it is the
        /// box the game projects artwork down through, and every variant shares one box. Measured
        /// in a real city: <c>NA_RoadArrow_Forward</c>, <c>NA_RoadArrow_Forward_Left90</c> and
        /// <c>NA_RoadArrow_Left180</c> are byte-identical 3 × 1 × 6 m boxes with byte-identical
        /// UVs and one shared material, and their mesh UVs are a default box unwrap — six faces
        /// across six unrelated atlas rects, one of them with a zero normal. Nothing in the mesh
        /// says which arrow it is.
        /// <b>What does say.</b> <c>DecalProperties.m_TextureArea</c> — a <c>Bounds2</c> the game
        /// hands the shader as <c>_TextureArea</c> alongside <c>_MeshSize</c> and
        /// <c>_DecalLayerMask</c> (see <c>ManagedBatchSystem.CreateBatch</c> and
        /// <c>RenderPrefabRenderer.Instance.SetDecalProperties</c>). It is the sub-rect of the
        /// atlas this prefab draws, and the arrow sheet is a 8 × 4 grid of 0.125 × 0.25 cells whose
        /// aspect matches the 3 × 6 m footprint exactly. Baking that rect across the footprint
        /// turns a shared box into the marking it stands for, and shrinks the mesh from 24
        /// vertices to 4.
        /// The quad sits at the volume's mid-height, not its floor: a projector is centred on the
        /// surface it paints so that it catches ground a little above and below, so the midpoint is
        /// the best estimate of the carriageway and the floor would bury the marking half a metre
        /// under it.
        /// </summary>
        /// <param name="mesh">The mesh to rewrite in place.</param>
        /// <param name="renderPrefab">The render prefab that may carry decal properties.</param>
        /// <param name="trace">Optional sink recording the decision.</param>
        /// <returns>Whether the mesh was replaced.</returns>
        /// <summary>
        /// The import pipeline's own up-facing vertex test (`AssetImportPipeline.
        /// SetupDecalComponent`): a real normal (`SqrMagnitude > 0.01`) within 0.01° of straight
        /// up. cos(0.01°) ≈ 0.99999998.
        /// </summary>
        private static bool IsExactlyUp(float3 normal)
        {
            float lengthSq = math.lengthsq(normal);
            if (lengthSq <= 0.01f) return false;
            return (normal.y / math.sqrt(lengthSq)) >= 0.99999998f;
        }

        private bool TryFlattenDecal(MeshAsset mesh, RenderPrefab renderPrefab, List<string> trace)
        {
            if ((mesh == null) || (renderPrefab == null)) return false;
            if ((mesh.Vertices == null) || (mesh.Vertices.Length == 0)) return false;

            DecalProperties decal;
            try
            {
                if (!renderPrefab.TryGet(out decal) || (decal == null) || !decal.active) return false;
            }
            catch (Exception ex)
            {
                trace?.Add($"decal: properties did not resolve — {ex.GetType().Name} {ex.Message}");
                return false;
            }

            float3 min = mesh.Vertices[0], max = mesh.Vertices[0];
            for (int i = 1; i < mesh.Vertices.Length; i++)
            {
                min = math.min(min, mesh.Vertices[i]);
                max = math.max(max, mesh.Vertices[i]);
            }

            // A volume with no footprint projects nothing; leaving it alone keeps a degenerate
            // prefab visible as itself rather than as a zero-area quad.
            //
            if (!((max.x - min.x) > 1e-4f) || !((max.z - min.z) > 1e-4f)) return false;

            float y = (min.y + max.y) * 0.5f;

            float3[] corners =
            {
                new float3(min.x, y, min.z), new float3(max.x, y, min.z),
                new float3(max.x, y, max.z), new float3(min.x, y, max.z)
            };

            // The UV PAIRING is taken from the authored mesh, never rebuilt from `m_TextureArea`
            // alone: reconstruction from the bounds has to guess which way `u` and `v` run
            // across the quad. An earlier version guessed `+x → +u` and pre-flipped `v`; the
            // guess was mirrored, and every road-marking text in the city read backwards
            // ("TAXI" as "IXAT", a backwards S in "BUS"), while the pre-flip cancelled the
            // writer's Unity→glTF flip and cropped some markings to the wrong band of their
            // atlas outright. Sampling each corner's nearest up-facing authored vertex keeps
            // orientation exactly as the game renders it, and the writer's usual flip applies
            // as it does to every other mesh. (The RECT is a different matter — see below.)
            //
            // The same up-facing selection the game's derivation uses; a projector volume whose
            // top face carries no upward normal (none measured, but authored content varies)
            // falls back to every vertex rather than shipping an unmapped quad.
            //
            // Pass 0 is the import pipeline's own vertex test (`SetupDecalComponent`:
            // `SqrMagnitude > 0.01` and `Angle(up, n) < 0.01°` — exactly-up, not merely upward),
            // so that for DefaultDecal objects the rect below reproduces `m_TextureArea` by
            // construction and the rescale is a true identity. Pass 1 keeps the previously
            // validated ≥ 0.9 tolerance for authored content the strict test misses; pass 2
            // takes everything rather than shipping an unmapped quad.
            //
            float2[] cornerUvs = new float2[4];
            for (int c = 0; c < 4; c++)
            {
                float best = float.MaxValue;
                for (int pass = 0; (pass < 3) && (best == float.MaxValue); pass++)
                {
                    for (int i = 0; i < mesh.Vertices.Length; i++)
                    {
                        if ((pass < 2) && (mesh.Normals != null) && (i < mesh.Normals.Length)
                            && ((pass == 0) ? !IsExactlyUp(mesh.Normals[i]) : (mesh.Normals[i].y < 0.9f))) continue;

                        float2 offset = mesh.Vertices[i].xz - corners[c].xz;
                        float distance = math.lengthsq(offset);
                        if (distance < best)
                        {
                            best = distance;
                            cornerUvs[c] = ((mesh.Uvs != null) && (i < mesh.Uvs.Length)) ? mesh.Uvs[i] : default;
                        }
                    }
                }
            }

            // The authored pairing gives the ORIENTATION; `m_TextureArea` gives the RECT. The
            // shader never samples the mesh UVs of a decal — it is handed `_TextureArea`
            // (ManagedBatchSystem.CreateBatch) and maps the projector footprint into that rect.
            // For DefaultDecal imports the pipeline derives the area from these same UVs, so the
            // rescale below is the identity and the road-marking text keeps rendering exactly as
            // validated. But the LANE decal parts — centre lines, crosswalks, lane lines — ship
            // authored box UVs that all crop one generic, mostly-transparent slab of the LaneLine
            // atlas ([0.625, 0.25]–[0.875, 0.5], measured: mean α 27/255), while their prefabs'
            // `m_TextureArea` selects the actual opaque marking band. Keeping the authored rect
            // is why every crosswalk and centre line in the first full import was invisible.
            // Rescaling the authored rect into the area keeps the pairing (the mirrored-text
            // lesson above) and takes the crop from where the game actually takes it.
            //
            float2 rectMin = new(float.MaxValue), rectMax = new(float.MinValue);
            for (int pass = 0; (pass < 3) && (rectMin.x == float.MaxValue); pass++)
            {
                for (int i = 0; i < mesh.Vertices.Length; i++)
                {
                    if ((pass < 2) && (mesh.Normals != null) && (i < mesh.Normals.Length)
                        && ((pass == 0) ? !IsExactlyUp(mesh.Normals[i]) : (mesh.Normals[i].y < 0.9f))) continue;
                    if ((mesh.Uvs == null) || (i >= mesh.Uvs.Length)) continue;

                    rectMin = math.min(rectMin, mesh.Uvs[i]);
                    rectMax = math.max(rectMax, mesh.Uvs[i]);
                }
            }

            float2 rectSize = rectMax - rectMin;
            float2 areaMin = decal.m_TextureArea.min;
            float2 areaSize = decal.m_TextureArea.max - decal.m_TextureArea.min;
            bool remapped = false;
            if (math.all(rectSize > 1e-6f) && math.all(areaSize > 0f)
                && (math.any(math.abs(rectMin - areaMin) > 1e-4f)
                    || math.any(math.abs(rectSize - areaSize) > 1e-4f)))
            {
                for (int c = 0; c < 4; c++)
                {
                    cornerUvs[c] = areaMin + (((cornerUvs[c] - rectMin) / rectSize) * areaSize);
                }
                remapped = true;
            }

            mesh.Vertices = corners;
            mesh.Uvs = cornerUvs;

            mesh.Normals = new[] { math.up(), math.up(), math.up(), math.up() };
            mesh.Tangents = null;

            // Same rule the network layer's generated strips use: take the winding the quad
            // actually produces rather than assume one, and reverse it when it faces the ground.
            //
            bool flip = math.cross(mesh.Vertices[1] - mesh.Vertices[0],
                                   mesh.Vertices[2] - mesh.Vertices[0]).y < 0f;

            mesh.Indices = flip ? new[] { 0, 2, 1, 0, 3, 2 } : new[] { 0, 1, 2, 0, 2, 3 };

            string materialId = ((mesh.Primitives.Count > 0) && !string.IsNullOrEmpty(mesh.Primitives[0].MaterialId))
                ? mesh.Primitives[0].MaterialId : $"{mesh.AssetId}_mat0";

            mesh.Primitives.Clear();
            mesh.Primitives.Add(new MeshPrimitive
            {
                StartIndex = 0,
                IndexCount = 6,
                StartVertex = 0,
                VertexCount = 4,
                MaterialIndex = 0,
                MaterialId = materialId
            });

            mesh.BoundsMin = new float3(min.x, y, min.z);
            mesh.BoundsMax = new float3(max.x, y, max.z);

            trace?.Add($"decal: flattened to a {max.x - min.x:F2} × {max.z - min.z:F2} m quad at y {y:F2}, "
                     + $"corner uvs [{cornerUvs[0].x:F4}, {cornerUvs[0].y:F4}]–[{cornerUvs[2].x:F4}, {cornerUvs[2].y:F4}]"
                     + (remapped ? $" (authored rect rescaled into textureArea [{areaMin.x:F4}, {areaMin.y:F4}]+"
                                 + $"[{areaSize.x:F4}, {areaSize.y:F4}])" : ""));
            return true;
        }

    }
}
