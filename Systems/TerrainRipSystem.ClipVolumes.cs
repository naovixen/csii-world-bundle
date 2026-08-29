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
    /// <c>clipvolumes.jsonl</c> (§8c): one strip as the two boundary béziers and the band
    /// between them, and the file they are written to.
    /// </summary>
    /// <remarks>
    /// The band is a pair of offsets RELATIVE to the lane surface, not absolute heights — a
    /// consumer intersects the volume with whatever ground it has rather than with the one this
    /// rip saw.
    /// </remarks>
    public partial class TerrainRipSystem
    {
        /// <summary>
        /// Write the collected clip volumes as <c>clipvolumes.jsonl</c> (contract §8c).
        /// <b>This layer costs bytes rather than saving them</b> — on a full city it is roughly
        /// 4× the <c>cutout.png</c> it replaces. It is shipped for fidelity: that mask was a 3.5 m
        /// top-down intersection against <i>one</i> heightmap, so it could not express a volume
        /// open at one height and closed at another, and it went stale the moment a consumer
        /// edited terrain. Nothing else in the bundle carries these curves — <c>networks.jsonl</c>
        /// has no curve field and <c>lanes.jsonl</c> carries lane polylines, not composition
        /// boundaries.
        /// </summary>
        /// <param name="outputDirectory">The bundle root.</param>
        /// <returns>The report line describing the outcome — returned rather than appended
        /// because this runs on the writer task, and the report belongs to the main thread.</returns>
        private string WriteClipVolumes(string outputDirectory)
        {
            if ((_clipVolumes == null) || (_clipVolumes.Count == 0))
            {
                return "Clip volumes: none; clipvolumes.jsonl omitted";
            }

            try
            {
                using var writer = new Carto.IO.RecordWriter(
                    IOUtils.CombinePath(outputDirectory, "clipvolumes.jsonl"));

                foreach (Dictionary<string, object> record in _clipVolumes)
                {
                    writer.Write(record);
                }

                return $"Clip volumes: {_clipVolumes.Count} strip(s) written to clipvolumes.jsonl "
                     + "(what the game's RoadClip pass hides; §8c)";
            }
            catch (Exception ex)
            {
                _log.Warn($"Could not write clipvolumes.jsonl: {ex.Message}");
                return $"Clip volumes: {ex.Message}; layer omitted";
            }
            finally
            {
                _clipVolumes = null;
            }
        }

        /// <summary>
        /// One clipping strip as a <c>clipvolumes.jsonl</c> record (contract §8c).
        /// The two boundary béziers, plus the band as a pair of offsets <b>relative to the lane
        /// surface</b> — which is not shipped separately because it is recoverable from the curves
        /// themselves: at parameter <c>t</c> the lane height is the mean of the two boundaries'
        /// <c>y</c>, exactly as the stamp below takes it from the four corners of each quad.
        /// </summary>
        /// <param name="context">The pass in progress, for the band and the strip's provenance.</param>
        /// <param name="segment">The trimmed boundary curves.</param>
        /// <returns>The record.</returns>
        private static Dictionary<string, object> ClipVolumeRecord(in StampContext context, Segment segment)
        {
            return new Dictionary<string, object>
            {
                ["id"] = context.Owner,
                ["kind"] = context.Kind,
                ["left"] = Curve(segment.m_Left),
                ["right"] = Curve(segment.m_Right),
                ["bottom"] = Math.Round((double)context.CutBottom, 3),
                ["top"] = Math.Round((double)context.CutTop, 3)
            };
        }

        /// <summary>
        /// A boundary bézier as its four control points, millimetre-rounded like every other
        /// coordinate in the bundle.
        /// </summary>
        /// <param name="curve">The curve.</param>
        /// <returns>Four <c>[x, y, z]</c> triples.</returns>
        private static double[][] Curve(Bezier4x3 curve)
        {
            return new[]
            {
                BundlePrecision.Vector(curve.a), BundlePrecision.Vector(curve.b),
                BundlePrecision.Vector(curve.c), BundlePrecision.Vector(curve.d)
            };
        }

        /// <summary>
        /// Lerp one boundary bézier toward another by a per-control-point parameter — the game's
        /// <c>MathUtils.Lerp(Bezier4x3, Bezier4x3, Bezier4x1)</c>.
        /// </summary>
        private static Bezier4x3 LerpCurve(Bezier4x3 from, Bezier4x3 to, float4 t)
        {
            return new Bezier4x3(
                math.lerp(from.a, to.a, t.x),
                math.lerp(from.b, to.b, t.y),
                math.lerp(from.c, to.c, t.z),
                math.lerp(from.d, to.d, t.w));
        }
    }
}
