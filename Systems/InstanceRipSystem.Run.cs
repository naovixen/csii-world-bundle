using Carto.Domain;
using Carto.IO;
using Carto.Utils;
using Colossal.Logging;
using Game;
using Game.Objects;
using Game.Prefabs;
using System;
using System.Collections.Generic;
using System.Text;
using Unity.Collections;
using Unity.Entities;
using Unity.Mathematics;

namespace Carto.Systems
{
    /// <summary>
    /// The state one instance rip carries between frames, and the kind filter that selects what
    /// it walks.
    /// </summary>
    public partial class InstanceRipSystem
    {
        /// <summary>
        /// One instance rip in flight.
        /// </summary>
        private class InstanceRun : RipWalk.IRun
        {
            /// <summary>The bundle root, and the record file being written.</summary>
            public string OutputDirectory, Path;

            /// <summary>The placed objects to walk, held across frames (persistent allocator).</summary>
            public NativeArray<Entity> Entities;

            /// <summary>
            /// The two exclusion verdicts, decided once in the pre-pass and indexed by position in
            /// <see cref="Entities"/>.
            /// The pre-pass has to run both tests over every entity to build the asset-id table,
            /// and the walk then needed the same two answers again — 2.6 M redundant probes, each
            /// one an owner read plus a prefab read plus a sub-mesh buffer scan. Caching a bit
            /// each is the whole fix; the walk's own test order is unchanged.
            /// </summary>
            public NativeBitArray ExcludedUtility, ExcludedCargo;

            /// <summary>How far the walk has got.</summary>
            public int Index;

            /// <summary>The record writer, touched only by <see cref="WriterTask"/> once the run starts.</summary>
            public InstanceWriter Writer;

            /// <summary>
            /// Records waiting to be serialised, and the task draining them. Newtonsoft
            /// serialisation dominates the pass — several times the cost of the ECS reads — so
            /// keeping it inside the frame budget multiplied the wall time by the frame rate;
            /// off-thread it costs the main thread nothing (the mesh rip's own pattern).
            /// </summary>
            public System.Collections.Concurrent.BlockingCollection<InstanceRecord> Queue;
            public System.Threading.Tasks.Task WriterTask;

            /// <summary>Hands one record to the writer; held once so the per-entity path allocates no closure.</summary>
            public Action<InstanceRecord> Emit;

            /// <summary>Whether the walk has handed over its last record.</summary>
            public bool ReadingDone;

            /// <summary>Which kinds this run covers.</summary>
            public RipKindFilter Kinds;

            /// <summary>Records excluded by <see cref="Kinds"/>.</summary>
            public int SkippedFiltered;

            /// <summary>The shared asset-id assignment (see <see cref="BeginRip"/>).</summary>
            public Dictionary<Entity, RipAssetIds.AssetIdEntry> AssetIdTable;

            /// <summary>The distinct asset ids referenced.</summary>
            public HashSet<string> AssetIds = new();

            // The tallies the walk keeps.
            public Dictionary<string, int> ByKind = new();
            public int SkippedNoPrefab, WithParent, SkippedUtilityNodes, SkippedVehicleCargo;
            public Dictionary<Entity, int> UtilityNodesByPrefab = new();
            public Dictionary<Entity, int> VehicleCargoByPrefab = new();
            public int StackExpanded, StackRecords;
            public int ProcessFailed, SkippedStale;

            /// <summary>
            /// The entities behind those two counters. Each one is a record the bundle is
            /// missing — mid-rip churn or a per-entity failure — and §10 wants such gaps
            /// declared in <c>warnings.jsonl</c> rather than left to a log line.
            /// </summary>
            public List<int> StaleEntities = new();
            public List<(int Id, string Error)> FailedEntities = new();
            public List<string> SkipSamples = new();
            public HashSet<int> UnresolvedPrefabs = new();
            public HashSet<string> GeometrylessPrefabs = new();

            /// <summary>The report accumulated as the run goes.</summary>
            public StringBuilder Report = new();

            /// <summary>Wall-clock duration of the whole run.</summary>
            public System.Diagnostics.Stopwatch Clock = System.Diagnostics.Stopwatch.StartNew();

            /// <summary>
            /// Where the finish summary goes when a pipeline drives this rip; <c>null</c> means the
            /// run was started by hand and reports through its own dialog.
            /// </summary>
            public Action<RipOutcome> Completed;

            // --- RipWalk.IRun -------------------------------------------------------------
            // Explicit, so the fields above keep their names and their public surface. The frame
            // loop these serve is `Systems/Rip/RipWalk.cs`; it was written out here and in
            // LaneRipSystem identically, comments included.
            int RipWalk.IRun.Index { get => Index; set => Index = value; }

            int RipWalk.IRun.Count => Entities.Length;

            bool RipWalk.IRun.ReadingDone { get => ReadingDone; set => ReadingDone = value; }

            bool RipWalk.IRun.WriterFaulted => WriterTask.IsFaulted;

            bool RipWalk.IRun.WriterCompleted => WriterTask.IsCompleted;

            int RipWalk.IRun.QueueCount => Queue.Count;

            void RipWalk.IRun.CompleteAdding() => Queue.CompleteAdding();
        }

        /// <summary>The rip in flight, or <c>null</c>.</summary>
        private InstanceRun _run;

        /// <summary>
        /// Whether a rip is currently running.
        /// </summary>
        public bool IsRipping => _run != null;

        /// <summary>
        /// How long the walk may hold the main thread each frame. At least one object is always
        /// processed, so an expensive one cannot stall the run. Higher than the mesh rip's 6 ms:
        /// with serialisation off-thread this is pure component reads, and the rip usually runs
        /// behind a paused options page — the smoke test showed 6 ms stretching a seconds-long
        /// pass into minutes.
        /// </summary>
        private const double FrameBudgetMilliseconds = 12d;

        /// <summary>
        /// How many records may wait for the writer before the walk pauses. Records are small;
        /// this bounds memory without ever making the main thread block on the queue.
        /// </summary>
        private const int QueueBackpressure = 8192;
    }
}
