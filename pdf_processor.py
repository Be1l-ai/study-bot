import re
from collections import Counter, defaultdict

import fitz  # PyMuPDF


# ─────────────────────────────────────────────
# Step 1: Extract raw text blocks per page
# ─────────────────────────────────────────────

def extract_pages(pdf_path: str) -> list[dict]:
    doc = fitz.open(pdf_path)
    pages = []
    for i, page in enumerate(doc):
        blocks = page.get_text("blocks")
        # Sort by vertical position, then horizontal (natural reading order)
        blocks = sorted(blocks, key=lambda b: (round(b[1] / 10), b[0]))
        # Keep only text blocks with meaningful content
        text_blocks = [
            b[4].replace("\n", " ").strip()
            for b in blocks
            if b[6] == 0 and len(b[4].strip()) > 15  # b[6]==0 means text block
        ]
        pages.append({
            "page_num": i,
            "blocks": text_blocks,
            "top": text_blocks[0] if text_blocks else "",
            "bottom": text_blocks[-1] if text_blocks else "",
        })
    doc.close()
    return pages


# ─────────────────────────────────────────────
# Step 2: Identify and remove headers / footers
# Compare top and bottom lines across ALL pages.
# Candidate if a line appears on >=30% of pages.
# Confirm as header/footer only if it appears on
# >=90% of pages in the same edge (top or bottom).
# ─────────────────────────────────────────────

def remove_headers_footers(pages: list[dict]) -> list[dict]:
    total = len(pages)
    if total == 0:
        return pages

    top_counts    = Counter(p["top"]    for p in pages if p["top"])
    bottom_counts = Counter(p["bottom"] for p in pages if p["bottom"])

    candidate_threshold = max(2, total * 0.30)  # appears on 30 %+ of pages
    strict_edge_threshold = max(2, total * 0.90)  # appears on 90 %+ at top OR bottom

    combined_counts = top_counts + bottom_counts
    noise = set()
    for text, count in combined_counts.items():
        top_hits = top_counts.get(text, 0)
        bottom_hits = bottom_counts.get(text, 0)
        if (
            count >= candidate_threshold
            and (top_hits >= strict_edge_threshold or bottom_hits >= strict_edge_threshold)
        ):
            noise.add(text)

    cleaned = []
    for page in pages:
        body = [b for b in page["blocks"] if b not in noise and len(b) > 20]
        if body:
            cleaned.append({"page_num": page["page_num"], "text": " ".join(body)})
    return cleaned


# ─────────────────────────────────────────────
# Step 3: Extract topic markers from a page
# Markers = exact dates (Month DD, YYYY) and
#           place names (2–4 capitalised words,
#           appearing on multiple pages but not
#           on nearly every page).
# Single-word names like "Rizal" or "Spain" that
# appear on almost every page are excluded via
# the frequency filter below.
# ─────────────────────────────────────────────

_DATE_RE = re.compile(
    r'\b(?:January|February|March|April|May|June|July|'
    r'August|September|October|November|December)'
    r'\s+\d{1,2},?\s+\d{4}\b',
    re.IGNORECASE
)
_PLACE_RE = re.compile(
    r'\b[A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,}){0,3}\b'  # 1–4 capitalised words
)

_MAX_MARKER_LINK_GAP = 6
_MAX_CLUSTER_PAGE_GAP = 2
_MAX_TOPIC_CHARS = 2400


def _extract_markers(text: str) -> set[str]:
    dates  = set(_DATE_RE.findall(text))
    places = set(_PLACE_RE.findall(text))
    return dates | places


def _contiguous_runs(cluster_pages: list[dict]) -> list[list[dict]]:
    """Split a cluster into nearby page runs to avoid over-merged topics."""
    if not cluster_pages:
        return []

    sorted_pages = sorted(cluster_pages, key=lambda p: p["page_num"])
    runs: list[list[dict]] = [[sorted_pages[0]]]
    for page in sorted_pages[1:]:
        prev = runs[-1][-1]
        if page["page_num"] - prev["page_num"] <= _MAX_CLUSTER_PAGE_GAP:
            runs[-1].append(page)
        else:
            runs.append([page])
    return runs


def _chunk_run(run_pages: list[dict]) -> list[str]:
    """Chunk long runs so each topic is teachable instead of one giant summary."""
    chunks: list[str] = []
    current_parts: list[str] = []
    current_len = 0

    for page in run_pages:
        text = page["text"].strip()
        if not text:
            continue

        candidate_len = current_len + len(text) + (1 if current_parts else 0)
        if current_parts and candidate_len > _MAX_TOPIC_CHARS:
            combined = " ".join(current_parts).strip()
            if len(combined) > 80:
                chunks.append(combined)
            current_parts = [text]
            current_len = len(text)
        else:
            current_parts.append(text)
            current_len = candidate_len

    if current_parts:
        combined = " ".join(current_parts).strip()
        if len(combined) > 80:
            chunks.append(combined)

    return chunks


def group_into_topics(cleaned_pages: list[dict]) -> list[str]:
    if not cleaned_pages:
        return []

    # Extract markers per page
    page_markers: dict[int, set[str]] = {
        p["page_num"]: _extract_markers(p["text"]) for p in cleaned_pages
    }

    # Count how many pages each marker appears on
    marker_freq: Counter = Counter()
    for markers in page_markers.values():
        marker_freq.update(markers)

    total_pages = len(cleaned_pages)
    # Valid topic markers: appear on 2–70 % of pages
    valid_markers = {
        m for m, cnt in marker_freq.items()
        if 2 <= cnt <= total_pages * 0.70
    }

    # Build a mapping: marker → page indices
    marker_to_pages: dict[str, list[int]] = defaultdict(list)
    for page in cleaned_pages:
        for m in page_markers[page["page_num"]] & valid_markers:
            marker_to_pages[m].append(page["page_num"])

    # Union-Find to cluster pages that share a marker
    parent = {p["page_num"]: p["page_num"] for p in cleaned_pages}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        a, b = find(a), find(b)
        if a != b:
            parent[b] = a

    for pages_sharing in marker_to_pages.values():
        seq = sorted(set(pages_sharing))
        for i in range(1, len(seq)):
            # Link only reasonably close pages so one recurring marker does not
            # collapse the whole document into one giant topic.
            if seq[i] - seq[i - 1] <= _MAX_MARKER_LINK_GAP:
                union(seq[i - 1], seq[i])

    # Group pages by cluster root
    clusters: dict[int, list[dict]] = defaultdict(list)
    for page in cleaned_pages:
        clusters[find(page["page_num"])].append(page)

    # Sort topics by first page number so they arrive in document order.
    # Each cluster is further split into contiguous runs and size-limited chunks.
    topics_with_order = []
    for cluster_pages in clusters.values():
        for run in _contiguous_runs(cluster_pages):
            if not run:
                continue
            run_start = min(p["page_num"] for p in run)
            for chunk in _chunk_run(run):
                topics_with_order.append((run_start, chunk))

    topics_with_order.sort(key=lambda t: t[0])
    return [t for _, t in topics_with_order]


# ─────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────

def process_pdf(pdf_path: str) -> list[str]:
    """Return a list of topic strings extracted from the PDF."""
    pages   = extract_pages(pdf_path)
    cleaned = remove_headers_footers(pages)
    topics  = group_into_topics(cleaned)
    return topics
