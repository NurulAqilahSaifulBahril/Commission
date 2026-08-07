import pypdf

pdf_path = r"Commission.pdf"
try:
    reader = pypdf.PdfReader(pdf_path)
    print(f"Num pages: {len(reader.pages)}")
    found_zulkarnain = False
    found_zul = False
    for i, page in enumerate(reader.pages):
        text = page.extract_text()
        if "Zulkarnain" in text:
            print(f"Found 'Zulkarnain' on Page {i+1}:")
            found_zulkarnain = True
            lines = text.split("\n")
            for line in lines:
                if "Zulkarnain" in line:
                    print(f"  {line}")
        if "Zul" in text and "Zulkarnain" not in text:
            # Check if standalone "Zul" is found (excluding words like "Zulkarnain" or "Zulhafiz")
            import re
            matches = re.findall(r'\bZul\b', text)
            if matches:
                print(f"Found standalone 'Zul' on Page {i+1}:")
                found_zul = True
                lines = text.split("\n")
                for line in lines:
                    if re.search(r'\bZul\b', line):
                        print(f"  {line}")
                        
    if not found_zulkarnain:
        print("Zulkarnain NOT found in the PDF.")
    if not found_zul:
        print("Standalone 'Zul' NOT found in the PDF.")
except Exception as e:
    print(f"Error: {e}")
