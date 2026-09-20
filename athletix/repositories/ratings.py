# -*- coding: utf-8 -*-
"""Coach rating data access."""

from athletix.repositories.base import Repository


class RatingRepository(Repository):

    def list_all(self):
        return self._all("SELECT * FROM ratings")

    def aggregates(self):
        """{coach_id: {"avg": float, "count": int}} for every rated coach.

        One GROUP BY instead of shipping every rating row to the browser and
        averaging there - which also meant every viewer received who rated
        whom for the whole platform.
        """
        return {r["coach_id"]: {"avg": round(r["avg_stars"], 2),
                                "count": r["n"]}
                for r in self._all(
                    "SELECT coach_id, AVG(stars) AS avg_stars, COUNT(*) AS n "
                    "FROM ratings GROUP BY coach_id")}

    def by_athlete(self, athlete_id):
        """{coach_id: stars} - only the caller's own ratings."""
        return {r["coach_id"]: r["stars"] for r in self._all(
            "SELECT coach_id, stars FROM ratings WHERE athlete_id=?",
            (athlete_id,))}

    def count(self):
        return self._one("SELECT COUNT(*) FROM ratings")[0]

    def given_by(self, athlete_id):
        return self._all(
            "SELECT coach_id,stars,updated_at FROM ratings WHERE athlete_id=?",
            (athlete_id,))

    def for_coach_with_names(self, coach_id, limit=200):
        """Ratings a coach has received, with the rater's name.

        Attribution - who rated whom - is operator-only information; the
        service enforces that. The join happens here rather than by shipping
        the ratings table and the user table to a browser to match up.
        """
        return self._all(
            "SELECT r.stars, r.updated_at, u.id AS athlete_id, u.name "
            "FROM ratings r LEFT JOIN users u ON u.id = r.athlete_id "
            "WHERE r.coach_id=? ORDER BY r.updated_at DESC LIMIT ?",
            (coach_id, limit))

    def upsert(self, coach_id, athlete_id, stars, updated_at):
        """The composite primary key plus a session-derived athlete_id means
        this can only ever touch the caller's own row."""
        self._exec(
            "INSERT INTO ratings (coach_id,athlete_id,stars,updated_at) "
            "VALUES (?,?,?,?) ON CONFLICT(coach_id,athlete_id) DO UPDATE SET "
            "stars=excluded.stars, updated_at=excluded.updated_at",
            (coach_id, athlete_id, stars, updated_at))
