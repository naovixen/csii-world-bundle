using System.Collections.Generic;
using Unity.Mathematics;

namespace Carto.Domain
{
    /// <summary>
    /// The five texture slots the bundle contract defines, in the order they appear in
    /// <c>materials.json</c>.
    /// The contract's material object is <b>closed</b> — a map outside this set cannot be written
    /// at all, which is deliberate: a consumer must never silently drop a map it should have
    /// handled. CS2 surfaces carry plenty of other textures (wind flutter, emissive ids, interior
    /// atlases); they are read but not exported in schema 1.
    /// </summary>
    public enum MaterialMap
    {
        /// <summary>Albedo, sRGB, alpha in A.</summary>
        BaseColor = 0,

        /// <summary>Tangent-space normal, linear, OpenGL convention.</summary>
        Normal = 1,

        /// <summary>CS2 MADS map: R metallic, G AO, B detail, A smoothness.</summary>
        Mask = 2,

        /// <summary>Emission, sRGB.</summary>
        Emissive = 3,

        /// <summary>
        /// CS2 <c>_ControlMask</c>: R, G and B select between the three channels of a
        /// <c>ColorVariation</c>, A is snow coverage.
        /// <b>Without this the colour palette is decoration.</b> <c>colors.json</c> carries each
        /// render prefab's three colour channels and each instance carries which variation it was
        /// painted, but neither says <i>where</i> on the mesh each channel applies — this texture
        /// does, per texel. Exporting the palette without it leaves a consumer with three colours
        /// and no way to use them, which is the failure the palette was added to prevent.
        /// </summary>
        ColorMask = 4
    }

    /// <summary>
    /// A material with its texture keys already resolved to paths — what a glTF writer needs, as
    /// opposed to what <c>materials.json</c> needs.
    /// Only the three maps that map cleanly onto glTF's own PBR model appear here.
    /// <see cref="MaterialMap.Mask"/> is deliberately absent: CS2 packs metallic in R, ambient
    /// occlusion in G and <i>smoothness</i> in A, while glTF's metallic-roughness texture wants
    /// metallic in B and <i>roughness</i> in G. Wiring one to the other would not be an
    /// approximation, it would be wrong — and silently so, since the result still renders. The
    /// mask stays in <c>materials.json</c>, which the contract makes authoritative for exactly
    /// this reason.
    /// </summary>
    public struct MaterialDescription
    {
        /// <summary>Whether a material was found at all.</summary>
        public bool Found { get; set; }

        /// <summary>Bundle-root-relative texture paths, or <c>null</c>.</summary>
        public string BaseColor { get; set; }

        /// <inheritdoc cref="BaseColor"/>
        public string Normal { get; set; }

        /// <inheritdoc cref="BaseColor"/>
        public string Emissive { get; set; }

        /// <summary>Multiplied into the base colour.</summary>
        public float4 Tint { get; set; }

        /// <summary>Multiplied into the emission.</summary>
        public float3 EmissiveFactor { get; set; }

        /// <summary><c>opaque</c>, <c>mask</c> or <c>blend</c>.</summary>
        public string AlphaMode { get; set; }

        /// <summary>The cutoff <c>mask</c> tests against.</summary>
        public float AlphaCutoff { get; set; }

        /// <summary>Whether back faces are drawn.</summary>
        public bool DoubleSided { get; set; }
    }

    /// <summary>
    /// One CS2 <c>SurfaceAsset</c> reduced to the bundle contract's material model, ready to become
    /// one entry in <c>materials.json</c>.
    /// Texture slots hold a <i>key</i> rather than a path while the rip is running. A texture's
    /// bundle path is its content hash, which is only known once the PNG has been encoded — and
    /// encoding happens on a background task, long after the material record is built. The keys are
    /// resolved to paths in one pass when the table is written.
    /// </summary>
    public class MaterialAsset
    {
        /// <summary>
        /// The bundle material id — the key in <c>materials.json</c>, and the join key a glTF
        /// primitive carries in its material <c>name</c>.
        /// </summary>
        public string MaterialId { get; set; }

        /// <summary>
        /// The surface asset's original name, kept for logging.
        /// </summary>
        public string SourceName { get; set; }

        /// <summary>
        /// The texture key per <see cref="MaterialMap"/> slot, or <c>null</c> where the surface has
        /// no such map.
        /// </summary>
        public string[] MapKeys { get; } = new string[5];

        /// <summary>
        /// Multiplied into <see cref="MaterialMap.BaseColor"/>. Linear RGBA in 0..1. <c>null</c>
        /// means white.
        /// </summary>
        public float4? Tint { get; set; }

        /// <summary>
        /// Multiplied into <see cref="MaterialMap.Emissive"/>. Linear RGB, may exceed 1.
        /// </summary>
        public float3? EmissiveFactor { get; set; }

        /// <summary>
        /// <c>opaque</c>, <c>mask</c> or <c>blend</c>.
        /// </summary>
        public string AlphaMode { get; set; }

        /// <summary>
        /// The cutoff <see cref="AlphaMode"/> <c>mask</c> tests against.
        /// </summary>
        public float? AlphaCutoff { get; set; }

        /// <summary>
        /// Whether back faces are drawn.
        /// </summary>
        public bool DoubleSided { get; set; }

        /// <summary>
        /// Producer hints a consumer may act on or ignore.
        /// </summary>
        public List<string> Flags { get; } = new();

        /// <summary>
        /// Serialise to the contract's material object, omitting everything unset.
        /// Omission is meaningful here: the contract says a consumer substitutes its own default
        /// for an absent map, so writing an explicit default would be a claim the producer cannot
        /// make.
        /// </summary>
        /// <param name="resolve">Maps a texture key to its bundle-relative path, or <c>null</c> when the texture never reached disk.</param>
        /// <returns>The material object.</returns>
        public Dictionary<string, object> ToJson(System.Func<string, string> resolve)
        {
            Dictionary<string, object> result = new();

            AddMap(result, "baseColor", MaterialMap.BaseColor, resolve);
            AddMap(result, "normal", MaterialMap.Normal, resolve);
            AddMap(result, "mask", MaterialMap.Mask, resolve);
            AddMap(result, "emissive", MaterialMap.Emissive, resolve);
            AddMap(result, "colorMask", MaterialMap.ColorMask, resolve);

            if (Tint.HasValue)
            {
                float4 tint = math.saturate(Tint.Value);
                if (!tint.Equals(new float4(1f, 1f, 1f, 1f)))
                    result["tint"] = new[] { tint.x, tint.y, tint.z, tint.w };
            }

            if (EmissiveFactor.HasValue)
            {
                float3 emissive = math.max(EmissiveFactor.Value, float3.zero);
                if (math.any(emissive != float3.zero))
                    result["emissiveFactor"] = new[] { emissive.x, emissive.y, emissive.z };
            }

            if (!string.IsNullOrEmpty(AlphaMode) && (AlphaMode != "opaque")) result["alphaMode"] = AlphaMode;
            if (AlphaCutoff.HasValue && (AlphaMode == "mask")) result["alphaCutoff"] = math.saturate(AlphaCutoff.Value);
            if (DoubleSided) result["doubleSided"] = true;
            if (Flags.Count > 0) result["flags"] = Flags.ToArray();

            return result;
        }

        /// <summary>
        /// Resolve this material for a glTF writer.
        /// </summary>
        /// <param name="resolve">Maps a texture key to its bundle-relative path.</param>
        /// <returns>The resolved description.</returns>
        public MaterialDescription Describe(System.Func<string, string> resolve)
        {
            return new MaterialDescription
            {
                Found = true,
                BaseColor = Path(MaterialMap.BaseColor, resolve),
                Normal = Path(MaterialMap.Normal, resolve),
                Emissive = Path(MaterialMap.Emissive, resolve),
                Tint = Tint.HasValue ? math.saturate(Tint.Value) : new float4(1f, 1f, 1f, 1f),
                EmissiveFactor = EmissiveFactor.HasValue ? math.max(EmissiveFactor.Value, float3.zero) : float3.zero,
                AlphaMode = string.IsNullOrEmpty(AlphaMode) ? "opaque" : AlphaMode,
                AlphaCutoff = AlphaCutoff.HasValue ? math.saturate(AlphaCutoff.Value) : 0.5f,
                DoubleSided = DoubleSided
            };
        }

        /// <summary>
        /// Resolve one texture slot to a path.
        /// </summary>
        private string Path(MaterialMap slot, System.Func<string, string> resolve)
        {
            string key = MapKeys[(int)slot];
            return string.IsNullOrEmpty(key) ? null : resolve?.Invoke(key);
        }

        /// <summary>
        /// Write one texture slot, if it resolved to a file.
        /// </summary>
        private void AddMap(Dictionary<string, object> target, string name, MaterialMap slot, System.Func<string, string> resolve)
        {
            string key = MapKeys[(int)slot];
            if (string.IsNullOrEmpty(key)) return;

            string path = resolve?.Invoke(key);
            if (!string.IsNullOrEmpty(path)) target[name] = path;
        }
    }
}
