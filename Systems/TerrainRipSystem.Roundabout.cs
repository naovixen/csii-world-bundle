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
    /// Which composition and ring a roundabout node's SECOND strip pair takes.
    /// </summary>
    /// <remarks>
    /// <c>AddNode</c> sheds the composition state and trims the ring for that pair, and a
    /// roundabout at grade answers differently from one that is not. Both rules are the game's,
    /// read out of <c>CullRoadsJob</c>, and they are here rather than beside the clip walk
    /// because they are a question about geometry, not about clipping.
    /// </remarks>
    public partial class TerrainRipSystem
    {
        /// <summary>
        /// The composition and ring trims the game hands the roundabout's SECOND strip pair —
        /// <c>CullRoadsJob.AddNode</c>'s <c>m_MiddleRadius &gt; 0</c> branch.
        /// <para>
        /// The game does not evaluate that pair against the composition stored on the entity. It
        /// copies it, and where a side carries an elevation transition it sheds the
        /// <c>Elevated</c> or <c>Tunnel</c> general flag and replaces the transition side flag
        /// with the state the ring is transitioning INTO — <c>Raised</c> under an elevated
        /// composition, <c>Lowered</c> under a tunnel. That moves the band onto a different
        /// branch of <c>AddSegment</c>: an elevated composition's band is never raised to
        /// <c>ElevationLimit·3</c>, but once the flag is shed and <c>Lowered</c> is set, it is.
        /// </para>
        /// <para>
        /// It also trims the ring ends toward the middle curve's far point by the sync vertex
        /// offsets, scaled by the distance across the node at that end. The trims shorten the
        /// stamped footprint; omitting them (which this used to) over-stamps, and over-stamping
        /// is the direction that deletes terrain no road covers.
        /// </para>
        /// </summary>
        /// <returns>Whether the composition was mutated at all; when false the caller keeps the
        /// stored composition's band and both trims are zero.</returns>
        private bool RoundaboutSecondPair(Entity composition, NetGeometryData netGeometry,
                                          EdgeNodeGeometry node, out NetCompositionData mutated,
                                          out float trimLeft, out float trimRight)
        {
            mutated = default;
            trimLeft = 0f;
            trimRight = 0f;
            if ((composition == Entity.Null) || !EntityManager.HasComponent<NetCompositionData>(composition)) return false;

            NetCompositionData data = EntityManager.GetComponentData<NetCompositionData>(composition);
            mutated = data;
            bool elevated = (data.m_Flags.m_General & CompositionFlags.General.Elevated) != 0;
            bool tunnel = (data.m_Flags.m_General & CompositionFlags.General.Tunnel) != 0;

            // AddNode has THREE branches here, not two, and this port had only the first two
            // (2026-08-14). The third is the at-grade case: a side carrying `LowTransition`
            // together with `Raised`, `Lowered` or `SoundBarrier` gets the same ring trim, and
            // the game CLEARS both bits before the second pair is banded. Keeping them raised
            // `cutTop` to `ElevationLimit * 3` — 12 m on a road — over an untrimmed ring, which
            // carves terrain the game leaves standing. The case in the wild is a roundabout node
            // on a road coming out of a trench (`Lowered | LowTransition`).
            //
            if (!elevated && !tunnel) return AtGradeSecondPair(data, node, ref mutated,
                                                               ref trimLeft, ref trimRight);

            CompositionFlags.General shed = elevated ? CompositionFlags.General.Elevated
                                                     : CompositionFlags.General.Tunnel;
            // An elevated ring transitions down into Raised; a tunnel ring up into Lowered.
            //
            CompositionFlags.Side into = elevated ? CompositionFlags.Side.Raised
                                                  : CompositionFlags.Side.Lowered;

            if ((data.m_Flags.m_Left & CompositionFlags.Side.HighTransition) != 0)
            {
                trimLeft = data.m_SyncVertexOffsetsLeft.x;
                mutated.m_Flags.m_General &= ~shed;
                mutated.m_Flags.m_Left &= ~CompositionFlags.Side.HighTransition;
            }
            else if ((data.m_Flags.m_Left & CompositionFlags.Side.LowTransition) != 0)
            {
                trimLeft = data.m_SyncVertexOffsetsLeft.x;
                mutated.m_Flags.m_General &= ~shed;
                mutated.m_Flags.m_Left &= ~CompositionFlags.Side.LowTransition;
                mutated.m_Flags.m_Left |= into;
            }

            if ((data.m_Flags.m_Right & CompositionFlags.Side.HighTransition) != 0)
            {
                trimRight = 1f - data.m_SyncVertexOffsetsRight.w;
                mutated.m_Flags.m_General &= ~shed;
                mutated.m_Flags.m_Right &= ~CompositionFlags.Side.HighTransition;
            }
            else if ((data.m_Flags.m_Right & CompositionFlags.Side.LowTransition) != 0)
            {
                trimRight = 1f - data.m_SyncVertexOffsetsRight.w;
                mutated.m_Flags.m_General &= ~shed;
                mutated.m_Flags.m_Right &= ~CompositionFlags.Side.LowTransition;
                mutated.m_Flags.m_Right |= into;
            }

            // The offsets are fractions of the node's width at each end, so they scale by the
            // distance from the ring's start to the middle curve — the game's own scaling.
            //
            if (trimLeft != 0f)
            {
                trimLeft *= math.distance(node.m_Left.m_Left.a.xz, node.m_Middle.a.xz);
            }
            if (trimRight != 0f)
            {
                trimRight *= math.distance(node.m_Middle.a.xz, node.m_Left.m_Right.a.xz);
            }
            return true;
        }

        /// <summary>
        /// <c>AddNode</c>'s third branch: an at-grade composition whose side carries
        /// <c>LowTransition</c> beside <c>Raised</c>, <c>Lowered</c> or <c>SoundBarrier</c>.
        /// <para>
        /// Unlike the elevated and tunnel branches this one sets nothing — it only <b>clears</b>
        /// the pair of bits, and the clearing is what matters: <c>TryGetClipBand</c>'s
        /// <c>(!tunnel &amp;&amp; lowered)</c> raise fires on the stored flags and lifts the band to
        /// <c>ElevationLimit * 3</c>, which over a roundabout ring is 12 m of terrain deleted
        /// where the game deletes none. A side with <c>LowTransition</c> and none of the three
        /// states is left alone, exactly as the game leaves it.
        /// </para>
        /// </summary>
        /// <returns>Whether anything was cleared; false leaves the caller on the stored band.</returns>
        private static bool AtGradeSecondPair(NetCompositionData data, EdgeNodeGeometry node,
                                              ref NetCompositionData mutated,
                                              ref float trimLeft, ref float trimRight)
        {
            bool changed = false;

            if ((data.m_Flags.m_Left & CompositionFlags.Side.LowTransition) != 0)
            {
                CompositionFlags.Side state = FirstState(data.m_Flags.m_Left);
                if (state != 0)
                {
                    trimLeft = data.m_SyncVertexOffsetsLeft.x;
                    mutated.m_Flags.m_Left &= ~(state | CompositionFlags.Side.LowTransition);
                    changed = true;
                }
            }

            if ((data.m_Flags.m_Right & CompositionFlags.Side.LowTransition) != 0)
            {
                CompositionFlags.Side state = FirstState(data.m_Flags.m_Right);
                if (state != 0)
                {
                    trimRight = 1f - data.m_SyncVertexOffsetsRight.w;
                    mutated.m_Flags.m_Right &= ~(state | CompositionFlags.Side.LowTransition);
                    changed = true;
                }
            }

            if (trimLeft != 0f) trimLeft *= math.distance(node.m_Left.m_Left.a.xz, node.m_Middle.a.xz);
            if (trimRight != 0f) trimRight *= math.distance(node.m_Middle.a.xz, node.m_Left.m_Right.a.xz);
            return changed;

            // The game tests Raised, then Lowered, then SoundBarrier, and takes the first — so a
            // side carrying two of them keeps the later one. Order is load-bearing.
            //
            static CompositionFlags.Side FirstState(CompositionFlags.Side side)
            {
                if ((side & CompositionFlags.Side.Raised) != 0) return CompositionFlags.Side.Raised;
                if ((side & CompositionFlags.Side.Lowered) != 0) return CompositionFlags.Side.Lowered;
                if ((side & CompositionFlags.Side.SoundBarrier) != 0) return CompositionFlags.Side.SoundBarrier;
                return (CompositionFlags.Side)0;
            }
        }
    }
}
