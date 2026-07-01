import os
import sys
import zipfile
try:
    # pyrefly: ignore [missing-import]
    import gdown
except ImportError:
    print("gdown is not installed. Please run: pip install gdown")
    sys.exit(1)

# ==============================================================================
# INSTRUCTIONS FOR DEMO / SHOWCASE DEPLOYMENT:
# 1. Zip your 4 `.tif` files into a single file (astranav_data.zip).
# 2. Upload astranav_data.zip to Google Drive.
# 3. Right-click the file in Google Drive -> Share -> "Anyone with the link".
# 4. Copy the link. It will look like this:
#    https://drive.google.com/file/d/1XyZABC...XYZ/view?usp=sharing
# 5. Extract ONLY the ID part (e.g. 1XyZABC...XYZ) and paste it below:
# ==============================================================================

GDRIVE_FILE_ID = "YOUR_FILE_ID_HERE"

def download_and_extract():
    if GDRIVE_FILE_ID == "YOUR_FILE_ID_HERE":
        print("ERROR: You must replace YOUR_FILE_ID_HERE with your actual Google Drive File ID.")
        sys.exit(1)

    print("========================================")
    print(" Downloading Real ISRO Data for Demo")
    print("========================================")

    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    backend_data_dir = os.path.join(root_dir, "backend", "data")
    zip_path = os.path.join(backend_data_dir, "astranav_data.zip")

    # Check if data already exists to avoid re-downloading on every startup
    check_file = os.path.join(backend_data_dir, "optical", "synthetic_ohrc.tif")
    if os.path.exists(check_file):
        print("Data already exists locally. Skipping download.")
        return

    # Download from Google Drive using gdown
    print(f"Downloading from Google Drive (ID: {GDRIVE_FILE_ID})...")
    url = f'https://drive.google.com/uc?id={GDRIVE_FILE_ID}'
    gdown.download(url, zip_path, quiet=False)

    print("Extracting zip file...")
    # Extract the zip file directly into backend/data/
    # This assumes the zip was created directly from the 4 .tif files
    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
        for member in zip_ref.namelist():
            # Extract to correct folders based on filenames
            if "dfsar" in member:
                out_dir = os.path.join(backend_data_dir, "sar")
            elif "dem" in member:
                out_dir = os.path.join(backend_data_dir, "dem")
            elif "temp" in member:
                out_dir = os.path.join(backend_data_dir, "thermal")
            elif "ohrc" in member:
                out_dir = os.path.join(backend_data_dir, "optical")
            else:
                continue

            os.makedirs(out_dir, exist_ok=True)
            # Read from zip and write to correct directory
            filename = os.path.basename(member)
            if not filename:
                continue
                
            out_path = os.path.join(out_dir, filename)
            with zip_ref.open(member) as source, open(out_path, "wb") as target:
                target.write(source.read())
            
            print(f"Extracted: {out_path}")

    # Clean up the zip file to save space
    if os.path.exists(zip_path):
        os.remove(zip_path)
    
    print("\nData download and extraction complete! The server is ready to start.")

if __name__ == "__main__":
    download_and_extract()
