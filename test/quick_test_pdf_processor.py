import unittest

from pdf_processor import group_into_topics, remove_headers_footers


class TestPdfProcessorHeaderFooterRules(unittest.TestCase):
    def test_empty_pages_returns_empty(self):
        cleaned = remove_headers_footers([])
        self.assertEqual(cleaned, [])

    def test_two_pages_threshold_still_removes_consistent_edges(self):
        pages = []
        for i in range(2):
            top = "Tiny Doc Header"
            bottom = "Tiny Doc Footer"
            body = f"Small doc body page {i} with enough text to keep."
            pages.append({
                "page_num": i,
                "blocks": [top, body, bottom],
                "top": top,
                "bottom": bottom,
            })

        cleaned = remove_headers_footers(pages)
        self.assertEqual(len(cleaned), 2)
        for page in cleaned:
            self.assertNotIn("Tiny Doc Header", page["text"])
            self.assertNotIn("Tiny Doc Footer", page["text"])

    def test_exact_90_percent_top_repetition_is_removed(self):
        pages = []
        top_header = "Ninety Percent Header"
        bottom = "Unique Bottom"

        for i in range(10):
            top = top_header if i < 9 else "Different Header On Last Page"
            body = f"Body content page {i} with enough text length for filter."
            pages.append({
                "page_num": i,
                "blocks": [top, body, f"{bottom} {i}"],
                "top": top,
                "bottom": f"{bottom} {i}",
            })

        cleaned = remove_headers_footers(pages)
        self.assertEqual(len(cleaned), 10)
        for i, page in enumerate(cleaned):
            if i < 9:
                self.assertNotIn(top_header, page["text"])
            else:
                self.assertIn("Different Header On Last Page", page["text"])

    def test_page_dropped_when_only_noise_remains(self):
        pages = [
            {
                "page_num": 0,
                "blocks": [
                    "Always Header",
                    "short",
                    "Always Footer",
                ],
                "top": "Always Header",
                "bottom": "Always Footer",
            },
            {
                "page_num": 1,
                "blocks": [
                    "Always Header",
                    "This body line is long enough to keep after cleaning.",
                    "Always Footer",
                ],
                "top": "Always Header",
                "bottom": "Always Footer",
            },
        ]

        cleaned = remove_headers_footers(pages)
        # Page 0 loses header/footer and short body, so it gets dropped.
        self.assertEqual(len(cleaned), 1)
        self.assertEqual(cleaned[0]["page_num"], 1)

    def test_header_footer_removed_but_title_kept(self):
        pages = []
        for i in range(10):
            top = "Document Header Repeated On Every Page"
            bottom = "Document Footer Repeated On Every Page"
            title = "Chapter One Title That Repeats Sometimes"
            body = f"Main body content for page {i} with enough descriptive text."
            blocks = [top, title, body, bottom]
            pages.append({"page_num": i, "blocks": blocks, "top": top, "bottom": bottom})

        cleaned = remove_headers_footers(pages)

        self.assertEqual(len(cleaned), 10)
        for page in cleaned:
            text = page["text"]
            self.assertNotIn("Document Header Repeated On Every Page", text)
            self.assertNotIn("Document Footer Repeated On Every Page", text)
            self.assertIn("Chapter One Title That Repeats Sometimes", text)

    def test_repeated_title_below_90_percent_edge_is_not_removed(self):
        pages = []
        repeated_title = "Repeated Section Title Appears On Many Pages"
        footer = "Consistent Footer Line Across All Pages"

        for i in range(10):
            if i < 8:
                top = repeated_title  # 80% top repetition, should NOT be removed
                blocks = [top, f"Body text page {i} with enough useful details.", footer]
            else:
                top = f"Unique Top Line For Page {i}"
                blocks = [top, repeated_title, f"Body text page {i} with enough useful details.", footer]

            pages.append({"page_num": i, "blocks": blocks, "top": top, "bottom": footer})

        cleaned = remove_headers_footers(pages)

        self.assertEqual(len(cleaned), 10)
        for page in cleaned:
            text = page["text"]
            self.assertNotIn("Consistent Footer Line Across All Pages", text)
            self.assertIn(repeated_title, text)


class TestPdfTopicGrouping(unittest.TestCase):
    def test_same_marker_far_apart_pages_do_not_collapse_to_one_topic(self):
        cleaned_pages = []

        for i in range(4):
            cleaned_pages.append({
                "page_num": i,
                "text": (
                    "June 19, 1861 event details in Manila. "
                    "This paragraph contains enough explanatory material for testing."
                ),
            })

        for i in range(15, 19):
            cleaned_pages.append({
                "page_num": i,
                "text": (
                    "June 19, 1861 event details in Manila. "
                    "Another distant section with enough explanatory material for testing."
                ),
            })

        topics = group_into_topics(cleaned_pages)
        self.assertGreaterEqual(len(topics), 2)

    def test_long_contiguous_run_is_split_into_multiple_topics(self):
        cleaned_pages = []
        long_piece = (
            "Cebu City chapter details and timeline with names and events "
            "for exam preparation and understanding. "
        ) * 30

        for i in range(8):
            cleaned_pages.append({
                "page_num": i,
                "text": f"June 19, 1861 {long_piece}",
            })

        topics = group_into_topics(cleaned_pages)
        self.assertGreaterEqual(len(topics), 2)


if __name__ == "__main__":
    unittest.main()
