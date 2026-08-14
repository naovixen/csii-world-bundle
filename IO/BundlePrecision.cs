using System;
using Unity.Mathematics;

namespace Carto.IO
{
    /// <summary>
    /// How many decimals the bundle publishes a coordinate to, in one place.
    /// <para>
    /// These are not formatting preferences, they are contract-visible: every position in the
    /// bundle is a number of metres rounded to <b>millimetres</b>, and every rotation is a unit
    /// quaternion rounded to <b>six</b> decimals, which is what keeps it unit within the 1e-3
    /// tolerance a validator checks. Full float precision would add several bytes per number
    /// across hundreds of thousands of lines to express a precision the game does not
    /// meaningfully have.
    /// </para>
    /// <para>
    /// Collected here because it had been written out four times — in
    /// <see cref="InstanceWriter"/>, <see cref="NetworkWriter"/>, <see cref="LaneWriter"/> and
    /// the area writer — with the later copies carrying comments that said "as
    /// <c>InstanceWriter</c> does" and "matching every other coordinate in the bundle". A
    /// constant whose copies have to describe each other wants to be one constant.
    /// </para>
    /// </summary>
    public static class BundlePrecision
    {
        /// <summary>Decimals a position keeps: metres to the millimetre.</summary>
        public const int PositionDecimals = 3;

        /// <summary>Decimals a quaternion component keeps.</summary>
        public const int RotationDecimals = 6;

        /// <summary>
        /// A world position — or a per-axis scale, which the bundle publishes to the same three
        /// decimals — as the bundle writes it.
        /// </summary>
        /// <param name="value">The triple.</param>
        /// <returns>The rounded triple.</returns>
        public static double[] Vector(float3 value) => new[]
        {
            Math.Round((double)value.x, PositionDecimals),
            Math.Round((double)value.y, PositionDecimals),
            Math.Round((double)value.z, PositionDecimals)
        };

        /// <summary>
        /// A rotation as the bundle publishes it — <c>[x, y, z, w]</c>, glTF's component order.
        /// </summary>
        /// <param name="value">The rotation.</param>
        /// <returns>The rounded quadruple.</returns>
        public static double[] Rotation(quaternion value) => new[]
        {
            Math.Round((double)value.value.x, RotationDecimals),
            Math.Round((double)value.value.y, RotationDecimals),
            Math.Round((double)value.value.z, RotationDecimals),
            Math.Round((double)value.value.w, RotationDecimals)
        };

        /// <summary>
        /// A scalar in metres as the bundle publishes it — a length, a width, an elevation.
        /// </summary>
        /// <param name="value">The value in metres.</param>
        /// <returns>The rounded value.</returns>
        public static double Metres(float value) => Math.Round((double)value, PositionDecimals);
    }
}
