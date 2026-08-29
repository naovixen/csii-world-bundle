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
    /// One piece's flags, probes, conform decision and coordinate maps, read once before any of
    /// its vertices are placed.
    /// </summary>
    public partial class NetworkRipSystem
    {
        /// <summary>
        /// One piece's flags, shading frame, conform decision and coordinate maps, read once.
        /// </summary>
        /// <param name="setup">The composition this piece belongs to.</param>
        /// <param name="piece">The piece.</param>
        /// <param name="source">Its authored geometry.</param>
        /// <returns>The piece setup every range and vertex below is placed against.</returns>
        private PieceSetup ReadPiece(CompositionSetup setup, NetCompositionPiece piece, PieceGeometry source)
        {
            PieceSetup ps = ReadPieceFlags(setup, piece, source);
            ps.Section = ProbeSection(setup, piece);
            ResolveConform(setup, ps, piece);
            ComputeMaps(setup, ps, piece);
            return ps;
        }

        /// <summary>
        /// The section and piece flags, and the authored shading frame.
        /// </summary>
        /// <param name="setup">The composition this piece belongs to.</param>
        /// <param name="piece">The piece.</param>
        /// <param name="source">Its authored geometry.</param>
        /// <returns>A partly-filled setup; the caller adds the probes and the maps.</returns>
        private PieceSetup ReadPieceFlags(CompositionSetup setup, NetCompositionPiece piece, PieceGeometry source)
        {
            PieceSetup ps = new PieceSetup
            {
                Piece = piece,
                Source = source,
                Invert = (piece.m_SectionFlags & NetSectionFlags.Invert) != 0,
                FlipMesh = (piece.m_SectionFlags & NetSectionFlags.FlipMesh) != 0,
                Median = (piece.m_SectionFlags & NetSectionFlags.Median) != 0,
                RightSide = (piece.m_SectionFlags & NetSectionFlags.Right) != 0,
                HalfLength = (piece.m_SectionFlags & NetSectionFlags.HalfLength) != 0,
                PreserveShape = (piece.m_PieceFlags & NetPieceFlags.PreserveShape) != 0,
                DisableTiling = (piece.m_PieceFlags & NetPieceFlags.DisableTiling) != 0,
                BackCut = (piece.m_PieceFlags & NetPieceFlags.SkipBottomHalf) != 0,
                ZeroWidth = piece.m_Size.x == 0f,
                LowerBottom = (piece.m_PieceFlags & NetPieceFlags.LowerBottomToTerrain) != 0,
                RaiseTop = (piece.m_PieceFlags & NetPieceFlags.RaiseTopToTerrain) != 0,
                SmoothTop = (piece.m_PieceFlags & NetPieceFlags.SmoothTopNormal) != 0
            };
            ps.FrontCut = (setup.IsNode && !setup.Roundabout && !ps.PreserveShape) || ps.HalfLength;
            ps.Order = math.select(new int3(0, 1, 2), new int3(2, 1, 0), ps.Invert != ps.FlipMesh);

            if (ps.PreserveShape) _railPieces++;
            if (ps.BackCut) _skipBottomPieces++;
            if (ps.LowerBottom) _lowerBottomPieces++;
            if (ps.RaiseTop) _raiseTopPieces++;
            if (ps.SmoothTop) _smoothTopPieces++;

            // Authored shading frame, or the face rebuild for a piece that carries none.
            //
            ps.Normals = source.Normals;
            ps.Tangents = source.Tangents;
            ps.Authored = ps.Normals != null;
            if (ps.Authored) _authoredFramePieces++;
            else _rebuiltFramePieces++;
            return ps;
        }

        /// <summary>
        /// The §42 probe row for this piece in this composition: one row per piece, keyed before
        /// the vertex loop so the loop only accumulates. Resolving the name is the expensive half
        /// and happens on first sight of a piece, never per placement.
        /// </summary>
        /// <param name="setup">The composition this piece belongs to.</param>
        /// <param name="piece">The piece.</param>
        /// <returns>The probe row, with this placement already counted.</returns>
        private SectionProbe ProbeSection(CompositionSetup setup, NetCompositionPiece piece)
        {
            PieceKey sectionKey = new PieceKey(piece.m_Piece, setup.Composition, setup.IsNode);
            if (!_sectionProbes.TryGetValue(sectionKey, out SectionProbe section))
            {
                string sectionName = Instance.Prefab.TryGetPrefab(piece.m_Piece, out PrefabBase sectionPiece)
                                     && (sectionPiece != null)
                    ? sectionPiece.name : $"<entity {piece.m_Piece.Index}>";
                _sectionProbes[sectionKey] = section = new SectionProbe
                {
                    Name = sectionName,
                    Where = setup.IsNode ? "node" : "edge",
                    Composition = setup.Composition.Index,
                    Size = piece.m_Size,
                    OffsetX = piece.m_Offset.x
                };
            }
            section.Placements++;
            return section;
        }

        /// <summary>
        /// Whether this piece conforms to the terrain, and the height bounds the classification
        /// needs.
        /// </summary>
        /// <remarks>
        /// The per-slice height bounds serve two rules, so they are built for either
        /// (<c>BatchMeshHelpers</c> line 682: <c>flag17 || flag18 || flag19</c>). Terrain
        /// conforming classifies each vertex against them and applies the vertex shader's terrain
        /// move at rip time — the portal face over every tunnel mouth is a RaiseTopToTerrain
        /// piece, and without it the mouth stands open above the arch. SmoothTopNormal needs no
        /// terrain at all: it only replaces the authored normal at the top bound.
        /// </remarks>
        /// <param name="setup">The composition this piece belongs to.</param>
        /// <param name="ps">The piece setup to fill.</param>
        /// <param name="piece">The piece.</param>
        private void ResolveConform(CompositionSetup setup, PieceSetup ps, NetCompositionPiece piece)
        {
            if (ps.LowerBottom || ps.RaiseTop || ps.SmoothTop)
            {
                ps.HeightBounds = GetPieceHeightBounds(piece.m_Piece, ps.Source, piece.m_Size.z);
                ps.BoundsScaleZ = 256f / math.max(piece.m_Size.z, 0.001f);
            }

            // The gate applies to `RaiseTop` ONLY (narrowed 2026-08-14). The argument for
            // it is about a TOP being dragged down to a ground the deck stands clear of; it
            // says nothing about a bottom reaching down to meet the ground, and the game
            // tags the two independently. Suppressing both left an elevated composition's
            // abutment and retaining pieces floating.
            //
            bool wantsConform = ps.LowerBottom || (ps.RaiseTop && !setup.ElevatedAwayFromGround);
            if (wantsConform)
            {
                // N-F2: a heightmap that cannot be captured used to leave the piece silently
                // unconformed while the report still claimed it was conformed. Counted here,
                // declared in warnings.jsonl and stated honestly in the report.
                //
                ps.Conforming = EnsureConformTerrain();
                if (!ps.Conforming) _unconformedPieces++;
            }

            if (ps.RaiseTop && setup.ElevatedAwayFromGround)
            {
                // Deliberately not raised: an elevated composition clear of the ground.
                // Counted separately from `_unconformedPieces`, which means "we wanted to
                // and could not" — this one means "we must not".
                _elevatedUnconformedPieces++;
            }

            if (!ps.Conforming) return;

            string probeName = Instance.Name.GetPrefabName(piece.m_Piece);
            if (!_conformProbes.TryGetValue(probeName, out ConformPieceProbe probe))
            {
                _conformProbes[probeName] = probe = new ConformPieceProbe
                {
                    Flags = (ps.LowerBottom && ps.RaiseTop) ? "LowerBottom+RaiseTop"
                          : ps.LowerBottom ? "LowerBottom" : "RaiseTop",
                    Size = piece.m_Size
                };
            }
            ps.Probe = probe;
        }

        /// <summary>
        /// The flip select, the placement offset, and the three coordinate maps.
        /// </summary>
        /// <remarks>
        /// <para>
        /// The game's flip select — <c>select(float4(-1,1,-1,1), float4(1,1,1,-1),
        /// bool4(Invert, false, FlipMesh, Invert != FlipMesh))</c>: authored pieces are x- and
        /// z-negated by default; Invert restores x, FlipMesh restores z. The <c>w</c> term flips
        /// tangent handedness, and now that the authored frame is shipped it is applied as the
        /// game applies it (<c>tangent *= float3</c>, all four components).
        /// </para>
        /// <para>
        /// The generator computes two more per-vertex quantities here that this port does not:
        /// <c>v.z</c> — the piece's OWN cross-anchor in the same xParam coordinate the position
        /// lerp uses (<c>offset.x/width + 0.5</c> on an edge or a rail, the half-frame forms
        /// <c>1 + (offset.x − middleOffset)/leftHalf</c> and
        /// <c>(offset.x − middleOffset)/rightHalf</c> at a node, 0 and 1 at a roundabout's island
        /// and outer edge) — and <c>v.w</c>, the pre-rebase along coordinate scaled by 0.5 on an
        /// edge, 20 at a node and 40 at a roundabout. Both go into the vertex's <c>UV0.zw</c> and
        /// are read only by the compiled vertex shader, which is not decompilable, so what they
        /// drive is unknown. They cannot be a position term: <c>m_Position</c> is written from
        /// <c>vertex</c> alone, and this port evaluates the same map on the CPU and bakes the
        /// result. So they are deliberately not computed — a value assigned to nothing reads as a
        /// half-finished port, and this comment is the honest form of it. (Re-derived from
        /// <c>GenerateCompositionMesh</c> 2026-08-17, audit §53.)
        /// </para>
        /// </remarks>
        /// <param name="setup">The composition this piece belongs to.</param>
        /// <param name="ps">The piece setup to fill.</param>
        /// <param name="piece">The piece.</param>
        private static void ComputeMaps(CompositionSetup setup, PieceSetup ps, NetCompositionPiece piece)
        {
            ps.Sx = ps.Invert ? 1f : -1f;
            ps.Sz = ps.FlipMesh ? 1f : -1f;
            ps.Sw = (ps.Invert != ps.FlipMesh) ? -1f : 1f;

            ps.Offset = piece.m_Offset;
            ps.Scale4 = new(1f / setup.Data.m_Width, 1f / (piece.m_Size.z * 0.5f));
            ps.Scale5 = new(1f / setup.LeftHalfWidth, 1f / (piece.m_Size.z * 0.5f));
            ps.Scale6 = new(1f / setup.RightHalfWidth, 1f / (piece.m_Size.z * 0.5f));
            ps.Bias7 = new(0.5f, 1f);
            ps.Bias8 = new(1f - (setup.MiddleOffset / setup.LeftHalfWidth), 1f);
            ps.Bias9 = new((0f - setup.MiddleOffset) / setup.RightHalfWidth, 1f);

            if (setup.IsNode && ps.PreserveShape)
            {
                float railScale = 0.5f * piece.m_Size.z / setup.Data.m_Width;
                ps.Scale4.y *= railScale;
                ps.Bias7.y *= railScale;
            }
            else if (setup.Roundabout)
            {
                // Deliberately empty, and load-bearing. The game's branch here sets only the
                // `v.z` cross-anchor described above — but the branch itself must stay,
                // because being an `else if` is what stops a HalfLength piece inside a
                // roundabout composition from taking the halving below. That is the game's own
                // else-if chain, and deleting the branch would silently change which pieces
                // get `Sz *= 2`.
                //
            }
            else if (ps.HalfLength)
            {
                ps.Sz *= 2f;
                ps.Offset.z += 0.5f * piece.m_Size.z;
            }
        }
    }
}
