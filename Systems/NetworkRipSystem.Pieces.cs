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
    /// One composition piece's authored geometry, read once from the ECS buffers the game itself
    /// generates from.
    /// </summary>
    public partial class NetworkRipSystem
    {
        /// <summary>
        /// One piece's authored geometry, read once from the ECS buffers the game itself
        /// generates from (<c>MeshVertex</c>/<c>MeshUV0</c>/<c>MeshIndex</c>/<c>MeshMaterial</c>),
        /// with each material range's bundle id resolved through the same reader every building
        /// goes through so surfaces land in the material table.
        /// </summary>
        private class PieceGeometry
        {
            public float3[] Vertices;
            public float2[] Uvs;
            public int[] Indices;

            /// <summary>
            /// The authored shading frame (<c>MeshNormal</c>/<c>MeshTangent</c>), or <c>null</c>
            /// when the piece carries no such buffer — the game indexes both unconditionally, so
            /// a piece without them is an asset the game itself could not draw, and its vertices
            /// fall back to the face rebuild.
            /// </summary>
            public float3[] Normals;
            public float4[] Tangents;

            public int[] RangeStartVertex, RangeVertexCount, RangeStartIndex, RangeIndexCount;
            public string[] RangeMaterial;
        }

        private readonly Dictionary<Entity, PieceGeometry> _pieceGeometry = new();

        // ---- terrain conforming (LowerBottomToTerrain / RaiseTopToTerrain) ------------------
        // The game classifies each vertex of a flagged piece against per-z-slice height bounds
        // (`BatchMeshHelpers.InitializeHeightBounds`, 257 slices) and writes the class into the
        // vertex colour; its vertex shader then applies the terrain move per class. The two
        // moves differ (§38): class 1 (LowerBottomToTerrain) is a downward CLAMP — the bottom
        // edge is buried where the terrain is below it and left at road level where it is not,
        // which is what keeps a trench wall standing; class 2 (RaiseTopToTerrain) is an
        // ASSIGNMENT — the top edge is pinned to the terrain surface in both directions, which
        // is how one portal-face asset closes a trench of any depth (§34). Applied here at rip
        // time against the same flattened heightmap the bundle ships, so the meshes arrive
        // closed.
        //

        /// <summary>Per-slice min/max heights of one piece's authored geometry.</summary>
        private sealed class PieceHeightBounds
        {
            public float SizeZ;
            public float[] Min;
            public float[] Max;
        }

        private readonly Dictionary<Entity, PieceHeightBounds> _pieceHeightBounds = new();

        /// <summary>
        /// Per-prefab aggregate of the conforming pass — the §38 probe. One row per flagged
        /// piece in the rip report: how its vertices classified, where the move sent them, and
        /// what the triangle cuts dropped.
        /// </summary>
        private sealed class ConformPieceProbe
        {
            public string Flags;
            public float3 Size;
            public int Vertices;
            public int Class1, Class2;
            public int Lowered, Kept, Raised;
            public int FrontCutTris, BackCutTris, OutOfWindowTris;
        }

        private readonly Dictionary<string, ConformPieceProbe> _conformProbes = new();

        /// <summary>
        /// Per piece, what the cross-section walk actually BUILT — the §42 probe.
        /// <para>"Which piece was read" is already reported; what is missing is how far across
        /// the frame its vertices reached and how big the geometry it produced is. A piece that
        /// is read, placed, and collapses to a ribbon looks identical to a healthy one in every
        /// other count the walk keeps, and that is how an elevated tram came to ship its
        /// structure with no running surface (§42).</para>
        /// <para>The companion half-frame reach probe is gone: §41.9's short flare was never a
        /// piece-to-frame mapping fault, and the reach it measured is answered offline by
        /// `tools/test_node_fan.py` against the shipped meshes (§43).</para>
        /// <para><b>This one stays permanently</b> (the open PROBE question, settled 2026-08-12).
        /// It was kept where its companion was dropped because the two numbers it reports —
        /// authored piece size against the extent that piece produced — are not derivable from
        /// the bundle: the authored size never ships, so no offline gate can ask the question at
        /// all. What it costs was the real objection, and that is now paid once per distinct
        /// piece rather than once per placement; see <see cref="PieceKey"/>.</para>
        /// </summary>
        private sealed class SectionProbe
        {
            public string Name;                  // the piece prefab's name
            public string Where;                 // "node" or "edge"
            public float3 Size;                  // authored piece size
            public float OffsetX;                // authored cross-section offset
            public int Composition;              // the composition entity this row is for
            public int Placements, Vertices;
            public float XMin = float.MaxValue, XMax = float.MinValue;
            public float3 Lo, Hi;                // emitted extent, relative to composition centre
            public bool HasBounds;

            /// <summary>The report's row label. Carries the composition because one piece now
            /// gets one row PER composition — without it two rows would print identically and a
            /// per-composition collapse would read as a duplicate rather than as a finding.</summary>
            public string Label => $"{Name}|{Where}|comp{Composition}";
        }

        /// <summary>
        /// What identifies a probe row: the piece prefab, <b>the composition it was placed in</b>,
        /// and which half of the network. <b>Keyed by entity, not by name, and that is the
        /// point</b> — the probe runs inside the per-placement loop of the mesh stage, the longest
        /// stage of the rip, so resolving a prefab name and interpolating a string there charged
        /// the whole city for a diagnostic that only ever needed the name once per distinct piece.
        /// <para>
        /// <b>The composition is in the key because the defect it hunts is per-composition</b>
        /// <i>(added 2026-08-14)</i>. The extents accumulate as `world - centre` against the
        /// CURRENT segment's centre, so unioning a piece's placements across compositions unions
        /// different local frames: a piece that collapses to a ribbon on the elevated tram deck
        /// and builds normally on 200 at-grade segments produced a healthy union extent and a
        /// full `xParam` span, and neither clause fired. That is exactly the §42 shape — same
        /// piece, different composition — the probe exists to catch.
        /// </para>
        /// </summary>
        private readonly struct PieceKey : IEquatable<PieceKey>
        {
            private readonly Entity _piece;
            private readonly Entity _composition;
            private readonly bool _isNode;

            public PieceKey(Entity piece, Entity composition, bool isNode)
            {
                _piece = piece;
                _composition = composition;
                _isNode = isNode;
            }

            public bool Equals(PieceKey other) => (_piece == other._piece)
                                               && (_composition == other._composition)
                                               && (_isNode == other._isNode);
            public override bool Equals(object obj) => (obj is PieceKey other) && Equals(other);
            public override int GetHashCode()
            {
                int hash = _piece.GetHashCode() * 397;
                hash = (hash ^ _composition.GetHashCode()) * 397;
                return hash ^ (_isNode ? 1 : 0);
            }
        }
    }
}
