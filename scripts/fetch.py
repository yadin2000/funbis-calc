#!/usr/bin/env python3
"""Build a local dog-breed dataset (photos + Hebrew names) for the quiz game.

Source of truth is Wikidata: every item that is an instance of "dog breed"
(Q39367), ordered by sitelink count so the best-documented breeds come first.
Images come from the curated P18 picture plus the breed's Commons category.

Run it as many times as you like -- breeds that already have a full set of
images on disk are skipped without touching the network.
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

MAX_BREEDS = 100
IMAGES_PER_BREED = 5
REQUEST_DELAY = 0.5
TIMEOUT = 60

CONTACT = "dog-breed-quiz/1.0 (https://github.com/yadin2000/funbis-calc; contact: f488yt79sn@privaterelay.appleid.com)"
USER_AGENT = CONTACT

SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"

OUTPUT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMAGES_DIR = os.path.join(OUTPUT_DIR, "images")
BREEDS_JSON = os.path.join(OUTPUT_DIR, "breeds.json")
FAILED_LOG = os.path.join(OUTPUT_DIR, "failed.log")

SKIP_EXTENSIONS = (".svg", ".ogv", ".webm", ".gif")

# Anything a flaky network or a surprising response body can throw at us.
# urllib.error.URLError and HTTPError are both OSError subclasses.
FETCH_ERRORS = (OSError, http.client.HTTPException, ValueError, KeyError)

QUERY = """
SELECT ?breed ?nameEn ?nameHe ?image ?commons ?sitelinks WHERE {
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


def request(url, headers=None, binary=False):
    """One HTTP GET, always followed by the courtesy delay."""
    all_headers = {"User-Agent": USER_AGENT}
    if headers:
        all_headers.update(headers)
    req = urllib.request.Request(url, headers=all_headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
            payload = response.read()
    finally:
        time.sleep(REQUEST_DELAY)
    if binary:
        return payload
    return payload.decode("utf-8")


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
    try:
        return sorted(f for f in os.listdir(directory) if not f.startswith("."))
    except OSError:
        return []


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

    dataset = []
    for index, breed in enumerate(breeds, start=1):
        name = breed["name_en"]
        slug = slugify(name)
        directory = os.path.join(IMAGES_DIR, slug)

        present = existing_images(directory)
        if len(present) >= IMAGES_PER_BREED:
            paths = ["images/%s/%s" % (slug, f) for f in present[:IMAGES_PER_BREED]]
            print("[%d/%d] %s -- %d images (cached)" % (index, total, name, len(paths)))
            dataset.append({
                "slug": slug,
                "name_en": name,
                "name_he": breed["name_he"],
                "images": paths,
                "image_count": len(paths),
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
