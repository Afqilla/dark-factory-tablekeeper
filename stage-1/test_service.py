import concurrent.futures
import http.client
import json
import threading
import unittest
from datetime import date, timedelta
from http.server import ThreadingHTTPServer

import service


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), service.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def request(self, method, path, value=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=4)
        data = None if value is None else json.dumps(value)
        conn.request(method, path, body=data, headers={"Content-Type": "application/json", **(headers or {})})
        response = conn.getresponse()
        raw = response.read()
        status = response.status
        conn.close()
        return status, json.loads(raw) if raw else None

    def fixture(self, zone="Europe/Berlin", day=None, cutoff=0):
        day = day or (date.today() + timedelta(days=30)).isoformat()
        weekday = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][date.fromisoformat(day).weekday()]
        fixture = {
            "users": [{"id": "u_ada", "email": "ada@example.com", "password": "correct horse", "display_name": "Ada"}],
            "restaurants": [{"id": "r", "name": "Room", "timezone": zone, "slot_minutes": 30,
                "reservation_duration_minutes": 90, "cancellation_cutoff_minutes": cutoff,
                "opening_hours": [{"weekday": weekday, "opens": "00:00", "closes": "23:59"}],
                "tables": [{"id": "t1", "label": "1", "capacity": 2}, {"id": "t2", "label": "2", "capacity": 4}]}],
            "reservations": []}
        self.assertEqual(self.request("POST", "/_test/reset", fixture)[0], 204)
        status, login = self.request("POST", "/auth/login", {"email": "ada@example.com", "password": "correct horse"})
        self.assertEqual(status, 200)
        return login["token"], day

    def test_public_catalog_and_grid(self):
        _, day = self.fixture()
        self.assertEqual(self.request("GET", "/restaurants")[1]["restaurants"][0]["id"], "r")
        status, data = self.request("GET", f"/availability?restaurant_id=r&date={day}&party_size=2")
        self.assertEqual(status, 200)
        self.assertIn("t1", data["slots"][0]["available_table_ids"])

    def test_idempotency_booking_conflict_and_import_receipt(self):
        token, day = self.fixture()
        auth = {"Authorization": "Bearer " + token, "Idempotency-Key": "same"}
        body = {"restaurant_id": "r", "table_id": "t1", "starts_at_local": day + "T18:00", "party_size": 2}
        first_status, first = self.request("POST", "/reservations", body, auth)
        replay_status, replay = self.request("POST", "/reservations", body, auth)
        self.assertEqual((first_status, replay_status, replay), (201, 200, first))
        mismatch = dict(body, party_size=1)
        self.assertEqual(self.request("POST", "/reservations", mismatch, auth)[1]["error"]["code"], "idempotency_key_reuse")
        export_status, exported = self.request("GET", "/_test/export")
        self.assertEqual(export_status, 200)
        self.assertEqual(self.request("POST", "/_test/reset", {"users": [], "restaurants": [], "reservations": []})[0], 204)
        self.assertEqual(self.request("POST", "/_test/import", exported)[0], 204)
        self.assertEqual(self.request("POST", "/reservations", body, auth), (200, first))

    def test_half_open_and_concurrent_writes(self):
        token, day = self.fixture()
        headers = {"Authorization": "Bearer " + token, "Idempotency-Key": "parallel"}
        body = {"restaurant_id": "r", "table_id": "t1", "starts_at_local": day + "T18:00", "party_size": 2}
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            replies = list(pool.map(lambda _: self.request("POST", "/reservations", body, headers), range(8)))
        self.assertEqual(sum(code == 201 for code, _ in replies), 1)
        self.assertEqual(sum(code == 200 for code, _ in replies), 7)
        # Adjacent half-open interval is available at precisely the first booking's end.
        adjacent = dict(body, starts_at_local=day + "T19:30")
        self.assertEqual(self.request("POST", "/reservations", adjacent, {**headers, "Idempotency-Key": "adjacent"})[0], 201)

    def test_competing_distinct_keys_only_confirm_one_booking(self):
        token, day = self.fixture()
        body = {"restaurant_id": "r", "table_id": "t1", "starts_at_local": day + "T18:00", "party_size": 2}
        def send(n):
            return self.request("POST", "/reservations", body, {"Authorization": "Bearer " + token, "Idempotency-Key": f"race-{n}"})
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            replies = list(pool.map(send, range(12)))
        self.assertEqual(sum(code == 201 for code, _ in replies), 1)
        self.assertEqual(sum(code == 409 and value["error"]["code"] == "table_unavailable" for code, value in replies), 11)

    def test_dst_skipped_and_repeated_local_times(self):
        self.fixture("Europe/Berlin", "2026-03-29")
        status, data = self.request("GET", "/availability?restaurant_id=r&date=2026-03-29&party_size=2")
        self.assertEqual(status, 200)
        self.assertNotIn("2026-03-29T02:00", [s["starts_at_local"] for s in data["slots"]])
        token, _ = self.fixture("Europe/Berlin", "2026-10-25")
        status, data = self.request("GET", "/availability?restaurant_id=r&date=2026-10-25&party_size=2")
        starts = [s for s in data["slots"] if s["starts_at_local"] == "2026-10-25T02:00"]
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]["starts_at"], "2026-10-25T02:00:00+02:00")
        result = self.request("POST", "/reservations", {"restaurant_id": "r", "table_id": "t1", "starts_at_local": "2026-10-25T02:00", "party_size": 2}, {"Authorization": "Bearer " + token, "Idempotency-Key": "fall"})
        self.assertEqual(result[0], 201)

    def test_batch_move_is_atomic_and_replayable(self):
        token, day = self.fixture()
        auth = {"Authorization": "Bearer " + token}
        refs = []
        for n, starts in enumerate(("18:00", "21:00")):
            status, created = self.request("POST", "/reservations", {
                "restaurant_id": "r", "table_id": "t1", "starts_at_local": day + "T" + starts, "party_size": 2
            }, {**auth, "Idempotency-Key": f"create-{n}"})
            self.assertEqual(status, 201)
            refs.append(created["reference"])
        batch = {"moves": [{"reference": refs[0], "table_id": "t2"}, {"reference": refs[1], "table_id": "t2"}]}
        status, moved = self.request("POST", "/reservation-moves", batch, {**auth, "Idempotency-Key": "move-key"})
        self.assertEqual(status, 201)
        replay = self.request("POST", "/reservation-moves", batch, {**auth, "Idempotency-Key": "move-key"})
        self.assertEqual(replay, (200, moved))
        # A second operation that creates an overlap must fail without changing either booking.
        conflicting = {"moves": [{"reference": refs[0], "starts_at_local": day + "T22:00"}, {"reference": refs[1], "starts_at_local": day + "T22:00"}]}
        bad_status, bad = self.request("POST", "/reservation-moves", conflicting, {**auth, "Idempotency-Key": "move-conflict"})
        self.assertEqual((bad_status, bad["error"]["code"]), (409, "table_unavailable"))
        listing = self.request("GET", "/reservations", headers=auth)[1]["reservations"]
        self.assertEqual({r["reference"]: r["table_id"] for r in listing}, {refs[0]: "t2", refs[1]: "t2"})


if __name__ == "__main__":
    unittest.main()
