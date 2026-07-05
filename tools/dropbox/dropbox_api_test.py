"""Manual integration test for dropbox_api — run directly to verify live API access."""

from tools.dropbox.dropbox_api import create_participant_folder, get_file_request

TEST_PARTICIPANT_ID = "TEST_001"

result = create_participant_folder(TEST_PARTICIPANT_ID)
print("folder_path:      ", result["folder_path"])
print("file_request_url: ", result["file_request_url"])
print("file_request_id:  ", result["file_request_id"])

info = get_file_request(result["file_request_id"])
print("is_open:          ", info["is_open"])
print("file_count:       ", info["file_count"])
