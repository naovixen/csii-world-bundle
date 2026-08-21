using System;
using System.Collections.Generic;
using System.Text;

using Game.Prefabs;
using Unity.Entities;

namespace Carto.Systems
{
    /// <summary>
    /// What an <c>assetId</c> is, and how one is assigned so that every layer of the bundle joins
    /// on the same key.
    /// </summary>
    /// <remarks>
    /// This lived inside <see cref="MeshRipSystem"/>, which is not whose rule it is: five systems
    /// call it — the mesh, instance, material, appearance and network rips — and every one of them
    /// had to say <c>MeshRipSystem.</c> for a policy that belongs to the bundle contract. Moved out
    /// verbatim by refactor Phase 7; the behaviour, the comments and the measurements they carry
    /// are unchanged.
    /// </remarks>
    public static class RipAssetIds
    {
        /// <summary>
        /// Reduce a prefab name to the bundle contract's <c>assetId</c> pattern
        /// (<c>^[A-Za-z0-9][A-Za-z0-9._-]*$</c>), so it is safe both as a file name and as a
        /// cross-file join key.
        /// </summary>
        /// <remarks>
        /// <b>Test the characters explicitly, not with <c>char.IsLetterOrDigit</c></b>
        /// <i>(2026-08-14, H12)</i>. <c>NetworkRipSystem</c> once carried a second sanitiser that
        /// did, and <c>IsLetterOrDigit</c> is Unicode-aware: a non-Latin letter came through
        /// unchanged and produced an id the contract's <c>^[A-Za-z0-9][A-Za-z0-9._-]*$</c> pattern
        /// rejects — an invalid file name in <c>networks/</c> that a validator flags as a bad stem.
        /// That sanitiser became a one-line alias for this method and, by refactor Phase 7, had no
        /// callers left at all; the finding outlives it.
        /// </remarks>
        /// <param name="name">The raw prefab name.</param>
        /// <returns>The sanitised asset id.</returns>
        public static string SanitizeAssetId(string name)
        {
            if (string.IsNullOrWhiteSpace(name)) return "unnamed";

            StringBuilder builder = new(name.Length);
            foreach (char c in name)
            {
                bool keep = ((c >= 'A') && (c <= 'Z')) || ((c >= 'a') && (c <= 'z'))
                         || ((c >= '0') && (c <= '9')) || (c == '.') || (c == '_') || (c == '-');
                builder.Append(keep ? c : '_');
            }

            string result = builder.ToString();

            // The contract requires the first character to be alphanumeric.
            //
            int start = 0;
            while ((start < result.Length) && !char.IsLetterOrDigit(result[start])) start++;

            // Where sanitising destroys the name, the id must not. A prefab named entirely in a
            // non-Latin script reduces to nothing at all, and a first full-city rip proved the
            // consequence: five such prefabs all became `unnamed`, so four of their meshes were
            // silently overwritten by the fifth and the instance layer referenced one id for five
            // different assets. Falling back on a digest of the original name keeps distinct
            // prefabs distinct, and keeps both layers agreeing on the id, since both derive it
            // from the same prefab name.
            //
            if (start >= result.Length) return $"asset_{Digest(name)}";

            result = result.Substring(start);

            // Truncation collides the same way, just more rarely: two long names sharing a prefix.
            // Capped at 160 rather than the contract's 180 so a derived id — `__stack_first` and
            // friends add up to 13 characters — still fits the schema's assetId pattern.
            //
            return (result.Length > 160) ? $"{result.Substring(0, 151)}_{Digest(name)}" : result;
        }

        /// <summary>
        /// A prefab's bundle <c>assetId</c>, and whether its managed prefab resolved at all.
        /// </summary>
        public struct AssetIdEntry
        {
            /// <summary>The id both bundle layers join on.</summary>
            public string AssetId;

            /// <summary>Whether a managed prefab was found behind the entity.</summary>
            public bool Resolved;
        }

        /// <summary>
        /// Assign every prefab entity its bundle <c>assetId</c>, guaranteeing the ids are unique.
        /// <b>The id is the bundle's join key, so a duplicate is silent corruption:</b> two prefabs
        /// sharing an id means one mesh file overwrites the other and every instance of both then
        /// points at whichever won. A full-city rip proved it happens — writing 4,203 files left
        /// 4,201 on disk — because CS2 does not require prefab names to be unique.
        /// <see cref="SanitizeAssetId"/> alone cannot see this: it is handed one name at a time and
        /// duplication is a property of the whole set.
        /// Only the names that actually clash are suffixed, so ordinary ids stay readable. The
        /// suffix is a digest of the prefab's asset identifier, which makes the assignment a
        /// function of the prefab set rather than of iteration order — that is what lets
        /// <see cref="InstanceRipSystem"/> and this system compute the same ids independently.
        /// </summary>
        /// <param name="entityManager">The entity manager to read prefab data from.</param>
        /// <param name="prefabEntities">The distinct prefab entities to assign ids to.</param>
        /// <param name="disambiguated">Receives how many ids needed a suffix.</param>
        /// <returns>The id for every entity given.</returns>
        public static Dictionary<Entity, AssetIdEntry> BuildAssetIds(EntityManager entityManager, ICollection<Entity> prefabEntities, out int disambiguated)
        {
            Dictionary<Entity, AssetIdEntry> result = new(prefabEntities.Count);
            Dictionary<string, List<Entity>> byBaseId = new(prefabEntities.Count);
            Dictionary<Entity, string> discriminators = new(prefabEntities.Count);

            foreach (Entity prefabEntity in prefabEntities)
            {
                PrefabBase prefab = null;
                if (entityManager.HasComponent<PrefabData>(prefabEntity))
                    Instance.Prefab.TryGetPrefab(entityManager.GetComponentData<PrefabData>(prefabEntity), out prefab);

                bool resolved = (prefab != null) && !string.IsNullOrEmpty(prefab.name);
                string baseId = resolved ? SanitizeAssetId(prefab.name) : $"unresolved_{prefabEntity.Index}";

                result[prefabEntity] = new AssetIdEntry { AssetId = baseId, Resolved = resolved };
                discriminators[prefabEntity] = Discriminator(prefab, prefabEntity);

                if (!byBaseId.TryGetValue(baseId, out List<Entity> sharing)) byBaseId[baseId] = sharing = new List<Entity>();
                sharing.Add(prefabEntity);
            }

            disambiguated = 0;
            foreach (KeyValuePair<string, List<Entity>> group in byBaseId)
            {
                if (group.Value.Count < 2) continue;

                foreach (Entity prefabEntity in group.Value)
                {
                    AssetIdEntry entry = result[prefabEntity];
                    entry.AssetId = $"{group.Key}_{Digest(discriminators[prefabEntity])}";
                    result[prefabEntity] = entry;
                    disambiguated++;
                }
            }

            return result;
        }

        /// <summary>
        /// Something that tells two same-named prefabs apart, stably across sessions.
        /// The asset identifier is unique per asset and survives a reload, which the entity index
        /// does not — an id derived from the entity index would change every time the save is
        /// loaded, and the two bundle layers are written by two separate button presses.
        /// </summary>
        /// <param name="prefab">The managed prefab, if it resolved.</param>
        /// <param name="prefabEntity">The prefab entity, as a last resort.</param>
        /// <returns>A discriminating string.</returns>
        private static string Discriminator(PrefabBase prefab, Entity prefabEntity)
        {
            if (prefab == null) return $"entity:{prefabEntity.Index}";

            try
            {
                string identifier = prefab.asset?.identifier;
                if (!string.IsNullOrEmpty(identifier)) return identifier;
            }
            catch (Exception)
            {
                // An asset-less prefab (generated at runtime) throws rather than returning null.
                //
            }

            return $"{prefab.GetType().Name}:{prefab.name}";
        }

        /// <summary>
        /// A short, stable digest of a string, used to keep asset ids distinct when sanitising
        /// cannot.
        /// FNV-1a, written out rather than taken from <c>string.GetHashCode</c>, which is
        /// randomised per process and would hand the same prefab a different id on every run.
        /// </summary>
        /// <param name="value">The original, unsanitised name.</param>
        /// <returns>Eight lowercase hex digits.</returns>
        private static string Digest(string value)
        {
            uint hash = 2166136261u;
            foreach (char c in value)
            {
                hash ^= c;
                hash *= 16777619u;
            }

            return hash.ToString("x8");
        }
    }
}
