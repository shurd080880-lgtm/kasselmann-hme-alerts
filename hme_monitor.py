import json
import os
import time
import urllib.request
import uuid
from pathlib import Path

HME_URL = "https://hme-live2-leaderboard.azurewebsites.net/api/cib/lbdata/?storeUID=C261A68011D84960B705E846BC287752"
ONESIGNAL_URL = "https://api.onesignal.com/notifications?c=push"
APP_ID = "48b2684c-c9e3-466c-afe5-24779f7b2096"
THRESHOLD = 250
REQUIRED_SECONDS = 5 * 60
CHECK_INTERVAL_SECONDS = 60
STATE_FILE = Path("hme-alert-state.json")

# Each phone stores all 8 location choices in only two OneSignal tags.
STORE_TARGETS = {
    "Pendleton-Kasselmann": ("hme_group_a", 1),
    "Eminence - Kasselmann": ("hme_group_a", 2),
    "LaGrange -Kasselmann": ("hme_group_a", 4),
    "Hanover- Kasselmann": ("hme_group_a", 8),
    "Madison- Kasselmann": ("hme_group_b", 1),
    "Clarksville-Kasselmann": ("hme_group_b", 2),
    "Buckner-Kasselmann": ("hme_group_b", 4),
    "Veterans-Kasselmann": ("hme_group_b", 8),
}


def http_json(url, headers=None, method="GET", body=None):
    req = urllib.request.Request(url, headers=headers or {}, method=method)
    if body is not None:
        req.data = json.dumps(body).encode("utf-8")
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def load_state():
    if not STATE_FILE.exists():
        return {"alerted": {}, "above_since": {}}
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        state.setdefault("alerted", {})
        state.setdefault("above_since", {})
        return state
    except Exception:
        return {"alerted": {}, "above_since": {}}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def walk_dicts(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_dicts(child)


def find_current_hour_average(store_obj):
    for obj in walk_dicts(store_obj):
        bucket = str(obj.get("TimeBucketType", "")).strip().lower()
        avg = obj.get("AverageTimeInSec")
        if bucket == "currenthour" and isinstance(avg, (int, float)):
            return float(avg)
    return None


def extract_store_readings(payload):
    readings = {}
    seen_store_objects = set()
    for obj in walk_dicts(payload):
        store_name = obj.get("StoreName") or obj.get("storeName")
        if not store_name:
            continue
        marker = id(obj)
        if marker in seen_store_objects:
            continue
        seen_store_objects.add(marker)
        avg = find_current_hour_average(obj)
        if avg is not None:
            readings[str(store_name).strip()] = avg
    return readings


def fetch_readings():
    payload = http_json(HME_URL, headers={"User-Agent": "Kasselmann-HME-Alerts/1.0"})
    readings = extract_store_readings(payload)
    if not readings:
        raise RuntimeError("HME response did not contain CurrentHour AverageTimeInSec values")
    return readings


def build_store_filters(tag_key, bit):
    matching_values = [str(mask) for mask in range(16) if mask & bit]
    filters = []
    for index, value in enumerate(matching_values):
        if index:
            filters.append({"operator": "OR"})
        filters.append({"field": "tag", "key": tag_key, "relation": "=", "value": value})
    return filters


def send_push(store_name, average):
    api_key = os.environ.get("ONESIGNAL_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Missing ONESIGNAL_API_KEY environment variable")

    target = STORE_TARGETS.get(store_name)
    if not target:
        print(f"No OneSignal target mapping for {store_name}; alert not sent.", flush=True)
        return False

    tag_key, bit = target
    body = {
        "app_id": APP_ID,
        "target_channel": "push",
        "filters": build_store_filters(tag_key, bit),
        "headings": {"en": f"🚨 {store_name} HME Alert"},
        "contents": {
            "en": f"{store_name} has remained at or above {THRESHOLD} seconds for 5 minutes. Current Hour Average: {round(average)} seconds."
        },
        "ios_sound": "default",
        "android_sound": "default",
        "name": f"HME threshold alert - {store_name}",
        "url": "https://shurd080880-lgtm.github.io/kasselmann-hme-alerts/",
        "idempotency_key": str(uuid.uuid4()),
    }

    result = http_json(
        ONESIGNAL_URL,
        headers={
            "Authorization": f"Key {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
        body=body,
    )
    print(f"OneSignal response for {store_name} using {tag_key} bit {bit}: {result}", flush=True)
    errors = result.get("errors") if isinstance(result, dict) else None
    notification_id = result.get("id") if isinstance(result, dict) else None
    success = bool(notification_id) and not errors
    if not success:
        print(f"Alert delivery failed for {store_name}; keeping it eligible for retry.", flush=True)
    return success


def process_readings(state, readings, now):
    alerted = state.setdefault("alerted", {})
    above_since = state.setdefault("above_since", {})

    for store, avg in readings.items():
        if avg >= THRESHOLD:
            if store not in above_since:
                above_since[store] = now
                print(f"{store} crossed threshold at {round(avg)} seconds; starting 5-minute timer.", flush=True)

            elapsed = max(0, now - float(above_since[store]))
            remaining = max(0, REQUIRED_SECONDS - elapsed)
            print(f"{store}: {round(avg)} sec, above threshold for {int(elapsed)} sec, {int(remaining)} sec remaining.", flush=True)

            if elapsed >= REQUIRED_SECONDS and not alerted.get(store, False):
                if send_push(store, avg):
                    alerted[store] = True
                    print(f"Marked {store} alerted after confirmed OneSignal acceptance.", flush=True)
                else:
                    alerted[store] = False
        else:
            if store in above_since:
                print(f"{store} recovered to {round(avg)} seconds; resetting 5-minute timer.", flush=True)
                above_since.pop(store, None)
            if alerted.get(store, False):
                print(f"{store} recovered below threshold; allowing a future alert.", flush=True)
                alerted[store] = False


def main():
    state = load_state()
    print("Kasselmann HME monitor started in continuous mode.", flush=True)

    while True:
        try:
            now = time.time()
            readings = fetch_readings()
            print(f"HME readings: {readings}", flush=True)
            process_readings(state, readings, now)
            save_state(state)
        except Exception as error:
            print(f"Monitor error: {error}. Retrying in {CHECK_INTERVAL_SECONDS} seconds.", flush=True)

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
