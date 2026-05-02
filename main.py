# standard library
import re
from pathlib import Path
from datetime import datetime as dt, timezone
from time import sleep
import os
import json

# third-party
from rapidfuzz import fuzz
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from mutagen import File as MutagenFile

## --- Setup ---
os.chdir(os.path.dirname(__file__)) # ensure we run in the script's directory, so relative paths work correctly

## --- Configuration ---
def load_config(path="config.json"):
    try:
        with open(path, "r") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        raise RuntimeError(f"Config file not found: {path}")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Invalid JSON: {e}")

    # --- Basic validation ---
    required_keys = ["playlists", "paths", "audio", "spotify"]
    for key in required_keys:
        if key not in cfg:
            raise RuntimeError(f"Missing top-level key: '{key}'")
        
    # --- Validate playlists config ---
    validate_spotify_playlist_url(cfg["playlists"].get("queue_url", ""))
    validate_spotify_playlist_url(cfg["playlists"].get("downloaded_url", ""))

    # --- Extract + convert ---
    try:
        cfg["paths"]["music_root"] = Path(cfg["paths"]["music_root"])
        cfg["audio"]["extensions"] = set(cfg["audio"]["extensions"])
    except KeyError as e:
        raise RuntimeError(f"Missing config key: {e}")

    return cfg

def validate_spotify_playlist_url(url: str):
    if not isinstance(url, str):
        raise ValueError("Playlist URL must be a string")

    if "open.spotify.com/playlist/" not in url:
        raise ValueError(f"Invalid Spotify playlist URL: {url}")
    
config = load_config()

QUEUE_PLAYLIST_URL = config["playlists"]["queue_url"]
DOWNLOADED_PLAYLIST_URL = config["playlists"]["downloaded_url"]
MUSIC_ROOT = config["paths"]["music_root"]
AUDIO_EXTENSIONS = config["audio"]["extensions"]
spotify_cfg = config["spotify"]

## --- Spotify API Setup ---
auth_manager = SpotifyOAuth(
    client_id=spotify_cfg["client_id"],
    client_secret=spotify_cfg["client_secret"],
    redirect_uri=spotify_cfg["redirect_uri"],
    scope=" ".join(spotify_cfg["scope"]),
)

print("f\nAuthenticating with Spotify...")
token_info = auth_manager.get_cached_token()
if not token_info:
    print("No cached token → logging in...")
    auth_manager.get_access_token()

print("Authentication ready.")

sp = spotipy.Spotify(auth_manager=auth_manager)
print(f"Logged in as: {sp.current_user()['display_name']}")

def get_playlist_from_url(sp, url: str):
    playlist_id = url.split("playlist/")[1].split("?")[0].split("/")[0]
    playlist = sp.playlist(playlist_id)

    return {
        "id": playlist["id"],
        "name": playlist["name"]
    }

def get_playlist_tracks(id):
    results = sp.playlist_items(
        id,
        additional_types=["track"]
    )

    tracks = []

    while results:
        for item in results["items"]:
            track = item["track"]

            if track is None:
                continue

            tracks.append({
                "id": track["id"],
                "title": track["name"],
                "title_norm": normalize(track["name"]),
                "artists": [a["name"] for a in track["artists"]],
                "artists_norm": " ".join(
                    sorted(normalize(a["name"]) for a in track["artists"])
                ),
                "duration_ms": track["duration_ms"],
                "added_at": parse_added_at(item["added_at"])
            })

        if results["next"]:
            results = sp.next(results)
        else:
            break

    return tracks

def parse_added_at(added_at: str):
    # Spotify format: 2025-12-01T14:22:11Z
    return dt.fromisoformat(added_at.replace("Z", "+00:00"))

def get_playlist_first_entry_time(tracks):
    return min(t["added_at"] for t in tracks if t["added_at"] is not None)

## --- Local file discovery ---
def get_recent_files():
    count_total = 0
    count_to_check = 0

    for path in MUSIC_ROOT.rglob("*"):
        if not path.is_file():
            continue

        if path.suffix.lower() not in AUDIO_EXTENSIONS:
            continue

        file_time = dt.fromtimestamp(
            path.stat().st_birthtime,
            tz=timezone.utc
        )

        count_total += 1
        
        if file_time >= CUTOFF:
            count_to_check += 1
            yield path

def extract_track_info(paths):
    """
    Generator that converts file paths → (artist, title, duration)
    """

    for path in paths:
        audio = MutagenFile(path, easy=True)

        if audio is None:
            continue

        # --- title ---
        title = (audio.get("title", [None])[0]
                 or path.stem)
        title_norm = normalize(title)

        # --- artist ---
        artists = audio.get("artist", ["Unknown"])
        artists_norm = " ".join(
            sorted(normalize(a) for a in artists)
        )

        # --- duration ---
        duration_ms = None

        if audio and audio.info and hasattr(audio.info, "length"):
            length = audio.info.length

            if length and length > 0: ## MUTAGEN sometimes returns length=0 for unknown formats, so we check for that
                duration_ms = int(length * 1000)

        yield {
            "artists": artists,
            "artists_norm": artists_norm,
            "title": title,
            "title_norm": title_norm,
            "duration_ms": duration_ms,
            "path": path
        }

## --- Matching ---
JUNK_PATTERNS = [
    r"\(.*?remaster.*?\)",
    r"\(.*?remastered.*?\)",
    r"\(.*?radio edit.*?\)",
    r"\(.*?explicit.*?\)",
    r"\(.*?version.*?\)",
    r"\(.*?edit.*?\)",
    r"\(.*?live.*?\)",
    r"\(.*?\)",
    r"\[.*?\]",
    r"feat\.?.*",
    r"ft\.?.*",
]

def normalize(s: str) -> str:
    s = s.lower()
    for pattern in JUNK_PATTERNS:
        s = re.sub(pattern, "", s)
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s
from rapidfuzz import fuzz

def match_to_spotify(local_tracks, spotify_tracks, threshold=88):
    """
    Yields spotify track IDs for matched local tracks
    """

    for local in local_tracks:

        best_score = 0
        best_id = None

        for sp in spotify_tracks:

            # --- TITLE MATCH ---
            title_score = fuzz.ratio(
                local["title_norm"],
                sp["title_norm"]
            )

            # --- ARTIST MATCH ---
            artist_score = fuzz.token_set_ratio(
                local["artists_norm"],
                sp["artists_norm"]
            )

            # --- WEIGHTED SCORE ---
            score = (0.7 * title_score) + (0.3 * artist_score)

            # --- DURATION CHECK (soft penalty) ---
            if local["duration_ms"] and sp["duration_ms"]:
                diff = abs(local["duration_ms"] - sp["duration_ms"])

                # > 3 sec difference → penalize
                if diff > 3000:
                    score -= 15

                # > 10 sec difference → strong penalty
                if diff > 10000:
                    score -= 30

            # --- BEST MATCH TRACKING ---
            if score > best_score:
                best_score = score
                best_id = sp["id"]

        # --- FINAL DECISION ---
        if best_score >= threshold:
            yield best_id

def score(local_track, spotify_track) -> float:
    """
    local_track: object with .artist and .title
    spotify_track: dict from Spotify API
    """

    # --- Extract fields ---
    local_artist = normalize(local_track.artist)
    local_title = normalize(local_track.title)

    spotify_artist = normalize(
        " ".join(a["name"] for a in spotify_track["artists"])
    )
    spotify_title = normalize(spotify_track["name"])

    # --- Compute scores ---
    artist_score = fuzz.token_set_ratio(local_artist, spotify_artist)
    title_score = fuzz.token_set_ratio(local_title, spotify_title)

    # --- Combine ---
    # Title matters slightly more than artist
    final_score = 0.6 * title_score + 0.4 * artist_score

    return final_score

## --- Moving tracks ---
def move_track(sp, track_ids, queue_playlist_id, downloaded_playlist_id):
    """
    Generator that:
    1. adds track to downloaded playlist
    2. removes track from queue playlist
    """
    count = 0;

    for track_id in track_ids:

        track_uri = f"spotify:track:{track_id}"
        track = sp.track(track_id)

        title = track["name"]
        artists = [a["name"] for a in track["artists"]]

        # --- 1. ADD TO DOWNLOADED PLAYLIST ---
        sp.playlist_add_items(
            downloaded_playlist_id,
            [track_uri]
        )

        # --- 2. REMOVE FROM SOURCE PLAYLIST ---
        sp.playlist_remove_all_occurrences_of_items(
            queue_playlist_id,
            [track_uri]
        )

        count += 1

        # --- yield confirmation ---
        yield {
            "track_id": track_id,
            "title": title,
            "artists": artists,
            "count": count
        }

## --- Testing ---
def fetch_spotify_track_info(sp, track_ids):
    """
    Generator:
    input: iterable of spotify track IDs
    output: (title, artists)
    """

    for track_id in track_ids:
        track = sp.track(track_id)

        title = track["name"]
        artists = [a["name"] for a in track["artists"]]

        yield {
            "track_id": track_id,
            "title": title,
            "artists": artists
        }

## --- Main logic ---
queue_playlist = get_playlist_from_url(sp, QUEUE_PLAYLIST_URL)
downloaded_playlist = get_playlist_from_url(sp, DOWNLOADED_PLAYLIST_URL)
spotify_tracks_queue = get_playlist_tracks(queue_playlist["id"])
CUTOFF = get_playlist_first_entry_time(spotify_tracks_queue)

print(
    f"\nDo you want me to move all tracks which I can find already in the search path,\n"
    f"from playlist '{queue_playlist['name']}' to playlist '{downloaded_playlist['name']}'?"
)

answer = input("Type y/n: ").strip().lower()

if answer != "y":
    print("Aborted.")
    exit()

print("First entry in queue playlist added at:", CUTOFF.isoformat())

last_count_moved = 0;
for output in move_track(sp, match_to_spotify(extract_track_info(get_recent_files()), spotify_tracks_queue), queue_playlist["id"], downloaded_playlist["id"]):
    print("moved: ",output["track_id"], "-", output["title"], "by", ", ".join(output["artists"]))
    last_count_moved = output["count"]

## --- Verify results ---
spotify_tracks_queue = get_playlist_tracks(queue_playlist["id"])

for track in spotify_tracks_queue:
    print("left in queue: ", track["id"], "-", track["title"], "by", ", ".join(track["artists"]))

print("f\nDONE \n Local files with created time after", CUTOFF.isoformat(), "have been checked against the Spotify queue playlist.\n")
print("f\n Total local audio files: \t","local audio files checked:","      moved:", last_count_moved, "           left in queue: ", len(spotify_tracks_queue))
