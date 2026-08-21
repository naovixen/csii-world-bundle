using System;

using Carto.IO;

namespace Carto.Systems
{
    /// <summary>
    /// The frame loop shared by every rip stage that walks a captured entity list, hands records
    /// to a bounded queue and lets a writer task drain it.
    /// </summary>
    /// <remarks>
    /// <para>
    /// This loop was written out four times — <c>InstanceRipSystem</c>, <c>LaneRipSystem</c>,
    /// <c>MeshRipSystem</c>, <c>NetworkRipSystem</c> — and the first two were byte-identical
    /// down to their comments, differing only in a type name and whether the noun was "object" or
    /// "lane". That is four places to get four rules right:
    /// </para>
    /// <list type="bullet">
    /// <item>a frame budget, so a rip does not freeze the game;</item>
    /// <item>back-pressure, so the walk does not out-run the writer into memory;</item>
    /// <item>a faulted-writer check, because a queue a dead writer never drains is a main thread
    /// blocked for the rest of the session;</item>
    /// <item>a per-entity try/catch, because one bad entity must not cost the other million their
    /// records.</item>
    /// </list>
    /// <para>
    /// <c>NetworkRipSystem</c> is the proof that four copies is four chances: it had none of the
    /// last three until refactor Phase 1, and <c>MeshRipSystem.cs:744</c> had documented the trap
    /// it fell into for months.
    /// </para>
    /// </remarks>
    public static class RipWalk
    {
        /// <summary>
        /// What <see cref="Advance"/> needs of a run in flight. Implemented EXPLICITLY by each
        /// system's run class, so the run's own field names and public surface stay as they are.
        /// </summary>
        public interface IRun
        {
            /// <summary>How far the walk has got.</summary>
            int Index { get; set; }

            /// <summary>How many entities there are in total.</summary>
            int Count { get; }

            /// <summary>Whether the walk has handed over its last record.</summary>
            bool ReadingDone { get; set; }

            /// <summary>Whether the writer task has died.</summary>
            bool WriterFaulted { get; }

            /// <summary>Whether the writer task has finished draining.</summary>
            bool WriterCompleted { get; }

            /// <summary>How many records are queued and not yet written.</summary>
            int QueueCount { get; }

            /// <summary>Tell the writer no more records are coming.</summary>
            void CompleteAdding();
        }

        /// <summary>
        /// Walk one frame's worth, then finish the run once the writer has drained.
        /// </summary>
        /// <remarks>
        /// The budget and the back-pressure are ARGUMENTS, not constants here: each stage tunes
        /// them and each one's reason is recorded where it is set — the instance walk runs at
        /// 12 ms against the mesh rip's 6 because it is pure component reads with serialisation
        /// off-thread, and 6 ms stretched a seconds-long pass into minutes.
        /// </remarks>
        /// <param name="run">The run in flight.</param>
        /// <param name="phase">The caption to report progress under.</param>
        /// <param name="frameBudgetMilliseconds">How long the walk may hold the main thread.</param>
        /// <param name="queueBackpressure">How many records may wait before the walk pauses.</param>
        /// <param name="processOne">Process the entity at this index. May throw.</param>
        /// <param name="onFailed">Record that the entity at this index threw. Must not throw.</param>
        /// <param name="fail">Abort the pass — the writer is dead and the queue will never drain.</param>
        /// <param name="finish">Close the run out.</param>
        public static void Advance(
            IRun run,
            RipProgress.Phase phase,
            double frameBudgetMilliseconds,
            int queueBackpressure,
            Action<int> processOne,
            Action<int, Exception> onFailed,
            Action fail,
            Action finish)
        {
            if (!run.ReadingDone)
            {
                // A faulted writer never drains the queue: the walk would pause on back-pressure
                // for the rest of the session, ReadingDone would never turn true, and a pipeline
                // driving the rip would wait on this stage forever. Abort the pass instead.
                //
                if (run.WriterFaulted)
                {
                    fail();
                    return;
                }

                System.Diagnostics.Stopwatch budget = System.Diagnostics.Stopwatch.StartNew();

                while (run.Index < run.Count)
                {
                    // Back-pressure: pause the walk rather than pile records up faster than the
                    // writer serialises them.
                    if (run.QueueCount >= queueBackpressure) break;

                    // One bad entity must not cost the rest of the walk its records.
                    //
                    try
                    {
                        processOne(run.Index);
                    }
                    catch (Exception ex)
                    {
                        onFailed(run.Index, ex);
                    }

                    run.Index++;

                    if (budget.Elapsed.TotalMilliseconds >= frameBudgetMilliseconds) break;
                }

                RipProgress.Report(phase, run.Index, run.Count);

                if (run.Index >= run.Count)
                {
                    run.ReadingDone = true;
                    run.CompleteAdding();
                }

                return;
            }

            if (run.WriterCompleted) finish();
        }
    }
}
