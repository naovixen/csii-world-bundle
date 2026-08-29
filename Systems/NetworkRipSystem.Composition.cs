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
    /// The generator's own preamble and per-piece setup: what one composition and one piece
    /// contribute to every vertex placed from them.
    /// </summary>
    public partial class NetworkRipSystem
    {
        /// <summary>
        /// Everything one COMPOSITION contributes to every piece it holds, read once.
        /// </summary>
        /// <remarks>
        /// The generator's own preamble, as a record rather than eight locals threaded through a
        /// 550-line loop. Nothing here changes once the composition is read, which is what makes
        /// the piece and range walks below safe to read in isolation.
        /// </remarks>
        private sealed class CompositionSetup
        {
            /// <summary>The composition entity, for the §42 probe key.</summary>
            public Entity Composition;

            /// <summary>The composition's own data — width and offsets the maps are built from.</summary>
            public NetCompositionData Data;

            /// <summary>Whether this composition is a node's, and whether it is a roundabout's.</summary>
            public bool IsNode, Roundabout;

            /// <summary>An elevated composition standing clear of the ground; see the note below.</summary>
            public bool ElevatedAwayFromGround;

            /// <summary>The lateral offset of the middle curve from the centre line.</summary>
            public float MiddleOffset;

            /// <summary>
            /// The two half widths, either side of the middle curve. The decompiler called these
            /// <c>num7</c> and <c>num8</c>; the trailing comments in the original said what they
            /// are, and now the names do.
            /// </summary>
            public float LeftHalfWidth, RightHalfWidth;

            /// <summary>The composition's pieces, copied out of the ECS buffer.</summary>
            public NetCompositionPiece[] Pieces;
        }

        /// <summary>
        /// Everything one PIECE contributes to every range and vertex it holds, read once.
        /// </summary>
        private sealed class PieceSetup
        {
            /// <summary>The piece itself, and its authored geometry.</summary>
            public NetCompositionPiece Piece;

            /// <summary>The authored buffers this piece was read from.</summary>
            public PieceGeometry Source;

            /// <summary>The section flags, decoded.</summary>
            public bool Invert, FlipMesh, Median, RightSide, HalfLength;

            /// <summary>The piece flags, decoded.</summary>
            public bool PreserveShape, DisableTiling, LowerBottom, RaiseTop, SmoothTop;

            /// <summary>Which half of the authored tile the cuts remove, and whether x is pinned.</summary>
            public bool FrontCut, BackCut, ZeroWidth;

            /// <summary>The authored shading frame, or null where the piece carries none.</summary>
            public float3[] Normals;

            /// <summary>The authored tangents, or null.</summary>
            public float4[] Tangents;

            /// <summary>Whether this piece ships a shading frame at all.</summary>
            public bool Authored;

            /// <summary>The per-slice height bounds, when any conform or smoothing rule wants them.</summary>
            public PieceHeightBounds HeightBounds;

            /// <summary>Slices per authored metre along the piece.</summary>
            public float BoundsScaleZ;

            /// <summary>Whether terrain conforming is both wanted and available this run.</summary>
            public bool Conforming;

            /// <summary>The §42 section probe row for this piece in this composition.</summary>
            public SectionProbe Section;

            /// <summary>The conform probe row, when this piece conforms.</summary>
            public ConformPieceProbe Probe;

            /// <summary>The game's flip select, as three scalars.</summary>
            public float Sx, Sz, Sw;

            /// <summary>The piece's placement offset, after the half-length shift.</summary>
            public float3 Offset;

            /// <summary>The three map scales — full frame, left half, right half.</summary>
            public float2 Scale4, Scale5, Scale6;

            /// <summary>Their biases.</summary>
            public float2 Bias7, Bias8, Bias9;

            /// <summary>The index read order, reversed under Invert XOR FlipMesh.</summary>
            public int3 Order;
        }

        /// <summary>One material range's window into the piece's arrays.</summary>
        private readonly struct PieceRange
        {
            /// <summary>Where the range starts and how long it runs, in vertices and in indices.</summary>
            public readonly int StartVertex, VertexCount, StartIndex, IndexCount;

            /// <summary>Read one range out of the piece's parallel arrays.</summary>
            /// <param name="source">The piece's authored geometry.</param>
            /// <param name="r">The range index.</param>
            public PieceRange(PieceGeometry source, int r)
            {
                StartVertex = source.RangeStartVertex[r];
                VertexCount = source.RangeVertexCount[r];
                StartIndex = source.RangeStartIndex[r];
                IndexCount = source.RangeIndexCount[r];
            }
        }

        /// <summary>Where one vertex lands in the composition's frame, before the curves are read.</summary>
        private struct VertexMap
        {
            /// <summary>Which two of the composition's curves this vertex lerps between.</summary>
            public int2 Pair;

            /// <summary>The lerp coordinate across, and the curve parameter along.</summary>
            public float XParam, ZParam;

            /// <summary>
            /// How much of the piece's own across/along metre each parameter is worth — the
            /// diagonal of the map's Jacobian, kept per branch because each half frame has its
            /// own width. <c>MapScale.x == 0</c> marks a pinned column, whose across direction
            /// the map collapses.
            /// </summary>
            public float2 MapScale;

            /// <summary>Whether this vertex went through a half frame, and which one.</summary>
            public bool Halved, LeftHalf;
        }

        private void BuildGameComposition(Entity composition, in CompositionCurves p,
                                          float3 center, Dictionary<Entity, MeshAsset> cache,
                                          MeshAssembly assembly)
        {
            CompositionSetup setup = ReadComposition(composition);
            if (setup == null) return;

            foreach (NetCompositionPiece piece in setup.Pieces)
            {
                PieceGeometry source = GetPieceGeometry(piece.m_Piece, cache);
                if (source == null)
                {
                    RecordEmptyPiece(piece);
                    continue;
                }

                PieceSetup pieceSetup = ReadPiece(setup, piece, source);
                for (int r = 0; r < source.RangeMaterial.Length; r++)
                {
                    BuildRange(setup, pieceSetup, new PieceRange(source, r), r, p, center, assembly);
                }
            }
        }

        /// <summary>
        /// The composition's own preamble: the guards, the world-layer skip, and the eight values
        /// every piece is placed against. Null when this composition contributes no mesh.
        /// </summary>
        /// <param name="composition">The composition entity.</param>
        /// <returns>The setup, or null.</returns>
        private CompositionSetup ReadComposition(Entity composition)
        {
            if ((composition == Entity.Null) || !EntityManager.HasComponent<NetCompositionMeshRef>(composition)) return null;
            Entity meshEntity = EntityManager.GetComponentData<NetCompositionMeshRef>(composition).m_Mesh;
            if ((meshEntity == Entity.Null) || !EntityManager.HasBuffer<NetCompositionPiece>(meshEntity)) return null;
            if (!EntityManager.HasComponent<NetCompositionData>(composition)) return null;
            if (SkipUnworldlyLayers(meshEntity)) return null;

            NetCompositionData data = EntityManager.GetComponentData<NetCompositionData>(composition);
            bool roundabout = (data.m_Flags.m_General & CompositionFlags.General.Roundabout) != 0;
            if (roundabout) _roundaboutParts++;

            DynamicBuffer<NetCompositionPiece> buffer = EntityManager.GetBuffer<NetCompositionPiece>(meshEntity, true);
            NetCompositionPiece[] pieces = new NetCompositionPiece[buffer.Length];
            for (int i = 0; i < buffer.Length; i++) pieces[i] = buffer[i];

            return new CompositionSetup
            {
                Composition = composition,
                Data = data,
                IsNode = (data.m_Flags.m_General & CompositionFlags.General.Node) != 0,
                Roundabout = roundabout,
                ElevatedAwayFromGround = IsElevatedAwayFromGround(data),
                MiddleOffset = data.m_MiddleOffset,
                LeftHalfWidth = (data.m_Width * 0.5f) + data.m_MiddleOffset,
                RightHalfWidth = (data.m_Width * 0.5f) - data.m_MiddleOffset,
                Pieces = pieces
            };
        }

        /// <summary>
        /// Whether this composition's mesh draws on no world camera, and so is skipped.
        /// </summary>
        /// <remarks>
        /// The game replaces the Default batch layer with the mesh's own default layers when
        /// they name any (<c>UpdateNetInstances</c>) — and then still draws the mesh on those
        /// layers. <c>CommonUtils.GetBoundsMask</c> names the world-view set: Default, Moving,
        /// Tunnel and Marker are <c>NormalLayers</c>; only Pipeline/SubPipeline/Waterway select
        /// the utility-cutaway and overlay cameras. So the skip is for compositions whose
        /// layers hold no world bit — underground pipe and cable runs — and NOT for Tunnel
        /// compositions, which are the tunnel walls and decks themselves. (An earlier
        /// revision skipped every Default-less composition, which silenced all tunnels.)
        /// </remarks>
        /// <param name="meshEntity">The composition's mesh entity.</param>
        /// <returns>Whether to skip it.</returns>
        private bool SkipUnworldlyLayers(Entity meshEntity)
        {
            if (!EntityManager.HasComponent<NetCompositionMeshData>(meshEntity)) return false;

            const MeshLayer world = MeshLayer.Default | MeshLayer.Moving | MeshLayer.Tunnel | MeshLayer.Marker;
            MeshLayer defaultLayers = EntityManager.GetComponentData<NetCompositionMeshData>(meshEntity).m_DefaultLayers;
            if ((defaultLayers != 0) && ((defaultLayers & world) == 0))
            {
                _pipelineCompositions++;
                return true;
            }
            return false;
        }

        /// <summary>
        /// Whether this composition is an elevated deck standing clear of the ground, whose
        /// pieces must NOT be conformed to it.
        /// </summary>
        /// <remarks>
        /// A deck standing 17 m above grade must not be dragged down to it. Terrain conforming
        /// was gated on PIECE flags alone (LowerBottomToTerrain / RaiseTopToTerrain), and
        /// RaiseTopToTerrain assigns <c>world.y = terrain</c> unconditionally — so any elevated
        /// composition whose pieces carry that flag would have its top edge pinned to the
        /// ground far below. The §38 downward clamp is no protection here: for a deck above
        /// grade <c>terrain &lt; world.y</c> always holds, so clamp and assignment agree.
        /// <para>
        /// Dormant in the city ripped 2026-08-10 (0.07% of elevated vertices sit within 2 cm
        /// of the heightmap, per-segment median 0.000%), so this changes no shipped geometry
        /// there — it closes a latent trap.
        /// </para>
        /// <para>
        /// The exemption is the roadbed walk's own rule, verbatim (<c>TryGetGroundComposition</c>):
        /// an elevated composition with a Low/HighTransition side is where the deck MEETS the
        /// ground, and that end still has to conform or the ramp separates from its approach.
        /// </para>
        /// <para>
        /// <b>This is a producer CHOICE, not a port</b> (stated plainly 2026-08-14, after a
        /// review found no counterpart for it in the game). <c>BatchMeshHelpers</c> sets the
        /// conform classification from <c>NetPieceFlags</c> alone — there is no <c>Elevated</c>
        /// test anywhere in that path — but it does not conform on the CPU at all: it writes the
        /// class into a vertex <c>Color32.b</c> and the SHADER decides what to do with it. CS2
        /// ships its shaders compiled, so what the GPU does with a raise-top vertex 17 m above
        /// its terrain sample is not readable, and the producer has to pick. It picks "leave the
        /// deck where it is", which cannot separate a deck from its own ramp because the
        /// transition sides are exempt. Recorded as a choice so the next person does not read it
        /// as a faithful port of something.
        /// </para>
        /// </remarks>
        /// <param name="data">The composition's data.</param>
        /// <returns>Whether its pieces stand clear of the ground.</returns>
        private static bool IsElevatedAwayFromGround(NetCompositionData data)
        {
            bool elevated = (data.m_Flags.m_General & CompositionFlags.General.Elevated) != 0;
            CompositionFlags.Side conformSides = data.m_Flags.m_Left | data.m_Flags.m_Right;
            return elevated
                && ((conformSides & (CompositionFlags.Side.LowTransition
                                     | CompositionFlags.Side.HighTransition)) == 0);
        }

        /// <summary>
        /// Name a piece whose geometry came back empty.
        /// </summary>
        /// <remarks>
        /// The one exclusion in this walk that said nothing. A composition whose running surface
        /// is dropped here still ships its RoadParts, so the segment looks present and is hollow
        /// — measured on the 2026-08-11 rip, an elevated tram deck carries 12% of the up-facing
        /// area its width and length imply against 72% for an elevated road or train. Name the
        /// piece so the next rip says which one, instead of leaving it to be found by rendering
        /// a viaduct.
        /// </remarks>
        /// <param name="piece">The piece with no geometry.</param>
        private void RecordEmptyPiece(NetCompositionPiece piece)
        {
            _emptyPieces++;
            if (_emptyPieceNames.Count < 64)
            {
                _emptyPieceNames.Add(Instance.Prefab.TryGetPrefab(piece.m_Piece, out PrefabBase emptyPiece)
                                     && (emptyPiece != null)
                    ? emptyPiece.name : $"<entity {piece.m_Piece.Index}>");
            }
        }

    }
}
