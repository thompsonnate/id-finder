"""
mock_data.py
------------
Simulates external API responses for local development and testing.
Swap these out by setting USE_MOCKS=False once you have real API keys.

The mock data is seeded from real MusicBrainz / Last.fm / YouTube
response shapes so the parsing code works identically in prod.
"""

from app.fingerprint import SongFeatures
from app.candidates import CandidateSong

USE_MOCKS = False  # Set to False in production with real API keys


# ── Realistic seed fingerprints by known song ────────────────────────────────

MOCK_SEEDS: dict[str, SongFeatures] = {
    # ── Electronic seeds ──────────────────────────────────────────────────────
    "strings of life::rhythim is rhythim": SongFeatures(
        title="Strings of Life", artist="Rhythim Is Rhythim",
        mbid="ee1b0a47-1234-4abc-89dc-aabbccddeeff",
        bpm=127.0, key="A", mode="minor",
        energy=0.85, danceability=0.78, valence=0.60,
        acousticness=0.02, instrumentalness=0.95,
        genre_tags=["deep house", "house", "chicago house"],
        mood_tags=["euphoric", "uplifting"],
        has_audio_features=True, source="mock_acousticbrainz",
    ),
    "surgeon::surgeon": SongFeatures(
        title="Surgeon", artist="Surgeon",
        mbid="ff2c1b58-5678-4def-90ed-bbccddeeaabb",
        bpm=140.0, key="F", mode="minor",
        energy=0.92, danceability=0.65, valence=0.20,
        acousticness=0.01, instrumentalness=0.98,
        genre_tags=["techno", "hard techno", "industrial techno"],
        mood_tags=["dark", "intense"],
        has_audio_features=True, source="mock_acousticbrainz",
    ),
    "windowlicker::aphex twin": SongFeatures(
        title="Windowlicker", artist="Aphex Twin",
        mbid="aa3d2c69-9abc-4fed-01fe-ccddeeffaabb",
        bpm=102.0, key="D", mode="minor",
        energy=0.70, danceability=0.55, valence=0.30,
        acousticness=0.05, instrumentalness=0.90,
        genre_tags=["idm", "electronica", "experimental electronic"],
        mood_tags=["weird", "intense"],
        has_audio_features=True, source="mock_acousticbrainz",
    ),
    "inner city life::goldie": SongFeatures(
        title="Inner City Life", artist="Goldie",
        mbid="bb4e3d7a-abcd-4123-bcde-ddeeffaabbcc",
        bpm=170.0, key="G", mode="minor",
        energy=0.80, danceability=0.70, valence=0.35,
        acousticness=0.10, instrumentalness=0.60,
        genre_tags=["drum and bass", "jungle", "liquid dnb"],
        mood_tags=["melancholy", "dark", "atmospheric"],
        has_audio_features=True, source="mock_acousticbrainz",
    ),
    "spastik::plastikman": SongFeatures(
        title="Spastik", artist="Plastikman",
        mbid="cc5f4e8b-bcde-4234-cdef-eeffaabbccdd",
        bpm=134.0, key="C", mode="minor",
        energy=0.75, danceability=0.72, valence=0.15,
        acousticness=0.01, instrumentalness=0.99,
        genre_tags=["minimal techno", "techno", "dub techno"],
        mood_tags=["hypnotic", "dark"],
        has_audio_features=True, source="mock_acousticbrainz",
    ),
    # ── Rock seeds ────────────────────────────────────────────────────────────
    "paranoid android::radiohead": SongFeatures(
        title="Paranoid Android",
        artist="Radiohead",
        mbid="c1a58a47-0f73-4e5a-89dc-ccbf5df0cb1d",
        bpm=82.5,
        key="C#",
        mode="minor",
        energy=0.73,
        danceability=0.31,
        valence=0.15,
        acousticness=0.12,
        instrumentalness=0.04,
        genre_tags=["alternative rock", "art rock", "progressive rock", "indie"],
        mood_tags=["dark", "intense", "melancholy"],
        has_audio_features=True,
        source="mock_acousticbrainz",
    ),
    "bohemian rhapsody::queen": SongFeatures(
        title="Bohemian Rhapsody",
        artist="Queen",
        mbid="b1a57e47-0f73-4e5a-89dc-ccbf5df0cb2e",
        bpm=72.0,
        key="Bb",
        mode="major",
        energy=0.62,
        danceability=0.38,
        valence=0.45,
        acousticness=0.35,
        instrumentalness=0.02,
        genre_tags=["classic rock", "arena rock", "progressive rock", "glam rock"],
        mood_tags=["epic", "dramatic"],
        has_audio_features=True,
        source="mock_acousticbrainz",
    ),
}


def get_mock_seed(title: str, artist: str) -> SongFeatures:
    """Return a mock seed or generate a plausible one."""
    key = f"{title.lower()}::{artist.lower()}"

    if key in MOCK_SEEDS:
        return MOCK_SEEDS[key]

    # Generate a plausible seed for any unknown song
    import hashlib
    h = int(hashlib.md5(key.encode()).hexdigest(), 16)

    return SongFeatures(
        title=title,
        artist=artist,
        mbid=f"mock-{h % 99999:05d}-mbid",
        bpm=float(80 + (h % 80)),          # 80–160 BPM range
        key=["C", "D", "E", "F", "G", "A", "B"][h % 7],
        mode="minor" if h % 2 == 0 else "major",
        energy=round((h % 100) / 100, 2),
        danceability=round(((h >> 4) % 100) / 100, 2),
        valence=round(((h >> 8) % 100) / 100, 2),
        acousticness=round(((h >> 12) % 100) / 100, 2),
        instrumentalness=round(((h >> 16) % 30) / 100, 2),
        genre_tags=_pick_genres(h),
        mood_tags=_pick_moods(h),
        has_audio_features=True,
        source="mock_generated",
    )


def get_mock_candidates(seed: SongFeatures) -> list[CandidateSong]:
    """
    Return a realistic candidate pool of 40 songs mixing:
    - Last.fm-style similar tracks (with match scores)
    - YouTube-style underground tracks (with view counts)
    - MusicBrainz tag matches
    """
    candidates = []

    # Last.fm similar tracks (mainstream + semi-known)
    lastfm_similars = _get_lastfm_candidates(seed)
    candidates.extend(lastfm_similars)

    # YouTube underground candidates
    yt_candidates = _get_youtube_candidates(seed)
    candidates.extend(yt_candidates)

    # MusicBrainz tag matches
    mb_candidates = _get_mb_candidates(seed)
    candidates.extend(mb_candidates)

    return candidates


def _get_lastfm_candidates(seed: SongFeatures) -> list[CandidateSong]:
    """Simulates Last.fm track.getSimilar response."""
    genre = seed.genre_tags[0] if seed.genre_tags else "indie"

    templates = {
        # Electronic subgenre pools — curated for realistic cross-recommendation
        "deep house": [
            ("Nude Photo", "Marshall Jefferson", 0.91),
            ("Can You Feel It", "Larry Heard", 0.89),
            ("Your Love", "Frankie Knuckles", 0.86),
            ("Mystery of Love", "Larry Heard", 0.83),
            ("Move Your Body", "Marshall Jefferson", 0.78),
            ("Washing Machine", "Mood II Swing", 0.74),
        ],
        "house": [
            ("Move Your Body", "Marshall Jefferson", 0.88),
            ("Jack Your Body", "Steve 'Silk' Hurley", 0.85),
            ("Your Love", "Frankie Knuckles", 0.82),
            ("Promised Land", "Joe Smooth", 0.78),
            ("Baby Wants to Ride", "Frankie Knuckles", 0.74),
        ],
        "chicago house": [
            ("Your Love", "Frankie Knuckles", 0.93),
            ("Move Your Body", "Marshall Jefferson", 0.90),
            ("Jack Your Body", "Steve 'Silk' Hurley", 0.86),
            ("Promised Land", "Joe Smooth", 0.80),
            ("I'll Always Love You", "Ten City", 0.75),
        ],
        "tech-house": [
            ("Lose Control", "Floorplan", 0.90),
            ("Terminus", "Rebekah", 0.87),
            ("Wiggle It", "DJ Sneak", 0.83),
            ("Acid Rain", "Skream", 0.79),
            ("Feel It", "DJ Pierre", 0.75),
        ],
        "microhouse": [
            ("Donna", "Luomo", 0.92),
            ("To Heal", "Isolée", 0.89),
            ("Wir sind wir", "Pole", 0.85),
            ("Piknik Elektronik", "Akufen", 0.81),
            ("Vom", "Luomo", 0.78),
        ],
        "minimal techno": [
            ("Spastik", "Plastikman", 0.93),
            ("Clink", "Richie Hawtin", 0.90),
            ("Ping", "Plastikman", 0.87),
            ("BC-One", "Richie Hawtin", 0.83),
            ("The Bells", "Jeff Mills", 0.79),
            ("Cycle 30", "Robert Hood", 0.75),
        ],
        "techno": [
            ("The Bells", "Jeff Mills", 0.91),
            ("Beyond Sequence", "Jeff Mills", 0.88),
            ("Cycle 30", "Robert Hood", 0.85),
            ("Minimal Nation", "Robert Hood", 0.81),
            ("Acid Rain", "Underground Resistance", 0.76),
            ("Jaguar", "Model 500", 0.72),
        ],
        "dub techno": [
            ("Borderland", "Basic Channel", 0.93),
            ("Phylyps Trak", "Basic Channel", 0.90),
            ("Quadrant Dub", "Basic Channel", 0.87),
            ("Mental Lapse", "Monolake", 0.82),
            ("Silence", "Maurizio", 0.78),
        ],
        "drum and bass": [
            ("Inner City Life", "Goldie", 0.92),
            ("Pulp Fiction", "Ed Rush & Optical", 0.88),
            ("Circles", "Adam F", 0.85),
            ("New Emotion", "Origin Unknown", 0.81),
            ("Valley of the Shadows", "Origin Unknown", 0.78),
            ("Brown Paper Bag", "Roni Size", 0.74),
        ],
        "liquid dnb": [
            ("Inner City Life", "Goldie", 0.90),
            ("Brown Paper Bag", "Roni Size", 0.87),
            ("Music Box", "Goldie", 0.84),
            ("Circles", "Adam F", 0.80),
            ("Feel the Truth", "LTJ Bukem", 0.76),
        ],
        "jungle": [
            ("Terminator", "Goldie", 0.92),
            ("Original Nuttah", "UK Apache & Shy FX", 0.89),
            ("Incredible", "M-Beat", 0.85),
            ("Darkrider", "Renegade", 0.80),
        ],
        "breakbeat": [
            ("Block Rockin' Beats", "The Chemical Brothers", 0.91),
            ("Setting Sun", "The Chemical Brothers", 0.87),
            ("Push the Button", "Fatboy Slim", 0.83),
            ("Rockafeller Skank", "Fatboy Slim", 0.79),
            ("Loops of Fury", "The Chemical Brothers", 0.75),
        ],
        "tribal house": [
            ("Tribal Confusion", "Louie Vega", 0.91),
            ("Give Me Your Love", "Osunlade", 0.87),
            ("El Ritmo de la Noche", "Osunlade", 0.83),
            ("Tribute to the Gods", "Louie Vega", 0.78),
        ],
        "idm": [
            ("Windowlicker", "Aphex Twin", 0.92),
            ("Alberto Balsam", "Aphex Twin", 0.89),
            ("Roygbiv", "Boards of Canada", 0.85),
            ("Turquoise Hexagon Sun", "Boards of Canada", 0.82),
            ("Rae", "Autechre", 0.78),
            ("Gantz Graf", "Autechre", 0.74),
        ],
        "alternative rock": [
            ("Exit Music (For a Film)", "Radiohead", 0.92),
            ("Karma Police", "Radiohead", 0.89),
            ("Creep", "Radiohead", 0.85),
            ("Black Hole Sun", "Soundgarden", 0.76),
            ("Smells Like Teen Spirit", "Nirvana", 0.71),
            ("Everlong", "Foo Fighters", 0.68),
            ("1979", "Smashing Pumpkins", 0.65),
        ],
        "indie": [
            ("Two Weeks", "Grizzly Bear", 0.88),
            ("Dog Days Are Over", "Florence + The Machine", 0.82),
            ("Little Talks", "Of Monsters and Men", 0.79),
            ("Electric Feel", "MGMT", 0.75),
            ("Rebellion (Lies)", "Arcade Fire", 0.70),
        ],
        "classic rock": [
            ("Stairway to Heaven", "Led Zeppelin", 0.88),
            ("Hotel California", "Eagles", 0.84),
            ("Dream On", "Aerosmith", 0.79),
            ("More Than a Feeling", "Boston", 0.74),
        ],
        "progressive rock": [
            ("Roundabout", "Yes", 0.87),
            ("In the Court of the Crimson King", "King Crimson", 0.84),
            ("Comfortably Numb", "Pink Floyd", 0.91),
            ("Supper's Ready", "Genesis", 0.78),
            ("Close to the Edge", "Yes", 0.73),
        ],
    }

    matches = []
    for g in seed.genre_tags[:3]:
        matches.extend(templates.get(g, templates["indie"]))

    if not matches:
        matches = templates["indie"]

    seen = set()
    unique = []
    for title, artist, score in matches:
        k = f"{title}::{artist}"
        if k not in seen:
            seen.add(k)
            unique.append(CandidateSong(
                title=title,
                artist=artist,
                source="lastfm",
                source_url=f"https://www.last.fm/music/{artist.replace(' ', '+')}/_/{title.replace(' ', '+')}",
                lastfm_match=score,
                genre_tags=seed.genre_tags[:2],
            ))

    return unique


def _get_youtube_candidates(seed: SongFeatures) -> list[CandidateSong]:
    """Simulates yt-dlp search results — mix of underground and known tracks."""
    genre = seed.genre_tags[0] if seed.genre_tags else "indie"

    _electronic_keywords = {
        "techno", "house", "drum and bass", "dnb", "jungle", "breakbeat",
        "ambient", "trance", "idm", "dubstep", "electro", "microhouse",
        "minimal techno", "dub techno", "deep house", "tech-house", "tribal house",
        "uk garage", "grime", "psytrance", "electronica",
    }
    is_electronic = any(g.lower() in _electronic_keywords for g in seed.genre_tags)

    electronic_underground = [
        # Rare rips, dubplates, and vinyl-only finds
        ("Borderland (Maurizio remix)", "Basic Channel", "aB1cD2eF3gH", 3_100, "wax_archaeologist", 0.94),
        ("Spastik (unreleased mix)", "Plastikman", "bC2dE3fG4hI", 4_800, "detroitvinylarchive", 0.92),
        ("The Bells (Robert Hood version)", "Jeff Mills", "cD3eF4gH5iJ", 6_200, "UR_vault", 0.89),
        ("Phylyps Trak II (dub)", "Basic Channel", "dE4fG5hI6jK", 2_700, "dub_techno_rips", 0.91),
        ("Nude Photo (Larry Heard original)", "Larry Heard", "eF5gH6iJ7kL", 8_900, "deepchicagoarchive", 0.88),
        ("Jungle Ting (dubplate)", "Renegade", "fG6hI7jK8lM", 3_300, "junglism93", 0.90),
        ("Circles (VIP)", "Adam F", "gH7iJ8kL9mN", 11_200, "dnb_classics_vinyl", 0.85),
        ("Valley of the Shadows (repress)", "Origin Unknown", "hI8jK9lM0nO", 5_500, "origin_unknown_fan", 0.87),
        ("Donna (Luomo unreleased)", "Luomo", "iJ9kL0mN1oP", 1_900, "microhouse_vault", 0.93),
        ("Roygbiv (Boards of Canada radio session)", "Boards of Canada", "jK0lM1nO2pQ", 7_400, "idm_sessions", 0.86),
        # Less underground but YouTube-native electronic
        ("Alberto Balsam", "Aphex Twin", "kL1mN2oP3qR", 980_000, "AphexTwinOfficial", 0.52),
        ("Inner City Life", "Goldie", "lM2nO3pQ4rS", 2_400_000, "GoldieOfficial", 0.48),
    ]

    underground_pool = [
        # Rock / general underground finds
        ("The Sad Punk", "Pixies", "dQw4w9WgXcX", 12_400, "Pixies Official", 0.82),
        ("Mezzanine (Demo)", "Massive Attack", "eYq7WapuDLU", 8_200, "massiveattackvevo", 0.35),
        ("Exit (demo tape)", "Radiohead", "fJ9rUzIMcZQ", 4_100, "rarehoarder", 0.91),
        ("Lift (rare B-side)", "Radiohead", "kXYiU_JCYtU", 6_700, "rarehoarder", 0.92),
        ("Trees (acoustic)", "Portishead", "kXYiU_JCYtU", 3_300, "vinyl_rips_hq", 0.89),
        ("Rearranged (live BBC)", "Limp Bizkit", "oHg5SJYRHA0", 22_000, "BBC Archive", 0.45),
        ("Subterranean Homesick Alien (Alt Mix)", "Radiohead", "xvFZjo5PgG0", 5_500, "oknotokforum", 0.88),
        ("The National Anthem (Peel Session)", "Radiohead", "iik25wqIuFo", 9_100, "peel_sessions", 0.87),
        ("Spitting Venom (live)", "Modest Mouse", "5diU9PKT3mk", 18_000, "modest_archive", 0.72),
        ("Nikes (early version)", "Frank Ocean", "iik25wqIuFo", 41_000, "frankoceanarchive", 0.69),
        # Less underground but still YouTube-native
        ("Anyone Can Play Guitar", "Radiohead", "dQw4w9WgXcX", 1_200_000, "Radiohead", 0.55),
        ("Street Spirit (Fade Out)", "Radiohead", "eYq7WapuDLU", 890_000, "Radiohead", 0.57),
    ]

    return [
        CandidateSong(
            title=title,
            artist=artist,
            source="youtube",
            source_url=f"https://youtube.com/watch?v={yt_id}",
            youtube_id=yt_id,
            view_count=views,
            underground_score=_yt_underground_score(views, channel),
            genre_tags=seed.genre_tags[:2],
            raw_metadata={"channel": channel, "tags": seed.genre_tags},
        )
        for title, artist, yt_id, views, channel, _ in (
            electronic_underground if is_electronic else underground_pool
        )
    ]


def _get_mb_candidates(seed: SongFeatures) -> list[CandidateSong]:
    """Simulates MusicBrainz tag-search results."""
    return [
        CandidateSong(
            title="Fake Plastic Trees",
            artist="Radiohead",
            source="musicbrainz",
            mbid="abc-123-fake",
            genre_tags=["alternative rock", "art rock"],
        ),
        CandidateSong(
            title="Idioteque",
            artist="Radiohead",
            source="musicbrainz",
            mbid="abc-456-fake",
            genre_tags=["art rock", "electronic"],
        ),
        CandidateSong(
            title="Pyramid Song",
            artist="Radiohead",
            source="musicbrainz",
            mbid="abc-789-fake",
            genre_tags=["art rock", "progressive rock"],
        ),
        CandidateSong(
            title="Motion Picture Soundtrack",
            artist="Radiohead",
            source="musicbrainz",
            mbid="abc-012-fake",
            genre_tags=["alternative rock"],
        ),
    ]


def _yt_underground_score(views: int, channel: str) -> float:
    score = 0.5
    if views < 10_000:
        score += 0.3
    elif views < 100_000:
        score += 0.2
    elif views < 500_000:
        score += 0.1
    elif views > 10_000_000:
        score -= 0.3

    official = ["vevo", "official", "records", "universal", "sony", "warner", "music"]
    if any(s in channel.lower() for s in official):
        score -= 0.2

    return max(0.0, min(1.0, score))


def _pick_genres(h: int) -> list[str]:
    all_genres = [
        "alternative rock", "indie", "electronic", "hip hop", "jazz",
        "classical", "folk", "metal", "pop", "r&b", "soul", "funk",
        "progressive rock", "post-rock", "ambient", "experimental",
    ]
    idx1 = h % len(all_genres)
    idx2 = (h >> 4) % len(all_genres)
    return list({all_genres[idx1], all_genres[idx2]})


def _pick_moods(h: int) -> list[str]:
    all_moods = ["dark", "upbeat", "melancholy", "energetic", "calm", "intense", "happy", "sad"]
    idx = h % len(all_moods)
    return [all_moods[idx]]
