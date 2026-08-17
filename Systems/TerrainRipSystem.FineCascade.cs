using Carto.IO;
using Game.Simulation;
using System;
using System.Collections.Generic;
using System.Text;
using Unity.Mathematics;
using UnityEngine;
using UnityEngine.Rendering;

namespace Carto.Systems
{
    /// <summary>
    /// The fine terrain capture — the ground the game actually draws near the camera.
    ///
    /// <b>Why.</b> <c>GetHeightData()</c> is the CPU readback of one slice of the height cascade,
    /// <c>baseLod</c>: the playable map at 3.5 m/texel. But <c>m_HeightmapCascade</c> is a
    /// four-slice texture array, all 4096², and slices <c>baseLod+1..3</c> are camera-centred
    /// windows of <c>playable/2^n</c> — 1.75 m and 0.875 m per texel — each re-rendered by
    /// <c>RenderCascade</c> as the raw base heightmap blitted in plus every lot, lane and area
    /// height adjustment <i>rasterised at that slice's texel size</i> (<c>DrawHeightAdjustments</c>,
    /// three passes with <c>_CascadeMinHeights</c>). The GPU terrain samples the finest slice that
    /// covers a point (<c>colossal_TerrainTextureArrayLODArea/Range</c>). So within ~3.6 km of the
    /// camera the game's ground follows a road's flattening at 1.75 m, and within ~1.8 km at
    /// 0.875 m; a consumer meshing the 3.5 m readback shows the same road bed quantised four
    /// times coarser — on a cross-slope the heightfield rises through the kerb between two
    /// samples (audit §45; the 2026-08-16 BeamNG load #10 poke-throughs and the §34 seam
    /// approximation are both this).
    ///
    /// <b>How.</b> The window follows <c>CameraUpdateSystem.position</c> (which is the camera's
    /// transform, which the gameplay <c>CameraController</c> derives from pivot + zoom + angle;
    /// its <c>position</c> setter is a no-op). Slice <c>baseLod+1</c> is 1.75 m/texel and half
    /// the playable area wide, so four camera positions — the quadrant centres — tile the map.
    /// Per window: pivot to the centre, look straight down (pitch 90°, so the camera stands
    /// exactly over the pivot), wait until <c>heightMapSliceArea[k]</c> IS that window and the
    /// cascade render for it has been consumed (<c>heightMapRenderRequired</c> back to false),
    /// then read the slice back synchronously and copy it into its quadrant of an 8192² array.
    /// The camera is restored afterwards. Everything used is public API on <c>TerrainSystem</c>
    /// and <c>IGameCameraController</c>; nothing is poked by reflection, and the game's own cull
    /// + render path produces the data. If the window never lands (no gameplay camera, a map
    /// with a different cascade layout, a stalled render) the capture reports and yields
    /// nothing — the base heightmap ships as before, and <c>warnings.jsonl</c> records it.
    ///
    /// Written by <see cref="TerrainRipSystem.RipTerrain"/> as <c>terrain/heightmap_fine.png</c>
    /// with the <c>rasters.terrainFine</c> block (contract §8, additive; schema 2 unchanged);
    /// sampled by the network rip's terrain conforming when present (§34).
    /// </summary>
    public partial class TerrainRipSystem
    {
        /// <summary>The captured fine raster, south-first rows like the stamp arrays, or null.</summary>
        private ushort[] _fineHeights;
        private int _fineWidth, _fineHeight;
        /// <summary>Metres per texel of the fine raster.</summary>
        private double _fineCell;
        /// <summary>World x/z of the fine raster's south-west texel centre.</summary>
        private double2 _fineOriginSW;
        /// <summary>The cascade slice the fine raster came from.</summary>
        private int _fineSlice;
        /// <summary>The report of the last capture, and its warning for warnings.jsonl (or null).</summary>
        private string _fineReport, _fineWarning;

        /// <summary>The capture in flight, or null.</summary>
        private FineRun _fine;

        /// <summary>Whether a fine capture is running.</summary>
        public bool IsCapturingFine => _fine != null;

        /// <summary>
        /// The frames a window may take to land and render before the capture gives up on it.
        /// The game snaps the window in the first <c>UpdateCascades</c> after the camera moved and
        /// renders it the same frame; a few frames is normal, hundreds means the camera never
        /// went there (a modal that froze it, an unsupported controller).
        /// </summary>
        private const int FrameTimeout = 240;

        /// <summary>Frames the settled state must hold before the readback is issued.</summary>
        private const int SettleFrames = 2;

        private sealed class FineRun
        {
            public Action<string> Completed;
            public int Slice;
            public int Width, Height;               // texels per slice (the cascade texture size)
            public float2 PlayableOffset, PlayableArea;
            public float2 WindowSize;               // metres, of the slice window
            public int Cols, Rows;                  // windows across / down
            public int Index;                       // next window to capture
            public int Frames;                      // frames spent on the current window (or calibrating)
            public int Settled;                     // consecutive frames the current window was in place
            /// <summary>Whether the camera's horizontal offset from its pivot has been measured.</summary>
            public bool Calibrated;
            /// <summary>
            /// The window is being kicked away first: UpdateCascades only re-snaps a slice when its
            /// corner would move by more than a quarter of the window, so a camera that already
            /// stands within that of a target (but not on it) is sent to the far side of the map
            /// and brought back — two snaps, both bigger than the threshold.
            /// </summary>
            public bool Kicking;
            public float2 KickOrigin;
            /// <summary>Camera transform x/z minus pivot x/z at the chosen pitch and zoom.</summary>
            public float2 PivotToCamera;
            public ushort[] Fine;                   // the assembled raster, south-first rows
            public bool CameraTaken;
            /// <summary>Whether the run was installed as the capture in flight (its Completed callback is then owed).</summary>
            public bool Started;
            public Vector3 SavedPivot, SavedRotation;
            public float SavedZoom;
            public Game.Rendering.IGameCameraController Controller;
            public StringBuilder Report = new();
            public System.Diagnostics.Stopwatch Clock = System.Diagnostics.Stopwatch.StartNew();
            public int Readbacks;
        }

        /// <summary>
        /// Start the fine capture; returns at once and proceeds a step per frame in
        /// <see cref="OnUpdate"/>. Any previous capture's raster is released first.
        /// </summary>
        /// <param name="completed">Receives the finish summary when a pipeline drives the capture.</param>
        public string BeginFineCascade(Action<string> completed = null)
        {
            if (_fine != null) return "A fine terrain capture is already running.";
            ReleaseFine();

            FineRun run = new() { Completed = completed };
            TerrainSystem terrain = Instance.Terrain;
            Texture cascade = terrain.GetCascadeTexture();
            float4[] areas = terrain.heightMapSliceArea;
            if (cascade == null || areas == null || areas.Length < 4)
            {
                return Finish(run, "Fine terrain: no height cascade resident; layer omitted");
            }

            int baseLod = TerrainSystem.baseLod;
            run.Slice = baseLod + 1;
            if (run.Slice > 3)
            {
                return Finish(run, $"Fine terrain: baseLod {baseLod} leaves no finer slice; layer omitted");
            }
            run.Width = cascade.width;
            run.Height = cascade.height;
            run.PlayableOffset = terrain.playableOffset;
            run.PlayableArea = terrain.playableArea;
            // UpdateCascades: slice n covers playable / 2^(n - baseLod), centred on the camera.
            run.WindowSize = run.PlayableArea / math.pow(2f, run.Slice - baseLod);
            run.Cols = (int)math.round(run.PlayableArea.x / run.WindowSize.x);
            run.Rows = (int)math.round(run.PlayableArea.y / run.WindowSize.y);
            if (run.Cols < 1 || run.Rows < 1 || run.Cols * run.Rows > 64)
            {
                return Finish(run, $"Fine terrain: unexpected cascade layout ({run.Cols}x{run.Rows} windows); layer omitted");
            }

            Game.Rendering.IGameCameraController controller = Instance.Camera.activeCameraController;
            if (controller == null)
            {
                return Finish(run, "Fine terrain: no active game camera controller to steer the cascade with; layer omitted");
            }
            run.Controller = controller;
            run.SavedPivot = controller.pivot;
            run.SavedRotation = controller.rotation;
            run.SavedZoom = controller.zoom;
            run.CameraTaken = true;
            run.Fine = new ushort[(long)run.Width * run.Cols * run.Height * run.Rows];

            // Pitch and zoom now, pivot untouched: the next frames measure where the camera
            // transform lands relative to the pivot at this attitude (zero if the controller
            // honours a 90° pitch; a few metres if it clamps below it), and every window is
            // aimed with that offset taken out — UpdateCascades centres on the transform.
            // The run is installed only once the controller has taken the attitude: a setter
            // that throws must not leave a zombie run advancing behind a failed stage.
            try
            {
                controller.rotation = new Vector3(90f, run.SavedRotation.y, 0f);
                controller.zoom = math.max(run.SavedZoom, 400f);
            }
            catch (Exception ex)
            {
                return Finish(run, $"Fine terrain: the camera controller refused the capture attitude ({ex.Message}); layer omitted");
            }
            _fine = run;
            run.Started = true;
            RipProgress.Report(RipProgress.Phase.Terrain, 0, run.Cols * run.Rows);
            double texel = run.WindowSize.x / run.Width;
            return $"Steering the height cascade over {run.Cols}x{run.Rows} window(s) of slice {run.Slice} "
                 + $"({texel:F3} m/texel) in the background.";
        }

        /// <summary>The window `index`'s centre in world x/z.</summary>
        private static float2 WindowCentre(FineRun run, int index)
        {
            int col = index % run.Cols;
            int row = index / run.Cols;
            return run.PlayableOffset + new float2((col + 0.5f) * run.WindowSize.x, (row + 0.5f) * run.WindowSize.y);
        }

        /// <summary>Put the camera transform over the current window's centre (pivot = centre − measured offset) — or, when the
        /// slice already stands within a quarter window of it without being on it, kick it to the far corner first.</summary>
        private void AimCamera(FineRun run)
        {
            TerrainSystem terrain = Instance.Terrain;
            float2 target = WindowCentre(run, run.Index);
            float4 area = terrain.heightMapSliceArea[run.Slice];
            float2 wantOrigin = target - run.WindowSize * 0.5f;
            float2 offBy = math.abs(area.xy - wantOrigin);
            float quarter = math.min(run.WindowSize.x, run.WindowSize.y) * 0.25f;
            bool near = math.lengthsq(area) > 0f && math.all(offBy < quarter) && math.any(offBy > 0.01f);
            if (near && !run.Kicking)
            {
                // Far side: the diagonally opposite window centre is at least half the map away.
                run.Kicking = true;
                run.KickOrigin = area.xy;
                target = new float2(run.PlayableOffset.x + run.PlayableArea.x - (target.x - run.PlayableOffset.x),
                                    run.PlayableOffset.y + run.PlayableArea.y - (target.y - run.PlayableOffset.y));
            }
            PlaceCamera(run, target);
        }

        /// <summary>Set the controller so the camera transform stands over <paramref name="target"/>.</summary>
        private static void PlaceCamera(FineRun run, float2 target)
        {
            float2 centre = target - run.PivotToCamera;
            Game.Rendering.IGameCameraController controller = run.Controller;
            controller.rotation = new Vector3(90f, run.SavedRotation.y, 0f);
            controller.zoom = math.max(run.SavedZoom, 400f);
            controller.pivot = new Vector3(centre.x, controller.pivot.y, centre.y);
            run.Settled = 0;
        }

        /// <summary>
        /// Measure the camera transform's horizontal offset from the pivot at the capture
        /// attitude. Three frames after the attitude change the controller has applied it; the
        /// offset is then constant for the run (it depends on zoom and angles only — the
        /// controller's collision pushes the camera up, not sideways, when it looks straight
        /// down). Returns false while still waiting.
        /// </summary>
        private static bool Calibrate(FineRun run)
        {
            if (run.Frames < 3) return false;
            float3 camera = Instance.Camera.position;
            Vector3 pivot = run.Controller.pivot;
            run.PivotToCamera = new float2(camera.x - pivot.x, camera.z - pivot.z);
            run.Calibrated = true;
            return true;
        }

        /// <summary>Whether cascade slice <c>run.Slice</c> currently covers window <c>run.Index</c> (origin and size within 1 cm).</summary>
        private static bool WindowInPlace(FineRun run, TerrainSystem terrain, out float2 error)
        {
            float4 area = terrain.heightMapSliceArea[run.Slice];
            float2 centre = WindowCentre(run, run.Index);
            float2 expectMin = centre - run.WindowSize * 0.5f;
            error = math.abs(area.xy - expectMin);
            float2 sizeError = math.abs((area.zw - area.xy) - run.WindowSize);
            return math.all(error <= 0.01f) && math.all(sizeError <= 0.01f);
        }

        /// <summary>
        /// Advance the capture by one frame. Any exception ends the run through <see cref="Finish"/>
        /// so the camera is always given back and the pipeline always hears the stage end.
        /// </summary>
        private void AdvanceFine(FineRun run)
        {
            try
            {
                AdvanceFineUnguarded(run);
            }
            catch (Exception ex)
            {
                _log.Warn($"Fine terrain: capture threw: {ex}");
                if (ReferenceEquals(_fine, run))
                    Finish(run, $"Fine terrain: capture threw at window {run.Index + 1} ({ex.Message}); layer omitted", warn: true);
            }
        }

        private void AdvanceFineUnguarded(FineRun run)
        {
            TerrainSystem terrain = Instance.Terrain;
            run.Frames++;
            if (!run.Calibrated)
            {
                if (!Calibrate(run)) return;
                run.Frames = 0;
                AimCamera(run);
                return;
            }
            if (run.Kicking)
            {
                // Wait for the kick to take (the slice origin moved off where it was), then aim
                // at the real target — that move is a whole window and re-snaps for certain.
                float4 area = terrain.heightMapSliceArea[run.Slice];
                if (math.any(math.abs(area.xy - run.KickOrigin) > 1f) || run.Frames > 60)
                {
                    run.Kicking = false;
                    run.KickOrigin = area.xy;
                    run.Frames = 0;
                    PlaceCamera(run, WindowCentre(run, run.Index));
                }
                return;
            }
            if (!WindowInPlace(run, terrain, out float2 error))
            {
                run.Settled = 0;
                if (run.Frames == 30 || run.Frames == 120)
                {
                    // Re-aim with a fresh offset measurement: a modal or the controller's own
                    // smoothing may have eaten the first move, or the attitude settled late.
                    Calibrate(run);
                    AimCamera(run);
                }
                if (run.Frames > FrameTimeout)
                {
                    float4 area = terrain.heightMapSliceArea[run.Slice];
                    Finish(run, $"Fine terrain: window {run.Index + 1}/{run.Cols * run.Rows} of slice {run.Slice} never landed "
                              + $"(cascade at ({area.x:F1}, {area.y:F1})-({area.z:F1}, {area.w:F1}), wanted origin off by "
                              + $"({error.x:F1}, {error.y:F1}) m after {FrameTimeout} frames); layer omitted",
                           warn: true);
                }
                return;
            }
            // In place. The snap that put it there also queued its render; the next UpdateCascades
            // finds nothing pending and drops heightMapRenderRequired — that is when the slice
            // holds the window's height adjustments.
            if (terrain.heightMapRenderRequired)
            {
                run.Settled = 0;
                if (run.Frames > FrameTimeout)
                {
                    Finish(run, $"Fine terrain: window {run.Index + 1} landed but its cascade render never settled; layer omitted", warn: true);
                }
                return;
            }
            if (++run.Settled < SettleFrames) return;

            // Read the slice back — synchronously; the readback is ordered after the executed
            // cascade command buffer.
            try
            {
                Texture cascade = terrain.GetCascadeTexture();
                AsyncGPUReadbackRequest request = AsyncGPUReadback.Request(cascade, 0, 0, run.Width, 0, run.Height, run.Slice, 1);
                request.WaitForCompletion();
                if (request.hasError)
                {
                    Finish(run, $"Fine terrain: GPU readback of slice {run.Slice} failed at window {run.Index + 1}; layer omitted", warn: true);
                    return;
                }
                Unity.Collections.NativeArray<ushort> data = request.GetData<ushort>();
                if (data.Length < run.Width * run.Height)
                {
                    Finish(run, $"Fine terrain: readback returned {data.Length} of {run.Width * run.Height} samples; layer omitted", warn: true);
                    return;
                }
                // Rows come V = 0 first (south), like the base readback. Window (col, row) fills
                // columns col*W.. and rows row*H.. of the assembled south-first raster.
                int col = run.Index % run.Cols;
                int row = run.Index / run.Cols;
                int fineWidth = run.Width * run.Cols;
                for (int y = 0; y < run.Height; y++)
                {
                    int dst = ((row * run.Height) + y) * fineWidth + (col * run.Width);
                    Unity.Collections.NativeArray<ushort>.Copy(data, y * run.Width, run.Fine, dst, run.Width);
                }
                run.Readbacks++;
            }
            catch (Exception ex)
            {
                Finish(run, $"Fine terrain: readback threw at window {run.Index + 1}: {ex.Message}; layer omitted", warn: true);
                return;
            }

            run.Index++;
            run.Frames = 0;
            RipProgress.Report(RipProgress.Phase.Terrain, run.Index, run.Cols * run.Rows);
            if (run.Index < run.Cols * run.Rows)
            {
                AimCamera(run);
                return;
            }

            // Done: keep the raster for RipTerrain / the network conform.
            _fineHeights = run.Fine;
            _fineWidth = run.Width * run.Cols;
            _fineHeight = run.Height * run.Rows;
            _fineCell = run.WindowSize.x / run.Width;
            _fineSlice = run.Slice;
            _fineOriginSW = new double2(run.PlayableOffset.x + (_fineCell * 0.5), run.PlayableOffset.y + (_fineCell * 0.5));
            run.Fine = null;
            Finish(run, $"Fine terrain: {_fineWidth}x{_fineHeight} @ {_fineCell:F3} m from cascade slice {run.Slice} "
                      + $"({run.Cols}x{run.Rows} windows, {run.Readbacks} readbacks, {run.Clock.Elapsed.TotalSeconds:F1} s)");
        }

        /// <summary>End the capture: restore the camera, record the report, hand the summary on.</summary>
        private string Finish(FineRun run, string summary, bool warn = false)
        {
            if (run.CameraTaken && run.Controller != null)
            {
                try
                {
                    run.Controller.pivot = run.SavedPivot;
                    run.Controller.rotation = run.SavedRotation;
                    run.Controller.zoom = run.SavedZoom;
                }
                catch (Exception ex)
                {
                    _log.Warn($"Fine terrain: could not restore the camera: {ex.Message}");
                }
                run.CameraTaken = false;
            }
            run.Fine = null;
            _fineReport = summary;
            // Any finish without a raster — a hard failure or a soft skip (no cascade, no
            // controller) — goes to warnings.jsonl too: it explains a missing layer.
            _fineWarning = (_fineHeights == null) ? summary : null;
            if (ReferenceEquals(_fine, run)) _fine = null;
            if (run.Started) RipProgress.Clear();
            _log.Info(summary);
            // A run that never started (a synchronous refusal in Begin) reports through Begin's
            // return value; the pipeline sees Running() false and takes that as the summary.
            if (run.Started) run.Completed?.Invoke(summary);
            return summary;
        }

        /// <summary>Drop the captured raster (after RipTerrain wrote it, or on a world unload).</summary>
        public void ReleaseFine()
        {
            _fineHeights = null;
            _fineWidth = _fineHeight = 0;
        }

        /// <summary>
        /// The captured fine raster for other rip systems (the network conform): south-first rows,
        /// the base heightmap's elevation encoding.
        /// </summary>
        public bool TryGetFineHeights(out ushort[] heights, out int width, out int height, out double cell, out double2 originSW)
        {
            heights = _fineHeights;
            width = _fineWidth;
            height = _fineHeight;
            cell = _fineCell;
            originSW = _fineOriginSW;
            return heights != null;
        }

        /// <summary>
        /// The <c>rasters.terrainFine</c> block for the captured raster, or null.
        /// </summary>
        private Dictionary<string, object> FineRastersBlock()
        {
            if (_fineHeights == null) return null;
            return new Dictionary<string, object>
            {
                ["file"] = "terrain/heightmap_fine.png",
                ["size"] = new[] { _fineWidth, _fineHeight },
                ["resolution"] = new[] { Math.Round(_fineCell, 6), Math.Round(_fineCell, 6) },
                ["originWorld"] = new[]
                {
                    Math.Round(_fineOriginSW.x, 3), 0.0,
                    Math.Round(_fineOriginSW.y + (_fineCell * (_fineHeight - 1)), 3)
                },
                ["source"] = $"TerrainSystem height cascade slice {_fineSlice}",
                ["semantics"] = "the ground the game renders near the camera: the same heightmap with the lot/lane/area "
                              + "height adjustments rasterised at this finer texel size (RenderCascade per slice); same "
                              + "elevation decode as heightmap.png; identical to it away from the flattening footprint; "
                              + "prefer it over heightmap.png wherever a consumer meshes the ground"
            };
        }
    }
}
