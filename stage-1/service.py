"""Tablekeeper stage 1 HTTP service (standard library only)."""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import uuid
from datetime import date, datetime, time, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str = "Request failed"):
        self.status, self.code, self.message = status, code, message


def fail(status: int, code: str, message: str = "Request failed"):
    raise ApiError(status, code, message)


def now_rfc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def dt_out(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def pw_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    return "scrypt$" + salt.hex() + "$" + hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1).hex()


def pw_check(password: str, value: str) -> bool:
    try:
        scheme, salt_hex, digest_hex = value.split("$")
        if scheme != "scrypt":
            return False
        salt = bytes.fromhex(salt_hex)
        candidate = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
        return hmac.compare_digest(candidate.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def new_state():
    return {"users": {}, "restaurants": {}, "reservations": {}, "tokens": {}, "idempotency": {}}


state = new_state()
lock = threading.RLock()
WEEK = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
LOCAL_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+$")


def json_type(obj, key, expected, required=True):
    if key not in obj:
        if required:
            fail(422, "validation_failed", f"Missing {key}")
        return None
    value = obj[key]
    if expected is int:
        ok = type(value) is int
    elif expected is bool:
        ok = type(value) is bool
    else:
        ok = isinstance(value, expected)
    if not ok:
        fail(400, "malformed_request", f"Invalid type for {key}")
    return value


def validate_id(value):
    if not isinstance(value, str) or not value or len(value) > 64:
        fail(422, "validation_failed", "Invalid ID")
    return value


def local_candidates(local: datetime, zone: ZoneInfo):
    found = []
    for fold in (0, 1):
        aware = local.replace(tzinfo=zone, fold=fold)
        instant = aware.astimezone(timezone.utc)
        back = instant.astimezone(zone)
        if back.replace(tzinfo=None) == local and all(instant != x[0] for x in found):
            found.append((instant, aware))
    return sorted(found, key=lambda x: x[0])


def local_parse(value, zone):
    if not isinstance(value, str) or not LOCAL_RE.fullmatch(value):
        fail(422, "validation_failed", "starts_at_local must be YYYY-MM-DDTHH:MM")
    try:
        naive = datetime.strptime(value, "%Y-%m-%dT%H:%M")
    except ValueError:
        fail(422, "validation_failed", "Invalid local date or time")
    candidates = local_candidates(naive, zone)
    if not candidates:
        fail(422, "invalid_local_time", "This local time does not exist")
    return naive, candidates[0][0].astimezone(zone)


def rget(obj, key, expected, optional=False):
    return json_type(obj, key, expected, required=not optional)


def reservation_shape(res):
    return {k: res[k] for k in ("reservation_id", "reference", "restaurant_id", "table_id", "party_size", "status", "starts_at_local", "starts_at", "ends_at", "created_at")}


def table_for(restaurant, table_id):
    for table in restaurant["tables"]:
        if table["id"] == table_id:
            return table
    return None


def restaurant_hours(restaurant, local):
    for item in restaurant["opening_hours"]:
        if item["weekday"] == WEEK[local.weekday()]:
            return time.fromisoformat(item["opens"]), time.fromisoformat(item["closes"])
    return None


def validate_booking(restaurant, data, original=None, ignore_refs=frozenset()):
    # PATCH-like fields; malformed JSON types are distinct from domain validation.
    table_id = rget(data, "table_id", str, optional=True) if "table_id" in data else (original["table_id"] if original else None)
    local_value = rget(data, "starts_at_local", str, optional=True) if "starts_at_local" in data else (original["starts_at_local"] if original else None)
    party = data.get("party_size") if "party_size" in data else (original["party_size"] if original else None)
    if table_id is None:
        fail(422, "validation_failed", "Missing table_id")
    if local_value is None:
        fail(422, "validation_failed", "Missing starts_at_local")
    if type(party) is not int or party < 1:
        fail(422, "validation_failed", "party_size must be a positive integer")
    table = table_for(restaurant, table_id)
    if table is None:
        fail(404, "not_found", "Table not found")
    if party > table["capacity"]:
        fail(422, "party_exceeds_capacity", "Party exceeds table capacity")
    zone = ZoneInfo(restaurant["timezone"])
    local, starts = local_parse(local_value, zone)
    hours = restaurant_hours(restaurant, local)
    if hours is None:
        fail(422, "outside_opening_hours", "Restaurant is closed")
    opens, closes = hours
    if local.time() < opens or local.time() >= closes:
        fail(422, "outside_opening_hours", "Outside opening hours")
    if ((local.hour * 60 + local.minute) - (opens.hour * 60 + opens.minute)) % restaurant["slot_minutes"] != 0:
        fail(422, "not_on_slot_grid", "Not on the slot grid")
    end = starts.astimezone(timezone.utc) + timedelta(minutes=restaurant["reservation_duration_minutes"])
    end_local = end.astimezone(zone)
    if end_local.date() != local.date() or end_local.time().replace(tzinfo=None) > closes:
        fail(422, "outside_opening_hours", "Reservation ends after closing")
    for existing in state["reservations"].values():
        if existing["status"] != "confirmed" or existing["restaurant_id"] != restaurant["id"] or existing["table_id"] != table_id or existing["reference"] in ignore_refs:
            continue
        other_start = datetime.fromisoformat(existing["starts_at"]).astimezone(timezone.utc)
        other_end = other_start + timedelta(minutes=restaurant["reservation_duration_minutes"])
        if starts.astimezone(timezone.utc) < other_end and other_start < end:
            fail(409, "table_unavailable", "Table is already occupied")
    return table_id, party, local_value, starts, end.astimezone(zone)


def make_reservation(user_id, restaurant, validated):
    table_id, party, local_value, starts, ends = validated
    refs = {r["reference"] for r in state["reservations"].values()}
    while True:
        reference = "".join(secrets.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789") for _ in range(6))
        if reference not in refs:
            break
    rid = "res_" + uuid.uuid4().hex
    res = {"reservation_id": rid, "reference": reference, "restaurant_id": restaurant["id"], "table_id": table_id,
           "party_size": party, "status": "confirmed", "starts_at_local": local_value,
           "starts_at": dt_out(starts), "ends_at": dt_out(ends), "created_at": now_rfc(), "user_id": user_id}
    state["reservations"][rid] = res
    return reservation_shape(res)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def send_json(self, status, value):
        data = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_empty(self, status):
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def body(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
            parsed = json.loads(self.rfile.read(n))
        except Exception:
            fail(400, "malformed_request", "Malformed JSON")
        if not isinstance(parsed, dict):
            fail(400, "malformed_request", "JSON body must be an object")
        return parsed

    def user(self, public=False):
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or not auth[7:]:
            fail(401, "unauthenticated", "Bearer token required")
        uid = state["tokens"].get(auth[7:])
        if uid not in state["users"]:
            fail(401, "unauthenticated", "Unknown bearer token")
        return uid

    def request_idem(self, body, user_id):
        key = self.headers.get("Idempotency-Key")
        if key is None or key == "":
            fail(400, "missing_idempotency_key", "Idempotency-Key is required")
        if not (1 <= len(key) <= 255):
            fail(422, "validation_failed", "Invalid Idempotency-Key")
        ident = user_id + "\0" + self.command + "\0" + urlsplit(self.path).path + "\0" + key
        fingerprint = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        old = state["idempotency"].get(ident)
        if old:
            if old["method"] != self.command or old["path"] != urlsplit(self.path).path or old["body"] != fingerprint:
                fail(409, "idempotency_key_reuse", "Key was used for another request")
            return ident, old
        return ident, None

    def save_idem(self, ident, body, status, response):
        state["idempotency"][ident] = {"method": self.command, "path": urlsplit(self.path).path,
          "body": json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False), "status": status, "response": copy.deepcopy(response)}

    def do_GET(self):
        with lock:
            try:
                self.route_get()
            except ApiError as e:
                self.send_json(e.status, {"error": {"code": e.code, "message": e.message}})
            except Exception:
                self.send_json(500, {"error": {"code": "internal_error", "message": "Internal server error"}})

    def do_POST(self):
        with lock:
            try:
                self.route_write("POST")
            except ApiError as e:
                self.send_json(e.status, {"error": {"code": e.code, "message": e.message}})
            except Exception:
                self.send_json(500, {"error": {"code": "internal_error", "message": "Internal server error"}})

    def do_PATCH(self):
        with lock:
            try:
                self.route_write("PATCH")
            except ApiError as e:
                self.send_json(e.status, {"error": {"code": e.code, "message": e.message}})
            except Exception:
                self.send_json(500, {"error": {"code": "internal_error", "message": "Internal server error"}})

    def route_get(self):
        path = urlsplit(self.path).path
        if path == "/health":
            self.send_json(200, {"status": "ok"}); return
        if path == "/_test/export":
            self.send_json(200, {"track": "tablekeeper", "format_version": 1, "state": copy.deepcopy(state)}); return
        if path not in ("/restaurants", "/availability") and not re.fullmatch(r"/restaurants/[^/]+", path):
            self.user()
        if path == "/restaurants":
            self.send_json(200, {"restaurants": [{k: r[k] for k in ("id", "name", "timezone")} for r in state["restaurants"].values()]}); return
        match = re.fullmatch(r"/restaurants/([^/]+)", path)
        if match:
            rid = match.group(1); r = state["restaurants"].get(rid)
            if not r: fail(404, "not_found")
            self.send_json(200, {k: copy.deepcopy(r[k]) for k in ("id", "name", "timezone", "slot_minutes", "reservation_duration_minutes", "cancellation_cutoff_minutes", "opening_hours", "tables")}); return
        if path == "/availability":
            qs = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
            for key in ("restaurant_id", "date", "party_size"):
                if key not in qs or not qs[key][0]: fail(422, "validation_failed", f"Missing {key}")
            rid, datestr, partstr = qs["restaurant_id"][0], qs["date"][0], qs["party_size"][0]
            if not re.fullmatch(r"[0-9]+", partstr): fail(422, "validation_failed", "Invalid party_size")
            party = int(partstr)
            if party < 1: fail(422, "validation_failed", "Invalid party_size")
            try:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", datestr): raise ValueError()
                day = date.fromisoformat(datestr)
            except ValueError: fail(422, "validation_failed", "Invalid date")
            r = state["restaurants"].get(rid)
            if not r: fail(404, "not_found")
            hours = restaurant_hours(r, datetime.combine(day, time.min))
            slots = []
            if hours:
                opens, closes = hours
                cur = datetime.combine(day, opens)
                limit = datetime.combine(day, closes)
                while cur + timedelta(minutes=r["reservation_duration_minutes"]) <= limit:
                    candidates = local_candidates(cur, ZoneInfo(r["timezone"]))
                    if candidates:
                        starts = candidates[0][0].astimezone(ZoneInfo(r["timezone"]))
                        end = starts.astimezone(timezone.utc) + timedelta(minutes=r["reservation_duration_minutes"])
                        endloc = end.astimezone(ZoneInfo(r["timezone"]))
                        if endloc.date() == day and endloc.time().replace(tzinfo=None) <= closes:
                            free = []
                            for table in r["tables"]:
                                if table["capacity"] < party: continue
                                occupied = False
                                for res in state["reservations"].values():
                                    if res["status"] != "confirmed" or res["restaurant_id"] != rid or res["table_id"] != table["id"]: continue
                                    other = datetime.fromisoformat(res["starts_at"]).astimezone(timezone.utc)
                                    if starts.astimezone(timezone.utc) < other + timedelta(minutes=r["reservation_duration_minutes"]) and other < end:
                                        occupied = True; break
                                if not occupied: free.append(table["id"])
                            slots.append({"starts_at_local": cur.strftime("%Y-%m-%dT%H:%M"), "starts_at": dt_out(starts), "available_table_ids": free})
                    cur += timedelta(minutes=r["slot_minutes"])
            self.send_json(200, {"restaurant_id": rid, "date": datestr, "timezone": r["timezone"], "slots": slots}); return
        if path == "/reservations":
            uid = self.user()
            items = [reservation_shape(r) for r in state["reservations"].values() if r["user_id"] == uid]
            items.sort(key=lambda x: datetime.fromisoformat(x["starts_at"]).astimezone(timezone.utc), reverse=True)
            self.send_json(200, {"reservations": items}); return
        match = re.fullmatch(r"/reservations/([^/]+)", path)
        if match:
            uid = self.user(); res = self.by_ref(match.group(1), uid)
            self.send_json(200, reservation_shape(res)); return
        self.send_json(404, {"error": {"code": "not_found", "message": "Not found"}})

    def by_ref(self, reference, uid):
        for res in state["reservations"].values():
            if res["reference"] == reference and res["user_id"] == uid: return res
        fail(404, "not_found", "Reservation not found")

    def route_write(self, method):
        global state
        path = urlsplit(self.path).path
        no_body_cancel = method == "POST" and re.fullmatch(r"/reservations/[^/]+/cancel", path) and self.headers.get("Content-Length", "0") == "0"
        body = {} if no_body_cancel else self.body()
        if path == "/_test/reset":
            try:
                state = fixture_state(body)
            except ApiError:
                raise
            except Exception:
                fail(422, "validation_failed", "Invalid fixture")
            self.send_empty(204); return
        if path == "/_test/import":
            try:
                state = import_state(body)
            except ApiError:
                raise
            except Exception:
                fail(422, "validation_failed", "Invalid state")
            self.send_empty(204); return
        if path == "/auth/signup":
            email = json_type(body, "email", str); password = json_type(body, "password", str); name = json_type(body, "display_name", str)
            if not EMAIL_RE.fullmatch(email) or len(password) < 8: fail(422, "validation_failed")
            if any(u["email"].lower() == email.lower() for u in state["users"].values()): fail(409, "email_taken")
            uid = "u_" + uuid.uuid4().hex; token = secrets.token_urlsafe(32)
            state["users"][uid] = {"id": uid, "email": email, "password_hash": pw_hash(password), "display_name": name}
            state["tokens"][token] = uid
            self.send_json(201, {"user_id": uid, "display_name": name, "token": token}); return
        if path == "/auth/login":
            email = json_type(body, "email", str); password = json_type(body, "password", str)
            user = next((u for u in state["users"].values() if u["email"].lower() == email.lower()), None)
            if not user or not pw_check(password, user["password_hash"]): fail(401, "unauthenticated")
            token = secrets.token_urlsafe(32); state["tokens"][token] = user["id"]
            self.send_json(200, {"user_id": user["id"], "display_name": user["display_name"], "token": token}); return
        uid = self.user()
        if path == "/reservations" and method == "POST":
            ident, previous = self.request_idem(body, uid)
            if previous:
                self.send_json(200, previous["response"]); return
            rid = validate_id(json_type(body, "restaurant_id", str)); tid = validate_id(json_type(body, "table_id", str))
            r = state["restaurants"].get(rid)
            if not r or not table_for(r, tid): fail(404, "not_found")
            validated = validate_booking(r, body)
            result = make_reservation(uid, r, validated); self.save_idem(ident, body, 201, result)
            self.send_json(201, result); return
        if path == "/reservation-moves" and method == "POST":
            ident, previous = self.request_idem(body, uid)
            if previous:
                self.send_json(200, previous["response"]); return
            moves = json_type(body, "moves", list)
            if not 1 <= len(moves) <= 8 or any(not isinstance(m, dict) for m in moves): fail(422, "validation_failed")
            refs = []
            for m in moves:
                ref = m.get("reference")
                if not isinstance(ref, str): fail(422, "validation_failed")
                refs.append(ref)
            if len(set(refs)) != len(refs): fail(422, "validation_failed")
            originals = [self.by_ref(ref, uid) for ref in refs]
            restaurant_ids = {x["restaurant_id"] for x in originals}
            if len(restaurant_ids) != 1: fail(422, "validation_failed")
            r = state["restaurants"][next(iter(restaurant_ids))]
            staged = []
            for move, original in zip(moves, originals):
                if original["status"] != "confirmed": fail(409, "reservation_cancelled")
                if self.cutoff(original, r): fail(409, "cutoff_passed")
                update = {k: move[k] for k in ("table_id", "starts_at_local", "party_size") if k in move}
                # validate each against all other bookings, ignoring all listed bookings, then validate staged against each other below.
                val = validate_booking(r, update, original, set(refs))
                staged.append((original, val))
            staged_intervals = []
            for original, val in staged:
                tid, party, localval, starts, ends = val
                for otid, ostart, oend in staged_intervals:
                    if tid == otid and starts.astimezone(timezone.utc) < oend and ostart < ends.astimezone(timezone.utc):
                        fail(409, "table_unavailable")
                staged_intervals.append((tid, starts.astimezone(timezone.utc), ends.astimezone(timezone.utc)))
            results = []
            for original, val in staged:
                tid, party, localval, starts, ends = val
                original.update({"table_id": tid, "party_size": party, "starts_at_local": localval, "starts_at": dt_out(starts), "ends_at": dt_out(ends)})
                results.append(reservation_shape(original))
            response = {"reservations": results}; self.save_idem(ident, body, 201, response); self.send_json(201, response); return
        match = re.fullmatch(r"/reservations/([^/]+)(?:/(cancel))?", path)
        if match:
            reference, cancel_path = match.groups()
            res = self.by_ref(reference, uid)
            r = state["restaurants"][res["restaurant_id"]]
            if cancel_path and method == "POST":
                if res["status"] == "cancelled": self.send_json(200, reservation_shape(res)); return
                if self.cutoff(res, r): fail(409, "cutoff_passed")
                res["status"] = "cancelled"; self.send_json(200, reservation_shape(res)); return
            if not cancel_path and method == "PATCH":
                if res["status"] != "confirmed": fail(409, "reservation_cancelled")
                if self.cutoff(res, r): fail(409, "cutoff_passed")
                val = validate_booking(r, body, res, {res["reference"]})
                tid, party, localval, starts, ends = val
                res.update({"table_id": tid, "party_size": party, "starts_at_local": localval, "starts_at": dt_out(starts), "ends_at": dt_out(ends)})
                self.send_json(200, reservation_shape(res)); return
        self.send_json(404, {"error": {"code": "not_found", "message": "Not found"}})

    @staticmethod
    def cutoff(res, restaurant):
        starts = datetime.fromisoformat(res["starts_at"]).astimezone(timezone.utc)
        return datetime.now(timezone.utc) >= starts - timedelta(minutes=restaurant["cancellation_cutoff_minutes"])


def fixture_state(fixture):
    global state
    users = fixture.get("users", [])
    restaurants = fixture.get("restaurants", [])
    reservations = fixture.get("reservations", [])
    if not isinstance(users, list) or not isinstance(restaurants, list) or not isinstance(reservations, list): fail(422, "validation_failed")
    result = new_state()
    for user in users:
        if not isinstance(user, dict): fail(422, "validation_failed")
        uid = validate_id(user.get("id")); email = user.get("email"); pw = user.get("password"); name = user.get("display_name")
        if not all(isinstance(x, str) for x in (email, pw, name)) or not EMAIL_RE.fullmatch(email): fail(422, "validation_failed")
        result["users"][uid] = {"id": uid, "email": email, "password_hash": pw_hash(pw), "display_name": name}
    for raw in restaurants:
        if not isinstance(raw, dict): fail(422, "validation_failed")
        r = copy.deepcopy(raw)
        rid = validate_id(r.get("id")); ZoneInfo(r["timezone"])
        for field in ("slot_minutes", "reservation_duration_minutes", "cancellation_cutoff_minutes"):
            if type(r.get(field)) is not int or (field != "cancellation_cutoff_minutes" and r[field] < 1) or r[field] < 0: fail(422, "validation_failed")
        if not isinstance(r.get("opening_hours"), list) or not isinstance(r.get("tables"), list): fail(422, "validation_failed")
        for h in r["opening_hours"]:
            if h.get("weekday") not in WEEK or time.fromisoformat(h["opens"]) >= time.fromisoformat(h["closes"]): fail(422, "validation_failed")
        for t in r["tables"]:
            validate_id(t.get("id"))
            if type(t.get("capacity")) is not int or t["capacity"] < 1: fail(422, "validation_failed")
        result["restaurants"][rid] = r
    # Seed through the same validation/occupancy rules, preserving supplied identities and references.
    previous = state
    state = result
    try:
        for raw in reservations:
            if not isinstance(raw, dict): fail(422, "validation_failed")
            uid = raw.get("user_id"); rid = raw.get("restaurant_id")
            if uid not in result["users"] or rid not in result["restaurants"]: fail(422, "validation_failed")
            r = result["restaurants"][rid]
            val = validate_booking(r, raw)
            ridval = validate_id(raw.get("id")); ref = validate_id(raw.get("reference"))
            if not re.fullmatch(r"[A-Z0-9]{6,12}", ref) or ridval in result["reservations"] or any(x["reference"] == ref for x in result["reservations"].values()): fail(422, "validation_failed")
            res = make_reservation(uid, r, val)
            created = result["reservations"].pop(res["reservation_id"])
            created["reservation_id"] = ridval; created["reference"] = ref; created["status"] = "confirmed"
            result["reservations"][ridval] = created
    except Exception:
        state = previous
        raise
    return result


def import_state(obj):
    if obj.get("track") != "tablekeeper" or obj.get("format_version") != 1 or not isinstance(obj.get("state"), dict): fail(422, "validation_failed")
    raw = obj["state"]
    required = ("users", "restaurants", "reservations", "tokens", "idempotency")
    if any(not isinstance(raw.get(k), dict) for k in required): fail(422, "validation_failed")
    for uid, user in raw["users"].items():
        if not isinstance(user, dict) or validate_id(uid) != uid or user.get("id") != uid or not all(isinstance(user.get(k), str) for k in ("email", "display_name", "password_hash")):
            fail(422, "validation_failed")
        if not user["password_hash"].startswith("scrypt$"):
            fail(422, "validation_failed")
    for rid, restaurant in raw["restaurants"].items():
        if not isinstance(restaurant, dict) or validate_id(rid) != rid or restaurant.get("id") != rid:
            fail(422, "validation_failed")
        if not isinstance(restaurant.get("name"), str) or not isinstance(restaurant.get("timezone"), str):
            fail(422, "validation_failed")
        ZoneInfo(restaurant["timezone"])
        for field in ("slot_minutes", "reservation_duration_minutes", "cancellation_cutoff_minutes"):
            value = restaurant.get(field)
            if type(value) is not int or value < (0 if field == "cancellation_cutoff_minutes" else 1):
                fail(422, "validation_failed")
        hours = restaurant.get("opening_hours")
        tables = restaurant.get("tables")
        if not isinstance(hours, list) or not isinstance(tables, list):
            fail(422, "validation_failed")
        seen_days = set()
        for entry in hours:
            if not isinstance(entry, dict) or entry.get("weekday") not in WEEK or not isinstance(entry.get("opens"), str) or not isinstance(entry.get("closes"), str):
                fail(422, "validation_failed")
            if not re.fullmatch(r"\d{2}:\d{2}", entry["opens"]) or not re.fullmatch(r"\d{2}:\d{2}", entry["closes"]):
                fail(422, "validation_failed")
            opens, closes = time.fromisoformat(entry["opens"]), time.fromisoformat(entry["closes"])
            if opens >= closes or entry["weekday"] in seen_days:
                fail(422, "validation_failed")
            seen_days.add(entry["weekday"])
        seen_tables = set()
        for table in tables:
            if not isinstance(table, dict):
                fail(422, "validation_failed")
            tid = validate_id(table.get("id"))
            if tid in seen_tables or not isinstance(table.get("label"), str) or type(table.get("capacity")) is not int or table["capacity"] < 1:
                fail(422, "validation_failed")
            seen_tables.add(tid)
    refs = set()
    for key, res in raw["reservations"].items():
        if not isinstance(res, dict) or validate_id(key) != key or res.get("reservation_id") != key:
            fail(422, "validation_failed")
        if res.get("user_id") not in raw["users"] or res.get("restaurant_id") not in raw["restaurants"] or res.get("status") not in ("confirmed", "cancelled"):
            fail(422, "validation_failed")
        restaurant = raw["restaurants"][res["restaurant_id"]]
        table = next((t for t in restaurant["tables"] if t["id"] == res.get("table_id")), None)
        if table is None or type(res.get("party_size")) is not int or not 1 <= res["party_size"] <= table["capacity"]:
            fail(422, "validation_failed")
        required_strings = ("starts_at_local", "starts_at", "ends_at", "created_at")
        if not all(isinstance(res.get(k), str) for k in required_strings):
            fail(422, "validation_failed")
        zone = ZoneInfo(restaurant["timezone"])
        try:
            local, expected_start = local_parse(res["starts_at_local"], zone)
        except ApiError:
            fail(422, "validation_failed")
        opening = restaurant_hours(restaurant, local)
        if opening is None or local.time() < opening[0] or local.time() >= opening[1] or ((local.hour * 60 + local.minute) - (opening[0].hour * 60 + opening[0].minute)) % restaurant["slot_minutes"]:
            fail(422, "validation_failed")
        try:
            actual_start = datetime.fromisoformat(res["starts_at"])
            actual_end = datetime.fromisoformat(res["ends_at"])
            created_at = datetime.fromisoformat(res["created_at"])
            if actual_start.tzinfo is None or actual_end.tzinfo is None or created_at.tzinfo is None:
                fail(422, "validation_failed")
            expected_end = expected_start.astimezone(timezone.utc) + timedelta(minutes=restaurant["reservation_duration_minutes"])
            expected_end_local = expected_end.astimezone(zone)
            if (actual_start.astimezone(timezone.utc) != expected_start.astimezone(timezone.utc)
                    or actual_start.utcoffset() != expected_start.utcoffset()
                    or actual_end.astimezone(timezone.utc) != expected_end
                    or actual_end.utcoffset() != expected_end_local.utcoffset()
                    or expected_end_local.date() != local.date()
                    or expected_end_local.time().replace(tzinfo=None) > opening[1]):
                fail(422, "validation_failed")
        except (TypeError, ValueError, OverflowError):
            fail(422, "validation_failed")
        ref = res.get("reference")
        if not isinstance(ref, str) or not re.fullmatch(r"[A-Z0-9]{6,12}", ref) or ref in refs:
            fail(422, "validation_failed")
        refs.add(ref)
    confirmed = [r for r in raw["reservations"].values() if r["status"] == "confirmed"]
    for index, left in enumerate(confirmed):
        left_start = datetime.fromisoformat(left["starts_at"]).astimezone(timezone.utc)
        duration = raw["restaurants"][left["restaurant_id"]]["reservation_duration_minutes"]
        left_end = left_start + timedelta(minutes=duration)
        for right in confirmed[index + 1:]:
            if left["restaurant_id"] != right["restaurant_id"] or left["table_id"] != right["table_id"]:
                continue
            right_start = datetime.fromisoformat(right["starts_at"]).astimezone(timezone.utc)
            right_duration = raw["restaurants"][right["restaurant_id"]]["reservation_duration_minutes"]
            if left_start < right_start + timedelta(minutes=right_duration) and right_start < left_end:
                fail(422, "validation_failed")
    for token, uid in raw["tokens"].items():
        if not isinstance(token, str) or uid not in raw["users"]: fail(422, "validation_failed")
    for key, receipt in raw["idempotency"].items():
        if not isinstance(key, str) or not isinstance(receipt, dict) or receipt.get("method") not in ("POST", "PATCH") or not isinstance(receipt.get("path"), str) or not isinstance(receipt.get("body"), str) or not isinstance(receipt.get("response"), dict):
            fail(422, "validation_failed")
        try:
            if not isinstance(json.loads(receipt["body"]), dict): fail(422, "validation_failed")
        except (TypeError, ValueError):
            fail(422, "validation_failed")
    return copy.deepcopy(raw)


def serve():
    port = int(os.environ.get("PORT", "8080"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    serve()
