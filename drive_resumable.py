"""Bounded resumable retries, with server offset recovery after uncertain PUTs."""
import re
import time
import requests
from drive_transfer_control import check_cancelled
from drive_upload_errors import DriveUploadError


def upload_chunk(session, url, part, offset, total, mime):
    end = offset + len(part)
    cursor = offset

    def completed(response):
        data = response.json()
        file_id = data.get("id")
        if not isinstance(file_id, str) or not re.fullmatch(r"[\w-]+", file_id):
            raise DriveUploadError("google_unavailable")
        # Build a known, clean Drive URL rather than returning arbitrary JSON URLs.
        return {"link": f"https://drive.google.com/file/d/{file_id}/view"}

    for attempt in range(4):
        check_cancelled()
        uncertain = False
        try:
            with session.put(url, data=part[cursor-offset:], headers={
                "Content-Type": mime, "Content-Range": f"bytes {cursor}-{end-1}/{total}",
            }, timeout=(15, 90), allow_redirects=False) as response:
                status = response.status_code
                if status in {200, 201} and end == total:
                    return completed(response)
                if status == 308 and end < total:
                    if response.headers.get("Range") != f"bytes=0-{end-1}":
                        raise ValueError("Google did not acknowledge the complete chunk.")
                    return {"received": end}
                if status in {401, 403}:
                    # Resumable URIs are self-authorizing. Never resend a rejected
                    # chunk with unrelated credentials or create a duplicate file.
                    try:
                        payload = response.json()
                    except ValueError:
                        payload = {}
                    reasons = [entry.get("reason") for entry in payload.get("error", {}).get("errors", [])]
                    raise DriveUploadError("google_quota" if "storageQuotaExceeded" in reasons else
                                           "google_reconnect" if status == 401 else "google_permission")
                uncertain = status == 429 or status >= 500
                if not uncertain:
                    raise DriveUploadError("google_unavailable")
        except (requests.Timeout, requests.ConnectionError):
            uncertain = True
        if uncertain:
            for _ in range(min(16, 2 ** attempt * 4)):
                check_cancelled()
                time.sleep(.25)
            # The failed PUT might have been committed. Ask Drive before retrying
            # so no bytes are duplicated and partially accepted chunks can resume.
            try:
                with session.put(url, data=b"", headers={"Content-Range": f"bytes */{total}"},
                                 timeout=(15, 90), allow_redirects=False) as probe:
                    if probe.status_code in {200, 201} and end == total:
                        return completed(probe)
                    if probe.status_code == 308:
                        acknowledged = probe.headers.get("Range")
                        match = re.fullmatch(r"bytes=0-(\d+)", acknowledged or "")
                        if acknowledged and not match:
                            raise DriveUploadError("google_unavailable")
                        received = int(match[1]) + 1 if match else 0
                        if not offset <= received <= end:
                            raise DriveUploadError("google_unavailable")
                        if received == end and end < total:
                            return {"received": end}
                        if received == end:
                            raise DriveUploadError("google_unavailable")
                        cursor = received
                    elif probe.status_code in {401, 403, 404, 410}:
                        raise DriveUploadError("google_reconnect" if probe.status_code == 401 else "google_permission")
            except (requests.Timeout, requests.ConnectionError):
                pass
    raise DriveUploadError("google_unavailable")
