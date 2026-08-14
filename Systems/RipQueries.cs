using Unity.Entities;

namespace Carto.Systems
{
    /// <summary>
    /// Entity queries shared by more than one rip stage, so that a rule about <i>what the bundle
    /// contains</i> lives in one place.
    /// </summary>
    public static class RipQueries
    {
        /// <summary>
        /// Every placed world object the bundle describes: the query the instance pass enumerates
        /// records from and the mesh pass counts prefab usage with.
        /// <para>
        /// <b>It is one query because the two layers must describe the same world.</b> Both stages
        /// carried their own copy of this descriptor, byte-for-byte identical, with the reasoning
        /// on only one of them — and the exclusions are the dangerous half, because a rule matrix
        /// has no row for absence. If the copies ever drifted, the mesh layer and the instance
        /// layer would disagree about which objects exist and nothing would say so: the symptom
        /// would be an <c>assetId</c> nobody places, or a placement with no mesh, both of which
        /// have benign explanations of their own.
        /// </para>
        /// <para>
        /// What each exclusion is for:
        /// </para>
        /// <list type="bullet">
        /// <item><description><c>Moving</c>, <c>Deleted</c>, <c>Temp</c> — things in motion are
        /// vehicles and citizens, not world geometry; the other two are mid-edit bookkeeping that
        /// must never reach the bundle.</description></item>
        /// <item><description><c>Marker</c> — spawn points, pedestrian access, pipe and cable
        /// nodes: editor gizmos the game never draws. They are numerous enough to reach the top of
        /// the prefab usage table, so keeping them would scatter stand-in boxes across the whole
        /// reconstruction in exchange for nothing anyone can see.</description></item>
        /// <item><description><c>Creature</c>, <c>Vehicle</c> — agents, not scenery.
        /// <c>Moving</c> only excludes the ones in motion at the instant of export: a citizen
        /// waiting at a stop and a parked car have no <c>Moving</c>, so they were being written
        /// into the bundle as world geometry. A level is the built environment; the traffic and
        /// pedestrians passing through it at the moment someone pressed the button are
        /// not.</description></item>
        /// </list>
        /// </summary>
        /// <returns>The descriptor to hand <c>GetEntityQuery</c>.</returns>
        public static EntityQueryDesc WorldObjects() => new()
        {
            All = new ComponentType[]
            {
                ComponentType.ReadOnly<Game.Objects.Object>(),
                ComponentType.ReadOnly<Game.Objects.Transform>(),
                ComponentType.ReadOnly<Game.Prefabs.PrefabRef>()
            },
            None = new ComponentType[]
            {
                ComponentType.ReadOnly<Game.Objects.Moving>(),
                ComponentType.ReadOnly<Game.Common.Deleted>(),
                ComponentType.ReadOnly<Game.Tools.Temp>(),
                ComponentType.ReadOnly<Game.Objects.Marker>(),
                ComponentType.ReadOnly<Game.Creatures.Creature>(),
                ComponentType.ReadOnly<Game.Vehicles.Vehicle>()
            }
        };
    }
}
