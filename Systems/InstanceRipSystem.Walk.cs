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
    /// The frame loop, one placed object at a time, and the sub-object stack it expands.
    /// </summary>
    public partial class InstanceRipSystem
    {
        /// <summary>
        /// Advance the walk by one frame's worth. The loop itself — budget, back-pressure, the
        /// faulted-writer abort and the per-entity catch — is <see cref="RipWalk.Advance"/>,
        /// which this system and <see cref="LaneRipSystem"/> once wrote out identically.
        /// </summary>
        /// <param name="run">The run in flight.</param>
        private void Advance(InstanceRun run)
        {
            RipWalk.Advance(
                run,
                RipProgress.Phase.Instances,
                FrameBudgetMilliseconds,
                QueueBackpressure,
                index => ProcessOne(run, index, run.Entities[index]),
                (index, ex) =>
                {
                    run.ProcessFailed++;
                    run.FailedEntities.Add((run.Entities[index].Index, ex.GetType().Name));
                    if (run.ProcessFailed <= 5) _log.Warn($"Failed to record object entity {run.Entities[index].Index}: {ex}");
                },
                () => Fail(run),
                () => Finish(run));
        }

        /// <summary>
        /// Handle one placed object: classify, resolve, record.
        /// </summary>
        /// <param name="run">The rip in flight.</param>
        /// <param name="index">Its position in the snapshot, which the pre-pass's cached verdicts are keyed by.</param>
        /// <param name="entity">The object entity.</param>
        private void ProcessOne(InstanceRun run, int index, Entity entity)
        {
            // The walk holds its snapshot across frames while the simulation may run, so an
            // entity can be gone by the time its turn comes — the same mid-rip churn the mesh
            // and network rips already count. The acceptance rip hit 97 of these as raw NREs.
            //
            if (!EntityManager.Exists(entity))
            {
                run.SkippedStale++;
                run.StaleEntities.Add(entity.Index);
                return;
            }

            // Buried utility-network furniture: excluded here and in the mesh rip alike, so
            // neither its placements nor its meshes reach the bundle. The verdict is the
            // pre-pass's own — the same test on the same entity, decided once.
            //
            if (run.ExcludedUtility.IsSet(index))
            {
                run.SkippedUtilityNodes++;

                // Tallied by prefab, and named in the report. A silent count says the
                // filter ran; it does not say it caught the right things, and the previous
                // version of this filter ran happily while catching none of them. Naming
                // what was dropped is what makes both failures visible — the nodes still
                // getting through, and anything drawn that should not have been dropped.
                //
                Entity nodePrefab = _prefabRefs[entity].m_Prefab;
                if (nodePrefab != Entity.Null)
                {
                    run.UtilityNodesByPrefab.TryGetValue(nodePrefab, out int seen);
                    run.UtilityNodesByPrefab[nodePrefab] = seen + 1;
                }

                return;
            }

            // Cargo riding a vehicle — a log pile on a trailer — is part of the agent it
            // rides, not of the built environment, and the vehicle it belongs to is itself
            // excluded. Same treatment as the utility nodes: dropped in both rip systems
            // alike, tallied by prefab, and named in the report.
            //
            if (run.ExcludedCargo.IsSet(index))
            {
                run.SkippedVehicleCargo++;

                Entity cargoPrefab = _prefabRefs[entity].m_Prefab;
                if (cargoPrefab != Entity.Null)
                {
                    run.VehicleCargoByPrefab.TryGetValue(cargoPrefab, out int seen);
                    run.VehicleCargoByPrefab[cargoPrefab] = seen + 1;
                }

                return;
            }

            Entity prefabEntity = _prefabRefs[entity].m_Prefab;
            if (prefabEntity == Entity.Null)
            {
                run.SkippedNoPrefab++;
                return;
            }

            // The advanced kind filter, before any bookkeeping: an excluded object contributes
            // no record, no warning and no asset id — the bundle describes the selection.
            //
            string kind = ClassifyKind(entity, prefabEntity);
            if (!run.Kinds.Allows(kind))
            {
                run.SkippedFiltered++;
                return;
            }

            // An object whose managed prefab will not resolve is still a real object
            // standing in the city, so keep its placement under a synthetic id rather than
            // dropping it. PLAN §3 is explicit that placement must survive even when the
            // geometry cannot: a consumer substitutes a stand-in mesh and every instance
            // still lands in the right place. Dropping them silently is what breaks the
            // contract's 1:1 fidelity requirement.
            //
            RipAssetIds.AssetIdEntry idEntry = run.AssetIdTable[prefabEntity];
            string assetId = idEntry.AssetId;
            bool unresolved = !idEntry.Resolved;

            // A prefab with no SubMesh buffer has no geometry to rip, and the mesh rip
            // will never say so: its query requires SubMesh, so such a prefab is not
            // enumerated at all and cannot be skipped with a reason. This is the only
            // place both facts are in hand — the reference is being written here, and the
            // prefab entity is right there to ask — so the gap is declared here. Parking
            // lots, helipad airspace, area placeholders and intersections are all real
            // objects with real placements and no mesh of their own.
            //
            if (!unresolved && !_prefabSubMeshes.HasBuffer(prefabEntity))
                run.GeometrylessPrefabs.Add(assetId);

            if (unresolved)
            {
                run.SkippedNoPrefab++;
                run.UnresolvedPrefabs.Add(prefabEntity.Index);

                if (run.SkipSamples.Count < 5)
                {
                    run.SkipSamples.Add($"entity {entity.Index} prefab {prefabEntity.Index} "
                        + $"hasPrefabData={EntityManager.HasComponent<PrefabData>(prefabEntity)} "
                        + $"[{string.Join(", ", RipDiagnostics.DescribeComponents(EntityManager, entity))}]");
                }
            }

            Game.Objects.Transform transform = _transforms[entity];

            InstanceRecord record = new()
            {
                Id = entity.Index,
                AssetId = assetId,
                Kind = kind,
                Position = transform.m_Position,
                Rotation = transform.m_Rotation
            };

            // Owner is the sub-object relationship (a prop on a lot, a wing of a building).
            // Attached is a different thing — an object bound to a network at a curve
            // position — and using it as the parent link found only 98 of 1.4M objects.
            //
            Entity parent = Entity.Null;
            if (_owners.TryGetComponent(entity, out Game.Common.Owner owner)) parent = owner.m_Owner;
            if ((parent == Entity.Null) && _attachments.TryGetComponent(entity, out Attached attached))
                parent = attached.m_Parent;

            if (parent != Entity.Null)
            {
                record.Parent = parent.Index;
                run.WithParent++;
            }

            AddAttributes(entity, prefabEntity, kind, record);

            // Flag it in the record too, so a consumer can tell a stand-in from a real
            // asset without cross-referencing warnings.jsonl.
            //
            if (unresolved) record.Attributes["unresolved"] = true;

            // A stacked object is not one placement but several, and how many is a property
            // of this instance rather than of its prefab.
            //
            int expanded = ExpandStack(entity, prefabEntity, record, run.Emit, run.AssetIds);
            if (expanded > 0)
            {
                run.StackExpanded++;
                run.StackRecords += expanded;
                run.ByKind.TryGetValue(kind, out int stacked);
                run.ByKind[kind] = stacked + expanded;
                return;
            }

            run.Queue.Add(record);
            run.AssetIds.Add(assetId);
            run.ByKind.TryGetValue(kind, out int count);
            run.ByKind[kind] = count + 1;
        }
    }
}
