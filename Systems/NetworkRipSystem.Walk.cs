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
    /// The stage's frame loop: three lists walked under one shared budget, and the failure paths
    /// out of it.
    /// </summary>
    /// <remarks>
    /// Edges, then orphan nodes, then lane geometry — each with its own index and its own
    /// back-pressure check. That is why this stage keeps its own loop rather than taking
    /// <c>RipWalk</c>'s: a sequence-of-lists abstraction to fit it would be more machinery than
    /// the lines it would save (refactor Phase 7's record).
    /// </remarks>
    public partial class NetworkRipSystem
    {
        /// <summary>
        /// Advance a rip by one frame's budget.
        /// </summary>
        /// <param name="run">The rip in flight.</param>
        private void Advance(NetworkRun run)
        {
            // A faulted encoder never drains the queue, so a plain `Add` on a full one would
            // block the MAIN THREAD for the rest of the session and the stage would never
            // finish. `MeshRipSystem.cs:744` documents the same trap and this is its answer.
            //
            if (run.Encoder.IsFaulted)
            {
                Fail(run);
                return;
            }

            double started = run.Clock.Elapsed.TotalMilliseconds;

            while (run.Index < run.Edges.Length)
            {
                // Back-pressure: pause rather than assemble meshes faster than they are written.
                if (run.Queue.Count >= QueueCapacity) break;

                RipStep(run, run.Edges[run.Index], "edge", RipOne);
                run.Index++;

                if ((run.Clock.Elapsed.TotalMilliseconds - started) >= FrameBudgetMs) break;
            }

            // Orphan caps after the edges, under the same frame budget.
            //
            if (run.Index >= run.Edges.Length)
            {
                while ((run.OrphanIndex < run.Orphans.Length)
                       && ((run.Clock.Elapsed.TotalMilliseconds - started) < FrameBudgetMs))
                {
                    if (run.Queue.Count >= QueueCapacity) break;

                    RipStep(run, run.Orphans[run.OrphanIndex], "orphan node", RipOrphan);
                    run.OrphanIndex++;
                }
            }

            // Lane-drawn geometry last, still under the same budget: hanging utility cables,
            // fence and hedge lanes, bent along their curves the way the game bends them.
            //
            if ((run.Index >= run.Edges.Length) && (run.OrphanIndex >= run.Orphans.Length))
            {
                while ((run.LaneIndex < run.Lanes.Length)
                       && ((run.Clock.Elapsed.TotalMilliseconds - started) < FrameBudgetMs))
                {
                    if (run.Queue.Count >= QueueCapacity) break;

                    RipStep(run, run.Lanes[run.LaneIndex], "lane", RipLaneGeometry);
                    run.LaneIndex++;
                }
            }

            RipProgress.Report(RipProgress.Phase.Networks, run.Index + run.OrphanIndex + run.LaneIndex,
                               run.Edges.Length + run.Orphans.Length + run.Lanes.Length);

            if ((run.Index >= run.Edges.Length) && (run.OrphanIndex >= run.Orphans.Length)
                && (run.LaneIndex >= run.Lanes.Length)) Finish(run);
        }

        /// <summary>
        /// Abort the pass because the encoder task died.
        /// </summary>
        /// <remarks>
        /// The queue is bounded, so an encoder that has faulted stops it draining for good: the
        /// reader would pause on back-pressure for the rest of the session, the stage would never
        /// finish, and a pipeline driving the rip would wait on it forever. There was no such path
        /// here before — <c>MeshRipSystem.Fail</c> has had one since it was written, and this is
        /// the same one, reporting through the same completion-callback-or-dialog shape.
        /// The layers written so far are left on disk: a mixed-vintage bundle is the documented
        /// outcome of a partial rip, and deleting them would lose work the fault did not cost.
        /// </remarks>
        /// <param name="run">The run that cannot continue.</param>
        private void Fail(NetworkRun run)
        {
            _run = null;
            run.Clock.Stop();
            RipProgress.Clear();

            Exception cause = run.Encoder.Exception?.GetBaseException();
            _log.Error($"Network encoder faulted at {run.Index}/{run.Edges.Length} edge(s); aborting the pass: {run.Encoder.Exception}");

            Instance.MeshRip.WarningSink = null;
            try { run.Queue?.CompleteAdding(); } catch { /* already completed */ }
            Instance.MaterialRip.Abandon();
            run.Writer?.Dispose();
            run.LaneWriter?.Dispose();

            string summary = $"FAILED: the network encoder faulted at {run.Index}/{run.Edges.Length} edge(s) — "
                           + $"networks/ and lanemeshes/ are incomplete: {cause?.Message ?? "unknown error"}"
                           + "\n\nFull details: Logs/Carto.log";

            RipReport.Deliver(run.Completed, RipOutcome.Failure(summary), summary, "network rip", _log);
        }

        /// <summary>
        /// Run one per-entity step, counting a throw against the run instead of killing the stage.
        /// </summary>
        /// <remarks>
        /// Without this, one edge that throws — a prefab shape nothing anticipated, a stale
        /// entity that <see cref="EntityManager.Exists"/> did not catch — takes the whole
        /// networks stage down and with it every layer it writes, on a rip that has already
        /// spent twenty minutes. <c>MeshRipSystem.ReadOne</c> has caught per prefab since it was
        /// written; this is the same guarantee per edge, per orphan node and per lane.
        /// </remarks>
        /// <param name="run">The run in progress.</param>
        /// <param name="entity">The entity to process.</param>
        /// <param name="what">What kind of thing it is, for the report line.</param>
        /// <param name="step">The work to do.</param>
        private void RipStep(NetworkRun run, Entity entity, string what, Action<NetworkRun, Entity> step)
        {
            try
            {
                step(run, entity);
            }
            catch (Exception ex)
            {
                run.Failed++;
                run.Report.AppendLine($"  ! {what} {entity.Index}: {ex.GetType().Name} {ex.Message}");
                _log.Warn($"Failed to rip {what} {entity.Index}: {ex}");
            }
        }

        /// <summary>
        /// Hand one item to the bounded queue without wedging on a dead encoder — the rule and
        /// its history are <see cref="RipQueue"/>. What is local: several items are queued per
        /// edge (one per piece mesh, plus every texture the material rip hands over through the
        /// callback at <c>BeginRip</c>), so a throw here is caught by <see cref="RipStep"/> and
        /// counted against that entity.
        /// </summary>
        /// <param name="run">The run in progress.</param>
        /// <param name="item">The item to queue.</param>
        private static void Enqueue(NetworkRun run, NetworkItem item)
            => RipQueue.Enqueue(run.Queue, run.Encoder, "network encoder", item);

        /// <summary>
        /// Rip one network edge into the run.
        /// </summary>
        /// <param name="run">The rip in flight.</param>
        /// <param name="entity">The edge to rip.</param>
        private void RipOne(NetworkRun run, Entity entity)
        {
                {
                    // The work list is captured once and then worked through over many frames, so
                    // the world moves underneath it: the simulation deletes and rebuilds net
                    // entities as it runs, and an edge captured at the start may be gone by the
                    // time its turn comes. Nothing in ECS makes a stale Entity safe to ask about —
                    // it faults inside EntityComponentStore rather than answering false — so the
                    // liveness check has to come first.
                    //
                    if (!EntityManager.Exists(entity))
                    {
                        run.SkippedStale++;
                        return;
                    }

                    Composition composition = EntityManager.GetComponentData<Composition>(entity);
                    Entity edgeComposition = composition.m_Edge;
                    bool isTaxiway = (edgeComposition != Entity.Null)
                                  && EntityManager.HasComponent<TaxiwayComposition>(edgeComposition);

                    // Network markers — pipe and cable junction anchors, and airspace — are the
                    // same invisible simulation furniture the instance rip drops, so the two
                    // layers agree on what "the built environment" means. Taxiways carry the
                    // marker component and stay, as the vector export has them: an airport's
                    // routes are real infrastructure a consumer wants.
                    //
                    // They stay as a LINE, though, and never as a surface — see `markerOnly`
                    // below. The claim this exception used to carry, that taxiways "are genuinely
                    // drawn", is not what the renderer does (§44).
                    //
                    bool markerOnly = EntityManager.HasComponent<Game.Net.Marker>(entity);
                    if (markerOnly && !isTaxiway)
                    {
                        run.SkippedMarker++;
                        return;
                    }

                    if (isTaxiway
                        && ((EntityManager.GetComponentData<TaxiwayComposition>(edgeComposition).m_Flags & TaxiwayFlags.Airspace) != 0))
                    {
                        run.SkippedMarker++;
                        return;
                    }

                    EdgeGeometry geometry = EntityManager.GetComponentData<EdgeGeometry>(entity);
                    Edge edge = EntityManager.GetComponentData<Edge>(entity);
                    float3 center = Colossal.Mathematics.MathUtils.Center(geometry.m_Bounds);

                    float width = GetWidth(edgeComposition, entity, geometry);
                    if (!(width > 0f))
                    {
                        // The schema's centerline form requires a positive width, since a consumer
                        // extrudes by it; a zero-width run is nothing anyone can draw.
                        //
                        run.SkippedNoWidth++;
                        return;
                    }

                    NetworkRecord record = new() { Id = entity.Index, Kind = "segment" };

                    // Mesh form where the network has a cross-section to assemble, centerline form
                    // where it does not. The contract allows a bundle to mix them freely and this
                    // is what that is for: a road, a rail line or a path is built from pieces and
                    // becomes real geometry, while a pipe or a cable has no surface at all and is
                    // only ever a line the consumer extrudes.
                    //
                    // A `Game.Net.Marker` edge belongs on the second side of that line, and the
                    // game says so plainly: `BatchInstanceSystem.UpdateNetInstances` does
                    // `meshLayer &= ~Default; meshLayer |= Marker` for one, and the Marker layer
                    // is drawn only when `RenderingSystem.markersVisible` — the editor, or the
                    // player toggling markers on. There is no taxiway exception anywhere in the
                    // renderer. The one that used to be here came from the vector export, where
                    // it is right, and it does not carry over to geometry: it put 91 opaque
                    // taxiway slabs across the airport, 27,648 triangles the game never draws.
                    //
                    // The surface a player sees at an airport belongs to the lot and the airport
                    // building; the `Invisible Airplane Taxiway` network is the route under it,
                    // and its composition pieces really are named for being invisible.
                    //
                    // Hidden until 2026-08-11 because these pieces' render-cache buffers are
                    // never resident — the game never batches a mesh it does not draw — so
                    // `GetPieceGeometry` returned null and the edge fell to centerline anyway.
                    // `0eeb24f` made the geometry asset the fallback, which is right for a piece
                    // the game does draw and wrong for one it does not, and the slabs appeared.
                    // Deciding it here, on the game's own component, fixes it for every marker
                    // network rather than for the one that was noticed (§44).
                    //
                    string assetId = $"seg_{entity.Index}";
                    MeshAsset assembled = markerOnly
                        ? null
                        : AssembleNetwork(entity, geometry, center, assetId, run.PieceCache, null);
                    if (markerOnly) run.MarkerCenterline++;

                    if (assembled != null)
                    {
                        // Handed to the writer rather than serialised here: assembling the edge has
                        // just queued its pieces' textures, and the .glb can only name them once
                        // they carry a content hash. See NetworkItem.
                        //
                        Enqueue(run, new NetworkItem { Mesh = assembled });
                        record.AssetId = assetId;
                        record.Position = center;
                        record.Rotation = quaternion.identity;
                        run.Triangles += assembled.TriangleCount;
                    }
                    else
                    {
                        List<float3> centerline = BuildCenterline(entity, edge, geometry);
                        if (centerline.Count < 2)
                        {
                            run.SkippedDegenerate++;
                            return;
                        }

                        record.Centerline = centerline;
                        record.Width = width;
                        run.Points += centerline.Count;
                    }

                    AddAttributes(entity, edgeComposition, width, record, run.AggregateNames);

                    run.Writer.Write(record);

                    string category = record.Attributes.TryGetValue("category", out object c) ? c.ToString() : "other";
                    run.ByCategory.TryGetValue(category, out int categoryCount);
                    run.ByCategory[category] = categoryCount + 1;

                    string form = record.Attributes.TryGetValue("form", out object f) ? f.ToString() : "normal";
                    run.ByForm.TryGetValue(form, out int formCount);
                    run.ByForm[form] = formCount + 1;
                }

        }
    }
}
