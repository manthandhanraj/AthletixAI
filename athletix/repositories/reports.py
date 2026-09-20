# -*- coding: utf-8 -*-
"""Assessment (report) data access, including AI results and video metadata."""

from athletix.repositories.base import Repository


class ReportRepository(Repository):

    # ── reads ──────────────────────────────────────────────────────────
    def find_by_id(self, report_id):
        return self._one("SELECT * FROM reports WHERE id=?", (report_id,))

    def list_for_athlete(self, athlete_id, limit=None, offset=0):
        """`id` is the tiebreaker so offset paging stays deterministic when
        two reports share a timestamp."""
        if limit is None:
            return self._all("SELECT * FROM reports WHERE athlete_id=? "
                             "ORDER BY date, id", (athlete_id,))
        return self._all("SELECT * FROM reports WHERE athlete_id=? "
                         "ORDER BY date, id LIMIT ? OFFSET ?",
                         (athlete_id, limit, offset))

    def count_for_athlete(self, athlete_id):
        return self._one("SELECT COUNT(*) FROM reports WHERE athlete_id=?",
                         (athlete_id,))[0]

    def list_all(self):
        """NOTE (Phase 2.1 finding A1): unbounded, and every caller then does
        a per-row ai_results lookup. Pagination and the N+1 fix are Phase 2.4
        work - deliberately not changed here."""
        return self._all("SELECT * FROM reports ORDER BY date")

    def ai_result(self, report_id):
        return self._one("SELECT * FROM ai_results WHERE report_id=?",
                         (report_id,))

    def ai_results_all(self):
        """Every AI result, keyed by report id. Only for callers authorized
        to see detail on every report (coach / admin / owner)."""
        return {r["report_id"]: r for r in self._all("SELECT * FROM ai_results")}

    def ai_results_for_reports(self, report_ids):
        """AI results for exactly these reports, keyed by report id.

        For a PAGE of reports: one query instead of one per row. The caller
        passes ids it has already authorized, and the list is bounded by the
        page size (MAX_LIMIT = 500), which stays well under SQLite's 999
        parameter ceiling - `ai_results_for_athlete` is the unbounded case.
        """
        ids = list(report_ids)
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        return {r["report_id"]: r for r in self._all(
            "SELECT * FROM ai_results WHERE report_id IN (%s)" % placeholders,
            ids)}

    def ai_results_for_athlete(self, athlete_id):
        """AI results for one athlete's reports, keyed by report id.

        The subquery encodes the scope, so an athlete viewer never loads
        another athlete's AI verdicts - and it avoids an IN list with one
        parameter per report, which would blow SQLITE_MAX_VARIABLE_NUMBER.
        """
        return {r["report_id"]: r for r in self._all(
            "SELECT * FROM ai_results WHERE report_id IN "
            "(SELECT id FROM reports WHERE athlete_id=?)", (athlete_id,))}

    def count(self):
        return self._one("SELECT COUNT(*) FROM reports")[0]

    def count_live(self):
        return self._one("SELECT COUNT(*) FROM reports WHERE live=1")[0]

    def performance_series(self):
        """Every report reduced to what a performance summary needs.

        ONE query for the whole cohort, ordered so a caller can fold it into
        per-athlete aggregates in a single pass. The ordering is
        (athlete_id, date, id): `id` is the tiebreaker so two reports filed in
        the same second fold in a deterministic order - the frontend used to
        rely on array order for exactly this, and a summary that flipped
        between requests would make "progress" flicker.

        This is what replaces shipping 3,800 reports to every browser: the
        rows are read here and leave as ~200 bytes per athlete.
        """
        return self._all(
            "SELECT athlete_id, id, date, sport, overall, live, speed, "
            "agility, strength, stamina, technique FROM reports "
            "ORDER BY athlete_id, date, id")

    def counters(self, today_prefix, week_cutoff):
        """Platform-wide report counters, computed in SQL.

        `today_prefix` is an ISO date ('2026-08-30') matched against the
        stored 'YYYY-MM-DDTHH:MM:SS' text, and `week_cutoff` is a full ISO
        timestamp. Both comparisons are lexicographic, which is exactly what
        ISO-8601 guarantees - see POSTGRESQL_READINESS.md 3.3.
        """
        row = self._one(
            "SELECT COUNT(*) AS total, "
            "COALESCE(SUM(live), 0) AS live, "
            "COALESCE(SUM(CASE WHEN date >= ? THEN 1 ELSE 0 END), 0) AS today, "
            "COALESCE(SUM(CASE WHEN date >= ? THEN 1 ELSE 0 END), 0) AS week "
            "FROM reports", (today_prefix, week_cutoff))
        return {"total": row["total"], "live": row["live"],
                "today": row["today"], "week": row["week"]}

    def average_latest_overall(self):
        """Mean of each athlete's most recent overall score. One query."""
        row = self._one(
            "SELECT AVG(overall) AS avg_overall FROM reports r WHERE r.id = "
            "(SELECT r2.id FROM reports r2 WHERE r2.athlete_id = r.athlete_id "
            " ORDER BY r2.date DESC, r2.id DESC LIMIT 1)")
        return row["avg_overall"]

    def daily_counts(self, since):
        """Reports per calendar day since `since`. One query, one row a day."""
        return self._all(
            "SELECT substr(date, 1, 10) AS day, COUNT(*) AS n FROM reports "
            "WHERE date >= ? GROUP BY day ORDER BY day", (since,))

    def athletes_active_since(self, since):
        """Distinct athletes who filed a report since `since`."""
        return {r["athlete_id"] for r in self._all(
            "SELECT DISTINCT athlete_id FROM reports WHERE date >= ?",
            (since,))}

    def recent_with_names(self, limit=12):
        """Newest reports joined to the athlete's name, for the operator feed.

        The join happens in SQL rather than by shipping the user table to the
        browser and matching there.
        """
        return self._all(
            "SELECT r.id, r.athlete_id, r.date, r.sport, r.overall, u.name "
            "FROM reports r LEFT JOIN users u ON u.id = r.athlete_id "
            "ORDER BY r.date DESC, r.id DESC LIMIT ?", (limit,))

    def latest_and_earliest_per_athlete(self, columns="speed, strength, overall"):
        """Rows ordered by date so a caller can fold them into first/last."""
        return self._all("SELECT athlete_id, %s FROM reports ORDER BY date"
                         % columns)

    # ── writes ─────────────────────────────────────────────────────────
    def create(self, athlete_id, date, sport, metrics, overall, live,
               provider=None, version=None, trusted=0, confidence=None):
        """Insert a report, including where its score came from.

        The provenance columns are written here rather than derived later:
        a row with no recorded provider would be indistinguishable from a
        server-verified one, which is exactly the ambiguity Phase 2.8 exists
        to remove. Defaults keep older call sites (and the tests that build
        fixture data directly) working - an unrecorded score is treated as
        untrusted, never as verified.
        """
        cur = self._exec(
            "INSERT INTO reports (athlete_id,date,sport,speed,agility,strength,"
            "stamina,technique,overall,live,scoring_provider,scoring_version,"
            "scoring_trusted,scoring_confidence) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (athlete_id, date, sport, metrics["speed"], metrics["agility"],
             metrics["strength"], metrics["stamina"], metrics["technique"],
             overall, live, provider, version, 1 if trusted else 0,
             confidence))
        return cur.lastrowid

    def add_ai_result(self, report_id, potential, medal_prob, risk, best_fit):
        self._exec("INSERT INTO ai_results (report_id,potential,medal_prob,"
                   "risk,best_fit) VALUES (?,?,?,?,?)",
                   (report_id, potential, medal_prob, risk, best_fit))

    def add_video(self, report_id, athlete_id, filename, size_mb, source,
                  uploaded_at):
        self._exec("INSERT INTO videos (report_id,athlete_id,filename,size_mb,"
                   "source,uploaded_at) VALUES (?,?,?,?,?,?)",
                   (report_id, athlete_id, filename, size_mb, source,
                    uploaded_at))
