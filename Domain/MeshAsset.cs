using System.Collections.Generic;
using Unity.Mathematics;

namespace Carto.Domain
{
    /// <summary>
    /// A single drawable range of a <see cref="MeshAsset"/>, corresponding to one
    /// <c>Game.Prefabs.MeshMaterial</c> entry and becoming one glTF primitive.
    /// （<see cref="MeshAsset"/> 中單一可繪製的範圍，對應一筆 <c>Game.Prefabs.MeshMaterial</c>，並輸出為一個 glTF primitive。）
    /// </summary>
    public class MeshPrimitive
    {
        /// <summary>
        /// The first index of this primitive within <see cref="MeshAsset.Indices"/>.
        /// （此 primitive 在 <see cref="MeshAsset.Indices"/> 中的起始索引。）
        /// </summary>
        public int StartIndex { get; set; }

        /// <summary>
        /// The number of indices in this primitive. Always a multiple of three.
        /// （此 primitive 的索引數量，必為三的倍數。）
        /// </summary>
        public int IndexCount { get; set; }

        /// <summary>
        /// The first vertex this primitive refers to, as reported by the game.
        /// （遊戲回報的此 primitive 起始頂點。）
        /// </summary>
        public int StartVertex { get; set; }

        /// <summary>
        /// The number of vertices this primitive refers to, as reported by the game.
        /// （遊戲回報的此 primitive 頂點數量。）
        /// </summary>
        public int VertexCount { get; set; }

        /// <summary>
        /// The game's material slot for this primitive.
        /// （此 primitive 在遊戲中的材質槽位。）
        /// </summary>
        public int MaterialIndex { get; set; }

        /// <summary>
        /// The bundle material id. This is written into the glTF material's <c>name</c>, which the
        /// bundle contract defines as the join key into <c>materials.json</c>.<br/>
        /// （束材質 ID。此值會寫入 glTF 材質的 <c>name</c>，依束契約作為連結 <c>materials.json</c> 的鍵。）
        /// </summary>
        public string MaterialId { get; set; }
    }

    /// <summary>
    /// One unique prefab mesh, read from the game's CPU-side ECS mesh buffers and ready to be
    /// written as a single <c>.glb</c>.<br/>
    /// （單一預製模板網格，讀取自遊戲 CPU 端的 ECS 網格緩衝區，可直接輸出為一個 <c>.glb</c>。）<br/><br/>
    /// All geometry here is still in <b>CS2 world space</b> — left-handed, Y-up, and with Unity's
    /// bottom-left UV origin. <see cref="Carto.IO.GltfWriter"/> performs the conversion to the
    /// bundle's mesh frame on write; nothing converts it earlier.<br/>
    /// （此處的幾何仍為 <b>CS2 世界座標</b>：左手座標系、Y 軸向上，且使用 Unity 左下角的 UV 原點。
    /// 轉換至束的網格座標系由 <see cref="Carto.IO.GltfWriter"/> 在寫出時進行。）
    /// </summary>
    public class MeshAsset
    {
        /// <summary>
        /// The sanitised prefab name. Becomes <c>meshes/&lt;AssetId&gt;.glb</c>.
        /// （淨化後的預製模板名稱，將成為 <c>meshes/&lt;AssetId&gt;.glb</c>。）
        /// </summary>
        public string AssetId { get; set; }

        /// <summary>
        /// The prefab's original, unsanitised name, kept for logging and diagnostics.
        /// （預製模板未經淨化的原始名稱，供記錄與診斷使用。）
        /// </summary>
        public string SourceName { get; set; }

        /// <summary>
        /// Vertex positions, in metres.（頂點位置，單位為公尺。）
        /// </summary>
        public float3[] Vertices { get; set; }

        /// <summary>
        /// Vertex normals. May be <c>null</c> when the source mesh carries none.
        /// （頂點法線。若來源網格沒有法線則為 <c>null</c>。）
        /// </summary>
        public float3[] Normals { get; set; }

        /// <summary>
        /// Texture coordinates. May be <c>null</c>.（貼圖座標，可能為 <c>null</c>。）
        /// </summary>
        public float2[] Uvs { get; set; }

        /// <summary>
        /// Tangents, <c>xyz</c> plus a handedness sign in <c>w</c>. May be <c>null</c>.
        /// （切線，<c>xyz</c> 加上位於 <c>w</c> 的手性符號，可能為 <c>null</c>。）
        /// </summary>
        public float4[] Tangents { get; set; }

        /// <summary>
        /// Triangle indices into <see cref="Vertices"/>.（指向 <see cref="Vertices"/> 的三角形索引。）
        /// </summary>
        public int[] Indices { get; set; }

        /// <summary>
        /// The drawable ranges making up this mesh.（構成此網格的可繪製範圍。）
        /// </summary>
        public List<MeshPrimitive> Primitives { get; set; } = new();

        /// <summary>
        /// The mesh's local-space bounding box, which is the authoritative source of an
        /// instance's true height under the bundle contract.<br/>
        /// （網格於區域座標下的邊界框；依束契約，這是實例真實高度的權威來源。）
        /// </summary>
        public float3 BoundsMin { get; set; }

        /// <inheritdoc cref="BoundsMin"/>
        public float3 BoundsMax { get; set; }

        /// <summary>
        /// The LOD level this asset represents. <c>0</c> is the full-detail mesh.
        /// （此資產代表的 LOD 層級，<c>0</c> 為最高細節網格。）
        /// </summary>
        public int Lod { get; set; }

        /// <summary>
        /// The number of triangles across every primitive.（所有 primitive 的三角形總數。）
        /// </summary>
        public int TriangleCount => (Indices?.Length ?? 0) / 3;
    }
}
