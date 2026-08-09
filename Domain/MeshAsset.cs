using System.Collections.Generic;
using Unity.Mathematics;

namespace Carto.Domain
{
    /// <summary>
    /// A single drawable range of a <see cref="MeshAsset"/>, corresponding to one
    /// <c>Game.Prefabs.MeshMaterial</c> entry and becoming one glTF primitive.
    /// </summary>
    public class MeshPrimitive
    {
        /// <summary>
        /// The first index of this primitive within <see cref="MeshAsset.Indices"/>.
        /// </summary>
        public int StartIndex { get; set; }

        /// <summary>
        /// The number of indices in this primitive. Always a multiple of three.
        /// </summary>
        public int IndexCount { get; set; }

        /// <summary>
        /// The first vertex this primitive refers to, as reported by the game.
        /// </summary>
        public int StartVertex { get; set; }

        /// <summary>
        /// The number of vertices this primitive refers to, as reported by the game.
        /// </summary>
        public int VertexCount { get; set; }

        /// <summary>
        /// The game's material slot for this primitive.
        /// </summary>
        public int MaterialIndex { get; set; }

        /// <summary>
        /// The bundle material id. This is written into the glTF material's <c>name</c>, which the
        /// bundle contract defines as the join key into <c>materials.json</c>.
        /// </summary>
        public string MaterialId { get; set; }
    }

    /// <summary>
    /// One unique prefab mesh, read from the game's CPU-side ECS mesh buffers and ready to be
    /// written as a single <c>.glb</c>.
    /// All geometry here is still in <b>CS2 world space</b> — left-handed, Y-up, and with Unity's
    /// bottom-left UV origin. <see cref="Carto.IO.GltfWriter"/> performs the conversion to the
    /// bundle's mesh frame on write; nothing converts it earlier.
    /// </summary>
    public class MeshAsset
    {
        /// <summary>
        /// The sanitised prefab name. Becomes <c>meshes/&lt;AssetId&gt;.glb</c>.
        /// </summary>
        public string AssetId { get; set; }

        /// <summary>
        /// The prefab's original, unsanitised name, kept for logging and diagnostics.
        /// </summary>
        public string SourceName { get; set; }

        /// <summary>
        /// Vertex positions, in metres.
        /// </summary>
        public float3[] Vertices { get; set; }

        /// <summary>
        /// Vertex normals. May be <c>null</c> when the source mesh carries none.
        /// </summary>
        public float3[] Normals { get; set; }

        /// <summary>
        /// Texture coordinates. May be <c>null</c>.
        /// </summary>
        public float2[] Uvs { get; set; }

        /// <summary>
        /// Tangents, <c>xyz</c> plus a handedness sign in <c>w</c>. May be <c>null</c>.
        /// </summary>
        public float4[] Tangents { get; set; }

        /// <summary>
        /// Triangle indices into <see cref="Vertices"/>.
        /// </summary>
        public int[] Indices { get; set; }

        /// <summary>
        /// The drawable ranges making up this mesh.
        /// </summary>
        public List<MeshPrimitive> Primitives { get; set; } = new();

        /// <summary>
        /// The mesh's local-space bounding box, which is the authoritative source of an
        /// instance's true height under the bundle contract.
        /// </summary>
        public float3 BoundsMin { get; set; }

        /// <inheritdoc cref="BoundsMin"/>
        public float3 BoundsMax { get; set; }

        /// <summary>
        /// The LOD level this asset represents. <c>0</c> is the full-detail mesh.
        /// </summary>
        public int Lod { get; set; }

        /// <summary>
        /// The number of triangles across every primitive.
        /// </summary>
        public int TriangleCount => (Indices?.Length ?? 0) / 3;
    }
}
