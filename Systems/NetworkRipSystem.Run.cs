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
    /// The state one network rip carries between frames, and the layers it writes into.
    /// </summary>
    public partial class NetworkRipSystem
    {
        /// <summary>
        /// One unit of work for the writer: a segment mesh to serialise, or a texture to encode.
        /// <b>Both share one queue, drained in order by one writer.</b> A texture's bundle path is
        /// the content hash of the PNG it becomes, which is not known until it has been encoded, so
        /// a <c>.glb</c> can only name its textures if they were written first. An edge resolves and
        /// queues its pieces' textures while it is being assembled — before the assembled mesh is
        /// queued — so a single ordered writer always encodes them first. Writing the <c>.glb</c>
        /// inline on the main thread instead, as this once did, races the encoder and loses: the
        /// material is named but every map resolves to <c>null</c>, and the segment opens
        /// untextured.
        /// </summary>
        private struct NetworkItem
        {
            /// <summary>The assembled segment mesh to serialise, or <c>null</c>.</summary>
            public MeshAsset Mesh;

            /// <summary>The texture to encode, or <c>null</c>.</summary>
            public PendingTexture Texture;

            /// <summary>Whether <see cref="Mesh"/> is baked lane geometry, written under <c>lanemeshes/</c> rather than <c>networks/</c>.</summary>
            public bool IsLane;
        }

        /// <summary>
        /// A network rip in flight, carried between frames.
        /// </summary>
        private class NetworkRun
        {
            public Entity[] Edges;
            public int Index;
            public Entity[] Orphans;
            public int OrphanIndex;
            public NetworkWriter Writer;
            public string OutputDirectory;
            public string MeshDirectory;
            public StringBuilder Report = new();
            public Dictionary<Entity, MeshAsset> PieceCache = new();
            public Dictionary<Entity, string> AggregateNames = new();
            public Dictionary<string, int> ByCategory = new();
            public Dictionary<string, int> ByForm = new();
            public System.Diagnostics.Stopwatch Clock = System.Diagnostics.Stopwatch.StartNew();
            public int SkippedMarker, SkippedNoWidth, SkippedDegenerate, SkippedStale;

            /// <summary>Entities whose rip threw and was counted rather than killing the stage.</summary>
            public int Failed;
            public int MarkerCenterline;
            public int Points, Triangles;
            public long Bytes;
            public BlockingCollection<NetworkItem> Queue;
            public Task Encoder;
            public List<RipWarning> Skipped = new();

            /// <summary>Whether decal parts — road markings, wear — enter the lane catalogue.</summary>
            public bool IncludeLaneDecals = true;

            /// <summary>
            /// Where the finish summary goes when a pipeline drives this rip; <c>null</c> means the
            /// run was started by hand and reports through its own dialog.
            /// </summary>
            public Action<RipOutcome> Completed;

            // ---- The lane-geometry stage (bundle layer `lanemeshes/`). ----
            //
            /// <summary>The lane entities to consider, and how far the stage has got.</summary>
            public Entity[] Lanes = Array.Empty<Entity>();
            public int LaneIndex;

            /// <summary>The <c>lanemeshes.jsonl</c> writer and the <c>lanemeshes/</c> directory.</summary>
            public NetworkWriter LaneWriter;
            public string LaneMeshDirectory;

            /// <summary>Per-prefab drawable lane parts, read once and referenced per lane.</summary>
            public Dictionary<Entity, LanePrefabEntry> LaneParts = new();

            /// <summary>The <c>laneprefabs.json</c> catalogue under construction, keyed as written.</summary>
            public Dictionary<string, object> LaneCatalogue = new();

            /// <summary>Part-mesh asset ids assigned over the whole run — the uniqueness the join key requires.</summary>
            public Dictionary<Entity, string> LaneMeshIds = new();
            public HashSet<string> LaneIdsUsed = new();

            /// <summary>
            /// What each part mesh's decal is — <c>marking</c> or <c>wear</c>, keyed by asset id,
            /// absent where its materials do not say. See <see cref="ClassifyDecalRole"/>.
            /// </summary>
            public Dictionary<string, string> LaneDecalRoles = new();
            public Dictionary<string, int> LaneDecalsByRole = new();

            /// <summary>Lane-stage counters, in report order.</summary>
            public long LaneBytes;
            public int SkippedLanePipeline, SkippedLaneDecal, SkippedLaneStale, SkippedLaneDegenerate;
            public int SkippedLaneDecalUnclassified;
            public int SkippedLaneExcluded, LaneHanging, LaneTunnel, LaneWorn;
            public Dictionary<string, int> LanesByCategory = new();

            /// <summary>
            /// The material system, resolved on the main thread and held for the writer.
            /// Looking a system up goes through the ECS world, which is a main-thread affair; the
            /// writer needs the reference but must not be the one to obtain it.
            /// </summary>
            public MaterialRipSystem Materials;
        }

        /// <summary>
        /// The arrays one assembled network mesh is accumulated into — every composition instance
        /// of one edge (its two node flares and the run between them) appends to the same set, and
        /// <see cref="Finish(string, MeshAssembly)"/> packs them.
        /// Carried as one object rather than six parameters because the shading frame joined them:
        /// a normal and a tangent are written in the very loop that writes the position, so that
        /// the bend is evaluated once per vertex and not once per attribute.
        /// </summary>
        private class MeshAssembly
        {
            public readonly List<float3> Vertices = new();
            public readonly List<float2> Uvs = new();
            public readonly List<int> Indices = new();
            public readonly List<MeshPrimitive> Primitives = new();

            /// <summary>The authored shading frame, rotated into the bend.</summary>
            public readonly List<float3> Normals = new();
            public readonly List<float4> Tangents = new();

            /// <summary>
            /// How many vertices came from a piece with no authored normal buffer and so hold a
            /// zero placeholder for <see cref="Finish(string, MeshAssembly)"/> to rebuild.
            /// </summary>
            public int Rebuilt;
        }

        /// <summary>
        /// One straight sub-mesh of a lane prefab as the per-lane stage needs it: the selection
        /// flags the game tests per lane, and the part's index in the prefab's catalogue entry.
        /// The game assembles lane geometry the same way it assembles a road: the prefab's
        /// <c>SubMesh</c> parts are tiled along the lane's bézier and deformed onto it in the
        /// vertex shader (<c>BatchInstanceSystem.UpdateLaneInstances</c> chooses the parts and the
        /// tile count, <c>BatchDataSystem.UpdateLaneData</c> hands the shader the curve's control
        /// points). The producer ships that same parameter set — straight meshes once, bend
        /// inputs per lane — and the consumer bends (contract §5B v2).
        /// </summary>
        private class LanePartRef
        {
            public SubMeshFlags Flags;
            public int Index;
        }

        /// <summary>
        /// One lane prefab's drawable parts and prefab-level bend inputs, read once and referenced
        /// by every lane record that uses the prefab.
        /// </summary>
        private class LanePrefabEntry
        {
            public string Key;
            public string Category = "lane";
            public float Hanging;
            public List<LanePartRef> Parts = new();
        }
    }
}
