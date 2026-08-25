#!/usr/bin/env python3
"""Build a local dog-breed dataset (photos + Hebrew names) for the quiz game.

Source of truth is Wikidata: every item that is an instance of "dog breed"
(Q39367), ordered by sitelink count so the best-documented breeds come first.
That count rides along into breeds.json, where the quiz uses it to rank breeds
from famous to obscure and build its difficulty levels.
Each breed gets IMAGES_PER_BREED photos: the curated P18 picture, falling back
to the breed's Commons category when Wikidata has no P18. It also picks up the
opening line of the breed's Hebrew Wikipedia article, which the quiz shows as a
"did you know" after each answer.

Run it as many times as you like -- breeds that already have their photos on
disk are skipped without touching the network, and lowering IMAGES_PER_BREED
prunes the surplus on the next run.
"""

import http.client
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

MAX_BREEDS = 200
IMAGES_PER_BREED = 1
REQUEST_DELAY = 1.0
TIMEOUT = 60
# Wikimedia answers 429 when it thinks we are hammering it. A single 429 used
# to cost us the photo outright, which is how a run came back with one image
# for most breeds -- so those are now waited out and retried.
MAX_ATTEMPTS = 4
# Bumping this discards facts picked by an older, worse heuristic.
FACT_VERSION = 3
BACKOFF_SECONDS = 5
MAX_BACKOFF = 120

CONTACT = "dog-breed-quiz/1.0 (https://github.com/yadin2000/funbis-calc; contact: f488yt79sn@privaterelay.appleid.com)"
USER_AGENT = CONTACT

SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
HEWIKI_API = "https://he.wikipedia.org/w/api.php"

OUTPUT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMAGES_DIR = os.path.join(OUTPUT_DIR, "images")
BREEDS_JSON = os.path.join(OUTPUT_DIR, "breeds.json")
FAILED_LOG = os.path.join(OUTPUT_DIR, "failed.log")

SKIP_EXTENSIONS = (".svg", ".ogv", ".webm", ".gif")

# Anything a flaky network or a surprising response body can throw at us.
# urllib.error.URLError and HTTPError are both OSError subclasses.
FETCH_ERRORS = (OSError, http.client.HTTPException, ValueError, KeyError)

QUERY = """
SELECT ?breed ?nameEn ?nameHe ?image ?commons ?sitelinks ?article WHERE {
  ?breed wdt:P31 wd:Q39367 ;
         wikibase:sitelinks ?sitelinks .
  ?breed rdfs:label ?nameEn .
  FILTER(LANG(?nameEn) = "en")
  OPTIONAL {
    ?breed rdfs:label ?nameHe .
    FILTER(LANG(?nameHe) = "he")
  }
  OPTIONAL { ?breed wdt:P18 ?image }
  OPTIONAL { ?breed wdt:P373 ?commons }
  OPTIONAL {
    ?page schema:about ?breed ;
          schema:isPartOf <https://he.wikipedia.org/> ;
          schema:name ?article .
  }
}
ORDER BY DESC(?sitelinks)
"""


def log_failure(name):
    """Record a breed we could not finish, one name per line."""
    try:
        with open(FAILED_LOG, "a", encoding="utf-8") as handle:
            handle.write(name + "\n")
    except OSError as exc:
        print("  could not write failed.log: %s" % exc)


def slugify(name):
    """lowercase-hyphenated ASCII, safe as a directory name."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    ascii_name = ascii_name.lower()
    ascii_name = re.sub(r"[^a-z0-9]+", "-", ascii_name)
    return ascii_name.strip("-") or "breed"


def retry_delay(error, attempt):
    """How long to wait before retrying -- Wikimedia's figure if it gave one."""
    header = ""
    if hasattr(error, "headers") and error.headers:
        header = (error.headers.get("Retry-After") or "").strip()
    if header.isdigit():
        return min(int(header), MAX_BACKOFF)
    return min(BACKOFF_SECONDS * (2 ** attempt), MAX_BACKOFF)


def retryable(error):
    """Rate limiting and server hiccups are worth another go; 404 is not."""
    if isinstance(error, urllib.error.HTTPError):
        return error.code == 429 or error.code >= 500
    return isinstance(error, FETCH_ERRORS)


def request(url, headers=None, binary=False):
    """One HTTP GET, retried through rate limiting, then the courtesy delay."""
    all_headers = {"User-Agent": USER_AGENT}
    if headers:
        all_headers.update(headers)
    req = urllib.request.Request(url, headers=all_headers)

    for attempt in range(MAX_ATTEMPTS):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
                payload = response.read()
        except FETCH_ERRORS as exc:
            last = attempt == MAX_ATTEMPTS - 1
            if last or not retryable(exc):
                time.sleep(REQUEST_DELAY)
                raise
            wait = retry_delay(exc, attempt)
            print("    %s -- waiting %ds before retry %d/%d"
                  % (exc, wait, attempt + 2, MAX_ATTEMPTS))
            time.sleep(wait)
            continue
        time.sleep(REQUEST_DELAY)
        return payload if binary else payload.decode("utf-8")


def file_title(url):
    """The Commons file name behind a URL, normalised for comparison."""
    path = urllib.parse.urlparse(url).path
    title = urllib.parse.unquote(path.rsplit("/", 1)[-1])
    return title.replace("_", " ").strip()


def is_image(title):
    return not title.lower().endswith(SKIP_EXTENSIONS)


def fetch_breeds():
    """All dog breeds from Wikidata, deduplicated, best documented first."""
    url = SPARQL_ENDPOINT + "?" + urllib.parse.urlencode({"query": QUERY})
    raw = request(url, headers={"Accept": "application/sparql-results+json"})
    bindings = json.loads(raw)["results"]["bindings"]

    breeds = []
    seen = set()
    for row in bindings:
        uri = row["breed"]["value"]
        if uri in seen:
            continue
        seen.add(uri)
        breeds.append({
            "name_en": row["nameEn"]["value"],
            "name_he": row["nameHe"]["value"] if "nameHe" in row else None,
            "image": row["image"]["value"] if "image" in row else None,
            "commons": row["commons"]["value"] if "commons" in row else None,
            "sitelinks": int(row["sitelinks"]["value"]) if "sitelinks" in row else 0,
            "article": row["article"]["value"] if "article" in row else None,
        })
    return breeds


def thumb_url(image_url, width=800):
    """A width-limited rendering of a P18 file -- never the original."""
    separator = "&" if "?" in image_url else "?"
    return "%s%swidth=%d" % (image_url, separator, width)


def category_thumbs(category, wanted, exclude):
    """Up to `wanted` 800px thumbnails from a Commons category."""
    params = {
        "action": "query",
        "format": "json",
        "formatversion": "2",
        "generator": "categorymembers",
        "gcmtitle": "Category:" + category,
        "gcmtype": "file",
        "gcmlimit": "50",
        "prop": "imageinfo",
        "iiprop": "url",
        "iiurlwidth": "800",
    }
    raw = request(COMMONS_API + "?" + urllib.parse.urlencode(params))
    pages = json.loads(raw).get("query", {}).get("pages", [])

    thumbs = []
    for page in pages:
        title = page.get("title", "")
        if title.startswith("File:"):
            title = title[len("File:"):]
        title = title.replace("_", " ").strip()
        if not is_image(title) or title in exclude:
            continue
        info = page.get("imageinfo") or []
        if not info:
            continue
        url = info[0].get("thumburl") or ""
        if not url:
            continue
        exclude.add(title)
        thumbs.append(url)
        if len(thumbs) >= wanted:
            break
    return thumbs


def existing_images(directory):
    """The photos already on disk for one breed, in numeric order."""
    try:
        names = [f for f in os.listdir(directory) if not f.startswith(".")]
    except OSError:
        return []

    def order(name):
        stem = os.path.splitext(name)[0]
        return (0, int(stem), "") if stem.isdigit() else (1, 0, name)

    return sorted(names, key=order)


# Scoring a sentence for how much of a "did you know" it is. The first run
# showed why this matters: 44% of the facts came back as taxonomy ("הוא אחד
# מגזעי כלב הבית"), bare measurements, or worse.
STRONG = ("ביותר", "הכי ", "היחיד", "היחידה", "לראשונה", "נכחד", "מקודש", "בעולם")
NOTABLE = (
    "שימש", "משמש", "שימשו", "פותח", "פותחה", "טופח", "הורבע", "נועד", "מיועד",
    "מסוגל", "מקורו", "מוצאו", "נחשב", "בוית", "אולף", "קרוי על שם", "נגזר",
    "פירוש", "המאה ה", "בשנת", "ציד", "שמירה", "רעיי", "מזחל", "חוש ריח",
    "הצל", "מהיר", "אינטליגנט", "התפרסם", "מפורסם", "סיפור", "עתיק", "נפוץ",
    "סובל", "מחלה", "תורש", "נשיא", "מלך", "מלכה", "מלחמ", "סרט", "יוצא דופן",
    "בניגוד", "ידוע ב", "מיוחד", "אגדה", "הוברח", "גילוי",
)
# A lead almost always opens by classifying the breed, which is the least
# interesting thing the article has to say.
DEFINITIONS = (
    "הוא גזע", "היא גזע", "הם גזע", "הוא כלב הבית", "הוא תת-מין", "הוא תת המין",
    "הוא אחד מגזעי", "שייך למשפחת הכלביים", "הוא שמו של גזע", "מקבוצת הטריירים",
    "השייך לקבוצת", "הוא גזע של כלב", "שם מדעי",
)
MEASURES = ("ס\"מ", "סנטימ", "ק\"ג", "קילוג", "מטר")
DIMENSIONS = ("גובה", "משקל", "אורך", "גובהו", "משקלו", "אורכו")
# Editorial residue that has no business in a game.
ARTEFACTS = ("[דרוש מקור]", "[דרושה הבהרה]", "[מקור]")

# Below this a sentence is not worth showing at all -- better an absent box
# than a boring one.
MIN_SCORE = 3


def clean(text):
    """Strip the editor-facing residue out of article text."""
    for mark in ARTEFACTS:
        text = text.replace(mark, "")
    text = re.sub(r"\[\d+\]", "", text)          # [1] footnote markers
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def sentences_of(text):
    """Article text as sentences, skipping headings and stub lines."""
    out = []
    for line in text.split("\n"):
        line = line.strip()
        if not line or line.startswith("=="):     # section headings
            continue
        line = clean(line)
        # A period followed by a space *or* directly by a Hebrew letter -- real
        # articles contain both, and only handling the first glued two
        # sentences together.
        for chunk in re.split(r"(?<=[^\d])\.(?=\s|[\u0590-\u05FF])", line):
            chunk = chunk.strip().rstrip(" .")
            if chunk:
                out.append(chunk)
    return out


def is_measurement(sentence):
    """A sentence that is only how tall and how heavy the dog is."""
    return (any(u in sentence for u in MEASURES)
            and any(dim in sentence for dim in DIMENSIONS))


def score_sentence(sentence):
    """How much of a "did you know" a sentence is."""
    strong = any(word in sentence for word in STRONG)
    # A superlative earns its keep even when the sentence also classifies the
    # breed -- "one of the oldest breeds" is a fact, not taxonomy.
    interesting = strong or any(word in sentence for word in NOTABLE)
    score = 0
    if strong:
        score += 5
    if interesting:
        score += 3
    if any(ch.isdigit() for ch in sentence) and interesting:
        score += 1                                 # a date or a speed, not a size
    if "%" in sentence or re.search(r"\b1[5-9]\d\d\b|\b20\d\d\b", sentence):
        score += 3                                 # a real figure or a real year
    if is_measurement(sentence) and not strong:
        score -= 6
    if any(word in sentence for word in DEFINITIONS) and not interesting:
        score -= 6
    if "מספר הגזע" in sentence:
        score -= 6
    # Only genuinely stubby lines are penalised: "he is the fastest dog in the
    # world" is 43 characters and is exactly what we are looking for.
    if len(sentence) < 35:
        score -= 3
    if len(sentence) > 230:
        score -= 2
    return score


def cap(sentence, limit=230):
    sentence = clean(sentence).rstrip(" .")
    if not sentence:
        return ""
    if len(sentence) > limit:
        return sentence[:limit].rsplit(" ", 1)[0] + "…"
    return sentence + "."


def best_fact(text):
    """The most interesting sentence in the article, or nothing at all."""
    ranked = [(score_sentence(part), -i, part)
              for i, part in enumerate(sentences_of(text))]
    if not ranked:
        return ""
    score, _, sentence = max(ranked)
    return cap(sentence) if score >= MIN_SCORE else ""


def fetch_fact(article):
    """The lead of a breed's Hebrew Wikipedia article, as a "did you know"."""
    params = {
        "action": "query",
        "format": "json",
        "formatversion": "2",
        "prop": "extracts",
        "explaintext": "1",
        "redirects": "1",
        "titles": article,
    }
    raw = request(HEWIKI_API + "?" + urllib.parse.urlencode(params))
    pages = json.loads(raw).get("query", {}).get("pages", [])
    for page in pages:
        extract = page.get("extract") or ""
        if extract:
            return best_fact(extract[:20000])
    return ""


def known_facts():
    """Facts collected on an earlier run, so a re-run does not refetch them."""
    try:
        with open(BREEDS_JSON, encoding="utf-8") as handle:
            previous = json.load(handle)
    except (OSError, ValueError):
        return {}
    return {b["slug"]: b["fact"] for b in previous
            if isinstance(b, dict) and b.get("slug") and b.get("fact")
            and b.get("fact_version") == FACT_VERSION}


def trim_extras(directory, keep):
    """Drop photos past the cap, so lowering IMAGES_PER_BREED actually takes."""
    dropped = 0
    for name in existing_images(directory)[keep:]:
        try:
            os.remove(os.path.join(directory, name))
            dropped += 1
        except OSError:
            pass
    return dropped


def download(url, destination):
    payload = request(url, binary=True)
    if not payload:
        raise ValueError("empty response")
    with open(destination, "wb") as handle:
        handle.write(payload)


def main():
    try:
        with open(FAILED_LOG, "w", encoding="utf-8"):
            pass
    except OSError as exc:
        print("could not reset failed.log: %s" % exc)

    try:
        breeds = fetch_breeds()
    except FETCH_ERRORS as exc:
        print("SPARQL query failed: %s" % exc)
        return 1

    breeds = [b for b in breeds if b["name_he"]]
    breeds = breeds[:MAX_BREEDS]
    total = len(breeds)
    print("%d breeds with a Hebrew label -- starting downloads" % total)

    facts = known_facts()

    def fact_for(slug, breed):
        """Reuse a fact we already have; otherwise read one off he.wikipedia."""
        if facts.get(slug):
            return facts[slug]
        if not breed["article"]:
            return ""
        try:
            return fetch_fact(breed["article"])
        except FETCH_ERRORS as exc:
            print("  fact lookup failed for %s: %s" % (breed["name_en"], exc))
            return ""

    dataset = []
    for index, breed in enumerate(breeds, start=1):
        name = breed["name_en"]
        slug = slugify(name)
        directory = os.path.join(IMAGES_DIR, slug)

        dropped = trim_extras(directory, IMAGES_PER_BREED)
        present = existing_images(directory)
        if len(present) >= IMAGES_PER_BREED:
            paths = ["images/%s/%s" % (slug, f) for f in present[:IMAGES_PER_BREED]]
            note = " (cached)" if not dropped else " (cached, %d pruned)" % dropped
            print("[%d/%d] %s -- %d images%s" % (index, total, name, len(paths), note))
            dataset.append({
                "slug": slug,
                "name_en": name,
                "name_he": breed["name_he"],
                "images": paths,
                "image_count": len(paths),
                "sitelinks": breed["sitelinks"],
                "fact": fact_for(slug, breed),
                "fact_version": FACT_VERSION,
            })
            continue

        candidates = []
        seen_titles = set()
        failed = False
        if breed["image"]:
            title = file_title(breed["image"])
            if is_image(title):
                seen_titles.add(title)
                candidates.append(thumb_url(breed["image"]))

        if breed["commons"] and len(candidates) < IMAGES_PER_BREED:
            try:
                candidates.extend(category_thumbs(
                    breed["commons"],
                    IMAGES_PER_BREED - len(candidates),
                    seen_titles,
                ))
            except FETCH_ERRORS as exc:
                print("  category lookup failed for %s: %s" % (name, exc))
                failed = True

        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as exc:
            print("[%d/%d] %s -- cannot create directory: %s" % (index, total, name, exc))
            log_failure(name)
            continue

        paths = []
        for url in candidates:
            position = len(paths) + 1
            filename = "%d.jpg" % position
            try:
                download(url, os.path.join(directory, filename))
            except FETCH_ERRORS as exc:
                print("  download failed for %s (%s): %s" % (name, url, exc))
                failed = True
                continue
            paths.append("images/%s/%s" % (slug, filename))
            if len(paths) >= IMAGES_PER_BREED:
                break

        # Whatever ended up in the directory is the truth, including photos a
        # previous run fetched and this one could not reach.
        present = existing_images(directory)
        paths = ["images/%s/%s" % (slug, f) for f in present[:IMAGES_PER_BREED]]

        print("[%d/%d] %s -- %d images" % (index, total, name, len(paths)))
        if failed or not paths:
            log_failure(name)
        if not paths:
            continue

        dataset.append({
            "slug": slug,
            "name_en": name,
            "name_he": breed["name_he"],
            "images": paths,
            "image_count": len(paths),
            "sitelinks": breed["sitelinks"],
            "fact": fact_for(slug, breed),
            "fact_version": FACT_VERSION,
        })

    try:
        with open(BREEDS_JSON, "w", encoding="utf-8") as handle:
            json.dump(dataset, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    except OSError as exc:
        print("could not write breeds.json: %s" % exc)
        return 1

    print("wrote %s with %d breeds" % (os.path.basename(BREEDS_JSON), len(dataset)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
