# HabitSync AI - Phase 1 Prototype

A local single-user synchronization prototype that connects **Google Tasks**, **WhatsApp (via Twilio)**, and **Google Gemini** into a conversational accountability partner.

---

## 🛠 Project Structure

```
├── .venv/                      # Python Virtual Environment
├── config.py                   # Environment settings and validator
├── google_tasks_service.py     # Google Tasks OAuth & API synchronization
├── gemini_service.py           # Google Gemini conversational accountability agent
├── whatsapp_service.py         # Twilio WhatsApp messaging & reminder formatting
├── main.py                     # FastAPI server (WhatsApp inbound webhook & dashboard)
├── cli_test.py                 # Interactive CLI test suite for each feature
├── requirements.txt            # Python dependencies
├── .env.example                # Example environment configuration
└── README.md                   # Setup guide
```

---

## 🚀 Setup Guide (Step-by-Step)

### 1. Copy `.env.example` to `.env`
Create your `.env` file in this directory:
```bash
copy .env.example .env
```

---

### 2. Google Gemini API Key (Takes 1 Minute - Free)
1. Visit [Google AI Studio](https://aistudio.google.com).
2. Sign in with your Google account.
3. Click **"Get API key"** -> **"Create API key"**.
4. Paste the key in `.env`:
   ```ini
   GEMINI_API_KEY=AIzaSy...
   ```

---

### 3. Google Tasks OAuth Client ID (credentials.json)
To allow HabitSync AI to access and update your Google Tasks:
1. Go to [Google Cloud Console](https://console.cloud.google.com).
2. Create or select a project.
3. In the sidebar, go to **APIs & Services** -> **Library**.
4. Search for **"Google Tasks API"** and click **Enable**.
5. Go to **APIs & Services** -> **OAuth consent screen**:
   * User Type: **External**.
   * Fill in App Name (e.g. `HabitSync AI`) and your email.
   * Under **Publishing status**, keep it in **Testing** and add your own Gmail under **Test users**.
6. Go to **APIs & Services** -> **Credentials**:
   * Click **Create Credentials** -> **OAuth client ID**.
   * Application type: **Desktop app**.
   * Name: `HabitSync Desktop Client`.
   * Click **Create**.
7. Click the **Download JSON** icon on your newly created Client ID.
8. Rename the downloaded file to **`credentials.json`** and place it directly inside this project directory (`c:\Users\91789\Desktop\Gemini Personal Asistant\credentials.json`).

---

### 4. Twilio WhatsApp Sandbox (Free Testing)
1. Create a free account at [Twilio](https://www.twilio.com).
2. Go to **Messaging** -> **Try it out** -> **Send a WhatsApp message** (WhatsApp Sandbox).
3. Follow the instructions on the screen:
   * Send the code (e.g. `join <two-words>`) from your personal phone's WhatsApp to Twilio's WhatsApp number (`+1 415 523 8886`).
4. Copy your **Account SID** and **Auth Token** from the Twilio Console dashboard.
5. In `.env`:
   ```ini
   TWILIO_ACCOUNT_SID=AC...
   TWILIO_AUTH_TOKEN=your_auth_token
   TWILIO_WHATSAPP_NUMBER=whatsapp:+14155238886
   USER_WHATSAPP_NUMBER=whatsapp:+91XXXXXXXXXX
   ```

---

## 🧪 Testing with CLI (`cli_test.py`)

You can test each component independently using `.venv\Scripts\python.exe cli_test.py`:

```powershell
# 1. Check which credentials and files are ready
.venv\Scripts\python.exe cli_test.py --check

# 2. Authenticate Google Tasks (opens browser for 1-click Google login)
.venv\Scripts\python.exe cli_test.py --auth

# 3. View your pending tasks in Google Tasks
.venv\Scripts\python.exe cli_test.py --list

# 4. Add a test task to Google Tasks
.venv\Scripts\python.exe cli_test.py --add "AWS Daily Streak Check-in"

# 5. Test completing a task (marks it done in Google Tasks)
.venv\Scripts\python.exe cli_test.py --complete

# 6. Test Gemini conversational agent with your tasks context
.venv\Scripts\python.exe cli_test.py --gemini "What tasks should I do first today?"

# 7. Send an actual WhatsApp reminder to your phone
.venv\Scripts\python.exe cli_test.py --send-reminder

# 8. Simulate an incoming message without running a server
.venv\Scripts\python.exe cli_test.py --simulate "done"
.venv\Scripts\python.exe cli_test.py --simulate "I feel lazy today"
```

---

## 🌐 Running the Local Server (`main.py`)

Run the FastAPI local server:
```powershell
.venv\Scripts\python.exe main.py
```
* **Dashboard:** Open `http://localhost:8000` in your browser to view connection status and click to send reminders.
* **Tasks API:** Open `http://localhost:8000/tasks` to inspect your active tasks in JSON format.
* **Webhook Endpoint:** `POST http://localhost:8000/webhook/whatsapp` receives WhatsApp replies from Twilio.

### Hooking up Twilio Webhook (ngrok / Cloudflare Tunnel):
To receive real WhatsApp replies on your local machine:
1. Expose port 8000:
   ```bash
   ngrok http 8000
   ```
2. In Twilio Console -> **Messaging** -> **Settings** -> **WhatsApp Sandbox Settings**:
   * Set **WHEN A MESSAGE COMES IN** to: `https://<your-ngrok-url>/webhook/whatsapp` (HTTP POST).
3. Text **"done"** or chat with your bot on WhatsApp!

---

## ☁️ Deploying Free on Render (`render.yaml`)

1. Generate your single-line cloud env bundle locally:
   ```powershell
   .venv\Scripts\python.exe cli_test.py --export-cloud-env
   ```
   This writes a gitignored `.cloud-env.txt` containing `GOOGLE_TOKEN_JSON`, `GOOGLE_CREDENTIALS_JSON`, and your keys.
2. Push this repository to a **private** GitHub repository (secrets in `.env`, `credentials.json`, and `token.json` are excluded via `.gitignore`).
3. On [Render](https://dashboard.render.com), click **New +** -> **Blueprint** (or **Web Service**), connect your repo, and paste the env vars from `.cloud-env.txt`.
4. Stop your local `main.py` / `start_gui.bat` instance so Render holds the active Telegram webhook (`https://<your-app>.onrender.com/webhook/telegram`).
5. *(Recommended for 24/7 scheduler)* Create a free 5-minute monitor on [UptimeRobot](https://uptimerobot.com) pointing to `https://<your-app>.onrender.com/health` so Render stays awake.


