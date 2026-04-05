import re
from typing import Any


def _extract_candidate_text(candidate_data: list[Any]) -> str:
    text = ""
    try:
        text = candidate_data[1][0] or ""
    except Exception:
        text = ""
    if re.match(r"^http://googleusercontent\.com/card_content/\d+", text):
        try:
            text = candidate_data[22][0] or text
        except Exception:
            pass
    return re.sub(r"http://googleusercontent\.com/\w+/\d+\n*", "", text)


def _collect_media_urls(data: Any, result: dict[str, list[str]]) -> None:
    if isinstance(data, str):
        if not data.startswith("http"):
            return
        if "usercontent.google.com" in data and ("download" in data or "video" in data.lower()):
            result["video"].append(data)
        elif data.startswith("https://lh3.googleusercontent.com/"):
            result["image"].append(data)
        elif data.startswith("https://encrypted-tbn") or data.startswith("https://www.google.com/imgres"):
            result["web_image"].append(data)
        return

    if isinstance(data, list):
        for item in data:
            _collect_media_urls(item, result)
        return

    if isinstance(data, dict):
        for value in data.values():
            _collect_media_urls(value, result)


def _candidate_score(candidate: dict[str, Any]) -> tuple[int, int, int, int]:
    return (
        len(candidate.get("generated_images", [])) + len(candidate.get("generated_videos", [])),
        len(candidate.get("text", "") or ""),
        len(candidate.get("thoughts", "") or ""),
        len(candidate.get("web_images", [])),
    )


def build_snapshot_from_raw_capture(raw_capture: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(raw_capture, dict):
        return {}

    frames = raw_capture.get("frames")
    if not isinstance(frames, list):
        return {}

    candidates_by_rcid: dict[str, dict[str, Any]] = {}
    metadata: list[Any] = []

    for frame in frames:
        if not isinstance(frame, dict):
            continue
        part_json = frame.get("part_json")
        if not isinstance(part_json, list):
            continue

        if not metadata and isinstance(part_json[1] if len(part_json) > 1 else None, list):
            metadata = part_json[1]

        candidate_list = part_json[4] if len(part_json) > 4 and isinstance(part_json[4], list) else []
        for candidate_data in candidate_list:
            if not isinstance(candidate_data, list):
                continue
            rcid = candidate_data[0] if candidate_data and isinstance(candidate_data[0], str) else ""
            if not rcid:
                continue

            text = _extract_candidate_text(candidate_data)
            thoughts = ""
            try:
                thoughts = candidate_data[37][0][0] or ""
            except Exception:
                thoughts = ""

            url_map: dict[str, list[str]] = {"image": [], "video": [], "web_image": []}
            _collect_media_urls(candidate_data, url_map)

            current = candidates_by_rcid.get(rcid, {"rcid": rcid, "text": "", "thoughts": "", "web_images": [], "generated_images": [], "generated_videos": []})

            if len(text) >= len(current["text"]):
                current["text"] = text
            if len(thoughts) >= len(current["thoughts"]):
                current["thoughts"] = thoughts

            seen_web = {img["url"] for img in current["web_images"]}
            for url in url_map["web_image"]:
                if url not in seen_web:
                    current["web_images"].append({"url": url, "title": "", "alt": ""})
                    seen_web.add(url)

            seen_img = {img["url"] for img in current["generated_images"]}
            for url in url_map["image"]:
                if url not in seen_img:
                    current["generated_images"].append({"url": url, "title": "[Generated Image]", "alt": ""})
                    seen_img.add(url)

            seen_vid = {vid["url"] for vid in current["generated_videos"]}
            for idx, url in enumerate(url_map["video"]):
                if url not in seen_vid:
                    thumb = url_map["image"][idx] if idx < len(url_map["image"]) else ""
                    current["generated_videos"].append({"url": url, "thumbnail_url": thumb, "title": f"[Generated Video {idx + 1}]"})
                    seen_vid.add(url)

            candidates_by_rcid[rcid] = current

    candidates = list(candidates_by_rcid.values())
    chosen_index = 0
    if candidates:
        chosen_index, chosen = max(enumerate(candidates), key=lambda item: _candidate_score(item[1]))
    else:
        chosen = {}
    return {
        "metadata": metadata,
        "chosen": chosen_index,
        "text": chosen.get("text", ""),
        "thoughts": chosen.get("thoughts") or None,
        "rcid": chosen.get("rcid", ""),
        "candidates": candidates,
    }


def collect_generated_image_urls(raw_capture: dict[str, Any] | None) -> list[str]:
    snapshot = build_snapshot_from_raw_capture(raw_capture)
    idx = snapshot.get("chosen", 0)
    chosen = snapshot.get("candidates", [{}])[idx] if snapshot.get("candidates") else {}
    return [img.get("url", "") for img in chosen.get("generated_images", []) if img.get("url")]


def collect_generated_video_urls(raw_capture: dict[str, Any] | None) -> list[str]:
    snapshot = build_snapshot_from_raw_capture(raw_capture)
    idx = snapshot.get("chosen", 0)
    chosen = snapshot.get("candidates", [{}])[idx] if snapshot.get("candidates") else {}
    return [vid.get("url", "") for vid in chosen.get("generated_videos", []) if vid.get("url")]
