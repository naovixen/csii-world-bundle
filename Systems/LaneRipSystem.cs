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
        private class LaneRun
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
            public Action<string> Completed;
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
        public string BeginRip(string outputDirectory, Action<string> completed = null)
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
        /// Advance a rip by one frame's budget.
        /// </summary>
        /// <param name="run">The rip in flight.</param>
        private void Advance(LaneRun run)
        {
            if (!run.ReadingDone)
            {
                // A faulted writer never drains the queue: the walk would pause on back-pressure
                // for the rest of the session, ReadingDone would never turn true, and a pipeline
                // driving the rip would wait on this stage forever. Abort the pass instead.
                //
                if (run.WriterTask.IsFaulted)
                {
                    Fail(run);
                    return;
                }

                System.Diagnostics.Stopwatch budget = System.Diagnostics.Stopwatch.StartNew();

                while (run.Index < run.Entities.Length)
                {
                    // Back-pressure: pause the walk rather than pile records up faster than the
                    // writer serialises them.
                    if (run.Queue.Count >= QueueBackpressure) break;

                    // One bad lane must not cost the rest of the graph its records.
                    //
                    try
                    {
                        ProcessOne(run, run.Entities[run.Index]);
                    }
                    catch (Exception ex)
                    {
                        run.ProcessFailed++;
                        run.FailedEntities.Add((run.Entities[run.Index].Index, ex.GetType().Name));
                        if (run.ProcessFailed <= 5) _log.Warn($"Failed to record lane entity {run.Entities[run.Index].Index}: {ex}");
                    }

                    run.Index++;

                    if (budget.Elapsed.TotalMilliseconds >= FrameBudgetMilliseconds) break;
                }

                RipProgress.Report(RipProgress.Phase.Lanes, run.Index, run.Entities.Length);

                if (run.Index >= run.Entities.Length)
                {
                    run.ReadingDone = true;
                    run.Queue.CompleteAdding();
                }

                return;
            }

            if (run.WriterTask.IsCompleted) Finish(run);
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
        /// Close the file, report, and hand the summary to whoever is listening.
        /// </summary>
        /// <param name="run">The finished run.</param>
        private void Finish(LaneRun run)
        {
            _run = null;
            run.Clock.Stop();
            RipProgress.Clear();

            bool writerFaulted = false;
            try
            {
                run.WriterTask.Wait();
            }
            catch (Exception ex)
            {
                writerFaulted = true;
                _log.Warn($"Lane writer ended with an error: {ex}");
            }

            StringBuilder report = run.Report;

            // The orchestrator counts a stage failed by its summary's FAILED prefix. A writer
            // that died after the walk finished lost queued records, so the stage must not
            // read as a success.
            //
            if (writerFaulted) report.Insert(0, "FAILED: the lane writer ended with an error — lanes.jsonl is incomplete; see the log.\n");
            report.AppendLine($"Wrote {run.Writer.Count} lane(s) to lanes.jsonl in {run.Clock.Elapsed.TotalSeconds:F1} s");
            foreach (KeyValuePair<string, int> pair in run.ByKind) report.AppendLine($"  {pair.Key}: {pair.Value}");
            report.AppendLine($"  {run.Secondary} derived from a master lane");
            if (run.Degenerate > 0) report.AppendLine($"Skipped {run.Degenerate} lane(s) with no usable curve");
            if (run.ProcessFailed > 0) report.AppendLine($"FAILED to record {run.ProcessFailed} lane(s) — see the log");
            if (run.SkippedStale > 0) report.AppendLine($"Skipped {run.SkippedStale} lane(s) the simulation removed mid-rip");

            if (run.DrawnLanes > 0)
            {
                int drawnPrefabs = 0;
                foreach (KeyValuePair<Entity, bool> pair in run.PrefabHasGeometry) if (pair.Value) drawnPrefabs++;

                report.AppendLine($"Lane geometry cross-check: {run.DrawnLanes} lane(s) over "
                                + $"{drawnPrefabs} prefab(s) draw a mesh along their curve "
                                + "(baked by the network rip into lanemeshes/, minus its counted skips). Largest:");

                List<KeyValuePair<Entity, int>> ranked = new(run.DrawnLanesByPrefab);
                ranked.Sort((a, b) => b.Value.CompareTo(a.Value));
                for (int i = 0; (i < 10) && (i < ranked.Count); i++)
                {
                    string name = Instance.Name.GetPrefabName(ranked[i].Key);
                    report.AppendLine($"  {(string.IsNullOrEmpty(name) ? $"prefab {ranked[i].Key.Index}" : name),-40} {ranked[i].Value,8}");
                }
            }

            int laneCount = run.Writer.Count;
            Abandon(run);

            // Unconditional, matching the instance pass: a rip with nothing to declare still
            // has to clear the lane declarations a previous one left.
            //
            WriteWarnings(run, report);

            _log.Info(report.ToString());

            if (run.Completed != null)
            {
                try { run.Completed(report.ToString()); }
                catch (Exception ex) { _log.Warn($"Lane rip completion callback failed: {ex}"); }
                return;
            }

            try
            {
                string summary = (writerFaulted ? "FAILED: the writer ended with an error — the file is incomplete.\n" : "")
                               + $"Wrote {laneCount} lane record(s) in {run.Clock.Elapsed.TotalSeconds:F1} s.\n\nFull report: Logs/Carto.log";
                Game.UI.MessageDialog dialog = new("Options.SECTION[Carto.Carto.Mod]", summary, "Common.OK");
                Instance.UI.appBindings.ShowMessageDialog(dialog, null);
            }
            catch (Exception ex)
            {
                _log.Warn($"Could not show the lane rip result dialog: {ex.Message}");
            }
        }

        /// <summary>
        /// Abort a rip whose writer task has died mid-walk. Nothing more can ever be written —
        /// the queue will not drain again — so the only honest outcome is a failed stage:
        /// release what the run holds and hand a summary with the <c>FAILED</c> prefix, which
        /// is what the orchestrator counts a stage failure by, to whoever is listening.
        /// </summary>
        /// <param name="run">The rip whose writer faulted.</param>
        private void Fail(LaneRun run)
        {
            _run = null;
            run.Clock.Stop();
            RipProgress.Clear();

            Exception cause = run.WriterTask.Exception?.GetBaseException();
            _log.Error($"Lane writer faulted at {run.Index}/{run.Entities.Length} lane(s); aborting the pass: {run.WriterTask.Exception}");

            int written = run.Writer?.Count ?? 0;
            Abandon(run);

            string summary = $"FAILED: the lane writer faulted at {run.Index}/{run.Entities.Length} lane(s) — "
                           + $"lanes.jsonl is incomplete ({written} record(s) before the fault): "
                           + $"{cause?.Message ?? "unknown error"}\n\nFull details: Logs/Carto.log";

            if (run.Completed != null)
            {
                try { run.Completed(summary); }
                catch (Exception ex) { _log.Warn($"Lane rip completion callback failed: {ex}"); }
                return;
            }

            try
            {
                Game.UI.MessageDialog dialog = new("Options.SECTION[Carto.Carto.Mod]", summary, "Common.OK");
                Instance.UI.appBindings.ShowMessageDialog(dialog, null);
            }
            catch (Exception ex)
            {
                _log.Warn($"Could not show the lane rip result dialog: {ex.Message}");
            }
        }

        /// <summary>
        /// Release what a run holds open — the record file and the entity array.
        /// </summary>
        /// <param name="run">The run to release.</param>
        private static void Abandon(LaneRun run)
        {
            // Let the writer drain what it holds before the file is closed under it.
            //
            bool drained = true;
            try { run.Queue?.CompleteAdding(); } catch { /* already completed */ }
            try { drained = run.WriterTask?.Wait(TimeSpan.FromSeconds(10)) ?? true; }
            catch { /* completed with an error — done either way; logged by the Finish paths */ }

            // A writer that has not stopped may still be inside a WriteLine, and closing the
            // stream under it risks a torn record. On timeout the stream is deliberately left
            // to the finalizer instead of being disposed under a live thread.
            //
            if (drained)
            {
                try { run.Writer?.Dispose(); } catch { /* already closed */ }
            }
            else
            {
                _log.Warn("Lane writer still running after 10 s; leaving lanes.jsonl open rather than closing it mid-write.");
            }

            run.Writer = null;
            if (run.Entities.IsCreated) run.Entities.Dispose();
        }

        /// <summary>
        /// Declare this pass's gaps in the bundle's <c>warnings.jsonl</c> — one line per lane
        /// the simulation removed mid-rip or that failed to record, so a missing graph edge is
        /// explicable rather than mysterious (contract §10). The same ownership-merge scheme as
        /// the instance and mesh passes: only <c>kind: "lane"</c> lines are this pass's to
        /// replace, so the passes can run in any order without erasing each other.
        /// </summary>
        /// <param name="run">The finished run.</param>
        /// <param name="report">The report to note the outcome in.</param>
        private void WriteWarnings(LaneRun run, StringBuilder report)
        {
            int kept = WarningsFile.Rewrite(
                run.OutputDirectory,
                line => WarningsFile.IsKind(line, WarningsFile.LaneKinds),
                LaneDeclarations(run));
            if (kept < 0) return;

            report.AppendLine($"Wrote {WarningsFile.FileName} ({run.StaleEntities.Count + run.FailedEntities.Count} lane entries: "
                            + $"{run.StaleEntities.Count} removed mid-rip, {run.FailedEntities.Count} failed), "
                            + $"keeping {kept} from the other passes");
        }

        /// <summary>
        /// This pass's §10 declarations, in write order. Both kinds name an ENTITY rather than an
        /// asset: a lane the simulation removed mid-rip no longer resolves to a prefab to ask for.
        /// </summary>
        /// <param name="run">The finished run.</param>
        /// <returns>One record per gap.</returns>
        private static IEnumerable<Dictionary<string, object>> LaneDeclarations(LaneRun run)
        {
            foreach (int entityId in run.StaleEntities)
            {
                yield return WarningsFile.ById("lane", entityId,
                    "removed by the simulation mid-rip; no record written");
            }

            foreach ((int entityId, string error) in run.FailedEntities)
            {
                yield return WarningsFile.ById("lane", entityId,
                    $"failed to record ({error}); no record written — see the log");
            }
        }

        /// <summary>
        /// What may use this lane.
        /// </summary>
        /// <param name="entity">The lane.</param>
        /// <returns>The contract's lane kind.</returns>
        private string Classify(Entity entity)
        {
            if (EntityManager.HasComponent<CarLane>(entity)) return "car";
            if (EntityManager.HasComponent<TrackLane>(entity)) return "track";
            if (EntityManager.HasComponent<PedestrianLane>(entity)) return "pedestrian";
            if (EntityManager.HasComponent<ParkingLane>(entity)) return "parking";
            if (EntityManager.HasComponent<ConnectionLane>(entity)) return "connection";
            return "other";
        }

        /// <summary>
        /// Carry across whatever the lane's own component knows: speed, bendiness and flags.
        /// The speed limit is stored in metres per second and published in km/h, matching the
        /// <c>limit</c> that <c>networks.jsonl</c> already carries so the two agree.
        /// </summary>
        /// <param name="entity">The lane.</param>
        /// <param name="record">The record to fill.</param>
        private void AddLaneData(Entity entity, LaneRecord record)
        {
            if (EntityManager.HasComponent<CarLane>(entity))
            {
                CarLane lane = EntityManager.GetComponentData<CarLane>(entity);
                record.SpeedLimit = lane.m_SpeedLimit * 3.6f;
                record.Curviness = lane.m_Curviness;
                AddFlags(record, lane.m_Flags.ToString());
                return;
            }

            if (EntityManager.HasComponent<TrackLane>(entity))
            {
                TrackLane lane = EntityManager.GetComponentData<TrackLane>(entity);
                record.SpeedLimit = lane.m_SpeedLimit * 3.6f;
                record.Curviness = lane.m_Curviness;
                AddFlags(record, lane.m_Flags.ToString());
                return;
            }

            if (EntityManager.HasComponent<PedestrianLane>(entity))
            {
                AddFlags(record, EntityManager.GetComponentData<PedestrianLane>(entity).m_Flags.ToString());
                return;
            }

            if (EntityManager.HasComponent<ParkingLane>(entity))
            {
                AddFlags(record, EntityManager.GetComponentData<ParkingLane>(entity).m_Flags.ToString());
            }
        }

        /// <summary>
        /// Split a flags enum's rendered name into the contract's array of names.
        /// Names rather than a bit field: the numbers are CS2's internal values and would silently
        /// change meaning between game versions, where a name that disappears is a name a consumer
        /// simply stops seeing.
        /// </summary>
        /// <param name="record">The record to add to.</param>
        /// <param name="flags">The enum's <c>ToString</c>.</param>
        private static void AddFlags(LaneRecord record, string flags)
        {
            if (string.IsNullOrEmpty(flags) || (flags == "0")) return;

            foreach (string name in flags.Split(','))
            {
                string trimmed = name.Trim();
                if (trimmed.Length > 0) record.Flags.Add(trimmed);
            }
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
