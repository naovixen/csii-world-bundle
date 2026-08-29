using Carto.Domain;
using Carto.IO;
using Carto.Utils;
using Colossal.Logging;
using Game;
using Game.Net;
using System;
using System.Collections.Generic;
using System.Text;
using Unity.Collections;
using Unity.Entities;
using Unity.Mathematics;

namespace Carto.Systems
{
    /// <summary>
    /// What one lane record carries: its classification, its curve data, and its flags.
    /// </summary>
    public partial class LaneRipSystem
    {
        /// <summary>
        /// What may use this lane.
        /// </summary>
        /// <param name="entity">The lane.</param>
        /// <returns>The contract's lane kind.</returns>
        private string Classify(Entity entity)
        {
            if (EntityManager.HasComponent<CarLane>(entity)) return "car";
            if (EntityManager.HasComponent<TrackLane>(entity)) return "track";
            if (EntityManager.HasComponent<PedestrianLane>(entity)) return "pedestrian";
            if (EntityManager.HasComponent<ParkingLane>(entity)) return "parking";
            if (EntityManager.HasComponent<ConnectionLane>(entity)) return "connection";
            return "other";
        }

        /// <summary>
        /// Carry across whatever the lane's own component knows: speed, bendiness and flags.
        /// The speed limit is stored in metres per second and published in km/h, matching the
        /// <c>limit</c> that <c>networks.jsonl</c> already carries so the two agree.
        /// </summary>
        /// <param name="entity">The lane.</param>
        /// <param name="record">The record to fill.</param>
        private void AddLaneData(Entity entity, LaneRecord record)
        {
            if (EntityManager.HasComponent<CarLane>(entity))
            {
                CarLane lane = EntityManager.GetComponentData<CarLane>(entity);
                record.SpeedLimit = lane.m_SpeedLimit * 3.6f;
                record.Curviness = lane.m_Curviness;
                AddFlags(record, lane.m_Flags.ToString());
                return;
            }

            if (EntityManager.HasComponent<TrackLane>(entity))
            {
                TrackLane lane = EntityManager.GetComponentData<TrackLane>(entity);
                record.SpeedLimit = lane.m_SpeedLimit * 3.6f;
                record.Curviness = lane.m_Curviness;
                AddFlags(record, lane.m_Flags.ToString());
                return;
            }

            if (EntityManager.HasComponent<PedestrianLane>(entity))
            {
                AddFlags(record, EntityManager.GetComponentData<PedestrianLane>(entity).m_Flags.ToString());
                return;
            }

            if (EntityManager.HasComponent<ParkingLane>(entity))
            {
                AddFlags(record, EntityManager.GetComponentData<ParkingLane>(entity).m_Flags.ToString());
            }
        }

        /// <summary>
        /// Split a flags enum's rendered name into the contract's array of names.
        /// Names rather than a bit field: the numbers are CS2's internal values and would silently
        /// change meaning between game versions, where a name that disappears is a name a consumer
        /// simply stops seeing.
        /// </summary>
        /// <param name="record">The record to add to.</param>
        /// <param name="flags">The enum's <c>ToString</c>.</param>
        private static void AddFlags(LaneRecord record, string flags)
        {
            if (string.IsNullOrEmpty(flags) || (flags == "0")) return;

            foreach (string name in flags.Split(','))
            {
                string trimmed = name.Trim();
                if (trimmed.Length > 0) record.Flags.Add(trimmed);
            }
        }
    }
}
