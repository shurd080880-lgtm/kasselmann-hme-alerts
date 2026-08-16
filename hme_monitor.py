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
CHECK_INTERVAL_SECONDS = 60
REQUIRED_MINUTES = 5
STATE_FILE = Path("hme-alert-state.json")

STORE_TAGS = {
    "Pendleton-Kasselmann": "pendleton",
    "Eminence - Kasselmann": "eminence",
    "LaGrange -Kasselmann": "lagrange",
    "Hanover- Kasselmann": "hanover",
    "Madison- Kasselmann": "madison",
    "Clarksville-Kasselmann": "clarksville",
    "Buckner-Kasselmann": "buckner",
    "Veterans-Kasselmann": "veterans",
}


def http_json(url, headers=None, method="GET", body=None):
    req = urllib.request.Request(url, headers=headers or {}, method=method)
    if body is not None:
        req.data = json.dumps(body).encode("utf-8")
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def load_state():
    if not STATE_FILE.exists():
        return {"alerted": {}}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"alerted": {}}


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


def send_push(store_name, average):
    api_key = os.environ.get("ONESIGNAL_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Missing ONESIGNAL_API_KEY GitHub Actions secret")

    store_tag = STORE_TAGS.get(store_name)
    if not store_tag:
        print(f"No OneSignal store tag mapping for {store_name}; alert not sent.")
        return

    tag_key = f"hme_store_{store_tag}"
    body = {
        "app_id": APP_ID,
        "target_channel": "push",
        "filters": [
            {"field": "tag", "key": tag_key, "relation": "=", "value": "1"}
        ],
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
    print(f"Sent OneSignal alert for {store_name} to {tag_key}=1: {result}")


def main():
    state = load_state()
    alerted = state.setdefault("alerted", {})
    first = fetch_readings()
    print("Current HME readings:", first)
    print(f"Stores found: {len(first)}")

    for store, avg in first.items():
        if avg < THRESHOLD and alerted.get(store):
            alerted[store] = False
            print(f"Reset alert state for {store}; current average is {avg}")

    candidates = {
        store: avg
        for store, avg in first.items()
        if avg >= THRESHOLD and not alerted.get(store, False)
    }

    if not candidates:
        save_state(state)
        print("No new stores need a 5-minute threshold check.")
        return

    print("Monitoring candidates for 5 continuous minutes:", candidates)

    for minute in range(1, REQUIRED_MINUTES + 1):
        time.sleep(CHECK_INTERVAL_SECONDS)
        latest = fetch_readings()
        print(f"Minute {minute} readings:", latest)
        for store in list(candidates):
            avg = latest.get(store)
            if avg is None or avg < THRESHOLD:
                print(f"{store} dropped below threshold; cancelling alert check.")
                candidates.pop(store, None)
            else:
                candidates[store] = avg
        if not candidates:
            break

    for store, avg in candidates.items():
        send_push(store, avg)
        alerted[store] = True

    save_state(state)


if __name__ == "__main__":
    main()
