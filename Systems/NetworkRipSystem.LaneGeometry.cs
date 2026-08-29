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
    /// The §5B lane-geometry layer: which parts a lane stamps along its curve, and the catalogue
    /// that says what each part is.
    /// </summary>
    /// <remarks>
    /// A structurally separate stage that happens to share this system's walk: it writes its own
    /// layer (<c>lanemeshes.jsonl</c>) and its own catalogue (<c>laneprefabs.json</c>), and it
    /// reads the net graph only to find the lanes. Refactor Phase 9 names a
    /// <c>LaneGeometryRipSystem</c> for it; that is a change to the rip's stage list, not a
    /// move, and it is recorded in the plan rather than folded in here.
    /// </remarks>
    public partial class NetworkRipSystem
    {
        /// <param name="run">The rip in flight.</param>
        /// <param name="entity">The lane to record.</param>
        private void RipLaneGeometry(NetworkRun run, Entity entity)
        {
            if (!EntityManager.Exists(entity))
            {
                run.SkippedLaneStale++;
                return;
            }

            Entity prefab = EntityManager.GetComponentData<PrefabRef>(entity).m_Prefab;
            if (prefab == Entity.Null) return;

            LanePrefabEntry entry = GetLanePrefab(run, prefab);
            if (entry.Parts.Count == 0) return;

            Curve curve = EntityManager.GetComponentData<Curve>(entity);
            if (!(curve.m_Length > 0.1f))
            {
                // The game's own gate (`UpdateLaneInstances`): a sub-decimetre lane draws nothing.
                //
                run.SkippedLaneDegenerate++;
                return;
            }

            Entity owner = Entity.Null;
            if (EntityManager.HasComponent<Game.Common.Owner>(entity))
                owner = EntityManager.GetComponentData<Game.Common.Owner>(entity).m_Owner;

            // A lane whose owning network runs below ground is drawn by the game on its Tunnel
            // layer — real geometry, visible whenever the cutaway shows it. The bundle renders
            // tunnels in full (the terrain cutout opens their portals), so these lanes are
            // included and tagged rather than skipped; a consumer that wants the surface world
            // only filters on the tag. The test is the game's `IsNetOwnerTunnel` in full: the
            // owner's own elevation, and — for node owners, which carry no elevation of their
            // own — every edge connected to the node.
            //
            bool tunnel = (owner != Entity.Null) && IsNetOwnerTunnel(owner);

            SubMeshFlags excluded = SubMeshFlags.RequireEditor
                | (Instance.City.leftHandTraffic ? SubMeshFlags.RequireRightHandTraffic
                                                 : SubMeshFlags.RequireLeftHandTraffic);

            if (EntityManager.HasComponent<Game.Net.PedestrianLane>(entity)
                && ((EntityManager.GetComponentData<Game.Net.PedestrianLane>(entity).m_Flags & PedestrianLaneFlags.Unsafe) != 0))
                excluded |= SubMeshFlags.RequireSafe;

            if (EntityManager.HasComponent<Game.Net.CarLane>(entity))
            {
                Game.Net.CarLane carLane = EntityManager.GetComponentData<Game.Net.CarLane>(entity);
                if ((carLane.m_Flags & CarLaneFlags.Unsafe) != 0) excluded |= SubMeshFlags.RequireSafe;
                if ((carLane.m_Flags & CarLaneFlags.LevelCrossing) == 0) excluded |= SubMeshFlags.RequireLevelCrossing;
            }

            if (EntityManager.HasComponent<Game.Net.TrackLane>(entity))
            {
                Game.Net.TrackLane trackLane = EntityManager.GetComponentData<Game.Net.TrackLane>(entity);
                if ((trackLane.m_Flags & TrackLaneFlags.LevelCrossing) == 0) excluded |= SubMeshFlags.RequireLevelCrossing;
                excluded |= ((trackLane.m_Flags & (TrackLaneFlags.Switch | TrackLaneFlags.DiamondCrossing)) == 0)
                          ? SubMeshFlags.RequireTrack : SubMeshFlags.RequireClear;
            }

            // The hanging drop is the game's cubic: ends are `HangingLane.m_Distances`, middle
            // handles `(distance + hanging × length) × ⅔` — the quadratic-to-cubic lift of a
            // parabola. The record ships the per-lane distances; the prefab-level `m_Hanging`
            // factor is in the catalogue, and the consumer builds the cubic (contract §5B v2
            // documents the formula).
            //
            // WIND, stated correctly (re-derived from source 2026-08-17, audit §53). The earlier
            // wording here — "wind sway omitted" — undersold it. `BatchDataSystem`'s
            // `LaneProperty.HangingDistances` builds exactly this control set and then multiplies
            // the WHOLE of it by the local wind SPEED (`sqrt(lengthsq(Wind.SampleWind(...)))` at
            // each of the curve's four control points). The droop is therefore proportional to
            // wind, not merely perturbed by it, and in dead calm the game draws the wire straight.
            // What we ship is the unscaled base — the game's shape at wind speed 1 — which is the
            // right thing for a bundle a consumer replays with no weather, and it is an
            // APPROXIMATION of no single frame of the game rather than a faithful snapshot of one.
            //
            float2 hangDistances = float2.zero;
            if (EntityManager.HasComponent<HangingLane>(entity))
                hangDistances = EntityManager.GetComponentData<HangingLane>(entity).m_Distances;
            bool hasHang = (entry.Hanging != 0f) || math.any(hangDistances != 0f);

            // Node lanes carry a width offset where shared lane ends merge; the scale runs
            // start-to-end exactly as `BuildCurveScale(NodeLane, NetLaneData)` hands it over.
            //
            float2 widthScale = new(1f, 1f);
            if (EntityManager.HasComponent<NodeLane>(entity) && EntityManager.HasComponent<NetLaneData>(prefab))
            {
                NetLaneData laneData = EntityManager.GetComponentData<NetLaneData>(prefab);
                if (laneData.m_Width != 0f)
                    widthScale = 1f + (EntityManager.GetComponentData<NodeLane>(entity).m_WidthOffset / laneData.m_Width);
            }

            // The uncut spans of the curve, walked the way `UpdateLaneInstances` walks
            // `CutRange`s: a driveway through a fence, or a junction through a hedge, is a hole,
            // not an end. The game re-gates every span on the same 0.1 m draw threshold
            // (`curve3.m_Length > 0.1f` on the span's arc length), so sub-decimetre fragments
            // between two cuts draw nothing and are not recorded. A lane whose every span fails
            // draws nothing at all.
            //
            List<float2> spans = new();
            bool hasCuts = EntityManager.HasBuffer<CutRange>(entity);
            if (hasCuts)
            {
                DynamicBuffer<CutRange> cuts = EntityManager.GetBuffer<CutRange>(entity, true);
                float previous = 0f;
                for (int j = 0; j <= cuts.Length; j++)
                {
                    float min = (j < cuts.Length) ? cuts[j].m_CurveDelta.min : 1f;
                    float max = (j < cuts.Length) ? cuts[j].m_CurveDelta.max : 1f;
                    if ((min >= previous) && ((curve.m_Length * (min - previous)) > 0.1f))
                        spans.Add(new float2(previous, min));
                    previous = max;
                }

                if (spans.Count == 0)
                {
                    run.SkippedLaneDegenerate++;
                    return;
                }
            }

            LaneGeometryRecord record = new()
            {
                Id = entity.Index,
                Prefab = entry.Key,
                Category = entry.Category,
                CurveA = curve.m_Bezier.a,
                CurveB = curve.m_Bezier.b,
                CurveC = curve.m_Bezier.c,
                CurveD = curve.m_Bezier.d,
                Spans = spans,
                WidthScale = widthScale
            };

            // The per-lane part selection the game applies in `UpdateLaneInstances`, resolved by
            // the producer so a consumer never needs the flag semantics: a lane whose every part
            // is excluded draws nothing, exactly as in-game.
            //
            foreach (LanePartRef part in entry.Parts)
            {
                if ((part.Flags & excluded) == 0) record.Parts.Add(part.Index);
            }

            if (record.Parts.Count == 0)
            {
                run.SkippedLaneExcluded++;
                return;
            }

            if (owner != Entity.Null) record.Owner = owner.Index;

            if (tunnel)
            {
                record.Tunnel = true;
                run.LaneTunnel++;
            }

            // Road wear (audit P10): the serialized state behind the game's lane-decal
            // deterioration tint in the normal view — BatchDataSystem sets
            // `colossal_CurveDeterioration = LaneCondition.m_Wear / 10` per lane. Raw game value
            // (0..10), only when non-zero, to keep the layer lean. Not `NetCondition.m_Wear`,
            // which is infoview-only.
            //
            if (EntityManager.HasComponent<Game.Net.LaneCondition>(entity))
            {
                float wear = EntityManager.GetComponentData<Game.Net.LaneCondition>(entity).m_Wear;
                if (wear != 0f)
                {
                    record.Wear = wear;
                    run.LaneWorn++;
                }
            }

            if (hasHang)
            {
                record.HasHang = true;
                record.HangDistances = hangDistances;
                run.LaneHanging++;
            }

            // The lane's resolved paint, exactly as instances carry it (contract §7.2) — hedges
            // and fences run through the same MeshColor variation as props do.
            //
            if (EntityManager.HasBuffer<Game.Rendering.MeshColor>(entity))
            {
                DynamicBuffer<Game.Rendering.MeshColor> colors = EntityManager.GetBuffer<Game.Rendering.MeshColor>(entity, true);
                if (colors.Length > 0) record.ColorSet = InstanceRipSystem.ColorSetJson(colors[0].m_ColorSet);
            }

            run.LaneWriter.Write(record);
            run.LanesByCategory.TryGetValue(entry.Category, out int count);
            run.LanesByCategory[entry.Category] = count + 1;
        }

    }
}
