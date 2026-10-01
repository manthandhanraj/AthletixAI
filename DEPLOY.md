# Phase 4 — Go Live (Render, free)

## Step 1 — Code GitHub pe daalo
1. github.com pe naya repo banao (e.g. `athletixai`) — **Private** rakhna theek hai.
2. VS Code terminal me project folder ke andar:
       git init
       git add .
       git commit -m "AthletixAI"
       git branch -M main
       git remote add origin https://github.com/TERA-USERNAME/athletixai.git
       git push -u origin main
   (`.env` apne aap skip hoga — `.gitignore` me hai. Isko kabhi push mat karna.)

## Step 2 — Render pe deploy
1. render.com pe GitHub se sign up karo.
2. **New +** → **Web Service** → apna repo select karo.
3. Render `render.yaml` khud padh lega. Bas **Create** dabao.
4. 3-5 min me live link milega: `https://athletixai.onrender.com`

## Step 3 — Email ON karo (production)
Render dashboard → tumhari service → **Environment** → in 5 ko add karo:
    SMTP_HOST = smtp.gmail.com
    SMTP_PORT = 465
    SMTP_USER = athletixai0919@gmail.com
    SMTP_PASS = <16-char Gmail App Password>
    MAIL_FROM = AthletixAI <athletixai0919@gmail.com>
Save → auto redeploy → ab real emails jayenge.

(SECRET_KEY, FLASK_DEBUG=0, DB_PATH pehle se render.yaml me set hain.)

## Step 4 — Demo accounts judges ke liye (optional)

Judges ko live app me login karana ho to `DEMO_MODE` on karo. Ye do normal
accounts banata hai — ek athlete, ek coach. Dono ke paas **koi admin/owner
power nahi** hoti, aur password sirf environment me rehta hai.

> `SEED_DEMO=1` mat lagana. Wo production me jaan-boojh ke fail hota hai,
> kyunki uska password source code me likha hua hai.

### 4a — Password ko Render Secret File me daalo

Render dashboard → tumhari service → **Environment** → **Secret Files** →
**Add Secret File**:

- **Filename:** `.env`
- **Contents:**

      DEMO_PASSWORD=12345

**Add** → **Save Changes**. Render is file ko project root aur
`/etc/secrets/.env` dono jagah mount karta hai; app dono padhta hai, isliye
kuch aur karne ki zaroorat nahi.

Ye password **login page pe khule me dikhta hai**, isliye koi strength rule
nahi lagta — `12345` bhi chalega (bas khali na ho, aur 72 bytes se lamba
na ho). Secret File sirf isliye ki password git me na jaaye.

### 4b — Baaki teen normal env vars

Same page pe **Environment Variables** me:

    DEMO_MODE           = 1
    DEMO_ATHLETE_EMAIL  = demo.athlete@athletix.ai
    DEMO_COACH_EMAIL    = demo.coach@athletix.ai

Save → auto redeploy.

### 4c — Check karo

Deploy ke baad logs me ye dikhna chahiye (password kabhi print nahi hota):

    [INFO] Demo athlete account created: demo.athlete@athletix.ai
    [INFO] Demo coach account created: demo.coach@athletix.ai
    [INFO] Demo access is ON. Athlete: ...  Coach: ...  (password comes from
           DEMO_PASSWORD and is never printed)

Login page pe ab ek **Demo Access** box dikhega:

    Demo Athlete → demo.athlete@athletix.ai   [Try]
    Demo Coach   → demo.coach@athletix.ai     [Try]
    Password     → 12345

**Try** dabane pe sahi role (Athlete/Coach) select ho jata hai, email bhar
jata hai, aur cursor Password box me aa jata hai — judge ko sirf password
likhna hai.

Box sirf un accounts ke liye dikhta hai jo startup pe **sach me ban gaye**.
Agar `DEMO_PASSWORD` khali hai (ya koi account refuse hua), box nahi dikhega
— logs me `[FATAL]` line dekho.

**Sample data bhi aata hai.** `DEMO_MODE=1` wahi 12 sample athletes (Arjun
Singh, Priya Sharma, …, unke 6-6 reports ke saath) aur 3 sample coaches
(Coach Verma, Meera Iyer, Rajesh Khanna) bhi bana deta hai jo localhost pe
dikhte hain. Isse demo coach ko athletes list, leaderboard, compare,
analytics sab bhare hue milte hain. Logs me:

    [INFO] Demo sample roster: 15 sample profiles added (display only - nobody can sign in as them).

In 15 profiles me **koi login nahi kar sakta**. Unka password ek random
secret hai jo kahin save nahi hota. Unke email `@sample.athletix.invalid`
pe hain — ye domain kabhi register nahi ho sakta, isliye password reset bhi
impossible hai. Koi admin account nahi banta. Ye sample log site ke **sab
users** ko dikhenge (leaderboard, directory), sirf judges ko nahi.

### 4d — Password badalna ya demo band karna

- **Password rotate:** Secret File me `DEMO_PASSWORD` badlo → redeploy.
  Agle startup pe hash update ho jayega aur purane sessions apne aap invalid
  ho jayenge.
- **Band karna (judging ke baad):**
  1. `DEMO_MODE` ko `0` kar do (ya hata do) → redeploy. Demo box gayab.
  2. Render dashboard → service → **Shell** me ye ek command chalao:

         python -c "from athletix.bootstrap import remove_demo_data; remove_demo_data()"

     Ye dono demo logins aur saare 15 sample profiles (unke reports
     samet) hata deta hai. Sirf wahi rows chhuta hai jo DEMO_MODE ne banaye —
     real users, owner aur koi admin/owner account kabhi delete nahi hota.
     Output: `[INFO] Removed 15 sample profiles and 2 demo logins.`

  Step 1 pehle karna zaroori hai — warna agle startup pe sab dobara ban
  jayega.

### Kya guarantee hai

- Role hamesha `athlete`/`coach` — kabhi `admin`/`owner` nahi.
- `/api/admin/*` inke liye **403**.
- Agar demo email pe pehle se koi admin/owner account hai, app use **chhuti
  nahi** — warning print karke skip kar deti hai.
- Agar `DEMO_ATHLETE_EMAIL` aur `OWNER_EMAIL` same hue, app refuse kar deti
  hai.
- `DEMO_PASSWORD` missing hua to koi account nahi banta (app phir bhi
  normally chalti hai).
- Password public hai, isliye koi bhi visitor demo account se **password
  badal, email badal, account delete ya "sign out everywhere" nahi** kar
  sakta — ye sab **403** dete hain. Warna ek aadmi sab judges ke liye demo
  tod deta. Baaki features (profile edit, upload, reports, messages) khule
  hain.
- **DEMO_MODE band karne ke baad** ye lock hat jata hai aur accounts
  database me `12345` ke saath pade rehte hain. Isliye judging ke baad 4d
  wali `remove_demo_data()` command zaroor chalana.

## Step 5 — Domain (optional, ~Rs 800/saal)
1. Domain khareedo (Namecheap / GoDaddy / Hostinger) — e.g. athletixai.in
2. Render → Settings → **Custom Domain** → domain add karo.
3. Render jo CNAME dega, use apne domain provider ke DNS me daal do.
4. HTTPS Render khud lagata hai (free).

## Zaroori baatein
- **Free plan so jaata hai** 15 min inactivity ke baad; pehla request 30-50 sec le sakta hai. $7/mo plan me ye problem nahi.
- Data `/var/data/athletixai.db` pe save hota hai (persistent disk) — restart/redeploy pe **safe rahega**.
- Backup: kabhi-kabhi Render Shell se `athletixai.db` download kar lena.
