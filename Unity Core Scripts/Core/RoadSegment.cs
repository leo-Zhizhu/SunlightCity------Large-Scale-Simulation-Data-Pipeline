using UnityEngine;

/// <summary>
/// Marker component used to tag a road mesh as an addressable segment.
///
/// Intentionally empty: it carries no data and exists only so
/// <see cref="PathShadowSegmentChecker"/> can identify which segment a sampled point falls on
/// via GetComponent. Nothing in the production PostGIS pipeline uses it — that pipeline keys
/// off `meo_edges` UUIDs instead — so attaching it is only needed for the legacy
/// per-segment shadow-pattern tooling.
/// </summary>
public class RoadSegment : MonoBehaviour
{
}
