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
    /// The state one mesh rip carries between frames: its work list, its queue, and the counters
    /// the report reads.
    /// </summary>
    public partial class MeshRipSystem
    {
        /// <summary>
        /// One prefab waiting to be ripped.
        /// </summary>
        private struct WorkItem
        {
            public Entity PrefabEntity;
            public string AssetId;
            public string SourceName;
            public int InstanceCount;
        }

        /// <summary>
        /// One thing waiting to be written: either an assembled prefab or a read-back texture.
        /// <b>Both travel in one queue, drained by one writer, in the order they were produced.</b>
        /// That ordering is not incidental — it is what lets a <c>.glb</c> reference its textures.
        /// A texture's bundle path is the content hash of the PNG it becomes, which is only known
        /// after encoding; the reader resolves and queues a prefab's textures before it queues the
        /// prefab, so a single ordered writer always encodes them first and the mesh can name the
        /// files by the time it is serialised. Two queues, or two writers, would race.
        /// </summary>
        private struct RipItem
        {
            /// <summary>The prefab to serialise, or <c>null</c>.</summary>
            public PrefabMesh Prefab;

            /// <summary>The texture to encode, or <c>null</c>.</summary>
            public PendingTexture Texture;

            /// <summary>A foundation skirt to generate and serialise, or <c>null</c>.</summary>
            public SkirtWork Skirt;
        }

        /// <summary>
        /// One prefab's foundation-skirt job, generated on the writer task so the silhouette scan
        /// costs the main thread nothing.
        /// A prefab part whose <c>MeshData</c> carries <c>MeshFlags.Base</c> draws one extra
        /// generated batch — the skirt between its floor and the terrain (see
        /// <see cref="IO.BaseSkirtBuilder"/>). The band is written as
        /// <c>&lt;assetId&gt;__base.glb</c>, separate from the prefab's own file, because it obeys
        /// a draw rule of its own: a consumer drops its bottom ring to the terrain surface, and
        /// skips it entirely for instances not resting on the ground.
        /// </summary>
        private class SkirtWork
        {
            public string AssetId;
            public string SourceName;
            public int InstanceCount;
            public List<SkirtPart> Parts = new();
        }

        /// <inheritdoc cref="SkirtWork"/>
        private struct SkirtPart
        {
            public MeshAsset Source;
            public float BaseOffset;
            public float3 Position;
            public quaternion Rotation;
            public string NodeName;
            public string MaterialId;
        }

        /// <summary>
        /// The mutable state of a rip in progress.
        /// A full rip reads thousands of prefabs and writes gigabytes, which cannot happen inside a
        /// button handler without freezing the game for minutes. The work is therefore split in two:
        /// reading geometry stays on the main thread — <c>EntityManager</c> and the asset database
        /// permit nothing else — but only for a few milliseconds per frame, while glTF serialisation
        /// and disk writes run on a background task fed through <see cref="Queue"/>.
        /// </summary>
        private class RipRun
        {
            /// <summary>The <c>meshes/</c> directory being written into.</summary>
            public string OutputDirectory;

            /// <summary>The prefabs to rip, richest-covering first.</summary>
            public List<WorkItem> Items;

            /// <summary>How far through <see cref="Items"/> the reader has got.</summary>
            public int Index;

            /// <summary>Assembled prefabs and read-back textures waiting to be written.</summary>
            public BlockingCollection<RipItem> Queue;

            /// <summary>The background serialiser.</summary>
            public Task Writer;

            /// <summary>
            /// The material system, resolved on the main thread and held for the writer.
            /// Looking a system up goes through the ECS world, which is a main-thread affair; the
            /// writer needs the reference but must not be the one to obtain it.
            /// </summary>
            public MaterialRipSystem Materials;

            /// <summary>Whether the reader has finished handing work over.</summary>
            public bool ReadingDone;

            /// <summary>
            /// Counters. The first three belong to the reader on the main thread, the rest to the
            /// writer task — kept apart so neither needs to synchronise with the other.
            /// </summary>
            public int Read, Skipped, ReadFailed;
            public int Written, WriteFailed;
            public long Bytes;

            /// <summary>
            /// Foundation skirts written, and Base-flagged parts whose mesh presented no
            /// silhouette at the base level (a valid outcome — the game generates an empty batch
            /// for those too). Writer-task counters, read after it drains.
            /// </summary>
            public int SkirtsWritten, SkirtsEmpty;

            /// <summary>Everything that did not become a file.</summary>
            public int Failed => ReadFailed + WriteFailed;

            /// <summary>Lines the writer wants in the report.</summary>
            public ConcurrentQueue<string> WriterNotes = new();

            /// <summary>Per-file sizes, for the size breakdown.</summary>
            public ConcurrentBag<KeyValuePair<string, long>> Sizes = new();

            /// <summary>
            /// Every prefab that produced no file, and why.
            /// These asset ids are still referenced by <c>instances.jsonl</c>, so the bundle must
            /// account for them rather than leave a consumer to discover the gap.
            /// </summary>
            public List<RipWarning> SkippedAssets = new();

            /// <summary>The report accumulated on the main thread.</summary>
            public StringBuilder Report = new();

            /// <summary>Wall-clock duration of the whole run.</summary>
            public Stopwatch Clock = Stopwatch.StartNew();

            /// <summary>
            /// Where the finish summary goes when a pipeline drives this rip; <c>null</c> means the
            /// run was started by hand and reports through its own dialog.
            /// </summary>
            public Action<RipOutcome> Completed;
        }
    }
}
