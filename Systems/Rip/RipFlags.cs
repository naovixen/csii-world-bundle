using System;
using System.Collections.Concurrent;
using System.Collections.Generic;

namespace Carto.Systems
{
    /// <summary>
    /// A flags enum's value as the contract's array of names — computed once per distinct value.
    /// </summary>
    /// <remarks>
    /// <c>Enum.ToString()</c> on a <c>[Flags]</c> value reflects into the enum's name table and
    /// builds a joined string, and the callers then split it again: the lane rip did that for
    /// 680,772 lanes and the instance rip for every elevated and every tree record, on values that
    /// take a few dozen distinct forms between them. The names are the same — this is the very
    /// <c>ToString</c>/<c>Split</c>/<c>Trim</c> the callers did, remembered per value.
    /// </remarks>
    internal static class RipFlags
    {
        private static readonly ConcurrentDictionary<(Type, long), string[]> _names = new();

        /// <summary>
        /// The set names of <paramref name="flags"/>, or an empty array for a zero value.
        /// </summary>
        public static string[] Names<T>(T flags) where T : struct, Enum
        {
            (Type, long) key = (typeof(T), Convert.ToInt64(flags));
            if (_names.TryGetValue(key, out string[] names)) return names;

            string rendered = flags.ToString();
            if (string.IsNullOrEmpty(rendered) || char.IsDigit(rendered[0]) || (rendered[0] == '-'))
            {
                names = Array.Empty<string>();
            }
            else
            {
                List<string> list = new();
                foreach (string name in rendered.Split(','))
                {
                    string trimmed = name.Trim();
                    if (trimmed.Length > 0) list.Add(trimmed);
                }
                names = list.ToArray();
            }
            _names[key] = names;
            return names;
        }
    }
}
