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

## Step 4 — Domain (optional, ~Rs 800/saal)
1. Domain khareedo (Namecheap / GoDaddy / Hostinger) — e.g. athletixai.in
2. Render → Settings → **Custom Domain** → domain add karo.
3. Render jo CNAME dega, use apne domain provider ke DNS me daal do.
4. HTTPS Render khud lagata hai (free).

## Zaroori baatein
- **Free plan so jaata hai** 15 min inactivity ke baad; pehla request 30-50 sec le sakta hai. $7/mo plan me ye problem nahi.
- Data `/var/data/athletixai.db` pe save hota hai (persistent disk) — restart/redeploy pe **safe rahega**.
- Backup: kabhi-kabhi Render Shell se `athletixai.db` download kar lena.
