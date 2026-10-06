"""Fictional demo catalog shared by the mock upstreams and the seeder.

Every title, person, and synopsis here is invented. External ids live in a
reserved range (9_000_000+) so they can never collide with a real TMDB/TVDB
record, and imdb ids use a "tt99…" prefix for the same reason.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

NOW = datetime.now(timezone.utc).replace(microsecond=0)

# (title, type, year, genres, rating, critic, official, runtime_min, director, cast, overview, palette)
_RAW = [
    ("Starfall Protocol", "Movie", 2024, ["Science Fiction", "Thriller"], 7.8, 88, "PG-13", 128,
     "Mara Ellison", ["Theo Vance", "Ines Calder", "Rowan Pike"],
     "When a decommissioned satellite starts transmitting again, a retired flight controller has one night to work out who is listening.",
     ("#0b1d3a", "#3f7cff", "#9fd3ff")),
    ("The Long Meridian", "Series", 2023, ["Drama", "Adventure"], 8.4, 93, "TV-14", 52,
     None, ["Lena Ortiz", "Calum Reyes", "Priya Natarajan"],
     "Three generations of a shipping family keep a failing ocean route alive, and the route keeps a secret of its own.",
     ("#06263a", "#1e8a9b", "#f2c76e")),
    ("Hollow Creek", "Movie", 2025, ["Horror", "Mystery"], 7.1, 81, "R", 104,
     "Silas Morrow", ["June Hart", "Elliot Crane"],
     "A county surveyor maps a valley that is slightly larger every time she measures it.",
     ("#120f0f", "#5e2a2a", "#d8a35c")),
    ("Paper Lanterns", "Movie", 2022, ["Animation", "Family"], 8.0, 95, "PG", 96,
     "Aiko Tanabe", ["Mina Sato", "Kenji Arata"],
     "A girl who repairs lanterns discovers each one remembers the last wish written on it.",
     ("#2b1240", "#d9556b", "#ffd28a")),
    ("Quiet Engines", "Movie", 2021, ["Drama"], 7.4, 86, "PG-13", 117,
     "Hollis Grant", ["Dana Whitlock", "Marcus Bell"],
     "A rail mechanic and the town she keeps running, in the last winter before the line closes.",
     ("#1b2329", "#6a7f8c", "#e7e1d3")),
    ("Glasshouse", "Series", 2024, ["Thriller", "Crime"], 8.1, 90, "TV-MA", 48,
     None, ["Nadia Fross", "Owen Laird"],
     "An architecture firm's flagship tower opens on schedule. Its first tenant is found on the roof garden.",
     ("#0f1a17", "#2f8f6f", "#cdf2e4")),
    ("The Cartographer's Daughter", "Movie", 2020, ["Adventure", "Fantasy"], 7.6, 84, "PG", 121,
     "Ines Varga", ["Clara Moss", "Tobias Fenn"],
     "Her father's last map shows an island no ship has found. She intends to be the first.",
     ("#20180c", "#b07a2c", "#f6e3b5")),
    ("Signal & Noise", "Series", 2022, ["Comedy", "Drama"], 7.9, 89, "TV-14", 30,
     None, ["Ravi Mehta", "Sofie Lund"],
     "The overnight crew of a tiny public radio station, and the strange calls that keep them on air.",
     ("#1a1033", "#7a5cff", "#ffb3e6")),
    ("Iron Orchard", "Movie", 2023, ["Action", "Science Fiction"], 6.9, 72, "PG-13", 112,
     "Victor Adair", ["Kai Dunmore", "Lyra Quinn"],
     "Autonomous harvesters on a terraforming colony stop taking orders the week the supply ship is due.",
     ("#141a10", "#7aa83a", "#e3f5b0")),
    ("Low Tide", "Movie", 2024, ["Drama", "Romance"], 7.3, 85, "PG-13", 101,
     "Ana Reyes", ["Isla Monroe", "Jonah Weller"],
     "Two strangers keep missing each other on the same stretch of coast, one tide at a time.",
     ("#0c2233", "#3a8ec2", "#f4d8b0")),
    ("Brightwater", "Series", 2025, ["Mystery", "Drama"], 8.2, 91, "TV-14", 55,
     None, ["Helen Marsh", "Felix Oduya"],
     "A lake town's annual regatta is cancelled for the first time in a century. Nobody will say why.",
     ("#0a1f2c", "#3fb0c9", "#f0f7f9")),
    ("Clockwork Sparrow", "Movie", 2019, ["Animation", "Adventure"], 7.7, 92, "G", 89,
     "Pell Hartley", ["Wren Abbot", "Moss Calder"],
     "A wind-up bird escapes a toymaker's window and learns what it costs to keep going.",
     ("#2a1a0c", "#e08a2e", "#ffe7b8")),
    ("Dead Reckoning Station", "Movie", 2022, ["Thriller", "Science Fiction"], 7.0, 79, "R", 109,
     "Greta Lowe", ["Sam Okafor", "Petra Nyl"],
     "A deep-ocean research crew loses contact with the surface and then, one by one, with each other.",
     ("#050f1a", "#1c4f7a", "#7fe0ff")),
    ("Saltgrass", "Series", 2021, ["Western", "Drama"], 8.0, 87, "TV-MA", 50,
     None, ["Cole Barrett", "Maribel Soto"],
     "Two ranching families share one aquifer and a long memory.",
     ("#2a1d12", "#c2873e", "#f3dcb5")),
    ("The Understudy", "Movie", 2023, ["Comedy"], 7.2, 83, "PG-13", 98,
     "Nora Blake", ["Felicity Shaw", "Arlo Benn"],
     "An understudy gets her big break the night the entire cast comes down with stage fright.",
     ("#2b0f1f", "#e04a7a", "#ffd0e0")),
    ("Northbound", "Movie", 2021, ["Documentary"], 8.3, 97, "PG", 92,
     "Tomas Elder", ["Narrated by Ruth Calloway"],
     "One year following a migrating herd across a thousand miles of tundra.",
     ("#0e1b22", "#5c8fa3", "#e8f1f4")),
    ("Ember Academy", "Series", 2024, ["Animation", "Fantasy", "Action"], 8.5, 94, "TV-PG", 24,
     None, ["Haru Kimura", "Sora Ito"],
     "First-years at a school for fire-keepers discover the eternal flame is going out.",
     ("#2a0c0c", "#ff6a3d", "#ffd27a")),
    ("Moonlit Courier", "Series", 2023, ["Animation", "Adventure"], 8.1, 90, "TV-PG", 24,
     None, ["Yuna Mori", "Daichi Sen"],
     "A night-shift delivery girl's route runs through a city that only exists after midnight.",
     ("#0d0f2b", "#5b6cff", "#ffe9a8")),
    ("Tin Pilots", "Movie", 2024, ["Animation", "Science Fiction"], 7.9, 89, "PG", 101,
     "Ren Akiyama", ["Kai Onoda", "Mio Hara"],
     "Two kids rebuild a scrapped mech and enter the junkyard league nobody is supposed to know about.",
     ("#10202a", "#38b6a8", "#f6f0c4")),
    ("Verdant", "Series", 2022, ["Animation", "Fantasy"], 8.0, 88, "TV-PG", 24,
     None, ["Aoi Tsuji", "Ren Hoshino"],
     "A botanist's apprentice tends a garden where every plant is a sleeping spirit.",
     ("#0c1f12", "#4fbf6a", "#e4ffd9")),
    ("Second Sunrise", "Movie", 2025, ["Science Fiction", "Drama"], 7.5, 86, "PG-13", 119,
     "Lena Kovac", ["Idris Vale", "Mae Sorensen"],
     "The first crew to wake on a generation ship finds the ship already awake.",
     ("#1a0f05", "#ff9a3c", "#fff1d6")),
    ("Back Roads", "Series", 2020, ["Comedy", "Drama"], 7.6, 84, "TV-14", 28,
     None, ["Gus Harlan", "Penny Ames"],
     "A small-town mail carrier knows everyone's business and has decided to start minding it.",
     ("#1f1a10", "#a99a5c", "#f5efd6")),
    ("The Archivist", "Movie", 2020, ["Mystery", "Thriller"], 7.4, 85, "PG-13", 113,
     "Celia Strand", ["Arthur Penn-Lowe", "Maeve Duff"],
     "A museum archivist finds her own handwriting in a ledger from 1887.",
     ("#16120c", "#7d6a4a", "#e9dfc8")),
    ("Halfway Light", "Movie", 2022, ["Romance", "Drama"], 7.0, 80, "PG-13", 105,
     "Dev Anand Rao", ["Sana Kapoor", "Luca Ferri"],
     "Two lighthouse keepers on opposite sides of a strait fall for each other by signal lamp.",
     ("#0f1626", "#e8b14a", "#fff4d4")),
]


def _slug(i: int) -> str:
    return f"demo{i:04d}"


ITEMS: list[dict] = []
for i, (title, typ, year, genres, rating, critic, official, runtime, director, cast,
        overview, palette) in enumerate(_RAW, start=1):
    anime = "Animation" in genres and (typ == "Series" or title in ("Tin Pilots", "Paper Lanterns"))
    ITEMS.append({
        "Id": _slug(i),
        "Name": title,
        "SortName": title.lower().removeprefix("the "),
        "Type": typ,
        "ProductionYear": year,
        "PremiereDate": f"{year}-0{(i % 9) + 1}-1{i % 9}T00:00:00Z",
        "Genres": genres,
        "CommunityRating": rating,
        "CriticRating": critic,
        "OfficialRating": official,
        "RunTimeTicks": runtime * 600_000_000,
        "Overview": overview,
        "People": ([{"Name": director, "Type": "Director"}] if director else [])
                  + [{"Name": c, "Type": "Actor"} for c in cast],
        "ProviderIds": {"Tmdb": str(9_000_000 + i), "Imdb": f"tt99{i:05d}",
                        **({"Tvdb": str(9_500_000 + i)} if typ == "Series" else {})},
        "ImageTags": {"Primary": f"p{i}"},
        "BackdropImageTags": [f"b{i}"],
        # newest first: item 1 added ~2 days ago, then spaced out
        "DateCreated": (NOW - timedelta(days=2 + i * 3, hours=i)).isoformat(),
        "Library": "Anime" if anime else ("Movies" if typ == "Movie" else "TV Shows"),
        "_palette": palette,
    })

BY_ID = {it["Id"]: it for it in ITEMS}
GENRES = sorted({g for it in ITEMS for g in it["Genres"]})

# Titles that are requested but NOT yet in the library — they drive the
# lifecycle journey and the reconciler's drift findings.
PIPELINE = [
    # (title, type, tmdb, tvdb, final_state, requested_by)
    ("Harbor of Glass", "movie", 9_100_001, None, "grabbed", "demo"),
    ("Wildfire Season", "series", 9_100_002, 9_600_002, "requested", "demo"),
    ("The Velvet Archive", "movie", 9_100_003, None, "downloaded", "demo"),
    ("Kite Runners of Ostra", "series", 9_100_004, 9_600_004, "grabbed", "maya"),
]

USERS = ["maya", "jordan", "sam", "riley", "alex", "demo"]
