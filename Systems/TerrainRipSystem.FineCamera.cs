using Carto.IO;
using Carto.Utils;
using Colossal.IO.AssetDatabase;
using Colossal.Logging;
using Colossal.Mathematics;
using Game;
using Game.Net;
using Game.Prefabs;
using Game.Simulation;
using Newtonsoft.Json;
using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using System.Threading.Tasks;
using Unity.Entities;
using Unity.Mathematics;
using UnityEngine;

namespace Carto.Systems
{
    /// <summary>
    /// Aiming the capture camera at one window of the fine cascade, and knowing when it settled.
    /// </summary>
    /// <remarks>
    /// The cascade slice the game keeps near the camera is only correct once the camera has been
    /// where it is for long enough for the terrain system to have filled it. That is what the
    /// settle count and the calibration are for; without them the capture reads whatever the
    /// previous frame left.
    /// </remarks>
    public partial class TerrainRipSystem
    {
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
    }
}
