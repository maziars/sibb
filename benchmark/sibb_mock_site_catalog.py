"""WebMall-backed product catalog for SIBB mock-shop generators.

Loads `sibb/benchmark/data/webmall_1.csv` — a 1152-SKU snapshot of the
WebMall benchmark catalog (research-permissive, attribution in
`sibb/benchmark/data/NOTICE.txt`) — and exposes a small API for shop
task generators:

    cat = load_catalog()
    winner = cat.find_one(rng, category="PC Peripherals",
                          max_price_cents=8000)
    distractors = cat.sample_distractors(rng, winner, n=7,
                                          max_price_cents=8000)

Design choices (calibrated by `sibb_runs/shopping_mock_site_realism.md`):

* **Catalog is bundled and read once at import.** The CSV is ~1.8 MB
  and the row schema is stable; parsing per-episode would add
  ~50 ms hot-path cost for no upside. The module-level singleton is
  populated lazily on first `load_catalog()` call to keep test
  import time fast.

* **Price is in cents (int).** Source rows are German-locale decimal
  comma (`"139,99"`); we parse once and store as int cents — avoids
  float comparison headaches in the verifier and matches how
  MockSite submission fields are tested elsewhere.

* **Brand is extracted from the category path.** WebMall's
  `Categories` column is hierarchical (`"Motherboard > Asus"`,
  `"Peripherals > Keyboard > Trust"`). The last segment is almost
  always the brand. Falls back to the first word of the title when
  the path has no brand-shaped leaf.

* **HTML in descriptions is stripped to plain text.** Rendering pages
  uses the cleaned-up text; the original HTML is kept on the dataclass
  too in case we want to render rich PDP content later.

* **Sampling is RNG-driven.** Every public method takes an `rng:
  random.Random` so the catalog state is identical across runs of a
  given seed. No global random state.

* **No size / color in the source data.** WebMall is a PC-components
  catalog; SKUs don't have size/color variants. Shop generators that
  want size/color attributes synthesize them on top of these SKUs
  per-seed (e.g., layer `["red","blue","black"]` color choices over a
  given keyboard SKU). That keeps the catalog module narrowly scoped
  to "what does WebMall give us" without leaking generator concerns.
"""
from __future__ import annotations

import csv
import os
import random
import re
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple


# ─────────────────────────────────────────────────────────────────────────────
#  Constants
# ─────────────────────────────────────────────────────────────────────────────

_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_CSV_PATH = os.path.join(_DATA_DIR, "webmall_1.csv")

# Global-Category values present in webmall_1.csv (verified at import time).
_KNOWN_GLOBAL_CATEGORIES = {
    "PC Components",
    "PC Peripherals",
    "Other Electronics",
}


# ─────────────────────────────────────────────────────────────────────────────
#  Public types
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SKU:
    """One row from the WebMall catalog, parsed into the fields shop
    generators actually need. Frozen so it can be hashed for use in
    sets / dict keys (e.g. "already-sampled" deduplication).
    """
    sku_id: str               # `"wm-1546"` — stable; usable as a slug in URL paths
    name: str                 # `"Asus PRIME B650M-A WIFI II, AMD B650, AM5, ..."`
    brand: str                # `"Asus"` — extracted from the category path
    global_category: str      # `"PC Components"`
    category_path: str        # `"Motherboard > Asus"`
    short_description: str    # 1-2 sentence prose, HTML stripped
    price_cents: int          # `13999` for €139.99
    in_stock: bool


# ─────────────────────────────────────────────────────────────────────────────
#  Loader
# ─────────────────────────────────────────────────────────────────────────────

_PRICE_RE = re.compile(r"^\s*(\d+)(?:[,.](\d{1,2}))?\s*$")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _bm25_tokenize(s: str) -> List[str]:
    """Lowercase + extract alphanumeric tokens. Drops punctuation,
    quotes, currency symbols. Used for both the BM25 corpus build and
    every search query so the two sides agree on tokenization."""
    if not s:
        return []
    return _TOKEN_RE.findall(s.lower())


def _parse_price_cents(s: str) -> Optional[int]:
    """Parse WebMall's German-locale price `"139,99"` (or `"139"`) to
    integer cents. Returns None on empty / malformed input.
    """
    if not s or not s.strip():
        return None
    m = _PRICE_RE.match(s)
    if not m:
        return None
    whole = int(m.group(1))
    frac = m.group(2)
    if frac is None:
        return whole * 100
    if len(frac) == 1:
        frac = frac + "0"
    return whole * 100 + int(frac)


def _strip_html(s: str, *, max_chars: int = 300) -> str:
    """Strip HTML tags from a WebMall description, collapse whitespace,
    truncate to `max_chars`. Empty input → empty string.
    """
    if not s:
        return ""
    text = _HTML_TAG_RE.sub(" ", s)
    text = " ".join(text.split())
    return text[:max_chars]


def _extract_brand(category_path: str, fallback_name: str) -> str:
    """Extract brand from a WebMall `Categories` path. Empirically the
    LAST segment is the brand at every depth observed in the corpus:

      * 1-deep `"Kingston"` → brand `Kingston`
      * 2-deep `"Motherboard > Asus"` → brand `Asus`
      * 3-deep `"Peripherals > Keyboard > Trust"` → brand `Trust`

    The CSV's `Brands` column is always empty so this path is the only
    reliable source. Falls back to the first whitespace-delimited token
    of the product name only when the path is completely empty.
    """
    parts = [p.strip() for p in category_path.split(">") if p.strip()]
    if parts:
        return parts[-1]
    if not fallback_name:
        return "Unknown"
    return fallback_name.split()[0].rstrip(",")


# ─────────────────────────────────────────────────────────────────────────────
#  Catalog
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Catalog:
    """Indexed view of the WebMall SKUs. Public surface — the loader
    populates this once and shop generators hold a reference."""

    skus: List[SKU]
    by_global_category: Dict[str, List[SKU]] = field(default_factory=dict)
    by_brand: Dict[str, List[SKU]] = field(default_factory=dict)
    # Lazy BM25 index over the title+brand+description corpus. Built
    # on first `bm25_search()` call so test imports that don't touch
    # search pay zero cost.
    _bm25: Any = None
    _bm25_corpus_tokens: Optional[List[List[str]]] = None

    # ── inspection ────────────────────────────────────────────────────

    def __len__(self) -> int:
        return len(self.skus)

    def categories(self) -> List[str]:
        """Sorted list of distinct global-category strings."""
        return sorted(self.by_global_category.keys())

    def brands(self) -> List[str]:
        """Sorted list of distinct brand strings."""
        return sorted(self.by_brand.keys())

    # ── filtering ─────────────────────────────────────────────────────

    def filter(self, *,
                category: Optional[str] = None,
                brand: Optional[str] = None,
                min_price_cents: Optional[int] = None,
                max_price_cents: Optional[int] = None,
                in_stock_only: bool = True) -> List[SKU]:
        """Return all SKUs matching the constraints, in catalog order.
        Empty list when nothing matches. Used by both generators (to
        pick a winner) and verifiers (to confirm the "right answer" is
        unique under the constraint set)."""
        out: List[SKU] = []
        for s in self.skus:
            if in_stock_only and not s.in_stock:
                continue
            if category is not None and s.global_category != category:
                continue
            if brand is not None and s.brand != brand:
                continue
            if min_price_cents is not None and s.price_cents < min_price_cents:
                continue
            if max_price_cents is not None and s.price_cents > max_price_cents:
                continue
            out.append(s)
        return out

    # ── sampling ──────────────────────────────────────────────────────

    def find_one(self, rng: random.Random, **filters) -> SKU:
        """Pick a single SKU matching the filters. Deterministic per
        rng. Raises ValueError if nothing matches — the caller's
        constraints are too tight."""
        candidates = self.filter(**filters)
        if not candidates:
            raise ValueError(
                f"Catalog.find_one: no SKU matches {filters!r}")
        return rng.choice(candidates)

    def sample_distractors(self, rng: random.Random, winner: SKU, n: int,
                            *, in_stock_only: bool = True,
                            **filters) -> List[SKU]:
        """Pick N distractors — SKUs that satisfy `filters` but are NOT
        the winner. Useful for building a search-results page where the
        winner is one of K matching candidates and the others look
        plausible but differ on some attribute.

        Raises ValueError if fewer than N distinct distractors are
        available; caller should loosen filters."""
        pool = self.filter(in_stock_only=in_stock_only, **filters)
        pool = [s for s in pool if s.sku_id != winner.sku_id]
        if len(pool) < n:
            raise ValueError(
                f"Catalog.sample_distractors: only {len(pool)} distractors "
                f"available for {filters!r} (asked for {n})")
        return rng.sample(pool, n)

    # ── Cheapest-winner eligibility (Step 5N) ────────────────────────

    _eligible_cheapest_cache: Any = None

    def eligible_cheapest_winners(
            self, price_caps: Tuple[int, ...]
            ) -> List[Tuple[SKU, int]]:
        """Return all `(sku, cap)` pairs where `sku` is the UNIQUE
        cheapest in-budget item among its (brand, category) family
        under the smallest cap that captures 2+ such items. Used by
        `gen_safari_shop_pick_by_attrs` to pick a winner with a
        canonical "cheapest" answer.

        Cached per Catalog instance — the result depends only on the
        catalog snapshot and the cap tuple. Recomputation is
        ~6.5M ops at ~250 ms; called once and cached forever.
        """
        if (self._eligible_cheapest_cache is not None
                and self._eligible_cheapest_cache[0] == price_caps):
            return self._eligible_cheapest_cache[1]
        out: List[Tuple[SKU, int]] = []
        for sku in self.skus:
            if not sku.in_stock:
                continue
            for cap in price_caps:
                if sku.price_cents > cap:
                    continue
                siblings = self.filter(
                    brand=sku.brand,
                    category=sku.global_category,
                    max_price_cents=cap)
                if len(siblings) < 2:
                    continue
                min_price = min(s.price_cents for s in siblings)
                if sku.price_cents != min_price:
                    continue
                if sum(1 for s in siblings
                        if s.price_cents == min_price) > 1:
                    continue
                # Solvability gate (Step 5N): a Q1-archetype agent
                # will likely search by the BRAND alone; ensure the
                # winner shows up in the top 8 BM25 results for that
                # query so the task is at least achievable.
                hits = self.bm25_search(sku.brand, n=8)
                if not any(h.sku_id == sku.sku_id for h in hits):
                    continue
                out.append((sku, cap))
                break  # smallest-cap is enough
        self._eligible_cheapest_cache = (price_caps, out)
        return out

    # ── BM25 search (Step 5N, 2026-06-08) ────────────────────────────

    def _ensure_bm25(self) -> None:
        if self._bm25 is not None:
            return
        from rank_bm25 import BM25Okapi
        self._bm25_corpus_tokens = [
            _bm25_tokenize(
                f"{s.name} {s.brand} {s.short_description}")
            for s in self.skus
        ]
        self._bm25 = BM25Okapi(self._bm25_corpus_tokens)

    def bm25_search(self, query: str, *, n: int = 8,
                     in_stock_only: bool = True) -> List[SKU]:
        """Return the top-N SKUs by BM25 relevance against the
        title+brand+description corpus. Empty/whitespace query → empty
        list (templates render an "empty results" state). Ties broken
        by catalog order so the result is deterministic per query."""
        q_tokens = _bm25_tokenize(query)
        if not q_tokens:
            return []
        self._ensure_bm25()
        scores = self._bm25.get_scores(q_tokens)
        # Index pairs sorted by (-score, catalog_order) → deterministic.
        ranked = sorted(
            enumerate(scores), key=lambda kv: (-kv[1], kv[0]))
        out: List[SKU] = []
        for idx, score in ranked:
            if score <= 0:
                break
            sku = self.skus[idx]
            if in_stock_only and not sku.in_stock:
                continue
            out.append(sku)
            if len(out) >= n:
                break
        return out

    def sample_mixed(self, rng: random.Random, n: int,
                      *, category: Optional[str] = None,
                      in_stock_only: bool = True) -> List[SKU]:
        """Pick N totally random SKUs (within optional category).
        Useful for filling a results grid with "unrelated" filler
        products that aren't tied to the winner's attribute space."""
        pool = self.filter(category=category, in_stock_only=in_stock_only)
        if len(pool) < n:
            raise ValueError(
                f"Catalog.sample_mixed: only {len(pool)} SKUs in "
                f"category={category!r} (asked for {n})")
        return rng.sample(pool, n)


# ─────────────────────────────────────────────────────────────────────────────
#  Module-level singleton
# ─────────────────────────────────────────────────────────────────────────────

_catalog_lock = threading.Lock()
_cached_catalog: Optional[Catalog] = None


def load_catalog(*, force_reload: bool = False) -> Catalog:
    """Return the singleton Catalog. Loads from disk on first call;
    subsequent calls return the cached instance. Thread-safe.

    `force_reload=True` re-reads the CSV — used by tests that mutate
    the catalog or want a fresh copy."""
    global _cached_catalog
    with _catalog_lock:
        if _cached_catalog is None or force_reload:
            _cached_catalog = _load_from_disk()
        return _cached_catalog


def _load_from_disk() -> Catalog:
    if not os.path.exists(_CSV_PATH):
        raise RuntimeError(
            f"WebMall catalog CSV missing at {_CSV_PATH!r}. The bundled "
            f"file should be checked in under sibb/benchmark/data/. See "
            f"NOTICE.txt for source attribution.")
    skus: List[SKU] = []
    seen_ids: Set[str] = set()
    with open(_CSV_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            sid = row.get("ID", "").strip()
            name = row.get("Name", "").strip()
            if not sid or not name:
                continue
            price = _parse_price_cents(row.get("Regular price", ""))
            if price is None or price <= 0:
                # Drop rows without a usable price — the shop can't list
                # them as buyable. WebMall has a handful of $0 / blank
                # rows; skipping them keeps the catalog clean.
                continue
            cat_path = row.get("Categories", "").strip()
            global_cat = row.get("Global Category", "").strip()
            if global_cat not in _KNOWN_GLOBAL_CATEGORIES:
                # Defensive: an unexpected category value would break
                # the by_global_category index. Skip rather than crash;
                # the load is best-effort.
                continue
            brand = _extract_brand(cat_path, name)
            short_desc = _strip_html(row.get("Description", ""))
            in_stock = (row.get("In stock?", "0").strip() == "1")
            sku_id = f"wm-{sid}"
            if sku_id in seen_ids:
                continue
            seen_ids.add(sku_id)
            skus.append(SKU(
                sku_id=sku_id,
                name=name,
                brand=brand,
                global_category=global_cat,
                category_path=cat_path,
                short_description=short_desc,
                price_cents=price,
                in_stock=in_stock,
            ))

    if not skus:
        raise RuntimeError(
            "WebMall catalog loaded zero usable SKUs. The bundled CSV "
            "schema may have drifted from the loader's expectations.")

    cat = Catalog(skus=skus)
    for s in skus:
        cat.by_global_category.setdefault(s.global_category, []).append(s)
        cat.by_brand.setdefault(s.brand, []).append(s)
    return cat
