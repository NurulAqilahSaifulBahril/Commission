import pypdf

pdf_path = r"Commission.pdf"
try:
    reader = pypdf.PdfReader(pdf_path)
    print(f"Num pages: {len(reader.pages)}")
    for i in range(min(15, len(reader.pages))):
        page = reader.pages[i]
        text = page.extract_text()
        if "Executive -" in text or "OUM -" in text or "Rate %" in text:
            print(f"--- Page {i+1} ---")
            print(text)
except Exception as e:
    print(f"Error: {e}")
