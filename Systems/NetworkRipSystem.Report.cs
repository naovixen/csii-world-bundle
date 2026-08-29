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
    /// What the network stage says it did, and the §10 gaps it declares.
    /// </summary>
    public partial class NetworkRipSystem
    {
        /// <summary>
        /// Close the file, report, and tell the user what came out.
        /// </summary>
        /// <param name="run">The finished run.</param>
        private void Finish(NetworkRun run)
        {
            _run = null;
            Instance.MeshRip.WarningSink = null;
            run.Clock.Stop();
            RipProgress.Clear();
            run.Writer?.Dispose();
            run.LaneWriter?.Dispose();

            // Materials last: the table can only name a texture by the content hash of the file it
            // became, so it is written once the writer has drained. Draining also flushes the last
            // segment meshes, which is why the byte count below is only final afterwards.
            //
            string materials = Drain(run);

            // The lane prefab catalogue (contract §5B v2): the straight parts and prefab-level
            // bend inputs every lanemeshes.jsonl record references by key.
            //
            if (run.LaneCatalogue.Count > 0)
            {
                try
                {
                    Carto.IO.JsonFile.Write(
                        IOUtils.CombinePath(run.OutputDirectory, "laneprefabs.json"), run.LaneCatalogue);
                }
                catch (Exception ex)
                {
                    _log.Error($"Could not write laneprefabs.json: {ex.Message}");
                }
            }

            StringBuilder report = run.Report;
            if (run.Writer != null)
            {
                report.AppendLine($"Wrote {run.Writer.Count} segment(s) to networks.jsonl in {run.Clock.Elapsed.TotalSeconds:F1} s");
                report.AppendLine($"  mesh form: {run.Writer.MeshForm} ({run.Triangles} triangles, "
                                + $"{run.Bytes / 1048576d:F1} MiB of glTF in networks/)");
                report.AppendLine($"  centerline form: {run.Writer.CenterlineForm} ({run.Points} point(s))");
            }
            else
            {
                report.AppendLine("Network surfaces deselected — networks.jsonl untouched.");
            }
            report.AppendLine($"Distinct cross-section pieces read: {run.PieceCache.Count}");
            report.AppendLine("Junction interiors are the arms' own node meshes, tiled to the centre "
                            + "(zero-width intersection pieces as three-rail wedges; audit §18)");
            if (_rotatedEdges > 0) report.AppendLine($"Rotated edges assembled: {_rotatedEdges}");
            if (_orphanCaps > 0) report.AppendLine($"Orphan node caps: {_orphanCaps}");

            // The lane-geometry stage: parameter records referencing part meshes stored once
            // (contract §5B v2); the consumer bends.
            //
            if ((run.LaneWriter != null) && ((run.LaneWriter.Count > 0) || (run.SkippedLanePipeline + run.SkippedLaneDecal > 0)))
            {
                report.AppendLine($"Lane geometry: {run.LaneWriter.Count} lane record(s) over "
                                + $"{run.LaneCatalogue.Count} prefab(s) in laneprefabs.json, "
                                + $"{run.LaneMeshIds.Count} part mesh(es) written once into lanemeshes/ "
                                + $"({run.LaneBytes / 1048576d:F1} MiB)");
                foreach (KeyValuePair<string, int> kvp in run.LanesByCategory) report.AppendLine($"  {kvp.Key}: {kvp.Value}");
                if (run.LaneHanging > 0) report.AppendLine($"  hanging (calm-air drop; the consumer builds the cubic): {run.LaneHanging}");
                if (run.LaneTunnel > 0) report.AppendLine($"  included {run.LaneTunnel} tunnel lane(s), tagged \"tunnel\" (the game draws them on its Tunnel cutaway layer)");
                if (run.LaneWorn > 0) report.AppendLine($"  {run.LaneWorn} lane(s) carry non-zero road wear (\"wear\", raw 0..10; tint = wear/10)");
                if (run.SkippedLanePipeline > 0) report.AppendLine($"  excluded {run.SkippedLanePipeline} part(s) with no world-view layer from the catalogue (Pipeline/SubPipeline/Waterway)");
                if (run.SkippedLaneDecal > 0) report.AppendLine(run.IncludeLaneDecals
                    ? $"  {run.SkippedLaneDecal} decal part(s) in the catalogue — wear tint and road markings, lifted by the consumer (§5B)"
                    : $"  excluded {run.SkippedLaneDecal} decal part(s) by the layer selection (lane decals toggle)");
                if (run.IncludeLaneDecals && (run.SkippedLaneDecal > 0))
                {
                    // The §5B split, per catalogue part, so the classification can be sanity-checked
                    // against the city rather than taken on trust: markings are the opaque paint,
                    // wear the half-strength tint the drive lanes carry.
                    //
                    run.LaneDecalsByRole.TryGetValue("marking", out int markings);
                    run.LaneDecalsByRole.TryGetValue("wear", out int worn);
                    report.AppendLine($"    decalRole: {markings} marking / {worn} wear / "
                                    + $"{run.SkippedLaneDecalUnclassified} unclassified (key omitted)");
                }
                if (run.SkippedLaneExcluded > 0) report.AppendLine($"  skipped {run.SkippedLaneExcluded} lane(s) whose parts its flags all exclude (drawn nowhere in-game either)");
                if (run.SkippedLaneStale > 0) report.AppendLine($"  skipped {run.SkippedLaneStale} lane(s) the simulation removed mid-rip");
                if (run.SkippedLaneDegenerate > 0) report.AppendLine($"  skipped {run.SkippedLaneDegenerate} lane(s) under the game's 0.1 m draw gate");
            }

            // Game rules seen but not reproduced — the numbers say whether the gap matters in
            // this city.
            if (_roundaboutParts > 0) report.AppendLine($"APPROXIMATED: {_roundaboutParts} roundabout part(s) — "
                                                      + "halves + wedge cover outer/inner/island; blend weights are shader-side");
            if (_railPieces > 0) report.AppendLine($"Corner-rail (PreserveShape) pieces placed: {_railPieces}");
            if (_skipBottomPieces > 0) report.AppendLine($"SkipBottomHalf pieces halved: {_skipBottomPieces}");
            if (_piecesFromAsset > 0)
                report.AppendLine($"Read {_piecesFromAsset} cross-section piece(s) from the geometry asset because their "
                                + "render-cache buffers were not resident this session (§42.6 — the buffers are a cache "
                                + "the game fills on demand, not the asset): "
                                + string.Join(", ", _pieceFromAssetNames));
            if (_emptyPieces > 0)
                report.AppendLine($"  DROPPED: {_emptyPieces} piece placement(s) had no readable geometry "
                                + "(no MeshVertex/MeshIndex/MeshMaterial buffer, or empty) — a composition "
                                + "loses that cross-section and ships hollow: "
                                + string.Join(", ", _emptyPieceNames));
            // The shading frame (audit #40 P6): the piece's own authored normals and tangents,
            // rotated into the bend rather than rebuilt from the bent faces.
            //
            if (_authoredFramePieces + _rebuiltFramePieces > 0)
            {
                report.AppendLine($"Shading frame: {_authoredFramePieces} piece placement(s) shipped the authored "
                                + $"MeshNormal/MeshTangent buffers rotated into the bend, {_rebuiltFramePieces} fell "
                                + "back to the face rebuild (no normal buffer on the piece)");
                if (_smoothTopPieces > 0)
                    report.AppendLine($"  SmoothTopNormal: {_smoothTopPieces} piece placement(s), {_smoothTopVerts} "
                                    + "vertex(es) at the top height bound flattened to (0,1,0) before the flip");
                if (_pinnedFrameVerts > 0)
                    report.AppendLine($"  {_pinnedFrameVerts} vertex(es) on a pinned column (median centre / zero-width "
                                    + "piece) took the two-column rotation — the map has no across direction there");
                report.AppendLine("  APPROXIMATED: the frame is the map's own Jacobian (cofactor for the normal, "
                                + "plain for the tangent); whether the game's vertex shader orthonormalizes first is "
                                + "shader-side and unknowable — the two agree on an orthogonal frame");
            }

            if (_unconformedPieces > 0)
                report.AppendLine($"NOT CONFORMED: {_unconformedPieces} LowerBottom/RaiseTop piece placement(s) shipped "
                                + "as authored — the terrain heightmap could not be captured (declared in warnings.jsonl)");

            if (_lowerBottomPieces + _raiseTopPieces > 0)
            {
                report.AppendLine($"{_lowerBottomPieces} LowerBottomToTerrain / {_raiseTopPieces} RaiseTopToTerrain "
                                + $"piece(s) flagged for terrain conforming, {_unconformedPieces} of them NOT conformed "
                                + "(BatchMeshHelpers' per-slice height bounds + the vertex shader's terrain sample, "
                                + "applied here):");
                if (_elevatedUnconformedPieces > 0)
                    report.AppendLine($"  {_elevatedUnconformedPieces} piece placement(s) held clear of the terrain: "
                                    + "an elevated composition with no Low/HighTransition side, which RaiseTopToTerrain "
                                    + "would otherwise pin to the ground far below (the roadbed walk's own rule)");
                report.AppendLine($"  sampled against the {_conformSource ?? "(no terrain)"} — the game's shader samples the "
                                + "finest cascade slice covering the vertex, so the fine capture is the closer of the two");
                report.AppendLine($"  bottom edges (clamp DOWN only — a trench wall's base never lifts, §38): "
                                + $"{_conformLoweredVerts} vertex(es) buried, {_conformKeptVerts} kept at road level");
                report.AppendLine($"  top edges (pinned to the terrain both ways — the §34 portal stretch): "
                                + $"{_conformRaisedVerts} vertex(es)");
                if (_conformOutOfWindowTris > 0)
                    report.AppendLine($"  DATA-CONTRACT VIOLATION: {_conformOutOfWindowTris} triangle(s) referenced "
                                    + "vertices outside their material range and were dropped (expected 0; "
                                    + "every scanned .Geometry asset keeps its ranges self-contained)");

                // One row per flagged piece — the §38 probe. A piece whose vertices ALL
                // classify is a two-rail wall: its whole shape is the conform move, so it is
                // the first suspect when a wall looks wrong.
                //
                List<KeyValuePair<string, ConformPieceProbe>> probes = new(_conformProbes);
                probes.Sort((a, b) => string.CompareOrdinal(a.Key, b.Key));
                foreach (KeyValuePair<string, ConformPieceProbe> kvp in probes)
                {
                    ConformPieceProbe pr = kvp.Value;
                    int classified = pr.Class1 + pr.Class2;
                    string share = (pr.Vertices > 0) ? $"{100f * classified / pr.Vertices:F0}%" : "n/a";
                    string twoRail = ((pr.Vertices > 0) && (classified == pr.Vertices)) ? " [TWO-RAIL: 100% classified]" : "";
                    report.AppendLine($"  {kvp.Key}: {pr.Flags}, size {pr.Size.x:F1}x{pr.Size.y:F1}x{pr.Size.z:F1}, "
                                    + $"{pr.Vertices} vert(s), class-1 {pr.Class1} / class-2 {pr.Class2} ({share}){twoRail}; "
                                    + $"{pr.Lowered} buried / {pr.Kept} kept / {pr.Raised} raised; "
                                    + $"dropped tris {pr.FrontCutTris} front / {pr.BackCutTris} back / {pr.OutOfWindowTris} window");
                }
            }
            // -- what the cross-section walk BUILT (§42 tram deck, §41.9 node flare) -----------
            //
            // Both defects are invisible to every other counter here, because both pieces are
            // found, read and placed. Only two numbers separate them from a healthy piece: how
            // far across the frame the vertices reached, and how big the geometry is. Reported
            // as anomalies rather than in full — 137 distinct pieces would bury the answer.
            //
            {
                List<SectionProbe> collapsed = new();
                foreach (SectionProbe s in _sectionProbes.Values)
                {
                    if ((s.Vertices == 0) || !s.HasBounds) continue;
                    // "Pinned across" needs the piece to have built no surface as well as to
                    // have a degenerate xParam span, and it did not ask the second question
                    // until 2026-08-14 — whereupon the first rip with the probe keyed
                    // per-composition produced 22 rows and every one was healthy.
                    //
                    // The rows carried their own disproof. `Alley Drive Piece 3` reports
                    // authored size 3.0 x 5.0 x 64.0, an xParam pinned at 0.500..0.500 — and a
                    // BUILT extent of 3.05 m across. It built its 3 m. Its mesh is simply
                    // authored at local x = 0, so `xParam = vertex.x / width + 0.5` is 0.5 for
                    // every vertex while the width comes from the frame mapping instead. The
                    // four `Raised Side Piece 0.5` rows say the same thing arithmetically: the
                    // composition width recovered from the span and the width recovered from the
                    // authored offsetX agree within 3.5% on all four, so their tiny spans are
                    // correct for a 0.5 m piece on a 30 m road.
                    //
                    // This is `m_Size` being an AUTHORED field again — the same trap that
                    // retired the `Size.y` clause (PROBE-2). A probe that fires on 22 healthy
                    // pieces per rip cannot report a real one, so the extent test is what
                    // decides and the span is only corroboration.
                    //
                    float3 extent = s.Hi - s.Lo;
                    bool pinnedAcross = (s.Size.x > 0.25f) && ((s.XMax - s.XMin) < 0.02f)
                                     && (math.min(extent.x, extent.z) < 0.05f);
                    bool flat = math.max(extent.x, extent.z) < 0.05f;
                    if (pinnedAcross || flat) collapsed.Add(s);
                }
                if (collapsed.Count > 0)
                {
                    collapsed.Sort((a, b) => string.CompareOrdinal(a.Label, b.Label));
                    report.AppendLine($"COLLAPSED: {collapsed.Count} cross-section piece(s) were read and placed "
                                    + "but built no surface (authored size against the extent they produced):");
                    int shown = 0;
                    foreach (SectionProbe s in collapsed)
                    {
                        if (shown++ >= 40) { report.AppendLine($"  … and {collapsed.Count - 40} more"); break; }
                        float3 extent = s.Hi - s.Lo;
                        report.AppendLine($"  {s.Label}: authored offsetX {s.OffsetX:F2}, size "
                                        + $"{s.Size.x:F1}x{s.Size.y:F1}x{s.Size.z:F1}; {s.Placements} placement(s), "
                                        + $"{s.Vertices} vertex(es); xParam {s.XMin:F3}..{s.XMax:F3}; "
                                        + $"built {extent.x:F2}x{extent.y:F2}x{extent.z:F2} m");
                    }
                }

            }

            if (_pipelineCompositions > 0)
                report.AppendLine($"Skipped {_pipelineCompositions} composition part(s) with no world-view layer "
                                + "(Pipeline/SubPipeline/Waterway — utility cutaway and overlay views only; "
                                + "Tunnel compositions are world geometry and included)");

            foreach (KeyValuePair<string, int> kvp in run.ByCategory) report.AppendLine($"  {kvp.Key}: {kvp.Value}");
            report.AppendLine("By form:");
            foreach (KeyValuePair<string, int> kvp in run.ByForm) report.AppendLine($"  {kvp.Key}: {kvp.Value}");
            if (run.SkippedMarker > 0) report.AppendLine($"Skipped {run.SkippedMarker} marker network(s)");
            if (run.MarkerCenterline > 0)
                report.AppendLine($"Centerline-only: {run.MarkerCenterline} marker network(s) kept as a line and "
                                + "not a surface — the game draws a Game.Net.Marker edge on the Marker layer "
                                + "only, which is off unless markers are toggled on (§44); taxiways are the "
                                + "networks this keeps");
            if (run.SkippedNoWidth > 0) report.AppendLine($"Skipped {run.SkippedNoWidth} edge(s) with no usable width");
            if (run.SkippedDegenerate > 0) report.AppendLine($"Skipped {run.SkippedDegenerate} edge(s) whose centerline collapsed to a point");
            if (run.SkippedStale > 0) report.AppendLine($"Skipped {run.SkippedStale} edge(s) the simulation removed mid-rip");
            if (run.Failed > 0)
                report.AppendLine($"FAILED on {run.Failed} entit(ies) — each counted and skipped rather than "
                                + "killing the stage; the `!` lines above name them");
            if (run.Skipped.Count > 0) report.AppendLine($"{run.Skipped.Count} surface(s) or texture(s) produced no material entry");

            // Contract §10: this stage's gaps go on disk, not only into the report. Written after
            // the drain, because that is what closes the material table and fills `run.Skipped`.
            //
            WriteWarnings(run, report);

            if (!string.IsNullOrEmpty(materials)) report.AppendLine($"Materials: {materials}");
            report.AppendLine($"Output: {run.OutputDirectory}");

            // The conforming pass's heightmap copy (~33 MB) and per-piece bounds are only
            // needed while meshes build.
            _conformHeights = null;
            _pieceHeightBounds.Clear();

            _log.Info(report.ToString());

            // NOTE the inversion, kept as it was: this stage gives the SHORT line to the
            // orchestrator and the FULL report to the modal, where the instance and lane rips do
            // the opposite. Three stages, three answers to "who gets which text" — worth settling,
            // but settling it changes what a rip reports, so it is not folded into a move.
            string summary = $"Wrote {run.Writer?.Count ?? 0} segment(s) and {run.LaneWriter?.Count ?? 0} lane record(s), "
                           + $"{(run.Bytes + run.LaneBytes) / 1048576d:F1} MiB of glTF, in {run.Clock.Elapsed.TotalSeconds:F0} s.";

            RipReport.Deliver(
                run.Completed,
                run.Failed > 0 ? RipOutcome.Failure(summary) : RipOutcome.Ok(summary),
                report.ToString(),
                "network rip",
                _log);
        }

    }
}
