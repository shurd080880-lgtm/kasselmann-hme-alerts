import json
import os
import time
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HME_URL = "https://hme-live2-leaderboard.azurewebsites.net/api/cib/lbdata/?storeUID=C261A68011D84960B705E846BC287752"
ONESIGNAL_URL = "https://api.onesignal.com/notifications?c=push"
APP_ID = "48b2684c-c9e3-466c-afe5-24779f7b2096"
THRESHOLD = 250
REQUIRED_SECONDS = 5 * 60
CHECK_INTERVAL_SECONDS = 60
STATE_FILE = Path("hme-alert-state.json")
LOCAL_TIME_ZONE = ZoneInfo("America/New_York")
MONITOR_START_HOUR = 7
MONITOR_STOP_HOUR = 21

# Each HME store now has its own direct OneSignal tag.
# This lets one qualifying store event create one OneSignal notification request,
# eliminating duplicate delivery caused by splitting mask-based targeting across batches.
STORE_TARGETS = {
    "Pendleton-Kasselmann": "hme_target_pendleton",
    "Eminence - Kasselmann": "hme_target_eminence",
    "LaGrange -Kasselmann": "hme_target_lagrange",
    "Hanover- Kasselmann": "hme_target_hanover",
    "Madison- Kasselmann": "hme_target_madison",
    "Clarksville-Kasselmann": "hme_target_clarksville",
    "Buckner-Kasselmann": "hme_target_buckner",
    "Veterans-Kasselmann": "hme_target_veterans",
}


def http_json(url, headers=None, method="GET", body=None):
    req = urllib.request.Request(url, headers=headers or {}, method=method)
    if body is not None:
        req.data = json.dumps(body).encode("utf-8")
    with urllib.request.urlopen(req, timeout=30) as response:
        text = response.read().decode("utf-8")
        return json.loads(text) if text else {}


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


def reset_alert_state(state):
    state["alerted"] = {}
    state["above_since"] = {}


def monitoring_active():
    local_hour = datetime.now(LOCAL_TIME_ZONE).hour
    return MONITOR_START_HOUR <= local_hour < MONITOR_STOP_HOUR


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


def log_alert_to_google_sheet(store_name, average, notification_id):
    webhook_url = os.environ.get("GOOGLE_SHEET_WEBHOOK_URL", "").strip()
    webhook_secret = os.environ.get("GOOGLE_SHEET_WEBHOOK_SECRET", "").strip()
    if not webhook_url:
        print("Google Sheet logging is not configured; alert was still sent.", flush=True)
        return

    now = datetime.now(LOCAL_TIME_ZONE)
    payload = {
        "secret": webhook_secret,
        "date": now.strftime("%m/%d/%Y"),
        "time": now.strftime("%I:%M:%S %p"),
        "location": store_name,
        "average_seconds": round(average),
        "threshold_seconds": THRESHOLD,
        "required_minutes": REQUIRED_SECONDS // 60,
        "result": "Alert Sent",
        "onesignal_notification_id": notification_id,
    }

    try:
        result = http_json(
            webhook_url,
            headers={"Content-Type": "application/json"},
            method="POST",
            body=payload,
        )
        print(f"Google Sheet log response for {store_name}: {result}", flush=True)
    except Exception as error:
        print(f"Google Sheet logging failed for {store_name}: {error}", flush=True)


def send_push(store_name, average):
    api_key = os.environ.get("ONESIGNAL_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Missing ONESIGNAL_API_KEY environment variable")

    tag_key = STORE_TARGETS.get(store_name)
    if not tag_key:
        print(f"No OneSignal target mapping for {store_name}; alert not sent.", flush=True)
        return False

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
    print(
        f"OneSignal response for {store_name} using direct tag {tag_key}: {result}",
        flush=True,
    )

    errors = result.get("errors") if isinstance(result, dict) else None
    notification_id = result.get("id") if isinstance(result, dict) else None
    succeeded = bool(notification_id) and not errors

    if succeeded:
        log_alert_to_google_sheet(store_name, average, str(notification_id))
        return True

    print(
        f"Alert delivery failed for {store_name}; keeping the store eligible for retry.",
        flush=True,
    )
    return False


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
    print("HME notification hours: 7:00 AM-9:00 PM Eastern.", flush=True)

    while True:
        try:
            if not monitoring_active():
                if state.get("alerted") or state.get("above_since"):
                    reset_alert_state(state)
                    save_state(state)
                    print("HME monitoring is OFF. Timers reset until 7:00 AM Eastern.", flush=True)
                time.sleep(CHECK_INTERVAL_SECONDS)
                continue

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
