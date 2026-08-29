using Carto.Domain;
using Carto.IO;
using Carto.Utils;
using Colossal.IO.AssetDatabase;
using Colossal.Logging;
using Game;
using Game.Prefabs;
using Newtonsoft.Json;
using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Text;
using UnityEngine;
using UnityEngine.Experimental.Rendering;

namespace Carto.Systems
{
    /// <summary>
    /// What a live CS2 material actually declares, and which of its properties are readable.
    /// </summary>
    /// <remarks>
    /// A virtual-textured property has no plain texture behind it, and an unbound one has
    /// nothing at all. Both look the same from the outside, so both are tested for by name
    /// rather than assumed from a null.
    /// </remarks>
    public partial class MaterialRipSystem
    {
        /// <summary>
        /// Record what the material library says this surface's decode binds.
        /// The material template is the authority — it is what the decode itself consults to decide
        /// where each layer goes — so the stacks it declares name every texture the surface has,
        /// including any beyond the five the contract keeps. Accumulating them over a run turns
        /// "is there a map we are still missing?" from an offline question into a line in the
        /// report — which is what the colour mask needed, since that question had only ever been
        /// put to a regex.
        /// </summary>
        /// <param name="run">The run in progress.</param>
        /// <param name="surface">The surface asset.</param>
        private static void DeclaredProperties(MaterialRun run, SurfaceAsset surface)
        {
            MaterialStackProperties[] stacks;
            try
            {
                stacks = AssetDatabase.global.resources.materialLibrary
                    .GetMaterialDescription(surface.materialTemplateHash)?.m_Stacks;
            }
            catch (Exception)
            {
                // A surface whose template will not resolve says nothing about the others.
                //
                return;
            }

            if (stacks != null)
            {
                foreach (MaterialStackProperties stack in stacks)
                {
                    if (stack?.m_PropertyNames == null) continue;
                    foreach (string property in stack.m_PropertyNames)
                        if (!string.IsNullOrEmpty(property)) run.TemplateProperties.Add(property);
                }
            }

            // The stacks only name the virtual-textured maps. A non-VT surface binds whatever is
            // in its own texture dictionary (`SyncToUnityMaterial` walks `m_Textures` directly),
            // so those keys go into the same census — otherwise a map that exists only outside
            // the VT scheme could still ship unnoticed, which is the §4.2 failure shape again.
            //
            try
            {
                foreach (KeyValuePair<string, TextureAsset> texture in surface.textures)
                    if (!string.IsNullOrEmpty(texture.Key)) run.TemplateProperties.Add(texture.Key);
            }
            catch (Exception)
            {
                // A surface whose dictionaries did not load adds nothing.
                //
            }
        }

        /// <summary>
        /// Every shader property the decode could have bound a texture to, so that the sweep in
        /// <see cref="Release"/> destroys all of them and not merely the ones the contract keeps.
        /// If the template cannot be had, the contract's own maps are still better than leaking
        /// every one.
        /// </summary>
        /// <param name="surface">The surface asset.</param>
        /// <returns>The property names to sweep.</returns>
        private static IEnumerable<string> DecodedProperties(SurfaceAsset surface)
        {
            MaterialStackProperties[] stacks = null;
            try
            {
                stacks = AssetDatabase.global.resources.materialLibrary
                    .GetMaterialDescription(surface.materialTemplateHash)?.m_Stacks;
            }
            catch (Exception)
            {
                // Fall through to the contract's four.
            }

            if (stacks == null) return Maps;

            List<string> properties = new();
            foreach (MaterialStackProperties stack in stacks)
            {
                if (stack?.m_PropertyNames == null) continue;
                foreach (string property in stack.m_PropertyNames)
                    if (!string.IsNullOrEmpty(property)) properties.Add(property);
            }

            return (properties.Count > 0) ? properties : (IEnumerable<string>)Maps;
        }

        /// <summary>
        /// Whether a map is one the game reads out of a virtual-texturing stack rather than from a
        /// texture asset of its own.
        /// </summary>
        /// <param name="surface">The surface asset.</param>
        /// <param name="property">The CS2 shader property name.</param>
        /// <param name="texture">The reference the surface handed over.</param>
        /// <returns>Whether virtual texturing owns it.</returns>
        private static bool IsVirtualTextured(SurfaceAsset surface, string property, TextureAsset texture)
        {
            try
            {
                return surface.IsHandledByVirtualTexturing(
                    new KeyValuePair<string, TextureAsset>(property, texture));
            }
            catch (Exception)
            {
                // The material library could not say. Unbound-and-unknown is still unbound.
                //
                return false;
            }
        }

        /// <summary>
        /// Whether an asset reference has nothing behind it to read.
        /// The database is checked first and separately: <c>isDummy</c> is itself defined in terms
        /// of the database, so asking a reference that has none is not obviously safe.
        /// </summary>
        /// <param name="asset">The asset to test.</param>
        /// <returns>Whether loading it would fail.</returns>
        private static bool IsUnbound(AssetData asset)
        {
            if (asset == null) return true;

            try
            {
                if (asset.database == null) return true;
                return asset.isDummy;
            }
            catch (Exception)
            {
                return true;
            }
        }
    }
}
