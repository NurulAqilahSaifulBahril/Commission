import pypdf

pdf_path = r"6. Presentation/Finance Output/Commission_2026_20260623_163223.pdf"
try:
    reader = pypdf.PdfReader(pdf_path)
    print(f"Num pages: {len(reader.pages)}")
    for i in range(8):
        page = reader.pages[i]
        text = page.extract_text()
        print(f"--- Page {i+1} ---")
        print(text)
except Exception as e:
    print(f"Error: {e}")
