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
    /// The frame loop and the failure path out of it.
    /// </summary>
    /// <remarks>
    /// This stage keeps its own loop rather than taking <c>RipWalk</c>'s: it reports
    /// <c>Phase.Write</c> progress on every frame of the DRAIN, which that loop does not, and
    /// its per-item catch sits inside <c>ReadOne</c> rather than in the loop (refactor Phase 7's
    /// record).
    /// </remarks>
    public partial class MeshRipSystem
    {
        /// <summary>
        /// Read one frame's worth of prefabs, then finish the run once the writer has drained.
        /// </summary>
        /// <param name="run">The run in progress.</param>
        private void Advance(RipRun run)
        {
            if (!run.ReadingDone)
            {
                // A faulted writer never drains the queue: the reader would pause on
                // back-pressure for the rest of the session, ReadingDone would never turn true,
                // and a pipeline driving the rip would wait on this stage forever. Abort instead.
                //
                if (run.Writer.IsFaulted)
                {
                    Fail(run);
                    return;
                }

                // Land the readbacks the previous frames issued before reading more.
                run.Materials.Pump();

                Stopwatch budget = Stopwatch.StartNew();

                while (run.Index < run.Items.Count)
                {
                    // Back-pressure: pause reading rather than pile assembled meshes up in memory
                    // faster than they can be written.
                    if (run.Queue.Count >= QueueCapacity) break;

                    ReadOne(run, run.Items[run.Index]);
                    run.Index++;

                    if (budget.Elapsed.TotalMilliseconds >= FrameBudgetMilliseconds) break;
                }

                RipProgress.Report(RipProgress.Phase.Meshes, run.Index, run.Items.Count);

                if (run.Index >= run.Items.Count)
                {
                    // Every readback still in flight is waited for now: nothing after this frame
                    // pumps, and an encoder waiting on one would wait forever.
                    run.Materials.Pump(force: true);
                    run.ReadingDone = true;
                    run.Queue.CompleteAdding();
                }

                return;
            }

            RipProgress.Report(RipProgress.Phase.Write, System.Threading.Volatile.Read(ref run.Written), run.Read);
            if (run.Writer.IsCompleted) Finish(run);
        }

        /// <summary>
        /// Assemble one prefab and hand it to the writer.
        /// </summary>
        /// <param name="run">The run in progress.</param>
        /// <param name="item">The prefab to read.</param>
        private void ReadOne(RipRun run, WorkItem item)
        {
            try
            {
                PrefabMesh prefab = BuildPrefabMesh(item, out string skipReason, out SkirtWork skirt, out List<PrefabMesh> variants);

                if (prefab == null)
                {
                    run.Skipped++;
                    run.SkippedAssets.Add(new RipWarning("mesh", item.AssetId, skipReason));
                    if (run.Skipped <= 20) run.Report.AppendLine($"  - {item.AssetId}: {skipReason}");
                    return;
                }

                run.Read++;
                Enqueue(run, new RipItem { Prefab = prefab });

                // The skirt rides behind the prefab that owns it. Its base surface was resolved —
                // and its textures queued — while the prefab was assembled, so the ordered writer
                // encodes them before the band's .glb needs their names.
                //
                if (skirt != null) Enqueue(run, new RipItem { Skirt = skirt });

                // The alternative-state files a prefab authors — a dead tree, a stump, a part-full
                // stockpile. Each is a whole replacement for the prefab's own file, chosen by the
                // consumer from `attrs.treeState` / `attrs.fullness`, and exists only where the
                // prefab flags the state, so absence means "the game has no such variant either".
                //
                foreach (PrefabMesh variant in variants) Enqueue(run, new RipItem { Prefab = variant });

                // A stacked prefab also ships one file per stack role, so that the instance pass
                // can place a foot once, a repeating section as many times as that instance needs,
                // and a cap once. The whole-prefab file above stays, and is still the right thing
                // for an instance of the same prefab that has no `Stack` component — a pillar used
                // as a standalone prop rather than under a viaduct.
                //
                bool stacked = false;
                foreach (StackRole role in prefab.StackRoles())
                {
                    stacked = true;
                    PrefabMesh part = new()
                    {
                        AssetId = PrefabMesh.StackPartId(prefab.AssetId, role),
                        SourceName = prefab.SourceName,
                        InstanceCount = prefab.InstanceCount
                    };

                    foreach (PrefabMeshPart p in prefab.Parts)
                        if (p.StackRole == role) part.Parts.Add(p);

                    Enqueue(run, new RipItem { Prefab = part });
                }

                // Parts with no stack role on a stacked prefab are drawn once, untiled, at the
                // instance transform — the game's sub-mesh test grants `IsStack*` bits through the
                // instance's `Stack` component and an unflagged entry passes regardless. They get
                // their own file because the instance pass never references the whole-prefab file
                // for a stacked prefab (every instance of one carries `Stack` by archetype), and
                // the stack part files must not double them per tile.
                //
                if (stacked)
                {
                    PrefabMesh rest = new()
                    {
                        AssetId = PrefabMesh.StackRestId(prefab.AssetId),
                        SourceName = prefab.SourceName,
                        InstanceCount = prefab.InstanceCount
                    };

                    foreach (PrefabMeshPart p in prefab.Parts)
                        if (p.StackRole == null) rest.Parts.Add(p);

                    if (rest.Parts.Count > 0)
                    {
                        StackRestParts++;
                        Enqueue(run, new RipItem { Prefab = rest });
                    }
                }
            }
            catch (Exception ex)
            {
                run.ReadFailed++;
                run.Report.AppendLine($"  ! {item.AssetId}: {ex.GetType().Name} {ex.Message}");
                _log.Warn($"Failed to assemble prefab `{item.AssetId}`: {ex}");
            }
        }

        /// <summary>
        /// Hand one item to the bounded queue without wedging on a dead writer — the rule and
        /// its history are <see cref="RipQueue"/>. What is local: several items are queued per
        /// prefab between <see cref="Advance"/>'s capacity checks, so a throw here is caught by
        /// <see cref="ReadOne"/> and counted against that prefab.
        /// </summary>
        /// <param name="run">The run in progress.</param>
        /// <param name="item">The item to queue.</param>
        private static void Enqueue(RipRun run, RipItem item)
            => RipQueue.Enqueue(run.Queue, run.Writer, "mesh writer", item,
                                // While the main thread waits for room it must keep landing
                                // readbacks — the writer may be waiting on one of them.
                                () => run.Materials.Pump(force: true));
    }
}
