import sys

try:
    import pypdf
    print("pypdf available")
    reader = pypdf.PdfReader(r"6. Presentation/5. Internal Commission_Pack.rev6.pdf")
    print(f"Num pages: {len(reader.pages)}")
    for i, page in enumerate(reader.pages):
        text = page.extract_text()
        if "Note" in text or "note" in text or "Reconciliation" in text:
            print(f"--- Page {i+1} ---")
            print(text)
except Exception as e:
    print(f"pypdf error: {e}")

try:
    import pdfplumber
    print("pdfplumber available")
    with pdfplumber.open(r"6. Presentation/5. Internal Commission_Pack.rev6.pdf") as pdf:
        for i, page in enumerate(pdf.pages):
            text = page.extract_text()
            if "Note" in text or "note" in text:
                print(f"--- Page {i+1} ---")
                print(text)
except Exception as e:
    print(f"pdfplumber error: {e}")
