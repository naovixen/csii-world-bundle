using Carto.Domain;
using Carto.IO;
using Carto.Utils;
using Colossal.Logging;
using Game;
using Game.Net;
using System;
using System.Collections.Generic;
using System.Text;
using Unity.Collections;
using Unity.Entities;
using Unity.Mathematics;

namespace Carto.Systems
{
    /// <summary>
    /// The system that writes the drivable graph — every lane, its path and what may use it —
    /// into <c>lanes.jsonl</c>.
    /// <b>Why this is a layer of its own.</b> <c>networks/</c> carries the road surface: where the
    /// asphalt is, what it is made of, how it meets a junction. It says nothing about where a
    /// vehicle may drive, in which direction, or how fast — and a level that only has the surface
    /// is scenery, not a road network. CS2 keeps the graph as plain ECS components with no
    /// readback risk, so this is among the cheapest useful things in the game to extract.
    /// <b>Master and slave lanes.</b> CS2 stores a lane once and derives its neighbours: a
    /// <c>SecondaryLane</c> is generated from a master rather than placed. Both are emitted, the
    /// derived ones marked, because a consumer laying out traffic wants every lane a vehicle can
    /// occupy, while one drawing lane lines may want only the placed ones.
    /// </summary>
    public partial class LaneRipSystem : GameSystemBase
    {
        /// <summary>
        /// Mod's logger.
        /// See <see cref="Instance.Log"/> for more information.
        /// </summary>
        static readonly ILog _log = Instance.Log;

        /// <summary>
        /// The query for placed lanes.
        /// </summary>
        EntityQuery _laneQuery;

        /// <summary>
        /// How finely a lane's bézier is sampled. A lane is a driving line rather than a surface,
        /// so it needs enough points to steer along and no more; eight segments holds a junction
        /// turn to a few centimetres.
        /// </summary>
        private const int Samples = 8;

        /// <summary>
        /// The event triggered when the system instance is created.
        /// </summary>
        protected override void OnCreate()
        {
            _laneQuery = GetEntityQuery(new EntityQueryDesc()
            {
                All = new ComponentType[]
                {
                    ComponentType.ReadOnly<Game.Net.Lane>(),
                    ComponentType.ReadOnly<Curve>()
                },
                None = new ComponentType[]
                {
                    ComponentType.ReadOnly<Game.Common.Deleted>(),
                    ComponentType.ReadOnly<Game.Tools.Temp>()
                }
            });

            base.OnCreate();
            _log.Debug("LaneRipSystem instance created.");
        }

        /// <summary>
        /// The event triggered when the system instance is destroyed.
        /// A run in flight holds a persistent native array and an open file — both must be
        /// released when the world unloads mid-rip.
        /// </summary>
        protected override void OnDestroy()
        {
            LaneRun run = _run;
            _run = null;

            if (run != null)
            {
                _log.Info($"Lane rip abandoned at {run.Index}/{run.Entities.Length} lane(s); the world was unloaded.");
                Abandon(run);
                RipProgress.Clear();
            }

            base.OnDestroy();
        }

        /// <summary>
        /// Advance any rip in progress by one frame's worth of work.
        /// </summary>
        protected override void OnUpdate()
        {
            LaneRun run = _run;
            if (run != null) Advance(run);
        }

        /// <summary>
        /// One lane rip in flight.
        /// </summary>
        private class LaneRun : RipWalk.IRun
        {
            /// <summary>The bundle root, and the record file being written.</summary>
            public string OutputDirectory, Path;

            /// <summary>The lanes to walk, held across frames (persistent allocator).</summary>
            public NativeArray<Entity> Entities;

            /// <summary>How far the walk has got.</summary>
            public int Index;

            /// <summary>The record writer, touched only by <see cref="WriterTask"/> once the run starts.</summary>
            public LaneWriter Writer;

            /// <summary>
            /// Records waiting to be serialised, and the task draining them — serialisation
            /// dominates the pass and costs the main thread nothing off-thread (see
            /// <see cref="InstanceRipSystem"/>, same reasoning).
            /// </summary>
            public System.Collections.Concurrent.BlockingCollection<LaneRecord> Queue;
            public System.Threading.Tasks.Task WriterTask;

            /// <summary>Whether the walk has handed over its last record.</summary>
            public bool ReadingDone;

            // The tallies the walk keeps.
            public Dictionary<string, int> ByKind = new();
            public int Secondary, Degenerate, ProcessFailed, SkippedStale;

            /// <summary>
            /// The entities behind the failed and stale counters. Each is a record the bundle
            /// is missing, and §10 wants such gaps declared in <c>warnings.jsonl</c> rather
            /// than left to a log line.
            /// </summary>
            public List<int> StaleEntities = new();
            public List<(int Id, string Error)> FailedEntities = new();
            public Dictionary<Entity, bool> PrefabHasGeometry = new();
            public Dictionary<Entity, int> DrawnLanesByPrefab = new();
            public int DrawnLanes;

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
            // InstanceRipSystem identically, comments included.
            int RipWalk.IRun.Index { get => Index; set => Index = value; }

            int RipWalk.IRun.Count => Entities.Length;

            bool RipWalk.IRun.ReadingDone { get => ReadingDone; set => ReadingDone = value; }

            bool RipWalk.IRun.WriterFaulted => WriterTask.IsFaulted;

            bool RipWalk.IRun.WriterCompleted => WriterTask.IsCompleted;

            int RipWalk.IRun.QueueCount => Queue.Count;

            void RipWalk.IRun.CompleteAdding() => Queue.CompleteAdding();
        }

        /// <summary>The rip in flight, or <c>null</c>.</summary>
        private LaneRun _run;

        /// <summary>
        /// Whether a rip is currently running.
        /// </summary>
        public bool IsRipping => _run != null;

        /// <summary>
        /// How long the walk may hold the main thread each frame. At least one lane is always
        /// processed, so an expensive one cannot stall the run. 12 ms, not the mesh rip's 6: with
        /// serialisation off-thread this is pure component reads behind a paused options page.
        /// </summary>
        private const double FrameBudgetMilliseconds = 12d;

        /// <summary>
        /// How many records may wait for the writer before the walk pauses.
        /// </summary>
        private const int QueueBackpressure = 8192;

        /// <summary>
        /// Start writing every lane in the city to <c>lanes.jsonl</c>, returning at once; the walk
        /// proceeds a slice per frame in <see cref="OnUpdate"/> (PLAN Phase 8 — the pass used to
        /// block the main thread whole).
        /// </summary>
        /// <param name="outputDirectory">The bundle root to write into.</param>
        /// <param name="completed">Receives the finish summary instead of a dialog when a pipeline drives the rip.</param>
        /// <returns>A short description of what was queued.</returns>
        public string BeginRip(string outputDirectory, Action<RipOutcome> completed = null)
        {
            if (_run != null) return "A lane rip is already running.";

            LaneRun run = new()
            {
                OutputDirectory = outputDirectory,
                Path = IOUtils.CombinePath(outputDirectory, "lanes.jsonl"),
                Entities = _laneQuery.ToEntityArray(Allocator.Persistent),
                Completed = completed
            };

            try
            {
                run.Report.AppendLine($"Lanes: {run.Entities.Length}");
                run.Writer = new LaneWriter(run.Path);
                run.Queue = new System.Collections.Concurrent.BlockingCollection<LaneRecord>();
                run.WriterTask = System.Threading.Tasks.Task.Run(() =>
                {
                    foreach (LaneRecord record in run.Queue.GetConsumingEnumerable())
                        run.Writer.Write(record);
                });
            }
            catch
            {
                Abandon(run);
                throw;
            }

            _run = run;
            RipProgress.Report(RipProgress.Phase.Lanes, 0, run.Entities.Length);

            return $"Walking {run.Entities.Length} lane(s) in the background.";
        }

        /// <summary>
        /// Advance the walk by one frame's worth. The loop itself — budget, back-pressure, the
        /// faulted-writer abort and the per-entity catch — is <see cref="RipWalk.Advance"/>,
        /// which this system and <see cref="InstanceRipSystem"/> once wrote out identically.
        /// </summary>
        /// <param name="run">The run in flight.</param>
        private void Advance(LaneRun run)
        {
            RipWalk.Advance(
                run,
                RipProgress.Phase.Lanes,
                FrameBudgetMilliseconds,
                QueueBackpressure,
                index => ProcessOne(run, run.Entities[index]),
                (index, ex) =>
                {
                    run.ProcessFailed++;
                    run.FailedEntities.Add((run.Entities[index].Index, ex.GetType().Name));
                    if (run.ProcessFailed <= 5) _log.Warn($"Failed to record lane entity {run.Entities[index].Index}: {ex}");
                },
                () => Fail(run),
                () => Finish(run));
        }

        /// <summary>
        /// Handle one lane: census its drawn geometry, then record the graph edge.
        /// Lanes are not only a graph: a lane whose prefab carries a <c>SubMesh</c> buffer is drawn
        /// world geometry — hanging utility cables, fence lanes, hedge rows — assembled along
        /// the lane's curve by <c>BatchInstanceSystem.UpdateLaneInstances</c>. The network rip
        /// bakes that geometry into the <c>lanemeshes/</c> layer (contract §5B); this census stays
        /// as the cross-check — the count here should track what that stage bakes plus what
        /// it deliberately skips (tunnel, pipeline, decal), and a mismatch is a bug.
        /// </summary>
        /// <param name="run">The rip in flight.</param>
        /// <param name="entity">The lane.</param>
        private void ProcessOne(LaneRun run, Entity entity)
        {
            // The snapshot outlives the frame it was taken in; the simulation may remove lanes
            // meanwhile (see InstanceRipSystem.ProcessOne — same churn, same counter).
            //
            if (!EntityManager.Exists(entity) || !EntityManager.HasComponent<Curve>(entity))
            {
                run.SkippedStale++;
                run.StaleEntities.Add(entity.Index);
                return;
            }

            Curve curve = EntityManager.GetComponentData<Curve>(entity);

            if (EntityManager.HasComponent<Game.Prefabs.PrefabRef>(entity))
            {
                Entity lanePrefab = EntityManager.GetComponentData<Game.Prefabs.PrefabRef>(entity).m_Prefab;
                if (lanePrefab != Entity.Null)
                {
                    if (!run.PrefabHasGeometry.TryGetValue(lanePrefab, out bool drawn))
                    {
                        drawn = EntityManager.HasBuffer<Game.Prefabs.SubMesh>(lanePrefab)
                             && (EntityManager.GetBuffer<Game.Prefabs.SubMesh>(lanePrefab, true).Length > 0);
                        run.PrefabHasGeometry[lanePrefab] = drawn;
                    }

                    if (drawn)
                    {
                        run.DrawnLanes++;
                        run.DrawnLanesByPrefab.TryGetValue(lanePrefab, out int seen);
                        run.DrawnLanesByPrefab[lanePrefab] = seen + 1;
                    }
                }
            }

            LaneRecord record = new()
            {
                Id = entity.Index,
                Kind = Classify(entity),
                Length = curve.m_Length,
                Secondary = EntityManager.HasComponent<SecondaryLane>(entity)
            };

            // A lane's owner is the edge or node it belongs to, which is what ties this
            // file to networks.jsonl.
            //
            if (EntityManager.HasComponent<Game.Common.Owner>(entity))
            {
                Entity owner = EntityManager.GetComponentData<Game.Common.Owner>(entity).m_Owner;
                if (owner != Entity.Null) record.Owner = owner.Index;
            }

            Tessellate(curve.m_Bezier, record.Path);
            if (record.Path.Count < 2) { run.Degenerate++; return; }

            AddLaneData(entity, record);

            run.Queue.Add(record);
            if (record.Secondary) run.Secondary++;
            run.ByKind.TryGetValue(record.Kind, out int count);
            run.ByKind[record.Kind] = count + 1;
        }



        /// <summary>
        /// Sample a lane's bézier into a polyline.
        /// </summary>
        /// <param name="curve">The lane's curve.</param>
        /// <param name="points">The polyline to fill.</param>
        private static void Tessellate(Colossal.Mathematics.Bezier4x3 curve, List<float3> points)
        {
            for (int i = 0; i <= Samples; i++)
            {
                float3 point = Colossal.Mathematics.MathUtils.Position(curve, i / (float)Samples);

                // A lane that doubles back on itself produces repeated points; a consumer steering
                // along the path would divide by a zero-length segment.
                //
                if ((points.Count > 0) && (math.distancesq(points[points.Count - 1], point) < 1e-6f)) continue;
                points.Add(point);
            }
        }
    }
}
