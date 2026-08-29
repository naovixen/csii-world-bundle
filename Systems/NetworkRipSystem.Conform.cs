using Carto.Domain;
using Colossal.Mathematics;
using Game.Net;
using Game.Prefabs;
using System;
using System.Collections.Generic;
using Unity.Entities;
using Unity.Mathematics;

namespace Carto.Systems
{
    /// <summary>
    /// The terrain a composition piece conforms to, and the per-slice height bounds that decide
    /// which of its vertices do.
    /// </summary>
    /// <remarks>
    /// The game does not conform on the CPU at all: <c>BatchMeshHelpers</c> writes a class into
    /// a vertex <c>Color32.b</c> and the SHADER decides what to do with it. CS2 ships its shaders
    /// compiled, so this side has to sample the heightmap itself and apply the move — a
    /// downward clamp for a bottom edge (§38), an assignment for a top (§34).
    /// </remarks>
    public partial class NetworkRipSystem
    {
        /// <summary>
        /// Lazily capture the terrain heightmap for conforming. The game's shader conforms per
        /// frame against the finest cascade slice covering the vertex (0.875–1.75 m near the
        /// camera); the fine cascade capture (<c>TerrainRipSystem.FineCascade.cs</c>, run before
        /// this stage) is that ground at 1.75 m, so it is sampled when present and the base
        /// 3.5 m readback — the same one the terrain layer ships — otherwise (§34's counted
        /// approximation, now only when the capture did not run).
        /// </summary>
        private bool EnsureConformTerrain()
        {
            if (_conformHeights != null) return _conformHeights.Length > 0;
            try
            {
                Game.Simulation.TerrainHeightData heightData = Instance.Terrain.GetHeightData(true);
                if (!heightData.isCreated)
                {
                    _conformHeights = Array.Empty<ushort>();
                    return false;
                }
                if (Instance.TerrainRip.TryGetFineHeights(out ushort[] fine, out int fineWidth, out int fineHeight,
                                                          out double fineCell, out double2 fineOriginSW))
                {
                    // Same bilinear read: texel (i, j) is centred at originSW + (i, j)·cell, so
                    // heightmap-space = (world − originSW) / cell — the base map's own
                    // (world + offset)·scale form with offset = −originSW and scale = 1/cell; the
                    // elevation encoding is the cascade's, i.e. the base map's.
                    _conformHeights = fine;
                    _conformResolution = new int2(fineWidth, fineHeight);
                    _conformScale = new float3((float)(1.0 / fineCell), heightData.scale.y, (float)(1.0 / fineCell));
                    _conformOffset = new float3((float)-fineOriginSW.x, heightData.offset.y, (float)-fineOriginSW.y);
                    _conformSource = $"fine cascade ({fineCell:F3} m)";
                    return true;
                }
                _conformHeights = heightData.heights.ToArray();
                _conformResolution = new int2(heightData.resolution.x, heightData.resolution.z);
                _conformScale = heightData.scale;
                _conformOffset = heightData.offset;
                _conformSource = $"base heightmap ({1f / heightData.scale.x:F3} m)";
                return true;
            }
            catch (Exception)
            {
                _conformHeights = Array.Empty<ushort>();
                return false;
            }
        }

        /// <summary>`TerrainUtils.SampleHeight`'s bilinear read, over the captured copy.
        ///</summary>
        private float SampleConformHeight(float3 world)
        {
            float2 xz = ((world + _conformOffset) * _conformScale).xz;
            int2 lo = (int2)math.floor(xz);
            int2 hi = lo + 1;
            lo = math.clamp(lo, 0, _conformResolution - 1);
            hi = math.clamp(hi, 0, _conformResolution - 1);
            float h00 = _conformHeights[(lo.y * _conformResolution.x) + lo.x];
            float h10 = _conformHeights[(lo.y * _conformResolution.x) + hi.x];
            float h01 = _conformHeights[(hi.y * _conformResolution.x) + lo.x];
            float h11 = _conformHeights[(hi.y * _conformResolution.x) + hi.x];
            float2 f = math.saturate(xz - math.floor(xz));
            float height = math.lerp(math.lerp(h00, h10, f.x), math.lerp(h01, h11, f.x), f.y);
            return (height / _conformScale.y) - _conformOffset.y;
        }

        /// <summary>
        /// The per-slice height bounds of one piece — `InitializeHeightBounds`/`AddHeightBounds`
        /// verbatim, cached per piece entity.
        /// </summary>
        private PieceHeightBounds GetPieceHeightBounds(Entity piece, PieceGeometry source, float sizeZ)
        {
            if (_pieceHeightBounds.TryGetValue(piece, out PieceHeightBounds cached)
                && (math.abs(cached.SizeZ - sizeZ) < 1e-3f))
            {
                return cached;
            }

            const int slices = 256;
            PieceHeightBounds bounds = new()
            {
                SizeZ = sizeZ,
                Min = new float[slices + 1],
                Max = new float[slices + 1]
            };
            for (int i = 0; i <= slices; i++)
            {
                bounds.Min[i] = float.MaxValue;
                bounds.Max[i] = float.MinValue;
            }

            float scale = slices / math.max(sizeZ, 0.001f);
            void AddEdge(float3 a, float3 b, int ia, int ib)
            {
                if (ia > ib)
                {
                    (a, b) = (b, a);
                    (ia, ib) = (ib, ia);
                }
                float step = 1f / (ib - ia + 1);
                for (int i = ia; i <= ib; i++)
                {
                    float y = math.lerp(a.y, b.y, (i - ia) * step);
                    bounds.Min[i] = math.min(bounds.Min[i], y);
                    bounds.Max[i] = math.max(bounds.Max[i], y);
                }
            }

            for (int t = 0; t + 2 < source.Indices.Length; t += 3)
            {
                float3 a = source.Vertices[source.Indices[t]];
                float3 b = source.Vertices[source.Indices[t + 1]];
                float3 c = source.Vertices[source.Indices[t + 2]];
                int ia = math.clamp((int)math.round(a.z * scale) + (slices >> 1), 0, slices);
                int ib = math.clamp((int)math.round(b.z * scale) + (slices >> 1), 0, slices);
                int ic = math.clamp((int)math.round(c.z * scale) + (slices >> 1), 0, slices);
                AddEdge(a, b, ia, ib);
                AddEdge(b, c, ib, ic);
                AddEdge(c, a, ic, ia);
            }

            _pieceHeightBounds[piece] = bounds;
            return bounds;
        }
    }
}
