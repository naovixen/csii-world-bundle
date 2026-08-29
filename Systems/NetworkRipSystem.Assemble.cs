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
    /// One segment's assembled mesh: the encode hand-off, the drain, and the finisher that turns
    /// the accumulated vertex lists into a <c>MeshAsset</c>.
    /// </summary>
    /// <remarks>
    /// <c>BuildNormals</c> is the face rebuild for the pieces that ship no authored frame, and
    /// <c>VertexSync</c> is the across-width knot table the game hands its vertex shader (§5.1)
    /// — the thing that keeps a kerb on the kerb line through a flare.
    /// </remarks>
    public partial class NetworkRipSystem
    {
        /// <summary>
        /// Encode every queued texture and serialise every queued segment mesh, in the order the
        /// rip produced them, until it stops queueing.
        /// </summary>
        /// <param name="run">The rip in flight.</param>
        private static void Encode(NetworkRun run)
        {
            MaterialRipSystem materials = run.Materials;

            foreach (NetworkItem item in run.Queue.GetConsumingEnumerable())
            {
                // One bad item must not take the writer down with it: the queue would then never
                // drain, and the rip would block forever waiting for it.
                //
                try
                {
                    // Textures first, always: they arrive before the segment that named them, and
                    // encoding one is what gives it the content hash that segment's .glb writes down.
                    //
                    if (item.Texture != null)
                    {
                        materials.WriteTexture(item.Texture);
                        continue;
                    }

                    MeshAsset mesh = item.Mesh;
                    if (mesh == null) continue;

                    string path = IOUtils.CombinePath(item.IsLane ? run.LaneMeshDirectory : run.MeshDirectory,
                                                      $"{mesh.AssetId}.glb");
                    long written = GltfWriter.Write(path, mesh, materials.Describe);
                    if (item.IsLane) System.Threading.Interlocked.Add(ref run.LaneBytes, written);
                    else run.Bytes += written;
                }
                catch (Exception ex)
                {
                    _log.Warn($"Could not write a network {(item.Texture != null ? "texture" : "mesh")}: {ex.Message}");
                }
            }
        }

        /// <summary>
        /// Close the writer's queue, wait for it, and write <c>materials.json</c>.
        /// </summary>
        /// <param name="run">The run being finished or abandoned.</param>
        /// <returns>The material summary, or <c>null</c>.</returns>
        private string Drain(NetworkRun run)
        {
            try
            {
                run.Queue?.CompleteAdding();
                run.Encoder?.Wait();
            }
            catch (Exception ex)
            {
                _log.Warn($"Network writer ended with an error: {ex.Message}");
            }

            try
            {
                return Instance.MaterialRip.Finish(run.Report, run.Skipped);
            }
            catch (Exception ex)
            {
                _log.Error($"Material rip failed to finish: {ex}");
                return null;
            }
        }

        /// <summary>
        /// Assemble one network edge into a single mesh: the junction flare at its start, the run
        /// between the nodes, and the flare at its end.
        /// <c>Composition</c> names three composition entities and each has its own cross-section,
        /// because an intersection's surface is genuinely different geometry from the road leading
        /// into it. Assembling all three covers node to node with nothing left between them, which
        /// is what a constant-width ribbon could never do at a skewed junction.
        /// </summary>
        /// <param name="entity">The edge entity.</param>
        /// <param name="geometry">Its edge geometry.</param>
        /// <param name="center">The origin the mesh is built around.</param>
        /// <param name="assetId">The asset id to label it with.</param>
        /// <param name="cache">The piece mesh cache.</param>
        /// <param name="report">Where to note the working, or <c>null</c> to stay quiet.</param>
        /// <returns>The mesh, or <c>null</c> when the network has no pieces to assemble.</returns>
        private MeshAsset AssembleNetwork(Entity entity, EdgeGeometry geometry, float3 center, string assetId,
                                          Dictionary<Entity, MeshAsset> cache, StringBuilder report)
        {
            Composition parts = EntityManager.GetComponentData<Composition>(entity);

            MeshAssembly assembly = new();

            // The game's own draw gates (`BatchInstanceSystem.UpdateNetInstances`): an edge part
            // needs more than 0.1 m of run, a flare more than 0.05 m on either half. Below that
            // the game draws nothing, and assembling it here would emit degenerate slivers the
            // game never shows. Geometry itself is the ported game generator
            // (NetworkRipSystem.GameMesh.cs): parameters exactly as `BatchDataHelpers` builds
            // them, vertices exactly as `GenerateCompositionMesh` maps them, evaluated on CPU.
            //
            if (EntityManager.HasComponent<StartNodeGeometry>(entity))
            {
                EdgeNodeGeometry node = EntityManager.GetComponentData<StartNodeGeometry>(entity).m_Geometry;
                if (math.any(node.m_Left.m_Length > 0.05f) | math.any(node.m_Right.m_Length > 0.05f))
                {
                    BuildNodeSide(parts.m_StartNode, node, center, cache, assembly);
                }
            }

            if (math.any((geometry.m_Start.m_Length + geometry.m_End.m_Length) > 0.1f)
                && (parts.m_Edge != Entity.Null)
                && EntityManager.HasComponent<NetCompositionMeshRef>(parts.m_Edge))
            {
                bool rotated = EntityManager.GetComponentData<NetCompositionMeshRef>(parts.m_Edge).m_Rotate;
                if (rotated) _rotatedEdges++;
                CompositionCurves curves = EdgeCurves(geometry, rotated);
                BuildGameComposition(parts.m_Edge, curves, center, cache, assembly);
            }

            if (EntityManager.HasComponent<EndNodeGeometry>(entity))
            {
                EdgeNodeGeometry node = EntityManager.GetComponentData<EndNodeGeometry>(entity).m_Geometry;
                if (math.any(node.m_Left.m_Length > 0.05f) | math.any(node.m_Right.m_Length > 0.05f))
                {
                    BuildNodeSide(parts.m_EndNode, node, center, cache, assembly);
                }
            }

            // No junction patch of any kind: the arms' node meshes tile the interior themselves —
            // median pieces on the middle curve plus the zero-width intersection piece's
            // three-rail wedge (audit §18) — so a patch could only double geometry the mapping
            // already emits. The bespoke fan this once carried invented geometry the game does
            // not have, and shipped it wrongly shaded.
            //
            return Finish(assetId, assembly);
        }

        /// <summary>
        /// The across-width vertex remap CS2 applies to a junction flare.
        /// A composition names four positions across each half of its cross-section
        /// (<c>NetCompositionData.m_SyncVertexOffsets*</c>), and a node geometry names where those
        /// four have to end up in this particular junction
        /// (<c>EdgeNodeGeometry.m_SyncVertexTargets*</c>). The game hands both to the vertex shader
        /// as <c>m_CompositionSync0..3</c> and lets it move each vertex across the width to match —
        /// which is how a lane edge still lines up with the lane it meets when four roads of
        /// different widths share a junction.
        /// The knots are measured per half, from the outer edge to the median, which is the
        /// coordinate the assembly's own <c>s</c> already runs in — <c>CalculateSyncVertexOffsets</c>
        /// normalises by <c>(x - middleOffset) / (width / 2)</c>, and every real composition
        /// measured has <c>middleOffset</c> zero. Both outermost knots are fixed points in the
        /// measured data, so this moves lane boundaries within a flare and never its outer kerb or
        /// its median: it is not what closes a junction, it is what stops the pieces inside one
        /// from stepping.
        /// The interpolation between knots is the shader's, and CS2 ships shaders compiled inside
        /// asset bundles, so it is read here as piecewise linear — the reading the measured knots
        /// support and the only one that keeps every knot exactly on its target. Outside the
        /// outermost knots the shift is held constant rather than extrapolated, so a composition
        /// whose knots do not span the full half cannot throw a vertex past the kerb.
        /// </summary>
        private readonly struct VertexSync
        {
            private readonly float4 _offsetsLeft, _targetsLeft, _offsetsRight, _targetsRight;
            private readonly bool _active;

            /// <summary>No remap — what an edge, and any composition without sync data, gets.
            ///</summary>
            public static VertexSync None => default;

            public VertexSync(NetCompositionData composition, EdgeNodeGeometry node)
            {
                _offsetsLeft = composition.m_SyncVertexOffsetsLeft;
                _targetsLeft = node.m_SyncVertexTargetsLeft;
                _offsetsRight = composition.m_SyncVertexOffsetsRight;
                _targetsRight = node.m_SyncVertexTargetsRight;

                // An unset target arrives as NaN, and one NaN would propagate into every vertex of
                // the flare. Where the two agree there is nothing to do anyway, so treat that as
                // inactive too and keep the cost off the ordinary node.
                //
                _active = !math.any(math.isnan(_offsetsLeft)) && !math.any(math.isnan(_targetsLeft))
                        && !math.any(math.isnan(_offsetsRight)) && !math.any(math.isnan(_targetsRight))
                        && (math.any(_offsetsLeft != _targetsLeft)
                            || math.any(_offsetsRight != _targetsRight));
            }

            /// <summary>
            /// Move one across-half position from where the cross-section authored it to where this
            /// junction needs it.
            /// Each half's knots live in that half's own <c>xParam</c> coordinate — left: 0 at the
            /// outer edge, 1 at the middle; right: 0 at the middle, 1 at the outer edge. That is
            /// the coordinate `GeometrySystem` builds them in: the twin conversion is
            /// `offsetsRight = 1 − offsetsLeft.wzyx` (line 2603) and
            /// `CalculateVertexSyncTarget`'s right branch anchors on `1 − a.w` (the outer-right
            /// margin), returning targets ascending toward the outer edge. No flip is applied
            /// here — an earlier revision flipped the right half through <c>1 − s</c>, which
            /// dragged every right-side curb knot to a mirrored position and jutted kerbs into
            /// the roadway at junction flares.
            /// </summary>
            /// <param name="s">The position across this half, in the half's own coordinate.
            ///</param>
            /// <param name="leftHalf">Whether this is the left half.</param>
            /// <returns>The remapped position.</returns>
            public float Apply(float s, bool leftHalf)
            {
                if (!_active) return s;
                return leftHalf ? Remap(s, _offsetsLeft, _targetsLeft)
                                : Remap(s, _offsetsRight, _targetsRight);
            }

            // The end regions scale with pinned endpoints (0 → 0, 1 → 1) rather than translate:
            // the flare's boundary curves were already moved to their targets when
            // `CalculateVertexSyncTarget` mutated the node geometry (`m_Left/m_Right.c/d`), so
            // the vertex riding the boundary at s = 0 must stay on it; a translation shears the
            // outer strip — the curb row — off the boundary the game keeps it on.
            //
            private static float Remap(float s, float4 o, float4 t)
            {
                if (s <= o.x) return Between(s, 0f, o.x, 0f, t.x);
                if (s <= o.y) return Between(s, o.x, o.y, t.x, t.y);
                if (s <= o.z) return Between(s, o.y, o.z, t.y, t.z);
                if (s <= o.w) return Between(s, o.z, o.w, t.z, t.w);
                return Between(s, o.w, 1f, t.w, 1f);
            }

            // Two knots at the same offset are a collapsed interval, not a slope; snapping to the
            // far target keeps the map monotone instead of dividing by zero.
            //
            private static float Between(float s, float o0, float o1, float t0, float t1)
                => ((o1 - o0) > 1e-6f) ? math.lerp(t0, t1, (s - o0) / (o1 - o0)) : t1;
        }

        /// <summary>
        /// Finish a shared mesh: pack the accumulated lists, and fill in the shading frame of any
        /// vertex whose piece carried no authored one.
        /// </summary>
        /// <param name="assetId">The asset id to label it with.</param>
        /// <param name="assembly">The accumulated arrays.</param>
        /// <returns>The mesh, or <c>null</c> when nothing was assembled.</returns>
        private static MeshAsset Finish(string assetId, MeshAssembly assembly)
        {
            if (assembly.Vertices.Count == 0) return null;

            MeshAsset mesh = new()
            {
                AssetId = assetId,
                SourceName = assetId,
                Vertices = assembly.Vertices.ToArray(),
                Uvs = assembly.Uvs.ToArray(),
                Indices = assembly.Indices.ToArray(),
                Primitives = assembly.Primitives,
                Normals = assembly.Normals.ToArray(),
                Tangents = assembly.Tangents.ToArray()
            };

            // Normals are the piece's own, rotated into the bend by the map that placed the vertex
            // (NetworkRipSystem.GameMesh.cs) — the game's authored creases survive, which the face
            // rebuild bevelled away. A piece with no normal buffer leaves a zero here, and only
            // those vertices take the rebuild.
            //
            if (assembly.Rebuilt > 0)
            {
                float3[] rebuilt = BuildNormals(mesh.Vertices, mesh.Indices);
                for (int i = 0; i < mesh.Normals.Length; i++)
                {
                    if (math.lengthsq(mesh.Normals[i]) > 1e-12f) continue;
                    mesh.Normals[i] = rebuilt[i];
                    mesh.Tangents[i] = AnyTangent(rebuilt[i]);
                }
            }

            return mesh;
        }

        /// <summary>
        /// A unit tangent perpendicular to a normal, for a vertex whose piece authored none.
        /// glTF's <c>TANGENT</c> is per-mesh, not per-primitive, so a mesh that mixes authored and
        /// rebuilt pieces still needs a value here; any perpendicular is as good as the arbitrary
        /// one a consumer would generate.
        /// </summary>
        /// <param name="normal">The unit normal.</param>
        /// <returns>The tangent, handedness <c>+1</c>.</returns>
        private static float4 AnyTangent(float3 normal)
        {
            float3 helper = (math.abs(normal.y) < 0.99f) ? new float3(0f, 1f, 0f) : new float3(1f, 0f, 0f);
            float3 tangent = math.cross(helper, normal);
            float length = math.length(tangent);
            return new float4((length > 1e-9f) ? (tangent / length) : new float3(1f, 0f, 0f), 1f);
        }
    }
}
