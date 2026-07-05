"""Manual integration test for dropbox_uploader — run directly to verify live uploads."""

import tempfile
from pathlib import Path

from tools.dropbox.dropbox_uploader import upload_file, upload_participant_kit

TEST_PARTICIPANT_ID = "TEST_001"

# --- upload_file: upload a small temp file to an explicit Dropbox path ---
with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
    f.write(b"RepairAudit uploader test payload")
    tmp_path = Path(f.name)

dropbox_path = upload_file(tmp_path, f"/RepairAudit/participants/{TEST_PARTICIPANT_ID}/test_upload.txt")
print("upload_file result:          ", dropbox_path)
tmp_path.unlink()

# --- upload_participant_kit: upload a small fake zip to the participant folder ---
with tempfile.NamedTemporaryFile(suffix=".zip", prefix=f"participant_kit_pilot_{TEST_PARTICIPANT_ID}_", delete=False) as f:
    f.write(b"PK\x03\x04")  # minimal zip magic bytes
    zip_path = Path(f.name)

dropbox_kit_path = upload_participant_kit(zip_path, TEST_PARTICIPANT_ID)
print("upload_participant_kit result:", dropbox_kit_path)
zip_path.unlink()
