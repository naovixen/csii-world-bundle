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
    /// Where one vertex lands on the composition's curves, and the shading frame that map
    /// implies for it.
    /// </summary>
    public partial class NetworkRipSystem
    {
        /// <summary>
        /// Which two curves this vertex lerps between, and where along and across them.
        /// </summary>
        /// <param name="setup">The composition.</param>
        /// <param name="ps">The piece.</param>
        /// <param name="vertex">The placed vertex; a median piece may pin its x.</param>
        /// <returns>The map.</returns>
        private static VertexMap MapVertex(CompositionSetup setup, PieceSetup ps, ref float3 vertex)
        {
            VertexMap map = default;
            if (!setup.IsNode)
            {
                map.Pair = math.select(new int2(0, 2), new int2(1, 3), vertex.z >= 0f);
                map.XParam = (vertex.x * ps.Scale4.x) + ps.Bias7.x;
                map.ZParam = (vertex.z * ps.Scale4.y) + ps.Bias7.y;
                if (vertex.z >= 0f) map.ZParam -= 1f;
                map.MapScale = ps.Scale4;
                return map;
            }

            if (ps.PreserveShape)
            {
                map.Pair = new int2(6, 7);
                map.XParam = (vertex.x * ps.Scale4.x) + ps.Bias7.x;
                map.ZParam = (vertex.z * ps.Scale4.y) + ps.Bias7.y;
                map.MapScale = ps.Scale4;
                return map;
            }

            if (ps.Median)
            {
                // dx keeps the AUTHORED spread even for a zero-width piece — the game computes it
                // before pinning x. The pin only moves the lerp coordinate, so a zero-width
                // intersection piece routes its side columns into the half frames where the
                // pinned x lands them ON the inner boundary curves, while its centre column rides
                // the middle to the junction centre: a three-rail ruled surface that paves the
                // centre wedge. Treating zero width as all-centre — this port's previous reading,
                // and audit §8's — collapsed the piece to a zero-area ribbon and left every
                // median-less-looking junction wedge open (audit §18; the `* Intersection Piece 0`
                // sections are exactly these).
                //
                float dx = vertex.x - ps.Piece.m_Offset.x;
                if (ps.ZeroWidth) vertex.x = ps.Piece.m_Offset.x;
                if (math.abs(dx) < 0.01f)
                {
                    // The game does NOT rebase the centre column's front half (its roundabout
                    // side branches subtract `int8 - base`, this one has no subtraction): the
                    // final saturate pins those vertices at the middle curve's end. Matched
                    // exactly.
                    //
                    map.Pair = setup.Roundabout ? math.select(new int2(4, 2), new int2(5, 3), vertex.z >= 0f)
                                                : new int2(4, 4);
                    map.XParam = 0f;
                    map.ZParam = (vertex.z * ps.Scale4.y) + ps.Bias7.y;
                    map.MapScale = new float2(0f, ps.Scale4.y);
                    return map;
                }
                if (dx > 0f) return RightHalfMap(setup, ps, vertex);
                return LeftHalfMap(setup, ps, vertex);
            }

            return ps.RightSide ? RightHalfMap(setup, ps, vertex) : LeftHalfMap(setup, ps, vertex);
        }

        /// <summary>The right half frame — the map a Right or positive-dx node vertex takes.</summary>
        /// <param name="setup">The composition.</param>
        /// <param name="ps">The piece.</param>
        /// <param name="vertex">The placed vertex.</param>
        /// <returns>The map.</returns>
        private static VertexMap RightHalfMap(CompositionSetup setup, PieceSetup ps, float3 vertex)
        {
            VertexMap map = default;
            map.Pair = setup.Roundabout ? math.select(new int2(4, 2), new int2(5, 3), vertex.z >= 0f)
                                        : new int2(1, 3);
            map.XParam = (vertex.x * ps.Scale6.x) + ps.Bias9.x;
            map.ZParam = (vertex.z * ps.Scale6.y) + ps.Bias9.y;
            if (setup.Roundabout && (vertex.z >= 0f)) map.ZParam -= 1f;
            map.MapScale = ps.Scale6;
            map.Halved = true;
            map.LeftHalf = false;
            return map;
        }

        /// <summary>The left half frame — the map every other node vertex takes.</summary>
        /// <param name="setup">The composition.</param>
        /// <param name="ps">The piece.</param>
        /// <param name="vertex">The placed vertex.</param>
        /// <returns>The map.</returns>
        private static VertexMap LeftHalfMap(CompositionSetup setup, PieceSetup ps, float3 vertex)
        {
            VertexMap map = default;
            map.Pair = setup.Roundabout ? math.select(new int2(0, 4), new int2(1, 5), vertex.z >= 0f)
                                        : new int2(0, 2);
            map.XParam = (vertex.x * ps.Scale5.x) + ps.Bias8.x;
            map.ZParam = (vertex.z * ps.Scale5.y) + ps.Bias8.y;
            if (setup.Roundabout && (vertex.z >= 0f)) map.ZParam -= 1f;
            map.MapScale = ps.Scale5;
            map.Halved = true;
            map.LeftHalf = true;
            return map;
        }

        /// <summary>
        /// Move a classified vertex onto the terrain.
        /// </summary>
        /// <remarks>
        /// LowerBottomToTerrain buries the bottom edge: the shader move is a downward clamp,
        /// never a lift — a trench wall's base stays at road level (§38: assignment collapsed
        /// 10 m walls onto the terrain lip). RaiseTopToTerrain pins the top edge to the terrain
        /// surface in both directions — the §34-validated portal stretch, unchanged.
        /// </remarks>
        /// <param name="ps">The piece.</param>
        /// <param name="conform">1 for lower-bottom, 2 for raise-top.</param>
        /// <param name="world">The placed vertex, moved in place.</param>
        private void ApplyConform(PieceSetup ps, int conform, ref float3 world)
        {
            float terrain = SampleConformHeight(world);
            if (conform == 1)
            {
                if (terrain < world.y) { world.y = terrain; _conformLoweredVerts++; ps.Probe.Lowered++; }
                else { _conformKeptVerts++; ps.Probe.Kept++; }
            }
            else
            {
                world.y = terrain;
                _conformRaisedVerts++;
                ps.Probe.Raised++;
            }
        }

        /// <summary>
        /// What this piece actually built, in the frame's own coordinate and in metres.
        /// </summary>
        /// <remarks>
        /// A collapsed piece has a healthy vertex count and a degenerate extent; a short half
        /// frame has a healthy extent and an xParam that never reaches 1.
        /// </remarks>
        /// <param name="section">The §42 probe row.</param>
        /// <param name="world">The placed vertex.</param>
        /// <param name="center">The mesh origin.</param>
        /// <param name="xParam">The across coordinate this vertex reached.</param>
        private static void RecordSectionExtent(SectionProbe section, float3 world, float3 center, float xParam)
        {
            float3 relative = world - center;
            section.Vertices++;
            section.XMin = math.min(section.XMin, xParam);
            section.XMax = math.max(section.XMax, xParam);
            if (section.HasBounds)
            {
                section.Lo = math.min(section.Lo, relative);
                section.Hi = math.max(section.Hi, relative);
            }
            else
            {
                section.Lo = section.Hi = relative;
                section.HasBounds = true;
            }
        }

        /// <summary>
        /// The shading frame, rotated by the very map that placed the vertex.
        /// </summary>
        /// <remarks>
        /// Position is <c>lerp(A(z), B(z), x) + (0, y, 0)</c>, so the Jacobian in the piece's own
        /// metres is [across, up, along]: across = (B − A) per across-metre, up = (0,1,0),
        /// along = the two curves' derivatives blended at the same x, per along-metre. The normal
        /// takes the inverse transpose — the cofactor, which is that matrix times a determinant a
        /// normalize removes — and the tangent takes the plain Jacobian with <c>w</c> passed
        /// through, which is what keeps a stretched kerb's bevel at its authored angle where a
        /// rotation-only frame would shear it. APPROXIMATION (counted): the game's vertex shader
        /// is handed only these curves, so it can build no other frame, but whether it
        /// orthonormalizes before rotating is shader-side and unknowable from the C# side; the two
        /// coincide wherever the frame is orthogonal, which is everywhere the road is not both
        /// flared and banked.
        /// </remarks>
        /// <param name="ps">The piece.</param>
        /// <param name="map">Where this vertex landed.</param>
        /// <param name="a">The first curve's point at this z.</param>
        /// <param name="b">The second curve's point at this z.</param>
        /// <param name="normal">The flipped authored normal.</param>
        /// <param name="tangent">The flipped authored tangent.</param>
        /// <param name="p">The composition's curves.</param>
        /// <param name="assembly">The shared assembly lists.</param>
        private void ShadeVertex(PieceSetup ps, in VertexMap map, float3 a, float3 b,
                                 float3 normal, float4 tangent, in CompositionCurves p,
                                 MeshAssembly assembly)
        {
            if (!ps.Authored)
            {
                // No authored buffer: leave the zero the finisher rebuilds from faces.
                //
                assembly.Normals.Add(default);
                assembly.Tangents.Add(default);
                assembly.Rebuilt++;
                return;
            }

            float3 across = (b - a) * map.MapScale.x;
            float3 up = new(0f, 1f, 0f);
            float3 along = math.lerp(Derivative(p.Curves[map.Pair.x], map.ZParam),
                                     Derivative(p.Curves[map.Pair.y], map.ZParam), map.XParam) * map.MapScale.y;

            float3 rotated = (normal.x * math.cross(up, along))
                           + (normal.y * math.cross(along, across))
                           + (normal.z * math.cross(across, up));
            float3 rotatedTangent = (tangent.x * across) + (tangent.y * up) + (tangent.z * along);

            // A pinned column (the median centre, and any zero-width piece) maps its whole across
            // direction onto one curve, so the frame has no across column to take a cofactor of.
            // Fall back to the rotation the remaining two columns determine, and count it.
            //
            if (math.lengthsq(rotated) < 1e-12f)
            {
                float3 alongDirection = NormalizeOr(along, new float3(0f, 0f, 1f));
                float3 acrossDirection = NormalizeOr(math.cross(up, alongDirection), new float3(1f, 0f, 0f));
                rotated = (normal.x * acrossDirection) + (normal.y * up) + (normal.z * alongDirection);
                rotatedTangent = (tangent.x * acrossDirection) + (tangent.y * up) + (tangent.z * alongDirection);
                _pinnedFrameVerts++;
            }

            float3 shaded = NormalizeOr(rotated, new float3(0f, 1f, 0f));
            assembly.Normals.Add(shaded);

            // A piece with normals but no tangent buffer is normal-mapped by nothing, so any
            // perpendicular serves — the same one a consumer would generate for itself, chosen
            // here so the attribute is never a zero vector a renderer would divide by.
            //
            assembly.Tangents.Add((ps.Tangents != null)
                ? new float4(NormalizeOr(rotatedTangent, new float3(1f, 0f, 0f)), tangent.w)
                : AnyTangent(shaded));
        }
    }
}
