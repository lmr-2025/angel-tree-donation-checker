"""Angel Tree bag check, powered by Jev (TypeSafe System One).

Run:  TYPESAFE_API_KEY=... python3 app.py      then open http://localhost:8000
      python3 app.py --dry-run                  builds the Jev request without sending it

A volunteer pastes a child's tag and adds photos of the bag. The goal is three strong
gifts per child; if the bag has fewer, the volunteer goes shopping. Jev counts the strong
gifts and answers one yes/no question per wish, and plain code below turns those
probabilities into a verdict and a shopping list.
"""

import argparse
import base64
import html
import io
import json
import os
import re
import urllib.error
import urllib.request
import uuid
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from PIL import Image, ImageOps

try:  # iPhone photos are HEIC; Pillow opens them once pillow-heif is installed
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass

API_URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
YES, NO = 0.7, 0.3  # TypeSafe's suggested starting thresholds for a Noul
MAX_SIDE, MIN_SIDE = 1568, 280  # Jev image limits, per side
GOAL = 3  # strong gifts each child should get
SMALL_LEGO = 150  # boxed LEGO sets under this many pieces count as half a strong gift
MAX_PACKING = 15  # packing-list items checked per bag

DEFAULT_RULES = """Gifts are new, in original packaging or with store tags.
Gifts are unwrapped."""

PHOTO_NOTES = ("The photos show one child's gift bag, laid out by a volunteer. Several photos may show the same items "
               "from different angles. Only the gift items count. Ignore background things such as furniture, bottles, "
               "curtains, bags and other household items, and boxes on chairs, on the floor or cut off at the edge "
               "of the photo, which are not part of the bag.")

STRONG = {
    "is": "A real, full-size gift that clearly matches one of the child's wishes, or is clearly built around their "
          "favorite, and suits their age. A wish can be covered by more than one strong gift, such as two LEGO sets.",
    "is_not": "Any gift that matches none of the child's wishes or favorites, however nice it is (a LEGO set "
              "is not strong for a child who did not ask for LEGO). Also small stocking stuffers, toys made for much "
              "younger children, clothing the child did not ask for, and anything in the background.",
    "leave_out": f"Small LEGO sets, with fewer than {SMALL_LEGO} pieces printed on the box (usually a palm-size "
                 "box), are counted separately, so leave them out of this count. LEGO polybags count as nothing.",
    "examples_strong": ["a 300-piece LEGO set for a child who wants LEGO sets",
                        "a Roblox gift card for a child who wants one",
                        "a LEGO Ninjago set for a child whose favorite is Ninjago"],
    "examples_not_strong": ["LEGO set for a child whose tag never mentions LEGO", "small LEGO set under 150 pieces (counted separately)",
                            "LEGO polybag with a few dozen pieces", "tricycle for a 12-year-old",
                            "baby shape sorter for a 12-year-old", "a card game for a child who only wished for a bike"],
}

# Toys that are clearly for a younger child, by the child's age band (see age_band). Babies and toddlers
# under 2 skip the check: nothing is too young for them.
TOO_YOUNG = {
    "little": {"true": ["baby rattle", "teether", "crib mobile", "infant activity gym"],
               "false": ["DUPLO set marked 18m+", "toddler ride-on", "kids' tablet marked 3+", "LEGO set marked 4+"]},
    "kid": {"true": ["baby rattle", "shape sorter", "DUPLO set", "toddler ride-on", "chunky wooden blocks"],
            "false": ["LEGO set marked 6+", "R/C car marked 8+", "card game marked 7+"]},
    "big": {"true": ["baby rattle", "shape sorter", "toddler ride-on", "preschool character playset",
                     "chunky wooden blocks"],
            "false": ["LEGO set marked 6+", "R/C car marked 8+", "card game marked 7+"]},
}


def too_young_criteria(band: str | None, who: str) -> dict:
    ex = TOO_YOUNG[band or "big"]
    return {"true": {"meaning": f"At least one gift item is made for children clearly younger than {who}",
                     "examples": ex["true"]},
            "false": {"meaning": f"Every gift suits {who} or is enjoyed at any age. An age mark like 4+ or 8+ is "
                                 "only a minimum age, so older children enjoy those toys too",
                      "examples": ex["false"]}}


RULE_CRITERIA = {
    "gifts are new, in original packaging or with store tags.": {
        "true": {"meaning": "Every item is in its store box or packaging, or has store tags attached",
                 "examples": ["sealed LEGO box", "R/C car in its retail box", "shirt with the price tag on"]},
        "false": {"meaning": "At least one item looks used, opened or has no packaging or tags",
                  "examples": ["loose toy with no box", "worn shoes", "opened box with parts missing"]}},
    "gifts are unwrapped.": {
        "true": {"meaning": "You can see the items' own boxes or packaging; nothing is covered in gift wrap",
                 "examples": ["toy boxes laid out on a table", "items in a clear store bag"]},
        "false": {"meaning": "At least one item is covered in wrapping paper or sealed in a gift box or gift bag",
                  "examples": ["box wrapped in holiday paper", "item hidden inside a closed gift bag"]}},
}

# What counts for common wish-list words. A donor can override any wish with "Wish: what counts".
# Each entry: words that trigger it, what counts and examples, what doesn't and examples. "replaces"
# drops looser entries the same wish also triggers, e.g. "Xbox gift card" is a gift card, not a game.
# "{who}" becomes the child ("a 5-year-old"), and "by_age" swaps in fields for an age band
# ("little" 2-5, "kid" 6-9, "big" 10+), so a 3-wheel scooter counts for a 4-year-old but not a 12-year-old.
GUIDE = [
    {"id": "tablet", "words": ("tablet",),
     "counts": "A tablet computer with a touchscreen",
     "yes": ["iPad", "Amazon Fire HD Kids tablet", "Samsung Galaxy Tab", "Android tablet"],
     "not": "Things that only look like or go with a tablet",
     "no": ["toy laptop", "tablet case on its own", "phone", "LeapFrog preschool learning toy"]},
    {"id": "tech", "words": ("tech", "electronic", "gadget"),
     "counts": "A toy or device that runs on batteries or charging and does something electronic",
     "yes": ["remote-control car", "drone", "robot toy", "coding kit", "kids' camera", "walkie-talkies",
             "headphones", "handheld game"],
     "not": "Toys with no electronics, even if they show machines or vehicles",
     "no": ["LEGO or other building set", "die-cast car", "plush toy", "book", "board game"]},
    {"id": "lego", "words": ("lego",),
     "counts": "A LEGO-brand set, with the red LEGO logo on the box",
     "yes": ["LEGO City", "LEGO Technic", "LEGO Ninjago", "LEGO DUPLO"],
     "not": "Building sets from other brands and toys that are not building sets",
     "no": ["Mega Bloks", "K'NEX", "Magna-Tiles", "R/C car", "action figure"]},
    {"id": "remote", "words": ("remote", "r/c", "rc"), "replaces": ("cars", "robot"),
     "counts": "A toy that is driven or controlled with a remote control that comes in the box",
     "yes": ["R/C car", "R/C monster truck", "drone", "R/C robot"],
     "not": "Vehicles or robots that have no remote control",
     "no": ["die-cast car", "push car", "LEGO car set", "wind-up robot"]},
    {"id": "cars", "words": ("hot wheels", "car"),
     "counts": "Toy cars the child can hold, race or drive",
     "yes": ["Hot Wheels or Matchbox car pack", "toy car race track", "R/C car"],
     "not": "Toys that are not cars, or boxes that only picture cars",
     "no": ["toy train", "puzzle with a car picture", "book about cars", "ride-on toy for toddlers"]},
    {"id": "robot", "words": ("robot",),
     "counts": "A toy robot or a kit for building a robot",
     "yes": ["R/C robot", "programmable robot", "robot-building kit", "transforming robot toy"],
     "not": "Toys that are not robots", "no": ["R/C car", "action figure", "LEGO animal set"]},
    {"id": "drone", "words": ("drone",), "replaces": ("remote",),
     "counts": "A small flying drone with a remote or app control",
     "yes": ["mini drone", "quadcopter", "drone with camera"],
     "not": "Things that do not fly", "no": ["R/C car", "toy plane with no remote", "paper airplane kit"]},
    {"id": "science", "words": ("science", "stem", "educational"),
     "counts": "A kit or toy that teaches a skill through doing: science, coding, building or math",
     "yes": ["volcano kit", "geode cracking kit", "microscope", "snap circuits", "coding robot", "chemistry set"],
     "not": "Toys without a learning activity, and toys for much younger children",
     "no": ["plush toy", "R/C car", "baby shape sorter"],
     "by_age": {"little": {"yes": ["bug catcher kit", "magnet discovery set", "kids' magnifying glass kit",
                                   "volcano kit"]}}},
    {"id": "book", "words": ("book",),
     "counts": "A book {who} would enjoy",
     "yes": ["chapter book", "graphic novel", "manga volume", "book box set"],
     "not": "Printed things that come with a toy, and books for a much younger child",
     "no": ["instruction booklet in a toy box", "board book for babies"],
     "by_age": {"little": {"yes": ["picture book", "lift-the-flap book", "sticker book", "character storybook"],
                           "no": ["instruction booklet in a toy box", "coloring page"]},
                "kid": {"yes": ["early reader", "chapter book", "graphic novel", "joke book"]}}},
    {"id": "puzzle", "words": ("puzzle",),
     "counts": "A puzzle to solve", "yes": ["500-piece jigsaw puzzle", "Rubik's cube", "brain-teaser set"],
     "not": "Building sets, and puzzles for a much younger child", "no": ["LEGO set", "wooden shape sorter"],
     "by_age": {"little": {"yes": ["24-piece jigsaw puzzle", "floor puzzle", "wooden peg puzzle"],
                           "no": ["LEGO set", "500-piece jigsaw puzzle"]}}},
    {"id": "game", "words": ("game",),
     "counts": "A board game, card game or party game {who} can play",
     "yes": ["Connect 4", "Sorry!", "Guess Who", "Pictionary", "Battleship"],
     "not": "Toys for free play, and games for a much younger child",
     "no": ["LEGO set", "R/C car", "plush toy"],
     "by_age": {"little": {"yes": ["Candy Land", "Hi Ho! Cherry-O", "memory matching game", "Hungry Hungry Hippos"]}}},
    {"id": "card", "words": ("card",),
     "counts": "Playing cards, a card game or trading cards",
     "yes": ["Go Fish deck", "Skip-Bo", "Pokemon cards", "deck of playing cards"],
     "not": "Other toys and games", "no": ["board game without cards", "LEGO set", "gift card"]},
    {"id": "gift_card", "words": ("gift card",), "replaces": ("card", "video_games"),
     "counts": "A gift card for the exact service or store named in the wish",
     "yes": ["Roblox gift card for a Roblox wish", "Xbox gift card for an Xbox wish"],
     "not": "Gift cards for something else, and things that are not gift cards",
     "no": ["Target or Visa gift card for a Roblox wish", "trading cards", "card game"]},
    {"id": "video_games", "words": ("playstation", "play station", "xbox", "nintendo", "switch", "joy-con", "video game"),
     "replaces": ("game", "card"),
     "counts": "A video game, controller or accessory for the console named in the wish",
     "yes": ["PlayStation 5 game", "Nintendo Switch Joy-Con pair", "Xbox controller"],
     "not": "Games for other consoles and non-video games", "no": ["board game", "Xbox game for a PlayStation wish"]},
    {"id": "game_room", "words": ("game room",), "replaces": ("game",),
     "counts": "Things that equip or decorate a game room for a preteen",
     "yes": ["gaming headset", "LED light strip", "door basketball hoop", "controller stand", "bean bag chair"],
     "not": "Ordinary toys", "no": ["LEGO set", "card game", "plush toy"]},
    {"id": "camera", "words": ("camera",),
     "counts": "An instant camera, its film, or accessories for one",
     "yes": ["Fujifilm Instax Mini camera", "Polaroid Go", "Instax film pack", "Instax album or camera case"],
     "not": "Other cameras and toys", "no": ["toy camera for preschoolers", "phone", "photo frame"]},
    {"id": "bike", "words": ("bike",),
     "counts": "A bicycle sized for {who}, with or without a helmet",
     "yes": ["bike with a helmet"],
     "not": "Bikes sized for a much younger or older child, and helmets or accessories on their own",
     "no": ["helmet only", "bike bell or lights"],
     "by_age": {
         "little": {"counts": "A balance bike or a bike with 10 to 14 inch wheels, with or without a helmet",
                    "yes": ["balance bike", "12-inch bike with training wheels", "14-inch bike with a toddler helmet"],
                    "no": ["24-inch bike", "helmet only", "bike bell or lights"]},
         "kid": {"counts": "A bike with 16 to 20 inch wheels, with or without a helmet",
                 "yes": ["16-inch bike", "20-inch BMX bike", "bike with a youth helmet"],
                 "no": ["balance bike", "12-inch toddler bike", "helmet only", "bike bell or lights"]},
         "big": {"counts": "A full-size bicycle (about 24 to 26 inch wheels), with or without a helmet",
                 "yes": ["24-inch mountain bike", "26-inch cruiser bike", "bike with a youth or adult helmet"],
                 "no": ["tricycle", "12-inch bike with training wheels", "helmet only", "bike bell or lights"]}}},
    {"id": "scooter", "words": ("scooter",),
     "counts": "A kick scooter sized for {who}, with or without a helmet",
     "yes": ["kick scooter", "scooter with a helmet"],
     "not": "Scooters sized for a much younger or older child, ride-ons, and helmets on their own",
     "no": ["helmet only", "foot-to-floor ride-on", "skateboard"],
     "by_age": {
         "little": {"counts": "A 3-wheel kick scooter for little kids, with or without a helmet",
                    "yes": ["3-wheel light-up scooter", "3-wheel kick scooter with a toddler helmet"],
                    "no": ["electric scooter", "helmet only", "foot-to-floor ride-on"]},
         "kid": {"yes": ["2-wheel kick scooter", "light-up kick scooter", "scooter with a youth helmet"]},
         "big": {"counts": "A kick scooter or electric scooter sized for {who}",
                 "yes": ["Razor A5 kick scooter", "electric scooter", "scooter with a helmet"],
                 "no": ["3-wheel toddler scooter", "foot-to-floor ride-on", "skateboard"]}}},
    {"id": "football", "words": ("football",),
     "counts": "An American football or football gear such as a youth helmet",
     "yes": ["youth-size football", "football and helmet set", "youth football helmet"],
     "not": "Other sports balls", "no": ["soccer ball", "basketball", "foam toddler ball"]},
    {"id": "soccer", "words": ("soccer",),
     "counts": "A soccer ball, soccer goal or net, or soccer gear",
     "yes": ["size 5 soccer ball", "pop-up soccer goal", "soccer ball and net set"],
     "not": "Other sports", "no": ["American football", "LEGO soccer display set"]},
    {"id": "barcelona", "words": ("barcelona",),
     "counts": "FC Barcelona clothing or gear",
     "yes": ["Barcelona jersey", "Barcelona hoodie", "Barcelona soccer ball"],
     "not": "Other teams and plain soccer gear", "no": ["Real Madrid jersey", "Real Madrid player merchandise"]},
    {"id": "guitar", "words": ("guitar",),
     "counts": "A real, playable guitar sized for a beginner",
     "yes": ["3/4-size acoustic guitar", "beginner electric guitar kit"],
     "not": "Toy guitars and other instruments", "no": ["toy guitar", "ukulele", "guitar picks only"]},
    {"id": "jewelry", "words": ("jewelry", "earring", "necklace", "ring"),
     "counts": "Jewelry for {who}", "yes": ["earrings", "necklace", "ring set", "charm bracelet"],
     "not": "Toy jewelry for a much younger child and things that are not jewelry",
     "no": ["dress-up jewelry for toddlers", "ring toss game", "jewelry box with nothing in it"]},
    {"id": "beauty", "words": ("make-up", "makeup", "perfume", "bath set", "body set"),
     "counts": "Makeup, perfume, body spray, lotion or bath gift sets for {who}",
     "yes": ["Bath & Body Works gift set", "body mist", "lip gloss set", "eyeshadow palette", "bath bomb set"],
     "not": "Pretend makeup for little kids and unrelated items", "no": ["pretend play makeup", "doll"]},
    {"id": "squishy", "words": ("slime", "nee-doh", "squishy", "squishies"),
     "counts": "Slime, Nee-Doh or squishy stress toys",
     "yes": ["Nee-Doh Nice Cube", "slime kit", "squishy foam toys", "squish ball pack"],
     "not": "Regular toys", "no": ["plush toy", "Play-Doh", "interactive pet toy"]},
    {"id": "craft", "words": ("craft", "art", "arts"),
     "counts": "A craft kit or art supplies {who} would use",
     "yes": ["diamond painting kit", "bead kit", "painting kit", "markers and sketchbook", "friendship bracelet kit"],
     "not": "Things without a making activity, and toddler crafts",
     "no": ["science kit, even one with paints (that is science, not art)", "single notebook", "LEGO set",
            "toddler coloring book"],
     "by_age": {"little": {"yes": ["crayons and coloring book", "washable paint set", "sticker craft kit",
                                   "dot marker set"],
                           "no": ["science kit, even one with paints (that is science, not art)", "LEGO set"]}}},
    {"id": "baking", "words": ("baking",),
     "counts": "A real baking set for kids", "yes": ["kids' baking kit with utensils", "cupcake baking set"],
     "not": "Pretend food", "no": ["pretend play food", "toy kitchen"]},
    {"id": "figures", "words": ("action figure",),
     "counts": "Action figures for {who}", "yes": ["Marvel Legends figure", "Pokemon figure", "figure multipack"],
     "not": "Plush, and figures for a much younger child", "no": ["plush toy", "chunky preschool figure"],
     "by_age": {"little": {"yes": ["superhero figure set", "dinosaur figure pack", "cartoon character figures"],
                           "no": ["plush toy", "collector figure marked 14+"]}}},
    {"id": "nerf", "words": ("nerf",),
     "counts": "Nerf blasters, darts or Nerf accessories", "yes": ["Nerf Elite blaster", "Nerf dart refill", "Nerf target set"],
     "not": "Other toys", "no": ["water gun", "toy sword"]},
    {"id": "watch", "words": ("watch",),
     "counts": "A wristwatch or kids' smartwatch", "yes": ["digital sports watch", "kids' smartwatch", "analog watch"],
     "not": "Toy watches and clocks", "no": ["preschool toy watch", "alarm clock"]},
    {"id": "hello_kitty", "words": ("hello kitty",),
     "counts": "Anything featuring Hello Kitty", "yes": ["Hello Kitty bedding", "Hello Kitty lamp", "Hello Kitty plush"],
     "not": "Other characters", "no": ["plain pink blanket", "My Melody-only item"]},
    {"id": "batman", "words": ("batman",),
     "counts": "Anything that features Batman",
     "yes": ["Batman action figure", "LEGO Batmobile", "Batman shirt", "Batman book"],
     "not": "Gifts without Batman, including other superheroes", "no": ["Spider-Man toy", "plain black car"]},
]


# ---------- images ----------

def prepare_image(raw: bytes) -> str:
    """Upright, within Jev's size limits, baseline JPEG, base64."""
    with Image.open(io.BytesIO(raw)) as original:
        img = ImageOps.exif_transpose(original).convert("RGB")
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    if min(img.size) < MIN_SIDE:
        scale = MIN_SIDE / min(img.size)
        img = img.resize((min(MAX_SIDE, round(img.width * scale)), min(MAX_SIDE, round(img.height * scale))))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=88)
    return base64.b64encode(out.getvalue()).decode("ascii")


# ---------- the child's tag ----------

SIZE_KEYS = {"shirt": "top", "top": "top", "pant": "pants", "jean": "pants", "coat": "coat", "jacket": "coat",
             "hoodie": "coat", "shoe": "shoes", "underwear": "underwear", "sock": "socks"}


def lines(text: str) -> list[str]:
    return [l.strip(" -•\t") for l in (text or "").splitlines() if l.strip(" -•\t")]


def read_age(text: str) -> tuple[str, int | None]:
    """'9 YEARS' -> ('9', 108), '20 months' -> ('20 months', 20)."""
    m = re.match(r"\s*(\d+)\s*(months?|mos?\b)?", text or "", re.I)
    if not m:
        return text.strip(), None
    n = int(m.group(1))
    return (f"{n} months", n) if m.group(2) else (str(n), n * 12)


def size_kind(key: str) -> str | None:
    return next((v for k, v in SIZE_KEYS.items() if k in key), None)


def parse_tag(text: str) -> dict:
    """Reads a pasted tag in either layout: "Name / MALE - 12 Years / Wish: ..." or "NAME: ALEX / AGE: 9 YEARS / WISH: ...".
    Ages can be in months ("MALE - 20 Months"). Size lines ("Shirt Size: 7", "Shoes: 13.5") and the code number are
    kept for the checklist; the name and code never leave this computer."""
    tag = {"name": "", "age": "", "age_months": None, "gender": "", "code": "", "wishes": [], "favorites": [],
           "concerns": [], "clothing": "", "sizes": {}}
    for line in lines(text):
        key, sep, value = line.partition(":") if ":" in line else line.partition("\t")
        key, value = key.strip().lower(), value.strip()
        if value.strip("-– ") == "":
            value = ""
        if m := re.fullmatch(r"(male|female|boy|girl)\s*-\s*(\d+\s*(?:years?|months?))", line.strip(), re.I):
            tag["gender"] = {"boy": "Male", "girl": "Female"}.get(m.group(1).lower(), m.group(1).title())
            tag["age"], tag["age_months"] = read_age(m.group(2))
        elif not sep:
            tag["name"] = tag["name"] or line
        elif not value:
            continue
        elif key == "name":
            tag["name"] = value.title()
        elif key == "gender":
            tag["gender"] = value.title()
        elif key == "age":
            tag["age"], tag["age_months"] = read_age(value)
        elif key.startswith(("code", "tag")):
            tag["code"] = value
        elif key.startswith(("wish", "need")):
            if value.lower() not in (w.lower() for w in tag["wishes"]):
                tag["wishes"].append(value)
        elif key.startswith("favorite"):
            tag["favorites"].append(value)
        elif key.startswith("special"):
            tag["concerns"].append(value)
        elif key.startswith("clothing"):
            tag["clothing"] = value
        elif kind := size_kind(key):
            tag["sizes"].setdefault(kind, value)
    return tag


def age_band(tag: dict | None) -> str | None:
    """'baby' under 2, 'little' 2-5, 'kid' 6-9, 'big' 10 and up; None when the tag has no age."""
    m = (tag or {}).get("age_months")
    if m is None:
        return None
    return "baby" if m < 24 else "little" if m < 72 else "kid" if m < 120 else "big"


def who(tag: dict | None) -> str:
    age = (tag or {}).get("age") or ""
    if not age:
        return "the child"
    return f"a {age.replace(' ', '-').rstrip('s')}-old" if "month" in age else f"a {age}-year-old"


def no_clothes(tag: dict) -> bool:
    return any(re.search(r"no\s+cloth", c, re.I) for c in tag["concerns"])


# ---------- the Jev request ----------

def wish_guidance(line: str, tag: dict | None = None) -> tuple[str, dict | None]:
    """Splits "Wish: what counts" and finds built-in definitions for words it recognises, sized to the child's age."""
    wish, _, own = line.partition(":")
    wish, own = wish.strip(), own.strip()
    if own:
        return wish, {"counts": own, "yes": [], "not": "Gifts that do not match that description", "no": []}
    hits = [g for g in GUIDE if any(re.search(rf"\b{re.escape(w)}s?\b", wish, re.I) for w in g["words"])]
    dropped = {r for h in hits for r in h.get("replaces", ())}
    hits = [h for h in hits if h["id"] not in dropped]
    if not hits:
        return wish, None
    band, child_is = age_band(tag), who(tag)
    hits = [{**h, **h.get("by_age", {}).get(band, {})} for h in hits]

    def fill(text):
        return text.replace("{who}", child_is)
    return wish, {"counts": "; or ".join(fill(h["counts"]) for h in hits),
                  "yes": [e for h in hits for e in h["yes"]],
                  "not": "; ".join(fill(h["not"]) for h in hits), "no": [e for h in hits for e in h["no"]]}


def fit_criteria(guide: dict | None) -> dict:
    """Matching yes/no descriptions so Jev can compare the two sides directly."""
    if not guide:
        return {"true": "A gift item in the photos fits the wish.",
                "false": "None of the gift items fit the wish, or a fit only appears in the background."}
    yes = {"meaning": f"At least one gift item is this: {guide['counts']}"}
    no = {"meaning": f"No gift item fits. Not counted: {guide['not']}, or things only in the background"}
    if guide["yes"]:
        yes["examples"] = guide["yes"]
    if guide["no"]:
        no["examples"] = guide["no"]
    return {"true": yes, "false": no}


def child(tag: dict) -> dict:
    """What Jev is told about the child. Never the name or the tag code."""
    out = {k: tag[k] for k in ("gender", "wishes", "favorites") if tag[k]}
    if tag["age"]:
        out["age"] = f"{tag['age']} old" if "month" in tag["age"] else f"{tag['age']} years old"
    return out


# Clothes for the full list: (id, label, what it is, size may be one up, examples).
CLOTHES = [
    ("top", "Top", "a shirt, top, sweatshirt or hoodie", False, ["graphic tee", "long-sleeve shirt", "hoodie"]),
    ("pants", "Pants", "pants, jeans, joggers or leggings", False, ["jeans", "joggers", "leggings"]),
    ("shoes", "Shoes", "a pair of shoes or sneakers", True, ["sneakers", "light-up shoes", "boots"]),
    ("coat", "Coat", "a coat or jacket", True, ["puffer coat", "fleece jacket", "rain jacket"]),
]


def clothes_question(item: tuple, size: str) -> tuple[dict, dict]:
    cid, _, what, up, examples = item
    sized = f" in size {size}" + (" or one size up" if up else "") if size else ""
    instructions = {"question": f"Is there {what} for `child` in `gift_photos`{sized}?"}
    if size:
        instructions["size"] = size
        instructions["how"] = "Read the size on the hang tag, label or shoe box."
    criteria = {"true": {"meaning": f"The photos show {what}{sized}", "examples": examples},
                "false": {"meaning": f"No {what} in the photos" + (", or its label shows another size" if size else "")}}
    return instructions, criteria


def build_request(form: dict) -> tuple[dict, list[dict]]:
    """Returns the API body and the checklist describing each question."""
    tag = parse_tag(form.get("tag"))
    full_list = form.get("goal") == "full"
    assets, state = {}, {}

    def add(raw):
        ref = f"img-{uuid.uuid4()}"
        assets[ref] = {"type": "image", "format": "jpg", "data_base64": prepare_image(raw)}
        return ref

    state["gift_photos"] = [add(raw) for raw in form["gift_photos"]]
    state["photo_notes"] = PHOTO_NOTES
    state["child"] = child(tag)

    questions, checklist = {}, []

    def ask(qid, kind, label, instructions, bad_if_yes=False, criteria=None):
        q = {"type": "noul", "instructions": instructions}
        if criteria:
            q["criteria"] = criteria
        questions[qid] = q
        checklist.append({"id": qid, "kind": kind, "label": label, "bad_if_yes": bad_if_yes})

    guides = [wish_guidance(x, tag) for x in tag["wishes"]]
    if any(g and "LEGO" in g["counts"] for g in [g for _, g in guides] +
           [wish_guidance(x, tag)[1] for x in tag["favorites"]]):
        questions["small_lego"] = {  # only worth half a gift each, and only when LEGO is on the tag
            "type": "score",
            "instructions": {"question": f"How many small boxed LEGO sets, with fewer than {SMALL_LEGO} pieces, are "
                                         "in `gift_photos`?",
                             "how": "Read the piece count (pcs) printed on each LEGO box. Count each box once, even "
                                    "if it appears in several photos. Do not count polybags or larger sets."},
            "criteria": ["None", "1 small set", "2 small sets", "3 small sets", "4 or more small sets"],
        }
    questions["strong_count"] = {
        "type": "score",
        "instructions": {
            "question": "How many separate strong gifts for `child` are in `gift_photos`, not counting small LEGO sets?",
            "strong_gift": STRONG,
            "what_each_wish_means": {w: g["counts"] for w, g in guides if g},
            "counting": "Count each physical item once, even if it appears in several photos.",
        },
        "criteria": ["No strong gifts", "1 strong gift", "2 strong gifts", "3 or more strong gifts"],
    }

    for i, (wish, guide) in enumerate(guides):
        ask(f"wish_{i}", "wish", wish,
            {"question": "Does at least one gift in `gift_photos` fit `wish`?", "wish": wish},
            criteria=fit_criteria(guide))
    for i, fav in enumerate(tag["favorites"]):
        _, guide = wish_guidance(fav, tag)
        ask(f"fav_{i}", "favorite", f"Favorite: {fav}",
            {"question": "Is at least one gift in `gift_photos` built around `favorite`?", "favorite": fav},
            criteria=fit_criteria(guide))

    if full_list and not no_clothes(tag):
        for item in CLOTHES:
            size = tag["sizes"].get(item[0], "")
            instructions, criteria = clothes_question(item, size)
            ask(f"clothes_{item[0]}", "clothes", item[1] + (f" (size {size})" if size else ""), instructions,
                criteria=criteria)

    for i, item in enumerate(lines(form.get("packing"))[:MAX_PACKING]):
        ask(f"packed_{i}", "packed", item,
            {"question": "Is `item` in `gift_photos`?", "item": item,
             "how": "Match the product on its box or tag. A similar item from another brand still counts."})

    band = age_band(tag)
    if band != "baby":
        ask("too_young", "flag", "Nothing meant for much younger kids",
            {"question": "Is any gift item in `gift_photos` made for children much younger than `child`?",
             "check": "Look for baby, toddler and preschool toys that are too young for this child."},
            bad_if_yes=True, criteria=too_young_criteria(band, who(tag)))
    for i, concern in enumerate(tag["concerns"]):
        ask(f"concern_{i}", "rule", f"Tag note: {concern}",
            {"question": "Does the bag in `gift_photos` respect `note`? Answer yes if nothing in the bag is affected.",
             "note": concern})
    for i, rule in enumerate(lines(form.get("rules"))):
        ask(f"rule_{i}", "rule", rule,
            {"question": "Do the gift items in `gift_photos` follow `rule`?", "rule": rule},
            criteria=RULE_CRITERIA.get(rule.lower()))

    body = {"model": MODEL, "state": state, "assets": assets, "questions": questions}
    return body, checklist


def call_jev(body: dict, api_key: str) -> dict:
    req = urllib.request.Request(
        API_URL, data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                 "TypeSafe-Danger-Zone-Images": "true"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Jev returned {e.code}: {e.read().decode(errors='replace')[:500]}") from None


# ---------- turning probabilities into a verdict ----------

def grade(checklist: list[dict], answers: dict, full_list: bool = False) -> dict:
    """Three strong gifts by default. With full_list, the bag also needs every wish, a top, pants, and shoes or a
    coat (clothes rows only exist when the tag allows clothes). Packing-list items that aren't seen come first."""
    rows = []
    for c in checklist:
        p = answers.get(c["id"], {}).get("noul")
        good = None if p is None else (1 - p if c["bad_if_yes"] else p)
        status = "unknown" if good is None else "met" if good >= YES else "missing" if good <= NO else "unsure"
        rows.append({**c, "p": p, "status": status})
    by_id = {r["id"]: r for r in rows}

    count = answers.get("strong_count", {})
    full = {int(k): v for k, v in count.get("probabilities", {}).items()}
    small = {int(k): v for k, v in answers.get("small_lego", {}).get("probabilities", {"0": 1.0}).items()}
    probs = {}  # strong gifts = full-size ones + half a gift per small LEGO set, rounded down, capped at GOAL
    for i, pi in full.items():
        for j, pj in small.items():
            total = min(GOAL, i + j // 2)
            probs[total] = probs.get(total, 0.0) + pi * pj
    small_likely = max(small, key=small.get)
    p_goal = probs.get(GOAL, 0.0)
    likely = max(probs, key=probs.get) if probs else 0

    on_tag = [r for r in rows if r["kind"] in ("wish", "favorite")]
    tag_met = any(r["status"] == "met" for r in on_tag)
    tag_unsure = any(r["status"] in ("unsure", "unknown") for r in on_tag)
    if on_tag and not tag_met and not tag_unsure:
        likely, p_goal = 0, 0.0  # nothing on the tag is in the bag, so nothing in it can be strong
    short = max(1, GOAL - likely)
    rule_fail = [r for r in rows if r["kind"] == "rule" and r["status"] == "missing"]
    wishes = [r for r in rows if r["kind"] == "wish"]
    open_wishes = [r for r in wishes if r["status"] != "met"]
    not_packed = [r for r in rows if r["kind"] == "packed" and r["status"] == "missing"]

    # what the full list still needs: each missing wish, top, pants, and shoes OR coat
    clothes = {r["id"]: r for r in rows if r["kind"] == "clothes"}
    need, unsure_need = [], []
    if full_list:
        need += [r["label"] for r in wishes if r["status"] == "missing"]
        unsure_need += [r["label"] for r in wishes if r["status"] in ("unsure", "unknown")]
        for cid in ("clothes_top", "clothes_pants"):
            r = clothes.get(cid)
            if r and r["status"] == "missing":
                need.append(r["label"])
            elif r and r["status"] != "met":
                unsure_need.append(r["label"])
        feet = [clothes[c] for c in ("clothes_shoes", "clothes_coat") if c in clothes]
        if any(r["status"] == "met" for r in feet):
            for r in feet:  # shoes OR coat: once one is in the bag, the other is optional
                if r["status"] != "met":
                    r["status"] = "optional"
        if feet and not any(r["status"] == "met" for r in feet):
            if all(r["status"] == "missing" for r in feet):
                need.append(f"{feet[0]['label']} (or {feet[1]['label'].lower()})" if len(feet) == 2 else feet[0]["label"])
            else:
                unsure_need.append(" or ".join(r["label"] for r in feet))

    todo = [f"Fix: {r['label']}" for r in rule_fail]
    todo += [f"Not in the photos: {r['label']}. Check the bins, or add a photo." for r in not_packed]
    todo += [f"Swap out the toy meant for little kids" if r["status"] == "missing"
             else "Double-check nothing is meant for little kids (Jev wasn't sure)"
             for r in rows if r["id"] == "too_young" and r["status"] != "met"]
    if rule_fail:
        verdict = "fix"
    elif not_packed:
        verdict = "pack"
    elif p_goal <= NO or need:
        verdict = "shop"
        if p_goal <= NO:
            todo.insert(0, f"Buy {short} more strong gift{'s' if short > 1 else ''}.")
            if short == 1 and small_likely % 2:
                todo.append(f"Idea: one more small LEGO set, to pair with the odd one in the bag")
            if not full_list:
                todo += [f"Idea: {r['label']}" for r in open_wishes] or ["Idea: a second gift for any wish on the tag"]
        todo += [f"Buy: {n}" for n in need]
    elif p_goal >= YES and not unsure_need and (tag_met or not on_tag):
        verdict = "ready"
    else:
        verdict = "check"
        todo.insert(0, "A volunteer should look: Jev isn't sure the bag is complete." if unsure_need else
                    "A volunteer should look: Jev isn't sure the bag has 3 strong gifts.")
    if small_likely:
        todo.append(f"Counted {small_likely}{'+' if small_likely == 4 else ''} small LEGO set"
                    f"{'s' if small_likely > 1 else ''} (under {SMALL_LEGO} pieces) as half a gift each.")
    todo += [f"Double-check: {r['label']} (Jev wasn't sure)" for r in rows
             if r["status"] == "unsure" and r["kind"] in ("rule", "wish", "clothes", "packed")]
    return {"verdict": verdict, "rows": rows, "todo": todo, "probs": probs, "likely": likely,
            "full": full, "small": small, "wishes_met": len(wishes) - len(open_wishes), "wishes_total": len(wishes)}


# ---------- angel files (packing day) ----------

def read_front_matter(text: str) -> tuple[dict, str]:
    """Tiny reader for the simple front matter angel files use: key: "value" and key: followed by - "item" lines."""
    if not text.startswith("---"):
        return {}, text
    head, _, body = text[3:].partition("\n---")
    data, key = {}, None
    for raw in head.splitlines():
        if m := re.match(r"^([A-Za-z_]+):\s*(.*)$", raw):
            key, value = m.group(1), m.group(2).strip()
            data[key] = value.strip('"') if value else []
        elif (m := re.match(r"^\s+-\s+(.*)$", raw)) and key and isinstance(data.get(key), list):
            data[key].append(m.group(1).strip().strip('"'))
    return data, body


def sizes_to_lines(sizes: str) -> tuple[list[str], list[str]]:
    """'Shirt 7; pants 6/7; coat 7/8; shoes 12. Hates sequins' -> size lines, plus leftover notes."""
    out, notes = [], []
    for part in re.split(r";|\.\s+", sizes or ""):
        part = part.strip(" .")
        if not part:
            continue
        word, _, rest = part.partition(" ")
        kind = size_kind(word.lower())
        if kind and rest:
            out.append(f"{kind.title()}: {rest.strip()}")
        else:
            notes.append(part)
    return out, notes


def load_angel(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        data, body = read_front_matter(f.read())
    wishes = []
    if m := re.search(r"^Wishes:\s*(.+)$", body, re.M):
        wishes = [w.strip() for w in m.group(1).split(";") if w.strip()]
    gender = {"boy": "MALE", "girl": "FEMALE"}.get(str(data.get("gender", "")).lower(), str(data.get("gender", "")).upper())
    age = str(data.get("age", ""))
    age_words = age if re.search(r"month", age, re.I) else f"{age} Years"
    size_lines, notes = sizes_to_lines(data.get("sizes", ""))
    tag = [data.get("first_name", ""), f"{gender} - {age_words}" if gender and age else f"Age: {age}"]
    tag += [f"Code Number: {data['tag_code']}"] if data.get("tag_code") else []
    tag += [f"Wish: {w}" for w in wishes] + size_lines + [f"Special Concerns: {n}" for n in notes]
    return {"code": data.get("tag_code") or os.path.splitext(os.path.basename(path))[0],
            "name": data.get("first_name", ""), "age": age, "status": data.get("status", ""),
            "tag": "\n".join(t for t in tag if t), "packing": list(data.get("assign") or []),
            "to_buy": [c for c in data.get("checklist") or [] if c.upper().startswith("NEED")]}


def load_angels(folder: str | None) -> dict:
    """Every angel file (*.md with front matter) in a private folder on this computer, keyed by tag code.
    Nothing from the folder is stored by the app or sent anywhere except the request you submit."""
    if not folder:
        return {}
    angels = {}
    for name in sorted(os.listdir(folder)):
        if name.endswith(".md") and not name.startswith(("_", ".")) and name.lower() != "readme.md":
            a = load_angel(os.path.join(folder, name))
            if a["status"] in ("", "active"):
                angels[a["code"]] = a
    return angels


# ---------- web page ----------

CSS = """
:root{--bg:#f3f5f1;--surface:#fff;--ink:#1d2a24;--muted:#5d6b64;--line:#d9e0db;--pine:#1f5c45;--pine-soft:#e3efe8;
--berry:#b0263f;--berry-soft:#f8e4e8;--kraft:#e7d3ae;--kraft-ink:#4a3a1f;--amber:#a46200;--amber-soft:#fbefd9}
@media (prefers-color-scheme:dark){:root{--bg:#111a16;--surface:#18241f;--ink:#e6efe9;--muted:#9aaca3;--line:#2b3a33;
--pine:#6fc79f;--pine-soft:#1d3a2e;--berry:#f07a8f;--berry-soft:#3a1c23;--kraft:#6a5634;--kraft-ink:#f6e8cc;
--amber:#f0b45a;--amber-soft:#3a2c14;color-scheme:dark}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,sans-serif;padding:28px 16px 56px}
.wrap{max-width:760px;margin:0 auto;display:grid;gap:22px}h1{font:400 2.2rem/1.1 Georgia,serif;margin:0}
.sub{color:var(--muted);margin:6px 0 0}
form{background:var(--kraft);color:var(--kraft-ink);border-radius:6px 22px 22px 6px;padding:24px;display:grid;gap:16px}
label{display:grid;gap:6px;font-weight:600;font-size:.95rem}
.lbl{font:500 .72rem/1 ui-monospace,monospace;letter-spacing:.1em;text-transform:uppercase;opacity:.8}
input[type=text],textarea{font:15px/1.4 system-ui,sans-serif;padding:9px 11px;border-radius:9px;border:1px solid #0002;background:var(--surface);color:var(--ink);width:100%}
textarea{min-height:76px}.row{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:12px}
small{font-weight:400;opacity:.8}button{justify-self:start;border:0;border-radius:999px;padding:12px 22px;font:600 1rem system-ui;background:var(--pine);color:var(--surface);cursor:pointer}
.card{background:var(--surface);border:1px solid var(--line);border-radius:16px;padding:20px;display:grid;gap:10px}
.pill{justify-self:start;font:700 .8rem/1 system-ui;text-transform:uppercase;letter-spacing:.04em;padding:7px 12px;border-radius:999px}
.ready{background:var(--pine-soft);color:var(--pine)}.check,.pack{background:var(--amber-soft);color:var(--amber)}.shop,.fix{background:var(--berry-soft);color:var(--berry)}
.bars{display:grid;grid-template-columns:repeat(4,1fr);gap:6px}.bar{border:1px solid var(--line);border-radius:10px;padding:8px;text-align:center;font-size:.85rem}
.bar b{display:block;font:600 1.1rem ui-monospace,monospace}.bar.on{border-color:var(--pine);background:var(--pine-soft)}
ul.checks{list-style:none;margin:0;padding:0;border:1px solid var(--line);border-radius:14px;background:var(--surface)}
ul.checks li{display:grid;grid-template-columns:28px 1fr auto;gap:10px;align-items:center;padding:11px 14px;border-top:1px solid var(--line)}
ul.checks li:first-child{border-top:0}.m{width:24px;height:24px;border-radius:50%;display:grid;place-items:center;font-weight:700;font-size:13px}
.met .m{background:var(--pine-soft);color:var(--pine)}.unsure .m,.unknown .m{background:var(--amber-soft);color:var(--amber)}.missing .m{background:var(--berry-soft);color:var(--berry)}.optional .m{background:var(--line);color:var(--muted)}
.p{font:500 .8rem ui-monospace,monospace;color:var(--muted);font-variant-numeric:tabular-nums}.kind{font:500 .68rem ui-monospace,monospace;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}
.choice{display:flex;gap:18px;flex-wrap:wrap;font-weight:400}.choice label{display:flex;gap:6px;align-items:center;font-weight:400}
select{font:15px system-ui,sans-serif;padding:8px 10px;border-radius:9px;border:1px solid #0002;background:var(--surface);color:var(--ink)}
.picker{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.picker button{padding:8px 16px;font-size:.9rem}
.err{background:var(--berry-soft);color:var(--berry);padding:14px 16px;border-radius:12px}pre{overflow:auto;font-size:12px}
"""

VERDICT = {"ready": "Ready to drop off", "check": "Volunteer: take a look", "shop": "Needs shopping",
           "fix": "Fix the bag", "pack": "Finish packing"}
LEVELS = ["0", "1", "2", "3+"]
TAG_EXAMPLE = "Sam&#10;MALE - 12 Years&#10;Wish: BIKE AND HELMET&#10;Wish: CRAFT KITS/ART SUPPLIES&#10;Favorite: AVENGERS, SPIDER MAN&#10;Wish: REMOTE CONTROL CARS/ROBOTS&#10;Special Concerns: PLEASE NO CLOTHES"
MARK = {"met": "✓", "unsure": "?", "unknown": "?", "missing": "✕", "optional": "–"}


def angel_picker(angels: dict, chosen: str) -> str:
    if not angels:
        return ""
    opts = "".join(f'<option value="{html.escape(c)}"{" selected" if c == chosen else ""}>'
                   f'{html.escape(a["name"] or c)}, {html.escape(a["age"])} · {html.escape(c)}</option>'
                   for c, a in sorted(angels.items(), key=lambda kv: kv[1]["name"].lower()))
    return f"""<form method="get" class="picker"><span class="lbl">Packing day</span>
<select name="angel"><option value="">Pick an angel…</option>{opts}</select><button type="submit">Load tag and list</button></form>"""


def page(body_html: str, form: dict | None = None, angels: dict | None = None, angel: dict | None = None) -> bytes:
    f = {k: html.escape(str(v)) for k, v in (form or {}).items() if isinstance(v, str)}
    if angel and not form:
        f = {"tag": html.escape(angel["tag"]), "packing": html.escape("\n".join(angel["packing"])), "goal": "full",
             "angel": html.escape(angel["code"])}
    rules = f.get("rules", html.escape(DEFAULT_RULES))
    goal_full = f.get("goal") == "full"
    to_buy = ""
    if angel and angel["to_buy"]:
        to_buy = ('<div class="card"><b>Still to buy, per the angel file</b><ul>' +
                  "".join(f"<li>{html.escape(t)}</li>" for t in angel["to_buy"]) + "</ul></div>")
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Angel Tree Bag Check</title><style>{CSS}</style></head><body><div class="wrap">
<header><h1>Angel Tree Bag Check</h1><p class="sub">Paste the child's tag and add photos of the bag. Jev checks the bag against the tag: 3 strong gifts, or the full list with clothes.</p></header>
{angel_picker(angels or {}, f.get("angel", ""))}
{body_html}{to_buy}
<form method="post" enctype="multipart/form-data">
  <input type="hidden" name="angel" value="{f.get('angel', '')}">
  <label>The child's tag <small>paste it as printed: name, age, Wish:, Favorite:, sizes, Special Concerns:. Add what counts after a wish, e.g. "Wish: TECH TOYS: R/C cars, robots"</small>
    <textarea name="tag" rows="8" placeholder="{TAG_EXAMPLE}" required>{f.get('tag','')}</textarea></label>
  <div><span class="lbl">What the bag needs</span><div class="choice">
    <label><input type="radio" name="goal" value="three"{"" if goal_full else " checked"}> 3 strong gifts</label>
    <label><input type="radio" name="goal" value="full"{" checked" if goal_full else ""}> Full list: every wish, a top, pants, and shoes or a coat, in the tag's sizes</label></div></div>
  <label><span class="lbl">Bag photos</span><input type="file" name="gift_photos" accept="image/*" multiple required>
    <small>Take everything out of the bag and lay it on a plain table, labels facing up. Lay LEGO boxes flat so the
    piece count shows, and turn clothes so the size tags show. iPhone (HEIC), JPEG or PNG.</small></label>
  <label>Packing list <small>optional, one item per line: what you set aside for this child. Each one is checked off in the photos (up to {MAX_PACKING}).</small>
    <textarea name="packing" rows="4">{f.get('packing','')}</textarea></label>
  <label>Drop-off rules <small>one per line, edit to match your site</small><textarea name="rules">{rules}</textarea></label>
  <button type="submit">Check the bag</button>
</form></div></body></html>""".encode()


def pct(r: dict) -> str:
    if r["p"] is None:
        return "—"
    return format(1 - r["p"] if r["bad_if_yes"] else r["p"], ".0%")


def result_html(g: dict, tag: dict, model: str) -> str:
    rows = "".join(
        f'<li class="{r["status"]}"><span class="m">{MARK[r["status"]]}</span><div>{html.escape(r["label"])}'
        f'<div class="kind">{r["kind"]}</div></div><span class="p">{pct(r)}</span></li>'
        for r in g["rows"])
    bars = "".join(f'<div class="bar{" on" if i == g["likely"] else ""}"><b>{g["probs"].get(i, 0):.0%}</b>{LEVELS[i]} strong</div>'
                   for i in range(len(LEVELS)))
    todo = "".join(f"<li>{html.escape(t)}</li>" for t in g["todo"]) or "<li>Nothing to add. Attach the tag and drop it off.</li>"
    age = tag["age"] and (tag["age"] if "month" in tag["age"] else f"{tag['age']} years")
    who = html.escape(" · ".join(x for x in (tag["name"], tag["gender"], age, tag["code"]) if x))
    return f"""<div class="card"><div class="kind">{who}</div><span class="pill {g['verdict']}">{VERDICT[g['verdict']]}</span>
<div class="bars">{bars}</div>
<div>{g['wishes_met']} of {g['wishes_total']} wishes covered</div><ul>{todo}</ul>
<div class="p">Answered by {html.escape(model)} · percent = Jev's probability</div></div>
<ul class="checks">{rows}</ul>"""


def parse_form(handler) -> dict:
    length = int(handler.headers.get("Content-Length", 0))
    raw = b"Content-Type: " + handler.headers["Content-Type"].encode() + b"\r\n\r\n" + handler.rfile.read(length)
    msg = BytesParser(policy=email_policy).parsebytes(raw)
    form = {"gift_photos": []}
    for part in msg.iter_parts():
        name, data = part.get_param("name", header="content-disposition"), part.get_payload(decode=True) or b""
        if part.get_filename() is not None:
            if not data:
                continue
            if name == "gift_photos":
                form["gift_photos"].append(data)
            else:
                form[name] = data
        else:
            form[name] = data.decode("utf-8", errors="replace").strip()
    return form


class Handler(BaseHTTPRequestHandler):
    dry_run = False
    angels_dir = None

    def send(self, content: bytes, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def angels(self) -> dict:
        try:
            return load_angels(self.angels_dir)
        except OSError:
            return {}

    def do_GET(self):
        angels = self.angels()
        code = parse_qs(urlparse(self.path).query).get("angel", [""])[0]
        self.send(page("", angels=angels, angel=angels.get(code)))

    def do_POST(self):
        form = parse_form(self)
        angels = self.angels()
        angel = angels.get(form.get("angel", ""))
        try:
            if not form["gift_photos"]:
                raise ValueError("Add at least one gift photo.")
            tag = parse_tag(form.get("tag"))
            if not tag["wishes"]:
                raise ValueError("Paste the child's tag, with at least one line starting with Wish:")
            body, checklist = build_request(form)
            if self.dry_run:
                preview = json.loads(json.dumps(body))
                for a in preview["assets"].values():
                    a["data_base64"] = f"<{len(a['data_base64'])} base64 chars>"
                self.send(page(f'<div class="card"><b>Dry run: this is what would go to Jev</b>'
                               f'<pre>{html.escape(json.dumps(preview, indent=2))}</pre></div>', form, angels, angel))
                return
            key = os.environ.get("TYPESAFE_API_KEY")
            if not key:
                raise ValueError("Set TYPESAFE_API_KEY before starting the app.")
            resp = call_jev(body, key)
            result = grade(checklist, resp["answers"], full_list=form.get("goal") == "full")
            self.send(page(result_html(result, tag, resp.get("model", MODEL)), form, angels, angel))
        except (ValueError, RuntimeError, OSError) as e:
            self.send(page(f'<div class="err">{html.escape(str(e))}</div>', form, angels, angel))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1",
                    help="address to listen on; the default keeps the app on this computer only")
    ap.add_argument("--dry-run", action="store_true", help="show the Jev request instead of sending it")
    ap.add_argument("--angels", default=os.environ.get("ANGELS_DIR"),
                    help="private folder of angel files (one .md per child) for packing day; never commit it")
    args = ap.parse_args()
    Handler.dry_run, Handler.angels_dir = args.dry_run, args.angels
    extra = [x for x in ("dry run" if args.dry_run else "", f"{len(load_angels(args.angels))} angels loaded" if args.angels else "") if x]
    print(f"Angel Tree Gift Check on http://{args.host}:{args.port}" + (f" ({', '.join(extra)})" if extra else ""))
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
