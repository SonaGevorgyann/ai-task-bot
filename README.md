# AI Task Bot

Create tasks from Telegram (text or voice) and manage them on a live dashboard called **To Do List**.

## What it does

- A text message becomes a task immediately.
- A voice message gets an instant “Transcribing…” reply. A worker turns the audio into text.
- Each person has a private board. `/board` sends a clickable **Open board** link, and the page shows only that person's tasks.
- The board updates live. Tasks can be shown as columns or as a list.
- Status buttons move a task between Pending, In Progress, and Completed. The bot messages the chat that created it.
- **Delete** asks you to confirm, then removes the task from that person's board.

## Architecture

- **Bot** talks to Telegram and calls the API.
- **API** (FastAPI) stores tasks and Telegram users in PostgreSQL. Each user has a private board token.
- **Redis** carries two queues (`transcription_jobs`, `notification_jobs`) and a pub/sub channel (`task_events`) for the dashboard.
- **Worker** downloads voice notes, transcribes them, and sends Telegram updates.
- **Dashboard** (React) loads one board and listens for that board's updates on Server-Sent Events at `/events`.

Speech-to-text uses an OpenAI-compatible endpoint. The sample env points at Groq Whisper.

## Run it

1. Copy the env file and fill in your own tokens:

```powershell
copy .env.example .env
```

`TELEGRAM_BOT_TOKEN` comes from BotFather. `STT_API_KEY` is a Groq or OpenAI speech-to-text key. The database values in the example are fine for local Docker. `DASHBOARD_URL` is the address the bot puts in the **Open board** link. Leave it as `http://127.0.0.1:3000` for a local run. Telegram does not make a `localhost` address clickable.

2. Start the stack:

```powershell
docker compose up --build
```

3. Message the bot. `/start` explains the commands and sends your board link. `/board` sends the link again. `/tasks` lists your latest tasks.

4. Tap **Open board** on the computer where Docker is running. The page is titled To Do List and lists only your tasks. Opening http://localhost:3000 without that link does not show anyone's tasks. A tap on a phone stays on the phone, because `127.0.0.1` is that phone.

API docs are at http://localhost:8000/docs.

## Dashboard

- Search, and filter by text or voice.
- Switch between Board and List.
- Move a card with Pending, In Progress, or Completed.
- Delete asks for **Delete task** or **Keep**.

A voice card says “Transcribing voice” until the worker finishes. If transcription fails, the card says it could not transcribe that note.

## Frontend only

Leave the API running (Docker publishes port 8000), then:

```powershell
cd frontend
npm install
npm run dev
```

Vite proxies `/api` to http://localhost:8000. Open http://localhost:5173. You still need the board link from the bot.
