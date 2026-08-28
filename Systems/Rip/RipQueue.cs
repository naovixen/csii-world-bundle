using System;
using System.Collections.Concurrent;
using System.Threading.Tasks;

namespace Carto.Systems
{
    /// <summary>
    /// The bounded hand-off from a rip stage's main-thread walk to its writer task.
    /// </summary>
    /// <remarks>
    /// <para>
    /// Two stages queue work under back-pressure — <c>MeshRipSystem</c> and
    /// <c>NetworkRipSystem</c> — and they wrote the same six lines twice.
    /// <c>NetworkRipSystem</c>'s copy said so in its own comment ("This is
    /// <c>MeshRipSystem.Enqueue</c>, for the same reason"), which is the signal
    /// <c>IO/BundlePrecision.cs</c> already acted on one level down: a constant whose copies have
    /// to describe each other wants to be one constant.
    /// </para>
    /// <para>
    /// The rule it carries is the one refactor Phase 1 fixed as a live defect. A plain
    /// <c>Add</c> on a bounded queue blocks until the writer makes room, and a writer that has
    /// FAULTED never will: the main thread waits for the rest of the session, with no dialog and
    /// no log line. Checking the writer between attempts turns that into a thrown exception the
    /// stage's per-entity catch counts and the next frame aborts on. Both stages queue several
    /// items per entity — one per piece mesh, plus every texture the material rip hands over —
    /// so the walk meets a full queue BETWEEN its own capacity checks, which is exactly where the
    /// naive version wedges.
    /// </para>
    /// </remarks>
    internal static class RipQueue
    {
        /// <summary>
        /// How long to wait for room before looking at the writer again, in milliseconds.
        /// Short enough that a faulted writer is noticed within a frame or two; long enough that
        /// a healthy one is not polled in a spin loop.
        /// </summary>
        private const int PollMs = 200;

        /// <summary>
        /// Hand one item to a bounded queue without wedging on a dead writer.
        /// </summary>
        /// <typeparam name="T">The queued record type.</typeparam>
        /// <param name="queue">The bounded queue the writer task drains.</param>
        /// <param name="writer">The writer task draining it.</param>
        /// <param name="writerName">What to call the writer in the exception — a person reads it.</param>
        /// <param name="item">The item to queue.</param>
        /// <exception cref="InvalidOperationException">The writer task has faulted.</exception>
        public static void Enqueue<T>(BlockingCollection<T> queue, Task writer, string writerName, T item)
        {
            while (!queue.TryAdd(item, PollMs))
            {
                if (writer.IsFaulted)
                    throw new InvalidOperationException($"the {writerName} task has faulted; the queue will never drain");
            }
        }
    }
}
