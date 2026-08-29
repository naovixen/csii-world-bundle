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
    /// The work list: every unique prefab mesh a rip has to read, and the state variants each
    /// one needs.
    /// </summary>
    /// <remarks>
    /// Dedupe by prefab is the whole economy of this stage — a city has thousands of trees and
    /// a handful of tree prefabs. What survives the walk is one work item per prefab per state,
    /// never per placement.
    /// </remarks>
    public partial class MeshRipSystem
    {
        /// <summary>
        /// Build the work list: every distinct prefab referenced by a placed object, ordered by how
        /// many objects use it.
        /// Ordering matters only when the run is capped — ripping the most-used prefabs first means
        /// a capped run still covers most of the city rather than an arbitrary corner of it.
        /// </summary>
        /// <param name="report">The report to note discovery counts in.</param>
        /// <param name="skippedAssets">Receives a §10 declaration for every prefab the cap drops.</param>
        /// <param name="maxPrefabs">The cap, or <c>0</c> for none.</param>
        /// <param name="kinds">Which instance kinds' prefabs to keep.</param>
        /// <param name="distinctPrefabs">Receives the number of distinct prefabs found.</param>
        /// <param name="placedObjects">Receives the number of placed objects walked.</param>
        /// <returns>The work list.</returns>
        private List<WorkItem> CollectWorkItems(StringBuilder report, List<RipWarning> skippedAssets, int maxPrefabs, RipKindFilter kinds, out int distinctPrefabs, out int placedObjects)
        {
            Dictionary<Entity, int> counts = new();

            // The instance pass filters kinds per instance, and one prefab's placements can span
            // kinds — the building/tree kinds hang off the placed entity, not the prefab. Judging
            // a prefab by a single sampled instance therefore breaks the join under a filter that
            // excludes anything: an instance kept by the instance pass could reference a mesh
            // this pass dropped. Collect the union of kinds actually placed for each prefab and
            // keep the prefab when any of them is allowed. Only built when the filter can exclude
            // at all, so the default all-kinds rip pays nothing per instance.
            //
            Dictionary<Entity, HashSet<string>> kindsByPrefab = kinds.Everything ? null : new();
            NativeArray<Entity> entities = _objectQuery.ToEntityArray(Allocator.TempJob);

            try
            {
                placedObjects = entities.Length;
                int utilityNodes = 0, vehicleCargo = 0;

                foreach (Entity entity in entities)
                {
                    // Dropped before it is counted, so the prefab set here matches the one
                    // InstanceRipSystem builds and the two layers assign the same ids.
                    //
                    if (IsUtilityMarkerSubObject(EntityManager, entity))
                    {
                        utilityNodes++;
                        continue;
                    }

                    if (IsVehicleCargoSubObject(EntityManager, entity))
                    {
                        vehicleCargo++;
                        continue;
                    }

                    Entity prefabEntity = EntityManager.GetComponentData<PrefabRef>(entity).m_Prefab;
                    if (prefabEntity == Entity.Null) continue;

                    counts.TryGetValue(prefabEntity, out int count);
                    counts[prefabEntity] = count + 1;

                    if (kindsByPrefab != null)
                    {
                        if (!kindsByPrefab.TryGetValue(prefabEntity, out HashSet<string> kindSet))
                            kindsByPrefab[prefabEntity] = kindSet = new HashSet<string>();
                        kindSet.Add(Instance.InstanceRip.ClassifyKind(entity, prefabEntity));
                    }
                }

                if (utilityNodes > 0) report.AppendLine($"Skipped {utilityNodes} buried utility-network node object(s).");
                if (vehicleCargo > 0) report.AppendLine($"Skipped {vehicleCargo} cargo prop(s) riding vehicles.");
            }
            finally
            {
                entities.Dispose();
            }

            distinctPrefabs = counts.Count;

            // Ids come from the shared assignment, not from each name in isolation, so that a
            // duplicated prefab name cannot make two prefabs write to the same file. The
            // assignment runs over the FULL prefab set even when a kind filter is active —
            // ids must not depend on the selection, or partial re-rips stop joining up.
            //
            Dictionary<Entity, RipAssetIds.AssetIdEntry> assetIds = RipAssetIds.BuildAssetIds(EntityManager, counts.Keys, out int disambiguated);
            if (disambiguated > 0) report.AppendLine($"Disambiguated {disambiguated} prefab(s) sharing a name.");

            int filtered = 0;
            List<WorkItem> items = new(counts.Count);
            foreach (KeyValuePair<Entity, int> kvp in counts)
            {
                RipAssetIds.AssetIdEntry entry = assetIds[kvp.Key];

                // The advanced kind filter, judged over every kind the prefab was actually
                // placed as — kept whenever any instance of it survives the instance pass's
                // per-instance filter, so no kept record references a dropped mesh.
                //
                if (kindsByPrefab != null)
                {
                    bool allowed = false;
                    foreach (string kind in kindsByPrefab[kvp.Key])
                    {
                        if (kinds.Allows(kind)) { allowed = true; break; }
                    }

                    if (!allowed)
                    {
                        filtered++;
                        continue;
                    }
                }

                // An unresolvable prefab still gets ripped under the same synthetic id
                // InstanceRipSystem gives its instances, so the two layers keep joining up.
                //
                items.Add(new WorkItem
                {
                    PrefabEntity = kvp.Key,
                    AssetId = entry.AssetId,
                    SourceName = GetPrefabName(kvp.Key) ?? entry.AssetId,
                    InstanceCount = kvp.Value
                });
            }

            if (filtered > 0) report.AppendLine($"Excluded {filtered} prefab(s) by the layer selection (advanced kind toggles).");

            items.Sort((a, b) => b.InstanceCount.CompareTo(a.InstanceCount));

            if ((maxPrefabs > 0) && (items.Count > maxPrefabs))
            {
                // Every capped-off prefab's asset id is still referenced by instances.jsonl, so
                // each must be declared in warnings.jsonl for a capped rip to stay contract-valid
                // (§10: a missing mesh declared is a warning; undeclared, a validator error).
                // A capped stacked prefab's instances reference its per-role part files rather
                // than the whole-prefab file (the derivation ExpandStack mirrors), so those ids
                // are declared alongside it — as are the alternative-state files an uncapped rip
                // would have derived, so a consumer looking for `__dead` on a dead tree finds a
                // declared gap rather than a silent one.
                //
                string reason = $"skipped by the mesh cap ({maxPrefabs} most-used prefabs); instances kept with placement only";
                for (int i = maxPrefabs; i < items.Count; i++)
                {
                    WorkItem item = items[i];
                    skippedAssets.Add(new RipWarning("mesh", item.AssetId, reason));

                    if (!EntityManager.HasBuffer<SubMesh>(item.PrefabEntity)) continue;

                    DynamicBuffer<SubMesh> buffer = EntityManager.GetBuffer<SubMesh>(item.PrefabEntity, true);
                    SubMesh[] subMeshes = new SubMesh[buffer.Length];
                    for (int j = 0; j < buffer.Length; j++) subMeshes[j] = buffer[j];

                    if (EntityManager.HasComponent<StackData>(item.PrefabEntity))
                    {
                        HashSet<StackRole> roles = new();
                        bool rest = false;

                        for (int j = 0; j < subMeshes.Length; j++)
                        {
                            StackRole? role = PrefabMesh.RoleOf(subMeshes[j].m_Flags);
                            if (role.HasValue) roles.Add(role.Value);
                            else if (ExportsSubMesh(subMeshes[j].m_Flags)) rest = true;
                        }

                        foreach (StackRole role in roles)
                            skippedAssets.Add(new RipWarning("mesh", PrefabMesh.StackPartId(item.AssetId, role), reason));
                        if (rest) skippedAssets.Add(new RipWarning("mesh", PrefabMesh.StackRestId(item.AssetId), reason));
                    }

                    foreach (StateVariant variant in StateVariants)
                    {
                        if (Authors(subMeshes, variant))
                            skippedAssets.Add(new RipWarning("mesh", item.AssetId + variant.Suffix, reason));
                    }
                }

                report.AppendLine($"Capped to the {maxPrefabs} most-used prefab(s) of {items.Count}; "
                                + $"{items.Count - maxPrefabs} capped-off prefab(s) declared in warnings.jsonl.");
                items.RemoveRange(maxPrefabs, items.Count - maxPrefabs);
            }

            return items;
        }


        /// <summary>
        /// Abort a rip whose writer task has died mid-read. Nothing more can ever be written —
        /// the queue will not drain again — so the only honest outcome is a failed stage:
        /// release what the run holds and hand back a <see cref="RipOutcome"/> that SAYS it
        /// failed. The summary still opens with the word FAILED because a person reads it; the
        /// orchestrator does not — it used to infer failure from that spelling, which meant a
        /// stage writing "Failed" was silently a success.
        /// </summary>
        /// <param name="run">The rip whose writer faulted.</param>
        private void Fail(RipRun run)
        {
            _run = null;
            ForgetSystems();
            run.Clock.Stop();
            RipProgress.Clear();

            Exception cause = run.Writer.Exception?.GetBaseException();
            _log.Error($"Mesh writer faulted at {run.Index}/{run.Items.Count} prefab(s); aborting the pass: {run.Writer.Exception}");

            // The same release OnDestroy performs for a rip in flight: unblock anything waiting
            // on the queue and let the material rip drop its half-built state.
            //
            try { run.Queue.CompleteAdding(); } catch { /* already completed */ }
            Instance.MaterialRip.Abandon(); // also releases any encoder waiting on a readback

            string summary = $"FAILED: the mesh writer faulted at {run.Index}/{run.Items.Count} prefab(s) — "
                           + $"meshes/ is incomplete ({System.Threading.Volatile.Read(ref run.Written)} file(s) before the fault): "
                           + $"{cause?.Message ?? "unknown error"}\n\nFull details: Logs/Carto.log";

            RipReport.Deliver(run.Completed, RipOutcome.Failure(summary), summary, "mesh rip", _log);
        }



        /// <summary>
        /// Resolve the surface a part's foundation skirt draws with: surface 0 of the render
        /// prefab's <c>BaseProperties.m_BaseType</c>, or of the game-wide default base mesh when
        /// the prefab declares none (<c>ManagedBatchSystem.CreateBatch</c>, the
        /// <c>GeneratedType.ObjectBase</c> branch).
        /// </summary>
        /// <param name="meshEntity">The part's mesh entity.</param>
        /// <returns>The bundle material id.</returns>
        private string ResolveBaseMaterial(Entity meshEntity)
        {
            RenderPrefab baseType = null;

            if (EntityManager.HasComponent<PrefabData>(meshEntity)
                && Instance.Prefab.TryGetPrefab(EntityManager.GetComponentData<PrefabData>(meshEntity), out PrefabBase prefabBase)
                && (prefabBase is RenderPrefab renderPrefab))
            {
                BaseProperties properties = renderPrefab.GetComponent<BaseProperties>();
                if ((properties != null) && (properties.m_BaseType != null)) baseType = properties.m_BaseType;
            }

            if ((baseType == null) && (_meshSettingsQuery.CalculateEntityCount() == 1))
            {
                MeshSettingsData settings = _meshSettingsQuery.GetSingleton<MeshSettingsData>();
                if ((settings.m_DefaultBaseMesh != Entity.Null)
                    && EntityManager.HasComponent<PrefabData>(settings.m_DefaultBaseMesh)
                    && Instance.Prefab.TryGetPrefab(EntityManager.GetComponentData<PrefabData>(settings.m_DefaultBaseMesh), out PrefabBase defaultBase))
                    baseType = defaultBase as RenderPrefab;
            }

            string materialId = ((baseType != null) && Instance.MaterialRip.IsRipping)
                              ? Instance.MaterialRip.Resolve(baseType, 0) : null;

            if (string.IsNullOrEmpty(materialId))
            {
                materialId = "base_default_mat0";
                if (Instance.MaterialRip.IsRipping)
                    Instance.MaterialRip.Declare(materialId, "base surface did not resolve");
            }

            return materialId;
        }
    }
}
