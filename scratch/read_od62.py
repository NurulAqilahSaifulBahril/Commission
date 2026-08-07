import pypdf
import sys

def main():
    pdf_path = r"1. Basic Commission/1. Excel/OD 62 25 Jun 2026 KC-Commission Adjustment and Commission Payment Mechanism Optimization.pdf"
    txt_path = r"scratch/od62_text.txt"
    print(f"Reading PDF: {pdf_path}")
    try:
        reader = pypdf.PdfReader(pdf_path)
        print(f"Total Pages: {len(reader.pages)}")
        with open(txt_path, "w", encoding="utf-8") as f:
            for idx, page in enumerate(reader.pages):
                f.write(f"\n--- Page {idx + 1} ---\n")
                f.write(page.extract_text() or "")
        print(f"Extracted text successfully written to {txt_path}")
    except Exception as e:
        print(f"Error reading PDF: {e}")

if __name__ == "__main__":
    main()
