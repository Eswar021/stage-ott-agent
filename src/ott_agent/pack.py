from __future__ import annotations

import csv
import hashlib
import json
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from .util import IST, UTC, jsonl, ts


def csv_rows(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


class Pack:
    """Read-only source catalogue. Runtime evidence is held in the event ledger."""

    def __init__(self, root: str | Path, *, progress=None):
        started = time.perf_counter()

        def notify(stage, **fields):
            if progress:
                progress({"stage": stage, "elapsed_s": round(time.perf_counter() - started, 2), **fields})

        self.root = Path(root).resolve()
        required = ["data/events.jsonl", "data/catalogue.csv", "data/episodes.csv",
                    "data/profiles_snapshot.csv", "config/goals.json", "config/interventions.json",
                    "config/pricing.json", "replay/triggers.jsonl", "replay/arms.csv"]
        missing = [p for p in required if not (self.root / p).is_file()]
        if missing:
            raise ValueError("Pack missing: " + ", ".join(missing))
        notify("catalogue_profiles", detail="Loading catalogue, episodes, profiles and configuration")
        self.catalogue = {r["title_id"]: r for r in csv_rows(self.root / "data/catalogue.csv")}
        self.episodes = defaultdict(list)
        for row in csv_rows(self.root / "data/episodes.csv"):
            self.episodes[row["content_slug"]].append(row)
        for rows in self.episodes.values():
            rows.sort(key=lambda r: (int(r["season"]), int(r["episode"])))
        self.profile_versions = defaultdict(list)
        for row in csv_rows(self.root / "data/profiles_snapshot.csv"):
            self.profile_versions[row["user_id"]].append(row)
        self.profiles = {u: max(rows, key=lambda r: r["updated_at"]) for u, rows in self.profile_versions.items()}
        goals = json.loads((self.root / "config/goals.json").read_text())
        self.goal_version = goals["version"]
        self.goals = {r["goal_id"]: r for r in goals["goals"]}
        self.interventions = json.loads((self.root / "config/interventions.json").read_text())
        self.pricing = json.loads((self.root / "config/pricing.json").read_text())
        self.arms = {r["user_id"]: r["arm"] for r in csv_rows(self.root / "replay/arms.csv")}
        self.pilot_outcomes = csv_rows(self.root / "pilot/pilot_outcomes.csv")
        self.history_start = datetime(2026, 9, 1, tzinfo=IST).astimezone(UTC)
        self.popularity = Counter()
        self.popularity_cutoff = self.history_start
        self.history = []
        seen = set()
        self.history_deliveries = 0
        notify("history", deliveries_read=0, unique_events=0)
        for event in jsonl(self.root / "data/events.jsonl"):
            self.history_deliveries += 1
            if self.history_deliveries % 25000 == 0:
                notify("history", deliveries_read=self.history_deliveries, unique_events=len(seen))
            if event["event_id"] in seen:
                continue
            seen.add(event["event_id"])
            self.history.append(event)
            self.popularity_cutoff = max(self.popularity_cutoff, ts(event["received_at"]))
            if event["event"] == "play_started":
                title = self.catalogue.get(event.get("properties", {}).get("title_id"))
                if title:
                    self.popularity[title["content_slug"]] += 1
        self.history.sort(key=lambda e: (e["received_at"], e["event_id"]))
        self.triggers = list(jsonl(self.root / "replay/triggers.jsonl"))
        self.live_events = list(jsonl(self.root / "replay/live_events.jsonl"))
        self.control_events = list(jsonl(self.root / "replay/control_events.jsonl"))
        self.model_faults = {r["trigger_event_id"]: r for r in jsonl(self.root / "replay/model_faults.jsonl")}
        self.replay_start = min(ts(e["deliver_at"]) for e in self.triggers + self.live_events)
        if self.popularity_cutoff > self.replay_start:
            raise ValueError("Historical events overlap replay; import must be bounded before replay start")
        notify("loaded", deliveries_read=self.history_deliveries, unique_events=len(self.history),
               trigger_deliveries=len(self.triggers))

    def fingerprint(self) -> str:
        digest = hashlib.sha256()
        for file in sorted(p for p in self.root.rglob("*") if p.is_file() and p.name != ".DS_Store"):
            digest.update(str(file.relative_to(self.root)).encode())
            with file.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        return digest.hexdigest()
