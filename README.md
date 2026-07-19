# 🏅 AthletixAI

**AI-powered sports talent assessment — using nothing but a phone camera.**

> Discover. Analyze. Elevate. Dominate.

🔗 **Live app:** https://athletixai.onrender.com
🎥 **Demo video:** _(add your link here)_

---

## The problem

India doesn't have a shortage of sporting talent. It has a shortage of **measurement**.

A 15-year-old bowling in a village has no way to know if she's improving. Professional
biomechanics labs cost lakhs and exist in a handful of cities. Coaches can't discover
athletes they've never seen. So talent gets missed — not because it isn't there, but
because nobody measured it.

This project is personal. I played cricket seriously and believed I could go further with
proper coaching — I never got it. AthletixAI exists so that fewer people have that story.

---

## What it does

An athlete records a training video on their phone. The app analyses their body movement
**on the device itself** and returns a performance report scored across five metrics:
Speed, Agility, Strength, Stamina and Technique.

Coaches get a searchable talent pool with real performance data instead of word-of-mouth.

**The complete flow**

| Step | What happens |
|---|---|
| 1 | Athlete signs up (name, sport, age) |
| 2 | Records a clip, or uses live camera mode |
| 3 | MediaPipe extracts **33 body keypoints** per frame — in the browser |
| 4 | The engine computes joint angles, velocity, symmetry, cadence, range of motion |
| 5 | A sport-category-specific formula converts those into 5 scores |
| 6 | Report is saved to the server; the athlete's coach can see it instantly |

---

## How the AI actually works

This is not a chatbot wrapper. There is a real computer-vision pipeline.

**1. Pose estimation (real ML model)**
Google MediaPipe's Pose Landmarker runs in the browser and returns 33 landmarks
(shoulders, elbows, wrists, hips, knees, ankles…) with x, y and a visibility confidence
for every sampled frame.

**2. Feature extraction (real measurements)**
From those landmarks we compute:

- **Joint angles** via the dot-product formula — e.g. knee angle from hip → knee → ankle
  `cos(θ) = (v1 · v2) / (|v1| × |v2|)`
- **Velocity & peak burst** — frame-to-frame displacement of wrists and ankles
- **Symmetry** — left/right shoulder and hip height difference
- **Cadence** — step count from ankle direction changes
- **Range of motion** — total vertical hip travel and deepest knee flexion

Frames where a landmark's visibility drops below 0.3 are discarded, so partial or occluded
bodies don't pollute the result.

**3. Category-specific scoring**
Different sports reward different qualities, so one formula would be wrong. There are
separate scoring profiles for **precision, endurance, combat, strength, artistic** and
general dynamic sports. An archer is rewarded for **stability**; a boxer for **explosive
bursts**; a lifter for **depth and range**.

**4. It refuses to guess**
This mattered more than any feature. The engine will **not** produce a report if:

- fewer than 12 frames contain a detected person
- the person is visible in under 40% of frames
- there isn't enough measured movement data

In those cases it returns a clear error instead of a fabricated score. There is no
`Math.random()` anywhere in the scoring path — the same clip always produces the same
result.

---

## Features

**Athlete**
Live pose analysis with skeleton overlay · Video upload & analysis · Performance reports
with per-sport metrics · Progress tracking with AI verdicts · Career ladder & training plan ·
Shareable athlete card & badges · Coach directory with ratings · Leaderboard ·
Messaging · PDF report export · AI chatbot · Voice commands · Privacy Center

**Coach**
Squad dashboard · Athlete profiles & full report history · Recruiter Hub with AI Scout
picks · Head-to-head compare (radar chart) · Clickable analytics drill-downs ·
Public coach profile · Messaging

**Owner (private admin)**
Platform overview with 12 live stats · 14-day activity chart · Full athlete & coach tables ·
Unified activity log
_(Demonstrated in the video — credentials are not published.)_

---

## Sports supported — 35, across 8 categories

Only sports a phone camera can genuinely analyse are included. Water sports, winter
sports, equestrian and mind sports were **deliberately removed** rather than shipped with
unreliable results.

| Category | Sports |
|---|---|
| Team | Football, Basketball, Volleyball, Rugby, Hockey, Handball, Lacrosse, Sepak Takraw, Kabaddi |
| Combat | Boxing, Wrestling, MMA, Judo, Karate, Taekwondo, Fencing |
| Bat & Ball | Cricket, Baseball, Softball |
| Racquet | Tennis, Badminton, Table Tennis |
| Endurance | Athletics, Cycling |
| Precision | Archery, Shooting, Golf, Snooker/Pool, Billiards |
| Artistic | Gymnastics, Parkour, Climbing, Skateboarding, Breakdancing |
| Strength | Weightlifting |

Each category defines its own metric labels, "what the AI analysed" checks, drills for the
weakest metric, and sport-specific injury-watch zones.

---

## Privacy by design

**Videos never leave the device.** Pose analysis runs entirely in the browser; only the
resulting numbers are sent to the server. Most users are minors, so this was a
non-negotiable design decision — it also means scaling costs almost nothing, since no
server-side GPU is required.

Passwords are hashed with bcrypt (12 rounds). CSRF protection on every state-changing
request. Parameterised SQL. Rate-limited logins. Full data export and account deletion
from the Privacy Center. See `/privacy` and `/terms` on the live app.

---

## Tech stack

| Layer | Choice | Why |
|---|---|---|
| Backend | Flask (Python) | Small, readable, fast to iterate |
| Database | SQLite | Zero-config; a clear migration path to Postgres |
| Frontend | Vanilla JS, single HTML file | No build step, one request, works on slow connections |
| CV / AI | MediaPipe Pose Landmarker | Runs on-device, 33 landmarks, no server GPU |
| Charts | Chart.js · **PDF** jsPDF | Reports and exports |
| Hosting | Render (gunicorn) | Auto-deploy on push, free TLS |

**Scale:** 1,295 lines of backend · 3,830 lines of frontend · 31 REST endpoints ·
12 database tables · 159 JS functions · installable PWA with offline support.

---

## Run it locally

```bash
git clone https://github.com/manthandhanraj/AthletixAI.git
cd AthletixAI
pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:5000

**Demo accounts** (seeded automatically on first run)

| Role | Email | Password |
|---|---|---|
| Athlete | `arjun@athletix.ai` | `1234` |
| Coach | `coach@athletix.ai` | `1234` |

Or create your own account — signup works immediately.

**Optional environment variables** (create a `.env` file)

```
SECRET_KEY=<random string>
OWNER_EMAIL=<admin email>
OWNER_PASSWORD=<admin password>
SMTP_HOST=smtp.gmail.com
SMTP_PORT=465
SMTP_USER=<your email>
SMTP_PASS=<app password>
```

Without SMTP configured the app runs in dev mode and prints verification codes to the
terminal, so the full flow is testable offline.

---

## Honest limitations

Stating these plainly, because a tool that hides its limits can't be trusted:

1. **This is a rule-based biomechanics engine, not a trained neural network.** The pose
   estimation is real ML; the scoring formulas encode domain knowledge, not learned
   weights.
2. **The weights are not yet calibrated against ground truth.** Doing that properly needs
   100+ athletes' videos paired with real measured performance (timed sprints, measured
   jumps, coach ratings). Collecting that dataset is the next milestone.
3. **Best for relative progress, not absolute certification.** "Your speed improved 8% this
   month" is trustworthy. "Your speed is exactly 82/100" is a calibrated estimate.
4. **2D pose has limits.** Depth is unreliable and camera angle affects results, which is
   why the engine leans on symmetry and relative change rather than absolute distances.
5. **SQLite is single-writer.** Fine for hundreds of concurrent users; Postgres migration
   is planned before that becomes a ceiling.

---

## Roadmap

- **Deep cricket module** — bowling arm-angle analysis against the ICC 15° legality rule,
  run-up speed, front-foot landing
- **Calibration** — collect labelled athlete data and fit the scoring weights to it
- **Per-sport models** — replace shared formulas with sport-specific ones, one sport at a time
- Academy dashboards, native mobile app, regional languages

---

## Built by

**Archita Tripathi** & **Manthan Dhanraj**

Built with AI assistance (OpenAI Codex and other AI tools) — the architecture, product
decisions, debugging and deployment are ours.

---

_Free for athletes. Always._
