import pypdf
import sys

pdf_path = r"6. Presentation/Finance Output/Commission_2026_20260623_163223.pdf"
try:
    reader = pypdf.PdfReader(pdf_path)
    print(f"Num pages: {len(reader.pages)}")
    for i, page in enumerate(reader.pages):
        text = page.extract_text()
        print(f"--- Page {i+1} ---")
        lines = text.split("\n")
        # Print first 20 lines and any notes
        for line in lines[:30]:
            print(line)
        for line in lines[30:]:
            if "%" in line or "Note" in line or "note" in line or "Executive" in line or "Senior" in line or "OUM" in line or "OSA" in line:
                print(line)
except Exception as e:
    print(f"Error: {e}")
