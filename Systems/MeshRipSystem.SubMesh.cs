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
    /// The game's own rule for which sub-meshes of a prefab are the mesh, ported.
    /// </summary>
    /// <remarks>
    /// A prefab's SubMesh buffer mixes simultaneous PARTS with mutually exclusive STATE
    /// variants, and only the game's flag rules tell them apart. Getting this wrong does not
    /// crash: it ships a building with its scaffolding, or a tree in every season at once.
    /// </remarks>
    public partial class MeshRipSystem
    {
        /// <summary>
        /// Every <c>Require*</c> bit, i.e. every flag that makes a sub-mesh conditional.
        /// </summary>
        private const SubMeshFlags RequireFlags =
            SubMeshFlags.RequireSafe | SubMeshFlags.RequireLevelCrossing | SubMeshFlags.RequireChild
            | SubMeshFlags.RequireTeen | SubMeshFlags.RequireAdult | SubMeshFlags.RequireElderly
            | SubMeshFlags.RequireDead | SubMeshFlags.RequireStump | SubMeshFlags.RequireEmpty
            | SubMeshFlags.RequireFull | SubMeshFlags.RequireEditor | SubMeshFlags.RequireClear
            | SubMeshFlags.RequireTrack | SubMeshFlags.RequireLeftHandTraffic
            | SubMeshFlags.RequireRightHandTraffic | SubMeshFlags.RequirePartial1
            | SubMeshFlags.RequirePartial2 | SubMeshFlags.RequireForward | SubMeshFlags.RequireBackward;

        /// <summary>
        /// The state a ripped prefab is exported in.
        /// A <c>SubMesh</c> buffer mixes two different things: parts drawn <i>together</i> (a
        /// building and its roof units) and <i>alternatives</i> the game picks between at draw time
        /// (a sapling, an adult tree, a dead tree and a stump are four entries on one tree prefab).
        /// Exporting all of them would stack every alternative into the same file. One state is
        /// therefore fixed and documented for the prefab's own file: a healthy adult, unloaded,
        /// undamaged, forward-facing. Traffic side is not a choice — it is read from the city.
        /// The alternatives an instance can actually be in — see <see cref="StateVariants"/> — get
        /// a derived file each rather than being lost.
        /// </summary>
        public static SubMeshFlags DefaultState => SubMeshFlags.RequireSafe | SubMeshFlags.RequireAdult
            | SubMeshFlags.RequireEmpty | SubMeshFlags.RequireClear | SubMeshFlags.RequireForward
            | (LeftHandTraffic ? SubMeshFlags.RequireLeftHandTraffic : SubMeshFlags.RequireRightHandTraffic);

        /// <summary>
        /// The city's traffic side, read from the city system once per rip rather than through
        /// the service locator on every sub-mesh selection (a system lookup per call, per prefab
        /// and per state variant). Refreshed at the start of a run; a city does not change sides
        /// mid-rip.
        /// </summary>
        private static bool LeftHandTraffic => _leftHandTraffic ??= Instance.City.leftHandTraffic;
        private static bool? _leftHandTraffic;

        /// <summary>Forget the cached traffic side (a new run, possibly a new city).</summary>
        private static void ResetTrafficSide() => _leftHandTraffic = null;
    }
}
