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
    /// The §4 per-record attributes: what a placed object carries besides its transform.
    /// </summary>
    public partial class InstanceRipSystem
    {
        /// <summary>
        /// Attach the semantic attributes that are cheap and reliable to read.
        /// </summary>
        /// <param name="entity">The object entity.</param>
        /// <param name="prefabEntity">Its prefab.</param>
        /// <param name="kind">The classification already decided for it.</param>
        /// <param name="record">The record to populate.</param>
        private void AddAttributes(Entity entity, Entity prefabEntity, string kind, InstanceRecord record)
        {
            // How far the object stands *above* the ground, not the ground's own height — the two
            // were confused in the contract's wording for a while, and the difference is the whole
            // value of the field. The game computes it as
            // `max(0, transform.y - SampleHeight(terrain, pos))` in
            // `ObjectToolBaseSystem.CreateDefinitionsJob`, so it is zero for anything sitting on
            // the ground and the component is simply absent for most objects.
            //
            if (_elevations.TryGetComponent(entity, out Game.Objects.Elevation elevation))
            {
                record.Attributes["elevation"] = Math.Round((double)elevation.m_Elevation, 3);

                // The stored component is *not* clamped at zero — a full city measured 29,610
                // negative values (audit §4.1's own correction); only the placement-definition
                // path clamps. The flags still matter: `Lowered` is how the game marks a
                // deliberate below-ground placement, which a consumer cutting terrain needs.
                //
                string[] flags = FlagNames(elevation.m_Flags.ToString());
                if (flags.Length > 0) record.Attributes["elevationFlags"] = flags;
            }

            // A decal is painted onto something, and which something is not a guess a consumer
            // should have to make — see the contract's §4.2a.
            //
            if (kind == "decal")
            {
                DecalInfo info = DecalOf(prefabEntity);

                string[] onto = FlagNames(info.Layers.ToString());
                if (onto.Length > 0) record.Attributes["decalLayers"] = onto;

                // The mesh is the painted quad, so the volume it was projected through is the one
                // thing the mesh layer no longer says. See `MeshRipSystem.TryFlattenDecal`.
                //
                if (info.Height > 0f) record.Attributes["decalHeight"] = math.round(info.Height * 1000f) / 1000f;
                // The game's own draw order for overlapping decals (audit §57). Additive attr;
                // omitted where the prefab carries no `DecalProperties`, so its absence means
                // "the game states no order here" rather than "priority zero".
                if (info.Priority != int.MinValue) record.Attributes["decalPriority"] = info.Priority;
            }

            // The paint this particular object was drawn with — the game's own resolved answer,
            // not a key to look it up by. `Game.Objects.Color` is **not** that answer: its only
            // writer is `ObjectColorSystem`, which fills it from the active *infoview* — the value
            // is UI-overlay state that changes with whichever info mode was last open, and a
            // previous version of this attribute exported it as the paint choice (357,176 records
            // of infoview state in one full city). The real choice is computed by
            // `MeshColorSystem` from `PseudoRandomSeed`, the palette's probabilities and the
            // season filters, and stored per instance in the `MeshColor` buffer — recomputed for
            // every entity on load, so it is always populated by the time a rip can run.
            // `CustomMeshColor` overrides it wholesale when the player painted the thing
            // (`SetCustomColor` applies entry 0 to every part).
            //
            if (_customMeshColors.TryGetBuffer(entity, out DynamicBuffer<Game.Rendering.CustomMeshColor> custom)
                && (custom.Length > 0))
            {
                record.Attributes["colorSet"] = ColorSetJson(custom[0].m_ColorSet);
                record.Attributes["colorCustom"] = true;
                _colorSets++;
                _customColors++;
            }
            else if (_meshColors.TryGetBuffer(entity, out DynamicBuffer<Game.Rendering.MeshColor> colors))
            {
                if (colors.Length > 0)
                {
                    record.Attributes["colorSet"] = ColorSetJson(colors[0].m_ColorSet);
                    _colorSets++;

                    // Parts of one instance can resolve different sets (per-part random seeds).
                    // The first part's set is exported; count how often that approximates.
                    //
                    for (int i = 1; i < colors.Length; i++)
                    {
                        if (!colors[i].m_ColorSet.Equals(colors[0].m_ColorSet)) { _divergentColorSets++; break; }
                    }
                }
            }

            // THE GAME DOES NOT DRAW AN OVERRIDDEN OBJECT. `Game.Common.Overridden` is what CS2
            // marks a tree, prop or lane that something else was built over, and
            // `Game.Rendering.PreCullingSystem` leaves such an entity's culling mask at
            // `BoundsMask.Debug` — it never ORs in the prefab's real mesh layers, at either of
            // the two sites that test it. `BoundsMask` even carries a `NotOverridden` bit. The
            // object still exists, still has its transform, and is simply not rendered.
            //
            // Shipped as an attribute rather than dropped from the rip: the bundle describes the
            // city, and `csiibundle.instances.should_draw` is where a consumer asks whether to
            // draw a record — the same seam `state: "construction"` already uses.
            //
            if (_overridden.HasComponent(entity)) record.Attributes["overridden"] = true;

            if (_trees.TryGetComponent(entity, out Tree tree))
            {
                // m_Growth is a byte ramp; the contract defines growth as 0..1.
                //
                record.Attributes["growth"] = Math.Round(tree.m_Growth / 255d, 3);

                // The size the game actually draws this tree at. The renderer multiplies the
                // instance transform by a scale from `BatchDataHelpers.CalculateTreeSubMeshData` —
                // a √-lerp between the prefab's per-stage sizes (`GrowthScaleData`) — and a
                // consumer cannot reproduce it from `growth` alone because the per-stage sizes are
                // prefab data the bundle does not carry. Uniform, so one number.
                //
                if (EntityManager.HasComponent<GrowthScaleData>(prefabEntity))
                {
                    GrowthScaleData growthScale = EntityManager.GetComponentData<GrowthScaleData>(prefabEntity);
                    Game.Rendering.BatchDataHelpers.CalculateTreeSubMeshData(tree, growthScale, out float3 treeScale);
                    if (math.abs(treeScale.x - 1f) > 1e-3f)
                    {
                        record.Attributes["growthScale"] = Math.Round((double)treeScale.x, 4);
                        _growthScales++;
                    }
                }

                // Not `state`: the contract reserves that key for the building lifecycle enum, and
                // a tree's vocabulary (Teen/Adult/Elderly/Dead/Stump/Collected) is a different one.
                // A sapling has no flag set at all, where ToString() yields "0" — omit rather than
                // emit a meaningless value, since `growth` already carries the information.
                //
                string treeState = tree.m_State.ToString();
                if (!string.IsNullOrEmpty(treeState) && !char.IsDigit(treeState[0]))
                    record.Attributes["treeState"] = treeState;
            }

            if (_buildings.HasComponent(entity))
            {
                if (EntityManager.HasComponent<Game.Common.Destroyed>(entity)) record.Attributes["state"] = "destroyed";
                else if (EntityManager.HasComponent<Game.Buildings.Abandoned>(entity)) record.Attributes["state"] = "abandoned";
                else if (EntityManager.HasComponent<Game.Buildings.Condemned>(entity)) record.Attributes["state"] = "condemned";
                else if (EntityManager.HasComponent<Game.Objects.UnderConstruction>(entity)) record.Attributes["state"] = "construction";
                else record.Attributes["state"] = "normal";

                // The zoned-building metadata the contract documents and audit P12 found never
                // produced. `level` and the zone link are one prefab-side component
                // (SpawnableBuildingData); the zone strings are cached per zone prefab.
                //
                if (EntityManager.HasComponent<SpawnableBuildingData>(prefabEntity))
                {
                    SpawnableBuildingData spawnable = EntityManager.GetComponentData<SpawnableBuildingData>(prefabEntity);
                    record.Attributes["level"] = (int)spawnable.m_Level;
                    _levels++;

                    if (spawnable.m_ZonePrefab != Entity.Null)
                    {
                        (string zone, string zoning) = ZoneOf(spawnable.m_ZonePrefab);
                        if (zone != null) record.Attributes["zone"] = zone;
                        if (zoning != null) record.Attributes["zoning"] = zoning;
                        if ((zone != null) || (zoning != null)) _zones++;
                    }
                }

                // The brand of the company renting the building — `CompanyData.m_Brand` on a
                // renter, resolved to the brand prefab's name. Household renters carry no
                // CompanyData; the first branded renter wins, matching the signage the game
                // actually draws for the building.
                //
                if (EntityManager.HasBuffer<Game.Buildings.Renter>(entity))
                {
                    DynamicBuffer<Game.Buildings.Renter> renters = EntityManager.GetBuffer<Game.Buildings.Renter>(entity, true);
                    for (int i = 0; i < renters.Length; i++)
                    {
                        Entity renter = renters[i].m_Renter;
                        if ((renter == Entity.Null) || !EntityManager.HasComponent<Game.Companies.CompanyData>(renter)) continue;

                        Entity brand = EntityManager.GetComponentData<Game.Companies.CompanyData>(renter).m_Brand;
                        string brandName = (brand != Entity.Null) ? Instance.Name.GetPrefabName(brand) : null;
                        if (!string.IsNullOrEmpty(brandName))
                        {
                            record.Attributes["brand"] = brandName;
                            _brands++;
                        }

                        break;
                    }
                }

                // The postal address, by the game's own resolver (`BuildingUtils.GetAddress`,
                // the walk along the road-edge aggregate the info panel uses) plus the district
                // the simulation keeps on the building. The contract's documented shape:
                // [district, street, number], district null outside every district.
                //
                if (Game.Buildings.BuildingUtils.GetAddress(EntityManager, entity, out Entity street, out int number)
                    && (street != Entity.Null))
                {
                    Entity district = EntityManager.HasComponent<Game.Areas.CurrentDistrict>(entity)
                        ? EntityManager.GetComponentData<Game.Areas.CurrentDistrict>(entity).m_District
                        : Entity.Null;

                    record.Attributes["address"] = new object[]
                    {
                        (district != Entity.Null) ? Instance.Name.GetLabelName(district) : null,
                        Instance.Name.GetLabelName(street),
                        number
                    };
                    _addresses++;
                }
            }

            // The player's own name for the object, when one was given. The generated display
            // name is locale output derived from prefab and address and is not repeated here;
            // the custom name is the one piece of naming that is city data (the NameSystem's
            // custom-name table — one dictionary lookup per object).
            //
            if ((_nameSystem != null) && _nameSystem.TryGetCustomName(entity, out string customName)
                && !string.IsNullOrEmpty(customName))
            {
                record.Attributes["name"] = customName;
                _customNames++;
            }

            // The content theme the prefab belongs to (European / North American, seasonal
            // packs): `ThemeObject` bakes it into the prefab's requirement buffer as the entry
            // whose target carries `ThemeData`. Cached per prefab — one walk each.
            //
            string theme = ThemeOf(prefabEntity);
            if (theme != null)
            {
                record.Attributes["theme"] = theme;
                _themes++;
            }

            // Stockpile fullness (garbage heaps, coal and log piles): serialized sim state, the
            // value `BatchDataHelpers.CalculateQuantitySubMeshData` selects the Empty / Partial1 /
            // Partial2 / Full sub-mesh variants by. The game stores a byte percentage 0..100
            // (QuantityUpdateSystem rounds fraction × 100) and it is shipped raw.
            //
            if (EntityManager.HasComponent<Quantity>(entity))
            {
                record.Attributes["fullness"] = (int)EntityManager.GetComponentData<Quantity>(entity).m_Fullness;
                _fullness++;
            }
        }

        /// <summary>
        /// A zone prefab's exported strings, cached: <c>zone</c> is the prefab's own name (the
        /// specific zoned type, e.g. <c>NA Residential Low</c>); <c>zoning</c> is the coarse class
        /// from <c>ZoneData</c> — residential/commercial/industrial by <c>m_AreaType</c>, office
        /// carved out of industrial by the <c>Office</c> flag, exactly the split the game's own
        /// zoning UI makes.
        /// </summary>
        /// <param name="zonePrefab">The zone prefab.</param>
        /// <returns>The strings, either possibly <c>null</c>.</returns>
        private (string Zone, string Zoning) ZoneOf(Entity zonePrefab)
        {
            if (_zoneTypes.TryGetValue(zonePrefab, out (string Zone, string Zoning) known)) return known;

            string name = Instance.Name.GetPrefabName(zonePrefab);
            string area = null;

            if (EntityManager.HasComponent<ZoneData>(zonePrefab))
            {
                ZoneData zoneData = EntityManager.GetComponentData<ZoneData>(zonePrefab);
                area = zoneData.m_AreaType switch
                {
                    Game.Zones.AreaType.Residential => "residential",
                    Game.Zones.AreaType.Commercial => "commercial",
                    Game.Zones.AreaType.Industrial => ((zoneData.m_ZoneFlags & ZoneFlags.Office) != 0) ? "office" : "industrial",
                    _ => null
                };
            }

            (string, string) result = (string.IsNullOrEmpty(name) ? null : name, area);
            _zoneTypes[zonePrefab] = result;
            return result;
        }

        /// <summary>
        /// The theme an object prefab belongs to, or <c>null</c>, cached. <c>ThemeObject</c> has
        /// no marker of its own on the object prefab: it adds an <c>ObjectRequirementElement</c>
        /// whose requirement entity is the theme prefab (the entity carrying <c>ThemeData</c>),
        /// so that entry is what identifies the theme.
        /// </summary>
        /// <param name="prefabEntity">The object prefab.</param>
        /// <returns>The theme prefab's name, or <c>null</c>.</returns>
        private string ThemeOf(Entity prefabEntity)
        {
            if (_prefabThemes.TryGetValue(prefabEntity, out string known)) return known;

            string theme = null;
            if (EntityManager.HasBuffer<ObjectRequirementElement>(prefabEntity))
            {
                DynamicBuffer<ObjectRequirementElement> requirements = EntityManager.GetBuffer<ObjectRequirementElement>(prefabEntity, true);
                for (int i = 0; i < requirements.Length; i++)
                {
                    Entity requirement = requirements[i].m_Requirement;
                    if ((requirement == Entity.Null) || !EntityManager.HasComponent<ThemeData>(requirement)) continue;

                    string themeName = Instance.Name.GetPrefabName(requirement);
                    if (!string.IsNullOrEmpty(themeName))
                    {
                        theme = themeName;
                        break;
                    }
                }
            }

            _prefabThemes[prefabEntity] = theme;
            return theme;
        }

        /// <summary>
        /// Split a flags enum's rendered name into the contract's array of names.
        /// Names rather than a bit field, for the same reason <c>lanes.jsonl</c> does it: the
        /// numeric values are CS2 internals that can change meaning between game versions, where a
        /// name that disappears is a name a consumer simply stops seeing. An enum with no bits set
        /// renders as <c>"0"</c>, which is not a name and is dropped.
        /// </summary>
        /// <param name="flags">The enum's <c>ToString</c>.</param>
        /// <returns>The names, possibly empty.</returns>
        /// <summary>
        /// A resolved <c>ColorSet</c> as the contract writes colours: three linear RGBA channels.
        /// </summary>
        /// <param name="set">The set.</param>
        /// <returns>Three RGBA arrays.</returns>
        internal static double[][] ColorSetJson(Game.Rendering.ColorSet set) => new[]
        {
            ChannelJson(set.m_Channel0), ChannelJson(set.m_Channel1), ChannelJson(set.m_Channel2)
        };

        /// <inheritdoc cref="ColorSetJson"/>
        private static double[] ChannelJson(UnityEngine.Color color) => new double[]
        {
            Math.Round(color.r, 4), Math.Round(color.g, 4), Math.Round(color.b, 4), Math.Round(color.a, 4)
        };

        private static string[] FlagNames(string flags)
        {
            if (string.IsNullOrEmpty(flags) || char.IsDigit(flags[0])) return Array.Empty<string>();

            List<string> names = new();
            foreach (string name in flags.Split(','))
            {
                string trimmed = name.Trim();
                if (trimmed.Length > 0) names.Add(trimmed);
            }

            return names.ToArray();
        }
    }
}
