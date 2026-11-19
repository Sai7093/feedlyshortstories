# FeedlyShortStories — Free Blogger Autoblogging, Webhooks & SQLite Archive

A zero-server, no-credit-card automation system replacing IFTTT for **`https://feedlyshortstories.blogspot.com/`**:

* **GitHub Actions (`.github/workflows/autoblog.yml`)**: Automatically checks RSS feeds every 30 minutes (2,000 free mins/month on a private repo; unlimited on a public repo).
* **SQLite Storage & Deduplication (`data/blog.db` + `db.py`)**: Prevents duplicate posts across URLs/titles, enforces daily rate limits to avoid Blogger spam locks, and stores your post history.
* **Historical Blogger Importer (`import_backup.py`)**: Imports your Blogger `.xml` backup **or Google Takeout (`feed.atom` / `Takeout/` folder)** into SQLite and audits duplicate labels (`NDTV News - Special` vs `NDTV News- Special`).
* **Webhook Support (`repository_dispatch`)**: Trigger an immediate RSS run or publish a custom post from Google Colab, a mobile shortcut, or any HTTP client.
* **Google Colab Control Center (`colab_control.ipynb`)**: Search your 30,000+ archived posts, run dry-runs, import backups to Google Drive, and trigger webhooks.
* **Modern Magazine Blogger Theme (`theme/modern-theme.xml`)**: 2026 responsive magazine/Bento layout with live Dark/Light toggle, sticky frosted glass header (`/` search hotkey), live JSONP Trending ticker, `Surprise Me` random story launcher, Grid/List switcher, tabbed Quick Explorer sidebar, editorial text-card typography for stories without thumbnails, and recommended-next post cards. (Legacy `theme/improved-theme.xml` is also kept in `theme/`.)

---

## Step 1: Enable Blogger API v3 & Create OAuth Credentials (One-Time)

1. Open [Google Cloud Console](https://console.cloud.google.com/) and create a free project (e.g., `feedly-autoblog`).
2. Go to **APIs & Services → Library**, search for **Blogger API v3**, and click **Enable**.
3. Go to **APIs & Services → OAuth consent screen**:
   * Choose **External** → fill in App name + your email.
   * **Important:** Under **Publishing status**, click **Publish App** ("In Production"). If you leave it in *Testing*, Google expires refresh tokens every **7 days**. (For personal use, you don't need Google review—you just click *Advanced → Go to app* once when authorizing your own account.)
4. Go to **APIs & Services → Credentials → Create Credentials → OAuth client ID**:
   * Application type: **Desktop app**
   * Download the JSON file (save it locally as `client_secret.json` — it is ignored by `.gitignore`).

---

## Step 2: Generate Your Refresh Token & Blog ID

Install dependencies and run the helper on your PC:

```powershell
pip install -r requirements.txt
python get_refresh_token.py --client-secret-file client_secret.json
```

*(If running in Google Colab instead of Windows, add `--manual`.)*

It opens Google Sign-In once and prints your 4 values:
* `BLOGGER_CLIENT_ID`
* `BLOGGER_CLIENT_SECRET`
* `BLOGGER_REFRESH_TOKEN`
* `BLOGGER_BLOG_ID`

---

## Step 3: Import Your Existing Blogger Posts (Google Takeout or Blogger XML)

Importing your existing blog prevents duplicate RSS posts and builds your searchable SQLite archive:

1. Either download your content from **Google Takeout** (`Takeout/Blogger/Blogs/.../feed.atom`) **or** in Blogger Dashboard go to **Settings → Manage blog → Back up content** (`blog-backup.xml`).
2. Run locally (accepts either a `.xml`/`.atom` file or the extracted Takeout folder):
   ```powershell
   python import_backup.py path\to\Takeout --db data/blog.db --archive-db data/archive.db --label-report
   ```
   * `data/blog.db` stays lightweight (deduplication hashes + metadata + short snippets) so it fits cleanly inside GitHub.
   * `data/archive.db` (ignored by Git via `.gitignore`) stores full HTML bodies for local/Google Drive backup.

---

## Step 4: Test Locally with `--dry-run`

Preview what `feeds.json` will pick up without posting anything to Blogger:

```powershell
python autoblog.py --dry-run
python autoblog.py --stats
```

Edit `feeds.json` to enable/disable feeds, adjust `max_posts_per_day` (recommended: `15–30` to avoid Blogger spam locks), or expand `label_map`.

---

## Step 5: Push to GitHub & Add Secrets

1. Create a **Private** GitHub repository (2,000 free Actions mins/month—more than enough for 30-min checks) and push this folder:
   ```powershell
   git init
   git add .
   git commit -m "Initial Blogger autoblog setup"
   git branch -M main
   git remote add origin https://github.com/YOUR_USER/YOUR_REPO.git
   git push -u origin main
   ```
2. In your GitHub repo: **Settings → Secrets and variables → Actions → New repository secret**, add:
   * `BLOGGER_CLIENT_ID`
   * `BLOGGER_CLIENT_SECRET`
   * `BLOGGER_REFRESH_TOKEN`
   * `BLOGGER_BLOG_ID`
   * *(Optional)* `NOTIFY_WEBHOOK_URL` (Discord or Slack webhook URL for publish/error alerts)
3. Go to the **Actions** tab → **Blogger Autoblog & Webhook Runner** → **Run workflow** (try `dry_run: true` first!).

---

## Step 6: Trigger via Webhook (`repository_dispatch`)

Create a GitHub Personal Access Token (Fine-grained token with `Contents: Read and write` for this repo) and trigger runs from Colab, Make, shortcuts, or `curl`:

**Trigger RSS check immediately:**
```bash
curl -X POST https://api.github.com/repos/YOUR_USER/YOUR_REPO/dispatches \
  -H "Authorization: Bearer YOUR_GITHUB_PAT" \
  -H "Accept: application/vnd.github+json" \
  -d '{"event_type":"run-rss","client_payload":{"dry_run":false}}'
```

**Publish a custom story via Webhook:**
```bash
curl -X POST https://api.github.com/repos/YOUR_USER/YOUR_REPO/dispatches \
  -H "Authorization: Bearer YOUR_GITHUB_PAT" \
  -H "Accept: application/vnd.github+json" \
  -d '{"event_type":"publish-post","client_payload":{"title":"Sample Story","content":"<p>Story body here</p>","labels":["Short Stories"],"is_draft":true}}'
```

---

## Step 7: Upgrade Your Blogger Website Theme

* `theme/modern-theme.xml` *(Recommended — 2026 Modern Dynamic Theme)*: Ground-up responsive editorial theme built for news & short stories, featuring Dark/Light mode, live Trending ticker, Bento Hero + 2-column Grid/List toggle, tabbed Quick Explorer sidebar, automatic fallback styling for text-only posts, and mobile-first responsive cards.
* `theme/improved-theme.xml` *(Legacy Picture Window patch)*: Retains the classic 2010 layout with fixed contrast/width bugs.
* `theme/original-backup.xml`: Backup copy of your original template.

To apply the modern theme on an existing or brand-new Blogger blog:
1. In Blogger Dashboard go to **Theme → ⌄ → Restore → Upload** `theme/modern-theme.xml`.
2. Go to **Theme → ⌄ → Mobile settings** and select **Desktop** (this ensures mobile visitors `?m=1` get your modern responsive theme instead of Blogger's legacy mobile skin).
3. Go to **Settings → Posts → Max posts shown on main page** and set it to **`9`** or **`11`** (1 Bento Hero + evenly balanced 2-column rows).
