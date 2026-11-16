# Angel Tree Donation Checker

A volunteer pastes a child's Angel Tree tag, adds photos of the packed bag, and gets one of five answers:

- **Ready** to drop off
- **Needs shopping**, with how many gifts to buy and ideas from the tag
- **Volunteer: take a look**, when the model isn't sure
- **Fix the bag**, when a tag note or drop-off rule is broken (for example, clothes in a "PLEASE NO CLOTHES" bag)
- **Finish packing**, when something on the packing list isn't in the photos

The photos are checked by [Jev](https://docs.typesafe.ai/llms.txt), TypeSafe's System One model. Jev doesn't write
text; it answers typed questions with probabilities. The app asks it how many strong gifts are in the bag, whether
each wish and favorite is covered, and whether anything is meant for much younger children. Plain code turns those
numbers into the verdict.

## Run it

```sh
pip install -r requirements.txt
TYPESAFE_API_KEY=... python3 app.py      # then open http://localhost:8000
python3 app.py --dry-run                  # shows the request it would send, without calling Jev
python3 app.py --angels samples/angels    # packing day, with made-up sample angels
python3 -m unittest                       # offline tests, no API key needed
```

Get an API key at console.typesafe.ai/keys. The app only listens on this computer (`127.0.0.1`); pass
`--host 0.0.0.0` to open it to your network.

## The rules

- **Goal:** 3 strong gifts per child (`GOAL` in `app.py`).
- **A strong gift** is a full-size item that matches one of the child's wishes or favorites and suits their age.
  Gifts that match nothing on the tag don't count, however nice. Neither do stocking stuffers or toddler toys.
- **Small LEGO sets** (under 150 pieces, `SMALL_LEGO`) count as half a gift: two make one strong gift. They only
  count when LEGO is on the tag. LEGO polybags count as nothing.
- **Full list** (choose it on the form): the bag also needs every wish, a top, pants, and shoes or a coat, in the
  sizes on the tag. Shoes and coats can be one size up. Once shoes or a coat is in, the other is optional. A
  "no clothes" note skips the clothes.
- **Age matters:** wish definitions and the "too young" check follow the child's age (2-5, 6-9, 10 and up), so a
  3-wheel scooter counts for a 4-year-old but not a 12-year-old, and DUPLO is fine for a 3-year-old. Children
  under 2 skip the "too young" check. Ages in months work ("FEMALE - 20 Months").
- **Ready** needs at least one wish or favorite clearly covered, whatever the gift count says.
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

Size lines (`Shirt: 7`, `PANTS SIZE: 7/8`, `Shoes: 13.5`, `Coat: 7T`) are used by the full list, and
`Code Number:` shows on the result. `NEED:` lines count as wishes, and `-` means blank. To tell Jev what a wish means, add it after a colon:
`Wish: TECH TOYS: R/C cars, robots`.

## Packing day

Point the app at a folder of angel files and pick a child from the list at the top. It fills in the tag, switches
to the full list, and loads the packing list: everything you set aside for that child. Each packing-list item is
checked off in the photos, and anything not seen comes first in the to-do list.

```sh
python3 app.py --angels /path/to/your/private/angels     # or set ANGELS_DIR
```

An angel file is Markdown with front matter, one per child. See `samples/angels/` for the format (made-up kids):
`first_name`, `age` (years, or "20 months"), `gender`, `tag_code`, `sizes` ("Shirt 6; pants 6; shoes 12"),
`assign` (the packing list) and `checklist` (lines starting `NEED` show as still to buy), plus a `Wishes:` line in
the body. You can also type a packing list by hand on any bag.

**Keep real angel files out of this repo.** It is public. The folder is read on your computer when the page loads;
nothing from it is saved, and Jev only ever gets the child's age, gender, wishes, favorites, sizes and item names,
never the name or tag code.

## What counts for each wish

`GUIDE` in `app.py` defines common wishes (tablet, LEGO, Roblox gift card, bike, instant camera, makeup sets,
craft kits and more), each with what counts, what doesn't, and examples of both. Jev reads these alongside the
photos. Adding a definition there is the main way to make the checker better at a kind of wish.

## Taking photos

Take everything out of the bag and lay it on a plain table, labels facing up. Lay LEGO boxes flat so the piece
count shows. Several photos of the same bag are fine: each item is counted once. iPhone (HEIC), JPEG and PNG
all work.
