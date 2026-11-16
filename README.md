# Angel Tree Donation Checker

A volunteer pastes a child's Angel Tree tag, adds photos of the packed bag, and gets one of four answers:

- **3 strong gifts: ready** to drop off
- **Needs shopping**, with how many gifts to buy and ideas from the tag
- **Volunteer: take a look**, when the model isn't sure
- **Fix the bag**, when a tag note or drop-off rule is broken (for example, clothes in a "PLEASE NO CLOTHES" bag)

The photos are checked by [Jev](https://docs.typesafe.ai/llms.txt), TypeSafe's System One model. Jev doesn't write
text; it answers typed questions with probabilities. The app asks it how many strong gifts are in the bag, whether
each wish and favorite is covered, and whether anything is meant for much younger children. Plain code turns those
numbers into the verdict.

## Run it

```sh
pip install -r requirements.txt
TYPESAFE_API_KEY=... python3 app.py      # then open http://localhost:8000
python3 app.py --dry-run                  # shows the request it would send, without calling Jev
python3 -m unittest                       # offline tests, no API key needed
```

Get an API key at console.typesafe.ai/keys.

## The rules

- **Goal:** 3 strong gifts per child (`GOAL` in `app.py`).
- **A strong gift** is a full-size item that matches one of the child's wishes or favorites and suits their age.
  Gifts that match nothing on the tag don't count, however nice. Neither do stocking stuffers or toddler toys.
- **Small LEGO sets** (under 150 pieces, `SMALL_LEGO`) count as half a gift: two make one strong gift. They only
  count when LEGO is on the tag. LEGO polybags count as nothing.
- **Thresholds:** 70% or higher is yes, 30% or lower is no, anything between goes to a volunteer (`YES`, `NO`).

## Tags

Paste the tag as printed. Both layouts work:

```
Sam                                  NAME:   ALEX
MALE - 12 Years                      AGE:    9 YEARS
Wish: BIKE AND HELMET                WISH:   SCIENCE KITS
Favorite: AVENGERS, SPIDER MAN       WISH:   -
Special Concerns: PLEASE NO CLOTHES  FAVORITE: LEGOS
```

`NEED:` lines count as wishes, and `-` means blank. To tell Jev what a wish means, add it after a colon:
`Wish: TECH TOYS: R/C cars, robots`.

## What counts for each wish

`GUIDE` in `app.py` defines common wishes (tablet, LEGO, Roblox gift card, bike, instant camera, makeup sets,
craft kits and more), each with what counts, what doesn't, and examples of both. Jev reads these alongside the
photos. Adding a definition there is the main way to make the checker better at a kind of wish.

## Taking photos

Take everything out of the bag and lay it on a plain table, labels facing up. Lay LEGO boxes flat so the piece
count shows. Several photos of the same bag are fine: each item is counted once. iPhone (HEIC), JPEG and PNG
all work.
