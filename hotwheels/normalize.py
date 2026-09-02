"""Turn messy marketplace titles into something comparable across sites.

The same 5-pack is listed as:
  Amazon:   "Hot Wheels Basic Car 5-Pack, Multicolor, Set of 5 Toy Cars 1:64"
  Flipkart: "HOT WHEELS 5 Car Pack Assortment (Multicolor, Pack of 5)"
  Blinkit:  "Hot Wheels 5 Car Pack"

Cross-site price comparison is worthless unless those collapse to one row, so
we strip marketing noise down to a signature and fuzzy-match the remainder.
"""

from __future__ import annotations

import re
import unicodedata

from rapidfuzz import fuzz, process

# Words that appear on every listing and carry no identifying signal.
NOISE = {
    "hot", "wheels", "hotwheels", "mattel", "official", "genuine", "original",
    "toy", "toys", "car", "cars", "vehicle", "vehicles", "diecast", "die",
    "cast", "scale", "multicolor", "multicolour", "assorted", "assortment",
    "colour", "color", "may", "vary", "styles", "style", "kids", "kid",
    "children", "child", "boys", "girls", "age", "ages", "years", "year",
    "old", "gift", "gifting", "gifts", "play", "playset", "collectible",
    "collectable", "collection", "new", "latest", "best", "pack", "packs",
    "set", "sets", "piece", "pieces", "pcs", "pc", "combo", "with", "and",
    "for", "the", "of", "in", "to", "a", "an", "by", "from", "inch", "cm",
    "free", "shipping", "india", "buy", "online", "great", "value",
    # Product-line filler that identifies nothing on its own.
    "basic", "asst", "each", "count", "pull", "back",
    # Brand words. Brand is a hard gate inside the canonical key, so leaving
    # these in the signature would only dilute the model-name comparison.
    "matchbox", "majorette", "tomica", "takara", "tomy", "maisto", "greenlight",
    "bburago", "burago", "welly", "kinsmart", "siku", "norev", "schuco",
    "tarmac", "works", "inno64", "inno", "machines", "johnny", "lightning",
    "greenlightcollectibles", "collectibles",
}

# Signature for a listing whose tokens are all filler - i.e. a plain assorted
# pack with no model named. Those genuinely are the same product across sites,
# so they must share a key rather than each falling back to its raw title.
GENERIC_SIG = "assorted"

SERIES_PATTERNS: list[tuple[str, str]] = [
    (r"\bmonster\s*truck", "Monster Trucks"),
    (r"\bmario\s*kart", "Mario Kart"),
    (r"\bpremium\b|\bcar\s*culture\b|\bboulevard\b|\bteam\s*transport\b", "Premium"),
    (r"\btrack\s*(set|builder|pack)?\b|\bloop\b|\blauncher\b|\bstunt\b", "Track Set"),
    (r"\bmonster\s*maker|\bcolou?r\s*shifter", "Color Shifters"),
    (r"\bid\s*car|\bhw\s*id\b", "HW id"),
    (r"\btreasure\s*hunt|\bsuper\s*th\b|\bsth\b", "Treasure Hunt"),
    (r"\bfast\s*(and|&)\s*furious|\bfnf\b", "Fast & Furious"),
    (r"\bmarvel\b|\bbatman\b|\bdc\b|\bstar\s*wars\b", "Character Cars"),
    (r"\bmonster\s*jam\b", "Monster Jam"),
    (r"\bcity\b", "HW City"),
    (r"\bbasic\b|\bmainline\b|\bsingle\b", "Mainline"),
]

# --------------------------------------------------------------------------
# realistic (licensed real car) vs fantasy (original casting)
# --------------------------------------------------------------------------
#
# Not the same axis as the "Mainline" series tag, which means Hot Wheels' basic
# line as opposed to Premium. This is about what the car *is*: a licensed model
# of a real vehicle, or an in-house invention like Twin Mill or Bone Shaker.
#
# The signal is whether the title names a real manufacturer or a real model.
# Titles frequently give only the model ("Skyline GT-R", "Corvette"), so both
# lists are needed.

MARQUES = r"""
abarth|acura|alfa\s*romeo|alpine|am\s*general|aston\s*martin|audi|austin|
bentley|bmw|bugatti|buick|cadillac|chevrolet|chevy|chrysler|citroen|
datsun|de\s*tomaso|dodge|ducati|ferrari|fiat|ford|gmc|honda|hummer|hyundai|
infiniti|international|isuzu|jaguar|jeep|kawasaki|kia|koenigsegg|lamborghini|
lancia|land\s*rover|lexus|lincoln|lotus|maserati|mazda|mclaren|mercedes|merc|
mercury|mg\b|mini\s*cooper|mitsubishi|morris|nissan|oldsmobile|opel|pagani|
peugeot|plymouth|polaris|pontiac|porsche|ram\b|range\s*rover|renault|
rolls[\s-]*royce|saleen|scion|seat\b|shelby|skoda|studebaker|subaru|suzuki|
tesla|toyota|triumph|tvr|vauxhall|volkswagen|volvo|vw\b|willys|yamaha|zenvo
"""

MODELS = r"""
360\s*modena|911|917|959|abarth|aventador|bel\s*air|beetle|bronco|brz|
camaro|carrera|celica|challenger|charger|chevelle|civic|cobra|cooper\s*s|
corolla|corvette|countach|cuda|defender|delorean|diablo|el\s*camino|
enzo|escort|evo|explorer|f-?150|f40|f50|fairlady|firebird|focus|fj\s*cruiser|
gallardo|golf|gt-?r|gt40|gti|hemi|huracan|impala|impreza|integra|
karmann|lancer|land\s*cruiser|le\s*mans|miata|model\s*[at]|monte\s*carlo|
mustang|nova|nsx|odyssey|outlaw|pantera|prelude|prowler|rx-?[78]|s2000|
sierra|silverado|silvia|skyline|stingray|supra|testarossa|thunderbird|
torino|trans\s*am|veyron|viper|wrangler|wrx|z28|zr1|250\s*gto|300sl|
barracuda|bathurst|blazer|bluebird|boss\s*302|c10|cedric|chevette|comet|
corona|corvair|cressida|cougar|delica|econoline|elise|esprit|exige|
fairlane|falcon|galaxie|gremlin|hiace|hilux|jeepster|javelin|laurel|
mach\s*1|marauder|maverick|montego|monza|pacer|patrol|ranchero|riviera|
road\s*runner|roadrunner|skylark|starlet|super\s*bee|tacoma|tahoe|
suburban|escalade|denali|240z|280z|300zx|350z|370z|r3[2-5]\b|ae86|mr2|
sti\b|s1[345]\b|gran\s*torino|grand\s*cherokee|alphard|vulcan|king\s*cab
"""


def _alts(block: str) -> re.Pattern[str]:
    body = "|".join(p.strip() for p in block.split("|") if p.strip())
    return re.compile(rf"(?<![a-z0-9])(?:{body})(?![a-z])", re.I | re.X)


_MARQUE_RE = _alts(MARQUES)
_MODEL_RE = _alts(MODELS)

REALISTIC, FANTASY = "Realistic", "Fantasy"


def detect_realism(title: str) -> str | None:
    """'Realistic', 'Fantasy', or None when the question does not apply.

    None covers multipacks and track sets: a 5-pack mixes both kinds, so
    labelling it either way would be a guess presented as a fact.
    """
    t = title or ""
    if pack_size(t) > 1 or SET_HINTS.search(t):
        return None
    if _MARQUE_RE.search(t) or _MODEL_RE.search(t):
        return REALISTIC
    return FANTASY


SET_HINTS = re.compile(
    r"\b(set|track|playset|pack|bundle|combo|garage|loop|launcher|kit)\b", re.I
)

# Scale notation ("1:64") must be removed before counting, or "5-Car Pack of
# 1:64 Scale" reads as "pack of 1".
_SCALE = re.compile(r"\b\d{1,3}\s*[:x]\s*\d{1,3}\b")

_PACK_PATTERNS = [
    re.compile(r"\bpack\s*of\s*(\d{1,2})\b", re.I),
    re.compile(r"\bset\s*of\s*(\d{1,2})\b", re.I),
    # "5-Car Pack", "5 car gift pack", "10 Car", "(5 Monster Trucks)".
    # One optional word may sit between the count and the noun ("5 Monster
    # Trucks"); more than that and it stops being a count.
    re.compile(r"\b(\d{1,2})\s*[- ]?\s*(?:\w+\s+)?(?:car|truck|vehicle)s?\b", re.I),
    re.compile(r"\b(\d{1,2})\s*[- ]?\s*pack\b", re.I),
    re.compile(r"\b(\d{1,2})\s*pcs?\b", re.I),
    re.compile(r"\b(\d{1,2})\s*pieces?\b", re.I),
]

_PRICE_RE = re.compile(r"(\d[\d,]*(?:\.\d{1,2})?)")


def clean_text(s: str) -> str:
    """Normalize unicode, collapse whitespace, drop bracketed marketing."""
    s = unicodedata.normalize("NFKD", s or "")
    s = s.replace("​", " ").replace("&amp;", "&")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def parse_price(raw: str | float | int | None) -> float | None:
    """'₹1,299.00' -> 1299.0. Returns None when nothing numeric is present."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    m = _PRICE_RE.search(str(raw).replace("₹", " "))
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def pack_size(title: str) -> int:
    stripped = _SCALE.sub(" ", title or "")
    for pat in _PACK_PATTERNS:
        m = pat.search(stripped)
        if m:
            n = int(m.group(1))
            if 1 <= n <= 50:
                return n
    return 1


def detect_series(title: str) -> str | None:
    low = title.lower()
    for pattern, label in SERIES_PATTERNS:
        if re.search(pattern, low):
            return label
    return None


def is_set(title: str) -> bool:
    return bool(SET_HINTS.search(title)) or pack_size(title) > 1


# Phrases whose numbers mean "how many" or "how old", never "which model".
# They are deleted wholesale so the remaining digits can be kept - those are
# model identity ("Promo 12", "Mario Kart 8", collector number "160/250").
_NUMERIC_NOISE = [
    re.compile(r"\bages?\s*\d+\s*(?:and\s*up|\+|and\s*above)?", re.I),
    re.compile(r"\b\d+\s*(?:yrs?|years?)\b(?:\s*(?:and\s*up|\+))?", re.I),
    re.compile(r"\bpack\s*of\s*\d+\b", re.I),
    re.compile(r"\bset\s*of\s*\d+\b", re.I),
    re.compile(r"\b\d+\s*[- ]?\s*(?:\w+\s+)?(?:car|truck|vehicle)s?\b", re.I),
    re.compile(r"\b\d+\s*[- ]?\s*(?:car\s*)?pack\b", re.I),
    re.compile(r"\b\d+\s*pcs?\b", re.I),
    re.compile(r"\b\d+\s*pieces?\b", re.I),
]


def signature(title: str) -> str:
    """Reduce a title to sorted identifying tokens.

    Keeps model names and numbers ('skyline', 'gtr', 'r34', 'promo 12') while
    discarding the boilerplate every listing shares.
    """
    low = clean_text(title).lower()
    low = re.sub(r"\(.*?\)|\[.*?\]", " ", low)          # bracketed asides
    low = _SCALE.sub(" ", low)                           # 1:64 scale
    for pat in _NUMERIC_NOISE:
        low = pat.sub(" ", low)
    low = re.sub(r"[^a-z0-9\s]", " ", low)

    tokens = []
    for tok in low.split():
        if tok in NOISE or len(tok) <= 1:
            continue
        tokens.append(tok)

    # Cap length so one site's keyword-stuffed title cannot dominate the match.
    return " ".join(sorted(set(tokens))[:12]) or GENERIC_SIG


def canonical_key(title: str, hint: str | None = None) -> str:
    """Stable identity string.

    Brand and pack size are baked in, so neither can be fuzzed away: a Matchbox
    Skyline is a different product from a Hot Wheels Skyline, and a single car
    is not a 5-pack, however similar the words in between.
    """
    brand = detect_brand(title, hint) or "Other"
    return f"{brand}|{signature(title)}|p{pack_size(title)}"


def key_brand(key: str) -> str:
    return key.split("|", 1)[0]


def key_signature(key: str) -> str:
    """The signature portion of a canonical key."""
    return key.split("|", 1)[-1].rsplit("|p", 1)[0]


def numeric_tokens(sig: str) -> frozenset[str]:
    """Bare numbers left in a signature, which carry model identity.

    Counts and ages are already stripped by `_NUMERIC_NOISE`, so whatever
    survives distinguishes variants: 'Promo 6' vs 'Promo 12'.
    """
    return frozenset(t for t in sig.split() if t.isdigit())


def find_match(
    title: str,
    candidates: dict[str, str],
    threshold: int = 87,
    hint: str | None = None,
) -> str | None:
    """Fuzzy-match a title's signature against known canonical keys.

    `candidates` maps canonical_key -> signature. Two things are hard gates
    rather than inputs to the score, because similarity cannot express them:
    pack size (a single car is not a 10-pack) and the numbers in the title
    ('Promo 6' and 'Promo 12' differ by one short token and score ~96, but they
    are different trucks).
    """
    sig = signature(title)
    if not sig:
        return None

    want_pack = pack_size(title)
    want_nums = numeric_tokens(sig)
    want_brand = detect_brand(title, hint) or "Other"
    pool = {
        k: v for k, v in candidates.items()
        if k.endswith(f"|p{want_pack}")
        and key_brand(k) == want_brand
        and numeric_tokens(v) == want_nums
    }
    if not pool:
        return None

    keys = list(pool.keys())
    sigs = [pool[k] for k in keys]

    # token_sort_ratio, not token_set_ratio. The set variant scores on the
    # *intersection*, so two different Premium cars sharing a long boilerplate
    # tail ("premium culture real riders tires") score ~90 and wrongly merge -
    # observed collapsing 17 distinct models into one product. The sort variant
    # compares the full token strings, so unmatched model names cost score.
    hit = process.extractOne(sig, sigs, scorer=fuzz.token_sort_ratio, score_cutoff=threshold)
    if hit is None:
        return None
    return keys[hit[2]]


# --------------------------------------------------------------------------
# brands
# --------------------------------------------------------------------------

# brand -> (regex of aliases, native_1_64)
# native_1_64 means the brand's normal output is 1:64-class, so a listing that
# states no scale is accepted. Multi-scale brands (Maisto, Bburago, Welly...)
# sell mostly 1:24/1:18, so they must say 1:64 explicitly or they are dropped.
BRANDS: dict[str, tuple[str, bool]] = {
    "Hot Wheels":       (r"hot\s*wheels?|hotwheels", True),
    "Matchbox":         (r"match\s*box|matchbox", True),
    "Majorette":        (r"majorette", True),
    "Tomica":           (r"tomica|takara\s*tomy", True),
    "Mini GT":          (r"\bmini\s*gt\b|\btsm\s*model", True),
    "M2 Machines":      (r"\bm2\s*machines?\b", True),
    "Greenlight":       (r"green\s*light\s*collectibles|\bgreenlight\b", True),
    "Auto World":       (r"\bauto\s*world\b", True),
    "Johnny Lightning": (r"johnny\s*lightning", True),
    "Inno64":           (r"\binno\s*64\b|\binno64\b", True),
    "Tarmac Works":     (r"tarmac\s*works", True),
    "Pop Race":         (r"\bpop\s*race\b", True),
    "Era Car":          (r"\bera\s*car\b", True),
    "Kaido House":      (r"kaido\s*house", True),
    "Schuco":           (r"\bschuco\b", False),
    "Siku":             (r"\bsiku\b", False),
    "Norev":            (r"\bnorev\b", False),
    "Maisto":           (r"\bmaisto\b", False),
    "Bburago":          (r"\bbburago\b|\bburago\b", False),
    "Welly":            (r"\bwelly\b", False),
    "Kinsmart":         (r"\bkinsmart\b", False),
}

_BRAND_RES: list[tuple[str, re.Pattern[str], bool]] = [
    (name, re.compile(pat, re.I), native) for name, (pat, native) in BRANDS.items()
]

# Any "1:NN" in the title. 1:64 is nominal; Majorette, Tomica and Siku sit a
# little either side of it and still share a shelf, so the whole 55-68 band
# counts as "1:64 class". Anything else (1:18, 1:24, 1:32, 1:43, 1:87) is a
# different size of model entirely and must not appear alongside these.
_ANY_SCALE = re.compile(r"\b1\s*[:/]\s*(\d{1,3})\b")
SCALE_MIN, SCALE_MAX = 55, 68


# Third-party sellers put the brand in the title to ride its search traffic:
# "BharatToys Hot Wheels Style Die Cast Toy Cars Set". These are not the brand
# and must not be priced against it.
_KNOCKOFF = re.compile(
    r"(?:hot\s*wheels?|hotwheels|matchbox|majorette|tomica)\s*"
    r"(?:style|styled|type|like|replica|copy)\b"
    r"|(?:compatible|inspired|similar)\s+(?:with|by|to)\s*"
    r"(?:hot\s*wheels?|matchbox|majorette)"
    r"|\bnon[- ]branded\b|\bfirst[- ]copy\b",
    re.I,
)


# Accessories name every brand they fit, so brand matching alone happily files
# a display rack under "Tomica". These are not cars and must not be priced
# against them. "Display Set"/"Collector Set" are deliberately absent - those
# are genuine Mattel products that contain cars.
_ACCESSORY = re.compile(
    r"\bdisplay\s*(?:rack|stand|shelf|case|box)\b"
    r"|\b(?:acrylic|protector|blister)\s*(?:case|pack|box)\b"
    r"|\bprotectors?\b|\bcard\s*saver\b|\bclam\s*shell\b"
    r"|\b(?:storage|carry(?:ing)?)\s*(?:case|box|bag)\b"
    r"|\borgani[sz]er\b|\bparking\s*lot\s*rack\b"
    r"|\b(?:key\s*chain|keychain|sticker|poster|doll|medal|badge|lanyard)\b"
    r"|\b(?:rubber\s*tyres?|rubber\s*tires?|wheels?\s*set|axles?)\s*(?:for|kit)?\b"
    r"|\bcar\s*stand\b|\bmodel\s*stand\b"
    # Scenery and display dressing sold alongside the cars.
    r"|\btapes?\b|\bdiorama\w*\b|\bbackdrop\b|\bplay\s*mat\b|\bdisplay\s*base\b"
    r"|\bwall\s*mount\b|\bshowcase\b",
    re.I,
)


# Marketplace sellers observed putting a brand name in the title of their own
# generic diecast ("Arizuul hot wheels 1:64 1970 dodge challenger"). The brand
# position rule alone does not catch these, because they lead with the brand.
# This list is maintained by hand from real results - add to it when a fake
# shows up in the dashboard.
FAKE_BRAND_SELLERS = re.compile(
    r"^\s*(?:arizuul|bharattoys|shinetoy|invite|okean|umadiya|graphene|"
    r"enakshi|artisoul|pluspoint|kidsvile|toyshine|wembley|zhask|countrylink)\b",
    re.I,
)

# Misspellings resellers use to ride the brand without matching it exactly.
_BRAND_TYPO = re.compile(r"\bhot\s*wheelss+\b|\bhotwheelss+\b|\bmatch\s*boxx+\b", re.I)


def is_knockoff(title: str) -> bool:
    text = title or ""
    return bool(
        _KNOCKOFF.search(text)
        or FAKE_BRAND_SELLERS.search(text)
        or _BRAND_TYPO.search(text)
    )


def is_accessory(title: str) -> bool:
    return bool(_ACCESSORY.search(title or ""))


# Brands whose names are ordinary words in another aisle. Blinkit sells "Big
# Matchbox by Homelites" - a box of safety matches - at Rs 10, which the
# Matchbox pattern matches happily. Only these brands must prove they are
# talking about a vehicle; requiring it of every brand would reject perfectly
# good model-only titles like "Majorette Porsche 911".
CONTEXT_REQUIRED = {"Matchbox", "Mini GT", "Auto World", "Pop Race", "Welly", "Siku"}

_CONTEXT = re.compile(
    r"\b(?:car|cars|truck|trucks|vehicle|vehicles|die[\s-]?cast|diecast|"
    r"model|models|racer|racing|track|playset|convoy|bus|bike|motorcycle|"
    r"scale|collect(?:or|ible|ibles)|assortment|1\s*[:/]\s*\d{2,3})\b",
    re.I,
)

# Marketplace titles lead with the brand. A brand name appearing well into the
# title is usually a third-party seller stuffing keywords: "Arizuul 1:64 Dodge
# Challenger Diecast Model hot wheels cars under 100".
BRAND_POSITION_LIMIT = 35


def has_vehicle_context(title: str) -> bool:
    return bool(_CONTEXT.search(title or ""))


def _all_brands(title: str) -> list[tuple[str, int]]:
    """Brands present, with the offset each was found at."""
    out = []
    for name, rx, _n in _BRAND_RES:
        m = rx.search(title or "")
        if m:
            out.append((name, m.start()))
    return out


def brand_from_hint(raw: str | None) -> str | None:
    """Map a store's own vendor/brand field onto a tracked brand.

    Specialty shops label the brand in a `vendor` field, and their titles often
    omit it entirely ("MILD HOOK CREASE - SUPER TREASURE HUNT - FERRARI").
    Without this those real listings would be discarded.
    """
    if not raw:
        return None
    for name, rx, _native in _BRAND_RES:
        if rx.search(raw):
            return name
    return None


def detect_brand(title: str, hint: str | None = None) -> str | None:
    if is_knockoff(title) or is_accessory(title):
        return None

    found = _all_brands(title)
    if not found:
        # Fall back to the seller's own brand field.
        return brand_from_hint(hint)
    # A title naming three or more brands is a compatibility list on an
    # accessory ("...for Siku Matchbox Greenlight Tomica"), not a car that
    # belongs to any one of them.
    if len(found) >= 3:
        return None

    name, pos = min(found, key=lambda p: p[1])
    if pos > BRAND_POSITION_LIMIT:
        return None
    if name in CONTEXT_REQUIRED and not has_vehicle_context(title):
        return None
    return name


def stated_scales(title: str) -> list[int]:
    """Every scale denominator mentioned in a title."""
    return [int(m) for m in _ANY_SCALE.findall(title or "") if m.isdigit()]


def is_target_diecast(title: str, hint: str | None = None) -> bool:
    """True for a 1:64-class diecast from a brand we track.

    Two failure modes this guards against, both seen live: marketplace
    'related items' from no-name sellers, and genuine listings from these same
    brands at the wrong size - Maisto and Bburago mostly sell 1:24, and a 1:24
    car next to a 1:64 car is not a price comparison.
    """
    brand = detect_brand(title, hint)
    if brand is None:
        return False

    scales = stated_scales(title)
    if scales:
        # A title may mention several (e.g. "1:64 scale, 1:43 display case");
        # accept when any stated scale is in the 1:64 band.
        return any(SCALE_MIN <= s <= SCALE_MAX for s in scales)

    # No scale stated: trust brands whose whole catalogue is 1:64-class.
    return BRANDS[brand][1]


def is_wanted(title: str, hint: str | None = None, *,
              exclude_fantasy: bool = False) -> bool:
    """The full keep/drop decision for one listing.

    `is_target_diecast` answers "is this the right brand at the right scale".
    This adds the taste question on top: with `exclude_fantasy` set, a casting
    with no real-world car behind it is dropped. Multipacks and track sets are
    never judged - `detect_realism` returns None for them, because a 5-pack
    mixes both kinds - so they survive either way.
    """
    if not is_target_diecast(title, hint):
        return False
    if exclude_fantasy and detect_realism(title) == FANTASY:
        return False
    return True


def looks_like_hotwheels(title: str) -> bool:
    """Back-compat alias used by the tests; brand-agnostic now."""
    return is_target_diecast(title)
