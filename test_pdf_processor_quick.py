import unittest

from pdf_processor import remove_headers_footers


class TestPdfProcessorQuick(unittest.TestCase):
    def test_removes_true_header_with_90_percent_edge_repetition(self):
        pages = []
        for i in range(10):
            top = "GLOBAL HEADER TEXT REPEATED ACROSS PAGES"
            if i == 9:
                top = "A DIFFERENT TOP LINE FOR LAST PAGE"
            pages.append(
                {
                    "page_num": i,
                    "blocks": [
                        top,
                        f"Main body paragraph content on page {i} with enough length.",
                        "Common footer line not repeated enough.",
                    ],
                    "top": top,
                    "bottom": "Common footer line not repeated enough.",
                }
            )

        cleaned = remove_headers_footers(pages)
        self.assertEqual(len(cleaned), 10)

        for page in cleaned:
            self.assertNotIn("GLOBAL HEADER TEXT REPEATED ACROSS PAGES", page["text"])

    def test_keeps_repeated_title_when_below_90_percent_edge_repetition(self):
        pages = []
        repeated_title = "CHAPTER TITLE THAT REPEATS BUT IS NOT A TRUE HEADER"

        for i in range(10):
            top = repeated_title if i < 4 else f"Unique top line for page {i}"
            pages.append(
                {
                    "page_num": i,
                    "blocks": [
                        top,
                        f"Core lesson text for page {i} that should be preserved.",
                        f"Unique footer line for page {i}",
                    ],
                    "top": top,
                    "bottom": f"Unique footer line for page {i}",
                }
            )

        cleaned = remove_headers_footers(pages)
        self.assertEqual(len(cleaned), 10)

        pages_with_title = [p for p in cleaned if repeated_title in p["text"]]
        self.assertEqual(len(pages_with_title), 4)


if __name__ == "__main__":
    unittest.main()
