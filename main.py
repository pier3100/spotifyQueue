## --- Imports ---
# standard library
import re
from pathlib import Path
from datetime import datetime as dt, timezone
from time import sleep
import os
import json
from dataclasses import dataclass
from typing import List

# third-party
from rapidfuzz import fuzz
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from mutagen import File as MutagenFile

## --- Constants ---
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

## --- Classes ---
@dataclass(frozen=True)
class SpotifyConfig:
    client_id: str
    client_secret: str
    redirect_uri: str
    scope: List[str]

@dataclass(frozen=True)
class Config:
    MUSIC_ROOT: Path
    DOWNLOADED_PLAYLIST_URL: str
    QUEUE_PLAYLIST_URL: str
    AUDIO_EXTENSIONS: set
    SPOTIFY_CFG: SpotifyConfig

## --- Functions ---
# Configuration    
def build_spotify_config(raw: dict) -> SpotifyConfig:
    return SpotifyConfig(
        client_id=raw["client_id"],
        client_secret=raw["client_secret"],
        redirect_uri=raw["redirect_uri"],
        scope=list(raw["scope"]),
    )

def build_config(cfg: dict) -> Config:
    return Config(
        MUSIC_ROOT=Path(cfg["paths"]["music_root"]),
        DOWNLOADED_PLAYLIST_URL=cfg["playlists"]["downloaded_url"],
        QUEUE_PLAYLIST_URL=cfg["playlists"]["queue_url"],
        AUDIO_EXTENSIONS=set(cfg["audio"]["extensions"]),
        SPOTIFY_CFG=build_spotify_config(cfg["spotify"]),
    )

def load_config(path="config.json") -> Config:
    try:
        with open(path, "r") as f:
            raw = json.load(f)
    except FileNotFoundError:
        raise RuntimeError(f"Config file not found: {path}")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Invalid JSON: {e}")

    return build_config(raw)

# Spotify
def spotify_login(spotify_cfg: SpotifyConfig):
    auth_manager = SpotifyOAuth(
        client_id=spotify_cfg.client_id,
        client_secret=spotify_cfg.client_secret,
        redirect_uri=spotify_cfg.redirect_uri,
        scope=" ".join(spotify_cfg.scope),
    )

    print(f"\nAuthenticating with Spotify...")
    token_info = auth_manager.get_cached_token()
    if not token_info:
        print("No cached token → logging in...")
        auth_manager.get_access_token()

    print("Authentication ready.")

    sp = spotipy.Spotify(auth_manager=auth_manager)
    print(f"Logged in as: {sp.current_user()['display_name']}")

    return sp

def get_playlist_from_url(sp, url: str):
    playlist_id = url.split("playlist/")[1].split("?")[0].split("/")[0]
    playlist = sp.playlist(playlist_id)

    return {
        "id": playlist["id"],
        "name": playlist["name"],
        "track_count": playlist["tracks"]["total"]
    }

def get_playlist_tracks(sp, id):
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

def move_track(sp, track_ids, queue_playlist_id, downloaded_playlist_id):
    """
    Function that:
    1. adds track to downloaded playlist
    2. removes track from queue playlist
    """
    if not track_ids:
        return {
            "moved": 0,
            "failed": 0,
            "results": []
        }

    results = []
    moved = 0
    failed = 0

    for track_id in track_ids:

        try:
            track_uri = f"spotify:track:{track_id}"
            track = sp.track(track_id)

            title = track["name"]
            artists = [a["name"] for a in track["artists"]]

            # --- ADD TO DOWNLOADED ---
            sp.playlist_add_items(
                downloaded_playlist_id,
                [track_uri]
            )

            # --- REMOVE FROM QUEUE ---
            sp.playlist_remove_all_occurrences_of_items(
                queue_playlist_id,
                [track_uri]
            )

            moved += 1

            results.append({
                "track_id": track_id,
                "title": title,
                "artists": artists,
                "status": "moved",
                "count": moved
            })

        except Exception as e:
            failed += 1

            results.append({
                "track_id": track_id,
                "status": "failed",
                "error": str(e),
                "count": moved
            })

    return {
        "moved": moved,
        "failed": failed,
        "results": results
    }

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

# Local file discovery
def get_recent_files(config: Config, cutoff):
    count_total = 0
    files = []

    for path in config.MUSIC_ROOT.rglob("*"):
        if not path.is_file():
            continue

        if path.suffix.lower() not in config.AUDIO_EXTENSIONS:
            continue

        file_time = dt.fromtimestamp(
            path.stat().st_birthtime,
            tz=timezone.utc
        )

        count_total += 1
        
        if file_time >= cutoff:
             files.append(path)

    return {
        "files": files or [],
        "count_total": count_total,
        "count_to_check": len(files)
    }

def extract_track_info(paths):
    """
    Function that converts file paths → (artist, title, duration)
    """
    if not paths:
        return []
    
    tracks = []

    for path in paths:
        audio = MutagenFile(path, easy=True)

        if audio is None:
            continue

        # --- title ---
        title = (audio.get("title", [None])[0] or path.stem)
        title_norm = normalize(title)

        # --- artist ---
        artists = audio.get("artist", ["Unknown"])
        artists_norm = " ".join(sorted(normalize(a) for a in artists))

        # --- duration ---
        duration_ms = None

        if audio and audio.info and hasattr(audio.info, "length"):
            length = audio.info.length

            if length and length > 0: ## MUTAGEN sometimes returns length=0 for unknown formats, so we check for that
                duration_ms = int(length * 1000)

        ## --- collect track info ---
        tracks.append({
            "artists": artists,
            "artists_norm": artists_norm,
            "title": title,
            "title_norm": title_norm,
            "duration_ms": duration_ms,
            "path": path
        })

    return tracks

# Matching
def normalize(s: str) -> str:
    s = s.lower()
    for pattern in JUNK_PATTERNS:
        s = re.sub(pattern, "", s)
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def match_to_spotify(local_tracks, spotify_tracks, threshold=88):
    """
    Returns spotify track IDs for matched local tracks
    """
    if not local_tracks or not spotify_tracks:
        return {
            "matches": [],
            "unmatched_count": len(local_tracks or []),
            "matched_count": 0
        }

    matches = []
    unmatched = 0

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
            matches.append({
                "spotify_id": best_id,
                "score": best_score,
                "local": local
            })
        else:
            unmatched += 1

    return {
        "matches": matches,
        "unmatched_count": unmatched,
        "matched_count": len(matches)
    }

## --- Main logic ---
def main():
    # Configuration
    os.chdir(os.path.dirname(__file__)) # ensure we run in the script's directory, so relative paths work correctly
    config = load_config()

    # Spotify login
    sp = spotify_login(config.SPOTIFY_CFG)

    # Fetch Spotify playlists and tracks
    queue_playlist = get_playlist_from_url(sp, config.QUEUE_PLAYLIST_URL)
    downloaded_playlist = get_playlist_from_url(sp, config.DOWNLOADED_PLAYLIST_URL)
    spotify_tracks_queue = get_playlist_tracks(sp,queue_playlist["id"])
    cutoff = get_playlist_first_entry_time(spotify_tracks_queue)

    ## User interaction
    # Display Spotify playlist info
    width_name = max(len(queue_playlist["name"]), len(downloaded_playlist["name"])) + 8
    width_count = max(len(str(queue_playlist["track_count"])), len(str(downloaded_playlist["track_count"]))) + 1
    print(
        f"\nQueue playlist:\t\t{queue_playlist['name']:{width_name}}  {queue_playlist['track_count']:{width_count}} tracks"
        f"\nDownloaded playlist:\t{downloaded_playlist['name']:{width_name}}  {downloaded_playlist['track_count']:{width_count}} tracks"
    )

    # Scan for files and extract track info
    print(f"\nFirst entry in queue playlist added at: {cutoff.isoformat()}, using this as cutoff date.")

    scan = get_recent_files(config, cutoff)
    print(f"\nFound\t\t{scan['count_total']} \t total audio files under {config.MUSIC_ROOT},")
    print(f"To check\t{scan['count_to_check']} \t against the Spotify queue playlist, as they have been created after the cutoff date.")

    tracks = extract_track_info(scan["files"])
    print(f"\nExtracted track info for local files, now matching against Spotify...")

    # Match local tracks to Spotify queue playlist
    match_result = match_to_spotify(tracks, spotify_tracks_queue)
    print(
        f"\nMatched\t\t{match_result['matched_count']} \t local files with a spotify track."
        f"\nUnmatched\t{match_result['unmatched_count']} \t local files.")

    # User confirmation
    if match_result["matched_count"] == 0:
        print("\nNo matches found, nothing to move. Exiting.")
        exit()
        
    print(
        f"\nDo you want me to move {match_result['matched_count']} tracks from queue playlist to downloaded playlist?"
    )

    answer = input(f"\nType y/n: ").strip().lower()
    if answer != "y":
        print("Aborted.")
        exit()

    # Modify Spotify playlists based on matches
    move_stats = move_track(sp, [m["spotify_id"] for m in match_result["matches"]], queue_playlist["id"], downloaded_playlist["id"])
    print(
        f"\nMoved\t\t{move_stats['moved']} \t tracks to downloaded playlist."
        f"\nFailed\t\t{move_stats['failed']} \t tracks to move."
    )

    # Display how many tracks are left in the queue playlist
    queue_playlist = get_playlist_from_url(sp, config.QUEUE_PLAYLIST_URL)
    print(
        f"\nDone."
        f"\nLeft in queue\t{queue_playlist['track_count']} \t tracks."
    )

## --- Run ---
if __name__ == "__main__":
    main()

## --- Legacy ---
""" 
# Verify results 
spotify_tracks_queue = get_playlist_tracks(queue_playlist["id"])

for track in spotify_tracks_queue:
    print("left in queue: ", track["id"], "-", track["title"], "by", ", ".join(track["artists"]))

QUEUE_PLAYLIST_URL = config["playlists"]["queue_url"]
DOWNLOADED_PLAYLIST_URL = config["playlists"]["downloaded_url"]
MUSIC_ROOT = config["paths"]["music_root"]
AUDIO_EXTENSIONS = config["audio"]["extensions"]
spotify_cfg = config["spotify"]

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
"""