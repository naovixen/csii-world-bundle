using System.Collections.Generic;
using Unity.Mathematics;

namespace Carto.Domain
{
    /// <summary>
    /// One drawn lane in <c>lanemeshes.jsonl</c> (contract §5B v2): the parameters a consumer
    /// needs to bend the prefab's straight part meshes along the lane's curve, rather than the
    /// baked result.
    /// <b>Why parameters, not geometry.</b> The first shipped form baked every lane into its own
    /// <c>.glb</c>: a full city produced 33,500 files and 8.6 GiB — 112 unique source meshes
    /// duplicated hundreds of times each, in breach of the repo's dedupe-by-prefab rule. The
    /// game itself stores exactly this parameter set and bends in the vertex shader; the bundle
    /// now mirrors that, with the bend algorithm documented in the contract and reference-
    /// implemented in the Blender harness.
    /// </summary>
    public class LaneGeometryRecord
    {
        /// <summary>The producer-local unique id, from the ECS entity index.</summary>
        public int Id;

        /// <summary>The key into <c>laneprefabs.json</c>.</summary>
        public string Prefab;

        /// <summary>The catalogue entry's category, copied down for filtering without the join.</summary>
        public string Category;

        /// <summary>The owning edge or building's id, or <c>-1</c>.</summary>
        public int Owner = -1;

        /// <summary>The lane's cubic bézier, four world-space control points.</summary>
        public float3 CurveA, CurveB, CurveC, CurveD;

        /// <summary>Indices into the catalogue entry's <c>parts</c> this lane draws.</summary>
        public List<int> Parts = new();

        /// <summary>The uncut curve-parameter spans; empty means the whole curve.</summary>
        public List<float2> Spans = new();

        /// <summary>Width scale at the curve's start and end.</summary>
        public float2 WidthScale = new(1f, 1f);

        /// <summary>Whether the lane hangs, and the end-drop distances when it does.</summary>
        public bool HasHang;
        public float2 HangDistances;

        /// <summary>The resolved paint, as instances carry it (contract §7.2), or <c>null</c>.</summary>
        public object ColorSet;

        /// <summary>Whether the owning network runs below ground — the game draws the lane on its Tunnel cutaway layer.</summary>
        public bool Tunnel;

        /// <summary>
        /// Road wear, raw from <c>Game.Net.LaneCondition.m_Wear</c> (0..10): the serialized sim
        /// state the game turns into the lane decals' deterioration tint in the normal view
        /// (<c>colossal_CurveDeterioration = wear / 10</c> in <c>BatchDataSystem</c> — not the
        /// infoview-only <c>NetCondition.m_Wear</c>). Zero is the common case and is omitted.
        /// </summary>
        public float Wear;
    }
}
