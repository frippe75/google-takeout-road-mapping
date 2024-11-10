import json
import os
import argparse
from datetime import datetime
from geopy.distance import geodesic
import geojson
import requests

OSRM_API_URL = "http://router.project-osrm.org/route/v1/driving/"

# Country name normalization dictionary
country_map = {
    "sweden": ["sverige", "sweden"],
    "usa": ["united states", "usa", "us"],
    "spain": ["españa", "spain", "espanya"],
    "france": ["france"]
}

def is_within_radius(lat, lon, center, radius_km):
    """Check if the coordinate is within the geofenced radius."""
    return geodesic((lat, lon), center).km <= radius_km

def parse_date(date_str):
    """Parse date string into a datetime object, handling fractional seconds if present."""
    try:
        return datetime.strptime(date_str, '%Y-%m-%dT%H:%M:%S.%fZ')
    except ValueError:
        return datetime.strptime(date_str, '%Y-%m-%dT%H:%M:%SZ')

def normalize_country_name(name):
    """Normalize common country name variations to a single base name."""
    name = name.lower()
    for base_name, variants in country_map.items():
        if name in variants:
            return base_name
    return name

def filter_by_country(address, exclude_countries):
    """Checks if any excluded country is present in the address using country name normalization."""
    address = address.lower()
    for country in exclude_countries:
        normalized_country = normalize_country_name(country)
        if any(alias in address for alias in country_map.get(normalized_country, [country.lower()])):
            return False
    return True

def extract_activity_segment(activity_segment, geofence=None, activity_filter=None, from_date=None, to_date=None, exclude_countries=None):
    """Extracts data from an activity segment if it meets the criteria."""
    activity_type = activity_segment.get('activityType', 'UNKNOWN')

    # Filter by activity type
    if activity_filter and activity_type not in activity_filter:
        return None

    # Filter by date range
    start_time = parse_date(activity_segment['duration']['startTimestamp'])
    end_time = parse_date(activity_segment['duration']['endTimestamp'])
    if (from_date and start_time < from_date) or (to_date and end_time > to_date):
        return None

    # Check for country exclusion using address field
    transit_path = activity_segment.get('transitPath', {})
    if 'transitStops' in transit_path and exclude_countries:
        for stop in transit_path['transitStops']:
            address = stop.get('address', '')
            if address and not filter_by_country(address, exclude_countries):
                print(f"Excluding segment with address: {address}")
                return None

    # Extract start and end points
    start_lat = activity_segment['startLocation']['latitudeE7'] / 1e7
    start_lon = activity_segment['startLocation']['longitudeE7'] / 1e7
    end_lat = activity_segment['endLocation']['latitudeE7'] / 1e7
    end_lon = activity_segment['endLocation']['longitudeE7'] / 1e7

    # Extract waypoints
    waypoints = activity_segment.get('waypointPath', {}).get('waypoints', [])
    waypoint_list = [(wp['latE7'] / 1e7, wp['lngE7'] / 1e7) for wp in waypoints]

    # Check if all points are within geofence
    if geofence:
        points_to_check = [(start_lat, start_lon), (end_lat, end_lon)] + waypoint_list
        if not any(is_within_radius(lat, lon, geofence['center'], geofence['radius_km']) for lat, lon in points_to_check):
            return None

    return {
        "activity_type": activity_type,
        "start_lat": start_lat,
        "start_lon": start_lon,
        "end_lat": end_lat,
        "end_lon": end_lon,
        "waypoints": waypoint_list
    }

def snap_to_road(start_lat, start_lon, waypoints, end_lat, end_lon):
    """Snaps route to roads using OSRM."""
    waypoints_str = ';'.join([f"{lon},{lat}" for lat, lon in waypoints])
    url = f"{OSRM_API_URL}{start_lon},{start_lat};{waypoints_str};{end_lon},{end_lat}?overview=full&geometries=geojson"
    
    response = requests.get(url)
    if response.status_code == 200:
        return response.json()
    else:
        print(f"Failed to snap to road: {response.status_code}, {response.text}")
        return None

def process_takeout_data(folder_path, output_geojson, geofence=None, activity_filter=None, from_date=None, to_date=None, exclude_countries=None, stroke_width=2.0, stroke_color="#FF0000"):
    """Processes all files in the Semantic Location History folder and extracts filtered data into GeoJSON."""
    features = []
    total_files = sum(len(files) for _, _, files in os.walk(folder_path))
    file_count = 0
    
    for root, dirs, files in os.walk(folder_path):
        for file in files:
            if file.endswith('.json'):
                file_count += 1
                file_path = os.path.join(root, file)
                print(f"Processing file {file_count}/{total_files}: {file_path}")
                
                with open(file_path, 'r') as f:
                    data = json.load(f)
                    route_count = 0
                    for entry in data['timelineObjects']:
                        if 'activitySegment' in entry:
                            route_count += 1
                            segment_data = extract_activity_segment(
                                entry['activitySegment'], 
                                geofence=geofence, 
                                activity_filter=activity_filter, 
                                from_date=from_date, 
                                to_date=to_date, 
                                exclude_countries=exclude_countries
                            )
                            if segment_data:
                                print(f"  Processing route {route_count} in file {file}")
                                # Snap route to road using OSRM
                                snapped_route = snap_to_road(
                                    segment_data['start_lat'], segment_data['start_lon'],
                                    segment_data['waypoints'],
                                    segment_data['end_lat'], segment_data['end_lon']
                                )
                                
                                if snapped_route and 'routes' in snapped_route:
                                    snapped_coordinates = snapped_route['routes'][0]['geometry']['coordinates']
                                    feature = geojson.Feature(
                                        geometry=geojson.LineString(snapped_coordinates),
                                        properties={
                                            "activityType": segment_data["activity_type"],
                                            "stroke-width": stroke_width,
                                            "stroke-color": stroke_color
                                        }
                                    )
                                    features.append(feature)
                                    print(f"  Successfully processed route {route_count} in file {file}")
                                else:
                                    print(f"  Failed to snap route {route_count} in file {file}")

    feature_collection = geojson.FeatureCollection(features)
    with open(output_geojson, 'w') as geojson_file:
        geojson.dump(feature_collection, geojson_file)
        print(f"Filtered routes with road snapping saved to {output_geojson}")

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Filter Google Takeout data and extract roads ridden by car.")
    parser.add_argument('--folder-path', type=str, required=True, help="Path to the Semantic Location History folder")
    parser.add_argument('--output-geojson', type=str, required=True, help="Output GeoJSON file for map visualization")
    parser.add_argument('--activity-types', type=str, nargs='+', help="Filter by activity types (e.g., IN_PASSENGER_VEHICLE)")
    parser.add_argument('--from-date', type=str, help="Start date (YYYY-MM-DD)")
    parser.add_argument('--to-date', type=str, help="End date (YYYY-MM-DD)")
    parser.add_argument('--center-lat', type=float, help="Center latitude for geofencing")
    parser.add_argument('--center-lon', type=float, help="Center longitude for geofencing")
    parser.add_argument('--radius-km', type=float, help="Radius in kilometers for geofencing")
    parser.add_argument('--exclude-countries', type=str, nargs='+', help="Exclude routes in specific countries (e.g., Sweden, USA)")
    parser.add_argument('--stroke-width', type=float, default=2.0, help="Set the stroke width for GeoJSON visualization")
    parser.add_argument('--stroke-color', type=str, default="#FF0000", help="Set the stroke color for GeoJSON visualization")

    args = parser.parse_args()

    from_date = datetime.strptime(args.from_date, '%Y-%m-%d') if args.from_date else None
    to_date = datetime.strptime(args.to_date, '%Y-%m-%d') if args.to_date else None

    geofence = None
    if args.center_lat and args.center_lon and args.radius_km:
        geofence = {
            "center": (args.center_lat, args.center_lon),
            "radius_km": args.radius_km
        }

    exclude_countries = [normalize_country_name(country) for country in args.exclude_countries] if args.exclude_countries else None

    process_takeout_data(
        args.folder_path, 
        args.output_geojson, 
       

