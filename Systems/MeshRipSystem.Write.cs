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
    /// The writer task: what a queued mesh becomes on disk, and the §4.1 foundation skirt beside
    /// it.
    /// </summary>
    public partial class MeshRipSystem
    {
        /// <summary>
        /// Serialise queued prefabs to disk. Runs on a background task for the whole rip.
        /// </summary>
        /// <param name="run">The run in progress.</param>
        private static void WriteQueued(RipRun run)
        {
            MaterialRipSystem materials = run.Materials;
            // Landing a readback is the main thread's job; the writer only ever waits for pixels
            // that are on their way (`TextureWriter.Write`).

            foreach (RipItem item in run.Queue.GetConsumingEnumerable())
            {
                // Textures first, always: they arrive before the prefab that named them, and
                // encoding one is what gives it the content hash the prefab's .glb writes down.
                //
                if (item.Texture != null)
                {
                    materials.WriteTexture(item.Texture);
                    continue;
                }

                if (item.Skirt != null)
                {
                    WriteSkirt(run, item.Skirt);
                    continue;
                }

                PrefabMesh prefab = item.Prefab;
                if (prefab == null) continue;

                try
                {
                    string path = IOUtils.CombinePath(run.OutputDirectory, $"{prefab.AssetId}.glb");
                    long size = GltfWriter.Write(path, prefab, materials.Describe);

                    System.Threading.Interlocked.Increment(ref run.Written);
                    System.Threading.Interlocked.Add(ref run.Bytes, size);
                    run.Sizes.Add(new KeyValuePair<string, long>(prefab.AssetId, size));
                }
                catch (Exception ex)
                {
                    System.Threading.Interlocked.Increment(ref run.WriteFailed);
                    run.WriterNotes.Enqueue($"  ! {prefab.AssetId}: {ex.GetType().Name} {ex.Message}");
                    _log.Warn($"Failed to write `{prefab.AssetId}.glb`: {ex}");
                }
            }

            // The encoders run beside this loop; the run is not written until they are.
            materials.DrainTextures();
        }

        /// <summary>
        /// Generate one prefab's foundation-skirt band and write it as
        /// <c>&lt;assetId&gt;__base.glb</c>. Runs on the writer task.
        /// </summary>
        /// <param name="run">The run in progress.</param>
        /// <param name="skirt">The gathered inputs.</param>
        private static void WriteSkirt(RipRun run, SkirtWork skirt)
        {
            try
            {
                PrefabMesh prefab = new()
                {
                    AssetId = skirt.AssetId,
                    SourceName = skirt.SourceName,
                    InstanceCount = skirt.InstanceCount
                };

                foreach (SkirtPart part in skirt.Parts)
                {
                    MeshAsset band = BaseSkirtBuilder.Build(part.Source, part.BaseOffset,
                                                            skirt.AssetId, skirt.SourceName, part.MaterialId);
                    if (band == null) continue;

                    prefab.Parts.Add(new PrefabMeshPart
                    {
                        Mesh = band,
                        Position = part.Position,
                        Rotation = part.Rotation,
                        NodeName = part.NodeName
                    });
                }

                // No silhouette at the base level is a valid outcome — the game generates an empty
                // batch there too — so it is counted, not warned about.
                //
                if (prefab.Parts.Count == 0)
                {
                    System.Threading.Interlocked.Increment(ref run.SkirtsEmpty);
                    return;
                }

                string path = IOUtils.CombinePath(run.OutputDirectory, $"{prefab.AssetId}.glb");
                long size = GltfWriter.Write(path, prefab, run.Materials.Describe);

                System.Threading.Interlocked.Increment(ref run.SkirtsWritten);
                System.Threading.Interlocked.Add(ref run.Bytes, size);
                run.Sizes.Add(new KeyValuePair<string, long>(prefab.AssetId, size));
            }
            catch (Exception ex)
            {
                System.Threading.Interlocked.Increment(ref run.WriteFailed);
                run.WriterNotes.Enqueue($"  ! {skirt.AssetId}: {ex.GetType().Name} {ex.Message}");
                _log.Warn($"Failed to write `{skirt.AssetId}.glb`: {ex}");
            }
        }
    }
}
