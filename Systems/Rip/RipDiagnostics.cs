using System;
using System.Collections.Generic;

using Unity.Collections;
using Unity.Entities;

namespace Carto.Systems
{
    /// <summary>
    /// Diagnostics the rip stages share — the lines a report prints when something did not work.
    /// </summary>
    /// <remarks>
    /// <see cref="DescribeComponents"/> was byte-identical in <c>InstanceRipSystem</c> and
    /// <c>MeshRipSystem</c>. Moved out by refactor Phase 7, taking the <c>EntityManager</c> as an
    /// argument rather than reading a system's own, which is what makes it shareable at all.
    /// </remarks>
    public static class RipDiagnostics
    {
        /// <summary>
        /// List the component types present on an entity, for diagnostics.
        /// </summary>
        /// <param name="entityManager">The entity manager to ask.</param>
        /// <param name="entity">The entity to describe.</param>
        /// <returns>The type names.</returns>
        public static List<string> DescribeComponents(EntityManager entityManager, Entity entity)
        {
            List<string> names = new();
            NativeArray<ComponentType> types = entityManager.GetComponentTypes(entity, Allocator.TempJob);
            try
            {
                foreach (ComponentType type in types) names.Add(type.GetManagedType()?.Name ?? type.ToString());
            }
            finally
            {
                types.Dispose();
            }

            names.Sort(StringComparer.Ordinal);
            return names;
        }
    }
}
